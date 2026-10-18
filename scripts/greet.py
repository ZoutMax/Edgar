"""Edgar's greeting at session start.

{name}: "You have to greet me every time differently then session start at login
a name, and a different greeting every time" ... "human looking greeting".

Runs as a SessionStart hook. Speaks ONE line through the same pipeline as
speak-bullets.py, so it follows whichever mode is set (edgar or paky), uses the
live audio device, and drives the face.

These are written as WHOLE SENTENCES, not a salutation glued to a stock remark -
combining pieces is exactly what makes a greeting sound machine-made. They use
contractions and ordinary rhythm, because the only thing {name} gets is the
sound of them.

A greeting used in the last KEEP runs is refused, so a repeat is impossible
until the pool runs dry rather than merely unlikely.
"""
import importlib.util
import json
import os
import random
import sys
import time
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
HISTORY = os.path.join(HOME, ".greet-history")
KEEP = 16

MORNING = [
    "Morning, {name}.",
    "Morning. What are we up to today?",
    "Morning, {name}. I'm up.",
    "Good morning. Where do you want to start?",
    "Morning, {name}. Everything came up fine.",
    "Morning. Give me a second to wake up properly.",
    "Morning, {name}. Coffee first, or straight in?",
    "Morning, {name}. Slept at all?",
    "Morning. The machine came back up on its own.",
    "Morning, {name}. I'm listening whenever you are.",
    "Morning. Something broke overnight, or are we building?",
]

AFTERNOON = [
    "Afternoon, {name}.",
    "Hey {name}, I'm back.",
    "Afternoon. What are we working on?",
    "Hello {name}. Everything's running.",
    "Afternoon, {name}. Right, where were we?",
    "Hi {name}. Ready when you are.",
    "Afternoon. All of me started on its own, you'll be pleased to know.",
    "Afternoon, {name}. Been busy?",
    "Hello {name}. Picking up where we left off?",
    "Afternoon. I've been sat here quietly.",
    "Hi {name}. Anything gone wrong since this morning?",
]

EVENING = [
    "Evening, {name}.",
    "Evening. Good to see you.",
    "Evening, {name}. What's the plan?",
    "Hello {name}. I'm listening.",
    "Evening, {name}. Everything's where you left it.",
    "Evening. What are we breaking tonight?",
    "Hi {name}. All quiet so far.",
    "Evening, {name}. Long day?",
    "Evening. Everything started cleanly.",
    "Hello {name}. What are we finishing tonight?",
    "Evening, {name}. I'm all ears.",
]

NIGHT = [
    "Still up, {name}?",
    "Late one again, {name}.",
    "It's late, {name}. I'm here anyway.",
    "Evening, {name}. Or morning. I've stopped keeping track.",
    "Still going, {name}? So am I.",
    "Late start, {name}. What's on your mind?",
    "Hello {name}. The whole house is asleep except us.",
    "Late again, {name}. I don't mind.",
    "Still here, {name}? So am I.",
    "Hello {name}. Nothing else is awake.",
    "It's the small hours, {name}. What are we doing?",
]


PENDING = os.path.join(HOME, "pending.txt")
PENDING_CHANCE = 0.55          # how often a greeting raises an open item

# Short openers for when the greeting carries a pending item - the item is the
# substance, so the opener stays out of its way.
OPENERS = {
    "morning": ["Morning, {name}.", "Morning.", "Good morning, {name}."],
    "afternoon": ["Afternoon, {name}.", "Hello {name}.", "Afternoon."],
    "evening": ["Evening, {name}.", "Evening.", "Hello {name}."],
    "night": ["Still up, {name}?", "Late one, {name}.", "Hello {name}."],
}


def part_of_day(hour):
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "night"


def pending_items():
    """Open items {name} may want reminding of. Blank and # lines ignored."""
    try:
        with open(PENDING, encoding="utf-8") as f:
            return [l.strip() for l in f
                    if l.strip() and not l.lstrip().startswith("#")]
    except Exception:
        return []


def pool(hour):
    if 5 <= hour < 12:
        return MORNING
    if 12 <= hour < 18:
        return AFTERNOON
    if 18 <= hour < 23:
        return EVENING
    return NIGHT


def recent():
    try:
        with open(HISTORY, encoding="utf-8") as f:
            return json.load(f)[-KEEP:]
    except Exception:
        return []


def remember(line):
    try:
        # store the pending item too, so the same one is not raised twice running
        extra = [i for i in pending_items() if i and i in line]
        hist = recent() + [line] + extra
        with open(HISTORY, "w", encoding="utf-8") as f:
            json.dump(hist[-KEEP:], f)
    except Exception:
        pass


def choose():
    """Either a plain greeting, or a greeting that raises an open item.

    The pending items make every login genuinely different, and useful - the
    greeting is the one moment {name} is listening and not yet busy.
    """
    hour = datetime.now().hour
    seen = set(recent())
    items = [i for i in pending_items() if i not in seen]

    if items and random.random() < PENDING_CHANCE:
        opener = random.choice(OPENERS[part_of_day(hour)])
        return "%s %s" % (opener, random.choice(items))

    lines = pool(hour)
    fresh = [l for l in lines if l not in seen]
    return random.choice(fresh or lines)


def personalise(line, name):
    """Put the user's name in, or take the address out altogether.

    The greetings are written WITH a name because that is how people greet
    each other. With no name configured the address has to be removed rather
    than left as a gap: "Hey {name}, I'm back." has to become "Hey, I'm
    back." and not "Hey , I'm back."
    """
    if name:
        return line.replace("{name}", name)
    out = line.replace(", {name}", "").replace(" {name},", ",")
    return out.replace(" {name}", "").replace("  ", " ").strip()


def main():
    if "--print" in sys.argv:
        print(personalise(choose(), CFG["name"]))
        return 0

    try:
        sys.stdin.read()          # the hook feeds JSON; it must be drained
    except Exception:
        pass

    spec = importlib.util.spec_from_file_location(
        "speak_bullets", os.path.join(EDGAR_DIR, "speak-bullets.py"))
    sb = importlib.util.module_from_spec(spec)
    sys.modules["speak_bullets"] = sb
    spec.loader.exec_module(sb)

    line = personalise(choose(), CFG["name"])
    sb.log("GREET: %s" % line)
    remember(line)

    # one greeting at a time, and never over the end-of-turn summary
    if not sb.acquire_lock(20.0):
        sb.log("greet: something else is speaking - skipping")
        return 0
    try:
        # NO forced emotion: the greeting is classified by emotion_for()
        # exactly like every other line Edgar speaks, so the face matches
        # what the words actually say.
        return sb.speak([line])
    finally:
        sb.release_lock()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        try:
            with open(os.path.join(HOME, "speak-bullets.log"), "a",
                      encoding="utf-8") as f:
                f.write("%s  greet FAILED: %s%s"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"), e, chr(10)))
        except Exception:
            pass
        sys.exit(0)
