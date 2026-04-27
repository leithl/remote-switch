# remote-switch
Raspberry Pi airplane hangar controller. Started as a one-relay remote for the airplane's engine-block oil-pan heater; now also runs an exhaust fan and a Durastar/Midea mini-split HVAC over the LAN — all from a single Pi Zero W on hangar WiFi (with LTE backup).

The web UI shows current state of all three devices, a 7d/30d/monthly temperature chart with colored bands per device, and a one-shot scheduler that can drive any of them.

## Equipment

Links provided for your convenience, but buy from wherever you prefer.

* [Raspberry Pi Zero W](https://www.raspberrypi.com/products/raspberry-pi-zero-w/)
  * You'll need a microSD card if you don't have one. 4GB+ is enough.
* [SIM7600 LTE modem HAT for Pi](https://www.waveshare.com/sim7600a-h-4g-hat.htm) also available on [Amazon](https://www.amazon.com/SIM7600A-H-4G-HAT-Communication-Positioning/dp/B082WH85WV/)
  * You'll need a SIM card. The docs say nano but the unit I had uses a mini SIM slot. I used a Google Fi SIM since it only costs data on my existing plan.
  * Skip this if you already have reliable WiFi at the hangar.
* [Digital Loggers IoT relay](https://dlidirect.com/products/iot-power-relay)
  * Connect `-` to GND on the Pi, and `+` to an unused GPIO pin.
  * Two of these for the engine-block heater (GPIO 17) and exhaust fan (GPIO 27).
* (optional) [DS18B20 temperature probe](https://www.adafruit.com/product/381) — displays temperature on the control page and drives fan auto-mode
  * You'll also need a 4.7kΩ resistor between the data and power lines.
* (optional) Midea **US-OSK105** WiFi USB dongle (~$30 on Amazon as ASIN B0GVSPFK1P) — required only if you want to control a Durastar or other Midea-OEM mini-split. See [Hangar HVAC](#optional-hangar-hvac-durastar-mini-split) below.

## Setup

### 1. Raspberry Pi OS

Install [Raspberry Pi OS Lite](https://www.raspberrypi.com/software/operating-systems/) and configure the following.

Add to `/boot/config.txt`:

```
# https://forums.raspberrypi.com/viewtopic.php?f=117&t=208748
# set GPIO pin 17 as output, default low (heater relay)
gpio=17=op,dl

# exhaust fan relay — must match FAN_GPIO_PIN in config.py
gpio=27=op,dl

# (optional) enable 1-wire for DS18B20 temp probe
dtoverlay=w1-gpio
```

Add to `/etc/rc.local` (before `exit 0`):

```
echo "17" > /sys/class/gpio/export
echo "27" > /sys/class/gpio/export
```

### 2. LTE Modem

Get your LTE modem running. The [manufacturer's documentation](https://www.waveshare.com/wiki/SIM7600A-H_4G_HAT) is detailed but may require some trial and error with APN settings.

### 3. Web Server (Apache)

Install Apache and add `www-data` to the `gpio` group so the web UI can toggle the relay:

```bash
sudo apt install apache2
sudo usermod -a -G gpio www-data
```

Enable CGI and mod_wsgi:

```bash
sudo apt install python3-jinja2 libapache2-mod-wsgi-py3
sudo a2enmod cgi wsgi
```

Create the site config for mod_wsgi (keeps Python alive between requests for fast page loads):

```bash
sudo tee /etc/apache2/conf-available/heater.conf << 'EOF'
WSGIDaemonProcess heater python-path=/usr/lib/cgi-bin/remote-switch processes=1 threads=2 display-name=heater
WSGIScriptAlias /cgi-bin/remote-switch/switch.py /usr/lib/cgi-bin/remote-switch/switch.py
<Directory /usr/lib/cgi-bin/remote-switch>
    WSGIProcessGroup heater
    WSGIApplicationGroup %{GLOBAL}
    Require all granted
</Directory>
EOF
sudo a2enconf heater
sudo systemctl restart apache2
```

### 4. Deploy Files

Clone the repository into your cgi-bin directory:

```bash
sudo git clone https://github.com/leithl/remote-switch.git /usr/lib/cgi-bin/remote-switch
```

Future updates are then just `sudo git pull` from that directory.

### 5. Configuration

Edit `/usr/lib/cgi-bin/remote-switch/config.py` for hardware settings:

```python
GPIO_PIN    = "17"    # GPIO pin connected to your relay
ENABLE_TEMP = True    # set to False to disable all temperature features
```

### 6. Disk Storage Permissions

The logger writes to SQLite on disk at `/var/lib/heater/heater.db`. The directory needs to be writable by both root (cron) and `www-data` (Apache):

```bash
sudo mkdir -p /var/lib/heater
sudo chown root:www-data /var/lib/heater
sudo chmod 2775 /var/lib/heater
```

The database file is created automatically on the first cron run and its permissions are set correctly by the logger — no manual step needed.

### 7. Cron Jobs

Add these entries to root's crontab (`sudo crontab -e`):

```
* * * * * /usr/lib/cgi-bin/remote-switch/log_temp.py
0 0 * * 0 /usr/lib/cgi-bin/remote-switch/log_temp.py flush
0 0 1 * * /usr/lib/cgi-bin/remote-switch/log_temp.py rollup
```

What each job does:
- **Every minute** — reads heater state, temperature (if probe connected), and ambient temperature (if configured); executes any due schedules; writes one row to `/run/heater.db` (RAM, tmpfs — no SD card write)
- **Weekly (Sunday midnight)** — flushes RAM database to `/var/lib/heater/heater.db` on disk
- **Monthly (1st midnight)** — pre-computes the previous month's chart data and stats into the `monthly_cache` table so past months load instantly; optionally emails a summary

On an unplanned power loss, data since the last weekly flush may be lost. For commanded reboots and shutdowns, see step 8 below.

### 8. Shutdown Flush Service

Install the included systemd unit so that any commanded reboot or shutdown flushes the RAM database to disk first:

```bash
sudo cp /usr/lib/cgi-bin/remote-switch/heater-flush.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now heater-flush.service
```

The service does nothing at boot. On any `reboot`, `shutdown`, or `systemctl stop` it runs `log_temp.py flush`, copying RAM readings to the disk database before the filesystem unmounts. On an unplanned power loss the RAM contents are still lost (acceptable — the disk database always has data up to the last flush).

Heater schedules are unaffected by reboots regardless — they are stored on disk and the every-minute cron job catches up any missed schedules on the first tick after boot.

### 9. Firewall

Install and configure a firewall:

```bash
sudo apt install ufw
sudo ufw allow ssh
sudo ufw allow http
sudo ufw enable
```

### 10. (optional) VPN

Install OpenVPN or WireGuard to connect to an existing private network.

---

## Optional: Ambient Temperature

The chart can display outdoor ambient temperature as a second line, fetched from [Open-Meteo](https://open-meteo.com/) (free, no API key). Requires `ENABLE_TEMP = True` in `config.py`.

Create `.env` in the same directory as the scripts (e.g. `/usr/lib/cgi-bin/remote-switch/.env`):

**Option A — airport ICAO code** (recommended for hangar use):

```
LOCATION=KLMO
```

On the first cron run, the airport is geocoded to lat/lon via the [OurAirports](https://ourairports.com/) public dataset, and `LATITUDE=` / `LONGITUDE=` are automatically appended to `.env`. Geocoding is skipped on all subsequent runs.

**Option B — direct coordinates**:

```
LATITUDE=45.5051
LONGITUDE=-122.6750
```

The ambient temp is fetched every 15 minutes (cached in RAM between fetches) to minimise LTE data usage — ~96 API calls/day. If `.env` is absent or the fetch fails, the chart continues to work normally without the ambient line.

---

## Optional: Monthly Email Summaries

Add to `.env`:

```
NOTIFY_EMAIL=you@example.com
```

Requires [`msmtp`](https://marlam.de/msmtp/) to be installed and configured. The monthly rollup cron job sends a summary with temperature stats and heater runtime.

---

## Scheduling

The web UI includes a one-shot scheduler that can act on either the engine-block heater (turn on/off) or the hangar HVAC (mode + target temp + fan, including a one-click Freeze Prevention preset). Schedules are stored in the database and executed by the every-minute cron job — no additional setup needed. Schedules survive reboots.

---

## Optional: Hangar HVAC (Durastar mini-split)

The web UI can also control a hangar Durastar/Midea mini-split (and any other Midea-OEM unit — Pioneer, MrCool, Senville, Comfee, etc.) over the LAN via the Midea WiFi dongle. This is independent of the engine-block heater described above — the heater stays on its own GPIO relay; the HVAC is reached over the LAN through the [`msmart-ng`](https://github.com/mill1000/midea-msmart) Python library.

### How it works
- A small WiFi dongle plugs into a USB-shaped port inside the indoor unit's front panel and bridges Midea's serial protocol to a TCP service on port 6444 of the dongle's LAN IP.
- After a one-time pairing through Midea's cloud, the Pi extracts a local `token` + `key` and from then on talks to the dongle directly on the LAN — no cloud roundtrip per command, and you can firewall the dongle off the internet.
- The web UI's HVAC card lets you set power, mode (Heat / Cool / Auto / Dry / Fan), target temperature in °F, and fan speed. A one-click "Freeze Prevention preset" button sends Heat / 60°F / Low fan — useful for "keep the hangar from freezing" scenarios.
- The scheduler accepts HVAC actions alongside heater actions; same `execute_epoch <= now` cron-driven dispatch.
- The dongle is polled at most once every 30 seconds (cached in `/run/heater-hvac.json`); page loads never block on the network. If the dongle is unreachable, the UI shows the last-known state with a "stale Xs" badge instead of erroring.
- HVAC activity is logged into `readings.ac_state` every minute and rendered on the chart as a purple band, so you can see HVAC + heater + fan + ambient temperature on one timeline.

### Hardware
Midea **US-OSK105** WiFi USB dongle (~$30 on Amazon: ASIN [B0GVSPFK1P](https://www.amazon.com/SmartKit-Adapter-Communication-Wireless-Connectivity/dp/B0GVSPFK1P)). The Durastar-branded `DRWIFIADPT1` is the same hardware behind a contractor-only Ferguson SKU — buy the generic Midea version and skip the wait.

### Setup

1. **Plug** the dongle into the USB-shaped port behind the indoor unit's front panel (the snap-off filter cover; no electrical work).
2. **Pair** it once via the **NetHome Plus** phone app — NOT SmartHome / MSmartHome. Their `get_token` cloud endpoint is currently broken (see [msmart-ng issue #201](https://github.com/mill1000/midea-msmart/issues/201)). If you registered through SmartHome, re-register via NetHome Plus before continuing.
3. **Install msmart-ng** on the Pi (Pi OS Lite ships without pip by default):
   ```bash
   sudo apt install python3-pip
   sudo pip install msmart-ng --break-system-packages
   ```
   The `--break-system-packages` flag is needed on Bookworm and later (PEP 668). For this project's deployment model (system Python under mod_wsgi + root cron) it's the pragmatic choice — a venv would require reconfiguring the WSGI daemon and cron paths.
4. **Run the setup helper** — it prompts for your NetHome Plus credentials, then in a single round-trip discovers the dongle on the LAN and authenticates it via Midea's cloud (returning the local `token` + `key`). The four `HVAC_*` keys are then written to `.env` and the script verifies with a live refresh:
   ```bash
   sudo python3 /usr/lib/cgi-bin/remote-switch/setup_hvac.py
   ```
5. Reload the web UI — the "Hangar HVAC" card replaces the "Not configured" placeholder, and HVAC becomes a device option in the scheduler.

mod_wsgi auto-reloads on `.env` changes the next time `switch.py` is touched; if the new card doesn't appear immediately, `sudo systemctl reload apache2` (or `git pull` to bump the file mtime) forces it.

### What's stored in `.env`
After pairing, four keys are added — don't edit by hand, use `setup_hvac.py` to refresh:
```
HVAC_DONGLE_IP=192.168.1.50
HVAC_DEVICE_ID=123456789012345
HVAC_TOKEN=<long hex string>
HVAC_KEY=<long hex string>
```

### What is "Freeze Prevention"?
The unit's real Freeze Protection flag — the same feature the IR remote drives. When active the indoor unit displays "FP" and the firmware holds a minimum heat output (~8°C / 46°F) internally; you don't need to (and can't) set a target temp or fan speed. Picking any other mode in the web UI exits the flag automatically. Ideal for "keep the hangar above freezing all winter without thinking about it."

### Drift detection
If someone uses the physical IR remote while you're away, the dongle's reported state diverges from what the web UI last commanded. When this happens, the HVAC card surfaces both the **Reported** state (what the unit is doing now) and **Last commanded** (what was last sent from the web), so you can see at a glance that the physical remote was used.

### Troubleshooting
- **`setup_hvac.py` finds no devices.** Check the dongle is powered (LED inside the indoor unit), paired via NetHome Plus, and on the same subnet as the Pi (hangar WiFi often runs an isolated guest network — make sure the Pi and the dongle are on the same one).
- **Setup fails at "Discovery / cloud auth failed".** Most often a SmartHome vs NetHome Plus account mismatch — re-register via NetHome Plus and retry. If that's not it, double-check the password (case-sensitive) and that you can sign in to the NetHome Plus app on the phone with the same credentials.
- **`ImportError: cannot import name '...' from 'msmart...'`.** msmart-ng's API has shifted between releases. The wrappers in this repo were verified against `msmart-ng==2025.12.0`. If you're on a much newer version and an import name has moved, see CLAUDE.md ("msmart-ng API surface") for the symbols to grep and pin a working version with `sudo pip install msmart-ng==2025.12.0`.
- **HVAC card shows "stale Xs ago".** The dongle isn't replying. Check the indoor unit has power, the dongle hasn't fallen out, and the LAN IP hasn't changed (DHCP). If the IP changed, re-run `setup_hvac.py` to refresh `.env`.
- **HVAC card shows "Not configured" after pairing.** Verify all four `HVAC_*` keys are present and non-empty in `.env`.

---

## Storage Architecture

To minimise SD card writes on the Raspberry Pi, all per-minute data is written to a SQLite database in RAM (`/run/heater.db`, on tmpfs), not to the SD card. This file is flushed to disk weekly. The web UI reads from both the RAM and disk databases via SQLite's `ATTACH` so no data is ever lost between flushes.

```
/run/heater.db                  ← RAM (tmpfs). Volatile. Written every minute.
/var/lib/heater/heater.db       ← Disk (SD card). Written weekly (flush) + monthly (rollup).
/run/heater-ambient.tmp         ← Ambient temp cache (15-min TTL, ~50 bytes).
/run/heater-hvac.json           ← Cached HVAC dongle state (30-sec TTL, ~400 bytes). Optional.
```

---

## Screenshot

<img width="367" height="338" alt="image" src="https://github.com/user-attachments/assets/cf57c170-1fed-49d0-ad67-8e05793cb1e2" />
