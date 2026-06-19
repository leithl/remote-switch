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


def _opt_int(cfg, key):
    """Parse an optional integer .env knob. Returns None when unset/blank so a
    caller can tell "leave the sensor's flash config alone" from a real 0."""
    raw = cfg.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip(), 0)
    except ValueError:
        print(f"presence: {key}={raw!r} is not an integer; ignoring")
        return None


def _opt_bool(cfg, key):
    """Parse an optional boolean .env knob (1/0, on/off, true/false, yes/no).
    Returns None when unset so the sensor's existing setting is left untouched."""
    raw = cfg.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    val = str(raw).strip().lower()
    if val in ("1", "on", "true", "yes"):
        return True
    if val in ("0", "off", "false", "no"):
        return False
    print(f"presence: {key}={raw!r} is not a boolean; ignoring")
    return None


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
        self._cfg = cfg  # retained so _open() can read the optional tuning knobs
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
            applied = self._apply_tuning(dev)
            self._dev = dev
            self.available = True
            tuned = f", tuning: {', '.join(applied)}" if applied else ""
            print(f"presence: C4001 ready (mode={self.mode}, "
                  f"addr={hex(self.addr)}){tuned}")
        except Exception as e:
            print(f"presence: C4001 init failed: {e}")

    def _apply_tuning(self, dev):
        """Apply optional .env tuning knobs after the mode is set. Every knob is
        opt-in: an unset key means its setter is never called, so the sensor
        keeps whatever is already in its flash (default behaviour = unchanged).
        Each setter the C4001 lib runs persists to that flash, so a value set
        once survives reboots/power cycles. Returns short descriptions of what
        was applied, for the ready line and --presence-test.

        Knobs (cm = centimetres; sensitivity 0-9, 9 = most sensitive):
          PRESENCE_RANGE_MIN_CM / _MAX_CM / _TRIG_CM -> set_detection_range
              raise MIN to ignore near-field clutter; set MAX to real coverage.
          PRESENCE_TRIG_SENS  -> set_trig_sensitivity  (lower = fewer false wakes)
          PRESENCE_KEEP_SENS  -> set_keep_sensitivity  (how readily it holds a target)
          PRESENCE_FRETTING   -> set_fretting_detection (0/off = ignore micro-motion)
        """
        cfg = self._cfg
        applied = []

        rmin = _opt_int(cfg, "PRESENCE_RANGE_MIN_CM")
        rmax = _opt_int(cfg, "PRESENCE_RANGE_MAX_CM")
        rtrig = _opt_int(cfg, "PRESENCE_RANGE_TRIG_CM")
        if rmin is not None or rmax is not None or rtrig is not None:
            # set_detection_range needs all three. Fill any unset arg with a
            # sensible default: full near-field (30 cm), ~12 m reach, trig=max.
            lo = rmin if rmin is not None else 30
            hi = rmax if rmax is not None else 1200
            tg = rtrig if rtrig is not None else hi
            try:
                dev.set_detection_range(lo, hi, tg)
                applied.append(f"range {lo}-{hi}cm (trig {tg})")
            except Exception as e:
                print(f"presence: set_detection_range({lo},{hi},{tg}) failed: {e}")

        ts = _opt_int(cfg, "PRESENCE_TRIG_SENS")
        if ts is not None:
            try:
                dev.set_trig_sensitivity(ts)
                applied.append(f"trig_sens {ts}")
            except Exception as e:
                print(f"presence: set_trig_sensitivity({ts}) failed: {e}")

        ks = _opt_int(cfg, "PRESENCE_KEEP_SENS")
        if ks is not None:
            try:
                dev.set_keep_sensitivity(ks)
                applied.append(f"keep_sens {ks}")
            except Exception as e:
                print(f"presence: set_keep_sensitivity({ks}) failed: {e}")

        fr = _opt_bool(cfg, "PRESENCE_FRETTING")
        if fr is not None:
            try:
                dev.set_fretting_detection(1 if fr else 0)  # FRETTING_ON / OFF
                applied.append(f"fretting {'on' if fr else 'off'}")
            except Exception as e:
                print(f"presence: set_fretting_detection failed: {e}")

        return applied

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
