#!/usr/bin/env python3
"""
log_temp.py — Cron script replacing log_temp.sh.

Usage:
    log_temp.py                 # Normal: log one reading (every minute)
    log_temp.py flush           # Persist RAM db → disk db, clear RAM
    log_temp.py rollup          # Cache previous month's stats + backfill any
                                # missing older months in the 13-month window
    log_temp.py rollup YYYY-MM  # Recompute one month's cache (backfill), no email
"""

import fcntl
import json
import logging
import logging.handlers
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

def do_rollup(month_arg=None):
    """Roll up one month into monthly_cache.

    Default (monthly cron): previous calendar month, with summary email,
    then self-heal — backfill any uncached older month in the stats table's
    13-month window (never overwriting existing entries).
    With an explicit 'YYYY-MM' arg: recompute that month and skip the email —
    used to backfill cache entries after stats additions (e.g. heater_kwh /
    hvac_kwh), since past months are otherwise never recomputed.
    """
    if month_arg:
        prev_month_date = datetime.strptime(month_arg + "-01", "%Y-%m-%d").date()
        # A partial-month snapshot is masked while the month is current (the
        # stats table computes the current month live) but becomes the
        # authoritative past-month view once it rolls over — permanently, if
        # the monthly cron ever misses its tick. Refuse to create one.
        if month_arg >= date.today().strftime("%Y-%m"):
            sys.exit(f"refusing to cache incomplete month {month_arg}")
    else:
        prev_month_date = config.subtract_months(date.today(), 1)

    env = config.load_env()
    notify_email = "" if month_arg else env.get("NOTIFY_EMAIL", "").strip()

    start_epoch, end_epoch, label = _month_bounds(prev_month_date)
    month_key = prev_month_date.strftime("%Y-%m")

    conn = config.get_db()
    rows = config.query_readings(conn, start_epoch, end_epoch)

    result = aggregate.compute(rows, start_epoch, end_epoch)

    # Backfill guard: a typo'd month (or a migrate-era month whose raw rows
    # no longer exist) would REPLACE a good cache entry with an all-None one.
    if month_arg and result["temp_stats"] is None:
        conn.close()
        sys.exit(f"no readings for {month_key}; cache left untouched")

    # Serialize to JSON and store in monthly_cache
    cache_data = json.dumps(result)
    conn.execute(
        "INSERT OR REPLACE INTO monthly_cache (month, data) VALUES (?, ?)",
        (month_key, cache_data)
    )
    conn.commit()

    # Self-heal (cron path only): backfill every older month of the stats
    # table's 13-month window that has no cache entry. A hole in the cache —
    # a missed cron tick, or months predating the rollup feature — pins the
    # batch-scan window in _compute_months_data (switch.py) and silently
    # reintroduces the full-table scan. Empty months are cached too: their
    # all-None stats render as "no data" exactly like an uncached empty
    # month, but their presence keeps the live window at just the current
    # month. INSERT OR IGNORE never overwrites — re-rolling old months can
    # lose fields the current code no longer computes (see CLAUDE.md on
    # pre-2026-05 hvac_on_hrs).
    if not month_arg:
        cached = {
            row[0] for row in conn.execute("SELECT month FROM monthly_cache")
        }
        for i in range(2, 13):  # i=1 (previous month) was just rolled above
            md = config.subtract_months(date.today(), i)
            mk = md.strftime("%Y-%m")
            if mk in cached:
                continue
            m_start, m_end, _ = _month_bounds(md)
            m_result = aggregate.compute(
                config.query_readings(conn, m_start, m_end), m_start, m_end
            )
            conn.execute(
                "INSERT OR IGNORE INTO monthly_cache (month, data) VALUES (?, ?)",
                (mk, json.dumps(m_result)),
            )
            # Commit per month so the write lock is never held across the
            # next iteration's compute (a real-data month after >=2 missed
            # ticks can take tens of seconds on the Pi Zero W, and the
            # per-minute do_log tick writes this DB too).
            conn.commit()
            print(f"backfilled monthly_cache for {mk}")

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
            if "heater_kwh" in rs:
                energy_kwh = rs["heater_kwh"] + rs.get("hvac_kwh", 0)
                runtime_line += f"; Energy: {energy_kwh:.1f} kWh"
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
# Open-Meteo outage alerting
# ---------------------------------------------------------------------------
# A single fetch_ambient() failure is routine (Open-Meteo returns the odd 503,
# and the 15-min ambient cache covers brief blips) and no longer emails --
# config.py logs it to the journal instead. We only page once the fetch has
# failed on AMBIENT_FAIL_ALERT consecutive minutes, which means the cache has
# fully expired AND live fetches keep failing -- i.e. something is actually
# broken (Open-Meteo down, or the Pi has lost outbound internet).
AMBIENT_FAIL_STATE   = Path("/run/heater-ambient-fail")  # tmpfs, a single int
AMBIENT_FAIL_ALERT   = 5     # consecutive failed minutes before the first email
AMBIENT_FAIL_REALERT = 360   # re-send every N further failures (~6h) so a lost
                             # first alert doesn't mean permanent silence


def _read_ambient_fail_count():
    try:
        return int(AMBIENT_FAIL_STATE.read_text().strip())
    except (OSError, ValueError):
        return 0


def _track_ambient_health(failed):
    """Count consecutive ambient-fetch failures and email on a sustained outage.

    `failed` is True only when a location is configured but the fetch returned
    no value -- an actual error, not "no location" and not a cache hit. State
    lives in tmpfs, so an unplanned reboot resets the count; the outage simply
    re-detects on the next run of failed minutes.
    """
    count = _read_ambient_fail_count()

    if not failed:
        # Fetch succeeded (or the cache is still valid, or no location is set).
        # If we had paged, send a one-line all-clear to close out the thread.
        if count >= AMBIENT_FAIL_ALERT:
            _send_alert_email(
                "Open-Meteo ambient fetch recovered",
                f"Ambient temperature fetches are succeeding again after "
                f"{count} consecutive failed minutes.",
            )
        try:
            AMBIENT_FAIL_STATE.unlink()
        except OSError:
            pass
        return

    count += 1
    try:
        AMBIENT_FAIL_STATE.write_text(f"{count}\n")
    except OSError:
        pass

    first  = count == AMBIENT_FAIL_ALERT
    repeat = count > AMBIENT_FAIL_ALERT and \
        (count - AMBIENT_FAIL_ALERT) % AMBIENT_FAIL_REALERT == 0
    if first or repeat:
        _send_alert_email(
            "Open-Meteo ambient fetch failing",
            f"fetch_ambient() has failed on {count} consecutive minutes. "
            f"Open-Meteo may be down, or the Pi has lost outbound internet -- "
            f"ambient temperature is not being logged.\n\n"
            f"Underlying error is in the journal:\n"
            f"  journalctl -t log_temp | grep fetch_ambient",
        )


def _send_alert_email(subject, message):
    """Send a plain-text alert to NOTIFY_EMAIL via msmtp, if both are present."""
    to = config.load_env().get("NOTIFY_EMAIL", "").strip()
    if not to or not shutil.which("msmtp"):
        return
    body = (
        f"Subject: {subject}\r\n"
        f"To: {to}\r\n"
        f"\r\n"
        f"{message}\r\n"
    )
    try:
        subprocess.run(
            ["msmtp", to], input=body, text=True, timeout=30, check=False)
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

    # Alert only on a sustained Open-Meteo outage, not on every 503. A miss
    # counts only when a location is configured (otherwise None is expected).
    _track_ambient_health(failed=bool(lat and lon) and ambient_c is None)

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
                # Hangar HVAC: params is JSON {power, mode, target_f, fan_speed,
                # turbo}. turbo absent (pre-turbo schedules) → None → unchanged.
                try:
                    p = json.loads(params) if params else {}
                    hvac.set_state(
                        power=p.get("power"),
                        mode=p.get("mode"),
                        target_f=p.get("target_f"),
                        fan_speed=p.get("fan_speed"),
                        turbo=p.get("turbo"),
                    )
                except Exception:
                    pass
            elif device == "fan":
                # Exhaust fan: action is a fan mode ('on'/'off'/'auto'). Set the
                # mode only — the fan auto-logic block below applies the GPIO
                # this same tick, and the per-minute cron re-enforces it after.
                if action in ("on", "off", "auto"):
                    try:
                        config.write_fan_mode(action)
                    except OSError:
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

    # Read HVAC mode + energy + indoor thermistor. Returns dict
    # {ac_state, power_w, total_kwh, indoor_f}, or None if the dongle isn't
    # configured / reachable (all four columns stay NULL in that case).
    hvac_log = hvac.state_for_log()
    ac_state     = hvac_log["ac_state"]  if hvac_log else None
    ac_power_w   = hvac_log["power_w"]   if hvac_log else None
    ac_total_kwh = hvac_log["total_kwh"] if hvac_log else None
    ac_indoor_f  = hvac_log["indoor_f"]  if hvac_log else None

    # Write reading to RAM db
    ram_conn = config.get_ram_db()
    ram_conn.execute(
        "INSERT OR REPLACE INTO readings "
        "(epoch, temp_c, heater_state, ambient_c, fan_state, ac_state, ac_power_w, ac_total_kwh, ac_indoor_f) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_epoch, temp_c, heater_state, ambient_c, fan_state, ac_state, ac_power_w, ac_total_kwh, ac_indoor_f)
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

def _configure_logging():
    """Route library log records to syslog instead of stderr.

    This runs once a minute from cron, and cron mails anything a job writes to
    stdout or stderr. With no logging handler configured, Python falls back to
    `logging.lastResort`, which prints WARNING and above straight to stderr --
    so every library log line becomes an email.

    That is not hypothetical: msmart calls `_LOGGER.error()` whenever a device
    response fails its CRC check, then drops that one response and carries on.
    It is benign (the AC stays reachable; only that sample's energy columns go
    NULL, which aggregations already treat as "no data") but it lands on ~2% of
    samples -- roughly 25 mails a day.

    Installing a syslog handler fixes both halves: the records go somewhere
    durable and greppable (`journalctl -t log_temp`), and because a handler now
    exists, lastResort stops writing to stderr. Uncaught exceptions still print
    a traceback to stderr and are still mailed, which is exactly the split we
    want -- real breakage pages, routine library noise does not.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    try:
        handler = logging.handlers.SysLogHandler(address="/dev/log")
    except OSError:
        # No syslog socket (unusual, but do not take the reading down over it).
        # lastResort still applies, so behaviour is simply the old behaviour.
        return
    handler.setFormatter(logging.Formatter(
        "log_temp[%(process)d]: %(name)s %(levelname)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.WARNING)


if __name__ == "__main__":
    _configure_logging()

    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "flush":
        do_flush()
    elif mode == "rollup":
        do_rollup(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        do_log()
