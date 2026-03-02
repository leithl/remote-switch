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
                f"Total: {rs['on_hrs']:.1f} hours, "
                f"Avg: {rs['avg_hrs_day']:.1f} hours/day"
            )
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
            "SELECT created_epoch, action FROM schedules WHERE execute_epoch <= ?",
            (now_epoch,)
        ).fetchall()

        for created_epoch, action in due:
            if action in ("0", "1"):
                try:
                    config.write_gpio(action)
                    heater_state = int(action)
                except (PermissionError, OSError):
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

    # Write reading to RAM db
    ram_conn = config.get_ram_db()
    ram_conn.execute(
        "INSERT OR REPLACE INTO readings (epoch, temp_c, heater_state, ambient_c) "
        "VALUES (?, ?, ?, ?)",
        (now_epoch, temp_c, heater_state, ambient_c)
    )
    ram_conn.commit()
    ram_conn.close()

    # Ensure www-data can read the RAM db
    try:
        os.chmod(str(config.RAM_DB), 0o664)
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
