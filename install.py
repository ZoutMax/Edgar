r"""Install Edgar: a speaking, listening face for Claude Code on Windows.

    python install.py                    install with defaults, asking your name
    python install.py --name Sam --yes   non-interactive
    python install.py --home D:\Edgar    put the runtime state somewhere else
    python install.py --no-tasks         skip the scheduled tasks
    python install.py --no-hooks         leave Claude Code's settings alone

What it does, in order, and nothing it has not been asked to:

  1. creates the runtime directory (default %LOCALAPPDATA%\Edgar)
  2. builds a virtual environment there and installs the requirements
  3. downloads the two Kokoro voice model files, about 340 MB, once
  4. writes config.json, keeping any settings already in it
  5. renders the spoken acknowledgement for each mode
  6. registers three scheduled tasks, so Edgar returns at logon AND revives
     himself within five minutes if a process ever dies
  7. adds three hooks to Claude Code's settings.json, after backing it up

Everything it writes is listed at the end, and uninstall.py reverses all of it.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "scripts")

MODEL_FILES = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/"
                        "download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/"
                       "download/model-files-v1.0/voices-v1.0.bin",
}

# The logon delays stagger the three, so the daemon is warm before the listener
# needs it and the face is up before either of them speaks.
TASKS = (
    ("Edgar-Speak-Daemon", "speak-daemon.py", "PT40S", None),
    ("Edgar-Voice-Input", "voice-input.py", "PT45S", None),
    ("Edgar-Face", "edgar.py", "PT50S", "--monitor {monitor} --size fill"),
)

# A TimeTrigger that repeats, as well as the logon trigger. Measured: neither
# RestartCount nor repetition on a LOGON trigger revives a process that dies -
# the first covers only a task that fails to start, and the second is scheduled
# when logon fires, which has long since happened. This revives it in about a
# minute, and is safe because every script refuses to run twice.
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>2020-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>{delay}</Delay>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>{runlevel}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

# event, matcher, script plus its own flags, timeout, status line
HOOKS = (
    ("Stop", None, ["speak-bullets.py"], 180, "Speaking bullet lines"),
    ("PreToolUse", "*", ["speak-bullets.py", "--live"], 60, "Narrating"),
    ("SessionStart", "startup|resume", ["greet.py"], 120, "Greeting"),
)

written = []


def say(msg):
    print("  " + msg)


def preflight():
    if os.name != "nt":
        sys.exit("Edgar uses Windows scheduled tasks and the Windows audio and "
                 "input APIs, so it only runs on Windows.")
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10 or newer is needed; this is %d.%d"
                 % sys.version_info[:2])
    if not os.path.isdir(SCRIPTS):
        sys.exit("scripts/ is missing - run this from inside the checkout.")


def make_venv(home):
    venv = os.path.join(home, "kokoro", "venv")
    py = os.path.join(venv, "Scripts", "python.exe")
    if not os.path.exists(py):
        say("building the virtual environment (a minute or two)")
        subprocess.check_call([sys.executable, "-m", "venv", venv])
        written.append(venv)
    say("installing the requirements")
    subprocess.check_call([py, "-m", "pip", "install", "--quiet",
                           "--upgrade", "pip"])
    subprocess.check_call([py, "-m", "pip", "install", "--quiet", "-r",
                           os.path.join(HERE, "requirements.txt")])
    return py


def fetch_models(home):
    models = os.path.join(home, "kokoro", "models")
    os.makedirs(models, exist_ok=True)
    for name, url in MODEL_FILES.items():
        dest = os.path.join(models, name)
        if os.path.exists(dest) and os.path.getsize(dest) > 1000000:
            say("%s is already here" % name)
            continue
        say("downloading %s" % name)
        urllib.request.urlretrieve(url, dest)
        written.append(dest)
        say("  %.0f MB" % (os.path.getsize(dest) / 1e6))


def write_config(home, name, monitor):
    path = os.path.join(home, "config.json")
    cfg = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            say("keeping the settings already in config.json")
        except Exception:
            say("config.json could not be read - writing a fresh one")
            cfg = {}
    with open(os.path.join(HERE, "config.example.json"), encoding="utf-8") as f:
        example = json.load(f)
    for key, value in example.items():
        cfg.setdefault(key, value)
    cfg["name"] = name or cfg.get("name", "")
    cfg["monitor"] = monitor or cfg.get("monitor", "left")
    # The example hint is about a fictional person and would bias Whisper
    # towards words this user never says. Better empty than wrong.
    if cfg.get("vocab_hint") == example.get("vocab_hint"):
        cfg["vocab_hint"] = ""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    written.append(path)
    say("wrote config.json")

    pending = os.path.join(home, "pending.txt")
    if not os.path.exists(pending):
        shutil.copy(os.path.join(HERE, "pending.example.txt"), pending)
        written.append(pending)
        say("wrote pending.txt, which the greeting draws from")


# "ok" on release, and "one moment" when the model is not loaded yet. The
# second one matters most on a FIRST run, when the model is still downloading
# and the wait is minutes rather than seconds: without it a held recording is
# indistinguishable from a dead button.
CUES = (("ack", None, "the spoken acknowledgement"),
        ("wait", "one moment", "the please-wait cue"))


def render_ack(py, home):
    env = dict(os.environ, EDGAR_HOME=home)
    for mode in ("edgar", "paky"):
        for kind, phrase, label in CUES:
            say("rendering %s for %s" % (label, mode))
            cmd = [py, "-I", os.path.join(SCRIPTS, "make-ack.py"),
                   "--mode", mode,
                   "--out", os.path.join(home, "%s-%s.wav" % (kind, mode))]
            if phrase:
                cmd += ["--phrase", phrase]
            result = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if result.returncode:
                detail = (result.stderr or result.stdout).strip().splitlines()
                say("  could not render it: %s"
                    % (detail[-1][:150] if detail else "?"))
            else:
                written.append(os.path.join(home, "%s-%s.wav" % (kind, mode)))


def register_tasks(py, home, monitor, elevated):
    pythonw = py.replace("python.exe", "pythonw.exe")
    user = "%s\\%s" % (os.environ.get("USERDOMAIN", ""),
                       os.environ.get("USERNAME", ""))
    for name, script, delay, extra in TASKS:
        arguments = '-I "%s"' % os.path.join(SCRIPTS, script)
        if extra:
            arguments += " " + extra.format(monitor=monitor)
        xml = TASK_XML.format(description="Edgar: %s" % script, delay=delay,
                              runlevel=("HighestAvailable" if elevated
                                        else "LeastPrivilege"),
                              user=user, command=pythonw, arguments=arguments,
                              workdir=SCRIPTS)
        temp = os.path.join(home, "_task.xml")
        with open(temp, "w", encoding="utf-16") as f:
            f.write(xml)
        result = subprocess.run(
            ["schtasks", "/create", "/tn", name, "/xml", temp, "/f"],
            capture_output=True, text=True)
        os.remove(temp)
        if result.returncode:
            say("%s FAILED: %s"
                % (name, (result.stdout + result.stderr).strip()[:140]))
        else:
            written.append("scheduled task " + name)
            say("registered %s" % name)


def patch_hooks(py, home):
    settings = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    if not os.path.exists(settings):
        say("no Claude Code settings.json here - skipping the hooks")
        return
    backup = settings + ".before-edgar"
    if not os.path.exists(backup):
        shutil.copy(settings, backup)
        written.append(backup)
    with open(settings, encoding="utf-8") as f:
        cfg = json.load(f)
    hooks = cfg.setdefault("hooks", {})
    added = 0
    for event, matcher, extra, timeout, status in HOOKS:
        entries = hooks.setdefault(event, [])
        script = os.path.join(SCRIPTS, extra[0])
        if any(script in json.dumps(entry) for entry in entries):
            continue
        entry = {"hooks": [{"type": "command",
                            "command": py,
                            "args": ["-I", script] + extra[1:],
                            "timeout": timeout,
                            "async": True,
                            "statusMessage": status}]}
        if matcher:
            entry["matcher"] = matcher
        entries.append(entry)
        added += 1
    if added:
        with open(settings, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        say("added %d hook(s) to settings.json (backup kept beside it)" % added)
    else:
        say("the hooks are already in settings.json")


def main():
    default_home = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Edgar")
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=default_home)
    ap.add_argument("--name", default=None, help="what Edgar calls you")
    ap.add_argument("--monitor", default="left",
                    help="left | right | middle | primary, or an index")
    ap.add_argument("--elevated", action="store_true",
                    help="run the tasks with highest privileges. Needed ONLY if"
                         " your Claude Code terminal is itself elevated, because"
                         " Windows refuses input sent to a higher-privilege"
                         " window. See the warning it prints.")
    ap.add_argument("--no-tasks", action="store_true")
    ap.add_argument("--no-hooks", action="store_true")
    ap.add_argument("--yes", action="store_true", help="ask nothing")
    args = ap.parse_args()

    preflight()
    home = os.path.abspath(args.home)
    name = args.name
    if name is None and not args.yes:
        try:
            name = input("  What should Edgar call you? (blank for nothing) ").strip()
        except EOFError:
            name = ""

    print()
    say("installing Edgar")
    say("  scripts : %s" % SCRIPTS)
    say("  state   : %s" % home)
    print()
    os.makedirs(home, exist_ok=True)
    py = make_venv(home)
    fetch_models(home)
    write_config(home, name or "", args.monitor)
    render_ack(py, home)
    if args.elevated:
        say("NOTE: --elevated runs the tasks with highest privileges, and the")
        say("      interpreter they run lives in a directory you can write to.")
        say("      Anything able to write there would then run elevated without")
        say("      a prompt. Only use it if your terminal is itself elevated,")
        say("      and consider installing under Program Files if you do.")

    if not args.no_tasks:
        register_tasks(py, home, args.monitor, args.elevated)
    if not args.no_hooks:
        patch_hooks(py, home)

    print()
    say("done. Written:")
    for item in written:
        say("  %s" % item)
    print()
    say("Start it now, without waiting for a logon:")
    say("  schtasks /run /tn Edgar-Speak-Daemon")
    say("  schtasks /run /tn Edgar-Face")
    say("  schtasks /run /tn Edgar-Voice-Input")
    say("Then hold the MIDDLE MOUSE BUTTON, wait for the beep, speak, release.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
