r"""Take Edgar off this machine again.

    python uninstall.py              remove the tasks and the hooks
    python uninstall.py --all        also delete the runtime state and the models

The runtime state is kept by default, because it holds your config.json, your
pending list and about 340 MB of voice model that is tedious to download again.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "scripts")
TASKS = ("Edgar-Speak-Daemon", "Edgar-Voice-Input", "Edgar-Face")


def say(msg):
    print("  " + msg)


def stop_processes():
    """The tasks hold the processes, so ending the task stops them."""
    for name in TASKS:
        subprocess.run(["schtasks", "/end", "/tn", name],
                       capture_output=True, text=True)


def remove_tasks():
    for name in TASKS:
        result = subprocess.run(["schtasks", "/delete", "/tn", name, "/f"],
                                capture_output=True, text=True)
        if result.returncode == 0:
            say("removed scheduled task %s" % name)
        else:
            say("%s was not registered" % name)


def _mentions(obj, needle):
    """Does any STRING inside this structure mention `needle`?

    Deliberately NOT `needle in json.dumps(obj)`: json.dumps escapes every
    backslash, so a Windows path with single separators never matches its own
    doubled form in the dump. That is how this function came to report "no
    Edgar hooks were in settings.json" while leaving all three behind -
    pointing at scripts that --all then deleted, so Claude Code would try to
    run them on every session.
    """
    if isinstance(obj, str):
        return needle in obj.lower()
    if isinstance(obj, dict):
        return any(_mentions(v, needle) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_mentions(v, needle) for v in obj)
    return False


def remove_hooks():
    settings = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    if not os.path.exists(settings):
        say("no Claude Code settings.json here")
        return
    with open(settings, encoding="utf-8") as f:
        cfg = json.load(f)
    hooks = cfg.get("hooks") or {}
    removed = 0
    for event in list(hooks):
        kept = []
        for entry in hooks[event]:
            # Only Edgar's own entries, identified by the scripts directory -
            # never anything else the user has configured.
            if _mentions(entry, SCRIPTS.lower()):
                removed += 1
            else:
                kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if removed:
        if not hooks:
            cfg.pop("hooks", None)
        with open(settings, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        say("removed %d hook(s) from settings.json" % removed)
    else:
        say("no Edgar hooks were in settings.json")


def _marker_home():
    r"""The runtime location install.py recorded for a --home install.

    Without this, `uninstall.py --all` after an `install.py --home` looked for
    %LOCALAPPDATA%\Edgar, found nothing there, and left the runtime and its
    340 MB of models behind without a word.
    """
    try:
        with open(os.path.join(SCRIPTS, ".edgar-home"), encoding="utf-8") as f:
            return f.read().strip() or None
    except Exception:
        return None


def remove_marker():
    marker = os.path.join(SCRIPTS, ".edgar-home")
    if os.path.exists(marker):
        os.remove(marker)
        say("removed the runtime marker")


def main():
    default_home = _marker_home() or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Edgar")
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=default_home)
    ap.add_argument("--all", action="store_true",
                    help="also delete the runtime state, config and models")
    args = ap.parse_args()

    print()
    stop_processes()
    remove_tasks()
    remove_hooks()

    home = os.path.abspath(args.home)
    remove_marker()
    if args.all:
        if os.path.isdir(home):
            shutil.rmtree(home, ignore_errors=True)
            say("deleted %s" % home)
    elif os.path.isdir(home):
        say("kept %s - delete it yourself, or re-run with --all" % home)
    print()
    say("Edgar is no longer started by Windows and no longer hooked into "
        "Claude Code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
