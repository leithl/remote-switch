# Project context for Claude

## What this is
Raspberry Pi airplane hangar heater controller. The web UI lets the user remotely toggle a GPIO-controlled relay (the heater), view a temperature/heater-state chart (7d/30d/monthly), and schedule future on/off actions. The DS18B20 probe logs hangar temp; outdoor ambient comes from Open-Meteo via lat/lon. The Pi Zero W is LTE-connected and physically located at the hangar.

## Runtime architecture
- **No standalone Python service.** The app runs as Apache mod_wsgi (`switch.py`) + root cron jobs. No daemon, no Docker, no systemd app service.
- `switch.py` — WSGI app (mod_wsgi), serves web UI + API endpoints
- `config.py` — shared constants, DB helpers, GPIO, temp probe, ambient fetch
- `aggregate.py` — aggregation logic for chart data and stats
- `log_temp.py` — cron job: logs readings every minute, flushes weekly, rollup monthly
- `templates/index.html` — Jinja2 template
- `heater-flush.service` — systemd unit, flushes RAM DB to disk on commanded shutdown/reboot

## Storage
- `/run/heater.db` — RAM (tmpfs). **Only holds `readings` table.** Written every minute by cron.
- `/var/lib/heater/heater.db` — Disk. Holds `readings` + `schedules` + `monthly_cache`. Flushed weekly.
- `/run/heater-ambient.tmp` — Ambient temp cache, 15-min TTL, ~50 bytes.
- The web UI ATTACHes both DBs and reads from both — no data gap between flushes.

## Schedules
- Stored in **disk DB only** — survive reboots with no extra effort.
- Executed by the every-minute cron job via `execute_epoch <= now`, so any schedule missed during downtime fires on the first tick after boot. No catch-up logic needed.

## Key patterns
- `_respond()` and `_redirect()` raise `_Response(Exception)` — never put these inside a bare `except Exception` block.
- SQL aggregation (`query_bucketed`, `query_batch_stats`) used instead of Python loops — Pi Zero W is slow.
- METAR/TAF fetched server-side (`?metar=1`, `?taf=1`) to avoid CORS issues with aviationweather.gov.
- `LOCATION` in `.env` can be an ICAO code (e.g. `KLMO`); lat/lon resolved via OurAirports CSV geocoding on first run.
- mod_wsgi daemon mode auto-reloads when `switch.py` changes — `git pull` is sufficient, no Apache restart needed.
