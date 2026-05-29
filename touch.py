"""
touch.py — FT6336U capacitive touch driver for the hangar dashboard.

I2C-based capacitive controller on the Haldzemo 3.5" IPS display. Reports
pre-calibrated pixel coordinates directly — no per-panel calibration step,
no raw → pixel mapping, no axis flipping (unless the chip's native
orientation doesn't match the LCD's landscape mounting, in which case the
TOUCH_SWAP_XY / TOUCH_INVERT_* env keys cover it).

Replaces the previous XPT2046/SPI driver. The two chips share nothing —
different bus, different command set, different calibration model — so this
is a full rewrite, not a port.

Lazy import of `smbus2` so this module imports cleanly on macOS dev.
"""

import time
from pathlib import Path

import config

# ---------------------------------------------------------------------------
# Bus + chip config
# ---------------------------------------------------------------------------

# Pi's primary I2C bus. Bus 0 is reserved on most Pi models for HAT EEPROM;
# bus 1 is the GPIO-exposed one (SDA on header pin 3, SCL on pin 5).
I2C_BUS_DEFAULT  = 1

# FT6336U default 7-bit address. Some FocalTech variants land on 0x70 or
# 0x15 — override via TOUCH_I2C_ADDR in .env if a scan shows otherwise.
I2C_ADDR_DEFAULT = 0x38

# FT6336U register map (FocalTech FT6336U datasheet, "Touch data report" §7).
#   0x02 — TD_STATUS: low nibble = current touch count (0–2 for this chip)
#   0x03 — TOUCH1_XH: low 4 bits = X high byte, upper 2 bits = event flag
#   0x04 — TOUCH1_XL: X low byte
#   0x05 — TOUCH1_YH: low 4 bits = Y high byte, upper 4 bits = touch ID
#   0x06 — TOUCH1_YL: Y low byte
REG_TD_STATUS  = 0x02
REG_TOUCH1_XH  = 0x03
REG_BLOCK_LEN  = 5  # read 0x02..0x06 in one block to avoid mid-read race

# The chip reports coordinates in its *configured* resolution range. On this
# Haldzemo board it's wired for the panel's native portrait orientation
# (320 wide × 480 tall). The LCD is mounted in landscape, so we swap X/Y
# before hit-testing against the renderer's 480×320 coordinate space.
#
# Defaults below were verified empirically on the Haldzemo 3.5" board on
# 2026-05-29: with SWAP_XY=1 / INVERT_X=0 / INVERT_Y=1, all four corner
# taps mapped to the correct landscape pixel quadrant. The Y invert is
# required because the chip's portrait Y axis has 0 at the panel's
# bottom-physical edge and increases toward the top, opposite of what the
# renderer expects. If a different board ships with the chip pre-configured
# for landscape or a different orientation, override these in .env.
DEFAULTS = {
    "TOUCH_I2C_BUS":  str(I2C_BUS_DEFAULT),
    "TOUCH_I2C_ADDR": hex(I2C_ADDR_DEFAULT),
    "TOUCH_SWAP_XY":  "1",
    "TOUCH_INVERT_X": "0",
    "TOUCH_INVERT_Y": "1",
}


def load_cfg():
    """Read TOUCH_* keys from .env; fall back to DEFAULTS for missing ones."""
    env = config.load_env()
    cfg = {}
    for k, default in DEFAULTS.items():
        cfg[k] = env.get(k, default).strip() or default
    return cfg


def _cfg_int(cfg, key):
    v = cfg[key]
    return int(v, 16) if v.startswith("0x") else int(v)


def _cfg_bool(cfg, key):
    return cfg[key] == "1"


# ---------------------------------------------------------------------------
# TouchReader — opens smbus, decodes the 5-byte block into a screen-pixel tap
# ---------------------------------------------------------------------------

class TouchReader:
    """Manages the I2C handle and per-poll register read.

    Construct with the display's landscape dimensions (480, 320 for this
    panel). Coordinates returned from `read_touch()` are in that space,
    suitable for direct hit-testing against `display.HEATER_BUTTON_RECT`.
    """

    def __init__(self, cfg=None, screen_w=480, screen_h=320):
        from smbus2 import SMBus
        self.cfg = cfg or load_cfg()
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.addr = _cfg_int(self.cfg, "TOUCH_I2C_ADDR")
        self._swap_xy = _cfg_bool(self.cfg, "TOUCH_SWAP_XY")
        self._inv_x   = _cfg_bool(self.cfg, "TOUCH_INVERT_X")
        self._inv_y   = _cfg_bool(self.cfg, "TOUCH_INVERT_Y")
        self._bus = SMBus(_cfg_int(self.cfg, "TOUCH_I2C_BUS"))

    def close(self):
        try:
            self._bus.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _read_block(self):
        """One 5-byte block read of the touch data registers."""
        return self._bus.read_i2c_block_data(self.addr, REG_TD_STATUS, REG_BLOCK_LEN)

    def read_touch(self):
        """Return (x, y) in screen-pixel coords, or None if no finger down.

        Capacitive controllers can momentarily report 0 touches between
        contacts; treat that as "no touch". The chip is debounced internally
        — additional software debounce lives in display_loop.py.
        """
        try:
            data = self._read_block()
        except OSError:
            # I2C bus glitch (NAK, bus busy, etc.) — caller treats this as
            # "no touch this tick" and keeps polling. Persistent failures
            # show up as repeated stderr lines from display_loop.py.
            return None

        n_touches = data[0] & 0x0F
        if n_touches == 0:
            return None

        # Decode touch #1 (we only act on single touch). High-bit masks per
        # datasheet — upper 2 bits of XH are the event flag, upper 4 bits
        # of YH are the touch ID. Mask to get pure coordinate bits.
        x_raw = ((data[1] & 0x0F) << 8) | data[2]
        y_raw = ((data[3] & 0x0F) << 8) | data[4]

        # Chip reports portrait coords by default; LCD is landscape-mounted.
        if self._swap_xy:
            x_raw, y_raw = y_raw, x_raw

        if self._inv_x:
            x_raw = self.screen_w - 1 - x_raw
        if self._inv_y:
            y_raw = self.screen_h - 1 - y_raw

        # Clamp into screen bounds — a tap just outside the active area
        # can land slightly out-of-range; clamping keeps hit-test safe.
        x = max(0, min(self.screen_w - 1, x_raw))
        y = max(0, min(self.screen_h - 1, y_raw))
        return (x, y)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def point_in_rect(x, y, rect):
    """rect = (x1, y1, x2, y2). Inclusive bounds."""
    x1, y1, x2, y2 = rect
    return x1 <= x <= x2 and y1 <= y <= y2


# ---------------------------------------------------------------------------
# Bus scan — replaces the XPT2046 calibration walkthrough. With capacitive,
# the only "configuration" needed is confirming the chip's I2C address and
# that swap/invert match the physical mount.
# ---------------------------------------------------------------------------

def run_scan():
    """Probe the I2C bus and print a brief tap-monitor.

    Replaces `--calibrate` from the old XPT2046 driver. Useful at install
    time to:
      1. Confirm the FT6336U appears at the expected address (0x38).
      2. Watch live coordinates while tapping known points on the panel,
         to decide whether TOUCH_SWAP_XY / TOUCH_INVERT_* need flipping.
    """
    cfg = load_cfg()
    addr = _cfg_int(cfg, "TOUCH_I2C_ADDR")
    bus_n = _cfg_int(cfg, "TOUCH_I2C_BUS")
    print(f"=== I2C scan on bus {bus_n} ===")
    from smbus2 import SMBus
    with SMBus(bus_n) as bus:
        found = []
        for a in range(0x03, 0x78):
            try:
                bus.read_byte(a)
                found.append(a)
            except OSError:
                continue
        if not found:
            print("  no devices responded — check SDA/SCL wiring, I2C enabled?")
        else:
            print(f"  responding: {', '.join(hex(a) for a in found)}")
            if addr in found:
                print(f"  ✓ FT6336U expected at {hex(addr)} — present")
            else:
                print(f"  ✗ FT6336U expected at {hex(addr)} — NOT present")
                return

    print()
    print("=== Live touch monitor (10s) — tap each corner ===")
    with TouchReader(cfg=cfg) as t:
        deadline = time.time() + 10.0
        last_print = (None, None)
        while time.time() < deadline:
            pt = t.read_touch()
            if pt is not None and pt != last_print:
                print(f"  tap at x={pt[0]:>3} y={pt[1]:>3}")
                last_print = pt
            time.sleep(0.05)
    print()
    print("If corner-tap coordinates don't match the panel corners (0,0 → "
          "479,319), flip TOUCH_SWAP_XY / TOUCH_INVERT_X / TOUCH_INVERT_Y "
          "in .env and re-run.")


if __name__ == "__main__":
    run_scan()
