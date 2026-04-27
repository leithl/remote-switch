# Project context for Claude

## What this is
Raspberry Pi airplane hangar controller. The web UI controls three independent devices in the hangar (engine-block heater, exhaust fan, climate HVAC), shows a temperature/state chart (7d/30d/monthly), and schedules future actions. The DS18B20 probe logs hangar temp; outdoor ambient comes from Open-Meteo via lat/lon. The Pi Zero W is at the hangar; primarily on hangar WiFi with LTE as backup.

## Runtime architecture
- **No standalone Python service.** The app runs as Apache mod_wsgi (`switch.py`) + root cron jobs. No daemon, no Docker, no systemd app service.
- `switch.py` — WSGI app (mod_wsgi), serves web UI + API endpoints
- `config.py` — shared constants, DB helpers, GPIO, temp probe, ambient fetch
- `aggregate.py` — aggregation logic for chart data and stats
- `log_temp.py` — cron job: logs readings every minute, flushes weekly, rollup monthly
- `hvac.py` — Hangar HVAC (Durastar/Midea mini-split) control via WiFi dongle on LAN, msmart-ng wrapper
- `setup_hvac.py` — one-time pairing helper: discovers the dongle, fetches local token+key, writes HVAC_* keys to .env
- `templates/index.html` — Jinja2 template
- `heater-flush.service` — systemd unit, flushes RAM DB to disk on commanded shutdown/reboot

## Three independent controlled devices
**Don't conflate these — they are physically and electrically separate systems.**
1. **Engine-block heater** (`config.GPIO_PIN`=17) — relay on the airplane oil-pan heater. The original purpose of this project. Toggled by the web UI's "Heater" card and `?state=` query param.
2. **Exhaust fan** (`config.FAN_GPIO_PIN`=27) — relay on a hangar exhaust fan with auto/on/off mode.
3. **Hangar HVAC** — Durastar DRAW33F2A mini-split (Midea OEM) reached over the LAN via the Midea WiFi dongle (US-OSK105) using `msmart-ng` on TCP/6444. Controlled from the "Hangar HVAC" card. Has its own scheduling path through the same scheduler. All HVAC code lives in `hvac.py`; `config.py` and the heater code never touch it.

## Storage
**Hot-path writes MUST go to `/run` (tmpfs / RAM), not `/var/lib/heater` (SD card).** SD cards on a Pi Zero W have a limited write-cycle budget; this app runs every minute for years. Any new feature that writes more often than ~once a week belongs in `/run`. The accepted tradeoff: anything in `/run` is lost on unplanned power loss — for this hangar that's fine because the disk DB has data up to the last weekly flush, and the `heater-flush.service` systemd unit catches commanded reboots/shutdowns.

Files:
- `/run/heater.db` — RAM (tmpfs). **Only holds `readings` table.** Written every minute by cron.
- `/run/heater-ambient.tmp` — Ambient temp cache, 15-min TTL, ~50 bytes. Written ~96×/day.
- `/run/heater-hvac.json` — Cached HVAC dongle state (reported + commanded views), 30s TTL, ~400 bytes. Written every minute by `log_temp.py` (via `hvac.state_for_log()`) and on every HVAC apply.
- `/var/lib/heater/heater.db` — Disk (SD card). Holds `readings` + `schedules` + `monthly_cache`. Written **only weekly** (`log_temp.py flush`) and **monthly** (`log_temp.py rollup`), plus when the user adds/cancels a schedule (rare).
- The web UI ATTACHes both DBs and reads from both — no data gap between flushes.

When adding a new persistent thing, ask: how often is it written? If more than weekly → `/run`. If user-action-driven and rare → SD card is fine.

## Schedule schema (extended for HVAC)
The `schedules` table has two columns added on top of the original heater schema:
- `device TEXT DEFAULT 'heater'` — `'heater'` or `'hvac'`
- `params TEXT DEFAULT ''` — JSON for HVAC schedules: `{power, mode, target_f, fan_speed}` or `{mode: "freeze"}`
Existing heater rows keep `action` = `"0"`/`"1"`; HVAC rows use `action` = `"set"` and read everything from `params`. `log_temp.py do_log()` dispatches by device.

## Readings columns added for HVAC
`readings.ac_state INTEGER` — 0=off, 1=heat, 2=cool, 3=fan, 4=dry, 5=auto. Logged every minute by cron via `hvac.state_for_log()`. `aggregate.compute_bucketed` collapses this to a binary "any non-off" band rendered in purple on the chart.

## Schedules
- Stored in **disk DB only** — survive reboots with no extra effort.
- Executed by the every-minute cron job via `execute_epoch <= now`, so any schedule missed during downtime fires on the first tick after boot. No catch-up logic needed.

## Key patterns
- `_respond()` and `_redirect()` raise `_Response(Exception)` — never put these inside a bare `except Exception` block.
- SQL aggregation (`query_bucketed`, `query_batch_stats`) used instead of Python loops — Pi Zero W is slow.
- METAR/TAF fetched server-side (`?metar=1`, `?taf=1`) to avoid CORS issues with aviationweather.gov.
- `LOCATION` in `.env` can be an ICAO code (e.g. `KLMO`); lat/lon resolved via OurAirports CSV geocoding on first run.
- mod_wsgi daemon mode auto-reloads when `switch.py` changes — `git pull` is sufficient, no Apache restart needed.
- All schema additions use `ALTER TABLE ... ADD COLUMN` wrapped in try/except in `get_db()` / `get_ram_db()`. Don't introduce a separate migration system.

## HVAC module specifics (`hvac.py`)
- **Source of truth for the hangar climate.** All Durastar/Midea code lives here; nothing else imports `msmart`.
- **Lazy import:** `import msmart` happens only inside helper functions, so the WSGI app and cron jobs boot fine on a Pi that hasn't run `pip install msmart-ng` yet. `is_configured()` checks `.env` keys without ever touching the network.
- **Async→sync bridge:** `asyncio.run(coro)` wraps every call. mod_wsgi handlers stay sync; the dongle is on the LAN so RTT is sub-second.
- **Cache file** `/run/heater-hvac.json`:
  ```json
  {"reported": {power, mode, target_c, target_f, fan_speed, indoor_c, indoor_f, epoch},
   "commanded": {…same fields…} | null}
  ```
  TTL is `hvac.CACHE_TTL_SECS = 30`. `get_state()` returns `{reported, commanded, stale, age_secs, diverged}`. On dongle error, returns the stale cached view with `stale=True` instead of raising.
- **Two-way visibility:** `set_state()` writes both `reported` (post-apply) and `commanded` (what we asked for). UI surfaces commanded only when `_diverged()` returns True (catches drift if someone uses the physical IR remote — common in this hangar).
- **Stable mode/fan tokens** (used in `.env`, schedule `params` JSON, `?hvac_*=` query strings, and chart logic): `MODE_AUTO/COOL/DRY/HEAT/FAN/FREEZE` and `FAN_AUTO/LOW/MED/HIGH`. Don't add or rename without checking all four call sites.
- **Freeze Prevention is a SOFTWARE PRESET, not a real anti-freeze flag.** `_resolve_freeze()` returns `(power=True, mode=HEAT, target_f=60, fan=LOW)` — Midea's heat-mode minimum. msmart-ng does not expose Midea's actual "8°C heating" feature byte over the dongle (it's IR-only on most units). If a true anti-freeze API later surfaces, replace `_resolve_freeze()` and leave the preset name unchanged.
- **`state_for_log()`** returns `None` (not 0) when not configured / unreachable, so the `readings.ac_state` column stays NULL rather than misreporting "off". Aggregations treat `None` as "no data, don't render".

## HVAC pairing (`setup_hvac.py`)
- Use the **NetHome Plus** app to pair, NOT SmartHome. The SmartHome `get_token` cloud endpoint is currently broken (msmart-ng issue #201). The script enforces `account="NetHomePlus"` when it calls `Cloud(...)`.
- One-time cloud roundtrip during pairing only; all subsequent control is local on TCP/6444. After pairing, the dongle's outbound internet can be blocked at the firewall without losing functionality.
- The script preserves other `.env` keys (only replaces the four `HVAC_*` lines).

## URL surface
- Heater: `?state=0|1`
- Fan: `?fan_state=0|1`, `?fan_mode=auto`
- HVAC immediate apply: `?hvac_apply=1` plus `&hvac_power=0|1&hvac_mode=…&hvac_target_f=…&hvac_fan_speed=…`. Special case: `&hvac_mode=freeze` triggers the Freeze Prevention preset and ignores the other args.
- Schedule add: `?sched_dt=YYYY-MM-DDTHH:MM&sched_device=heater|hvac` plus device-specific params (`sched_action` for heater; `sched_hvac_mode/target_f/fan_speed/power` for HVAC).
- Schedule cancel: `?cancel_id=<created_epoch>`.
- Chart range: `?range=7d|30d|YYYY-MM`.

## Chart bands and colors
- Heater (engine-block) — `rgba(220, 53, 69, 0.25)` red
- HVAC (climate) — `rgba(168, 85, 247, 0.25)` purple
- Fan — `rgba(13, 110, 253, 0.20)` blue
- Cold (≤48°F) — `rgba(255, 152, 0, 0.15)` orange box annotation
- Ambient line — `rgb(34, 197, 94)` green
