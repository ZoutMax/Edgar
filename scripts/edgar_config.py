r"""Everything a different machine or a different person has to change.

Edgar's scripts read their settings from here, so nothing personal or
machine-specific is baked into the code. The file is optional: with no config at
all Edgar runs on sensible defaults.

    %LOCALAPPDATA%\Edgar\config.json        (or $EDGAR_HOME/config.json)

Unknown keys are ignored, missing keys fall back to the defaults below, and a
malformed file is reported once and then ignored - a typo in a config must never
stop the voice working.
"""
import json
import os

HOME = os.environ.get("EDGAR_HOME") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Edgar")
CONFIG_PATH = os.path.join(HOME, "config.json")

DEFAULTS = {
    # What Edgar calls you, in the greeting.
    "name": "",

    # Audio devices are matched on a FRAGMENT of the Windows device name, in
    # order of preference, so a headset that comes and goes is followed
    # automatically. Empty means "whatever Windows considers the default".
    "mic_prefs": [],
    "out_prefs": [],

    # faster-whisper model. small.en is the sweet spot on CPU: tiny mishears
    # too much, medium costs seconds per utterance for no measurable gain.
    "model": "small.en",

    # Whisper listens better when it knows roughly what to expect. Write this as
    # PROSE about the things you actually talk about - a bare word list biases
    # it badly. Names, hardware, games, drive letters, stock phrases.
    "vocab_hint": "",

    # edgar - the face on a screen of its own, with a male synthetic voice.
    # paky  - voice only, no face.
    "mode": "edgar",

    # Which monitor the face uses: left, right, middle, primary, or an index.
    # A WORD is safer - indices shift when Windows re-detects the displays.
    "monitor": "left",

    # Spoken the moment the talk button is released, so you know you were heard
    # without waiting for the transcription. "" switches it off.
    "ack_phrase": "ok",

    # Hold the middle mouse button at least this long for it to count as speech
    # rather than an ordinary middle-click.
    "hold_ms": 400,
}

_CACHE = None
_WARNED = False


def load():
    """The merged settings. Read once, then cached for the process."""
    global _CACHE, _WARNED
    if _CACHE is not None:
        return _CACHE
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except Exception as e:
        if not _WARNED:
            _WARNED = True
            print("edgar: ignoring %s (%s)" % (CONFIG_PATH, e))
    _CACHE = cfg
    return cfg


def get(key, fallback=None):
    value = load().get(key, DEFAULTS.get(key))
    if value in ("", [], None) and fallback is not None:
        return fallback
    return value
