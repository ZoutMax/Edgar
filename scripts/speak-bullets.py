"""
Stop hook for Claude Code: speak the white-dot bullet lines of the last
assistant message, using Kokoro neural TTS.

Replaces the earlier SAPI/Zira version. Kokoro runs fully offline from
its own virtual environment, and the output device is chosen by name from the
Windows default output device (the monitor) is irrelevant.

Fails silently but always logs, so "did not fire" is distinguishable from
"fired and found nothing".
"""
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime

# A sibling module, so running any script directly finds it. It resolves
#   HOME  runtime state: logs, the face's state file, the acknowledgement
#         audio, the model cache and the recording history
#   CFG   the user's settings, or defaults when there is no config file
# and EDGAR_DIR is simply where these scripts were installed.
# python -I deliberately keeps the script directory OFF sys.path, and every
# hook and scheduled task runs with -I, so the sibling import needs it back.
# This adds OUR OWN directory, never the working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edgar_config

EDGAR_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = edgar_config.HOME
CFG = edgar_config.load()
STATE = os.path.join(HOME, ".speak-last-uuid")   # uuid of the last message handled

# Two races to beat.
#  1. The Stop hook fires in the same instant a message is appended, so reading
#     "the last assistant message" at t=0 gets the PREVIOUS turn's text.
#  2. A turn is often several assistant messages: a short lead-in, tool calls,
#     then the real answer. Taking the first unseen uuid speaks the lead-in and
#     marks the turn done, so the actual reply is never read.
# So: wait for the newest uuid to stop CHANGING for SETTLE_SECONDS, and treat
# that settled message as the turn's final one.
WAIT_SECONDS = 30.0
# Measured 2026-10-17: the final message lands within ~1 s of the hook firing, so a 3 s
# settle was ~2 s of pure waiting - half the total delay before the first word. 1.2 s is
# still 6x the poll interval, and .speak-last-uuid stops a mis-grab repeating.
SETTLE_SECONDS = 0.6    # was 1.2. The voice arrived too late and the user had
                        # started talking by then. The final message lands within
                        # ~1 s, so 0.6 still settles it while halving the wait.
POLL_SECONDS = 0.2
KOKORO = os.path.join(HOME, "kokoro")
MODEL = os.path.join(KOKORO, "models", "kokoro-v1.0.onnx")
VOICES = os.path.join(KOKORO, "models", "voices-v1.0.bin")
LOG = os.path.join(HOME, "speak-bullets.log")

VOICE = "af_bella"      # highest published grade of any Kokoro female voice:
                        # Overall A-, trained on 10-100 h. Next best are
                        # af_nicole and bf_emma at B-. See speak.py --list-voices
SPEED = 0.92            # The user's pick 2026-10-17 (option 3 of 4). Kokoro is TRAINED at
                        # speed 1.0, so every step away from it stretches the words and adds
                        # artifacts - keep it near 1.0 and get the unhurried feel from the
                        # pauses below instead. Don't go below ~0.8; it drawls.
# Kokoro has NO pitch control - create() takes only text/voice/speed/lang/is_phonemes/
# trim/sentence_pause/clause_pause/continuous. Pitch is a property of the chosen voice.
# Faking it needs resampling (drags speed along) or a phase vocoder (quality loss).
SENTENCE_PAUSE = 0.45   # gap between sentences (default 0.25)
CLAUSE_PAUSE = 0.20     # gap at commas (default 0.10)
# Output device preference, first match wins. Both a headset and a speakerphone expose a
# WASAPI endpoint that accepts 48000 Hz ONLY, so the exact 2x upsample works for either.
# Headset first (the user's choice 2026-10-17); the speakerphone as the fallback when they are not paired,
# so a turn is never silent just because the earbuds are in their case.
DEVICE_PREFS = tuple(CFG["out_prefs"])
LATENCY = 'low'         # WASAPI's own default, ~3 ms. A control test on 2026-10-17 proved the
                        # buffer was NEVER the cause of the crackle: after 0.003 / 'high' / 0.3 /
                        # 0.5 / 1.0 / 2.0 all seemed to "improve" it, going straight back to the
                        # 3 ms default was clean. The real fixes were WASAPI instead of
                        # DirectSound, the exact 2x upsample to 48 kHz, and PAD_SECONDS.
                        # Don't chase buffer size if crackle ever returns.
                        # Do NOT replace sd.play with an explicit mono OutputStream -
                        # WASAPI wants stereo here and the result is incomprehensible;
                        # sd.play does the channel mapping for us.
PAD_SECONDS = 0.15      # silence either side, or the first/last word gets cut

# Kokoro renders quietly - peaks land around 0.49-0.69 where 1.0 is full scale, so
# there is 3-6 dB of headroom going to waste. Peak-normalise every utterance, then
# soft-limit with tanh to lift the RMS further without hard clipping.
# Measured: raw rms 0.080 -> normalised 0.139 -> +2x soft limit 0.233 (~3x as loud).
GAIN_PEAK = 0.95        # normalise each utterance's peak to this
GAIN_BOOST = 1.0        # The user's pick 2026-10-17 after 2.0 / 1.4 / 1.2 / 1.0. At 1.0 the
                        # soft limiter is bypassed entirely, so it is peak-normalisation only
                        # (rms ~0.129 vs 0.233 at boost 2.0) - the cleanest option, since no
                        # limiting means no compression of the dynamics at all.
BULLET = "\u2022"

# Reading a whole reply aloud took 130 s for something the user reads in ~20 s -
# an audiobook trailing behind them, not a conversation. So:
#   * if the reply contains white-dot lines, speak ONLY those - they are the
#     deliberately-spoken version, written as speech rather than as a document;
#   * otherwise fall back to the opening prose, capped short, so a reply that
#     forgot its spoken lines still says something useful.
# The requirement: when the prompt is handed back, say a short summary of what
# was done. So: one brief spoken summary at the end of a turn, nothing more.
MAX_TOTAL = 1800        # ceiling for the prose fallback
SPOKEN_CAP = 420        # ~25 s at SPEED - a resume, not a reading
SPOKEN_LINES = 2        # at most two sentences
FALLBACK_LINES = 1      # if a reply forgot its summary, read just its opening

# --- live narration (PreToolUse) -------------------------------------------
# Stop fires only when the turn ends, so it can never narrate work in progress.
# PreToolUse fires as each tool is invoked, and the prose written just before a
# tool call is already final in the transcript - no settle wait needed, only a
# short poll in case the write has not flushed yet.
LIVE_LINES = 1          # one line while working: a heads-up, not a report
LIVE_CAP = 240
LIVE_WAIT = 2.0         # how long to wait for a new message to appear
LOCK = os.path.join(HOME, ".speak-lock")
LOCK_WAIT_LIVE = 2.0    # busy? skip, so narration stays current instead of queueing
LOCK_WAIT_STOP = 45.0   # the end-of-turn summary is worth waiting for
LOCK_STALE = 120.0      # a crashed run must not mute speech forever

# The user holds the middle mouse button to talk. voice-input.py raises this flag
# the instant the button goes down, and Edgar must then STOP - mid-word if need
# be. Speaking over them is worse than not finishing the sentence.
TALKING_FLAG = os.path.join(HOME, ".user-talking")
TALK_POLL = 0.05        # how often to check while playing

# speak-daemon.py keeps the voice model loaded. Hooks hand their lines to it and
# exit; if it is not running they speak in-process exactly as before.
DAEMON_HOST, DAEMON_PORT = "127.0.0.1", 51247


def send_to_daemon(lines, emotion=None, skip_if_busy=False):
    """True if the daemon accepted the lines."""
    import socket
    try:
        with socket.create_connection((DAEMON_HOST, DAEMON_PORT), timeout=1.0) as c:
            payload = {"lines": list(lines), "emotion": emotion,
                       "skip_if_busy": bool(skip_if_busy)}
            c.sendall((json.dumps(payload) + chr(10)).encode("utf-8"))
            c.settimeout(3.0)
            reply = c.recv(32).decode("utf-8", "replace").strip()
        if reply == "ok":
            return True
        log("daemon replied %r" % reply)
        return reply == "busy"        # busy is a deliberate skip, not a failure
    except Exception:
        return False


def rescan_devices(sd):
    """PortAudio snapshots the device list when the process starts. The daemon
    is long-lived, so moving the speakerphone to another USB port left it with stale
    channel counts and sample rates - WASAPI then refused and playback fell back
    to DirectSound, which sounds like distortion. Re-initialising refreshes it."""
    try:
        sd._terminate()
        sd._initialize()
        log("  rescanned audio devices")
        return True
    except Exception as e:
        log("  device rescan failed: %s" % e)
        return False


def user_is_talking():
    return os.path.exists(TALKING_FLAG)


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], msg))
    except Exception:
        pass


def newest_assistant(path):
    """(uuid, text) of the newest assistant text message, or (None, None)."""
    if not path or not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return None, None
    for line in reversed(lines):
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            o = json.loads(s)
        except Exception:
            continue
        if o.get("type") != "assistant":
            continue
        parts = [c["text"] for c in (o.get("message", {}).get("content") or [])
                 if c.get("type") == "text" and c.get("text")]
        if parts:
            return o.get("uuid"), "\n".join(parts)
    return None, None


def acquire_lock(wait_s):
    """One speaker at a time. Returns True if we hold the lock."""
    deadline = time.time() + wait_s
    while True:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK) > LOCK_STALE:
                    log("stealing a stale lock")
                    os.unlink(LOCK)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(0.15)
        except Exception as e:
            log("lock error: %s" % e)
            return True          # never let a lock bug silence speech entirely


def release_lock():
    try:
        os.unlink(LOCK)
    except Exception:
        pass


def wait_for_new_message(path):
    """Live mode: the newest assistant text whose uuid we have not spoken yet.
    The message carrying a tool_use is complete by definition, so there is
    nothing to settle - we only poll in case the transcript write is late."""
    seen = read_state()
    deadline = time.time() + LIVE_WAIT
    while True:
        uuid, text = newest_assistant(path)
        if uuid and uuid != seen:
            return uuid, text
        if time.time() >= deadline:
            return None, None
        time.sleep(POLL_SECONDS)


def read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def write_state(uuid):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            f.write(uuid or "")
    except Exception:
        pass


def wait_for_final_message(path):
    """The turn's LAST assistant message: newest unseen uuid that has stopped
    changing for SETTLE_SECONDS. Returns (uuid, text) or (None, None)."""
    seen = read_state()
    deadline = time.time() + WAIT_SECONDS
    cand_uuid = cand_text = None
    cand_since = 0.0

    while time.time() < deadline:
        uuid, text = newest_assistant(path)
        if uuid and uuid != seen:
            if uuid != cand_uuid:
                cand_uuid, cand_text = uuid, text      # a newer one appeared
                cand_since = time.time()
            elif time.time() - cand_since >= SETTLE_SECONDS:
                return cand_uuid, cand_text            # settled - this is the last
        time.sleep(POLL_SECONDS)

    # hit the overall cap; speak the best candidate we saw rather than nothing
    return (cand_uuid, cand_text) if cand_uuid else (None, None)


# ---------------------------------------------------------------------------
# Speech normalisation. Kokoro reads raw technical text literally - "GAIN_BOOST"
# becomes "gain underscore boost", "E:\\Code\\x" becomes "E colon backslash...",
# "48000 Hz" becomes "forty eight thousand H Z". None of that sounds human, so
# rewrite it into words BEFORE synthesis. The best fix is still to write the
# spoken lines as speech in the first place; this is the safety net.
# ---------------------------------------------------------------------------

_UNITS = [
    (r"\bkHz\b", "kilohertz"), (r"\bMHz\b", "megahertz"), (r"\bGHz\b", "gigahertz"),
    (r"\bHz\b", "hertz"),
    (r"\bGB/s\b", "gigabytes per second"), (r"\bMB/s\b", "megabytes per second"),
    (r"\bMbps\b", "megabits per second"), (r"\bGbps\b", "gigabits per second"),
    (r"\bTB\b", "terabytes"), (r"\bGB\b", "gigabytes"), (r"\bMB\b", "megabytes"),
    (r"\bKB\b", "kilobytes"),
    (r"\bms\b", "milliseconds"),
    (r"(?<=\d)\s*s\b", " seconds"),
    (r"\bC\b(?=\s|$)", "degrees"),
]

_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
    12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen",
    16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen",
    20: "twenty", 30: "thirty", 40: "forty", 50: "fifty",
    60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety",
}


def _int_to_words(n):
    if n < 0:
        return "minus " + _int_to_words(-n)
    if n in _WORDS:
        return _WORDS[n]
    if n < 100:
        return _WORDS[n // 10 * 10] + " " + _WORDS[n % 10]
    if n < 1000:
        head = _WORDS[n // 100] + " hundred"
        return head if n % 100 == 0 else head + " and " + _int_to_words(n % 100)
    for div, name in ((1_000_000_000, "billion"), (1_000_000, "million"), (1000, "thousand")):
        if n >= div:
            head = _int_to_words(n // div) + " " + name
            return head if n % div == 0 else head + " " + _int_to_words(n % div)
    return str(n)


def _decimal_to_words(m):
    whole, frac = m.group(1), m.group(2)
    out = _int_to_words(int(whole)) if whole not in ("", "0") else "nought"
    digits = " ".join(_WORDS[int(d)] for d in frac)
    return "%s point %s" % (out, digits)


def speechify(text):
    """Rewrite written text into something that reads aloud naturally."""
    t = text

    # file paths and URLs say nothing useful aloud - drop them
    t = re.sub(r"[A-Za-z]:\\[^\s,;:]+", "a file path", t)
    t = re.sub(r"https?://[^\s,;:]+", "a link", t)

    # SNAKE_CASE / snake_case identifiers -> spaced words
    t = re.sub(r"\b([A-Za-z]+(?:_[A-Za-z0-9]+)+)\b",
               lambda m: m.group(1).replace("_", " ").lower(), t)

    # units before numbers, so "48000 Hz" is handled as a unit not a bare int
    for pat, rep in _UNITS:
        t = re.sub(pat, rep, t)

    # leading +/- on measurements reads as punctuation
    t = re.sub(r"(?<![\w])\+(?=\d)", "", t)

    # decimals, then plain integers
    t = re.sub(r"\b(\d*)\.(\d+)\b", _decimal_to_words, t)
    t = re.sub(r"\b\d+\b", lambda m: _int_to_words(int(m.group(0))), t)

    # tidy whitespace
    return re.sub(r"\s{2,}", " ", t).strip()


def _unreadable(s):
    """True for lines that are screen furniture rather than speech."""
    if s.startswith("|") or s.startswith(">"):
        return True                                  # table row / blockquote rule
    if re.match(r"^[\s\-=_*|+:]+$", s):
        return True                                  # rules and table separators
    if re.search(r"[A-Za-z]:\\|https?://|\\\\", s):
        return True                                  # paths and URLs read horribly
    letters = sum(c.isalpha() or c.isspace() for c in s)
    if len(s) > 3 and letters / len(s) < 0.65:
        return True                                  # key=value / symbol dumps
    words = s.split()
    if len(words) < 3:
        return True                                  # labels, not sentences
    return False


def spoken_lines(text, max_lines=None, cap=None):
    """White-dot lines only: the deliberately-spoken version of the reply."""
    max_lines = SPOKEN_LINES if max_lines is None else max_lines
    cap = SPOKEN_CAP if cap is None else cap
    out, total = [], 0
    for raw in text.split("\n"):
        t = raw.strip()
        if not t.startswith(BULLET):
            continue
        s = t[1:].strip()
        s = re.sub(r"`([^`]*)`", r"\1", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", s)
        s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
        s = s.replace("**", "").strip()
        if not s:
            continue
        if total + len(s) > cap or len(out) >= max_lines:
            break
        out.append(s)
        total += len(s)
    return out


def choose_lines(text, max_lines=None, cap=None):
    """Spoken lines if the reply offers them, else a short prose opening."""
    say = spoken_lines(text, max_lines, cap)
    if say:
        return say, "spoken"
    return prose_lines(text)[:(max_lines or FALLBACK_LINES)], "prose-fallback"


def prose_lines(text):
    """Speakable prose from a markdown reply, in order."""
    out, total, in_fence = [], 0, False
    for raw in text.split("\n"):
        t = raw.strip()
        if t.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not t:
            continue

        # Inline formatting FIRST: stripping list markers before bold would eat
        # the leading asterisk of a line that opens with **bold**, leaving the
        # closing ** behind ("*Run 1** picked up...").
        s = t
        s = re.sub(r"`([^`]*)`", r"\1", s)                    # code spans
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)              # bold
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", s)     # italics
        s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)        # links -> label
        s = re.sub(r"^#{1,6}\s+", "", s)                      # heading markers
        # marker must be followed by space, so "**bold" is never mistaken for one
        s = re.sub(r"^(?:[-*+]\s+|\d+\.\s+|" + BULLET + r"\s*)", "", s)
        s = s.replace("**", "").strip()                       # any unpaired leftovers

        if not s or _unreadable(s):
            continue
        if not re.search(r"[.!?:]$", s):
            s += "."                                          # help the prosody
        if total + len(s) > MAX_TOTAL:
            out.append("There is more on screen than I will read out.")
            break
        out.append(s)
        total += len(s)
    return out


def main():
    live = "--live" in sys.argv
    log("FIRED%s" % (" (live)" if live else ""))
    raw = sys.stdin.read()
    if not raw.strip():
        log("no stdin - exiting")
        return 0
    hook = json.loads(raw)
    tp = hook.get("transcript_path")
    log("transcript: %s" % tp)

    # Hold the lock across BOTH the uuid claim and the speech. Claiming inside the
    # lock is what stops two overlapping runs reading the same message, and in
    # live mode a busy lock means we skip rather than queue - stale narration
    # arriving after the work is finished is worse than silence.
    if not acquire_lock(LOCK_WAIT_LIVE if live else LOCK_WAIT_STOP):
        log("another run is speaking - skipping")
        return 0
    try:
        if live:
            uuid, text = wait_for_new_message(tp)
            if not uuid:
                log("nothing new within %.1fs - staying quiet" % LIVE_WAIT)
                return 0
        else:
            uuid, text = wait_for_final_message(tp)
            if not uuid:
                log("no new assistant message settled within %.0fs - nothing to say"
                    % WAIT_SECONDS)
                return 0
        write_state(uuid)      # claim it even if it has no bullets, so it is never re-read
        log("message: %s" % uuid)

        say, mode = choose_lines(text, LIVE_LINES if live else None,
                                 LIVE_CAP if live else None)
        log("%s: %d lines, %d chars" % (mode, len(say), sum(len(s) for s in say)))
        if not say:
            return 0
        # Hand it to the warm daemon if it is up; otherwise speak here as before.
        if send_to_daemon(say, skip_if_busy=live):
            log("handed to the daemon")
            return 0
        return speak(say)
    finally:
        release_lock()


EDGAR_STATE = os.path.join(HOME, "edgar-state.json")
EDGAR_HZ = 50               # 20 ms frames

# --------------------------------------------------------------------------- modes
# Two personas, switched by the user saying "switch to edgar" / "switch to paky".
# Claude writes the mode file; everything else just reads it, so the switch
# survives restarts and reboots without touching any code.
#   edgar - the Electric Dreams face on the left screen, and a male voice
#   paky  - no face at all, the af_bella voice this was built with
MODE_FILE = os.path.join(HOME, "voice-mode.txt")
DEFAULT_MODE = "edgar"

# Edgar's voice is am_puck (the user picked it from 9) through the "little robot"
# chain. The point is that no RAW voice can sound like Edgar -
# he is a man through 1984 processing, so the machine goes on top:
#   pitch 1.10  smaller, and the synthesis is slowed by the same factor so the
#               resample that raises the pitch leaves the DURATION unchanged
#   band        350-3500 Hz, a small enclosed speaker - no deep body, no air
#   ring        70 Hz at 0.20 depth, the metallic buzz of period speech synthesis
MODES = {
    "edgar": {"voice": "am_puck", "speed": SPEED, "face": True,
              "robot": {"pitch": 1.10, "band": (350, 3500), "ring": (70.0, 0.20)}},
    "paky":  {"voice": VOICE,    "speed": SPEED, "face": False, "robot": None},
}


def edgarize(samples, rate, robot):
    """Turn a human voice into a small machine. Returns the processed audio."""
    import numpy as np
    from scipy.signal import butter, sosfilt, resample_poly
    if not robot:
        return samples
    x = samples.astype(np.float32)

    pitch = robot.get("pitch", 1.0)
    if abs(pitch - 1.0) > 1e-3:
        x = resample_poly(x, 1000, int(round(1000 * pitch))).astype(np.float32)

    lo, hi = robot.get("band", (0, 0))
    if lo and hi < rate / 2:
        sos = butter(4, [lo / (rate / 2), hi / (rate / 2)], btype="band", output="sos")
        x = sosfilt(sos, x).astype(np.float32)

    freq, depth = robot.get("ring", (0, 0))
    if depth:
        t = np.arange(len(x)) / float(rate)
        x = (x * (1.0 - depth + depth * np.sin(2 * np.pi * freq * t))).astype(np.float32)

    return x


def current_mode():
    try:
        with open(MODE_FILE, encoding="utf-8") as f:
            m = f.read().strip().lower()
        if m in MODES:
            return m
    except Exception:
        pass
    return DEFAULT_MODE

# Edgar's three faces, straight from the film stills: happy, neutral, not happy.
# The expression is taken from what is actually being said, so the face agrees
# with the words - a frown when something broke, a smile when it worked.
# My own mistake is the one thing that should always reach his face.
_MISTAKE = re.compile(
    "(?<![A-Za-z])(my (fault|mistake|error|own doing)|on my part|i broke|"
    "i was wrong|i got it wrong|miscount(ed)?|my own (mistake|error)|"
    "i had it wrong|i sliced)(?![A-Za-z])", re.I)

# Quoted and parenthesised spans are QUOTATIONS - log output, rule names, his
# own words - not Edgar reporting on his own state. "the three rules all behave
# (my own mistake wins)" is good news that merely NAMES the mistake rule.
# Blanked rather than deleted, so every match offset still lines up.
_QUOTED = re.compile(r"\([^()]{0,200}\)|\"[^\"]{0,200}\"|`[^`]{0,200}`")

# A line that states its own resolution cannot be angry. THIS is the fix: I
# report a success and name the problem it solved in the same breath, and the
# old rule read the problem. "I also fixed a bug", "Settings all preserved",
# "came through exactly right, which failed badly last night".
_RESOLVED = re.compile(
    r"\b(fixed|saved|preserved|works|worked|proved|proves|proven|confirmed|"
    r"verified|restored|resolved|passed|passes|intact|survived|"
    r"is done|are fine|is fine|all behave|compile clean|exactly right|"
    r"came through|in place|no longer|duplicate-safe|byte-identical)\b"
    r"|\b(cannot|can'?t|could not|will not|won'?t|never)\s+\w+"
    r"(?:\s+\w+){0,3}\s+again\b", re.I)

# A negative about how things USED to be is history, not news. It must sit
# NEXT TO the negative - "broke before", "failed last night" - because applied
# to the whole sentence it excused "was wrong because it ran code from before",
# which is a live problem report.
_HISTORIC = re.compile(
    r"\b(before|last night|earlier|previously|used to|up to now|until now)\b",
    re.I)

# A sentence about what COULD happen is narration, not a report of it.
_HYPOTHETICAL = re.compile(
    "(?<![A-Za-z])(if|would|could|risk|in case|otherwise|means that|so that|"
    "rather than|instead of)(?![A-Za-z])", re.I)

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _last_hits(sentence):
    """Where each sentiment LAST appears in one sentence."""
    out = []
    for name, rx in (("angry", _UNHAPPY), ("sad", _SAD), ("happy", _HAPPY)):
        last = None
        for last in rx.finditer(sentence):
            pass
        if last:
            out.append((last.start(), name))
    return out


_UNHAPPY = re.compile(
    r"\b(fail(ed|s|ure)?|error|broke|broken|crash(ed)?|wrong|bug|defects?|"
    # the negations must cover BOTH "couldn't" and "could not" - matching only
    # the contraction let "it did not submit" through as a smile
    r"cannot|can(?:'?t|\s+not)|could(?:n'?t|\s+not)|did(?:n'?t|\s+not)|"
    r"does(?:n'?t|\s+not)|would(?:n'?t|\s+not)|"
    r"not\s+(work|working|found|able|usable|safe)|unsafe|missing|regression|"
    r"refused?|blocked|denied|corrupt(ed)?|stuck|timed? ?out|"
    r"no\s+(usable|such|match)|unable|invalid|mistake|my fault|sorry)\b", re.I)

_HAPPY = re.compile(
    r"\b(work(s|ing|ed)?|fixed|done|complete(d)?|success(ful)?|confirmed|"
    r"verified|passed|correct(ly)?|good|great|ready|live|resolved|sorted|"
    r"all set|up and running|no errors?|clean|recovered|intact|preserved|"
    r"harmless(ly)?|prove[ds]?|proven|holds|in place|unchanged|safe|"
    # PHRASES, never a bare "ok": "the ok now fires on release" is narration.
    r"everything is ok|everything is fine|all ok|looks ok|seems ok|all good|"
    r"all fine|nothing lost|nothing broken)\b", re.I)


# One emotion drives the voice AND the face together. Pitch is the main carrier:
# a small machine goes up when pleased and drops when it is not, and the ring
# modulation gets harsher with the mood.
EMOTIONS = {
    "angry":   {"pitch": 1.02, "speed": 1.00, "ring": (88.0, 0.30), "face": "angry"},
    "sad":     {"pitch": 1.06, "speed": 0.94, "ring": (78.0, 0.25), "face": "sad"},
    "neutral": {"pitch": 1.10, "speed": 0.92, "ring": (72.0, 0.21), "face": "neutral"},
    "happy":   {"pitch": 1.16, "speed": 0.98, "ring": (68.0, 0.17), "face": "happy"},
}
DEFAULT_EMOTION = "neutral"      # the smiling still from the film


# The top of the ladder is for a real result, not routine good news.
# Word boundaries written as lookarounds ON PURPOSE: a backslash-b here was
# turned into a literal backspace (0x08) by the heredoc that wrote it, and the
# pattern then matched nothing at all. These forms contain no backslash.


# SAD is a LOST CONNECTION - a device gone, a thread of attention dropped -
# where ANGRY is an outright error or my own mistake.
#
# It used to list bare "lost", "gone", "empty" and "nothing found", which made
# a successful cleanup ("Archives gone - 2232 to 2211 MB") and a design
# improvement ("The oval is gone, now there is one shape") both read as grief.
# Measured on real traffic, that list produced 16 sad lines of which 10 were
# wrong. Narrowed, it produces 6, and they are the earbuds going flat.
_SAD = re.compile(
    "(?<![A-Za-z])(went offline|is offline|dropped out|disconnect(ed|s|ing)?|"
    "gone to sleep|lost track|out of range|unavailable|stopped answering|"
    "no longer responding|timed? ?out|stopped working)(?![A-Za-z])", re.I)


def emotion_for(text):
    """The emotion of a line, from five rules and nothing more.

    MEASURED, on 45 hand-labelled lines drawn from what Edgar has actually
    said - not on invented examples. The previous version scored 49% on that
    set while scoring 32/34 on a curated one, which is the whole lesson: it
    was tuned on text chosen to exercise it rather than on real traffic.
    This scores 91%, and every one of its remaining errors is a neutral line
    picking up a mild expression, never good news delivered with a scowl.

    The failure it exists to fix: I report a success and name the problem it
    solved in the same breath. "I also fixed a bug", "All three are
    duplicate-safe now", "38 memories, 0 broken links" - the old rule read
    the problem and put a scowl on every one.

    1. My OWN mistake anywhere wins. The user should always see that one.
    2. A lost connection is sad.
    3. If the line states its own RESOLUTION, a negative in it is the problem
       being solved, so the line is good news.
    4. Otherwise the LAST sentence carrying sentiment decides, and within it
       the LAST match - a report's verdict sits at its end - except that a
       negative immediately followed by "before" or "last night" is history.
    5. A sentence that EXPLAINS a risk carries no sentiment at all:
       "if the hook dies the button stops working" is narration, not a failure.

    Quotations are blanked before any of this, so log output and rule names
    cannot speak for Edgar.
    """
    text = _QUOTED.sub(lambda m: " " * len(m.group(0)), text or "")
    if _MISTAKE.search(text):
        return "angry"
    if _SAD.search(text):
        return "sad"
    resolved = bool(_RESOLVED.search(text))
    for sentence in reversed([s for s in _SENTENCE.split(text) if s.strip()]):
        if _HYPOTHETICAL.search(sentence):
            continue                    # explaining a risk, not reporting one
        hits = [(p, n) for p, n in _last_hits(sentence) if n != "sad"]
        if not hits:
            continue
        pos, name = max(hits)
        if name != "angry":
            return name
        if resolved:
            return "happy"              # the problem is named, but it is solved
        tok = ""
        for m in _UNHAPPY.finditer(sentence):
            if m.start() == pos:
                tok = m.group(0)
        if _HISTORIC.search(sentence[pos + len(tok):pos + len(tok) + 45]):
            return "neutral"            # it WAS broken; that is not now
        return "angry"
    return DEFAULT_EMOTION


def write_edgar_state(samples, rate, text="", speaking=True, expression=None):
    """RMS envelope of the audio about to play, for edgar.py's mouth.

    Written atomically (temp + replace) so Edgar never reads a half-written file
    at 50 fps. Failure here must never stop speech, hence the blanket except.
    """
    try:
        import numpy as np
        if speaking:
            step = max(1, int(rate / EDGAR_HZ))
            n = len(samples) // step
            frames = samples[:n * step].reshape(n, step)
            env = np.sqrt((frames.astype(np.float32) ** 2).mean(axis=1))
            peak = float(env.max()) if n else 0.0
            if peak > 1e-6:
                env = env / peak                     # normalise: mouth uses 0..1
            payload = {"speaking": True, "start": time.time(), "hz": EDGAR_HZ,
                       "env": [round(float(v), 3) for v in env], "text": text,
                       "expression": expression or EMOTIONS[DEFAULT_EMOTION]["face"]}
        else:
            payload = {"speaking": False}
        tmp = EDGAR_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, EDGAR_STATE)
    except Exception as e:
        log("edgar state write failed: %s" % e)


_KOKORO = None
_SPOKEN_ONCE = False


def get_kokoro():
    """Loaded once per process. In a one-shot hook run that is the same as
    before; inside speak-daemon.py it stays warm, which is the whole point -
    re-reading the 310 MB model was about 4 s of the delay before Edgar spoke."""
    global _KOKORO
    if _KOKORO is None:
        from kokoro_onnx import Kokoro
        _KOKORO = Kokoro(MODEL, VOICES)
    return _KOKORO


def speak(say, emotion=None):
    """emotion forces one face for every line - a greeting should smile,
    not be classified from its words."""
    mode = current_mode()
    cfg = MODES[mode]

    import numpy as np
    import sounddevice as sd
    from scipy.signal import resample_poly
    from kokoro_onnx import Kokoro

    # Kokoro emits 24 kHz. The speakerphone's DirectSound endpoint runs at 44100 and
    # resamples 24k -> 44.1k itself, which is audibly noisy (DirectSound is a
    # legacy path with a crude resampler and 120 ms of latency). Its WASAPI
    # endpoint accepts 48000 ONLY - which is an exact 2x of 24 kHz, so we can
    # upsample cleanly ourselves and hand the device its native rate. No
    # fractional resampling anywhere, and latency drops from 120 ms to 3 ms.
    # Enumerate the device list ONCE - sd.query_devices() was being called inside a
    # triple loop. (A ThreadPoolExecutor to overlap synthesis with this was tried on
    # 2026-10-17 and measured ~3x SLOWER: onnxruntime does not release the GIL
    # usefully here, so background synthesis took 4.8 s against 1.5 s inline.
    # Do not reintroduce it.)
    # PortAudio snapshots the device list at process start. A one-shot hook run
    # is always fresh, but the daemon lives for days - so from its second
    # utterance on, refresh before choosing. Costs ~200 ms against the 4 s the
    # daemon saves, and it is what stops a USB port change sounding distorted.
    global _SPOKEN_ONCE
    if _SPOKEN_ONCE:
        rescan_devices(sd)
    _SPOKEN_ONCE = True

    all_devices = list(enumerate(sd.query_devices()))
    api_names = {}
    def api_of(d):
        h = d["hostapi"]
        if h not in api_names:
            api_names[h] = sd.query_hostapis(h)["name"]
        return api_names[h]

    # Build the WHOLE preference list, not just the first match, and keep only
    # endpoints that actually open. A powered-off Bluetooth endpoint usually stays
    # enumerated, so a bare name match would pick dead buds and never reach the
    # the speakerphone. This also drops WDM-KS on the speakerphone automatically, which has never
    # opened (PaErrorCode -9996).
    candidates = []
    for pref in DEVICE_PREFS:                    # headset, then speakerphone
        for want in ("WASAPI", "WDM-KS", ""):    # best audio path first
            for i, d in all_devices:
                if d["max_output_channels"] <= 0:
                    continue
                if pref not in d["name"].lower():
                    continue
                hostapi = api_of(d)
                if want and want not in hostapi:
                    continue
                if any(c[0] == i for c in candidates):
                    continue
                try:
                    sd.check_output_settings(device=i, samplerate=48000,
                                             channels=2, dtype="float32")
                except Exception:
                    continue                     # enumerated but not usable
                candidates.append((i, d["name"], hostapi))

    if not candidates:
        # Deliberately NOT device=None: the system default is the monitor, and
        # the user wants speech on the headset or the speakerphone, never there.
        log("no usable output device among %s - saying nothing" % (DEVICE_PREFS,))
        return 0

    idx, dev, api = candidates[0]
    log("mode=%s  device=%s  api=%s  voice=%s  (%d usable candidate(s))"
        % (mode, dev, api, cfg["voice"], len(candidates)))

    kok = get_kokoro()
    for s in say:
        if user_is_talking():
            log("the user started talking - not speaking the rest")
            break
        spoken_text = speechify(s)
        log("SPEAK: %s" % s)
        if spoken_text != s:
            log("  as spoken: %s" % spoken_text)
        emo = emotion if emotion in EMOTIONS else emotion_for(s)
        em = EMOTIONS[emo]
        robot = cfg.get("robot")
        if robot:
            robot = dict(robot)                 # per-line copy, never mutate MODES
            robot["pitch"] = em["pitch"]
            robot["ring"] = em["ring"]
        # Slow the synthesis by the pitch factor so raising the pitch afterwards
        # restores the original pace instead of making him gabble.
        gen_speed = (em["speed"] / robot["pitch"]) if robot else cfg["speed"]
        log("  emotion=%s  pitch=%.2f" % (emo, robot["pitch"] if robot else 1.0))
        samples, rate = kok.create(spoken_text, voice=cfg["voice"], speed=gen_speed,
                                   lang="en-us",
                                   sentence_pause=SENTENCE_PAUSE,
                                   clause_pause=CLAUSE_PAUSE)
        out_rate = rate
        if "WASAPI" in api and rate == 24000:
            samples = resample_poly(samples, 2, 1).astype(np.float32)
            out_rate = 48000
        if robot:
            samples = edgarize(samples, out_rate, robot)
        # Lift the level: normalise the peak, then soft-limit for extra loudness.
        peak = float(np.abs(samples).max())
        if peak > 1e-6:
            samples = samples * (GAIN_PEAK / peak)
            if GAIN_BOOST != 1.0:
                samples = np.tanh(samples * GAIN_BOOST) * GAIN_PEAK
            samples = np.clip(samples, -1.0, 1.0).astype(np.float32)

        # Pad with silence: WASAPI's buffer is tiny (3 ms default, 10 ms "high")
        # and the first/last fraction of a second gets clipped without it.
        pad = np.zeros(int(out_rate * PAD_SECONDS), dtype=np.float32)
        samples = np.concatenate([pad, samples, pad])

        # Hand EDGAR an RMS envelope of exactly what is about to play, so his
        # mouth moves with the real audio instead of a random wiggle. Written
        # BEFORE playback starts, with the start time, so he can index into it.
        if cfg["face"]:
            write_edgar_state(samples, out_rate, s, expression=em["face"])
        # Walk DOWN the preference list on failure - headset, then speakerphone - instead of
        # dropping to device=None. The system default is the monitor, which is the
        # one place this must never come out of.
        for n, (cand_idx, cand_dev, cand_api) in enumerate(candidates):
            # `samples` has ALREADY been upsampled to out_rate. Handing that
            # same array to a non-WASAPI device at the ORIGINAL 24000 played it
            # at half speed, which sounds sluggish. The rate must always
            # match the data, whichever device ends up playing it.
            play_rate, play_samples = out_rate, samples
            try:
                sd.play(play_samples, play_rate, device=cand_idx, latency=LATENCY)
                # Poll rather than sd.wait(), so the button cuts Edgar off
                # mid-sentence instead of him talking over the user.
                cut = False
                while True:
                    st = sd.get_stream()
                    if st is None or not st.active:
                        break
                    if user_is_talking():
                        sd.stop()
                        cut = True
                        log("  cut off - the user started talking")
                        break
                    time.sleep(TALK_POLL)
                if cut:
                    write_edgar_state(None, 0, speaking=False)
                    return 0
                if n:
                    log("  played on fallback device %s (%s)" % (cand_dev, cand_api))
                    idx, dev, api = cand_idx, cand_dev, cand_api   # stick with it
                break
            except Exception as e:
                log("playback on %s (%s) failed: %s" % (cand_dev, cand_api, e))
                try:
                    sd.stop()
                except Exception:
                    pass
        else:
            log("every candidate device failed - nothing was spoken")
    # Always clear it, even in paky mode: if the mode changed mid-turn the face
    # must not be left on screen holding a stale envelope.
    write_edgar_state(None, 0, speaking=False)
    log("done")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("ERROR: %s" % e)
        log(traceback.format_exc().strip().replace("\n", " | "))
        sys.exit(0)
