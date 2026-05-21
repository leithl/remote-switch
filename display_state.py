"""
display_state.py — Assembles the state dict consumed by display.render().

Reads from the project's existing data sources — no new caches:
    - /run/heater.db          → latest hangar temp (temp_c)
    - GPIO 17 sysfs           → live heater state
    - /var/lib/heater/heater.db.schedules → next heater event
    - /run/heater-hvac.json   → HVAC reported state (read directly, never
                                 triggers a dongle round-trip from this path)
    - /run/heater-flashair.json → multi-stage FlashAir status (written by
                                   flashair-sync; produced by phase 1 PRs +
                                   the follow-up contract extension)

Each source is independent — if any one is missing, only that field of the
state dict goes to None and render() degrades gracefully.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from time import time

import config
import flashair
import hvac


# ---------------------------------------------------------------------------
# Individual source readers — each catches its own errors and returns None
# ---------------------------------------------------------------------------

def _latest_temp_c():
    """Most recent hangar temp from the RAM DB, or None."""
    try:
        conn = config.get_ram_db()
        row = conn.execute(
            "SELECT temp_c FROM readings ORDER BY epoch DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            return float(row[0])
    except (sqlite3.Error, OSError):
        pass
    return None


def _heater_on():
    """True iff the heater GPIO is driven high right now."""
    try:
        return config.read_gpio() == "1"
    except OSError:
        return False


def _fmt_schedule_time(epoch):
    """7:30 AM / 12:05 PM — strip the leading zero portably (works on Linux + macOS)."""
    return datetime.fromtimestamp(epoch).strftime("%I:%M %p").lstrip("0")


def _next_heater_event(now):
    """
    Next upcoming heater schedule, or None.

    Returns shape: {action: "ON"|"OFF", epoch: int, label: str}
    Matches what display.render() expects in heater.next_event.
    """
    try:
        conn = config.get_db()
        row = conn.execute(
            "SELECT execute_epoch, action FROM schedules "
            "WHERE device = 'heater' AND execute_epoch > ? "
            "ORDER BY execute_epoch LIMIT 1",
            (now,),
        ).fetchone()
        conn.close()
    except (sqlite3.Error, OSError):
        return None
    if not row:
        return None
    epoch, action = int(row[0]), str(row[1])
    label_action = "ON" if action == "1" else "OFF"
    return {
        "action": label_action,
        "epoch": epoch,
        "label": f"{label_action} at {_fmt_schedule_time(epoch)}",
    }


def _hvac_summary():
    """
    Read the HVAC cache file directly (never triggers a dongle fetch).

    log_temp.py is the canonical place where HVAC gets refreshed (its per-minute
    cron job calls hvac.state_for_log() → hvac.get_state() which writes the
    cache). The display loop only reads the cache to avoid 5-second polling of
    the dongle.
    """
    try:
        data = json.loads(hvac.HVAC_CACHE.read_text())
    except (OSError, ValueError):
        return None
    reported = data.get("reported")
    if not reported:
        return None
    return {
        "mode":     reported.get("mode", "off"),
        "target_f": reported.get("target_f"),
        "power_w":  int(reported.get("power_w") or 0),
    }


def _flashair(now):
    """
    Read /run/heater-flashair.json, return the state dict the renderer wants.

    None if the file doesn't exist — render() shows "not configured" in that case.

    Shares the file path + staleness threshold with flashair.py (the web-UI
    consumer), so both views stay in lock-step if either constant changes.

    Handles two contract generations:
      v0 (current main):  {epoch, last_sync_epoch, last_sync_files_n,
                           transferring, current_file}
      v1 (follow-up PR):  adds stage, current_ssid, files_done, files_total

    On v0 data, stage is inferred from `transferring`. On v1 data, everything
    comes through as-is. Both render correctly.

    `stale=True` is set here (not in the on-disk JSON) when epoch is too old —
    that's the signal that flashair-sync itself stopped writing the file.
    """
    data = flashair.read_cache()
    if data is None:
        return None

    epoch = int(data.get("epoch", 0))
    if epoch == 0 or now - epoch > flashair.STALE_AFTER_SECS:
        return {"epoch": epoch, "stale": True}

    out = {
        "epoch":              epoch,
        "last_sync_epoch":    data.get("last_sync_epoch"),
        "last_sync_files_n":  int(data.get("last_sync_files_n", 0)),
        "current_file":       data.get("current_file"),
        "stale":              False,
    }

    if "stage" in data:
        # v1 contract — full multi-stage info
        out["stage"]         = data["stage"]
        out["current_ssid"]  = data.get("current_ssid")   # may be None → "no wifi"
        out["files_done"]    = int(data.get("files_done", 0))
        out["files_total"]   = int(data.get("files_total", 0))
    else:
        # v0 contract — collapse the boolean into the stage enum.
        # current_ssid is intentionally omitted: v0 doesn't carry it, and we
        # want the renderer to suppress the SSID indicator (not show "no wifi"
        # red, which would imply wifi is actually down).
        out["stage"]         = "downloading" if data.get("transferring") else "idle"
        out["files_done"]    = 0
        out["files_total"]   = 0

    return out


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _c_to_f(c):
    return round(c * 9 / 5 + 32, 1) if c is not None else None


def get_state():
    """Build the full state dict for display.render()."""
    now = int(time())
    return {
        "now_epoch": now,
        "heater": {
            "on":         _heater_on(),
            "next_event": _next_heater_event(now),
        },
        "flashair": _flashair(now),
        "hvac":     _hvac_summary(),
        "hangar_f": _c_to_f(_latest_temp_c()),
    }


# ---------------------------------------------------------------------------
# CLI — dump the live state to stdout. Handy for ssh-debugging on the Pi.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(json.dumps(get_state(), indent=2, default=str))
