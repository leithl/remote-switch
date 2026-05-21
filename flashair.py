"""
flashair.py — Consume the flashair-sync daemon's /status endpoint.

flashair-sync runs on a separate Pi (the `pi` host) as a systemd daemon and
serves a single-line JSON status at `GET /status` on TCP/8765. This module
polls it once a minute via log_temp.py, caches the answer to
`/run/heater-flashair.json`, and exposes a one-line display string to
switch.py.

Cache contract (mirrors upstream verbatim plus `stale`, set by this
consumer on fetch failure or upstream-too-old):

    {"epoch": <int>, "last_sync_epoch": <int|None>,
     "last_sync_files_n": <int>, "transferring": <bool>,
     "current_file": <str|None>, "stale": <bool>}

Opt-in: surface only when `FLASHAIR_STATUS_URL` is present in .env (the
key may be set to an empty value to use the default URL). Without the
key the cache is never written and the UI shows nothing.

Mirrors the `/run/heater-hvac.json` pattern: tmpfs cache, 0664 perms so
both cron (root) and WSGI (www-data) can read it, never raises.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import config

FLASHAIR_CACHE     = Path("/run/heater-flashair.json")
DEFAULT_URL        = "http://pi.local:8765/status"
FETCH_TIMEOUT_SECS = 5
# Treat the cache as "unreachable" when its epoch is older than this. The
# cron writes every minute, so 120s gives one missed cycle of grace.
STALE_AFTER_SECS   = 120


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def status_url():
    """Return the configured upstream URL, or None if the key is absent."""
    env = config.load_env()
    if "FLASHAIR_STATUS_URL" not in env:
        return None
    val = env.get("FLASHAIR_STATUS_URL", "").strip()
    return val or DEFAULT_URL


def is_configured():
    return status_url() is not None


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _now_epoch():
    return int(datetime.now().timestamp())


def _write_cache(payload):
    """Write the cache file with 0664 perms. Never raises."""
    try:
        FLASHAIR_CACHE.parent.mkdir(parents=True, exist_ok=True)
        FLASHAIR_CACHE.write_text(json.dumps(payload))
        os.chmod(str(FLASHAIR_CACHE), 0o664)
    except OSError:
        pass


def read_cache():
    """Return the parsed cache dict, or None if absent / unreadable."""
    try:
        return json.loads(FLASHAIR_CACHE.read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Polling (called from log_temp.py do_log)
# ---------------------------------------------------------------------------

def refresh_cache():
    """Fetch upstream, update the cache. Never raises.

    On fetch failure (timeout, connect refused, JSON parse error, anything)
    the cache is written with `stale: true` and a fresh epoch — that way the
    UI's `now - epoch > 120s` check still distinguishes "we just tried and
    couldn't reach it" from "the cron itself stopped running".
    """
    if not is_configured():
        return
    url = status_url()
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cache = dict(data)
        cache["stale"] = False
    except Exception:
        cache = {
            "epoch": _now_epoch(),
            "last_sync_epoch": None,
            "last_sync_files_n": 0,
            "transferring": False,
            "current_file": None,
            "stale": True,
        }
    _write_cache(cache)


# ---------------------------------------------------------------------------
# UI rendering (called from switch.py)
# ---------------------------------------------------------------------------

def _fmt_age(secs):
    secs = int(secs)
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f"{m}:{s:02d} ago"
    if secs < 86400:
        h, rem = divmod(secs, 3600)
        m = rem // 60
        return f"{h}h{m:02d}m ago" if m else f"{h}h ago"
    return f"{secs // 86400}d ago"


def display_text(cache=None, now_epoch=None):
    """Return the one-line UI status, or None if nothing to show.

    States:
        idle     -> "12 files, 0:30 ago"
        active   -> "transferring log_20260521_134505_KLMO.csv"
                 -> "transferring..." (if no current_file)
        stale    -> "unreachable"
    """
    if cache is None:
        cache = read_cache()
    if cache is None:
        return None
    if now_epoch is None:
        now_epoch = _now_epoch()

    epoch = cache.get("epoch") or 0
    if cache.get("stale") or (now_epoch - epoch) > STALE_AFTER_SECS:
        return "unreachable"

    if cache.get("transferring"):
        cf = cache.get("current_file")
        return f"transferring {cf}" if cf else "transferring..."

    lse = cache.get("last_sync_epoch")
    n   = cache.get("last_sync_files_n") or 0
    noun = "file" if n == 1 else "files"
    if lse is None:
        return f"{n} {noun}, no recent sync"
    age = max(0, now_epoch - int(lse))
    return f"{n} {noun}, {_fmt_age(age)}"
