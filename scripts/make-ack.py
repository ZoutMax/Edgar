"""Render the spoken acknowledgement ONCE to a wav the listener can just play.

The user, 2026-10-18: *"i did no hear ok jus save a mp3 and play it mabe more easy ?"*
The daemon round trip logged itself as spoken and was still not audible, and it
needed Kokoro, a socket and ~1.3 s for one word. A file costs nothing: the
listener plays it on the SAME device and the SAME code path as the beeps, which
are known to be audible.

    python make-ack.py                      ok, in the current mode's voice
    python make-ack.py --phrase "got it" --mode paky

Written as 48 kHz 16-bit mono PCM - 48 kHz because that is what the WASAPI
devices here run at, so nothing has to resample at play time.
"""
import argparse
import importlib.util
import os
import sys
import wave

import numpy as np

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


def load_speak_bullets():
    spec = importlib.util.spec_from_file_location(
        "speak_bullets", os.path.join(EDGAR_DIR, "speak-bullets.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["speak_bullets"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phrase", default=CFG["ack_phrase"] or "ok")
    ap.add_argument("--mode", default=None, help="edgar | paky (default: the current mode)")
    ap.add_argument("--emotion", default="neutral")
    ap.add_argument("--out", default=None)
    ap.add_argument("--peak", type=float, default=0.90,
                    help="normalised peak - the ack has to carry over whatever "
                         "the user is watching, so it is louder than the beeps")
    args = ap.parse_args()

    sb = load_speak_bullets()
    mode = args.mode or sb.current_mode()
    cfg = sb.MODES[mode]
    em = sb.EMOTIONS[args.emotion]
    out = args.out or os.path.join(HOME, "ack-%s.wav" % mode)

    # Mirrors speak-bullets' own synthesis exactly, so the word sounds like the
    # rest of his speech rather than a stranger saying it.
    robot = cfg.get("robot")
    if robot:
        robot = dict(robot)
        robot["pitch"] = em["pitch"]
        robot["ring"] = em["ring"]
    gen_speed = (em["speed"] / robot["pitch"]) if robot else cfg["speed"]

    kok = sb.get_kokoro()
    samples, rate = kok.create(args.phrase, voice=cfg["voice"], speed=gen_speed,
                               lang="en-us",
                               sentence_pause=sb.SENTENCE_PAUSE,
                               clause_pause=sb.CLAUSE_PAUSE)
    out_rate = rate
    if rate == 24000:                       # exact 2x, never a fractional resampler
        from scipy.signal import resample_poly
        samples = resample_poly(samples, 2, 1).astype(np.float32)
        out_rate = 48000
    if robot:
        samples = sb.edgarize(samples, out_rate, robot)

    peak = float(np.abs(samples).max())
    if peak > 1e-6:
        samples = np.clip(samples * (args.peak / peak), -1.0, 1.0)

    # WASAPI's buffer is tiny and clips the first and last fraction of a second.
    pad = np.zeros(int(out_rate * sb.PAD_SECONDS), dtype=np.float32)
    samples = np.concatenate([pad, samples.astype(np.float32), pad])

    pcm = (samples * 32767.0).astype(np.int16)
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(out_rate)
        w.writeframes(pcm.tobytes())

    print("  wrote %s" % out)
    print("  %s  mode=%s  voice=%s  emotion=%s  pitch=%.2f"
          % (repr(args.phrase), mode, cfg["voice"], args.emotion,
             robot["pitch"] if robot else 1.0))
    print("  %d Hz, %.2f s, peak %.2f" % (out_rate, len(samples) / out_rate, args.peak))
    return 0


if __name__ == "__main__":
    sys.exit(main())
