#!/usr/bin/env python3
"""
log_temp.py — Cron script replacing log_temp.sh.

Usage:
    log_temp.py              # Normal: log one reading (every minute)
    log_temp.py flush        # Persist RAM db → disk db, clear RAM
    log_temp.py rollup       # Pre-compute previous month's stats and cache them
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# Ensure local modules are importable when invoked from cron with a different working directory
sys.path.insert(0, str(Path(__file__).parent))

import aggregate
import config
import hvac


def _month_bounds(d):
    """Return (start_epoch, end_epoch, label) for the month containing date d."""
    start = datetime(d.year, d.month, 1)
    end_date = config.subtract_months(d, -1)  # add 1 month
    end = datetime(end_date.year, end_date.month, 1)
    label = start.strftime("%b %Y")
    return int(start.timestamp()), int(end.timestamp()), label


# ---------------------------------------------------------------------------
# Flush mode
# ---------------------------------------------------------------------------

def do_flush():
    conn = config.get_db()
    if not config._has_ram(conn):
        return  # Nothing to flush
    conn.execute(
        "INSERT OR IGNORE INTO readings SELECT * FROM ram.readings"
    )
    conn.execute("DELETE FROM ram.readings")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Rollup mode
# ---------------------------------------------------------------------------

def do_rollup():
    env = config.load_env()
    notify_email = env.get("NOTIFY_EMAIL", "").strip()

    today = date.today()
    prev_month_date = config.subtract_months(today, 1)
    start_epoch, end_epoch, label = _month_bounds(prev_month_date)
    month_key = prev_month_date.strftime("%Y-%m")

    conn = config.get_db()
    rows = config.query_readings(conn, start_epoch, end_epoch)

    result = aggregate.compute(rows, start_epoch, end_epoch)

    # Serialize to JSON and store in monthly_cache
    cache_data = json.dumps(result)
    conn.execute(
        "INSERT OR REPLACE INTO monthly_cache (month, data) VALUES (?, ?)",
        (month_key, cache_data)
    )
    conn.commit()
    conn.close()

    # Send email summary if configured
    if notify_email and shutil.which("msmtp"):
        ts = result.get("temp_stats")
        rs = result.get("runtime_stats")

        temp_line = "No data"
        cold_line = ""
        runtime_line = "No data"
        coverage_line = "No data"

        if ts:
            temp_line = (
                f"Avg: {ts['avg_f']:.1f}\u00b0F / {ts['avg_c']:.1f}\u00b0C, "
                f"Min: {ts['min_f']:.1f}\u00b0F / {ts['min_c']:.1f}\u00b0C, "
                f"Max: {ts['max_f']:.1f}\u00b0F / {ts['max_c']:.1f}\u00b0C"
            )
            cold_line = f"Cold: {ts['cold_hrs']:.1f} hours at or below 48\u00b0F"
        if rs:
            runtime_line = (
                f"Heater: {rs['on_hrs']:.1f} hours total, "
                f"{rs['avg_hrs_day']:.1f} hours/day"
            )
            if "fan_on_hrs" in rs:
                runtime_line += f"; Fan: {rs['fan_on_hrs']:.1f} hours"
            coverage_line = (
                f"Temp: {rs['temp_coverage_pct']:.1f}%, "
                f"Heater: {rs['heater_coverage_pct']:.1f}%"
            )

        body = (
            f"Subject: Heater Monthly Summary - {label}\r\n"
            f"To: {notify_email}\r\n"
            f"\r\n"
            f"Heater Monthly Summary: {label}\r\n"
            f"{'=' * 40}\r\n"
            f"\r\n"
            f"Temperature:\r\n"
            f"  {temp_line}\r\n"
        )
        if cold_line:
            body += f"  {cold_line}\r\n"
        body += (
            f"\r\n"
            f"Heater Runtime:\r\n"
            f"  {runtime_line}\r\n"
            f"\r\n"
            f"Data Coverage:\r\n"
            f"  {coverage_line}\r\n"
        )

        try:
            subprocess.run(
                ["msmtp", notify_email],
                input=body,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Normal mode (log one reading)
# ---------------------------------------------------------------------------

def do_log():
    now_epoch = int(datetime.now().timestamp())

    # Get location (geocodes + writes .env on first run if LOCATION is set)
    lat, lon, _ = config.get_location()

    # Read sensors
    temp_c = config.read_temp()
    ambient_c = config.fetch_ambient(lat, lon)

    # Read current GPIO state
    heater_state = int(config.read_gpio())

    # Execute due schedules (with flock to prevent concurrent runs)
    lock_path = "/tmp/heater-schedule.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)

        conn = config.get_db()
        due = conn.execute(
            "SELECT created_epoch, action, COALESCE(device, 'heater'), COALESCE(params, '') "
            "FROM schedules WHERE execute_epoch <= ?",
            (now_epoch,)
        ).fetchall()

        for created_epoch, action, device, params in due:
            if device == "heater":
                # Engine-block heater: action is "0" or "1"
                if action in ("0", "1"):
                    try:
                        config.write_gpio(action)
                        heater_state = int(action)
                    except (PermissionError, OSError):
                        pass
            elif device == "hvac":
                # Hangar HVAC: params is JSON {power, mode, target_f, fan_speed}
                try:
                    p = json.loads(params) if params else {}
                    hvac.set_state(
                        power=p.get("power"),
                        mode=p.get("mode"),
                        target_f=p.get("target_f"),
                        fan_speed=p.get("fan_speed"),
                    )
                except Exception:
                    pass

            conn.execute(
                "DELETE FROM schedules WHERE created_epoch = ?",
                (created_epoch,)
            )

        conn.commit()
        conn.close()
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

    # Fan auto-logic
    fan_state = int(config.read_fan_gpio())
    fan_mode = config.read_fan_mode()

    if fan_mode == "auto":
        if temp_c is not None and ambient_c is not None:
            should_on = (
                temp_c >= config.FAN_TEMP_THRESHOLD_C and
                (temp_c - ambient_c) >= config.FAN_MARGIN_C
            )
        else:
            should_on = False
        desired = "1" if should_on else "0"
        if desired != str(fan_state):
            try:
                config.write_fan_gpio(desired)
                fan_state = int(desired)
            except (PermissionError, OSError):
                pass
    elif fan_mode == "on":
        if fan_state != 1:
            try:
                config.write_fan_gpio("1")
                fan_state = 1
            except (PermissionError, OSError):
                pass
    elif fan_mode == "off":
        if fan_state != 0:
            try:
                config.write_fan_gpio("0")
                fan_state = 0
            except (PermissionError, OSError):
                pass

    # Read HVAC mode (None if not configured / dongle unreachable)
    ac_state = hvac.state_for_log()

    # Write reading to RAM db
    ram_conn = config.get_ram_db()
    ram_conn.execute(
        "INSERT OR REPLACE INTO readings (epoch, temp_c, heater_state, ambient_c, fan_state, ac_state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now_epoch, temp_c, heater_state, ambient_c, fan_state, ac_state)
    )
    ram_conn.commit()
    ram_conn.close()

    # Ensure www-data can read the RAM and disk dbs
    for db_path in (config.RAM_DB, config.DISK_DB):
        try:
            os.chmod(str(db_path), 0o664)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "flush":
        do_flush()
    elif mode == "rollup":
        do_rollup()
    else:
        do_log()
