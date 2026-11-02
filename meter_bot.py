"""Holds and releases spacebar to keep a tick inside the yellow zone of an arc meter.

Setup:   pip install mss numpy
Run:     python meter_bot.py        (run as admin if the game ignores the key)

F6   calibrate (only needed once, or if the meter moves on screen)
F7   start / stop
F8   quit

These keys work from any window. Stops by itself at :29 and :59, ready for the
minigame to be restarted.

The yellow zone is re-read from the screen every frame, so a different range
each session needs no changes.
"""
import ctypes
import json
import math
import struct
import time
from collections import deque
from ctypes import wintypes
from pathlib import Path

import numpy as np

# ---- Settings you may want to tweak -----------------------------------------
INVERT = False        # True if holding space moves the tick right-to-left
TARGET = 0.5          # middle of the hold/release range: 0 = low edge, 1 = high edge
TRIGGER_RANGE = 0.7   # share of the safe zone between pressing and releasing; bigger = fewer inputs
MIN_PRESS = 0.08      # shortest press or release, in seconds
BAND = 0.09           # how far either side of the arc to look, as a share of its radius
TOLERANCE = 60        # zone colour match looseness; raise it if detection is patchy
TICK_COLOUR = (255, 255, 255)  # blue, green, red
TICK_TOLERANCE = 120  # tick colour match looseness
MIN_ZONE_COVER = 0.25 # share of the band that must be zone colour, which ignores specks
MIN_TICK_RUN = 0.3    # share of the band a solid white line must span to be a candidate
TICK_OVERHANG = 0.25  # how far past the arc's edges the tick must reach inside the zone
ZONE_MARGIN = 0.2     # share of the gold at each end to treat as unsafe
MAX_TICK_WIDTH = 0.04 # wider white marks than this share of the arc count as text
MIN_TICK_HEIGHT = 0.7 # white marks shorter than this share of the tick count as text
CHECK_HZ = 60         # checks per second
MAX_JUMP = 0.08       # the tick can't move further than this between frames
KEY_DELAY = 0.04      # seconds between deciding and the tick responding; raise it if it overshoots
MIN_PHASE = 0.08      # shortest stretch of movement used to measure the tick's speed
PHASE_SETTLE = 0.05   # seconds to ignore after the key changes when measuring speed
MIN_SPEED_RUNS = 3    # holds and releases to measure before running on the learned speeds
SPEED_RUNS = 8        # how many measurements the learned speeds are based on
MATCH_RANGE = 0.012   # how close a mark must be to the estimate to count as the tick
MATCH_GROWTH = 0.02   # how much that widens per second while the tick is hidden
MAX_MATCH = 0.1       # the widest that can get
NUDGE = 0.15          # share of the error a matching mark corrects each check
SPEED_TRIM = 0.02     # how fast matching marks fine-tune the learned speeds
MAX_SPEED_TRIM = 0.1  # the most that fine-tuning can move a speed from its measured value
MAX_TIMER = 5        # seconds with the tick hidden before starting the learning again
MAX_BLIND_HOLD = 0.3  # seconds to keep holding a hidden tick before any timing is learned
MAX_ZONE_GONE = 1.0   # seconds without the zone before treating the session as over
NO_RESPONSE = 2.0     # seconds of holding with no movement before warning about it
STOP_MINUTES = (29, 59)  # minutes of each hour to stop at, by this PC's clock
STOP_SECOND = 0       # second within those minutes to stop at
# -----------------------------------------------------------------------------

CONFIG = Path(__file__).with_name("meter_config.json")
SAMPLES = 400
SPREAD = 2
VK_F6, VK_F7, VK_F8 = 0x75, 0x76, 0x77
VK_SPACE = 0x20
SCAN_SPACE = 0x39


def circle_through(p1, p2, p3):
    (ax, ay), (bx, by), (cx, cy) = p1, p2, p3
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-6:
        raise ValueError("The three arc points are in a straight line; calibrate again.")
    a2, b2, c2 = ax**2 + ay**2, bx**2 + by**2, cx**2 + cy**2
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def build_sampler(cfg):
    """Pixel lookup grids shaped (SAMPLES, radii), running start-of-arc to end-of-arc."""
    cx, cy, r = circle_through(cfg["start"], cfg["mid"], cfg["end"])
    angle = lambda p: math.atan2(p[1] - cy, p[0] - cx)
    a0, am, a1 = angle(cfg["start"]), angle(cfg["mid"]), angle(cfg["end"])
    # Sweep whichever way round the circle passes through the middle point.
    cw = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) < cw:
        sweep = cw
    else:
        sweep = cw - 2 * math.pi
    thetas = a0 + np.linspace(0, 1, SAMPLES) * sweep
    band_px = max(4, round(r * BAND))
    radii = r + np.arange(-band_px, band_px + 1)
    xs = (cx + np.outer(np.cos(thetas), radii)).round().astype(int)
    ys = (cy + np.outer(np.sin(thetas), radii)).round().astype(int)
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    return xs - box[0], ys - box[1], box


def matches(pixels, colour, tolerance=TOLERANCE):
    return np.abs(pixels.astype(int) - np.array(colour)).sum(axis=-1) < tolerance


def longest_runs(mask):
    """Longest unbroken line of True across the band, for each position along the arc."""
    current = best = np.zeros(mask.shape[0], dtype=int)
    for column in mask.T:
        current = (current + 1) * column
        best = np.maximum(best, current)
    return best


def find_marks(bright, need, overhang):
    """Lists (position, height) for each mark in the mask that could be the tick."""
    # Widen sideways so a thin streak can't slip between sampling points.
    spread = bright.copy()
    for shift in range(1, SPREAD + 1):
        spread[shift:] |= bright[:-shift]
        spread[:-shift] |= bright[shift:]
    runs = longest_runs(spread)
    tall = runs >= need
    if overhang:
        # Text can touch one side of the arc, but the tick sticks out of both.
        both_sides = (spread[:, :overhang].mean(axis=1) >= 0.5) & (spread[:, -overhang:].mean(axis=1) >= 0.5)
        tall &= both_sides | (need == need.min())
    edges = np.flatnonzero(np.diff(np.concatenate(([0], tall.astype(int), [0]))))
    marks = []
    for start, stop in zip(edges[::2], edges[1::2]):
        if (stop - start) / SAMPLES <= MAX_TICK_WIDTH:
            marks.append(((start + stop - 1) / 2 / (SAMPLES - 1), int(runs[start:stop].max())))
    return marks


def scan_frame(frame, xs, ys, yellow):
    """Returns (zone, candidates): zone is (low, high) or None, and candidates
    lists (position, height) for each mark that could be the tick."""
    pixels = frame[ys, xs]
    scale = SAMPLES - 1
    cover = matches(pixels, yellow).sum(axis=1)
    found = np.flatnonzero(cover >= MIN_ZONE_COVER * xs.shape[1])
    zone = (found.min() / scale, found.max() / scale) if found.size else None

    white = matches(pixels, TICK_COLOUR, TICK_TOLERANCE)
    band = xs.shape[1]
    need = np.full(SAMPLES, MIN_TICK_RUN * band)
    if not found.size:
        return zone, find_marks(white, need, 0)

    # Inside the zone the tick has to reach past the arc's own thickness to count.
    first, last = found.min(), found.max()
    gold = matches(pixels, yellow, TICK_TOLERANCE)
    gold[:first] = gold[last + 1:] = False  # gold only counts as tick inside the zone
    arc = np.median(longest_runs(gold)[found])
    pad = SPREAD + 1
    need[max(0, first - pad):last + pad + 1] = max(need[0], arc + TICK_OVERHANG * (band - arc))
    overhang = max(1, int((band - arc) // 2))
    # The tick is white unless it has reached the red, when it pulses gold too.
    return zone, find_marks(white, need, overhang) + find_marks(white | gold, need, overhang)


class Tracker:
    """Keeps its own estimate of the tick from the key state and the learned speeds,
    so it needs only small nudges from the screen. A mark can't drag the estimate
    far or stop it moving, which is what stops text from taking over."""

    def __init__(self):
        self.lows, self.highs = deque(maxlen=90), deque(maxlen=90)
        self.heights = deque(maxlen=90)
        self.estimate = None
        self.holding = False
        self.flipped_at = self.matched_at = self.updated_at = self.zone_at = 0.0
        self.phase_start = self.phase_end = None
        self.forget_speeds()

    def forget_speeds(self):
        self.samples = {True: deque(maxlen=SPEED_RUNS), False: deque(maxlen=SPEED_RUNS)}
        self.speed = {True: None, False: None}

    def learned(self):
        return all(self.speed.values())

    def direction(self):
        return 1 if self.holding != INVERT else -1

    def flip(self, want, now):
        if want == self.holding or now - self.flipped_at < MIN_PRESS:
            return
        if not self.learned() and self.phase_start and self.phase_end:
            (start_at, start), (end_at, end) = self.phase_start, self.phase_end
            if end_at - start_at >= MIN_PHASE:
                measured = (end - start) * self.direction() / (end_at - start_at)
                if measured > 0:
                    self.samples[self.holding].append(measured)
                    if len(self.samples[self.holding]) >= MIN_SPEED_RUNS:
                        self.speed[self.holding] = np.median(self.samples[self.holding])
        self.holding = want
        self.flipped_at = now
        self.phase_start = self.phase_end = None

    def tall_marks(self, candidates):
        min_height = np.median(self.heights) * MIN_TICK_HEIGHT if self.heights else 0
        return [c for c in candidates if c[1] >= min_height]

    def lock_on(self, marks, now):
        """Nothing learned yet, so follow the screen directly."""
        if self.estimate is None:
            mark = max(marks, key=lambda c: c[1], default=None)
        else:
            near = [c for c in marks if abs(c[0] - self.estimate) <= MAX_JUMP]
            mark = min(near, key=lambda c: abs(c[0] - self.estimate), default=None)
        if mark is None:
            if now - self.matched_at > MAX_BLIND_HOLD:
                self.estimate = None
                self.flip(False, now)
            return
        self.estimate, self.matched_at = mark[0], now
        self.heights.append(mark[1])
        if now - self.flipped_at >= PHASE_SETTLE:  # skip the moment the key changes
            self.phase_start = self.phase_start or (now, mark[0])
            self.phase_end = (now, mark[0])

    def nudge(self, marks, now, step):
        """Learned: the estimate runs by itself, and marks only correct it a little."""
        hidden_for = now - self.matched_at
        reach = min(MAX_MATCH, MATCH_RANGE + MATCH_GROWTH * hidden_for)
        near = [c for c in marks if abs(c[0] - self.estimate) <= reach]
        mark = min(near, key=lambda c: abs(c[0] - self.estimate), default=None)
        if mark is None:
            if hidden_for > MAX_TIMER:  # lost it for too long: start the learning again
                self.forget_speeds()
                self.estimate = None
                self.flip(False, now)
            return
        error = mark[0] - self.estimate
        # Never pull harder than half the tick's own movement, so it can't get stuck on a mark.
        self.estimate += float(np.clip(error * NUDGE, -step / 2, step / 2))
        base = np.median(self.samples[self.holding])
        trimmed = self.speed[self.holding] + error * self.direction() * SPEED_TRIM
        self.speed[self.holding] = float(np.clip(trimmed, base * (1 - MAX_SPEED_TRIM), base * (1 + MAX_SPEED_TRIM)))
        self.matched_at = now
        self.heights.append(mark[1])

    def update(self, scan, now):
        zone, candidates = scan
        if zone is not None:
            self.lows.append(zone[0])
            self.highs.append(zone[1])
            self.zone_at = now
        elif now - self.zone_at > MAX_ZONE_GONE:  # meter gone, so forget everything
            self.__init__()
        if not self.lows:
            return False
        # The zone holds still, so the median ignores frames where text covers an edge.
        low, high = np.median(self.lows), np.median(self.highs)
        # The real target is narrower than the gold, so trim both ends.
        trim = (high - low) * ZONE_MARGIN
        low, high = low + trim, high - trim
        aim = low + (high - low) * TARGET
        hold_below = aim - (high - low) * TRIGGER_RANGE / 2
        release_above = aim + (high - low) * TRIGGER_RANGE / 2

        elapsed = now - self.updated_at
        self.updated_at = now
        marks = self.tall_marks(candidates)
        if self.learned() and self.estimate is not None:
            step = self.speed[self.holding] * elapsed
            self.estimate = min(1.0, max(0.0, self.estimate + step * self.direction()))
            self.nudge(marks, now, step)
        else:
            self.lock_on(marks, now)
        if self.estimate is None:
            return self.holding

        # Switch early to allow for the delay before the key takes effect.
        moving_up = self.holding != INVERT
        coast = (self.speed[self.holding] or 0) * KEY_DELAY
        if moving_up and self.estimate + coast >= release_above:
            self.flip(not self.holding, now)
        elif not moving_up and self.estimate - coast <= hold_below:
            self.flip(not self.holding, now)
        return self.holding


# ---- Windows input ----------------------------------------------------------
class _KeyInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


class _Input(ctypes.Structure):
    class _U(ctypes.Union):
        # The padding member gives the union the size of the largest INPUT variant.
        _fields_ = [("ki", _KeyInput), ("pad", ctypes.c_byte * 32)]
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]


def send_input_space(down):
    # Scan codes, because many games ignore virtual-key events.
    flags = 0x0008 | (0 if down else 0x0002)  # KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP
    event = _Input(type=1)
    event.u.ki = _KeyInput(0, SCAN_SPACE, flags, 0, 0)
    user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input))


def post_space(hwnd, down):
    """Sends space straight to one window, focused or not. Returns False if refused."""
    message = 0x0100 if down else 0x0101  # WM_KEYDOWN, WM_KEYUP
    lparam = 1 | (SCAN_SPACE << 16) | (0 if down else 0xC0000000)
    return bool(user32.PostMessageW(hwnd, message, VK_SPACE, lparam))


class SpaceKey:
    """Space for the game only, so it never lands in whatever else has focus."""

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.typed = False  # the current press went through SendInput

    def set(self, down):
        if down:
            self.typed = user32.GetForegroundWindow() == self.hwnd
        if self.typed:
            send_input_space(down)
            return True
        return post_space(self.hwnd, down)


def pressed(vk):
    return ctypes.windll.user32.GetAsyncKeyState(vk) & 1


def cursor():
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def window_at(x, y):
    return user32.GetAncestor(user32.WindowFromPoint(wintypes.POINT(x, y)), 2)  # GA_ROOT


def window_title(hwnd):
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buffer, 256)
    return buffer.value


def client_origin(hwnd):
    p = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(p))
    return p.x, p.y


def grab_window(hwnd):
    """The window's own contents as BGR, even if it's covered by other windows."""
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    width, height = rect.right, rect.bottom
    if width <= 0 or height <= 0:
        return None
    screen_dc = user32.GetDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        user32.PrintWindow(hwnd, memory_dc, 3)  # PW_CLIENTONLY | PW_RENDERFULLCONTENT
        # BITMAPINFOHEADER for a top-down 32-bit image.
        header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = np.empty((height, width, 4), dtype=np.uint8)
        gdi32.GetDIBits(memory_dc, bitmap, 0, height, pixels.ctypes.data, header, 0)
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, screen_dc)
    return pixels[:, :, :3]


def stop_due(now, last_stop):
    """Returns the clock slot to stop for, if one has arrived and hasn't been handled."""
    slot = (now.tm_yday, now.tm_hour, now.tm_min)
    if now.tm_min in STOP_MINUTES and now.tm_sec >= STOP_SECOND and slot != last_stop:
        return slot
    return None


# ---- Main -------------------------------------------------------------------
def grab(sct, box):
    shot = sct.grab({"left": box[0], "top": box[1],
                     "width": box[2] - box[0], "height": box[3] - box[1]})
    return np.asarray(shot)[:, :, :3]  # BGR


def pick_window(cfg):
    print("  Hover over the game window and press F6...")
    while not pressed(VK_F6):
        time.sleep(0.01)
    hwnd = window_at(*cursor())
    cfg["window"] = window_title(hwnd)
    cfg["origin"] = list(client_origin(hwnd))
    CONFIG.write_text(json.dumps(cfg, indent=2))
    print(f"  Using the window \"{cfg['window']}\".")
    return hwnd


def calibrate(sct):
    steps = [("start", "the LEFT end of the arc"),
             ("mid", "the TOP middle of the arc"),
             ("end", "the RIGHT end of the arc"),
             ("yellow", "the yellow zone")]
    cfg = {}
    for key, label in steps:
        print(f"  Hover over {label} and press F6...")
        while not pressed(VK_F6):
            time.sleep(0.01)
        x, y = cursor()
        if key == "yellow":
            cfg[key] = grab(sct, (x, y, x + 1, y + 1))[0, 0].tolist()
        else:
            cfg[key] = [x, y]
    hwnd = pick_window(cfg)
    print(f"  Saved to {CONFIG.name}.")
    return cfg, hwnd


def find_game(cfg):
    hwnd = user32.FindWindowW(None, cfg["window"]) if cfg.get("window") else None
    return hwnd or pick_window(cfg)


class Capture:
    """Reads the game window directly when it can, otherwise the visible screen."""

    def __init__(self, sct, hwnd, cfg, box):
        self.sct, self.hwnd, self.box = sct, hwnd, box
        ox, oy = cfg["origin"]
        self.window_box = (box[0] - ox, box[1] - oy, box[2] - ox, box[3] - oy)
        self.direct = self.crop_window() is not None and self.crop_window().max() > 8
        if self.direct:
            print("Reading the game window directly, so it can be covered or unfocused.")
        else:
            print("The game can't be read directly, so it must stay visible on screen "
                  "(it can still be unfocused, e.g. on another monitor).")

    def crop_window(self):
        frame = grab_window(self.hwnd)
        x0, y0, x1, y1 = self.window_box
        if frame is None or x0 < 0 or y0 < 0 or y1 > frame.shape[0] or x1 > frame.shape[1]:
            return None
        return frame[y0:y1, x0:x1]

    def frame(self):
        if self.direct:
            crop = self.crop_window()
            if crop is not None:
                return crop
        return grab(self.sct, self.box)


def main():
    import mss
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels on scaled displays

    # Newer mss versions renamed mss.mss to mss.MSS.
    with getattr(mss, "MSS", mss.mss)() as sct:
        cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else None
        if cfg is None:
            print("No calibration yet.")
            cfg, hwnd = calibrate(sct)
        else:
            hwnd = find_game(cfg)
        xs, ys, box = build_sampler(cfg)
        capture = Capture(sct, hwnd, cfg, box)
        space = SpaceKey(hwnd)
        print("Ready. F7 start/stop, F6 recalibrate, F8 quit.")
        print(f"Stops by itself at minutes {STOP_MINUTES} of each hour.")

        running = holding = False
        tracker = Tracker()
        last_stop = None
        held_from = None  # (time, position) when the current hold began
        warned = False
        try:
            while not pressed(VK_F8):
                if pressed(VK_F6):
                    space.set(False)
                    running = holding = False
                    tracker = Tracker()
                    cfg, hwnd = calibrate(sct)
                    xs, ys, box = build_sampler(cfg)
                    capture = Capture(sct, hwnd, cfg, box)
                    space = SpaceKey(hwnd)
                if pressed(VK_F7):
                    running = not running
                    tracker = Tracker()
                    held_from = None
                    # Starting during a stop minute shouldn't stop it again straight away.
                    last_stop = stop_due(time.localtime(), None) or last_stop
                    print("Running" if running else "Stopped")
                    if not running and holding:
                        space.set(False)
                        holding = False
                slot = stop_due(time.localtime(), last_stop)
                if slot:
                    last_stop = slot
                    if running:
                        running = False
                        tracker = Tracker()
                        if holding:
                            space.set(False)
                            holding = False
                        print(f"Stopped automatically at {time.strftime('%H:%M:%S')}. "
                              "Restart the minigame, then press F7.")
                if not running:
                    time.sleep(0.02)
                    continue

                started = time.perf_counter()
                scan = scan_frame(capture.frame(), xs, ys, cfg["yellow"])
                want = tracker.update(scan, started)
                if want != holding:
                    if not space.set(want) and not warned:
                        print("Windows refused the key. Run this as administrator.")
                        warned = True
                    holding = want
                    held_from = (started, tracker.estimate) if want else None
                # Spot a game that ignores keys while it isn't focused.
                if holding and held_from and not warned and held_from[1] is not None \
                        and tracker.estimate is not None and started - held_from[0] > NO_RESPONSE \
                        and abs(tracker.estimate - held_from[1]) < 0.005:
                    print("The tick isn't responding to space. This game may only accept "
                          "keys while it's focused.")
                    warned = True
                time.sleep(max(0, 1 / CHECK_HZ -(time.perf_counter() - started)))
        finally:
            space.set(False)


if __name__ == "__main__":
    main()
