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

ALL_MODES = [MODE_AUTO, MODE_COOL, MODE_DRY, MODE_HEAT, MODE_FAN]

MODE_LABELS = {
    MODE_AUTO:   "Auto",
    MODE_COOL:   "Cool",
    MODE_DRY:    "Dry",
    MODE_HEAT:   "Heat",
    MODE_FAN:    "Fan only",
}

# Freeze Prevention is intentionally NOT in ALL_MODES. On the Durastar DRAW33F2A
# the IR remote's "down twice from 60°F" sequence engages a hidden internal
# regulator that holds the room at ~46°F — and that regulator lives in a register
# the LAN protocol cannot read or write. The msmart `freeze_protection` SetState
# bit is purely cosmetic on this unit (icon flag only). We therefore never set
# it, and we don't expose FP in the web UI. The dongle still reports the flag in
# `freeze_protection` so the UI can surface "FP icon active" if it wants to —
# but driving it from here would just disturb the IR-set regulator. See
# CLAUDE.md "Why FP is IR-only" for the diagnostic story.

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
    return dev


def _run_async(coro):
    """asyncio.run() wrapper — keeps mod_wsgi sync handler path intact."""
    return asyncio.run(coro)


def _device_to_dict(dev):
    """Translate an msmart-ng AirConditioner's live attributes to a plain dict.

    `freeze_protection` is reported as a separate boolean (the unit's FP icon
    state) but is NOT surfaced as a `mode` value. See the comment at the top of
    this module for why FP is IR-only on this Durastar.
    """
    inv_modes = {v: k for k, v in _msmart_modes().items()}
    inv_fans  = {v: k for k, v in _msmart_fans().items()}

    target_c = float(dev.target_temperature) if dev.target_temperature is not None else None
    indoor_c = float(dev.indoor_temperature) if dev.indoor_temperature is not None else None

    return {
        "power":             bool(dev.power_state),
        "mode":              inv_modes.get(dev.operational_mode, str(dev.operational_mode)),
        "target_c":          target_c,
        "target_f":          round(target_c * 9 / 5 + 32, 1) if target_c is not None else None,
        "fan_speed":         inv_fans.get(dev.fan_speed, str(dev.fan_speed)),
        "indoor_c":          indoor_c,
        "indoor_f":          round(indoor_c * 9 / 5 + 32, 1) if indoor_c is not None else None,
        "freeze_protection": bool(getattr(dev, "freeze_protection", False)),
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

    Does NOT control Freeze Prevention. The msmart `freeze_protection` flag is
    purely cosmetic on this Durastar — the real ~46°F regulator is engaged only
    by the IR remote (down-twice from 60°F), and any LAN apply() risks disturbing
    the hidden regulator state. So during winter, while the unit is in IR-FP,
    avoid calling this function — it will likely cancel the FP regulator.

    Unrecognized `mode` values (including the legacy 'freeze' token from old
    schedules) are ignored — they fall through the `mode in msmart_modes` filter
    and don't change the unit's operational_mode.

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

    requested = {
        "power":     bool(power) if power is not None else None,
        "mode":      mode if (mode is not None and mode in msmart_modes) else None,
        "target_f":  float(target_f) if target_f is not None else None,
        "fan_speed": fan_speed if (fan_speed is not None and fan_speed in msmart_fans) else None,
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

            await dev.apply()
            return _device_to_dict(dev)

        post = _run_async(_apply())
        _save_reported(post)

        commanded = {
            "power":     post["power"]     if requested["power"]     is None else requested["power"],
            "mode":      post["mode"]      if requested["mode"]      is None else requested["mode"],
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
    Return integer for readings.ac_state, suitable for log_temp.py to write
    into the per-minute row. Returns None if HVAC not configured or unreachable
    (so the column stays NULL rather than mis-recorded as 0/off).
    """
    if not is_configured():
        return None
    s = get_state()
    if s is None or s.get("reported") is None:
        return None
    r = s["reported"]
    if not r.get("power"):
        return AC_STATE_OFF
    return {
        MODE_HEAT:   AC_STATE_HEAT,
        MODE_COOL:   AC_STATE_COOL,
        MODE_FAN:    AC_STATE_FAN,
        MODE_DRY:    AC_STATE_DRY,
        MODE_AUTO:   AC_STATE_AUTO,
    }.get(r.get("mode"), AC_STATE_OFF)


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
