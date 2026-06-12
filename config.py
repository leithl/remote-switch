"""
config.py — Shared constants and helpers for remote-switch.

Used by log_temp.py, switch.py, and migrate.py.
"""

import csv
import glob
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime
from io import StringIO
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR   = Path(__file__).parent
RAM_DB       = Path("/run/heater.db")
DISK_DB_DIR  = Path("/var/lib/heater")
DISK_DB      = DISK_DB_DIR / "heater.db"
AMBIENT_CACHE = Path("/run/heater-ambient.tmp")

# ---------------------------------------------------------------------------
# GPIO / temperature probe
# ---------------------------------------------------------------------------
GPIO_PIN    = "17"
W1_GLOB     = "/sys/bus/w1/devices/28-*/w1_slave"
ENABLE_TEMP = True  # set to False to disable all temperature features

# ---------------------------------------------------------------------------
# Exhaust fan
# ---------------------------------------------------------------------------
FAN_GPIO_PIN         = "27"   # GPIO pin for exhaust fan relay
FAN_TEMP_THRESHOLD_C = 26.67  # 80°F — hangar must be at or above this to run fan
FAN_MARGIN_C         = 2.78   # 5°F — ambient must be this much cooler than hangar
FAN_MODE_FILE        = DISK_DB_DIR / "fan_mode"  # persists 'auto' / 'on' / 'off'

# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------

def load_env():
    """Parse SCRIPT_DIR/.env (key=value, # comments). Returns dict."""
    env = {}
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        return env
    with env_file.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


def save_env_coords(lat, lon):
    """Append LATITUDE= and LONGITUDE= to SCRIPT_DIR/.env."""
    env_file = SCRIPT_DIR / ".env"
    with env_file.open("a") as f:
        f.write(f"\nLATITUDE={lat}\nLONGITUDE={lon}\n")


# ---------------------------------------------------------------------------
# Location / geocoding
# ---------------------------------------------------------------------------

def geocode_location(icao):
    """
    Look up ICAO airport code in OurAirports CSV.
    Saves coords to .env and returns (lat_str, lon_str) or (None, None).
    """
    try:
        url = "https://davidmegginson.github.io/ourairports-data/airports.csv"
        req = urllib.request.urlopen(url, timeout=15)
        text = req.read().decode("utf-8", errors="replace")
    except Exception:
        return None, None

    reader = csv.reader(StringIO(text))
    next(reader, None)  # skip header
    for row in reader:
        if len(row) >= 6 and row[1].strip('"') == icao:
            lat = row[4].strip('"')
            lon = row[5].strip('"')
            if lat and lon:
                save_env_coords(lat, lon)
                return lat, lon
    return None, None


def get_location():
    """
    Return (latitude_str, longitude_str, label_str).
    Reads .env; geocodes LOCATION if LATITUDE/LONGITUDE missing.
    Returns (None, None, "Ambient") if location not configured.
    """
    env = load_env()
    lat = env.get("LATITUDE", "").strip()
    lon = env.get("LONGITUDE", "").strip()
    location = env.get("LOCATION", "").strip()

    if lat and lon:
        label = f"Ambient ({location})" if location else f"Ambient ({lat}\u00b0, {lon}\u00b0)"
        return lat, lon, label

    if location:
        lat, lon = geocode_location(location)
        if lat and lon:
            # Re-read env to get fresh label after save
            label = f"Ambient ({location})"
            return lat, lon, label

    return None, None, "Ambient"


# ---------------------------------------------------------------------------
# GPIO
# ---------------------------------------------------------------------------

def _gpio_path():
    return Path(f"/sys/class/gpio/gpio{GPIO_PIN}/value")


def read_gpio():
    """Return '1' or '0'. Returns '0' if GPIO sysfs path absent."""
    p = _gpio_path()
    if p.exists():
        return p.read_text().strip()
    return "0"


def write_gpio(value):
    """Write '0' or '1' to GPIO sysfs. Raises PermissionError if not writable."""
    _gpio_path().write_text(str(value))


def _fan_gpio_path():
    return Path(f"/sys/class/gpio/gpio{FAN_GPIO_PIN}/value")


def read_fan_gpio():
    """Return '1' or '0'. Returns '0' if fan GPIO sysfs path absent."""
    p = _fan_gpio_path()
    if p.exists():
        return p.read_text().strip()
    return "0"


def write_fan_gpio(value):
    """Write '0' or '1' to fan GPIO sysfs. Raises PermissionError if not writable."""
    _fan_gpio_path().write_text(str(value))


def read_fan_mode():
    """Return current fan mode: 'auto', 'on', or 'off'. Defaults to 'auto'."""
    try:
        return FAN_MODE_FILE.read_text().strip()
    except OSError:
        return "auto"


def write_fan_mode(mode):
    """Persist fan mode ('auto', 'on', 'off') to disk."""
    DISK_DB_DIR.mkdir(parents=True, exist_ok=True)
    FAN_MODE_FILE.write_text(mode)


# ---------------------------------------------------------------------------
# DS18B20 temperature probe
# ---------------------------------------------------------------------------

def read_temp():
    """Return temperature in °C (float) or None if sensor absent / read fails."""
    matches = glob.glob(W1_GLOB)
    if not matches:
        return None
    device = matches[0]
    try:
        with open(device) as f:
            lines = f.readlines()
        if not lines or not lines[0].strip().endswith("YES"):
            return None
        for line in lines:
            if "t=" in line:
                raw = line.split("t=")[1].strip()
                return round(int(raw) / 1000, 1)
    except (OSError, ValueError):
        return None
    return None


# ---------------------------------------------------------------------------
# Ambient temperature (Open-Meteo, 15-min file cache)
# ---------------------------------------------------------------------------

def fetch_ambient(lat, lon):
    """
    Return ambient temperature in °C (float) or None.
    Caches result for 15 minutes in AMBIENT_CACHE to reduce API calls.
    """
    if not lat or not lon:
        return None

    now = int(datetime.now().timestamp())

    # Try cache first
    if AMBIENT_CACHE.exists():
        try:
            cached_ts, cached_val = AMBIENT_CACHE.read_text().strip().split(",", 1)
            if now - int(cached_ts) < 900:
                return float(cached_val)
        except (ValueError, OSError):
            pass

    # Fetch fresh
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}&current=temperature_2m"
    )
    try:
        req = urllib.request.urlopen(url, timeout=5)
        data = json.loads(req.read().decode())
        temp = data["current"]["temperature_2m"]
        AMBIENT_CACHE.write_text(f"{now},{temp}\n")
        return float(temp)
    except Exception as e:
        # Cron captures stderr; route to journal so multi-hour outages
        # (which silently leave ambient_c=NULL across many rows) are
        # diagnosable after the fact instead of invisible.
        print(f"fetch_ambient failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

_RAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    epoch         INTEGER PRIMARY KEY,
    temp_c        REAL,
    heater_state  INTEGER,
    ambient_c     REAL,
    fan_state     INTEGER,
    ac_state      INTEGER,
    ac_power_w    REAL,
    ac_total_kwh  REAL,
    ac_indoor_f   REAL
);
"""

_DISK_SCHEMA = _RAM_SCHEMA + """
CREATE TABLE IF NOT EXISTS schedules (
    created_epoch  INTEGER PRIMARY KEY,
    execute_epoch  INTEGER NOT NULL,
    action         TEXT NOT NULL,
    device         TEXT DEFAULT 'heater',
    params         TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS monthly_cache (
    month  TEXT PRIMARY KEY,
    data   TEXT NOT NULL
);
"""


def get_ram_db():
    """
    Open /run/heater.db (RAM).
    Uses MEMORY journal mode — no -wal/-shm files created in /run.
    Creates readings schema if needed.
    Returns sqlite3.Connection.
    """
    RAM_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(RAM_DB))
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.executescript(_RAM_SCHEMA)
    for col_sql in (
        "ALTER TABLE readings ADD COLUMN fan_state INTEGER",
        "ALTER TABLE readings ADD COLUMN ac_state INTEGER",
        "ALTER TABLE readings ADD COLUMN ac_power_w REAL",
        "ALTER TABLE readings ADD COLUMN ac_total_kwh REAL",
        "ALTER TABLE readings ADD COLUMN ac_indoor_f REAL",
    ):
        try:
            conn.execute(col_sql)
        except Exception:
            pass  # column already exists
    return conn


def get_db():
    """
    Open /var/lib/heater/heater.db (disk) and optionally ATTACH /run/heater.db as 'ram'.

    - Uses DELETE journal mode (no WAL files needed; /var/lib/heater dir is group-writable).
    - Only ATTACHes RAM db if the file already exists (avoids SQLite silently creating
      an empty file and then raising OperationalError on first query).
    - Returns sqlite3.Connection with ram schema attached when available.
    """
    DISK_DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DISK_DB))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(_DISK_SCHEMA)
    for col_sql in (
        "ALTER TABLE readings ADD COLUMN fan_state INTEGER",
        "ALTER TABLE readings ADD COLUMN ac_state INTEGER",
        "ALTER TABLE readings ADD COLUMN ac_power_w REAL",
        "ALTER TABLE readings ADD COLUMN ac_total_kwh REAL",
        "ALTER TABLE readings ADD COLUMN ac_indoor_f REAL",
        "ALTER TABLE schedules ADD COLUMN device TEXT DEFAULT 'heater'",
        "ALTER TABLE schedules ADD COLUMN params TEXT DEFAULT ''",
    ):
        try:
            conn.execute(col_sql)
        except Exception:
            pass  # column already exists

    if RAM_DB.exists():
        conn.execute(f"ATTACH DATABASE '{RAM_DB}' AS ram")
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS ram.readings (
                epoch         INTEGER PRIMARY KEY,
                temp_c        REAL,
                heater_state  INTEGER,
                ambient_c     REAL,
                fan_state     INTEGER,
                ac_state      INTEGER,
                ac_power_w    REAL,
                ac_total_kwh  REAL,
                ac_indoor_f   REAL
            );
        """)
        for col_sql in (
            "ALTER TABLE ram.readings ADD COLUMN fan_state INTEGER",
            "ALTER TABLE ram.readings ADD COLUMN ac_state INTEGER",
            "ALTER TABLE ram.readings ADD COLUMN ac_power_w REAL",
            "ALTER TABLE ram.readings ADD COLUMN ac_total_kwh REAL",
            "ALTER TABLE ram.readings ADD COLUMN ac_indoor_f REAL",
        ):
            try:
                conn.execute(col_sql)
            except Exception:
                pass  # column already exists

    return conn


def query_readings(conn, since_epoch, until_epoch):
    """
    Return rows from both disk and RAM (if attached) for the given epoch range.
    UNION (not UNION ALL) auto-deduplicates by epoch value.

    Row shape: (epoch, temp_c, heater_state, ambient_c, fan_state, ac_power_w).
    ac_state is not returned — chart logic now uses ac_power_w directly to
    build the power line. Pre-2026-05-07 rows have NULL ac_power_w.
    """
    cols = "epoch, temp_c, heater_state, ambient_c, fan_state, ac_power_w"
    if _has_ram(conn):
        sql = (
            f"SELECT {cols} FROM readings WHERE epoch >= ? AND epoch < ?"
            f" UNION SELECT {cols} FROM ram.readings WHERE epoch >= ? AND epoch < ?"
            " ORDER BY epoch"
        )
        return conn.execute(sql, (since_epoch, until_epoch, since_epoch, until_epoch)).fetchall()
    else:
        return conn.execute(
            f"SELECT {cols} FROM readings WHERE epoch >= ? AND epoch < ? ORDER BY epoch",
            (since_epoch, until_epoch)
        ).fetchall()


def query_temp_pairs(conn, since_epoch, until_epoch):
    """
    Return per-minute (epoch, temp_c, ac_indoor_f) rows from both DBs.

    Used for door-event detection — that needs raw 1-minute granularity,
    not bucketed averages, to catch the 5-15 min divergence between the
    floor-level Pi DS18B20 and the ceiling-level unit thermistor when a
    hangar door opens.
    """
    cols = "epoch, temp_c, ac_indoor_f"
    if _has_ram(conn):
        sql = (
            f"SELECT {cols} FROM readings WHERE epoch >= ? AND epoch < ?"
            f" UNION SELECT {cols} FROM ram.readings WHERE epoch >= ? AND epoch < ?"
            " ORDER BY epoch"
        )
        return conn.execute(sql, (since_epoch, until_epoch, since_epoch, until_epoch)).fetchall()
    return conn.execute(
        f"SELECT {cols} FROM readings WHERE epoch >= ? AND epoch < ? ORDER BY epoch",
        (since_epoch, until_epoch)
    ).fetchall()


def query_bucketed(conn, since_epoch, until_epoch, bucket_secs=900):
    """
    Return pre-aggregated 15-min bucket rows for chart rendering.

    Uses SQL GROUP BY to collapse ~N per-minute rows into ~N/15 rows,
    replacing Python-side bucketing in aggregate.compute().

    Each row: (bucket_epoch, avg_temp_c, avg_ambient_c, avg_heater_state).
    avg_heater_state is the fraction of minutes in the bucket the heater was on
    (0.0–1.0), or None if heater_state was never logged in this bucket.
    """
    bs = bucket_secs
    # AVG(ac_power_w) is the average compressor draw over the bucket.
    # Pre-2026-05-07 rows have NULL ac_power_w — AVG() ignores NULLs, so a
    # bucket from before this column existed just yields NULL avg_ac_power_w
    # (rendered as a gap in the power line). The chart no longer renders an
    # HVAC band; the power line replaces it.
    select = (
        f"(epoch/{bs})*{bs},"
        " AVG(temp_c), AVG(ambient_c), AVG(heater_state), AVG(fan_state),"
        " AVG(ac_power_w)"
    )
    inner_cols = "epoch, temp_c, heater_state, ambient_c, fan_state, ac_power_w"
    group = f"GROUP BY (epoch/{bs})*{bs} ORDER BY (epoch/{bs})*{bs}"
    if _has_ram(conn):
        sql = (
            f"SELECT {select} FROM ("
            f"SELECT {inner_cols} FROM readings"
            " WHERE epoch >= ? AND epoch < ?"
            " UNION"
            f" SELECT {inner_cols} FROM ram.readings"
            " WHERE epoch >= ? AND epoch < ?"
            f") {group}"
        )
        return conn.execute(sql, (since_epoch, until_epoch, since_epoch, until_epoch)).fetchall()
    sql = f"SELECT {select} FROM readings WHERE epoch >= ? AND epoch < ? {group}"
    return conn.execute(sql, (since_epoch, until_epoch)).fetchall()


def query_batch_stats(conn, since_epoch, until_epoch, power_on_threshold_w):
    """
    Compute per-calendar-month aggregated stats in a single SQL query.

    Replaces N calls to aggregate.compute() for monthly stats — all months
    are grouped in one pass over the data.

    power_on_threshold_w: watts above which an ac_power_w sample counts as
    "HVAC actually running" — pass aggregate.POWER_ON_THRESHOLD_W (kept as a
    parameter so config.py doesn't import aggregate).

    Returns dict: {month_key: stats_tuple} where month_key is 'YYYY-MM' in
    local time and stats_tuple is:
        (avg_c, min_c, max_c, cold_mins, count_temp,
         on_mins, count_heater,
         fan_mins, count_fan,
         avg_amb_c, min_amb_c, max_amb_c, cold_amb_mins, count_amb,
         hvac_watt_mins, count_hvac)
    hvac_watt_mins sums ac_power_w over above-threshold minutes only (0 when
    the month has no such minutes). Months with no data are absent from the dict.
    """
    thr = int(power_on_threshold_w)
    select = (
        "strftime('%Y-%m', epoch, 'unixepoch', 'localtime') AS mk,"
        " AVG(temp_c), MIN(temp_c), MAX(temp_c),"
        " SUM(CASE WHEN temp_c <= 8.89 THEN 1 ELSE 0 END),"
        " COUNT(temp_c),"
        " SUM(CASE WHEN heater_state = 1 THEN 1 ELSE 0 END),"
        " COUNT(heater_state),"
        " SUM(CASE WHEN fan_state = 1 THEN 1 ELSE 0 END),"
        " COUNT(fan_state),"
        " AVG(ambient_c), MIN(ambient_c), MAX(ambient_c),"
        " SUM(CASE WHEN ambient_c <= 8.89 THEN 1 ELSE 0 END),"
        " COUNT(ambient_c),"
        f" SUM(CASE WHEN ac_power_w > {thr} THEN ac_power_w ELSE 0 END),"
        " COUNT(ac_power_w)"
    )
    if _has_ram(conn):
        sql = (
            f"SELECT {select} FROM ("
            "SELECT epoch, temp_c, heater_state, ambient_c, fan_state, ac_power_w FROM readings"
            " WHERE epoch >= ? AND epoch < ?"
            " UNION"
            " SELECT epoch, temp_c, heater_state, ambient_c, fan_state, ac_power_w FROM ram.readings"
            " WHERE epoch >= ? AND epoch < ?"
            ") GROUP BY mk ORDER BY mk"
        )
        rows = conn.execute(sql, (since_epoch, until_epoch, since_epoch, until_epoch)).fetchall()
    else:
        sql = (
            f"SELECT {select} FROM readings"
            " WHERE epoch >= ? AND epoch < ?"
            " GROUP BY mk ORDER BY mk"
        )
        rows = conn.execute(sql, (since_epoch, until_epoch)).fetchall()
    return {row[0]: row[1:] for row in rows}


def query_first_epoch(conn):
    """
    Return the epoch of the oldest reading across disk + RAM, or None if
    there are no readings at all. Effectively free — epoch is the PRIMARY
    KEY, so MIN(epoch) is a B-tree head lookup, not a scan.
    """
    if _has_ram(conn):
        row = conn.execute(
            "SELECT MIN(e) FROM ("
            "SELECT MIN(epoch) AS e FROM readings"
            " UNION ALL SELECT MIN(epoch) AS e FROM ram.readings)"
        ).fetchone()
    else:
        row = conn.execute("SELECT MIN(epoch) FROM readings").fetchone()
    return row[0] if row else None


def _has_ram(conn):
    """Return True if 'ram' database is attached."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "ram":
            return True
    return False


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def subtract_months(d, n):
    """
    Return a date-like object that is n months before d.
    n may be negative to add months.
    d must support .year and .month; returns datetime.date.
    """
    from datetime import date
    month = d.month - n
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    return date(year, month, 1)
