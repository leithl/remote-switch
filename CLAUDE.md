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
- `display.py` / `display_loop.py` / `display_state.py` — 3.5" SPI IPS dashboard (ST7796U via luma.lcd), driven by the `display.service` systemd unit; renders heater / HVAC / hangar temp / FlashAir status. Fed by `flashair.py` (reads the flashair-sync status file) and `touch.py` (FT6336U capacitive touch on I2C). **Not auto-reloaded by mod_wsgi — `sudo systemctl restart display` after changes.**
- `presence.py` / `backlight.py` — optional motion-wake: a DFRobot C4001 mmWave sensor (I2C, shares the touch bus) turns the panel backlight off when the hangar's empty, on when motion is sensed. Opt-in via `PRESENCE_ENABLED`. See "Motion-wake backlight".

## Three independent controlled devices
**Don't conflate these — they are physically and electrically separate systems.**
1. **Engine-block heater** (`config.GPIO_PIN`=17) — relay on the airplane oil-pan heater. The original purpose of this project. Toggled by the web UI's "Heater" card and `?state=` query param.
2. **Exhaust fan** (`config.FAN_GPIO_PIN`=27) — relay on a hangar exhaust fan with auto/on/off mode.
3. **Hangar HVAC** — Durastar DRAW33F2A mini-split (Midea OEM) reached over the LAN via the Midea WiFi dongle (US-OSK105) using `msmart-ng` on TCP/6444. Controlled from the "Hangar HVAC" card. Has its own scheduling path through the same scheduler. All HVAC code lives in `hvac.py`; `config.py` and the heater code never touch it.

## Motion-wake backlight (`presence.py` + `backlight.py`)
Optional, opt-in via `PRESENCE_ENABLED=1`. Turns the dashboard backlight off when the hangar's empty, on when someone's there — saves LED-backlight hours + heat on the always-on panel.
- **Sensor:** DFRobot C4001 / **SEN0610** (24GHz mmWave) on **I2C 0x2A**, sharing the FT6336U touch bus (touch is 0x38 — no clash, no extra GPIO). The `DFRobot_C4001` lib is **vendored at the repo root** (`DFRobot_C4001.py`, from [DFRobot/DFRobot_C4001](https://github.com/DFRobot/DFRobot_C4001) master, MIT) — it ships with the deploy, no separate clone/copy. It imports `serial` (pyserial) + `smbus` at the top even on the I2C path, so still `sudo apt-get install -y python3-serial python3-smbus` — without them the import fails with `No module named 'serial'` and presence falls back to backlight-always-on. (To refresh the vendored copy: re-fetch `python/raspberrypi/DFRobot_C4001.py` from upstream; presence.py uses `begin`, `set_sensor_mode`, `set_detection_range`, `set_trig/keep_sensitivity`, `set_fretting_detection`, `motion_detection`, `get_target_{number,range,speed}`.)
- **Backlight:** the panel's LED pin is moved off the always-on VCC jumper to **GPIO18** (hardware-PWM). `backlight.py` PWMs it via gpiozero (full-off when idle, short fade). If the LED pin draws >~16 mA, it needs an N-MOSFET (gate←GPIO18).
- **Logic** (in `display_loop.py`): **rolling** idle timeout — `_last_activity` resets on every presence detection OR touch; backlight sleeps after `IDLE_TIMEOUT_SECS` (default 120) with neither. Default mode `motion` (SPEED_MODE, ~12 m, covers the hangar; a motionless person reads as no-target — the timeout bridges it). `presence` mode = EXIST_MODE (~8 m, holds still presence). While asleep the loop stops pushing frames and skips the data-driven fast refresh; wake does an immediate render.
- **Tap-to-wake:** a tap on a dark screen only wakes it (swallowed); a tap on a lit screen toggles the heater as normal.
- **Fail-safe:** an unwired/failed sensor (or missing lib) → backlight forced **ON**. Lazy imports mean the display runs fine without the sensor or lib, so the feature can never leave the hangar screen dark.
- **Beam caveat:** the C4001's 12 m is a forward lobe (100°×80°, narrowing with distance) — full depth down the centerline, wide up close; a motionless person in a far corner that never crossed the beam may not register. Fine for wake-on-entry.
- **Tune/verify:** `python3 display_loop.py --presence-test` dumps detected/range/speed for 30 s. `i2cdetect -y 1` should show `2a` + `38`. Deploy needs `sudo systemctl restart display` (the display service doesn't auto-reload).
- **Over-sensitive / near-field phantoms** (a `range` that bounces under ~2 m while you're across the hangar = clutter/multipath, not a track; the C4001 ships hot and metal throws reflections): `presence.py._apply_tuning()` applies opt-in `.env` knobs after `set_sensor_mode` — `PRESENCE_RANGE_MIN/MAX/TRIG_CM` (cm; raise MIN to gate near-field, set MAX to real reach), `PRESENCE_TRIG_SENS`/`PRESENCE_KEEP_SENS` (0-9, lower trig = fewer false wakes), `PRESENCE_FRETTING` (off = ignore micro-motion). Each knob is opt-in (unset = leave the sensor's flash config alone); the C4001 lib's setters persist to the **sensor's own flash**, so a value sticks across reboots once written. The `C4001 ready (… tuning: …)` line echoes what took effect. Only `detected` drives the backlight — a wrong `range` is cosmetic, but phantom `detected` with nobody present keeps the screen awake, which these dial out.

## Storage
**Hot-path writes MUST go to `/run` (tmpfs / RAM), not `/var/lib/heater` (SD card).** SD cards on a Pi Zero W have a limited write-cycle budget; this app runs every minute for years. Any new feature that writes more often than ~once a week belongs in `/run`. The accepted tradeoff: anything in `/run` is lost on unplanned power loss — for this hangar that's fine because the disk DB has data up to the last weekly flush, and the `heater-flush.service` systemd unit catches commanded reboots/shutdowns.

Files:
- `/run/heater.db` — RAM (tmpfs). **Only holds `readings` table.** Written every minute by cron.
- `/run/heater-ambient.tmp` — Ambient temp cache, 15-min TTL, ~50 bytes. Written ~96×/day.
- `/run/heater-hvac.json` — Cached HVAC dongle state (reported + commanded views), 30s TTL, ~400 bytes. Written every minute by `log_temp.py` (via `hvac.state_for_log()`) and on every HVAC apply.
- `/run/heater-flashair.json` — flashair-sync status (last sync epoch + file count + transferring flag), ~200 bytes. **Written by the flashair-sync daemon** (separate systemd service running as `pi` on this same Pi) on every sync state change — not by `log_temp.py`. Read by `switch.py` at page-render time. The "opt-in" knob is just whether flashair-sync is running on this host: file present → UI line appears; file absent → UI line hidden. Surfaces "FlashAir: N files, X ago" in the chart-card header.
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
- `readings.ac_power_w REAL` — instantaneous power draw from the dongle's energy meter, polled every minute, **BINARY-decoded** (see below for why). Idle baseline on the Durastar DRAW33F2A is ~146W (controller + indoor fan + standby); the compressor pushes it well past 200W when actively heating. The chart filters anything ≤ 200W as "off" (see `POWER_ON_THRESHOLD_W` below).
- `readings.ac_total_kwh REAL` — cumulative kWh since unit install (Nov 2025), **BINARY-decoded** then divided by 10 to convert deci-kWh → kWh. Monotonically increasing. The counter lives in the AC unit (not the dongle — survives dongle reboots/re-pairing). Subtract first/last in a window to compute kWh used.
  - **Why BINARY, not BCD (resolved 2026-05-08).** msmart-ng's `dev.get_total_energy_usage()` defaults to BCD-decoding. On this Durastar that produces artifacts: every ~65 minutes the value spuriously drops by 0.05 kWh, and every ~17 hours it loses 0.64 kWh. Both are signatures of msmart-ng's `decode_bcd()` being applied to a binary-incrementing byte (the low nibble rolls past 0xF and BCD output drops by 5; ditto for 0x?F/0x?A nibble pairs). Internal-consistency check on 2026-05-08 settled it: over 25h, BCD delta was 0.88 kWh (35W average) — contradicting BCD's own real-time power readout of 61.4W; BINARY delta was 3.4 kWh (136W average) — matching BINARY's real-time power of 146W within 7%. msmart-ng [issue #154](https://github.com/mill1000/midea-msmart/issues/154) documents that format is device-dependent. Keep using BINARY here unless a future msmart-ng upgrade changes the decode behavior.
  - **How verified.** Two-step: (1) `dev.refresh()` returns both formats — pick whichever matches a known reference. (2) Sample over 24h of idle: BINARY's accumulation rate should match BINARY's instantaneous rtp. If both checks line up, you have the right format.
  - **Note on `temp_c` vs unit thermistor:** Pi DS18B20 sits at ~4 ft; indoor-unit thermistor (the FP regulator's input, exposed as `ac_indoor_f` after PR #14) at ~12 ft. Vertical stratification means the Pi reads a few °F cooler than the unit's setpoint sees, so don't infer compressor activity from `temp_c` alone.
- `readings.ac_indoor_f REAL` — the indoor unit's own thermistor reading, in °F (same field shown on the web UI). The unit is ceiling-mounted at ~12 ft; this is what FP's regulator sees and acts on. Compare to the floor-level DS18B20 in `temp_c` (~4 ft) — the gap is the vertical-stratification signal, which should widen during compressor heat output and narrow when the unit is idle.
- `aggregate.compute_bucketed` averages `ac_power_w` per bucket and renders it as a Watts line on the chart's right y-axis (replacing the previous binary HVAC band). Buckets at or below `aggregate.POWER_ON_THRESHOLD_W` (=200) are filtered out — the ~146W standby baseline (controller + indoor fan + dongle) is treated as "off" and produces no point, so the chart only shows actual heating/cooling activity. The 200W threshold gives ~50W margin above idle. Pre-2026-05-07 rows have NULL `ac_power_w`; pre-2026-05-08 rows have BCD-decoded values and were migrated in-place (see `scripts/migrate-bcd-to-binary.py`). The `ac_state` column is still logged but no longer drives any chart logic.
- **Energy row in the monthly summary table (added 2026-06).** Runtime group shows per-month kWh = heater + HVAC. Heater: on-minutes × `aggregate.HEATER_POWER_W` (180W fixed resistive load — no meter, the constant is the whole calculation). HVAC: sum of per-minute `ac_power_w` for samples above `POWER_ON_THRESHOLD_W` — full draw of active minutes, idle/standby contributes nothing ("actual usage", not the meter's lifetime total; inherits the open W-vs-VA caveat below). Live months compute in SQL (`config.query_batch_stats`); past months read `monthly_cache`. Backfill rules for cached months:
  - **2026-05 (one-time deploy step):** `sudo python3 log_temp.py rollup 2026-05` — its cache was rolled up by the old code, so the Energy row under-reports by the ~11.5 kWh of measured May HVAC work until this runs. Explicit-month rollup skips the summary email, refuses the current/future month (a partial snapshot would permanently shadow live data once the month rolls over), and refuses months with no readings (protects against typos clobbering good entries).
  - **Months before 2026-05: do NOT re-roll.** `ac_power_w` is NULL pre-2026-05-07, so a re-roll can never add `hvac_kwh` — and it would *erase* the legacy `hvac_on_hrs` from the cache. These months render with a `*` on the Energy row (tooltip: heater only, HVAC unmeasured); the winter FP compressor work (~960 kWh lifetime estimate) is simply not in their totals. The unmetered window (2025-11 through 2026-04) is pinned as `ENERGY_UNMEASURED_*_MK` in `switch.py` — neither cached stats nor the live SQL carries any HVAC signal for those months, so the marker can't be data-driven.
  - **The monthly cron self-heals cache holes (added 2026-06).** After rolling the previous month, `do_rollup` backfills every uncached older month in the 13-month window — empty months get their all-None stats cached too (renders as "no data", but the entry's existence keeps the stats table's live batch-scan window at just the current month; an uncached month pins the window and silently reintroduces the ~13s full-table scan). It only fills holes (`INSERT OR IGNORE`), never overwrites, so the do-NOT-re-roll rule above is unaffected.
  - Seasonal note: idle draw reads ~21W in June 2026 vs the winter-long flat 146W, so the "idle baseline" varies; the 200W threshold clears both.

## Thermal analysis tools (established 2026-05-07, refined 2026-05-08)

Reusable findings from the `ac_total_kwh` investigation. Save future-you the half-day of dead ends — these are direct measurements from the data on this hangar:

- **Hangar τ ≈ 12.4 h** (median of 73 nighttime cooling episodes; mean 14.0, IQR 6.7–18.5). Direct measurement, not regression. Pick 4-hour windows in 22:00–06:00 MST with hangar > 50°F and monotonic drop, compute `τ_implied = mean(hangar−ambient) / drop_rate_°F_per_h`.
- **Natural floor ≈ 43°F at any ambient ≤ 30°F** (Pi-level / floor-level). Concrete-slab-on-grade ground-couples the hangar to the deep-ground temp (~50°F in Colorado, attenuated by the slab interface). Verify by bucketing all data by ambient and reading `temp_c`'s 5th percentile per bucket — the floor flattens around 43°F instead of tracking ambient. Unit-level (`ac_indoor_f`) probably reads a few °F warmer when the heat pump is working.
- **Idle electrical baseline ≈ 146W** at the dongle (BINARY rtp during FP-idle on 2026-05-08). Self-consistent inside the dataset: 1,393 consecutive per-minute samples since the BINARY fix landed (PR #19) all read **exactly 146.0 W**, and `ac_total_kwh` increments at that rate within ±5% (kWh quantization is 0.1, so per-hour deltas alternate 0.140/0.150). Lifetime math closes if 146W is real: 146W × ~4500h since 2025-11-01 install = ~657 kWh idle, leaving ~960 kWh of compressor work in the 1,617 kWh BINARY lifetime reading — plausible for FP-always-on through Colorado winter.
  - **Caveat — likely *apparent* power (VA), not real (W). Open as of 2026-05-08.** The 23.6 h of perfectly-flat 146.0 W with zero variance — through outdoor swings of 6→21 °C and indoor 10→23 °C, with the unit's thermistor sitting 55–83 °F (well above the ~46 °F IR-FP setpoint this Durastar actually regulates to, see "HVAC FP setpoint mystery") — is suspicious. Real inverter idle should fluctuate as the indoor blower PWMs and the controller polls; conditions don't call for compressor, base-pan, or crankcase heat (outdoor never < 6 °C). Industry references put a controller + dongle + slow indoor blower at 5–50 W real; +30–50 W if a crankcase heater fires; +70–120 W if a base-pan heater fires below freezing. None of those should be active. The dongle is plausibly reporting V × Irms with no power-factor correction — a $30 IoT meter is unlikely to track PF, and a [documented Mitsubishi case](https://www.greenbuildingadvisor.com/question/mitsubishi-mini-split-phantom-draw) showed a meter reading 260 W when real consumption was 50–65 W. The 2.378× ratio between BINARY and BCD decodes of the rtp byte is **not** a PF signal — it's just what `decode_bcd()` produces from a binary-encoded byte where one nibble is 0xB > 9. If 146 W is VA: `ac_power_w` and `ac_total_kwh` are both inflated 1.5–3× relative to billable kWh; the 200 W chart-filter threshold still works (real compressor work pushes well past it either way); kWh-delta thermal analysis in matched windows is unaffected (the inflation cancels).
  - **Decisive verification (at-hangar, deferred).** Any one of: (1) Clip a true-RMS Kill-A-Watt on the indoor unit's mains cord for 10 min during confirmed idle. If it reads 30–80 W while the dongle says 146 W, the dongle is VA. (2) Read the utility bill: 146 W × 4500 h since install = 657 kWh just idle = ~$79 at $0.12/kWh. If actual marginal cost from this unit is closer to $25–30, dongle is inflated ~3×. (3) Whole-house monitor / smart panel: directly compare measured W to dongle during a confirmed-idle window.
- **Vertical stratification matters.** Pi DS18B20 at ~4 ft; `ac_indoor_f` (the FP regulator's input) at ~12 ft. Gap widens during heat output, narrows when idle.
- **Hangar-door-open detection (validated 2026-05-08).** A door opening is a textbook stratification event in real time. On 2026-05-08 a hangar mate rolled out his plane around 07:49: the floor-level Pi dropped 2.7 °F (52.9 → 50.2 °F) over 9 min while `ac_indoor_f` (12-ft ceiling) **stayed flat at 56.3 °F the entire time**, only starting to rise 25 min later as floor-cold air finally mixed up. Cold dense air dumps in at floor level when the door cracks; buoyancy keeps the warm ceiling air pooled high. After the door closed, recovery on the Pi (6.5 °F in 60 min) was much faster than natural reheat could explain at 146 W idle and τ ≈ 12 h — the recovery was *mixing* (warm ceiling air re-equilibrating downward), not heat input. **Summer signature flips** because hot air rises: hot outdoor air pools at the ceiling first, so `ac_indoor_f` rises while the Pi stays cool. Universal detection signal: `s = |Δfloor| - |Δceiling|` over a 5-min sliding window. HVAC compressor *heat* has the *opposite* signature (ceiling leads floor) and is naturally rejected by the absolute-value form — but **HVAC *cooling* is NOT**: the ceiling head dumps cold air that sinks to the floor, so the Pi drops while the unit thermistor stays flat — the exact floor-leads-ceiling signature of a cold-air door event (confirmed false positive 2026-06-17). So the abs() form alone is insufficient. **Fix (2026-06-17):** `detect_door_events()` now also takes per-minute `ac_power_w` (via `config.query_temp_pairs`, now a 4-tuple) and suppresses any window where the compressor drew > `POWER_ON_THRESHOLD_W` (200 W) at any minute — drops both cooling false-positives and any heat leakage, regardless of direction. A genuine door opening *during* active HVAC is ambiguous and gets dropped too (acceptable: better than false-flagging). NULL/absent power (pre-2026-05-07 rows, legacy 3-tuple callers) counts as "off" → detection unchanged for that data. See `aggregate.detect_door_events()`.
- **Single-input Newton's-law regression fails** (R² ≈ 0.025) because the relevant boundary condition is air+ground blend, not air. **Don't waste time refitting that model** — use the empirical-cooling-rate approach instead.

The original BCD-vs-BINARY format question on `ac_total_kwh` is **resolved**, but a new W-vs-VA question opened on 2026-05-08 — see the "Caveat" bullet above. Two open questions remain:

- **Apparent vs real power on the dongle's energy meter** — deferred to a hangar visit. See the "Decisive verification" sub-bullet above for the three possible measurement protocols.
- **Exhaust fan effectiveness on hot days** — deferred. Once the hangar regularly hits the 80°F fan-on threshold (`config.FAN_TEMP_THRESHOLD_C`), pull paired (fan-on, fan-off) windows at matched ambient and compare `temp_c` cooling rate. Same methodology, no new logging needed.

## Schedules
- Stored in **disk DB only** — survive reboots with no extra effort.
- Executed by the every-minute cron job via `execute_epoch <= now`, so any schedule missed during downtime fires on the first tick after boot. No catch-up logic needed.

## Key patterns
- `_respond()` and `_redirect()` raise `_Response(Exception)` — never put these inside a bare `except Exception` block.
- SQL aggregation (`query_bucketed`, `query_batch_stats`) used instead of Python loops — Pi Zero W is slow.
- METAR/TAF fetched server-side (`?metar=1`, `?taf=1`) to avoid CORS issues with aviationweather.gov.
- `LOCATION` in `.env` can be an ICAO code (e.g. `KLMO`); lat/lon resolved via OurAirports CSV geocoding on first run.
- mod_wsgi daemon mode auto-reloads when **`switch.py`** changes. `git pull` is sufficient if the change is in `switch.py` itself; for changes to imported modules (`aggregate.py`, `hvac.py`, `config.py`), `git pull` updates the file on disk but mod_wsgi keeps the previously-imported module in memory until `switch.py`'s mtime changes. So after pulling a non-`switch.py` change, also do `touch switch.py` to force a daemon reload. (Symptom of forgetting: deployed code looks right on disk but the WSGI app behaves like it did before the pull. Verified 2026-05-08 with PR #19's `aggregate.py` threshold change.)
- All schema additions use `ALTER TABLE ... ADD COLUMN` wrapped in try/except in `get_db()` / `get_ram_db()`. Don't introduce a separate migration system.
- **Main-page TTFB budget (~0.5s on the Pi Zero W, fixed 2026-06-12 from 2.4s).** The temperature display reads the latest cron-logged row (`config.read_temp_cached`, falls back to a live probe read if logging stalls >5 min) — a live DS18B20 conversion blocks ~850ms, so don't put `read_temp()` back on the render path. Anything else slow belongs behind a lazy fragment endpoint (see "URL surface"), the established pattern for monthly stats, HVAC card, and door events.

## WiFi bridge (`scripts/setup-wifi-bridge.sh`)
- **Purpose: pairing only.** The Midea dongle's pairing app (NetHome Plus) refuses to pair against an open SSID. Many hangar WiFi networks are open. The bridge lets the Pi broadcast its own WPA2 SSID for the dongle to live on, while the Pi stays a client on the open hangar WiFi and NATs the dongle's traffic out.
- **The dongle's permanent home is the Pi's AP**, not the hangar WiFi. After the one-time cloud handshake (driven by `setup_hvac.py`), the dongle never needs the internet again — `msmart-ng` reaches it locally on the AP subnet at TCP/6444.
- **Single-radio AP+STA shares one channel.** `AP_CHAN` in the install script must match the hangar WiFi's current channel; hostapd refuses to start on a mismatch with "Could not set channel". If the hangar router roams channels, pin it.
- **Files written by the script:**
  - `/etc/systemd/system/uap0.service` — creates the `uap0` virtual __ap interface on `wlan0`, gives it `192.168.50.1/24`, then runs `ufw reload` via `ExecStartPost` so the persisted uap0 ALLOW lands in the live iptables chain after the interface exists (see UFW trap bullet)
  - `/etc/hostapd/hostapd.conf` — WPA2-PSK on `uap0`, channel hard-coded, `logger_*_level=4` (warning+) to suppress per-association info noise as a defense against any flap-driven assoc/disassoc spam filling log2ram's 128M tmpfs. The original 2026-05-09 trigger was the UFW DHCP-block trap below — the "keepalive" attribution from that morning was wrong, but the hostapd quiet stays as a useful backstop.
  - `/etc/dnsmasq.d/uap0.conf` — DHCP `192.168.50.50–.150` on `uap0` only (`bind-interfaces` keeps it off `wlan0`)
  - `/etc/NetworkManager/conf.d/uap0-unmanaged.conf` (Bookworm) or `denyinterfaces uap0` in `/etc/dhcpcd.conf` (Bullseye) — keeps the system network manager from fighting hostapd over `uap0`
  - iptables MASQUERADE on `wlan0` + FORWARD rules, persisted via `iptables-persistent` / `netfilter-persistent`
  - `ufw allow in on uap0` + `ufw reload` if UFW is active — without this UFW's default `after.rules` silently drops DHCP from the dongle (see UFW trap bullet)
- **Single dongle, single client.** Don't optimize this for many clients — there's exactly one (the Midea). Throughput is irrelevant; reliability over months is what matters.
- **BCM43438 AP+STA can wedge.** Symptom: dongle drops off, hostapd looks healthy but new associations fail. Recovery: `sudo systemctl restart hostapd`. If this becomes routine, the answer is a USB WiFi dongle (e.g. TL-WN725N) so AP and STA live on separate radios — not a watchdog. A watchdog is fine as a stopgap but masks the real issue.
- **UFW silently drops DHCP server traffic on UDP/67 by default. The single most misleading failure mode for "dongle keeps disassociating every ~30s" reports — burned a full day chasing it on 2026-05-09.** UFW's stock `/etc/ufw/after.rules` jumps inbound UDP/67 to `ufw-skip-to-policy-input → DROP` to suppress nuisance broadcast spam, with no logging at default UFW levels. A `ufw allow in on uap0` rule WILL save you (because `ufw-user-input` runs *before* `ufw-after-input` in chain order), **but only if it's actually loaded into the live iptables** — the persisted entry in `/etc/ufw/user.rules` can drift out of `ufw-user-input` (e.g., after a UFW package upgrade, or if uap0 didn't exist when ufw first loaded), and `ufw status` will lie that the rule is active because it reads from disk, not from the kernel.
  - **Symptom signature, all simultaneous:** hostapd cycles the dongle every ~34s (`disassociated` → 3s gap → `associated` → 31s connected — that 31s is the dongle's `1+2+4+8+16s` DHCPDISCOVER backoff timing out); `/var/lib/misc/dnsmasq.leases` empty; `ip neigh show dev uap0` says `INCOMPLETE` or `FAILED` for the dongle's IP; `iw dev uap0 station dump` shows `tx bitrate: 1.0 MBit/s` (PHY-rate floor for an unauthenticated client — once DHCP works it jumps to 50–70 MBit/s).
  - **One-command diagnostic:** `sudo iptables -L ufw-after-input -nvx | grep "udp dpt:67"` — if its `pkts` counter is climbing into the thousands per hour, it's this trap.
  - **Fix:** `sudo ufw reload` re-applies `user.rules` so the uap0 ALLOW lands in `ufw-user-input` ahead of the silent drop. The setup script does this at install time, AND `uap0.service` does it on every boot via `ExecStartPost=-/usr/sbin/ufw reload` — closes the boot-order resurfacing where `ufw.service` had started before `uap0` existed, causing iptables to silently drop the rule at load time. For a recurrence outside the boot path, just re-run `ufw reload` manually.
  - **Trap inside the trap — DO NOT debug `dnsmasq` first.** With UFW dropping the packets in `INPUT`, `dnsmasq` doesn't even SEE the DHCPDISCOVERs (they're killed before its UDP socket), so the journal stays silent *even with `log-dhcp` on in `/etc/dnsmasq.d/uap0.conf`*. That silence is the most misleading symptom in this stack — it makes you think the dongle isn't sending DHCP. It IS. The truth is on the wire: `sudo tcpdump -i uap0 -nn -vv "port 67 or port 68"` shows DISCOVERs every ~1–16s with no OFFERs coming back.
  - **Most-misleading red herring:** the dongle looks "wedged and needing a power cycle" but isn't — `iw event -t -f` shows clean `new station` / `del station` pairs every cycle, dongle re-associates every time. We chased "BCM43438 wedge", "dongle keepalive timeout", "Midea cloud heartbeat retry", and "hostapd inactivity poll" before getting to the actual cause. None of those theories survive a `tcpdump` on `uap0` — that's the diagnostic that cuts through everything.
- **Don't add a second AP for clients.** This bridge exists to pair an IoT device, not to serve users. Adding clients re-introduces all the throughput and channel-sharing issues we accept here.

## Diagnosing "dongle unreachable" / HVAC outages — READ THIS BEFORE PLANNING A HANGAR TRIP

The dongle being physically broken or "stuck and needing a power cycle" has **never been the actual cause** in this project's history. Every reachability outage so far has been Pi-side (UFW DHCP block) or pairing-side (token/cloud handshake). The dongle has self-recovered from every condition we've ever observed. A misdiagnosis here costs a 1-hour drive *and* leaves the hangar HVAC unmanaged in the meantime — so the bar for "the dongle is the problem" must be high.

**Default assumption: the dongle is fine.** Run the playbook below before ever scheduling a trip. If the playbook is inconclusive, run it again 30 minutes later — most "stuck" symptoms unstick themselves while you're typing.

**Pre-action diagnostic playbook.** Run in order. If a step shows the dongle reachable on its layer, the problem is above that layer; skip ahead.

1. **Radio (L1/L2): is the dongle associated to uap0?** `sudo iw dev uap0 station dump`
   - Station entry, `authorized: yes`, `tx bitrate > 10 MBit/s` → radio is healthy → step 3
   - Station entry but `tx bitrate: 1.0 MBit/s` → "associated but L3 broken" — the UFW trap signature; go to step 2
   - No station entry, repeated cycle in `iw event -t` → check hostapd, channel, signal
2. **L3: does the dongle have an IP?** `sudo cat /var/lib/misc/dnsmasq.leases`
   - Lease present → step 3
   - Empty → run `sudo iptables -L ufw-after-input -nvx | grep "udp dpt:67"`. Counter > 0 means the UFW trap is firing — fix with `sudo ufw reload` (see UFW trap bullet above). `uap0.service` runs this at boot via `ExecStartPost`, so a fresh-boot recurrence means the persisted rule in `/etc/ufw/user.rules` is itself missing — check that before re-adding with `ufw allow in on uap0`. If counter is 0, capture `tcpdump -i uap0 -nn -vv "port 67 or port 68"` for 60s and read whichever side of the conversation is missing.
3. **IP reachability:** `ping -c2 192.168.50.<lease_ip>` then `timeout 3 bash -c "exec 3<>/dev/tcp/192.168.50.<lease_ip>/6444"`
   - Both succeed → step 4
   - Ping fails despite a lease → dongle in deep power-save; `sudo systemctl restart hostapd` to force re-association
4. **App layer:** `sudo cat /run/heater-hvac.json` — if `epoch` is fresh (within last ~120s) and `power_w` is updating, msmart-ng is working and the issue is elsewhere (UI? scheduler? web app cache?). If stale, the dongle's auth token may have expired or the cloud handshake needs redoing — re-run `setup_hvac.py`.

**The one diagnostic that cuts through everything before doing the playbook:** `sudo tcpdump -i uap0 -nn -vv "port 67 or port 68"` for 60s.
- DHCPDISCOVERs out, no OFFER back → UFW trap (or dnsmasq down)
- DHCPACK flowing but TCP/6444 still failing later → msmart-ng/auth issue, NOT the dongle
- No DHCPDISCOVERs at all → radio/hostapd/dongle issue (and only this case justifies considering hardware)

**Anti-patterns to skip.** We have burned time on each of these:
- *"Power cycle the dongle"* — never been the right answer; the dongle self-recovers from everything we've seen. Last resort, only after the playbook above is exhausted AND a 30-min recheck still shows the same signature.
- *"Restart hostapd as a first move"* — fixes only the BCM43438 AP+STA wedge, which is rare and we've never confirmed in the wild. Skip unless `iw event -t` shows associations *failing*, not just cycling.
- *"Read dnsmasq journal first"* — useless for the UFW trap because dnsmasq doesn't see the dropped packets. `tcpdump` shows truth; dnsmasq doesn't. Don't enable `log-dhcp` and wait for it to tell you what's wrong — it will silently lie.
- *"Read hostapd debug"* — hostapd accurately logs assoc/disassoc events but says nothing about *why* the dongle disassocs. Don't camp there.
- *"Schedule a hangar trip"* — never the next step after a single failed poll. The playbook is ~5 minutes of typing; the trip is an hour each way.

**The 2026-05-09 incident in one sentence so future-us has a reference point:** dongle appeared "wedged and reconnecting every 30s, needs power cycle"; actual cause was UFW silently dropping the dongle's DHCPDISCOVERs (`ufw-after-input` rule on `udp dpt:67`); fix was `ufw reload`; misdiagnosis would have cost a hangar trip to power-cycle a perfectly healthy dongle.

**The 2026-05-20 incident in one sentence:** after an unplanned Pi reboot the same trap resurfaced because `ufw.service` had loaded `/etc/ufw/user.rules` before `uap0.service` created the interface, and iptables silently drops rules naming a missing interface at load time — so the persisted ALLOW never reached `ufw-user-input`; `sudo ufw reload` post-boot fixed it (rule loaded, dongle jumped from 1.0 → 72.2 MBit/s within ~16s); permanent fix added to `uap0.service` as `ExecStartPost=-/usr/sbin/ufw reload` so the reload runs automatically once `uap0` exists.

## Diagnosing "HVAC Apply doesn't stick" / UI shows stale state after clicking Apply

Form submits, browser redirects, status card stays the same. The dongle and msmart-ng are almost certainly fine — the unit DID switch. The bug is cache visibility, and it has one classic cause on this stack.

**One-command diagnostic:** `sudo ls -la /run/heater-hvac.json`
- `root:www-data` mode 664 → both sides can write → bug not present, look elsewhere
- `root:root` mode 664 → WSGI (www-data) can't update the cache → bug present

**Cross-check the unit actually changed mode:** the per-minute readings in `/run/heater.db` are written by `log_temp.py` as root and don't suffer this bug.
```bash
sudo python3 -c "import sqlite3; \
  [print(r) for r in sqlite3.connect('/run/heater.db').execute( \
   \"SELECT datetime(epoch,'unixepoch','-6 hours'), ac_state, ac_power_w \
    FROM readings ORDER BY epoch DESC LIMIT 10\")]"
```
If `ac_state` flipped (1=heat/freeze → 2=cool, etc.) within 1–2 minutes of the Apply click, the unit accepted the change and only the cache is wrong.

**Mechanism.** `log_temp.py` runs as root via cron, `switch.py` runs as `www-data` via mod_wsgi. Both write `/run/heater-hvac.json`. If root creates the file first at boot, it's `root:root` mode 664 and `www-data` has no group write. Every WSGI `set_state` after that sends the SetState to the dongle successfully *(the unit DOES switch)* but `_write_cache` hits a `PermissionError` that the inner `try/except OSError` swallows. The cache then keeps showing the previous root poll until the next cron tick (≤60s) reads the unit and overwrites with the truth — and the `commanded` view (which only `set_state` writes) stays `null` indefinitely.

**Fix.** `hvac.py:_write_cache` chowns the file to `:www-data` after every successful write so both sides can write. The file lives in tmpfs so the bug returns on every reboot until the fix is deployed. One-shot manual recovery on a stuck Pi: `sudo chown root:www-data /run/heater-hvac.json` (good until next reboot).

**Anti-patterns to skip.** Each of these was chased before getting to the cache-perms answer:
- *"The dongle is rejecting the FP→Cool transition"* — easy to suspect because LAN-FP is known to be quirky on this Durastar. But the per-minute log proves the unit transitioned; the cross-check above settles it in seconds.
- *"`set_state` is exception-failing silently"* — the outer `except Exception: return False` only catches `_apply()` errors. `_write_cache`'s `OSError` is caught *inside* the function and never reaches the outer handler, so the function returns `True` while the cache is unchanged.
- *"User is clicking Freeze Prevention preset by accident"* — Apache access log disambiguates instantly: grep for `hvac_apply=1` and read the actual query string.

**The 2026-05-15 incident in one sentence:** Apply (Cool/72°F) clicked 3 times, UI kept showing Freeze/61°F; per-minute log proved the unit transitioned to Cool within 30s of the first click; cache was owned `root:root` so WSGI couldn't update it; fix was a chown in `_write_cache`.

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
- **Stable mode/fan tokens** (used in `.env`, schedule `params` JSON, `?hvac_*=` query strings, and chart logic): `MODE_AUTO/COOL/DRY/HEAT/FAN/FREEZE` and `FAN_AUTO/LOW/MED/HIGH`. Don't add or rename without checking all four call sites. **Turbo is NOT a mode token** — it's a separate boolean modifier on `set_state(turbo=…)` / the `hvac_turbo` checkbox, orthogonal to mode; don't fold it into `ALL_MODES`.
- **Freeze Prevention — LAN flips the flag, not the regulator.** `MODE_FREEZE` maps to `dev.freeze_protection = True` over the LAN protocol. On this Durastar that turns on the unit's "FP" display, but the indoor target settles at the dongle-reported `min_target_temperature` (16°C / ~60°F) — NOT the 46°F regulator the IR remote engages with "down twice from 60°F". Same flag, different setpoints. The mechanism (or absence) of a LAN path to the 46°F regulator is an open question — see [docs/hvac-fp-investigation.md](docs/hvac-fp-investigation.md) for the in-person test plan. Setting any other explicit `mode` automatically clears the freeze flag — exit FP whenever the user picks a different mode. Capability flags (`dev.supports_freeze_protection`) are unreliable on some units (msmart-ng issue #76); we always send the SetState bit regardless. The state byte is `payload[21] bit 0x80` in msmart-ng's SetState/StateResponse if you ever need to debug at the wire level.
- **Turbo (the IR remote's "blast max output" button).** `set_state(turbo=…)` is tri-state (True/False/None); it maps to `dev.turbo` (older msmart-ng: `dev.turbo_mode` — resolved defensively by `_turbo_attr()`, prefers `turbo`). Sent like `freeze_protection`: always send the bit, ignore the unreliable `supports_turbo` flag. **Turbo persists until explicitly cleared** on this Durastar (observed at the hangar 2026-06-15 — it did NOT auto-drop after ~15 min, disproving the earlier assumption). Because it persists, turbo **IS** part of `_diverged()` drift detection (bool-coerced both sides so a missing/None key can't trigger spurious drift) — an IR-remote turbo toggle now raises the "physical remote may have been used" warning, and `_hvac_card.html` names `+ Turbo` in that line. Still NOT logged per-minute (`state_for_log()` unchanged) — it's a momentary user action, not a sampled signal. Cleared (forced None) in the Freeze Prevention branch — meaningless in FP. Surfaced in `_device_to_dict()` as `turbo` so the Apply checkbox pre-checks and the status line shows it.
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
- HVAC immediate apply: `?hvac_apply=1` plus `&hvac_power=0|1&hvac_mode=…&hvac_target_f=…&hvac_fan_speed=…&hvac_turbo=0|1`. Special case: `&hvac_mode=freeze` triggers the Freeze Prevention preset and ignores the other args (turbo included). `hvac_turbo` is the Apply form's Turbo checkbox; an unchecked box submits nothing, so absent → off.
- Schedule add: `?sched_dt=YYYY-MM-DDTHH:MM&sched_device=heater|hvac` plus device-specific params (`sched_action` for heater; `sched_hvac_mode/target_f/fan_speed/power` for HVAC).
- Schedule cancel: `?cancel_id=<created_epoch>`.
- Chart range: `?range=1d|7d|30d|YYYY-MM`.
- Lazy fragments (fetched by page JS after load, kept off the main TTFB): `?monthly_stats=1` (~1s SQL scan), `?hvac_state=1` (dongle round-trip), `?door_events=1&range=1d|7d` (~0.6s raw-row scan).

## Chart bands, lines, and colors
- Heater band (engine-block) — `rgba(220, 53, 69, 0.25)` red
- Fan band — `rgba(13, 110, 253, 0.20)` blue
- Cold annotation (≤48°F) — `rgba(255, 152, 0, 0.15)` orange box
- Door-open marker — `rgba(219, 39, 119, 0.55)` fuchsia bar (distinct from the orange cold annotation); computed by `aggregate.detect_door_events()` from raw per-minute rows (1d/7d only — 30d/monthly skipped because events would render as overlapping pixels). Lazy-loaded via `?door_events=1` and painted in after the chart renders — the 10k-row fetch costs ~0.6s on the Pi Zero W, so it's off the main TTFB (SQL window functions were measured *slower* than the Python scan on this CPU; don't re-try that). Tooltip shows `Door event (cold air in / warm air in, N°F peak)`.
- Hangar temp line — `rgb(75, 192, 192)` teal (left y-axis, °F)
- Ambient line — `rgb(34, 197, 94)` green (left y-axis, °F)
- HVAC power line — `rgb(168, 85, 247)` purple (**right y-axis, Watts**)
- Range buttons: 1d (60s buckets, surfaces compressor cycles), 7d / 30d / monthly (15-min buckets).
