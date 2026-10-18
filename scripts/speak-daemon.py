"""Keeps Edgar's voice model loaded, so speech starts in under a second.

Every hook run was a fresh Python process re-reading the 310 MB Kokoro model
from disk - about 4 s from the line appearing to the first word, by which time
The user had already started talking.

This holds the model in memory and does the speaking on request. The hooks
become thin clients: they hand over the lines and exit. If the daemon is not
running they fall back to speaking in-process, so nothing breaks.

    pythonw speak-daemon.py        (started by the Claude-Speak-Daemon task)
    python  speak-daemon.py --debug
"""
import argparse
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import traceback

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
HOST = "127.0.0.1"
PORT = 51247
IDLE_RELOAD_S = 3600.0       # re-pick audio devices at least this often


def load_speak_bullets():
    spec = importlib.util.spec_from_file_location(
        "speak_bullets", os.path.join(EDGAR_DIR, "speak-bullets.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["speak_bullets"] = mod
    spec.loader.exec_module(mod)
    return mod


sb = load_speak_bullets()
_speaking = threading.Lock()
_busy = threading.Event()


def log(msg):
    sb.log("daemon: %s" % msg)


def handle(conn, addr, debug=False):
    try:
        conn.settimeout(5.0)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return
        req = json.loads(buf.decode("utf-8").strip())
        lines = req.get("lines") or []
        emotion = req.get("emotion")
        skip_if_busy = bool(req.get("skip_if_busy"))

        if not lines:
            conn.sendall(b"empty\n")
            return

        # Live narration must SKIP when something is already being said, so it
        # stays current; the end-of-turn summary waits its turn instead.
        if skip_if_busy and _busy.is_set():
            conn.sendall(b"busy\n")
            log("busy - skipped %d line(s)" % len(lines))
            return

        conn.sendall(b"ok\n")          # ack now; the client must not wait for speech
    except Exception as e:
        try:
            conn.sendall(b"error\n")
        except Exception:
            pass
        log("request failed: %s" % e)
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass

    with _speaking:
        _busy.set()
        try:
            sb.speak(lines, emotion=emotion)
        except Exception:
            log("speak failed: %s" % traceback.format_exc().replace(chr(10), " | "))
        finally:
            _busy.clear()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # NO SO_REUSEADDR. On Windows it lets a SECOND socket bind a port that is
    # already in use - the opposite of Linux - so it defeated the guard below:
    # a duplicate daemon bound happily and the two raced to accept, each with
    # its own ~700 MB copy of Kokoro. The default refuses the bind, which is
    # what the except clause was written for all along.
    try:
        srv.bind((HOST, PORT))
    except OSError as e:
        log("port %d already in use (%s) - another daemon is running" % (PORT, e))
        return 1
    srv.listen(8)
    log("listening on %s:%d" % (HOST, PORT))

    # Load the model NOW rather than on the first request, so the first thing
    # The user hears is as quick as the rest.
    t0 = time.time()
    try:
        sb.get_kokoro()
        log("voice model warm in %.1fs" % (time.time() - t0))
    except Exception as e:
        log("could not preload the model: %s" % e)

    while True:
        try:
            conn, addr = srv.accept()
        except Exception as e:
            log("accept failed: %s" % e)
            time.sleep(0.5)
            continue
        threading.Thread(target=handle, args=(conn, addr, args.debug),
                         daemon=True).start()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        log("FATAL: %s" % traceback.format_exc().replace(chr(10), " | "))
        sys.exit(1)
