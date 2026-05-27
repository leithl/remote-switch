# IPS display upgrade — MSP3218 → Haldzemo 3.5" ST7796U

Migration from the original 3.2" 240×320 ILI9341 + XPT2046 resistive panel to a 3.5" 320×480 ST7796U IPS + FT6336U capacitive panel. Triggered by the XPT2046 touch chip silently failing on the old board (see "Why we're moving" below) — IPS was a viewing-angle nice-to-have that we did at the same time.

**The whole migration is designed for one hangar visit and a single reboot.** Code is staged ahead of time. The user does the hardware swap, runs three commands, watches the journal, taps the button. No on-site debugging needed in the happy path.

If you're looking at this doc to plan the trip, read top to bottom. If you're at the hangar with the new display in hand, jump to ["At the hangar"](#at-the-hangar).

## What's changing

Side-by-side. Everything moves except the SPI signal pins (CS / RST / DC / MOSI / SCK / MISO) and the GND.

| Aspect            | Old (MSP3218)                      | New (Haldzemo 3.5")                       |
|-------------------|------------------------------------|-------------------------------------------|
| Panel size        | 3.2"                               | 3.5"                                      |
| Panel type        | TN                                 | IPS (≥80° viewing angle)                  |
| Native resolution | 240×320 (rendered as 320×240 LS)   | 320×480 (rendered as 480×320 LS)          |
| Display IC        | ILI9341                            | ST7796U                                   |
| Touch IC          | XPT2046 (resistive)                | FT6336U (capacitive)                      |
| Touch bus         | SPI (CE1 on Pi pin 26)             | I2C (SDA / SCL on Pi pins 3 / 5)          |
| Touch resolution  | 12-bit raw → calibrated via .env   | Pre-calibrated pixel coords from chip     |
| VCC / LED supply  | 3V3 (Pi pin 17)                    | 5V (Pi pin 2)                             |
| SPI logic level   | 3.3V direct                        | 3.3V through on-board level shifters       |
| Header pins       | 14-pin, 1.0mm pitch                | 14-pin, 2.54mm pitch                      |

Code consequences:
- Display IC swap → `luma.lcd.device.ili9341` → `luma.lcd.device.ili9488` (closest register-compatible driver for ST7796U; community-verified for this panel family).
- Touch IC swap → full rewrite of [touch.py](../touch.py) for FT6336U over I2C using `smbus2`.
- Resolution change → [display.py](../display.py) authored natively at 480×320; coordinates and font sizes are not scaled at render time.
- Calibration → **no longer required**. Capacitive controllers report pre-calibrated pixel coords; the `--calibrate` flow is replaced by `--scan` (a passive I2C probe + tap monitor).

## Why we're moving

(So future-us doesn't second-guess the call.)

The old XPT2046 chip went silent in late May 2026 — display rendered fine, but the touch button became unresponsive. The full chain confirmed: chip itself is XPT2046 per silkscreen, all four diagnostic command bytes returned flat-zero MISO across 30+ samples, both touch wires (Pi pin 21 ↔ display pin 9, Pi pin 26 ↔ display pin 11) verified end-to-end with correct continuity, reseat had no effect. CE1 toggles correctly during transactions (`raspi-gpio get 7,8` during an SPI hammer confirmed Pi-side health). That left the fault on the display PCB — most likely a cold joint on Q1 (the NPN transistor marked "J3Y" between XPT2046 DOUT and the external SDO pad) or the chip itself. Reflowing fine-pitch SMD with the LCD still attached needs hot-air gear we don't have in the hangar; a $19 replacement that also delivers IPS was the obvious call.

## Before / after wiring

The most consequential part. **Both ends of the cable change.** New display pin labels (silkscreen): VCC / GND / LCD_CS / LCD_RST / LCD_RS / MOSI / SCK / LED / MISO / CTP_SCL / CTP_RST / CTP_SDA / CTP_INT / SD_CS. Pi-side moves are summarised at the bottom.

| Display pin | Display silkscreen | Old Pi pin               | New Pi pin               | Change?                  |
|-------------|--------------------|--------------------------|--------------------------|--------------------------|
| 1           | VCC                | 17 (3V3)                 | **2 (5V)**               | move ⟶ 5V               |
| 2           | GND                | 6 (or 9/14/20/25 — any)  | 6 (or any GND)           | same                     |
| 3           | LCD_CS             | 24 (CE0 / BCM 8)         | 24 (CE0 / BCM 8)         | same                     |
| 4           | LCD_RST            | 22 (BCM 25)              | 22 (BCM 25)              | same                     |
| 5           | LCD_RS             | 18 (BCM 24)              | 18 (BCM 24)              | same                     |
| 6           | MOSI               | 19 (BCM 10)              | 19 (BCM 10)              | same                     |
| 7           | SCK                | 23 (BCM 11)              | 23 (BCM 11)              | same                     |
| 8           | LED                | 17 (3V3)                 | **2 (5V)** (jumper)      | move ⟶ 5V (joins VCC)   |
| 9           | MISO               | 21 (BCM 9)               | 21 (BCM 9)               | same                     |
| 10          | CTP_SCL            | —                        | **5 (BCM 3 / SCL1)**     | **new wire**             |
| 11          | CTP_RST            | (was T_CS on old: 26)    | —                        | leave unconnected        |
| 12          | CTP_SDA            | —                        | **3 (BCM 2 / SDA1)**     | **new wire**             |
| 13          | CTP_INT            | —                        | —                        | leave unconnected        |
| 14          | SD_CS              | —                        | —                        | leave unconnected        |

Pi-side net changes:

- Pi pin 17 (3V3) — **two wires removed.** No longer used by the display.
- Pi pin 2 (5V) — **two wires added.** Splice / dupont-Y to feed both VCC (pin 1) and LED (pin 8) from the same 5V rail.
- Pi pin 26 (CE1) — **one wire removed.** Used to feed the XPT2046's T_CS; FT6336U doesn't use CE1.
- Pi pin 3 (SDA1) — **new wire** to CTP_SDA.
- Pi pin 5 (SCL1) — **new wire** to CTP_SCL.

Why 5V instead of keeping 3V3:
The Haldzemo listing says "compatible with 5V and 3.3V MCU" — that's about the SPI/I2C signal level, handled by on-board level shifters. The board's *power* rail (VCC) is spec'd 5V (listing "Working Voltage: 5V"). Backlight current is 95 mA — at 3V3 the LED will be dimmer than designed. 5V is the right call.

CTP_INT and CTP_RST left floating:
- CTP_INT is a level/edge signal the chip raises when a touch is active. We poll instead (20 Hz, cheap I2C block-read), no interrupt-handler complexity.
- CTP_RST is a manual reset that boards usually pull up internally; if it stays floating the chip self-initialises at power-on. Wire it only if first power-on shows the chip not responding (very rare).

## Code changes (already staged)

| File                                | What changed                                                   |
|-------------------------------------|----------------------------------------------------------------|
| [touch.py](../touch.py)             | Full rewrite. `TouchReader.read_touch()` returns pre-calibrated `(x, y)` from FT6336U over I2C. `run_calibration()` removed; `run_scan()` (I2C probe + 10s tap monitor) added. |
| [display.py](../display.py)         | Native 480×320 layout. `WIDTH/HEIGHT` updated; all positions, font sizes, and `HEATER_BUTTON_RECT` authored directly for the new resolution. |
| [display_loop.py](../display_loop.py) | luma.lcd driver: `ili9341` → `ili9488`. Touch path: `TouchReader.read_raw_with_pressure / read_averaged / to_pixels` → `TouchReader.read_touch()`. `--calibrate` flag → `--scan`. |
| [docs/ips-display-upgrade.md](ips-display-upgrade.md) | This file. |
| `.env` (on Pi, not in git)          | Old `TOUCH_X_MIN / X_MAX / Y_MIN / Y_MAX / AXES_SWAP / X_INVERT / Y_INVERT / PRESSURE_THRESHOLD` keys obsolete — safe to delete. New optional keys (only set if `--scan` shows inverted axes): `TOUCH_I2C_BUS / I2C_ADDR / SWAP_XY / INVERT_X / INVERT_Y`. Defaults work for the Haldzemo board out of the box. |

Diff highlights — for review before merging:

```diff
# display_loop.py: SPI display driver swap
- from luma.lcd.device import ili9341
+ from luma.lcd.device import ili9488

- return ili9341(serial, rotate=0, width=display.WIDTH, height=display.HEIGHT)
+ return ili9488(serial, rotate=0, width=display.WIDTH, height=display.HEIGHT)

# display_loop.py: touch poll uses new API, no more raw→pixel mapping
- raw = touch_reader.read_raw_with_pressure()
- ...
- avg = touch_reader.read_averaged(n=4)
- x_px, y_px = touch_reader.to_pixels(*avg)
+ pt = touch_reader.read_touch()
+ if pt is None: return
+ x_px, y_px = pt

# display.py: native 480x320
- WIDTH  = 320
- HEIGHT = 240
- HEATER_BUTTON_RECT = (10, 22, 150, 92)
+ WIDTH  = 480
+ HEIGHT = 320
+ HEATER_BUTTON_RECT = (15, 30, 225, 122)
```

## Pi-side prep

I2C isn't enabled by default on this Pi (we never used it before). Run before the hangar trip if you can — keeps the on-site work down to wiring + reboot.

```bash
ssh pi
sudo apt install -y python3-smbus
sudo raspi-config nonint do_i2c 0     # 0 = enable
sudo reboot
```

After reboot, confirm:

```bash
ssh pi
ls /dev/i2c-*
```

Should print `/dev/i2c-1`. (The old `/dev/spidev0.*` nodes also stay — SPI is still used for the display.)

If the staged code from this branch isn't on the Pi yet, push it now while the **old display is still working** (the new code falls back gracefully on missing modules; the existing service will just fail to render until the hardware is swapped — that's the cutover signal):

```bash
ssh pi
cd /usr/lib/cgi-bin/remote-switch
sudo git fetch origin
sudo git checkout <feature-branch-name>   # or whatever branch this lands on
sudo git pull
```

Stop the display service before the trip so it doesn't spam push-failures into the journal while the cable is disconnected:

```bash
sudo systemctl stop display
```

## At the hangar

**1. Shut down the Pi first.** Avoids any chance of brown-outs while you're wiring 5V.

```bash
ssh pi
sudo shutdown -h now
```

Wait for the green ACT LED to go fully dark.

**2. Unplug the Pi from power.**

**3. Disconnect the old display** — pull the 10-wire bundle off the display board. Keep the dupont housings clipped together so the colour-to-pin mapping survives the next step.

**4. Re-pin the cable on the Pi side** for the four wires that move (see Pi-side net changes above):

- Move the wire that was on Pi pin 17 (VCC) to **Pi pin 2 (5V)**.
- Move the second wire that was on Pi pin 17 (LED) — splice or use a dupont-Y so both VCC and LED share **Pi pin 2 (5V)**.
- Remove the wire from Pi pin 26 (the old T_CS) entirely. Set it aside or trim it.
- Add a new wire from Pi pin 3 (SDA1) → ready to go to display pin 12 (CTP_SDA).
- Add a new wire from Pi pin 5 (SCL1) → ready to go to display pin 10 (CTP_SCL).

**5. Connect to the new display.** Match the display's silkscreen labels to the colours of each cable end. The board labels every pin on the back; no counting required. The wiring table above is authoritative — when in doubt, the silkscreen on the back of the new board wins.

Reasonable extra check while you're there: tug each wire gently after seating — dupont contacts can look mated but be on a flaky side of the contact spring. Re-seat any that come out without resistance.

**6. Plug the Pi back in.** Watch the screen during boot — the green LED should blink, then the display should backlight after ~5 seconds.

**7. SSH in and verify:**

```bash
ssh pi
# Service should be running already (systemd auto-starts it)
sudo systemctl status display --no-pager -n 5

# Watch the journal during the next minute
sudo journalctl -u display -f
```

**Expected:**
- `Started Hangar dashboard ...`
- No `push failed` lines.
- No `touch init failed` lines.
- The physical screen shows the dashboard.

Tap the ON/OFF button. Within ~1s you should see in the journal:

```
display: heater toggle 0 -> 1
```

(or `1 -> 0` depending on initial state). If you see that line and the heater state actually changed on the web UI / GPIO 17, you're done. Skip the rest of this doc.

## First-power-on verification

If anything's off, run the `--scan` mode to see what the I2C side actually looks like:

```bash
ssh pi
sudo systemctl stop display
sudo python3 /usr/lib/cgi-bin/remote-switch/display_loop.py --scan
```

Expected output (no finger on screen, then tap each corner):

```
=== I2C scan on bus 1 ===
  responding: 0x38
  ✓ FT6336U expected at 0x38 — present

=== Live touch monitor (10s) — tap each corner ===
  tap at x=  8 y=  6     ← top-left
  tap at x=472 y=  9     ← top-right
  tap at x=  5 y=315     ← bottom-left
  tap at x=475 y=313     ← bottom-right
```

Then restart the service:

```bash
sudo systemctl start display
```

If the I2C scan shows nothing, see "Troubleshooting" → "I2C scan finds no devices".

If the scan shows 0x38 but the corner-tap coordinates are flipped or mirrored, add to `.env`:

```bash
sudo nano /usr/lib/cgi-bin/remote-switch/.env
```

| Symptom (top-left tap reports …) | Fix in .env                |
|----------------------------------|----------------------------|
| `x=475 y=313` (bottom-right)     | `TOUCH_INVERT_X=1` and `TOUCH_INVERT_Y=1` |
| `x=475 y=6` (top-right)          | `TOUCH_INVERT_X=1`         |
| `x=8 y=313` (bottom-left)        | `TOUCH_INVERT_Y=1`         |
| `x=6 y=8` (axes look swapped — y values tiny, x values big when tapping top-bottom) | `TOUCH_SWAP_XY=0` (off) |

Then restart the service and re-run `--scan` to confirm.

## Troubleshooting

### Display stays blank (backlight on, no pixels)

- **Verify driver swap landed.** `sudo grep ili9488 /usr/lib/cgi-bin/remote-switch/display_loop.py` should match. If it shows `ili9341`, the git pull didn't take — repeat `sudo git pull` and `sudo systemctl restart display`.
- **Check the journal for `push failed`.** A driver-incompat with ST7796 would surface here. If ili9488 doesn't produce a visible image, the next fallback is ili9486 (also close to ST7796): change `ili9488` → `ili9486` in [display_loop.py:86](../display_loop.py:86) and [:93](../display_loop.py:93), restart service.
- **Pi reset wire seated?** GPIO 25 → display pin 4 (LCD_RST). If RST is floating, the chip stays in reset and renders nothing. Reseat.

### Image rotated 90° (everything sideways)

The MADCTL bits luma sets default to a portrait read order; some ST7796 boards interpret them differently. Try `rotate=1` in [display_loop.py:_open_device()](../display_loop.py:93). If that flips it upside-down, use `rotate=3`.

### I2C scan finds no devices

```
=== I2C scan on bus 1 ===
  no devices responded — check SDA/SCL wiring, I2C enabled?
```

- **I2C enabled?** `grep i2c /boot/firmware/config.txt` should show `dtparam=i2c_arm=on`. If not: `sudo raspi-config nonint do_i2c 0 && sudo reboot`.
- **Bus selected?** `ls /dev/i2c-*` should list `/dev/i2c-1`. If only `/dev/i2c-0` is present, the wiring is going to the EEPROM-reserved bus instead of the GPIO-exposed one — confirm SDA is on Pi pin 3 (BCM 2), SCL on pin 5 (BCM 3).
- **Display end of the new wires landed on the right pads?** CTP_SDA = pin 12, CTP_SCL = pin 10 (not 11 and 13 — those are RST and INT).
- **5V actually reaching VCC?** A multimeter from Pi pin 2 to display pin 1 should read ~5V with the Pi powered on. If LED is on but chip isn't responding, VCC might be missing — easy to test.

### Touch reports coords but they don't match the screen

Run `--scan`, tap each corner, decode the result against the "Symptom → Fix" table above. The default `TOUCH_SWAP_XY=1` is correct for the Haldzemo board's portrait-native chip orientation in a landscape mount; if the seller switches their pre-config and ships a landscape-native variant, `TOUCH_SWAP_XY=0` is the override.

### Heater button taps register but `journal` doesn't show `heater toggle`

The hit-test is failing — coords are coming in but landing outside `HEATER_BUTTON_RECT (15, 30, 225, 122)`. Run `--scan`, tap inside the visible ON/OFF area, confirm coords are roughly in that rect. If they're way off, calibration overrides in `.env` (swap/invert) are what you need.

### Service log spam: `display: touch read failed: [Errno 121] Remote I/O error`

I2C bus glitch, usually one-off. Persistent failures (every poll) mean:
- SDA or SCL has a loose contact.
- Pull-up resistors are missing — uncommon, but some FT6336U breakouts skip them. Add external 4.7kΩ from each line to 3V3 (Pi pin 1).
- VCC is brownout-ing — chip resets each poll. Check 5V at display pin 1.

## Rolling back

Revert to the old MSP3218 hardware + code:

```bash
ssh pi
cd /usr/lib/cgi-bin/remote-switch
sudo git checkout main          # or whatever branch had the old code
sudo systemctl restart display
sudo shutdown -h now            # then go physically swap the old display back in
```

The old `TOUCH_*` keys in `.env` (if you didn't delete them) will still be read by the rolled-back `touch.py` since their key names are different from the new I2C keys. No conflict.

## Verification checklist

Tick all six before declaring done:

- [ ] Pre-trip: I2C enabled (`/dev/i2c-1` exists), `python3-smbus` installed, new code on the Pi.
- [ ] At the hangar: new wiring matches the table above; all dupont contacts seated firmly.
- [ ] First boot: `sudo systemctl status display` shows `active (running)`, no immediate restarts.
- [ ] Journal: no `push failed`, no `touch init failed`, no `touch read failed` (or only one-off transients).
- [ ] Display renders the dashboard correctly — colours, layout, hangar/ambient temperatures visible.
- [ ] Tapping the ON/OFF button logs `display: heater toggle X -> Y` and the physical heater state changes (verify on the web UI or `cat /sys/class/gpio/gpio17/value`).

## Code references

- [touch.py:40-58](../touch.py:40) — register layout / I2C address defaults / orientation flags.
- [touch.py:80-160](../touch.py:80) — `TouchReader` class.
- [touch.py:172-210](../touch.py:172) — `run_scan()`, called from `display_loop.py --scan`.
- [display.py:53-54](../display.py:53) — `WIDTH=480 / HEIGHT=320`.
- [display.py:309](../display.py:309) — `HEATER_BUTTON_RECT = (15, 30, 225, 122)`.
- [display_loop.py:72-93](../display_loop.py:72) — `_open_device()` using `ili9488` luma driver.
- [display_loop.py:96-104](../display_loop.py:96) — `_open_touch()` using the new FT6336U `TouchReader`.
- [display_loop.py:154-181](../display_loop.py:154) — `_poll_touch()`, the screen-pixel hit-test path.

## Related history

- 2026-05-27 (morning): XPT2046 on the original MSP3218 board confirmed non-responsive despite verified-correct wiring; touch IC silkscreen photo confirmed XPT2046; suspected Q1 buffer fault. Decision to replace rather than reflow.
- 2026-05-27 (afternoon): Haldzemo 3.5" IPS ordered. Code staged ahead of arrival.
