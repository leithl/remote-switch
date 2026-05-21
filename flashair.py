"""
flashair.py — Read the flashair-sync daemon's status file.

flashair-sync runs on this same Pi as a separate systemd daemon (User=pi)
and writes `/run/heater-flashair.json` on every sync state change. This
module reads that file at page-render time and exposes a one-line display
string to switch.py — no HTTP, no localhost loopback, no cron poll.

File contract (written by flashair-sync, see its README "Status file"):

    {"epoch": <int>, "last_sync_epoch": <int|None>,
     "last_sync_files_n": <int>, "transferring": <bool>,
     "current_file": <str|None>}

Opt-in: surface only when `/run/heater-flashair.json` exists. Installs
that don't run flashair-sync on this Pi see nothing in the UI; no env
config required.
"""

import json
from datetime import datetime
from pathlib import Path

FLASHAIR_STATUS_FILE = Path("/run/heater-flashair.json")
# Treat the file as "unreachable" when its epoch is older than this. The
# upstream daemon writes at every transferring on/off transition and at
# every sync record, so 120s gives a generous grace window before we
# call it stale (covers the idle-between-syncs gap).
STALE_AFTER_SECS = 120


# ---------------------------------------------------------------------------
# Opt-in / cache read
# ---------------------------------------------------------------------------

def is_configured():
    """True when flashair-sync is running on this Pi (its status file exists)."""
    return FLASHAIR_STATUS_FILE.exists()


def _now_epoch():
    return int(datetime.now().timestamp())


def read_cache():
    """Return the parsed status dict, or None if absent / unreadable."""
    try:
        return json.loads(FLASHAIR_STATUS_FILE.read_text())
    except (OSError, ValueError):
        return None


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
    if (now_epoch - epoch) > STALE_AFTER_SECS:
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
