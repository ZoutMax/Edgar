r"""Edgar's self-check: one command that says what is wrong.

    python scripts\\doctor.py
    python scripts\\doctor.py --quiet      only problems

Everything checked here has broken at least once on a real machine. Each
failure prints what to do about it, because a diagnostic that only says "FAIL"
leaves you exactly where you started.

Exit code is 0 when nothing is broken, 1 when something is.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

# Every task and hook runs with -I, which keeps the script's own directory OFF
# sys.path - so a sibling import needs this. It adds OUR directory, never the
# working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import edgar_config

EDGAR_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = edgar_config.HOME
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")

# The scheduled tasks that keep the three pieces alive.
TASKS = ("Edgar-Speak-Daemon", "Edgar-Voice-Input", "Edgar-Face")

# script -> what stops working without it
SCRIPTS = {
    "speak-bullets.py": "the voice itself",
    "speak-daemon.py": "the warm model, so speech starts in 60 ms not 4 s",
    "voice-input.py": "the talk button",
    "edgar.py": "the face",
    "greet.py": "the greeting at logon",
    "make-ack.py": "re-rendering the ok and one-moment cues",
    "mic_gain.py": "reading the microphone level",
}

DAEMON_PORT = 51247
MAX_LOG = 2 * 1024 * 1024
MODELS = (("kokoro-v1.0.onnx", 300 * 1024 * 1024),
          ("voices-v1.0.bin", 20 * 1024 * 1024))

_fails = []
_warns = []
_quiet = False


def head(title):
    if not _quiet:
        print("\n%s" % title)


def ok(msg):
    if not _quiet:
        print("  [ ok ] %s" % msg)


def warn(msg, fix=""):
    _warns.append(msg)
    print("  [warn] %s" % msg)
    if fix:
        print("         -> %s" % fix)


def fail(msg, fix=""):
    _fails.append(msg)
    print("  [FAIL] %s" % msg)
    if fix:
        print("         -> %s" % fix)


def mb(n):
    return "%.1f MB" % (n / 1048576.0)


def check_python():
    head("interpreter")
    venv = os.path.join(HOME, "kokoro", "venv", "Scripts", "python.exe")
    if os.path.exists(venv):
        ok("venv interpreter present")
    else:
        fail("no interpreter at %s" % venv,
             "re-run install.py - the tasks and hooks all point at it")
    ok("running on %s" % sys.version.split()[0])


def check_scripts():
    head("scripts")
    for name, what in sorted(SCRIPTS.items()):
        path = os.path.join(EDGAR_DIR, name)
        if not os.path.exists(path):
            fail("%s is missing, and with it %s" % (name, what))
            continue
        try:
            compile(open(path, encoding="utf-8").read(), path, "exec")
            ok("%-18s compiles" % name)
        except SyntaxError as e:
            fail("%s will not compile: line %s, %s" % (name, e.lineno, e.msg),
                 "restore it from backup/ - nothing that imports it can run")


def check_models():
    head("voice model")
    for name, want in MODELS:
        path = os.path.join(HOME, "kokoro", "models", name)
        if not os.path.exists(path):
            fail("%s is missing" % name,
                 "install.py downloads it from the kokoro-onnx releases")
        elif os.path.getsize(path) < want:
            fail("%s is only %s - a truncated download"
                 % (name, mb(os.path.getsize(path))),
                 "delete it and re-run install.py")
        else:
            ok("%-18s %s" % (name, mb(os.path.getsize(path))))


def check_cues():
    head("spoken cues")
    for kind, what in (("ack", "the ok on button release"),
                       ("wait", "the one moment while the model loads")):
        for mode in ("edgar", "paky"):
            name = "%s-%s.wav" % (kind, mode)
            path = os.path.join(HOME, name)
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                ok("%-16s %s" % (name, what))
            else:
                warn("%s is missing - %s will be silent in %s mode"
                     % (name, what, mode),
                     "python make-ack.py --mode %s --out %s" % (mode, name))


def check_mode():
    head("mode")
    path = os.path.join(HOME, "voice-mode.txt")
    if not os.path.exists(path):
        # Nothing has switched mode yet, which is not a fault.
        ok("edgar (no voice-mode.txt yet, so the default stands)")
        return
    try:
        mode = open(path, encoding="utf-8").read().strip().lower()
    except Exception as e:
        warn("cannot read voice-mode.txt (%s) - the default edgar stands" % e,
             "check the file's permissions")
        return
    if mode in ("edgar", "paky"):
        ok("%s" % mode)
    else:
        warn("voice-mode.txt says %r, which is neither edgar nor paky - the "
             "default edgar stands" % mode, "write edgar or paky into it")


def _processes():
    """Command lines of every running python process, or None if unknown."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\""
             " | ForEach-Object { $_.CommandLine }"],
            capture_output=True, text=True, timeout=30)
        if out.returncode:
            return None
        return [l for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        return None


def check_running(procs):
    head("running now")
    if procs is None:
        warn("could not list processes, so I cannot tell what is running",
             "check by hand in Task Manager")
        return
    here = os.path.normcase(EDGAR_DIR)
    for script, what in (("speak-daemon.py", "the warm voice model"),
                         ("voice-input.py", "the talk button"),
                         ("edgar.py", "the face")):
        # The command line must name THIS install's directory, not just the
        # script. Two copies of Edgar on one machine is the normal case while
        # developing, and matching on the bare filename reports the other
        # one's processes as this one's.
        if any(script in p and here in os.path.normcase(p) for p in procs):
            ok("%-18s %s" % (script, what))
        else:
            fail("%s is NOT running, so %s is gone" % (script, what),
                 "the scheduled task restarts it within a minute; to force it "
                 "now: schtasks /run /tn <task>")


def check_daemon():
    head("speech daemon")
    s = socket.socket()
    s.settimeout(2.0)
    t0 = time.time()
    try:
        s.connect(("127.0.0.1", DAEMON_PORT))
        ok("answering on port %d (%.0f ms)" % (DAEMON_PORT,
                                               1000 * (time.time() - t0)))
    except Exception as e:
        warn("not answering on port %d: %s" % (DAEMON_PORT, e),
             "speech still works, it just reloads the model every time "
             "(about 4 s instead of 60 ms)")
    finally:
        try:
            s.close()
        except Exception:
            pass


def check_tasks():
    head("scheduled tasks")
    for name in TASKS:
        try:
            out = subprocess.run(["schtasks", "/query", "/tn", name, "/fo",
                                  "list"], capture_output=True, text=True,
                                 timeout=30)
        except Exception as e:
            warn("could not query %s: %s" % (name, e))
            continue
        if out.returncode:
            fail("task %s does not exist" % name,
                 "re-run install.py, which registers all three")
            continue
        status = ""
        for line in out.stdout.splitlines():
            if line.lower().startswith("status:"):
                status = line.split(":", 1)[1].strip()
        if status.lower() == "disabled":
            fail("task %s is DISABLED" % name,
                 "schtasks /change /tn %s /enable" % name)
        else:
            ok("%-22s %s" % (name, status or "registered"))


def check_hooks():
    head("Claude Code hooks")
    if not os.path.exists(SETTINGS):
        warn("no settings.json at %s - Edgar cannot speak or greet" % SETTINGS,
             "re-run install.py")
        return
    try:
        cfg = json.load(open(SETTINGS, encoding="utf-8"))
    except Exception as e:
        fail("settings.json will not parse: %s" % e,
             "a broken settings.json disables every hook; restore the backup "
             "install.py left beside it")
        return
    wanted = {"Stop": "the spoken summary at hand-back",
              "PreToolUse": "the narration while working",
              "SessionStart": "the greeting"}
    seen = {}
    here = os.path.normcase(EDGAR_DIR)
    for event, entries in (cfg.get("hooks") or {}).items():
        for entry in entries or []:
            for h in entry.get("hooks", []) or []:
                blob = " ".join(h.get("args") or []) or h.get("command") or ""
                # A hook is Edgar's if it names one of his scripts OR points
                # anywhere inside his directory. The second half matters more:
                # a hook path that got CORRUPTED no longer contains a script
                # name, and matching on the name alone would skip the very
                # thing most worth reporting - it would look like a missing
                # hook rather than a broken one.
                if not (any(s in blob for s in SCRIPTS)
                        or here in os.path.normcase(blob)):
                    continue
                seen.setdefault(event, []).append(blob)
                for part in blob.split():
                    if part.lower().endswith(".py") and not os.path.exists(part):
                        fail("the %s hook points at a file that is not there: "
                             "%s" % (event, part),
                             "re-run install.py - a hook with a bad path fails "
                             "silently, which looks exactly like Edgar being "
                             "broken")
    for event, what in sorted(wanted.items()):
        blobs = seen.get(event, [])
        if not blobs:
            warn("no %s hook, so nothing provides %s" % (event, what),
                 "re-run install.py")
        elif len(blobs) > len(set(blobs)):
            fail("%s has the same hook twice - Edgar would speak every line "
                 "twice" % event,
                 "run uninstall.py --hooks-only then install.py")
        else:
            ok("%-13s %s" % (event, what))


def check_audio():
    head("audio devices")
    try:
        import sounddevice as sd
    except Exception as e:
        fail("sounddevice will not import: %s" % e,
             "pip install -r requirements.txt into the venv")
        return
    try:
        devs = sd.query_devices()
    except Exception as e:
        fail("PortAudio cannot list devices: %s" % e,
             "unplug and replug the audio device, then restart the listener")
        return
    ins = [d for d in devs if d.get("max_input_channels")]
    outs = [d for d in devs if d.get("max_output_channels")]
    if ins:
        ok("%d input device(s), default: %s"
           % (len(ins), devs[sd.default.device[0]]["name"]
              if sd.default.device[0] is not None else "none"))
    else:
        fail("no input device at all - nothing can be recorded",
             "plug in a microphone, then restart the listener")
    if outs:
        ok("%d output device(s), default: %s"
           % (len(outs), devs[sd.default.device[1]]["name"]
              if sd.default.device[1] is not None else "none"))
    else:
        fail("no output device - Edgar cannot be heard", "plug in a speaker")


def check_logs():
    head("logs and disk")
    for name in ("speak-bullets.log", "voice-input.log"):
        path = os.path.join(HOME, name)
        if not os.path.exists(path):
            ok("%-18s not written yet" % name)
            continue
        size = os.path.getsize(path)
        rotated = " (+ one rotated generation)" if os.path.exists(path + ".1") \
            else ""
        if size > MAX_LOG * 1.5:
            warn("%s is %s, past the %s ceiling - rotation is not happening"
                 % (name, mb(size), mb(MAX_LOG)),
                 "check that log() calls _rotate_log()")
        else:
            ok("%-18s %s%s" % (name, mb(size), rotated))
    try:
        import ctypes
        # GetDiskFreeSpaceExW FAILS on a directory that does not exist, and
        # leaves the counter at zero. Reported as it stood, that read "only
        # 0.0 GB free where Edgar lives" on a volume with 48 GB on it - the one
        # message certain to send someone off deleting files. So ask about the
        # nearest directory that DOES exist, and if even that will not answer,
        # say nothing rather than something false.
        probe = os.path.abspath(HOME)
        while not os.path.isdir(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        free = ctypes.c_ulonglong(0)
        if not ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(probe), None, None, ctypes.byref(free)):
            raise OSError("GetDiskFreeSpaceExW failed for %s" % probe)
        gb = free.value / (1024.0 ** 3)
        if gb < 1:
            fail("only %.1f GB free where Edgar lives" % gb,
                 "the model alone needs about 350 MB")
        else:
            ok("%.0f GB free" % gb)
    except Exception:
        pass


def _reexec_in_venv():
    r"""Re-run under Edgar's own interpreter, whichever one started us.

    The README says `python scripts\doctor.py`, and for everyone that is the
    SYSTEM python - where sounddevice and the rest were never installed. That
    turned a healthy install into "sounddevice will not import: pip install -r
    requirements.txt into the venv", which is advice someone would follow for
    nothing. The only interpreter that can answer questions about Edgar's
    packages is Edgar's own.

    Comparing the two paths is also what ends the recursion: after the switch
    sys.executable IS the venv, so the second run returns here immediately.
    """
    venv = os.path.join(HOME, "kokoro", "venv", "Scripts", "python.exe")
    if not os.path.exists(venv):
        return          # check_python() reports a missing venv properly
    if (os.path.normcase(os.path.abspath(sys.executable))
            == os.path.normcase(os.path.abspath(venv))):
        return
    if "--quiet" not in sys.argv:
        print("  (switching to Edgar's own interpreter: %s)" % venv)
        # execv REPLACES this process, and anything still in the stdout buffer
        # dies with it - which is exactly what swallowed the line above the
        # first time this ran.
        sys.stdout.flush()
    try:
        os.execv(venv, [venv, "-I", os.path.abspath(__file__)] + sys.argv[1:])
    except OSError as exc:
        # A venv that exists but will not start is itself worth reporting,
        # and the checks below still say something useful.
        warn("could not switch to the venv interpreter: %s" % exc,
             "the checks below ran on %s instead" % sys.executable)


def main():
    _reexec_in_venv()
    ap = argparse.ArgumentParser(description="Edgar's self-check")
    ap.add_argument("--quiet", action="store_true",
                    help="print only problems")
    args = ap.parse_args()
    global _quiet
    _quiet = args.quiet

    if not _quiet:
        print("Edgar self-check   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        print("home: %s" % HOME)

    procs = _processes()
    check_python()
    check_scripts()
    check_models()
    check_cues()
    check_mode()
    check_running(procs)
    check_daemon()
    check_tasks()
    check_hooks()
    check_audio()
    check_logs()

    print()
    if _fails:
        print("%d thing(s) broken, %d worth a look." % (len(_fails),
                                                        len(_warns)))
        return 1
    if _warns:
        print("Nothing broken. %d thing(s) worth a look." % len(_warns))
        return 0
    print("Everything is in order.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
