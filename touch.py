"""
touch.py — XPT2046 resistive touch driver for the hangar dashboard.

Shares the SPI bus with the ILI9341 display but uses SPI0 CE1 (BCM 7) as
the touch chip-select. Reads X/Y/Z via 3-byte SPI transactions per the
XPT2046 datasheet, applies linear calibration from raw 12-bit values to
display pixels (320×240 landscape), and exposes hit-testing for
display_loop.py.

Calibration values come from .env. Defaults work for "most" MSP3218-style
3.2" panels in landscape — refine via `display_loop.py --calibrate` once
the panel is wired up.

Lazy import of `spidev` so this module imports cleanly on machines
without it (e.g., local dev on macOS).
"""

import time
from pathlib import Path

import config

# SPI device for the touch panel. CE1 = BCM 7 (header pin 26). The display
# uses CE0 (BCM 8); both share the SPI0 bus.
SPI_BUS = 0
SPI_DEV = 1

# XPT2046 command bytes — see datasheet sec 4 "Conversion Channel Selection".
# We use single-ended 12-bit reads at normal power. Top bit = start, next 3
# = channel select, then mode + reference + power-down bits.
_CMD_X  = 0xD0  # 0b1101_0000 — X position
_CMD_Y  = 0x90  # 0b1001_0000 — Y position
_CMD_Z1 = 0xB0  # Z1 (pressure indicator A)

# Below this Z reading there's no finger on the panel. Sensible default for
# a 3.3V XPT2046; tune via TOUCH_PRESSURE_THRESHOLD if the chip is twitchy.
PRESSURE_THRESHOLD_DEFAULT = 150

# Default calibration for a 3.2" MSP3218 in landscape rotation. Raw 12-bit
# XPT2046 values corresponding to the edges of the visible area. AXES_SWAP
# is true because the XPT2046's "X" axis maps to the display's "Y" axis
# when the ILI9341 is rotated to landscape — universally true for this
# panel family.
DEFAULTS = {
    "TOUCH_X_MIN":              "300",
    "TOUCH_X_MAX":              "3900",
    "TOUCH_Y_MIN":              "300",
    "TOUCH_Y_MAX":              "3900",
    "TOUCH_AXES_SWAP":          "1",
    "TOUCH_X_INVERT":           "0",
    "TOUCH_Y_INVERT":           "0",
    "TOUCH_PRESSURE_THRESHOLD": str(PRESSURE_THRESHOLD_DEFAULT),
}


# ---------------------------------------------------------------------------
# Calibration loading
# ---------------------------------------------------------------------------

def load_cal():
    """Read TOUCH_* keys from .env; fall back to DEFAULTS for missing ones."""
    env = config.load_env()
    cal = {}
    for k, default in DEFAULTS.items():
        cal[k] = env.get(k, default).strip() or default
    return cal


def _cal_int(cal, key):
    return int(cal[key])


def _cal_bool(cal, key):
    return cal[key] == "1"


# ---------------------------------------------------------------------------
# TouchReader — opens spidev, reads raw values, applies calibration
# ---------------------------------------------------------------------------

class TouchReader:
    """Manages the SPI handle and the per-iteration raw → pixel mapping."""

    def __init__(self, cal=None):
        from spidev import SpiDev
        self._spi = SpiDev()
        self._spi.open(SPI_BUS, SPI_DEV)
        # XPT2046 supports up to 2.5 MHz; 1 MHz is safer over long jumper
        # leads and well under the chip's spec.
        self._spi.max_speed_hz = 1_000_000
        self._spi.mode = 0
        self.cal = cal or load_cal()

    def close(self):
        try:
            self._spi.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _read_12bit(self, cmd_byte):
        """One 3-byte SPI transaction → 12-bit value."""
        result = self._spi.xfer2([cmd_byte, 0x00, 0x00])
        # XPT2046 returns 12-bit value left-aligned across the next 2 bytes.
        return ((result[1] << 8) | result[2]) >> 4

    def read_raw_with_pressure(self):
        """One sample: returns (x_raw, y_raw, z) or None if no finger down."""
        z = self._read_12bit(_CMD_Z1)
        threshold = _cal_int(self.cal, "TOUCH_PRESSURE_THRESHOLD")
        if z < threshold:
            return None
        # Read X and Y while finger is still down. XPT2046 best practice is
        # to do a quick X-Y burst rather than re-checking Z between them.
        x = self._read_12bit(_CMD_X)
        y = self._read_12bit(_CMD_Y)
        return (x, y, z)

    def read_averaged(self, n=4):
        """Average N samples for noise filtering. None if < n/2 samples valid."""
        samples = []
        for _ in range(n):
            r = self.read_raw_with_pressure()
            if r is not None:
                samples.append((r[0], r[1]))
            time.sleep(0.001)
        if len(samples) < max(1, n // 2):
            return None
        avg_x = sum(s[0] for s in samples) // len(samples)
        avg_y = sum(s[1] for s in samples) // len(samples)
        return (avg_x, avg_y)

    def to_pixels(self, raw_x, raw_y, width=320, height=240):
        """Apply linear calibration: raw 12-bit → screen pixels."""
        x_min = _cal_int(self.cal, "TOUCH_X_MIN")
        x_max = _cal_int(self.cal, "TOUCH_X_MAX")
        y_min = _cal_int(self.cal, "TOUCH_Y_MIN")
        y_max = _cal_int(self.cal, "TOUCH_Y_MAX")
        swap  = _cal_bool(self.cal, "TOUCH_AXES_SWAP")
        x_inv = _cal_bool(self.cal, "TOUCH_X_INVERT")
        y_inv = _cal_bool(self.cal, "TOUCH_Y_INVERT")

        if swap:
            raw_x, raw_y = raw_y, raw_x
            x_min, x_max, y_min, y_max = y_min, y_max, x_min, x_max

        # Guard against divide-by-zero if the user mis-calibrated.
        x_range = max(x_max - x_min, 1)
        y_range = max(y_max - y_min, 1)

        x_px = (raw_x - x_min) * width / x_range
        y_px = (raw_y - y_min) * height / y_range

        if x_inv:
            x_px = width - x_px
        if y_inv:
            y_px = height - y_px

        # Clamp into screen bounds — calibration drift can push slightly out.
        x_px = max(0, min(width - 1, int(x_px)))
        y_px = max(0, min(height - 1, int(y_px)))
        return (x_px, y_px)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def point_in_rect(x, y, rect):
    """rect = (x1, y1, x2, y2). Inclusive bounds."""
    x1, y1, x2, y2 = rect
    return x1 <= x <= x2 and y1 <= y <= y2


# ---------------------------------------------------------------------------
# Interactive corner calibration (called from `display_loop.py --calibrate`)
# ---------------------------------------------------------------------------

def run_calibration():
    """
    Walk through the 4 corners, capture averaged raw values, and print
    suggested .env values.

    Returns a dict matching DEFAULTS' keys, ready for the user to paste
    into .env. Prompts go to stdout so the user can read them over ssh.
    """
    print("=== Touch calibration ===")
    print("Tap each corner of the display when prompted. Hold for ~1 second.")
    print()

    corners = [
        ("TOP-LEFT     (0, 0)",         "tl"),
        ("TOP-RIGHT    (319, 0)",       "tr"),
        ("BOTTOM-LEFT  (0, 239)",       "bl"),
        ("BOTTOM-RIGHT (319, 239)",     "br"),
    ]
    samples = {}

    with TouchReader(cal=DEFAULTS) as t:
        # Override threshold during calibration — we want low-Z taps to count.
        t.cal = dict(DEFAULTS, TOUCH_PRESSURE_THRESHOLD="50")

        for prompt, key in corners:
            print(f"  → Tap the {prompt} corner...")
            # Wait for steady touch
            started = None
            captured = []
            while True:
                r = t.read_raw_with_pressure()
                if r is None:
                    if captured and len(captured) >= 8:
                        break  # finger lifted, we have enough
                    started = None
                    captured = []
                else:
                    if started is None:
                        started = time.time()
                    captured.append((r[0], r[1]))
                    if time.time() - started > 1.5 and len(captured) >= 16:
                        break
                time.sleep(0.02)
            avg_x = sum(s[0] for s in captured) // len(captured)
            avg_y = sum(s[1] for s in captured) // len(captured)
            print(f"    raw=({avg_x}, {avg_y})  ({len(captured)} samples)")
            samples[key] = (avg_x, avg_y)
            # Wait for finger off the screen before next prompt
            while t.read_raw_with_pressure() is not None:
                time.sleep(0.05)
            time.sleep(0.3)

    # Derive calibration from the 4 corner samples. The raw axes are likely
    # swapped from the display axes in landscape rotation (AXES_SWAP=1).
    # tl, tr, bl, br — in display coords.
    tl, tr, bl, br = samples["tl"], samples["tr"], samples["bl"], samples["br"]

    # Find which raw axis varies most along the display's X (left↔right)
    dx_along_x = abs(tr[0] - tl[0])
    dy_along_x = abs(tr[1] - tl[1])
    axes_swap = dy_along_x > dx_along_x

    if axes_swap:
        # Raw "X" is the display Y axis; raw "Y" is the display X axis.
        x_min_raw = min(tl[1], bl[1])
        x_max_raw = max(tr[1], br[1])
        y_min_raw = min(tl[0], tr[0])
        y_max_raw = max(bl[0], br[0])
    else:
        x_min_raw = min(tl[0], bl[0])
        x_max_raw = max(tr[0], br[0])
        y_min_raw = min(tl[1], tr[1])
        y_max_raw = max(bl[1], br[1])

    x_invert = (tr[1 if axes_swap else 0] < tl[1 if axes_swap else 0])
    y_invert = (bl[0 if axes_swap else 1] < tl[0 if axes_swap else 1])

    out = {
        "TOUCH_X_MIN":              str(x_min_raw),
        "TOUCH_X_MAX":              str(x_max_raw),
        "TOUCH_Y_MIN":              str(y_min_raw),
        "TOUCH_Y_MAX":              str(y_max_raw),
        "TOUCH_AXES_SWAP":          "1" if axes_swap else "0",
        "TOUCH_X_INVERT":           "1" if x_invert else "0",
        "TOUCH_Y_INVERT":           "1" if y_invert else "0",
        "TOUCH_PRESSURE_THRESHOLD": str(PRESSURE_THRESHOLD_DEFAULT),
    }

    print()
    print("=== Suggested .env values ===")
    print("Add these lines to /usr/lib/cgi-bin/remote-switch/.env:")
    print()
    for k, v in out.items():
        print(f"  {k}={v}")
    print()
    return out


if __name__ == "__main__":
    run_calibration()
