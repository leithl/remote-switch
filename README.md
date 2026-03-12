# remote-switch
Raspberry Pi connected to a mobile/cell network to turn on/off an AC-powered device.

I use it in an airplane hangar to turn on an airplane oil pan heater, but it would work anywhere there is reception and power to turn on/off any device.

## Equipment

Links provided for your convenience, but buy from wherever you prefer.

* [Raspberry Pi Zero W](https://www.raspberrypi.com/products/raspberry-pi-zero-w/)
  * You'll need a microSD card if you don't have one. 4GB+ is enough.
* [SIM7600 LTE modem HAT for Pi](https://www.waveshare.com/sim7600a-h-4g-hat.htm) also available on [Amazon](https://www.amazon.com/SIM7600A-H-4G-HAT-Communication-Positioning/dp/B082WH85WV/)
  * You'll need a SIM card. The docs say nano but the unit I had uses a mini SIM slot. I used a Google Fi SIM since it only costs data on my existing plan.
  * Skip this if you already have reliable WiFi.
* [Digital Loggers IoT relay](https://dlidirect.com/products/iot-power-relay)
  * Connect `-` to GND on the Pi, and `+` to an unused GPIO pin.
* (optional) [DS18B20 temperature probe](https://www.adafruit.com/product/381) — displays temperature on the control page
  * You'll also need a 4.7kΩ resistor between the data and power lines.

## Setup

### 1. Raspberry Pi OS

Install [Raspberry Pi OS Lite](https://www.raspberrypi.com/software/operating-systems/) and configure the following.

Add to `/boot/config.txt`:

```
# https://forums.raspberrypi.com/viewtopic.php?f=117&t=208748
# set GPIO pin 17 as output, default low
gpio=17=op,dl

# (optional) enable 1-wire for DS18B20 temp probe
dtoverlay=w1-gpio
```

Add to `/etc/rc.local` (before `exit 0`):

```
echo "17" > /sys/class/gpio/export
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

The web UI includes a one-shot scheduler to turn the heater on or off at a future date and time. Schedules are stored in the database and executed by the every-minute cron job — no additional setup needed. Schedules survive reboots.

---

## Storage Architecture

To minimise SD card writes on the Raspberry Pi, all per-minute data is written to a SQLite database in RAM (`/run/heater.db`, on tmpfs), not to the SD card. This file is flushed to disk weekly. The web UI reads from both the RAM and disk databases via SQLite's `ATTACH` so no data is ever lost between flushes.

```
/run/heater.db                  ← RAM (tmpfs). Volatile. Written every minute.
/var/lib/heater/heater.db       ← Disk (SD card). Written weekly (flush) + monthly (rollup).
/run/heater-ambient.tmp         ← Ambient temp cache (15-min TTL, ~50 bytes).
```

---

## Screenshot

<img width="367" height="338" alt="image" src="https://github.com/user-attachments/assets/cf57c170-1fed-49d0-ad67-8e05793cb1e2" />
