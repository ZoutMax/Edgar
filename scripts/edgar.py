"""EDGAR - the face from Electric Dreams (1984), as Claude's speaking indicator.

A mustard-yellow CRT with two black eyes and a curved mouth, run FULLSCREEN on a
monitor of its own. When Claude speaks the mouth moves IN TIME WITH THE REAL
AUDIO: speak-bullets.py writes an RMS envelope of each utterance to
edgar-state.json before playing it, and this window indexes into that envelope by
elapsed time. The mouth is driven by the actual waveform, not a random wiggle.

tkinter only - it ships with Python, so this adds no dependency.

    pythonw edgar.py --monitor 1        normal, fullscreen on monitor 1
    python  edgar.py --monitor 1 --debug --windowed
    Escape quits.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import random
import sys
import time
import tkinter as tk
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
STATE = os.path.join(HOME, "edgar-state.json")

# MEASURED from four film stills, not eyeballed: the yellow screen was isolated
# as the largest connected yellow blob, the face as the dark pixels inside it,
# and every distance expressed as a fraction of that screen. See
# scratchpad/sample_edgar2.py. Medians across the stills.
YELLOW = "#7c703a"         # darker and more olive than it looks by eye
SCANLINE = "#726737"       # 92% of it
FACE = "#0a0802"
SURROUND = "#000000"      # the black around the CRT screen           # the CORE of the face. Taking the median of all dark
                           # pixels gave #1d1805, which reads brown - that average
                           # included the anti-aliased rim where black meets
                           # yellow. The solid interior is essentially black.

FACE_ASPECT = 1.19         # the CRT screen is ~1.19:1. The window is 1.78:1, so
                           # the face is laid out inside a centred 1.19 box and
                           # only the background fills the rest - otherwise the
                           # eyes drift apart and the face stretches.
EYE_Y = 0.306              # fraction of the face box height
EYE_DX = 0.158             # fraction of its width, either side of centre
EYE_R = 0.070              # radius / short side. The film's eye radius is
                           # 0.186 x eyegap; 0.060 measured 0.160 on screen.
MOUTH_Y = 0.712            # the CENTRE of the arc, not its ends (happy)
# The film does NOT keep the mouth at one height: its centre climbs as the frown
# deepens (1.079 -> 0.860 eyegaps below the eyes, happy -> angry). Measured per
# expression and converted to a fraction of the face-box height via
#   y = EYE_Y + drop * 0.376
MOUTH_Y_BY_EXPRESSION = {
    "happy":   0.306 + 1.070 * 0.376,      # 0.708, from the promo photo
    "neutral": 0.306 + 0.982 * 0.376,      # 0.675
    "sad":     0.306 + 0.915 * 0.376,      # 0.650
    "angry":   0.306 + 0.860 * 0.376,      # 0.629
}
MOUTH_HALF = 0.225         # half-width as a fraction of its width
MOUTH_W = 0.023            # stroke / short side. Film is 0.062 x eyegap;
                           # 0.019 measured 0.052 on screen - too thin.

# Every mouth is re-expressed as (half-width, 41 y-values at evenly spaced u in
# -1..1) so two expressions can be BLENDED point for point. Without a common
# parameter the curves have different x ranges and cannot be interpolated.
_GRID = 41


def _normalise(curve):
    xs = [a for a, _ in curve]
    ys = [b for _, b in curve]
    half = max(abs(xs[0]), abs(xs[-1]))
    out = []
    for i in range(_GRID):
        x = xs[0] + (xs[-1] - xs[0]) * i / (_GRID - 1.0)
        j = 0
        while j < len(xs) - 2 and xs[j + 1] < x:
            j += 1
        span = xs[j + 1] - xs[j]
        t = (x - xs[j]) / span if span else 0.0
        out.append(ys[j] + (ys[j + 1] - ys[j]) * t)
    return half, out



TRANSITION_S = 0.28        # how long a change of expression takes to morph
LIP_UPPER = 0.34           # a human opens mostly by dropping the jaw, so the
LIP_LOWER = 0.66           # lower lip travels about twice as far as the upper
BLINK_WHILE_TALKING = (5.0, 11.0)   # rarer than when idle, but never frozen

FPS = 50                   # matches the 20 ms envelope frames
IDLE_FPS = 10              # while hidden there is nothing to animate
BLINK_EVERY = (4.0, 9.0)
BLINK_MS = 130
MOUTH_OPEN_AT = 0.06       # envelope level above which the mouth opens
HIDE_AFTER_S = 1.4         # linger after speech so he does not flicker between lines

# Expressions, measured from the stills. Each value is the curve depth as
# a fraction of height; positive curves down at the centre (a smile), negative
# curves up (a frown), near-zero is the flat neutral line.
# Depths derived from the EYE GAP, which is crop-proof - measuring against the
# screen box was wrong because some stills crop the screen short (aspect 1.31 vs
# 1.16). Across the stills: smile = 0.194 x eyegap, frown = 0.375 x eyegap, so
# THE FROWN IS ABOUT TWICE AS DEEP AS THE SMILE. Converted to a fraction of the
# face-box height via eyegap = 2 x EYE_DX and the 1.19 aspect.
# A LADDER, not three buckets - The user: "there are more than 3".
# Depths are measured against the EYE GAP, which is crop-proof (some stills crop
# the screen short, which corrupts any fraction-of-screen measurement).
# Measured solidly: smile +0.194 (13,14,17), frown -0.319 (12,16),
# deep frown -0.431 (9,15,19). The in-between stops are interpolated.
# Converted to a fraction of the face-box height: eyegap = 2 x EYE_DX = 0.316 w,
# and w = 1.19 h, so eyegap = 0.376 h.
# The mouths are TRACED from the user's pictures, not generated from a curve.
# Each point is (x, y) in EYE-GAP units: x from the mouth's own centre, y BELOW
# THE EYE LINE. So every mouth is placed by the eyes, exactly as he asked, and
# no sine/depth parameter can distort the shape.  scratchpad/trace_mouths.py
# The mouth OUTLINES, copied out of the user's pictures - not drawn. Each is a
# closed polygon: the stroke's top edge left-to-right, then its bottom edge
# back. So it carries the real thickness and the way the ends taper.
# (x, y) in EYE-GAP units, y below the eye line, so the eyes place it and it
# scales to any screen without blurring.  scratchpad/copy_mouths.py
# The mouth CENTRE LINE taken from each of the user's pictures, then fitted
# with the lowest-degree polynomial that still follows it. Copying the raw
# outline reproduced the pixel stair-steps of the small stills - 18.png's
# screen is only ~110 px wide - and that read as hand-writing.
# (x, y) in EYE-GAP units, y below the eye line.
MOUTH_STROKE = 0.062       # closed-mouth stroke, in eye-gap units
MOUTH_OPEN = 0.300         # how far the lips part at full volume        # stroke width in eye-gap units, from the film
MOUTH_CURVES = {
    # angry - 16.png
    "angry": [
        (-0.7536,+1.3254), (-0.7159,+1.2818), (-0.6782,+1.2398), (-0.6405,+1.1997), (-0.6029,+1.1621),
        (-0.5652,+1.1272), (-0.5275,+1.0950), (-0.4898,+1.0658), (-0.4521,+1.0394), (-0.4145,+1.0159),
        (-0.3768,+0.9952), (-0.3391,+0.9771), (-0.3014,+0.9616), (-0.2638,+0.9484), (-0.2261,+0.9374),
        (-0.1884,+0.9285), (-0.1507,+0.9215), (-0.1130,+0.9163), (-0.0754,+0.9127), (-0.0377,+0.9108),
        (+0.0000,+0.9104), (+0.0377,+0.9115), (+0.0754,+0.9142), (+0.1130,+0.9185), (+0.1507,+0.9244),
        (+0.1884,+0.9320), (+0.2261,+0.9415), (+0.2638,+0.9530), (+0.3014,+0.9667), (+0.3391,+0.9827),
        (+0.3768,+1.0012), (+0.4145,+1.0223), (+0.4521,+1.0464), (+0.4898,+1.0734), (+0.5275,+1.1035),
        (+0.5652,+1.1368), (+0.6029,+1.1732), (+0.6405,+1.2128), (+0.6782,+1.2555), (+0.7159,+1.3009),
        (+0.7536,+1.3489),
    ],
    # sad - 18.png
    "sad": [
        (-0.7501,+1.0544), (-0.7126,+1.0492), (-0.6751,+1.0436), (-0.6376,+1.0378), (-0.6001,+1.0318),
        (-0.5626,+1.0258), (-0.5251,+1.0198), (-0.4876,+1.0139), (-0.4501,+1.0082), (-0.4126,+1.0028),
        (-0.3751,+0.9976), (-0.3376,+0.9928), (-0.3001,+0.9884), (-0.2625,+0.9844), (-0.2250,+0.9809),
        (-0.1875,+0.9779), (-0.1500,+0.9755), (-0.1125,+0.9736), (-0.0750,+0.9724), (-0.0375,+0.9716),
        (+0.0000,+0.9715), (+0.0375,+0.9719), (+0.0750,+0.9729), (+0.1125,+0.9745), (+0.1500,+0.9766),
        (+0.1875,+0.9791), (+0.2250,+0.9822), (+0.2625,+0.9857), (+0.3001,+0.9895), (+0.3376,+0.9937),
        (+0.3751,+0.9982), (+0.4126,+1.0028), (+0.4501,+1.0077), (+0.4876,+1.0125), (+0.5251,+1.0174),
        (+0.5626,+1.0222), (+0.6001,+1.0268), (+0.6376,+1.0311), (+0.6751,+1.0350), (+0.7126,+1.0384),
        (+0.7501,+1.0412),
    ],
    # neutral - 18.png mirrored
    "neutral": [
        (-0.7501,+0.9748), (-0.7126,+0.9801), (-0.6751,+0.9857), (-0.6376,+0.9915), (-0.6001,+0.9974),
        (-0.5626,+1.0035), (-0.5251,+1.0094), (-0.4876,+1.0153), (-0.4501,+1.0211), (-0.4126,+1.0265),
        (-0.3751,+1.0317), (-0.3376,+1.0365), (-0.3001,+1.0409), (-0.2625,+1.0449), (-0.2250,+1.0484),
        (-0.1875,+1.0513), (-0.1500,+1.0538), (-0.1125,+1.0556), (-0.0750,+1.0569), (-0.0375,+1.0577),
        (+0.0000,+1.0578), (+0.0375,+1.0574), (+0.0750,+1.0564), (+0.1125,+1.0548), (+0.1500,+1.0527),
        (+0.1875,+1.0501), (+0.2250,+1.0471), (+0.2625,+1.0436), (+0.3001,+1.0398), (+0.3376,+1.0356),
        (+0.3751,+1.0311), (+0.4126,+1.0264), (+0.4501,+1.0216), (+0.4876,+1.0167), (+0.5251,+1.0119),
        (+0.5626,+1.0071), (+0.6001,+1.0025), (+0.6376,+0.9982), (+0.6751,+0.9943), (+0.7126,+0.9908),
        (+0.7501,+0.9880),
    ],
    # happy - 20.png
    "happy": [
        (-0.7311,+0.5544), (-0.6945,+0.6262), (-0.6580,+0.6893), (-0.6214,+0.7447), (-0.5849,+0.7932),
        (-0.5483,+0.8358), (-0.5118,+0.8730), (-0.4752,+0.9055), (-0.4387,+0.9339), (-0.4021,+0.9587),
        (-0.3655,+0.9802), (-0.3290,+0.9988), (-0.2924,+1.0148), (-0.2559,+1.0285), (-0.2193,+1.0401),
        (-0.1828,+1.0498), (-0.1462,+1.0576), (-0.1097,+1.0637), (-0.0731,+1.0681), (-0.0366,+1.0710),
        (+0.0000,+1.0722), (+0.0366,+1.0718), (+0.0731,+1.0698), (+0.1097,+1.0660), (+0.1462,+1.0604),
        (+0.1828,+1.0529), (+0.2193,+1.0433), (+0.2559,+1.0315), (+0.2924,+1.0173), (+0.3290,+1.0005),
        (+0.3655,+0.9808), (+0.4021,+0.9580), (+0.4387,+0.9318), (+0.4752,+0.9017), (+0.5118,+0.8675),
        (+0.5483,+0.8287), (+0.5849,+0.7848), (+0.6214,+0.7352), (+0.6580,+0.6794), (+0.6945,+0.6166),
        (+0.7311,+0.5462),
    ],
}

MOUTH_SHAPES = {k: _normalise(v) for k, v in MOUTH_CURVES.items()}
DEFAULT_EXPRESSION = "neutral"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# CreateMutexW returns a HANDLE, and without an explicit restype ctypes truncates
# it to 32 bits on x64 - the same bug class that made SendInput fail silently in
# voice-input.py. Declare it.
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]

# Held for the life of the process. The module reference MATTERS: if this is
# garbage collected the mutex is released and a second Edgar can start.
_INSTANCE_LOCK = None


# --------------------------------------------------------------------- monitors

class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32)]


def monitors():
    """Every monitor's rect, in EnumDisplayMonitors order."""
    out = []
    PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC,
                              ctypes.POINTER(RECT), wt.LPARAM)

    def cb(hmon, hdc, lprc, lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        out.append((r.left, r.top, r.right - r.left, r.bottom - r.top,
                    mi.szDevice, bool(mi.dwFlags & 1)))
        return True

    user32.EnumDisplayMonitors(None, None, PROC(cb), 0)
    return out


def read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ------------------------------------------------------------------------ face

class Edgar:
    def __init__(self, root, debug=False, always=False, scanlines=False):
        self.root = root
        self.debug = debug
        self.always = always
        self.scanlines = scanlines
        self.visible = True
        self.last_speech = 0.0
        self.expression = DEFAULT_EXPRESSION
        self.shown = DEFAULT_EXPRESSION      # what is on screen right now
        self.blend = 1.0                     # 0 = still the old one, 1 = arrived
        self.canvas = tk.Canvas(root, bg=SURROUND, highlightthickness=0, bd=0,
                                cursor="none")
        self.canvas.pack(fill="both", expand=True)

        self.level = 0.0            # mouth openness, 0..1
        self.target = 0.0
        self.speaking = False
        self.blink_at = time.time() + random.uniform(*BLINK_EVERY)
        self.blinking_until = 0.0
        self.scan_cache = (0, 0, [])

        root.bind("<Escape>", lambda e: root.destroy())
        if not always:
            self.set_visible(False)             # start hidden; speech summons him
        self.tick()

    def set_visible(self, on):
        self.visible = on
        try:
            if on:
                self.root.deiconify()
                # deiconify drops topmost on an overrideredirect window
                self.root.attributes("-topmost", True)
                self.root.lift()
            else:
                self.root.withdraw()
        except Exception:
            pass
        if self.debug:
            print("visible=%s" % on, flush=True)

    def poll(self):
        st = read_state()
        amp, speaking = 0.0, False
        if st and st.get("speaking"):
            env = st.get("env") or []
            hz = st.get("hz") or 50
            i = int((time.time() - st.get("start", 0)) * hz)
            if 0 <= i < len(env):
                amp, speaking = float(env[i]), True
            elif i < 0:
                speaking = True                 # about to start
            self.expression = st.get("expression", DEFAULT_EXPRESSION)
        if speaking != self.speaking and self.debug:
            print("speaking=%s" % speaking, flush=True)
        self.speaking, self.target = speaking, amp

    def draw(self):
        c = self.canvas
        c.delete("all")
        W = c.winfo_width()
        H = c.winfo_height()
        if W < 10 or H < 10:
            return

        # The CRT screen is ~1.19:1 and the monitor is 1.78:1. Filling the whole
        # window with yellow stretched the face - the mouth worst of all. So the
        # yellow is drawn at the CRT's OWN aspect, centred, with black either
        # side, exactly as the screen sits in its bezel in the film.
        h = H
        w = min(W, H * FACE_ASPECT)
        ox = (W - w) / 2
        short = min(w, h)
        cx = ox + w / 2
        c.create_rectangle(ox, 0, ox + w, H, fill=YELLOW, outline=YELLOW)

        eye_y = h * EYE_Y
        eye_r = short * EYE_R
        blinking = time.time() < self.blinking_until
        for sx in (-1, 1):
            x = cx + sx * (w * EYE_DX)
            if blinking:
                c.create_rectangle(x - eye_r, eye_y - eye_r * 0.16,
                                   x + eye_r, eye_y + eye_r * 0.16,
                                   fill=FACE, outline=FACE)
            else:
                c.create_oval(x - eye_r, eye_y - eye_r, x + eye_r, eye_y + eye_r,
                              fill=FACE, outline=FACE)

        self.draw_mouth(c, w, h, cx, short, eye_y)

        # Scanlines are OFF by default: they were my invention, and the film
        # stills show a flat yellow screen with no visible line structure.
        if self.scanlines:
            step = max(3, int(H / 360))
            for y in range(0, H, step * 3):
                c.create_line(ox, y, ox + w, y, fill=SCANLINE)

    def draw_mouth(self, c, w, h, cx, short, eye_y):
        """The expression's own curve, morphing between faces and parted by sound.

        Three things that make it read as alive rather than mechanical: a change
        of expression MORPHS over TRANSITION_S instead of snapping; the lower lip
        travels about twice as far as the upper, because a human opens their
        mouth mostly by dropping the jaw; and the corners stay shut, tapered by
        sin(pi*t), so the smile or frown survives while he talks.
        """
        tgt_half, tgt_ys = MOUTH_SHAPES.get(
            self.expression, MOUTH_SHAPES[DEFAULT_EXPRESSION])
        old_half, old_ys = MOUTH_SHAPES.get(
            self.shown, MOUTH_SHAPES[DEFAULT_EXPRESSION])
        k = self.blend
        e = k * k * (3.0 - 2.0 * k)          # ease in and out
        half = old_half + (tgt_half - old_half) * e

        gap = 2.0 * (w * EYE_DX)
        lvl = min(1.0, max(0.0, self.level))
        n = len(tgt_ys)
        upper, lower = [], []
        for i in range(n):
            t = i / (n - 1.0)
            y = old_ys[i] + (tgt_ys[i] - old_ys[i]) * e
            open_amt = MOUTH_OPEN * lvl * math.sin(math.pi * t)
            x = cx + (-1.0 + 2.0 * t) * half * gap
            yc = eye_y + y * gap
            upper.append((x, yc - (MOUTH_STROKE * 0.5 + open_amt * LIP_UPPER) * gap))
            lower.append((x, yc + (MOUTH_STROKE * 0.5 + open_amt * LIP_LOWER) * gap))

        pts = []
        for x, y in upper:
            pts += [x, y]
        for x, y in reversed(lower):
            pts += [x, y]
        c.create_polygon(*pts, fill=FACE, outline=FACE, width=1, smooth=True)

    def tick(self):
        # An exception here stops root.after rescheduling, so the face freezes
        # mid-draw with no error anywhere - pythonw has no stderr. Catch it,
        # record it, and keep animating.
        try:
            self._tick()
        except Exception:
            try:
                line = time.strftime('%Y-%m-%d %H:%M:%S') + '  ' + \
                    traceback.format_exc().replace(chr(10), ' | ') + chr(10)
                with open(os.path.join(HOME, 'edgar.log'), 'a', encoding='utf-8') as f:
                    f.write(line)
            except Exception:
                pass
            self.root.after(int(1000 / IDLE_FPS), self.tick)

    def _tick(self):
        self.poll()
        now = time.time()

        # Only on screen while talking. The linger stops him blinking in and out
        # between the sentences of one turn, which is two separate playbacks.
        if self.speaking:
            self.last_speech = now
        if not self.always:
            want = self.speaking or (now - self.last_speech) < HIDE_AFTER_S
            if want != self.visible:
                self.set_visible(want)

        # morph towards a new expression rather than snapping to it
        if self.expression != self.shown:
            if self.blend >= 1.0:
                self.blend = 0.0
            self.blend = min(1.0, self.blend + (1.0 / FPS) / TRANSITION_S)
            if self.blend >= 1.0:
                self.shown = self.expression
        else:
            self.blend = 1.0

        if not self.visible:
            self.level = 0.0
            self.root.after(int(1000 / IDLE_FPS), self.tick)
            return

        # Snap open fast, close slower; tracking a 50 Hz envelope raw stutters.
        k = 0.55 if self.target > self.level else 0.25
        self.level += (self.target - self.level) * k

        # Blink even while talking - a face that never blinks mid-sentence looks
        # switched off. Just rarer than when idle.
        if now >= self.blink_at:
            self.blinking_until = now + BLINK_MS / 1000.0
            span = BLINK_WHILE_TALKING if self.speaking else BLINK_EVERY
            self.blink_at = now + random.uniform(*span)

        self.draw()
        self.root.after(int(1000 / FPS), self.tick)


def claim_single_instance():
    """True if this is the only Edgar, False if one is already running.

    A named mutex rather than a PID file: Windows releases it when the process
    ends, however it ends, so there is no stale lock to clear after a crash or a
    kill - and no chance of a recycled PID locking Edgar out of his own screen.

    Two Edgars both draw on the same monitor, stacked. That happened on
    2026-10-18: the logon task was started a second time because its
    `State: Ready` was read as "the face is dead", when the action is a cmd.exe
    launcher and Ready only ever means the LAUNCHER finished.
    """
    global _INSTANCE_LOCK
    ERROR_ALREADY_EXISTS = 183
    _INSTANCE_LOCK = kernel32.CreateMutexW(None, True, r"Local\this machine-edgar-face")
    if not _INSTANCE_LOCK:
        return True          # cannot lock at all - better a face than no face
    return kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--monitor", default=CFG["monitor"],
                    help="left | right | middle | primary, or an index. A WORD is safer: indices shift when Windows re-detects the displays.")
    ap.add_argument("--size", default="2560x1440",
                    help="WxH centred on that monitor, or 'fill' for the whole thing. "
                         "The 34in is 3440 wide; stretching the face across all of it "
                         "distorts it, so it is sized like the small screens instead.")
    ap.add_argument("--windowed", action="store_true", help="small window, for testing")
    ap.add_argument("--always", action="store_true",
                    help="stay on screen even when silent (default: only while talking)")
    ap.add_argument("--scanlines", action="store_true",
                    help="draw CRT scan lines (not in the film; off by default)")
    ap.add_argument("--no-lock", action="store_true",
                    help="allow a second instance - for testing alongside the logon one")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--list", action="store_true", help="print monitors and exit")
    args = ap.parse_args()

    user32.SetProcessDPIAware()      # geometry must be in physical pixels
    mons = monitors()

    if args.list:
        for i, (x, y, w, h, dev, prim) in enumerate(mons):
            print("[%d] %-12s %5d x %-5d at (%6d,%5d)%s"
                  % (i, dev.replace("\\\\.\\", ""), w, h, x, y, "  PRIMARY" if prim else ""))
        return 0

    if not args.no_lock and not claim_single_instance():
        # pythonw has no stderr, so leave a trace and then go quietly.
        try:
            with open(os.path.join(HOME, 'edgar.log'), 'a', encoding='utf-8') as f:
                f.write(time.strftime('%Y-%m-%d %H:%M:%S') +
                        '  another Edgar already holds the lock - this one exited' + chr(10))
        except Exception:
            pass
        print("  another Edgar is already running - exiting")
        return 0

    # --monitor accepts a POSITION word as well as an index. An index is the
    # EnumDisplayMonitors order, which silently changes when Windows
    # re-detects the displays: on 2026-10-18 the whole desktop shifted by
    # 2560 px and Edgar carried on drawing at -5120, off the edge of every
    # screen, reporting itself perfectly healthy the whole time.
    by_x = sorted(range(len(mons)), key=lambda i: mons[i][0])
    word = str(args.monitor).lower()
    if word in ("left", "l"):
        idx = by_x[0]
    elif word in ("right", "r"):
        idx = by_x[-1]
    elif word in ("middle", "centre", "center", "m"):
        idx = by_x[len(by_x) // 2]
    elif word in ("primary", "p"):
        idx = next((i for i, m in enumerate(mons) if m[5]), by_x[0])
    else:
        try:
            idx = int(args.monitor)
        except ValueError:
            idx = by_x[0]
        if not (0 <= idx < len(mons)):
            idx = by_x[0]
    x, y, w, h, dev, _ = mons[idx]
    print("  using monitor %d: %s %dx%d at (%d,%d)"
          % (idx, dev.replace(chr(92) * 2 + "." + chr(92), ""), w, h, x, y))

    root = tk.Tk()
    root.title("EDGAR")
    root.configure(bg=SURROUND)

    if args.windowed:
        root.geometry("440x300+%d+%d" % (x + 60, y + 60))
    else:
        # Size it, then CENTRE it on the chosen monitor.
        if args.size.lower() == "fill":
            vw, vh = w, h
        else:
            try:
                vw, vh = (int(v) for v in args.size.lower().split("x"))
            except Exception:
                vw, vh = w, h
            vw, vh = min(vw, w), min(vh, h)
        vx = x + (w - vw) // 2
        vy = y + (h - vh) // 2

        # overrideredirect + an explicit rect is true fullscreen on a CHOSEN
        # monitor. tk's own "-fullscreen" attribute follows the window's current
        # monitor, which is not necessarily the one asked for.
        root.overrideredirect(True)
        root.geometry("%dx%d+%d+%d" % (vw, vh, vx, vy))
        root.attributes("-topmost", True)

    Edgar(root, debug=args.debug, always=args.always, scanlines=args.scanlines)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
