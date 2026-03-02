"""
config.py — Shared constants and helpers for remote-switch.

Used by log_temp.py, switch.py, and migrate.py.
"""

import csv
import glob
import json
import os
import sqlite3
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
    except Exception:
        return None


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

_RAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    epoch         INTEGER PRIMARY KEY,
    temp_c        REAL,
    heater_state  INTEGER,
    ambient_c     REAL
);
"""

_DISK_SCHEMA = _RAM_SCHEMA + """
CREATE TABLE IF NOT EXISTS schedules (
    created_epoch  INTEGER PRIMARY KEY,
    execute_epoch  INTEGER NOT NULL,
    action         TEXT NOT NULL
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

    if RAM_DB.exists():
        conn.execute(f"ATTACH DATABASE '{RAM_DB}' AS ram")
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS ram.readings (
                epoch         INTEGER PRIMARY KEY,
                temp_c        REAL,
                heater_state  INTEGER,
                ambient_c     REAL
            );
        """)

    return conn


def query_readings(conn, since_epoch, until_epoch):
    """
    Return rows from both disk and RAM (if attached) for the given epoch range.
    UNION (not UNION ALL) auto-deduplicates by epoch value.
    """
    if _has_ram(conn):
        sql = """
            SELECT epoch, temp_c, heater_state, ambient_c FROM readings
            WHERE epoch >= ? AND epoch < ?
            UNION
            SELECT epoch, temp_c, heater_state, ambient_c FROM ram.readings
            WHERE epoch >= ? AND epoch < ?
            ORDER BY epoch
        """
        return conn.execute(sql, (since_epoch, until_epoch, since_epoch, until_epoch)).fetchall()
    else:
        return conn.execute(
            "SELECT epoch, temp_c, heater_state, ambient_c FROM readings "
            "WHERE epoch >= ? AND epoch < ? ORDER BY epoch",
            (since_epoch, until_epoch)
        ).fetchall()


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
