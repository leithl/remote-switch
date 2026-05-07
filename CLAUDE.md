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
- `scripts/setup-wifi-bridge.sh` — optional one-shot installer for the AP+STA bridge that lets the Midea dongle pair on open hangar WiFi. See "WiFi bridge" below.
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
- `readings.ac_state INTEGER` — 0=off, 1=heat, 2=cool, 3=fan, 4=dry, 5=auto. The unit's *mode*, not its *running state*. In Freeze Prevention overnight the unit sits in HEAT mode 24/7 even though the compressor cycles only briefly per hour, so this column alone overstates "HVAC on".
- `readings.ac_power_w REAL` — instantaneous power draw from the dongle's energy meter, polled every minute. Idle baseline on the Durastar DRAW33F2A is ~60W (controller + dongle + standby); the compressor pushes it well past 100W. The chart band uses `> 100W` as the "actually running" threshold.
- `readings.ac_total_kwh REAL` — cumulative lifetime kWh from the dongle. Subtract first/last in a window to compute kWh used. Monotonically increasing.
- `readings.ac_indoor_f REAL` — the indoor unit's own thermistor reading, in °F (same field shown on the web UI). The unit is ceiling-mounted at ~12 ft; this is what FP's regulator sees and acts on. Compare to the floor-level DS18B20 in `temp_c` (~4 ft) — the gap is the vertical-stratification signal, which should widen during compressor heat output and narrow when the unit is idle.
- `aggregate.compute_bucketed` averages `ac_power_w` per bucket and renders it as a Watts line on the chart's right y-axis (replacing the previous binary HVAC band). Buckets at or below `aggregate.POWER_ON_THRESHOLD_W` (=100) are filtered out — the ~60W standby baseline (controller + dongle + indoor fan trickle) is treated as "off" and produces no point, so the chart only shows actual heating/cooling activity. Pre-2026-05-07 rows have NULL `ac_power_w` — those buckets also produce no point. The `ac_state` column is still logged but no longer drives any chart logic.

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

## WiFi bridge (`scripts/setup-wifi-bridge.sh`)
- **Purpose: pairing only.** The Midea dongle's pairing app (NetHome Plus) refuses to pair against an open SSID. Many hangar WiFi networks are open. The bridge lets the Pi broadcast its own WPA2 SSID for the dongle to live on, while the Pi stays a client on the open hangar WiFi and NATs the dongle's traffic out.
- **The dongle's permanent home is the Pi's AP**, not the hangar WiFi. After the one-time cloud handshake (driven by `setup_hvac.py`), the dongle never needs the internet again — `msmart-ng` reaches it locally on the AP subnet at TCP/6444.
- **Single-radio AP+STA shares one channel.** `AP_CHAN` in the install script must match the hangar WiFi's current channel; hostapd refuses to start on a mismatch with "Could not set channel". If the hangar router roams channels, pin it.
- **Files written by the script:**
  - `/etc/systemd/system/uap0.service` — creates the `uap0` virtual __ap interface on `wlan0`, gives it `192.168.50.1/24`
  - `/etc/hostapd/hostapd.conf` — WPA2-PSK on `uap0`, channel hard-coded
  - `/etc/dnsmasq.d/uap0.conf` — DHCP `192.168.50.50–.150` on `uap0` only (`bind-interfaces` keeps it off `wlan0`)
  - `/etc/NetworkManager/conf.d/uap0-unmanaged.conf` (Bookworm) or `denyinterfaces uap0` in `/etc/dhcpcd.conf` (Bullseye) — keeps the system network manager from fighting hostapd over `uap0`
  - iptables MASQUERADE on `wlan0` + FORWARD rules, persisted via `iptables-persistent` / `netfilter-persistent`
- **Single dongle, single client.** Don't optimize this for many clients — there's exactly one (the Midea). Throughput is irrelevant; reliability over months is what matters.
- **BCM43438 AP+STA can wedge.** Symptom: dongle drops off, hostapd looks healthy but new associations fail. Recovery: `sudo systemctl restart hostapd`. If this becomes routine, the answer is a USB WiFi dongle (e.g. TL-WN725N) so AP and STA live on separate radios — not a watchdog. A watchdog is fine as a stopgap but masks the real issue.
- **Don't add a second AP for clients.** This bridge exists to pair an IoT device, not to serve users. Adding clients re-introduces all the throughput and channel-sharing issues we accept here.

## HVAC module specifics (`hvac.py`)
- **Source of truth for the hangar climate.** All Durastar/Midea code lives here; nothing else imports `msmart`.
- **Lazy import:** `import msmart` happens only inside helper functions, so the WSGI app and cron jobs boot fine on a Pi that hasn't run `pip install msmart-ng` yet. `is_configured()` checks `.env` keys without ever touching the network.
- **Async→sync bridge:** `asyncio.run(coro)` wraps every call. mod_wsgi handlers stay sync; the dongle is on the LAN so RTT is sub-second.
- **Cache file** `/run/heater-hvac.json`:
  ```json
  {"reported": {power, mode, target_c, target_f, fan_speed, indoor_c, indoor_f,
                freeze_protection, power_w, total_kwh, epoch},
   "commanded": {…same fields…} | null}
  ```
  TTL is `hvac.CACHE_TTL_SECS = 30`. `get_state()` returns `{reported, commanded, stale, age_secs, diverged}`. On dongle error, returns the stale cached view with `stale=True` instead of raising. `power_w` / `total_kwh` are populated by an extra `GetEnergyUsageCommand` that `_open_device()` enables via the local `dev.enable_energy_usage_requests = True` toggle (no SetState sent).
- **Two-way visibility:** `set_state()` writes both `reported` (post-apply) and `commanded` (what we asked for). UI surfaces commanded only when `_diverged()` returns True (catches drift if someone uses the physical IR remote — common in this hangar).
- **Stable mode/fan tokens** (used in `.env`, schedule `params` JSON, `?hvac_*=` query strings, and chart logic): `MODE_AUTO/COOL/DRY/HEAT/FAN/FREEZE` and `FAN_AUTO/LOW/MED/HIGH`. Don't add or rename without checking all four call sites.
- **Freeze Prevention is the unit's REAL flag.** `MODE_FREEZE` maps to `dev.freeze_protection = True` over the LAN protocol — the same feature the IR remote drives, the unit displays "FP", and the firmware holds a minimum heat output (~8°C/46°F) internally. Capability flags (`dev.supports_freeze_protection`) are unreliable on some units (msmart-ng issue #76); we always send the SetState bit regardless. Setting any other explicit `mode` automatically clears the freeze flag — exit FP whenever the user picks a different mode. The state byte is `payload[21] bit 0x80` in msmart-ng's SetState/StateResponse if you ever need to debug at the wire level.
- **`state_for_log()`** returns `{ac_state, power_w, total_kwh}` for `log_temp.py` to write into the per-minute row, or `None` when not configured / unreachable (all three columns stay NULL — aggregations treat NULL as "no data, don't render"). The dict shape lets one refresh feed three columns; don't reintroduce a separate getter.

## msmart-ng API surface (verified 2025.12.0)
The wrappers were verified against msmart-ng 2025.12.0. If you upgrade, watch these specific symbols — past versions have moved them, and a future bump may again:
- `from msmart.device import AirConditioner as AC` — constructor is positional `AC(ip, device_id, port)`. `await dev.authenticate(token, key)` (async) replaces older property-assignment patterns. Then `await dev.refresh()` and `await dev.apply()`.
- Enums: `AC.OperationalMode.{AUTO, COOL, DRY, HEAT, FAN_ONLY, SMART_DRY}`, `AC.FanSpeed.{AUTO, MAX, HIGH, MEDIUM, LOW, SILENT}`. We map our 4 fan tokens to AUTO/LOW/MEDIUM/HIGH.
- `from msmart.discover import Discover`. `Discover.discover(*, account=…, password=…, auto_connect=True)` does both LAN UDP discovery AND the cloud handshake in one call — returned `Device` objects already have `.token` / `.key` populated. **Do not call `cloud.get_token()` separately.**
- `from msmart.cloud import NetHomePlusCloud, SmartHomeCloud, BaseCloud`. The plain `Cloud` class no longer exists. We don't instantiate any of these directly — `Discover.discover()` does it for us.
- A drift sentinel: try `python3 -c "from msmart.cloud import NetHomePlusCloud"` after a version bump. If it fails, the API moved again and `setup_hvac.py` needs another patch.

## HVAC pairing (`setup_hvac.py`)
- Use the **NetHome Plus** app to pair, NOT SmartHome / MSmartHome. The SmartHome `get_token` cloud endpoint is currently broken (msmart-ng issue #201). If the user is on a SmartHome account they must re-register through NetHome Plus first.
- The script is one round-trip: prompt creds → `Discover.discover(account=, password=, auto_connect=True)` → returned device already has token+key → write `.env` → verify.
- One-time cloud roundtrip during pairing only; all subsequent control is local on TCP/6444. After pairing, the dongle's outbound internet can be blocked at the firewall without losing functionality.
- The script preserves other `.env` keys (only replaces the four `HVAC_*` lines).

## URL surface
- Heater: `?state=0|1`
- Fan: `?fan_state=0|1`, `?fan_mode=auto`
- HVAC immediate apply: `?hvac_apply=1` plus `&hvac_power=0|1&hvac_mode=…&hvac_target_f=…&hvac_fan_speed=…`. Special case: `&hvac_mode=freeze` triggers the Freeze Prevention preset and ignores the other args.
- Schedule add: `?sched_dt=YYYY-MM-DDTHH:MM&sched_device=heater|hvac` plus device-specific params (`sched_action` for heater; `sched_hvac_mode/target_f/fan_speed/power` for HVAC).
- Schedule cancel: `?cancel_id=<created_epoch>`.
- Chart range: `?range=7d|30d|YYYY-MM`.

## Chart bands, lines, and colors
- Heater band (engine-block) — `rgba(220, 53, 69, 0.25)` red
- Fan band — `rgba(13, 110, 253, 0.20)` blue
- Cold annotation (≤48°F) — `rgba(255, 152, 0, 0.15)` orange box
- Hangar temp line — `rgb(75, 192, 192)` teal (left y-axis, °F)
- Ambient line — `rgb(34, 197, 94)` green (left y-axis, °F)
- HVAC power line — `rgb(168, 85, 247)` purple (**right y-axis, Watts**)
- Range buttons: 1d (60s buckets, surfaces compressor cycles), 7d / 30d / monthly (15-min buckets).
