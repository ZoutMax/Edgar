"""
Push-to-talk voice input for Claude Code.

Hold the MIDDLE MOUSE BUTTON past HOLD_MS, speak, release. The speech is
transcribed locally with faster-whisper, typed into the Claude Code terminal and
submitted. A short click is left alone, so ordinary middle-click still works.

Nothing leaves the machine. Run it in the background:
    pythonw voice-input.py          (no console)
    python  voice-input.py --debug  (console, verbose)

Why this works where the earbud buttons did not: a mouse button really does
register as system input, and instead of needing an API to inject text into a
running session we simply TYPE it - which is identical to the user typing.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import traceback
import time
from datetime import datetime

import numpy as np
import sounddevice as sd

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
LOG = os.path.join(HOME, "voice-input.log")
MODEL_DIR = os.path.join(HOME, "whisper")

MODEL_NAME = CFG["model"]    # 769M params. base.en then small.en both mis-heard;
                           # medium.en was tried and reverted: it is 3.8 s against
                           # 1.4 s on a 3 s clip and fixed NOTHING. Every failure
                           # was the speakerphone clipping at p99.5 >= 0.86, every success
                           # was <= 0.34. The MIC LEVEL was the problem, not the
                           # model - the speakerphone capture level was 73%, now 50%.
HOLD_MS = int(CFG["hold_ms"])               # below this it is treated as an ordinary middle-click
TAIL_MS = 350               # keep listening after release; the last word is still in flight
MIN_SPEECH_S = 0.35         # ignore accidental blips
MAX_SPEECH_S = 120          # safety stop
SAMPLE_RATE = 16000         # what Whisper wants; captured audio is resampled to this
# Each endpoint accepts exactly ONE rate under WASAPI, and they disagree:
#   a Bluetooth headset  16000 only   (mSBC wideband - already Whisper's rate)
#   the speakerphone    48000 only   (decimates to 16000 exactly 3:1)
#   the webcam mic       48000 only
# So the rate has to travel with the device. Hardcoding 16000 meant the speakerphone
# fallback could never open at all.
MIC_RATES = (16000, 48000)                  # prefer 16k: no resampling needed
OUT_RATE = 48000                            # both output endpoints accept 48000
MIC_PREFS = tuple(CFG["mic_prefs"])        # first one that actually OPENS wins
                           # The webcam comes BEFORE the speakerphone on purpose: the speakerphone is an
                           # echo-cancelling speakerphone whose firmware mangles speech
                           # (it clipped at full scale on every utterance), while the
                           # webcam is a plain microphone. Output stays on the speakerphone.
OUT_PREFS = tuple(CFG["out_prefs"])        # where the beeps and the ack go
DEVICE_POLL_S = 5.0        # re-resolve this often while idle, so a disconnect falls back
# An OPTIONAL substring of the terminal's title. Leave it empty and Edgar
# types into whichever terminal window it finds - as long as exactly one
# qualifies. See find_window(): a title alone is not enough to trust.
TARGET_WINDOW = CFG["target_window"]
BEEP = True
# Whisper decodes with a language model, so it guesses familiar words over
# unfamiliar ones: "ANC" became "Active nose constellation", "restart" became
# "or start". initial_prompt biases it toward words it should expect. Keep this
# as a natural sentence, not a word list - that is how the model was trained.
# Whisper listens better when it knows roughly what to expect, so the user's
# own vocabulary comes from the config rather than the code: names, hardware,
# games, drive letters, stock phrases. Write it as PROSE - a bare word list
# biases the model badly - and leave it empty to let Whisper work unaided.
VOCAB_HINT = CFG["vocab_hint"]

KEEP_WAV = True
VOICE_MARK = "> "          # prefixes every spoken line, so Claude can tell voice
                           # from typing. Transcription can mangle a word; typing
                           # cannot, so a marked line that reads oddly should be
                           # questioned rather than guessed at.
KEEP_WAV_COUNT = 10        # keep a rolling history, so a bad transcription can be compared against a good one             # dump the last utterance so settings can be tested against it

TALKING_FLAG = os.path.join(HOME, ".user-talking")   # exists while the button is held

# Played the instant the button is released, so the user knows they were heard
# without waiting for the transcription. A PRE-RENDERED file, not live
# synthesis - see say_ack(). Set ACK_ENABLED False to switch it off; the
# beeps are separate and stay either way.
ACK_ENABLED = bool(CFG["ack_phrase"])
VOICE_MODE_FILE = os.path.join(HOME, "voice-mode.txt")
ACK_TEXT = "ok"            # only for the log and Edgar's state file
ACK_EXPRESSION = "neutral"
EDGAR_STATE = os.path.join(HOME, "edgar-state.json")
EDGAR_HZ = 50              # must match speak-bullets.py, which Edgar also reads

VK_MBUTTON = 0x04
VK_RETURN = 0x0D
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

# --------------------------------------------------------------------------- SendInput
# The INPUT union must be sized for its LARGEST member (MOUSEINPUT), not for the
# one we happen to use. Declaring only KEYBDINPUT makes sizeof(INPUT) 32 instead
# of 40, and SendInput validates cbSize and REJECTS the whole call, returning 0.
# That is why the first version logged "typed N chars" while nothing arrived -
# it never looked at the return value.
KEYEVENTF_UNICODE, KEYEVENTF_KEYUP, INPUT_KEYBOARD = 0x0004, 0x0002, 1


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


assert ctypes.sizeof(INPUT) == 40, "INPUT must be 40 bytes on x64, got %d" % ctypes.sizeof(INPUT)

user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wt.UINT

# Used to confirm WHO owns the window before typing into it.
kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
kernel32.CloseHandle.argtypes = (wt.HANDLE,)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Hosts a console can legitimately live in. Checked as well as the title,
# because any window at all can call itself whatever it likes.
TERMINAL_EXES = ("windowsterminal.exe", "powershell.exe", "pwsh.exe",
                 "cmd.exe", "conhost.exe", "wezterm-gui.exe",
                 "alacritty.exe", "python.exe", "node.exe")

# --------------------------------------------------------------------------- the talk button
# GetAsyncKeyState only READS the middle button - the click itself still travels
# on to whatever is under the pointer. The user rests the pointer on the SECONDARY
# TASKBAR, so every press to talk was also a click on an explorer surface:
# measured 2026-10-18 13:09:00, cursor (2560,1439), under the pointer
# 'Shell_SecondaryTrayWnd'. That is what minimised the terminal, and it is also
# what feeds the phantom Task View that fires ~650 ms after any physical click on
# an explorer surface.
#
# So the button is read through a LOW-LEVEL MOUSE HOOK, which can SWALLOW the
# event. It is swallowed ONLY over the shell's own surfaces - taskbar, desktop,
# Task View host - so middle-click paste in the console and open-in-new-tab in a
# browser keep working everywhere else.
WH_MOUSE_LL = 14
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
HC_ACTION = 0
WM_QUIT = 0x0012
GA_ROOT = 2

# WindowFromPoint returns the deepest CHILD - a single taskbar button, say - so
# the root ancestor is checked as well as the child itself.
SHELL_CLASSES = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
                 "XamlExplorerHostIslandWindow_WASDK", "TrayNotifyWnd",
                 "MSTaskListWClass", "MSTaskSwWClass", "ReBarWindow32",
                 "Windows.UI.Input.InputSite.WindowClass")


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT),
                ("mouseData", wt.DWORD),
                ("flags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]       # ULONG_PTR


HOOKPROC = ctypes.WINFUNCTYPE(wt.LPARAM, ctypes.c_int, wt.WPARAM, wt.LPARAM)

# SetWindowsHookExW and CallNextHookEx return POINTER-WIDTH values. Undeclared,
# ctypes truncates them to 32 bits on x64 and the hook handle is garbage - the
# same bug class as sizeof(INPUT) above.
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.CallNextHookEx.restype = wt.LPARAM
user32.UnhookWindowsHookEx.argtypes = (wt.HHOOK,)
user32.PostThreadMessageW.argtypes = (wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.WindowFromPoint.argtypes = (wt.POINT,)
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = (wt.HWND, ctypes.c_uint)
user32.GetAncestor.restype = wt.HWND
user32.GetMessageW.argtypes = (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)

_MB_DOWN = False         # driven by the hook: a SWALLOWED click never reaches GetAsyncKeyState
_HOOK_SEEN = 0.0         # stamped by the hook on every event, to prove it is still delivering
_HOOK_HANDLE = None      # so a dead hook can be retired before a new one goes in
_HOOK_TID = [0]          # the hook thread's Windows thread id, for WM_QUIT
_MB_SWALLOWED = False    # did the matching DOWN get swallowed? the UP must match it
_HOOK_ALIVE = False
_HOOK_REF = None         # the HOOKPROC must stay referenced or it is collected mid-hook


def _class_of(h):
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, b, 256)
    return b.value


def _over_shell(pt):
    """True if the pointer is on one of explorer's own surfaces."""
    h = user32.WindowFromPoint(pt)
    if not h:
        return False
    if _class_of(h) in SHELL_CLASSES:
        return True
    root = user32.GetAncestor(h, GA_ROOT)
    return bool(root) and _class_of(root) in SHELL_CLASSES


def _mouse_hook(ncode, wparam, lparam):
    # Keep this SHORT. Windows drops a low-level hook that overruns
    # LowLevelHooksTimeout, and a dropped hook means no talk button at all.
    global _MB_DOWN, _MB_SWALLOWED, _HOOK_SEEN
    try:
        if ncode == HC_ACTION:
            _HOOK_SEEN = time.time()      # any event at all proves it is alive
            if wparam == WM_MBUTTONDOWN:
                _MB_DOWN = True
                _MB_SWALLOWED = _over_shell(
                    ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents.pt)
                if _MB_SWALLOWED:
                    return 1            # the taskbar never sees it
            elif wparam == WM_MBUTTONUP:
                _MB_DOWN = False
                if _MB_SWALLOWED:
                    _MB_SWALLOWED = False
                    return 1            # an app that never saw the DOWN must not see the UP
    except Exception:
        pass                            # never break the mouse over a bug in here
    return user32.CallNextHookEx(None, ncode, wparam, lparam)


def start_mouse_hook():
    """Install the hook on its own thread - a hook needs a message pump."""
    global _HOOK_ALIVE, _HOOK_REF
    ready = threading.Event()

    def run():
        global _HOOK_ALIVE, _HOOK_REF, _HOOK_HANDLE
        _HOOK_TID[0] = kernel32.GetCurrentThreadId()
        _HOOK_REF = HOOKPROC(_mouse_hook)
        h = user32.SetWindowsHookExW(WH_MOUSE_LL, _HOOK_REF, None, 0)
        _HOOK_HANDLE = h
        _HOOK_ALIVE = bool(h)
        ready.set()
        if not h:
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=run, daemon=True, name="mouse-hook").start()
    ready.wait(3.0)
    return _HOOK_ALIVE


def mb_pressed():
    """The hook is authoritative - a swallowed click is invisible to GetAsyncKeyState.

    If the hook could not be installed we fall back to polling, which still talks;
    it just cannot stop the click reaching the taskbar.
    """
    if _HOOK_ALIVE:
        return _MB_DOWN
    return (user32.GetAsyncKeyState(VK_MBUTTON) & 0x8000) != 0


_HOOK_WATCH = [None, 0.0, 0.0]   # last cursor position, last heartbeat, last reinstall
HOOK_REINSTALL_EVERY_S = 60.0    # never thrash: at most one attempt a minute


def watch_hook():
    """Put the hook back if it has silently stopped delivering.

    Windows removes a low-level hook that overruns `LowLevelHooksTimeout`
    without telling anyone. `_MB_DOWN` would then be frozen False for ever and
    the talk button would simply do nothing, with nothing in the log.

    The hook sees EVERY mouse event, so a cursor that has moved while the hook
    stamped nothing is the SYMPTOM - but not proof: SetCursorPos moves the
    pointer without generating an event a hook sees. An earlier version treated
    that as proof and DISABLED the hook, quietly throwing away the taskbar
    protection it exists to provide.

    So it reinstalls rather than judges. A needless reinstall of a working hook
    costs nothing; a missed death costs the talk button. The old hook is removed
    and its thread told to quit first, so repeated attempts cannot pile up
    threads, and attempts are capped at one a minute.
    """
    global _MB_DOWN, _MB_SWALLOWED
    if not _HOOK_ALIVE:
        return False
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    here = (pt.x, pt.y)
    moved = _HOOK_WATCH[0] is not None and here != _HOOK_WATCH[0]
    quiet = _HOOK_SEEN <= _HOOK_WATCH[1]
    _HOOK_WATCH[0], _HOOK_WATCH[1] = here, _HOOK_SEEN
    if not (moved and quiet):
        return True
    if time.time() - _HOOK_WATCH[2] < HOOK_REINSTALL_EVERY_S:
        return True
    _HOOK_WATCH[2] = time.time()
    log("mouse hook saw no event while the cursor moved - reinstalling", True)
    try:
        if _HOOK_HANDLE:
            user32.UnhookWindowsHookEx(_HOOK_HANDLE)
        if _HOOK_TID[0]:
            user32.PostThreadMessageW(_HOOK_TID[0], WM_QUIT, 0, 0)
    except Exception as e:
        log("could not retire the old hook: %s" % e)
    _MB_DOWN, _MB_SWALLOWED = False, False      # never inherit a stuck button
    log("mouse hook reinstalled: %s" % start_mouse_hook(), True)
    return _HOOK_ALIVE



def _send(events):
    """Push a batch of INPUT events; return how many the OS accepted."""
    arr = (INPUT * len(events))(*events)
    kernel32.SetLastError(0)
    n = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if n != len(events):
        log("SendInput accepted %d/%d events, GetLastError=%d%s"
            % (n, len(events), kernel32.GetLastError(),
               "  (5 = ACCESS_DENIED: run this elevated)" if kernel32.GetLastError() == 5 else ""))
    return n


def _char_events(text):
    """Two events (down, up) per UTF-16 code unit, so astral chars survive."""
    units = text.encode("utf-16-le")
    out = []
    for i in range(0, len(units), 2):
        cu = units[i] | (units[i + 1] << 8)
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            e = INPUT(type=INPUT_KEYBOARD)
            e.ki = KEYBDINPUT(0, cu, flags, 0, None)
            out.append(e)
    return out


def _vkey_events(vk):
    out = []
    for flags in (0, KEYEVENTF_KEYUP):
        e = INPUT(type=INPUT_KEYBOARD)
        e.ki = KEYBDINPUT(vk, 0, flags, 0, None)
        out.append(e)
    return out


def claim_single_instance():
    """Named mutex, so only one listener ever runs. Windows releases it when the
    process dies - unlike a lock file there is no stale state to clean up after a
    crash. Two listeners would each type every utterance, duplicating it."""
    try:
        h = kernel32.CreateMutexW(None, True, r"Local\ClaudeVoiceInputListener")
        if not h:
            return True                      # never let a mutex bug block startup
        return kernel32.GetLastError() != 183    # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def is_elevated():
    """UIPI discards injected input aimed at a higher-integrity process."""
    try:
        tok = wt.HANDLE()
        if not advapi32.OpenProcessToken(ctypes.c_void_p(-1), 0x0008, ctypes.byref(tok)):
            return None
        val, ret = wt.DWORD(0), wt.DWORD(0)
        ok = advapi32.GetTokenInformation(tok, 20, ctypes.byref(val),
                                          ctypes.sizeof(val), ctypes.byref(ret))
        kernel32.CloseHandle(tok)
        return bool(val.value) if ok else None
    except Exception:
        return None



def log(msg, echo=False):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if echo:
        try:
            print(line, flush=True)
        except Exception:
            pass        # pythonw.exe has no stdout; the file log is the real one


# --------------------------------------------------------------------------- audio

def rescan_devices():
    """PortAudio snapshots the device list when it initialises, so earbuds that
    connected or disconnected since then stay invisible. Without this the
    fallback can never notice the headset went away."""
    try:
        sd._terminate()
        sd._initialize()
        return True
    except Exception as e:
        log("device rescan failed: %s" % e)
        return False


def pick_device(prefs, kind, skip_test_idx=None):
    """First preference that ACTUALLY OPENS -> (index, name, rate).

    Name-matching alone is not enough: a powered-off Bluetooth endpoint usually
    stays enumerated, so matching "buds" would return a dead device and the speakerphone
    would never be reached. check_*_settings is what proves it is live, and it is
    also how we learn which rate this endpoint will accept.
    """
    devs = list(enumerate(sd.query_devices()))
    apis = {}

    def api_of(d):
        h = d["hostapi"]
        if h not in apis:
            apis[h] = sd.query_hostapis(h)["name"]
        return apis[h]

    chan = "max_input_channels" if kind == "in" else "max_output_channels"
    rates = MIC_RATES if kind == "in" else (OUT_RATE,)
    check = sd.check_input_settings if kind == "in" else sd.check_output_settings

    apis_to_try = ("WASAPI",) if kind == "in" else ("WASAPI", "")
    for pref in prefs:
        for want in apis_to_try:
            for i, d in devs:
                if d[chan] <= 0 or pref not in d["name"].lower():
                    continue
                if want and want not in api_of(d):
                    continue
                for r in rates:
                    for ch in (1, 2):
                        try:
                            check(device=i, samplerate=r, channels=ch, dtype="float32")
                        except Exception:
                            continue
                        # check_*_settings is NOT enough: a switched-off Bluetooth
                        # headset leaves a ghost entry that passes the check and
                        # then fails to open. Only an open proves it is alive.
                        # Skipped for the device already in use, so a live
                        # Bluetooth mic is not re-opened every rescan.
                        if kind == "in" and i != skip_test_idx:
                            try:
                                st = sd.InputStream(samplerate=r, channels=1,
                                                    dtype="float32", device=i)
                                st.start(); st.stop(); st.close()
                            except Exception:
                                continue
                        return i, d["name"], r
    return None, "(system default)", SAMPLE_RATE


def beep(out_idx, freq, ms=90):
    if not BEEP:
        return
    try:
        rate = 48000
        t = np.linspace(0, ms / 1000, int(rate * ms / 1000), endpoint=False)
        env = np.minimum(1.0, np.minimum(t * 200, (t[-1] - t) * 200))
        tone = (0.18 * np.sin(2 * np.pi * freq * t) * env).astype(np.float32)
        sd.play(tone, rate, device=out_idx)
        sd.wait()
    except Exception as e:
        log("beep failed: %s" % e)


# --------------------------------------------------------------- typing into the UI

def _exe_of(hwnd):
    """The image name of the process owning a window, lower case."""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                  False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                               ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_window(substr):
    """The ONE terminal window to type into, or (None, None).

    A title substring on its own is not safe enough to ship: any window can
    call itself anything, so a window merely borrowing the name would collect
    whatever is dictated. The OWNER has to be a terminal host as well, and if
    more than one window still qualifies this REFUSES rather than guessing -
    the caller then leaves the text on the clipboard instead of sending it
    somewhere unintended.
    """
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if substr and substr.lower() not in buf.value.lower():
            return True
        if _exe_of(hwnd) not in TERMINAL_EXES:
            return True
        found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    if len(found) > 1:
        log("%d terminal windows qualify (%s) - refusing to guess which one to "
            "type into. Set target_window in config.json." %
            (len(found), ", ".join(repr(t) for _, t in found[:4])))
        return None, None
    return found[0] if found else (None, None)


# ------------------------------------------------- typing without stealing focus
# The target is a CONSOLE window, so its keystrokes can be written straight into
# the console input buffer. No SetForegroundWindow, so Windows cannot refuse it
# and flash the taskbar - which is exactly what it did when the user was working in
# another window. It also means text arrives while the terminal is in the
# background, instead of the terminal being yanked in front of them.


class COORD(ctypes.Structure):
    _fields_ = [('X', ctypes.c_short), ('Y', ctypes.c_short)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [('bKeyDown', wt.BOOL), ('wRepeatCount', wt.WORD),
                ('wVirtualKeyCode', wt.WORD), ('wVirtualScanCode', wt.WORD),
                ('UnicodeChar', ctypes.c_wchar), ('dwControlKeyState', wt.DWORD)]


class _MOUSE(ctypes.Structure):
    _fields_ = [('dwMousePosition', COORD), ('dwButtonState', wt.DWORD),
                ('dwControlKeyState', wt.DWORD), ('dwEventFlags', wt.DWORD)]


class INPUT_RECORD(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [('KeyEvent', KEY_EVENT_RECORD), ('MouseEvent', _MOUSE),
                    ('pad', ctypes.c_byte * 16)]
    _fields_ = [('EventType', wt.WORD), ('Event', _U)]


KEY_EVENT = 0x0001


def type_into_console(pid, text, submit=True):
    '''Write the text into the console's own input buffer. True if it went in.'''
    try:
        kernel32.FreeConsole()
        if not kernel32.AttachConsole(ctypes.c_uint(pid)):
            log('AttachConsole(%d) failed: %d' % (pid, kernel32.GetLastError()))
            return False
        GENERIC_RW = 0x80000000 | 0x40000000
        SHARE_RW = 0x00000001 | 0x00000002
        h = kernel32.CreateFileW('CONIN$', GENERIC_RW, SHARE_RW, None, 3, 0, None)
        if h == -1 or h == 0xFFFFFFFFFFFFFFFF:
            log('CONIN$ open failed: %d' % kernel32.GetLastError())
            kernel32.FreeConsole()
            return False
        chars = list(text) + ([chr(13)] if submit else [])
        recs = (INPUT_RECORD * (len(chars) * 2))()
        n = 0
        for ch in chars:
            vk = 0x0D if ch == chr(13) else 0
            for down in (1, 0):
                r = recs[n]
                r.EventType = KEY_EVENT
                r.Event.KeyEvent.bKeyDown = down
                r.Event.KeyEvent.wRepeatCount = 1
                r.Event.KeyEvent.wVirtualKeyCode = vk
                r.Event.KeyEvent.wVirtualScanCode = 0
                r.Event.KeyEvent.UnicodeChar = ch
                r.Event.KeyEvent.dwControlKeyState = 0
                n += 1
        written = wt.DWORD(0)
        ok = kernel32.WriteConsoleInputW(h, recs, len(recs), ctypes.byref(written))
        err = kernel32.GetLastError()
        kernel32.CloseHandle(h)
        kernel32.FreeConsole()
        if not ok or written.value != len(recs):
            log('WriteConsoleInput wrote %d/%d (err %d)' % (written.value, len(recs), err))
            return False
        return True
    except Exception as e:
        try:
            kernel32.FreeConsole()
        except Exception:
            pass
        log('console write failed: %s' % e)
        return False

def send_text(text, target, no_submit=False):
    """Focus the terminal and TYPE the text, then submit.

    Typing beats clipboard+Ctrl+V here: Ctrl+V is not reliably paste inside a
    console TUI. And SetForegroundWindow is REFUSED for a background process
    unless we first attach to the target's input queue - which is why the first
    version logged "focused" and then typed into nowhere.
    """
    hwnd, title = find_window(target)
    if not hwnd:
        log("target window %r not found - not typing" % target)
        return False

    # FIRST try writing straight into the console's input buffer. It needs no
    # focus, so Windows cannot refuse it and flash the taskbar, and it does not
    # yank the terminal in front of whatever the user is working in.
    marked = VOICE_MARK + text if VOICE_MARK else text
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value and type_into_console(pid.value, marked, submit=not no_submit):
        log("wrote %d chars into the console of pid %d (no focus change)"
            % (len(marked), pid.value))
        return True
    log("console write unavailable - falling back to focus and type")

    # Win32 will not let a background process steal focus unless we attach to
    # the foreground thread's input queue first.
    cur = user32.GetForegroundWindow()
    tid_target = user32.GetWindowThreadProcessId(hwnd, None)
    tid_cur = user32.GetWindowThreadProcessId(cur, None)
    attached = False
    if tid_cur and tid_target and tid_cur != tid_target:
        attached = bool(user32.AttachThreadInput(tid_cur, tid_target, True))

    # SW_RESTORE un-MAXIMIZES a maximized window, which shrank the terminal every
    # time. Only un-minimize, and only when it is actually minimized.
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)

    if attached:
        user32.AttachThreadInput(tid_cur, tid_target, False)

    got = user32.GetForegroundWindow()
    if got != hwnd:
        log("focus FAILED (wanted %s, got %s) - not typing" % (hwnd, got))
        return False
    log("focused: %r" % title)

    # --- type the text as real keystrokes ---
    # One batch: atomic, so nothing of the user's own typing can interleave, and a
    # single return value tells us whether the OS took it.
    events = _char_events(marked)
    sent = _send(events)
    if sent != len(events):
        log("typing REJECTED after %d/%d events - nothing reliable was entered"
            % (sent, len(events)))
        return False

    time.sleep(0.12)
    if _send(_vkey_events(VK_RETURN)) != 2:
        log("text entered but Enter was rejected - submit it yourself")
        return False
    log("typed %d chars and submitted" % len(text))
    return True


# ------------------------------------------------------------------------- main loop


_ACK_CACHE = {}            # path -> (samples, rate, env) or None if the file is missing
_ACK_RETRY = {}            # path -> when to look for a missing file again
ACK_RETRY_S = 60.0


def _ack_path(kind="ack"):
    """ack-edgar.wav, wait-paky.wav and so on, following the current mode.

    kind="ack"  - "ok", the instant the button is released
    kind="wait" - "one moment", when the model is not loaded yet and the
                  recording is being held. Without it the hold is SILENT and
                  reads as a hang: the first time it happened for real the
                  recording was safe for 43 s, and the only reasonable thing
                  to think was that the button had stopped working.
    """
    try:
        with open(VOICE_MODE_FILE, encoding="utf-8") as f:
            mode = f.read().strip().lower()
    except Exception:
        mode = ""
    return os.path.join(HOME, "%s-%s.wav" % (kind, mode or "edgar"))


def _load_ack(path):
    """Samples, rate and the 50 fps RMS envelope Edgar's mouth is driven by."""
    import wave
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    step = max(1, int(rate / EDGAR_HZ))
    n = len(data) // step
    env = np.sqrt((data[:n * step].reshape(n, step) ** 2).mean(axis=1))
    peak = float(env.max()) if n else 0.0
    if peak > 1e-6:
        env = env / peak                            # the mouth wants 0..1
    return data, rate, [round(float(v), 3) for v in env]


def _wake_edgar(env, text):
    """Show the face for the acknowledgement too.

    Playing a wav from here bypasses the daemon, so nothing was writing
    edgar-state.json and Edgar stayed hidden through his own acknowledgement.

    Same schema speak-bullets.py writes, and written the same way - temp file plus
    os.replace - because Edgar reads it at 50 fps and must never see half a file.
    He hides himself once the envelope runs out, so the clear below is only
    housekeeping, and it checks that nothing newer has claimed the file first.
    """
    import json
    start = time.time()
    try:
        tmp = EDGAR_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"speaking": True, "start": start, "hz": EDGAR_HZ,
                       "env": env, "text": text, "expression": ACK_EXPRESSION}, f)
        os.replace(tmp, EDGAR_STATE)
    except Exception as e:
        log("edgar state write failed: %s" % e)
        return

    def clear():
        try:
            with open(EDGAR_STATE, encoding="utf-8") as f:
                if json.load(f).get("start") != start:
                    return                  # real speech took over - leave it alone
            tmp = EDGAR_STATE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"speaking": False}, f)
            os.replace(tmp, EDGAR_STATE)
        except Exception:
            pass

    # A non-daemon Timer blocks interpreter shutdown for as long as it is
    # pending, which would make the listener linger when its task stops it.
    t = threading.Timer(len(env) / float(EDGAR_HZ) + 0.15, clear)
    t.daemon = True
    t.start()


def say_ack(out_idx, kind="ack"):
    """Play the pre-rendered acknowledgement, on RELEASE of the button.

    The daemon round trip logged itself as spoken and still was not audible, and
    it needed Kokoro, a socket and over a second for one word.

    A file removes all of that. It goes out through the SAME `sd.play` path as the
    beeps, which are known to reach them, and it does NOT wait: the word carries on
    while Whisper transcribes, so it adds no latency at all.

    Regenerate with make-ack.py after changing the phrase, the voice or the mode.
    """
    if not ACK_ENABLED:
        return False
    path = _ack_path(kind)
    if _ACK_CACHE.get(path, 1) is None and time.time() >= _ACK_RETRY.get(path, 0):
        del _ACK_CACHE[path]               # look for the file again
    if path not in _ACK_CACHE:
        try:
            _ACK_CACHE[path] = _load_ack(path)
            data, rate, env = _ACK_CACHE[path]
            log("ack loaded: %s  %.2f s @ %d Hz  %d envelope frames"
                % (os.path.basename(path), len(data) / rate, rate, len(env)))
        except FileNotFoundError:
            # Remember the failure so it complains once rather than on every
            # press, but EXPIRE it - otherwise running make-ack.py would have
            # no effect until the listener was restarted.
            _ACK_CACHE[path] = None
            _ACK_RETRY[path] = time.time() + ACK_RETRY_S
            log("no %s - run: python make-ack.py --phrase ... --out %s"
                % (os.path.basename(path), os.path.basename(path)))
        except Exception as e:
            _ACK_CACHE[path] = None
            log("ack could not be loaded: %s" % e)
    if _ACK_CACHE[path] is None:
        return False
    data, rate, env = _ACK_CACHE[path]
    try:
        # Queue the sound FIRST. sd.play returns as soon as it is queued, so
        # the face is still in step - and if the device refuses, Edgar is not
        # left mouthing a word that nobody can hear.
        sd.play(data, rate, device=out_idx)
    except Exception as e:
        log("ack failed: %s" % e)
        return False
    _wake_edgar(env, ACK_TEXT)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--model", default=MODEL_NAME)
    ap.add_argument("--window", default=TARGET_WINDOW)
    ap.add_argument("--no-submit", action="store_true", help="paste but do not press Enter")
    # On a first run the model is DOWNLOADED, and after a reboot it comes off
    # cold disk; a press during either is meant to be HELD rather than lost.
    # Once it is in the page cache it loads in a second, so that path is
    # unreachable in normal use - this makes it testable on demand.
    ap.add_argument("--delay-model", type=float, default=0.0,
                    metavar="SECONDS",
                    help="delay the model load, to test the warm-up hold path")
    args = ap.parse_args()
    echo = args.debug

    if not claim_single_instance():
        log("another listener already holds the mutex - exiting", echo)
        return 0

    mic_idx, mic_name, mic_rate = pick_device(MIC_PREFS, "in")
    out_idx, out_name, _ = pick_device(OUT_PREFS, "out")
    elev = is_elevated()
    log("START  mic=%s @%d  out=%s  model=%s  elevated=%s"
        % (mic_name, mic_rate, out_name, args.model,
           {True: "yes", False: "NO", None: "?"}[elev]), echo)
    if elev is False:
        log("  the Claude Code console is elevated - UIPI will block typing"
            " unless this listener is elevated too", echo)

    # Whisper is needed to TRANSCRIBE, not to record, so it loads on a thread
    # and the talk button goes live at once. Straight after a reboot the model
    # comes off cold disk and takes half a minute: loading it inline left the
    # button dead for the first two minutes, with the hook not yet installed
    # and nothing to say so. A press during the warm-up now records as usual
    # and only the transcription waits.
    from faster_whisper import WhisperModel
    _model = [None]
    _model_ready = threading.Event()

    def load_model():
        t0 = time.time()
        try:
            if args.delay_model > 0:
                log("--delay-model %.0fs: holding the model back on purpose, the"
                    " talk button is live NOW" % args.delay_model, True)
                time.sleep(args.delay_model)
            _model[0] = WhisperModel(args.model, device="cpu", compute_type="int8",
                                     download_root=MODEL_DIR)
            log("model ready in %.1fs" % (time.time() - t0), echo)
        except Exception as e:
            log("MODEL FAILED to load: %s - recording still works but nothing"
                " can be transcribed; restart the listener" % e, True)
        finally:
            _model_ready.set()

    threading.Thread(target=load_model, daemon=True).start()

    down_since = None
    recording = False
    beeped = False
    frames = []
    stream = None
    first_audio = [None]        # a list: the callback must not rebind it

    def on_audio(data, nframes, tinfo, status):
        if first_audio[0] is None:
            first_audio[0] = time.time()
        frames.append(data.copy())

    last_scan = time.time()

    if start_mouse_hook():
        log("mouse hook installed - hold the middle mouse button to talk. The "
            "click is swallowed over the taskbar and the desktop, and passed "
            "through everywhere else", echo)
    else:
        log("mouse hook FAILED to install - falling back to polling, so the click "
            "still reaches whatever is under the pointer", echo)

    while True:
        try:
            pressed = mb_pressed()

            # Re-resolve the devices while IDLE, never on the press itself - rescanning
            # costs a couple of hundred ms and the whole point is that the mic opens the
            # instant the button goes down. Five seconds after the headset are switched off
            # we are on the speakerphone, and five seconds after they come back we are on them.
            if not pressed and not recording and time.time() - last_scan >= DEVICE_POLL_S:
                last_scan = time.time()
                watch_hook()
                rescan_devices()
                m_idx, m_name, m_rate = pick_device(MIC_PREFS, "in", skip_test_idx=mic_idx)
                o_idx, o_name, _ = pick_device(OUT_PREFS, "out")
                if m_name != mic_name or m_rate != mic_rate:
                    log("mic   -> %s @%d (was %s @%d)" % (m_name, m_rate, mic_name, mic_rate), echo)
                if o_name != out_name:
                    log("sound -> %s (was %s)" % (o_name, out_name), echo)
                mic_idx, mic_name, mic_rate = m_idx, m_name, m_rate
                out_idx, out_name = o_idx, o_name

            if pressed and down_since is None:
                # Open the mic IMMEDIATELY. Waiting lost the start of every sentence.
                down_since = time.time()
                # Tell speak-bullets.py to shut up: the user has started talking and
                # Edgar must not speak over him.
                try:
                    open(TALKING_FLAG, "w").close()
                except Exception:
                    pass
                frames = []
                first_audio[0] = None
                recording = False
                try:
                    stream = sd.InputStream(samplerate=mic_rate, channels=1,
                                            dtype="float32", device=mic_idx,
                                            callback=on_audio)
                    stream.start()
                    recording = True
                except Exception as e:
                    # The cached device died since the last scan (buds switched off
                    # mid-session). Rescan and fall back NOW rather than losing the press.
                    log("could not open %s: %s - rescanning" % (mic_name, e), echo)
                    rescan_devices()
                    mic_idx, mic_name, mic_rate = pick_device(MIC_PREFS, "in")
                    out_idx, out_name, _ = pick_device(OUT_PREFS, "out")
                    last_scan = time.time()
                    try:
                        stream = sd.InputStream(samplerate=mic_rate, channels=1,
                                                dtype="float32", device=mic_idx,
                                                callback=on_audio)
                        stream.start()
                        recording = True
                        log("fell back to %s @%d" % (mic_name, mic_rate), echo)
                    except Exception as e2:
                        log("no usable mic: %s" % e2, echo)
                        down_since = None
                        # back off, or a held button retries this 100 times a second
                        last_scan = time.time() + 2.0

            elif pressed and recording and not beeped and first_audio[0] is not None:
                # Beep when the mic is ACTUALLY live, not at a guessed HOLD_MS. A
                # Bluetooth headset needs ~600 ms to bring up its SCO link, so the old
                # 400 ms beep said "go" while the mic was still dead and the first word
                # was lost every time. The beep is now a truthful go signal.
                beeped = True
                log("mic live after %.0f ms - speak now"
                    % ((first_audio[0] - down_since) * 1000), echo)
                beep(out_idx, 660)

            elif recording and (pressed and (time.time() - down_since) > MAX_SPEECH_S or not pressed):
                held_ms = (time.time() - down_since) * 1000
                # Mirror of the lead-in problem. On release the final word is still in
                # flight - partly still being spoken, partly sitting in the WASAPI
                # buffer - so cutting the stream at once truncated it and Whisper
                # guessed at the stub ("sentence" -> "sometimes"). Skipped for a short
                # click so ordinary middle-click stays instant.
                if held_ms >= HOLD_MS:
                    time.sleep(TAIL_MS / 1000.0)
                try:
                    stream.stop(); stream.close()
                except Exception:
                    pass
                recording = False
                try:
                    os.remove(TALKING_FLAG)
                except Exception:
                    pass

                # The user wants it the instant they let go, not when the
                # transcription lands. Fired HERE, once the stream is closed - any
                # earlier and the 350 ms tail would record Edgar saying it, and
                # Whisper would transcribe his own voice back into the prompt.
                lead_ms = (first_audio[0] - down_since) * 1000 if first_audio[0] else None
                down_since = None
                beeped = False
                audio = np.concatenate(frames).flatten() if frames else np.zeros(0, np.float32)
                dur = len(audio) / mic_rate

                if held_ms < HOLD_MS:               # ordinary middle-click
                    if echo:
                        log("short click (%.0f ms) - audio discarded" % held_ms, echo)
                    continue
                beep(out_idx, 440)
                say_ack(out_idx)      # plays on while Whisper works

                if dur < MIN_SPEECH_S:
                    log("too short (%.2fs) - ignored" % dur, echo)
                    # ZERO audio after a real hold means the stream opened but the
                    # device is stale - this is what a USB port change does to a
                    # long-running process. Force a rescan so the next press works.
                    if dur == 0.0 and held_ms >= HOLD_MS:
                        log("no audio at all - rescanning devices", echo)
                        rescan_devices()
                        mic_idx, mic_name, mic_rate = pick_device(MIC_PREFS, "in")
                        out_idx, out_name, _ = pick_device(OUT_PREFS, "out")
                        log("mic now %s @%d" % (mic_name, mic_rate), echo)
                        last_scan = time.time()
                    continue

                # Whisper only accepts 16 kHz. The speakerphone and the webcam capture at 48000,
                # which is an exact 3:1 decimation - resample_poly, never a fractional
                # resampler. (The buds are already 16000, so this is a no-op for them.)
                if mic_rate != SAMPLE_RATE:
                    from scipy.signal import resample_poly
                    from math import gcd
                    g = gcd(SAMPLE_RATE, mic_rate)
                    audio = resample_poly(audio, SAMPLE_RATE // g, mic_rate // g).astype(np.float32)
                    log("resampled %d -> %d Hz (%d:%d)"
                        % (mic_rate, SAMPLE_RATE, mic_rate // g, SAMPLE_RATE // g), echo)

                # A Bluetooth headset mic arrives around 0.009 peak - some 40 dB under what
                # Whisper was trained on, which leaves its mel spectrogram sitting in
                # the noise floor. Normalising the peak is the single biggest accuracy
                # win available here and costs nothing.
                # Normalise on the 99.5th PERCENTILE, not the maximum. A single
                # transient - a mouse click, a knock on the desk - sets the max and
                # drags the whole utterance down with it: on the speakerphone one spike of
                # 1.01 left the speech sitting at 0.127 instead of 0.50, four times
                # too quiet, and Whisper started guessing. Clipping that one spike
                # costs nothing; losing 12 dB across the whole sentence costs a lot.
                mag = np.abs(audio)
                peak = float(mag.max())
                ref = float(np.percentile(mag, 99.5))
                if ref > 1e-4:
                    audio = np.clip(audio * (0.5 / ref), -1.0, 1.0).astype(np.float32)
                log("audio %.1fs  p99.5 %.4f (peak %.4f) -> 0.50  mic lead-in %s"
                    % (dur, ref, peak, "%.0f ms" % lead_ms if lead_ms else "n/a"), echo)

                if KEEP_WAV:
                    try:
                        import wave
                        # Rolling history. Overwriting a single file meant that when
                        # a transcription went wrong there was nothing good to
                        # compare it against - I built a whole theory on that gap.
                        hist = os.path.join(HOME, "voice-history")
                        os.makedirs(hist, exist_ok=True)
                        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                        old_wavs = sorted(f for f in os.listdir(hist) if f.endswith(".wav"))
                        for f in old_wavs[:max(0, len(old_wavs) - KEEP_WAV_COUNT + 1)]:
                            try:
                                os.remove(os.path.join(hist, f))
                            except Exception:
                                pass
                        wp = os.path.join(HOME, "voice-last.wav")
                        with wave.open(wp, "wb") as w:
                            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
                            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
                        try:
                            import shutil
                            shutil.copy2(wp, os.path.join(hist, stamp + ".wav"))
                        except Exception:
                            pass
                        log("kept %s (+ history)" % wp, echo)
                    except Exception as e:
                        log("wav dump failed: %s" % e)

                t0 = time.time()
                # Only reached while the model is still warming up, which in
                # practice means the first press after a reboot. The audio is
                # already captured, so it waits rather than being thrown away.
                if not _model_ready.is_set():
                    log("model still loading - this recording is held, not lost", echo)
                    say_ack(out_idx, "wait")
                    _model_ready.wait()
                model = _model[0]
                if model is None:
                    log("no model loaded - the audio is kept but cannot be"
                        " transcribed", True)
                    beep(out_idx, 300)
                    continue
                # speech_pad_ms keeps the VAD from shaving the consonant off the first
                # word; condition_on_previous_text=False stops it inventing a
                # continuation of the last utterance; temperature=0 = no sampling.
                segs, _ = model.transcribe(
                    audio, language="en", beam_size=5, temperature=0.0,
                    condition_on_previous_text=False,
                    no_speech_threshold=0.5, log_prob_threshold=-0.8,
                    vad_filter=True,
                    initial_prompt=VOCAB_HINT,
                    vad_parameters={"min_silence_duration_ms": 300,
                                    "speech_pad_ms": 400})
                text = " ".join(s.text for s in segs).strip()
                log("heard (%.1fs audio, %.2fs transcribe): %s" % (dur, time.time() - t0, text), echo)

                if not text:
                    log("nothing recognised", echo)
                    beep(out_idx, 300)
                    continue

                if args.no_submit:
                    import pyperclip
                    pyperclip.copy(text)
                    log("copied to clipboard (--no-submit)", echo)
                else:
                    if send_text(text, args.window):
                        beep(out_idx, 880)
                    else:
                        import pyperclip
                        pyperclip.copy(text)
                        log("left it on the clipboard instead", echo)
                        beep(out_idx, 300)

            time.sleep(0.01)
        except Exception as e:
            # NEVER die here. The task has a logon trigger only, so exiting
            # means no talk button until the next reboot - which is exactly what
            # a ghost audio device's fatal -9999 did earlier today. Log it, put
            # the state back the way a fresh press expects, and carry on.
            log("press failed: %s | %s"
                % (e, traceback.format_exc().replace(chr(10), ' | ')), True)
            try:
                if stream is not None:
                    stream.stop(); stream.close()
            except Exception:
                pass
            stream, frames = None, []
            recording, beeped, down_since = False, False, None
            try:
                os.remove(TALKING_FLAG)
            except Exception:
                pass
            time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped by user", True)
    except Exception as e:
        import traceback
        log("FATAL: %s | %s" % (e, traceback.format_exc().replace("\n", " | ")), True)
        sys.exit(1)
