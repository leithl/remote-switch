"""
presence.py — DFRobot C4001 (SEN0610) 24GHz mmWave presence/motion sensor.

Wakes the dashboard backlight when someone's in the hangar. Lives on the SAME
I2C bus as the FT6336U touch chip — different address (0x2A vs touch's 0x38),
so it adds no GPIO. Read about once per second from display_loop's poll cycle.

Mirrors touch.py: the vendor lib is imported lazily so this module loads fine
on macOS dev / a Pi without the sensor wired, and every read failure degrades
to "unavailable" rather than raising. display_loop treats an unavailable sensor
as "keep the backlight on" — a dead sensor must never leave the hangar dark.

Requires the DFRobot_C4001 Python library on the Pi:
    git clone https://github.com/DFRobot/DFRobot_C4001
    # then put its python/raspberrypi dir on PYTHONPATH, or copy
    # DFRobot_C4001.py next to this file.

Sensor variant: SEN0610 = the 12 m "motion + ranging" C4001.
    PRESENCE_MODE=motion   (default) → SPEED_MODE: moving targets to ~12 m,
                                       covers the whole hangar. A motionless
                                       person reads as "no target" — the
                                       rolling idle timeout bridges that.
    PRESENCE_MODE=presence            → EXIST_MODE: still+moving presence,
                                       but only to ~8 m.
"""

import config

I2C_BUS_DEFAULT  = 1
I2C_ADDR_DEFAULT = 0x2A      # C4001 default (0x2A/0x2B); != FT6336U touch (0x38)
MODE_DEFAULT     = "motion"  # SPEED_MODE (12 m). "presence" -> EXIST_MODE (8 m).

# After this many consecutive read failures, declare the sensor dead so
# display_loop falls back to backlight-always-on.
_MAX_CONSEC_ERRORS = 10


class PresenceSensor:
    """Thin wrapper over DFRobot_C4001_I2C. `.detected()` is the only signal
    display_loop needs; `.read_raw()` backs the --presence-test dump."""

    def __init__(self, cfg=None):
        cfg = cfg if cfg is not None else config.load_env()
        # int(..., 0) accepts "0x2a" and "42" alike.
        self.bus  = int(str(cfg.get("PRESENCE_I2C_BUS", I2C_BUS_DEFAULT)), 0)
        self.addr = int(str(cfg.get("PRESENCE_I2C_ADDR", I2C_ADDR_DEFAULT)), 0)
        self.mode = (cfg.get("PRESENCE_MODE") or MODE_DEFAULT).strip().lower()
        self._dev = None
        self.available = False
        self._errors = 0
        self._open()

    def _open(self):
        try:
            from DFRobot_C4001 import (
                DFRobot_C4001_I2C, SPEED_MODE, EXIST_MODE,
            )
        except Exception as e:  # lib not installed → feature off, screen stays on
            print(f"presence: DFRobot_C4001 lib unavailable ({e}); "
                  f"presence wake disabled, backlight stays on")
            return
        try:
            dev = DFRobot_C4001_I2C(self.bus, self.addr)
            if not dev.begin():
                print(f"presence: C4001 not responding (bus {self.bus} "
                      f"addr {hex(self.addr)})")
                return
            dev.set_sensor_mode(EXIST_MODE if self.mode == "presence"
                                else SPEED_MODE)
            self._dev = dev
            self.available = True
            print(f"presence: C4001 ready (mode={self.mode}, "
                  f"addr={hex(self.addr)})")
        except Exception as e:
            print(f"presence: C4001 init failed: {e}")

    def detected(self):
        """True/False if a target is present, or None if the sensor is
        unavailable (caller should fail-safe to backlight-on)."""
        if not self.available:
            return None
        try:
            det = bool(self._dev.motion_detection())
            self._errors = 0
            return det
        except Exception as e:
            self._errors += 1
            if self._errors >= _MAX_CONSEC_ERRORS:
                self.available = False
                print(f"presence: C4001 read failed {self._errors}x ({e}); "
                      f"disabling, backlight stays on")
            return None

    def read_raw(self):
        """Diagnostic snapshot for --presence-test."""
        if not self.available:
            return {"available": False}
        out = {"available": True}
        for key, fn in (
            ("detected", lambda: bool(self._dev.motion_detection())),
            ("targets",  self._dev.get_target_number),
            ("range",    self._dev.get_target_range),
            ("speed",    self._dev.get_target_speed),
        ):
            try:
                out[key] = fn()
            except Exception as e:
                out[key] = f"err: {e}"
        return out
