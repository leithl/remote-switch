"""
hvac.py — Hangar HVAC (Durastar DRAW33F2A mini-split) control via Midea WiFi dongle.

Distinct from the engine-block heater (config.GPIO_PIN) and the exhaust fan
(config.FAN_GPIO_PIN). This module talks to the Durastar's Midea-OEM WiFi
dongle on the LAN over TCP/6444 using msmart-ng.

If HVAC_DONGLE_IP / HVAC_DEVICE_ID / HVAC_TOKEN / HVAC_KEY are not all set in
.env, is_configured() returns False and every function is a safe no-op. The
UI hides the HVAC card in that state.

State is cached in /run/heater-hvac.json with a 30s TTL so page loads don't
hammer the dongle. The cache stores both:
    reported   — what the dongle says the unit is doing right now
    commanded  — the most recent set_state() we sent
The UI shows reported by default; commanded is surfaced only when they differ
(catches drift from someone using the physical remote).
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import config

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
HVAC_CACHE     = Path("/run/heater-hvac.json")
CACHE_TTL_SECS = 30
LAN_PORT       = 6444

# Stable, lowercase mode/fan tokens — used in .env, schedules.params JSON,
# and query strings. Keep these short and human-readable.
MODE_AUTO   = "auto"
MODE_COOL   = "cool"
MODE_DRY    = "dry"
MODE_HEAT   = "heat"
MODE_FAN    = "fan"
MODE_FREEZE = "freeze"  # maps to dev.freeze_protection (Midea's real "FP" flag)

ALL_MODES = [MODE_AUTO, MODE_COOL, MODE_DRY, MODE_HEAT, MODE_FAN, MODE_FREEZE]

MODE_LABELS = {
    MODE_AUTO:   "Auto",
    MODE_COOL:   "Cool",
    MODE_DRY:    "Dry",
    MODE_HEAT:   "Heat",
    MODE_FAN:    "Fan only",
    MODE_FREEZE: "Freeze Prevention",
}

FAN_AUTO = "auto"
FAN_LOW  = "low"
FAN_MED  = "med"
FAN_HIGH = "high"
ALL_FANS = [FAN_AUTO, FAN_LOW, FAN_MED, FAN_HIGH]
FAN_LABELS = {FAN_AUTO: "Auto", FAN_LOW: "Low", FAN_MED: "Medium", FAN_HIGH: "High"}

# UI temp range — user inputs Fahrenheit. Conversions to °C happen at the boundary.
TEMP_MIN_F = 60
TEMP_MAX_F = 86

# Integer codes for the readings.ac_state column. log_temp.py logs these every
# minute; aggregate.py renders them as a chart band like heater_state / fan_state.
AC_STATE_OFF  = 0
AC_STATE_HEAT = 1
AC_STATE_COOL = 2
AC_STATE_FAN  = 3
AC_STATE_DRY  = 4
AC_STATE_AUTO = 5


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def is_configured():
    """True iff all four .env keys are set. Cheap — safe to call per request."""
    e = config.load_env()
    return all(e.get(k, "").strip() for k in (
        "HVAC_DONGLE_IP", "HVAC_DEVICE_ID", "HVAC_TOKEN", "HVAC_KEY"
    ))


# ---------------------------------------------------------------------------
# msmart-ng adapter — lazy import so the rest of the app boots without it
# ---------------------------------------------------------------------------

def _msmart_modes():
    from msmart.device import AirConditioner as AC
    return {
        MODE_AUTO: AC.OperationalMode.AUTO,
        MODE_COOL: AC.OperationalMode.COOL,
        MODE_DRY:  AC.OperationalMode.DRY,
        MODE_HEAT: AC.OperationalMode.HEAT,
        MODE_FAN:  AC.OperationalMode.FAN_ONLY,
    }


def _msmart_fans():
    from msmart.device import AirConditioner as AC
    return {
        FAN_AUTO: AC.FanSpeed.AUTO,
        FAN_LOW:  AC.FanSpeed.LOW,
        FAN_MED:  AC.FanSpeed.MEDIUM,
        FAN_HIGH: AC.FanSpeed.HIGH,
    }


async def _open_device():
    """Construct + authenticate an AirConditioner instance against the dongle.

    msmart-ng 2025.12.0 makes authenticate() async (it performs a handshake
    with the dongle), so this whole helper is async and lives inside the
    async closures in get_state / set_state.
    """
    from msmart.device import AirConditioner as AC
    e = config.load_env()
    dev = AC(
        ip=e["HVAC_DONGLE_IP"].strip(),
        device_id=int(e["HVAC_DEVICE_ID"].strip()),
        port=LAN_PORT,
    )
    await dev.authenticate(
        e["HVAC_TOKEN"].strip(),
        e["HVAC_KEY"].strip(),
    )
    # Local toggle (no SetState sent to the unit). Adds a GetEnergyUsageCommand
    # to each refresh() so power_w / total_kwh get populated on this Durastar.
    # Idle draw is ~60W; compressor cycles push it well past 100W. Lets us
    # distinguish "powered on, idle in FP" from "actively heating".
    dev.enable_energy_usage_requests = True
    return dev


def _run_async(coro):
    """asyncio.run() wrapper — keeps mod_wsgi sync handler path intact."""
    return asyncio.run(coro)


def _device_to_dict(dev):
    """Translate an msmart-ng AirConditioner's live attributes to a plain dict.

    When the unit's real Freeze Protection flag (`dev.freeze_protection`, exposed
    over the LAN protocol since msmart-ng 2023.x) is set, surface MODE_FREEZE as
    the effective mode regardless of the underlying operational_mode the firmware
    reports — the unit displays "FP" in this state and the UI should match.
    """
    inv_modes = {v: k for k, v in _msmart_modes().items()}
    inv_fans  = {v: k for k, v in _msmart_fans().items()}

    target_c = float(dev.target_temperature) if dev.target_temperature is not None else None
    indoor_c = float(dev.indoor_temperature) if dev.indoor_temperature is not None else None
    freeze_on = bool(getattr(dev, "freeze_protection", False))

    op_mode = inv_modes.get(dev.operational_mode, str(dev.operational_mode))
    effective_mode = MODE_FREEZE if freeze_on else op_mode

    # Energy fields — None on dongles that don't reply to GetEnergyUsageCommand.
    power_w = None
    total_kwh = None
    try:
        power_w = dev.get_real_time_power_usage()
        total_kwh = dev.get_total_energy_usage()
    except Exception:
        pass

    return {
        "power":             bool(dev.power_state),
        "mode":              effective_mode,
        "target_c":          target_c,
        "target_f":          round(target_c * 9 / 5 + 32, 1) if target_c is not None else None,
        "fan_speed":         inv_fans.get(dev.fan_speed, str(dev.fan_speed)),
        "indoor_c":          indoor_c,
        "indoor_f":          round(indoor_c * 9 / 5 + 32, 1) if indoor_c is not None else None,
        "freeze_protection": freeze_on,
        "power_w":           round(float(power_w), 1) if power_w is not None else None,
        "total_kwh":         round(float(total_kwh), 2) if total_kwh is not None else None,
    }


# ---------------------------------------------------------------------------
# Cache (single JSON file in /run, holds reported + commanded views)
# ---------------------------------------------------------------------------

def _read_cache():
    try:
        return json.loads(HVAC_CACHE.read_text())
    except (OSError, ValueError):
        return {"reported": None, "commanded": None}


def _write_cache(cache):
    try:
        HVAC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        HVAC_CACHE.write_text(json.dumps(cache))
        os.chmod(str(HVAC_CACHE), 0o664)
    except OSError:
        pass


def _now_epoch():
    return int(datetime.now().timestamp())


def _save_reported(state):
    cache = _read_cache()
    state = dict(state)
    state["epoch"] = _now_epoch()
    cache["reported"] = state
    _write_cache(cache)


def _save_commanded(state):
    cache = _read_cache()
    state = dict(state)
    state["epoch"] = _now_epoch()
    cache["commanded"] = state
    _write_cache(cache)


# ---------------------------------------------------------------------------
# Public API — sync, safe to call from WSGI / cron
# ---------------------------------------------------------------------------

def get_state(force=False):
    """
    Return current HVAC state as a dict, or None if not configured.

    Shape:
        {
          "reported":  {power, mode, target_c, target_f, fan_speed,
                        indoor_c, indoor_f, epoch} | None,
          "commanded": {power, mode, target_c, target_f, fan_speed, epoch} | None,
          "stale":     bool,        # True if reported came from cache after a fetch failure
          "age_secs":  int,         # how old the reported value is
          "diverged":  bool,        # True if commanded differs from reported in any meaningful field
        }

    If force=False and the cached reported value is < CACHE_TTL_SECS old,
    skip the network round-trip. On dongle error, return whatever's cached
    with stale=True; never raise.
    """
    if not is_configured():
        return None

    cache    = _read_cache()
    reported = cache.get("reported")
    age      = (_now_epoch() - reported["epoch"]) if reported and "epoch" in reported else None

    if not force and reported is not None and age is not None and age < CACHE_TTL_SECS:
        return _decorate(cache, age_secs=age, stale=False)

    try:
        async def _fetch():
            dev = await _open_device()
            await dev.refresh()
            return _device_to_dict(dev)

        fresh = _run_async(_fetch())
        _save_reported(fresh)
        cache = _read_cache()
        return _decorate(cache, age_secs=0, stale=False)
    except Exception:
        if reported is not None:
            return _decorate(cache, age_secs=(age or 0), stale=True)
        return None


def set_state(power=None, mode=None, target_f=None, fan_speed=None):
    """
    Apply a partial state change. Any None argument keeps the dongle's current value.

    mode='freeze' enables Midea's real Freeze Protection flag — the unit's display
    shows "FP" and the firmware holds a minimum heat output (~8°C/46°F). When freeze
    mode is set, target/fan/operational_mode are ignored: the unit drives them
    internally. Setting any non-freeze mode automatically clears the freeze flag.

    Returns True on success, False on error. On success, both reported and
    commanded views are persisted so the UI can show drift.
    """
    if not is_configured():
        return False

    msmart_modes = None
    msmart_fans  = None
    try:
        msmart_modes = _msmart_modes()
        msmart_fans  = _msmart_fans()
    except Exception:
        return False

    # Normalise inputs. MODE_FREEZE is special — it maps to the real freeze_protection
    # flag, not to operational_mode. Any other explicit mode set forces freeze off.
    set_freeze = None  # tri-state: True (turn on), False (turn off), None (don't touch)
    if mode == MODE_FREEZE:
        set_freeze = True
        # The unit handles target/fan/mode internally while in FP. Force power on,
        # clear the rest so we don't fight the firmware.
        power = True
        mode = None
        target_f = None
        fan_speed = None
    elif mode is not None:
        # User explicitly chose a non-freeze mode → exit FP if it was active.
        set_freeze = False

    requested = {
        "power":     bool(power) if power is not None else None,
        "mode":      mode if (mode is not None and mode in msmart_modes) else None,
        "target_f":  float(target_f) if target_f is not None else None,
        "fan_speed": fan_speed if (fan_speed is not None and fan_speed in msmart_fans) else None,
        "freeze":    set_freeze,
    }

    try:
        async def _apply():
            dev = await _open_device()
            await dev.refresh()

            if requested["power"] is not None:
                dev.power_state = requested["power"]
            if requested["mode"] is not None:
                dev.operational_mode = msmart_modes[requested["mode"]]
            if requested["target_f"] is not None:
                dev.target_temperature = _f_to_c(requested["target_f"])
            if requested["fan_speed"] is not None:
                dev.fan_speed = msmart_fans[requested["fan_speed"]]
            if requested["freeze"] is not None:
                # NB: ignore dev.supports_freeze_protection — capability flags lie on
                # some units (msmart-ng issue #76); the SetState bit is what counts.
                dev.freeze_protection = requested["freeze"]

            await dev.apply()
            return _device_to_dict(dev)

        post = _run_async(_apply())
        _save_reported(post)

        # Build commanded view by overlaying requested fields on reported result.
        # When we requested freeze=True, force commanded mode = MODE_FREEZE so the
        # divergence detector compares apples to apples on the next refresh.
        commanded_mode = post["mode"]
        if requested["freeze"] is True:
            commanded_mode = MODE_FREEZE
        elif requested["mode"] is not None:
            commanded_mode = requested["mode"]

        commanded = {
            "power":     post["power"]     if requested["power"]     is None else requested["power"],
            "mode":      commanded_mode,
            "target_c":  post["target_c"]  if requested["target_f"]  is None else _f_to_c(requested["target_f"]),
            "target_f":  post["target_f"]  if requested["target_f"]  is None else round(requested["target_f"], 1),
            "fan_speed": post["fan_speed"] if requested["fan_speed"] is None else requested["fan_speed"],
        }
        _save_commanded(commanded)
        return True
    except Exception:
        return False


def state_for_log():
    """
    Return a dict for log_temp.py to write into the per-minute row, or None
    if HVAC is not configured / unreachable (so all four columns stay NULL
    rather than mis-recorded as 0/off).

    Shape:
        {"ac_state": int, "power_w": float | None,
         "total_kwh": float | None, "indoor_f": float | None}

    `ac_state` is the legacy mode→int mapping; Freeze Prevention is heat work,
    so it's logged as AC_STATE_HEAT. `power_w` is the cleaner "actually running"
    signal — readings above ~100W indicate the compressor (or substantial fan)
    is drawing power. `indoor_f` is the unit's own ceiling-mounted thermistor
    (the FP regulator's input) and complements the floor-level DS18B20 in
    `temp_c`; the gap between them is the vertical-stratification signal.
    """
    if not is_configured():
        return None
    s = get_state()
    if s is None or s.get("reported") is None:
        return None
    r = s["reported"]
    if not r.get("power"):
        ac_state = AC_STATE_OFF
    else:
        ac_state = {
            MODE_HEAT:   AC_STATE_HEAT,
            MODE_FREEZE: AC_STATE_HEAT,
            MODE_COOL:   AC_STATE_COOL,
            MODE_FAN:    AC_STATE_FAN,
            MODE_DRY:    AC_STATE_DRY,
            MODE_AUTO:   AC_STATE_AUTO,
        }.get(r.get("mode"), AC_STATE_OFF)
    return {
        "ac_state":  ac_state,
        "power_w":   r.get("power_w"),
        "total_kwh": r.get("total_kwh"),
        "indoor_f":  r.get("indoor_f"),
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _decorate(cache, age_secs, stale):
    """Add UI-facing flags (stale, age_secs, diverged) to the cache view."""
    out = {
        "reported":  cache.get("reported"),
        "commanded": cache.get("commanded"),
        "stale":     stale,
        "age_secs":  age_secs,
        "diverged":  _diverged(cache.get("reported"), cache.get("commanded")),
    }
    return out


def _diverged(reported, commanded):
    """True iff commanded was set and differs meaningfully from reported."""
    if not reported or not commanded:
        return False
    fields = ("power", "mode", "fan_speed")
    for f in fields:
        if reported.get(f) != commanded.get(f):
            return True
    # Compare temps with a 0.5°C tolerance (dongle sometimes rounds half-degrees)
    rt = reported.get("target_c")
    ct = commanded.get("target_c")
    if rt is not None and ct is not None and abs(rt - ct) > 0.5:
        return True
    return False


def _f_to_c(f):
    return round((float(f) - 32) * 5 / 9, 1)
