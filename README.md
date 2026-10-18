# Edgar

A face that talks back, for [Claude Code](https://claude.com/claude-code) on Windows.

Edgar is the computer from *Electric Dreams* (1984): a mustard-yellow CRT with two
black eyes and a mouth that moves in time with real speech. He sits on a monitor of
his own, appears only while Claude is speaking, and reads out what Claude is doing
as it happens. You talk back by holding the middle mouse button.

Everything runs **offline and local** — no speech leaves the machine.

| | |
|---|---|
| **Hear** | Claude's replies, spoken by [Kokoro](https://github.com/thewh1teagle/kokoro-onnx) TTS through a small-robot filter |
| **Speak** | hold the middle mouse button, talk, release — [faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes it and types it into the terminal |
| **See** | four expressions — angry, sad, neutral, happy — chosen from what Claude actually says |

## Requirements

- Windows 10 or 11
- Python 3.10+ (3.12 recommended)
- About 1.5 GB of disk: 340 MB of voice model, ~250 MB of speech model, the rest venv
- A mouse with a middle button
- Claude Code, if you want the hooks (Edgar works without it, just with less to say)

## Install

```
git clone https://github.com/ZoutMax/Edgar.git
cd Edgar
python install.py
```

It asks what Edgar should call you, then does everything else: builds a virtual
environment, downloads the voice models, writes a config, renders the spoken
acknowledgement, registers three scheduled tasks and adds three hooks to Claude
Code's `settings.json` (backing it up first). It prints every path it wrote.

Start it without waiting for a logon:

```
schtasks /run /tn Edgar-Speak-Daemon
schtasks /run /tn Edgar-Face
schtasks /run /tn Edgar-Voice-Input
```

Then **hold the middle mouse button, wait for the beep, speak, and release.** The
beep matters: a Bluetooth microphone is deaf for its first half-second, so Edgar
beeps when the microphone is genuinely live rather than when you pressed.

To remove it all: `python uninstall.py` (add `--all` to delete the models too).

## Configuration

`%LOCALAPPDATA%\Edgar\config.json`, created by the installer. Every field is
optional — with no config at all Edgar runs on defaults.

| field | what it does |
|---|---|
| `name` | what Edgar calls you in the greeting. Blank removes the address entirely |
| `mic_prefs` | fragments of Windows device names, in order of preference. The first that actually **opens** wins, so a headset that comes and goes is followed automatically |
| `out_prefs` | the same, for speech, beeps and the acknowledgement |
| `model` | faster-whisper model. `small.en` is the sweet spot on CPU |
| `vocab_hint` | **prose** about what you talk about — names, hardware, drive letters, stock phrases. It measurably improves recognition of your own words. A bare word list biases it badly |
| `mode` | `edgar` (face plus a male synthetic voice) or `paky` (voice only, no face) |
| `monitor` | `left`, `right`, `middle`, `primary`, or an index. **Use a word** — indices shift when Windows re-detects displays |
| `ack_phrase` | spoken the instant you release the button, so you know you were heard without waiting for the transcription. `""` switches it off |
| `hold_ms` | how long the button must be held to count as speech rather than an ordinary middle-click |
| `target_window` | optional substring of your terminal title, if several terminals are open. Empty is fine: Edgar uses the only terminal that qualifies, and **refuses to guess** if more than one does |

After changing the voice, the mode or `ack_phrase`, re-render the acknowledgement:

```
python scripts\make-ack.py --mode edgar
```

## How it fits together

```
    middle mouse button ──► voice-input.py ──► faster-whisper ──► the terminal
                                  │
                                  └──► "ok", the instant you release

    Claude's reply ──► speak-bullets.py ──► speak-daemon.py ──► Kokoro ──► speakers
                              │                                    │
                              └──► emotion ──► edgar-state.json ──► edgar.py (the face)
```

- **`voice-input.py`** holds the talk button through a low-level mouse hook, so the
  click never reaches the taskbar underneath. It types into the terminal with
  `WriteConsoleInput`, which needs no focus, so nothing is yanked in front of what
  you are working on.
- **`speak-daemon.py`** keeps the 310 MB voice model warm. Speech starts in about a
  second instead of four; without it the hooks still work, just slower.
- **`speak-bullets.py`** picks the emotion, renders the audio, and hands the face an
  RMS envelope of exactly what is about to play — so the mouth moves with the real
  waveform, not a random wiggle.
- **`edgar.py`** draws the face. The four mouths are **traced from film stills**, not
  generated, and the lips part from the expression's own curve, with the lower lip
  travelling about twice as far as the upper, because that is how a jaw works.

## The emotion rule

The face and the voice are driven by the same choice, so the tone always matches the
words. It is three rules, measured against 34 real lines rather than guessed:

1. **A mistake Claude made itself wins**, anywhere in the line.
2. Otherwise the **last sentence** carrying any sentiment decides, and within it the
   **last match** decides — a report's verdict sits at its end.
3. A sentence that **explains a risk** carries no sentiment: *"if the hook dies the
   button stops working"* is narration, not a failure.

A first-match rule scored 24/34; this scores 33/34. The failure mode of the naive
version is worth knowing: one negative word anywhere turns a whole paragraph angry,
so *"a backup that doesn't parse is worse than none"* reads as rage.

- **Windows only.** The talk button, the task scheduling, the console injection and
  the audio device selection are all Win32.
- **CPU only.** `small.en` transcribes a 3-second phrase in about 1.5 s on a modern
  desktop. There is no GPU path.
- The face needs a monitor it can have to itself. On a single screen, run
  `scripts\edgar.py --windowed` or use `paky` mode.
## Privileges

The tasks are registered with **least privilege** by default. Pass `--elevated` only if your
Claude Code terminal is itself elevated, because Windows refuses input sent to a
higher-privilege window. Be aware of the trade: the interpreter those tasks run lives in a
directory you can write to, so anything able to write there would then run elevated without a
prompt. If you need elevation, consider installing under a directory only administrators can
write to.

Edgar types into a terminal window, and will only do so when the window is owned by a terminal
host process. A title alone is not trusted - any window can call itself anything - and if more
than one window qualifies, Edgar refuses and leaves the text on your clipboard instead.

## Known limits


## Licence

MIT. The Kokoro and faster-whisper models carry their own licences.
