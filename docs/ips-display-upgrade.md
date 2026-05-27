# Task: Add ST7796U + FT6336U display support for new 3.5" IPS panel

## Background

The hangar Pi Zero W currently drives a 3.2" ILI9341 TN TFT (320×240) with XPT2046 resistive touch over SPI. The TN panel has poor viewing angles. A replacement **Haldzemo 3.5" IPS 480×320 SPI display** with capacitive touch is arriving. Specs:

- **Display driver:** ST7796U (SPI, same bus — SPI0 CE0, BCM 8)
- **Touch driver:** FT6336U (I2C, address 0x38 — SDA on BCM 2, SCL on BCM 3)
- **Resolution:** 480×320
- **Power:** 5V (has onboard 3.3V level shifters for SPI/I2C signals)
- **Pin header:** 14-pin 2.54mm breakout (not a GPIO hat)

## Display wiring (unchanged from current ILI9341 except VCC moves to 5V)

| Board label | Pi connection | Notes |
|-------------|--------------|-------|
| VCC | 5V (pin 2 or 4) | **Changed from 3V3** — board needs 5V, has onboard level conversion |
| GND | GND (pin 14) | Same |
| LCD_CS | BCM 8 (pin 24) | SPI0 CE0 — same |
| LCD_RST | BCM 25 (pin 22) | Same |
| LCD_RS | BCM 24 (pin 18) | This is DC — same |
| MOSI | BCM 10 (pin 19) | Same |
| SCK | BCM 11 (pin 23) | Same |
| LED | 5V (pin 2 or 4) | **Changed from 3V3** |
| MISO | BCM 9 (pin 21) | Same |
| CTP_SCL | BCM 3 (pin 5) | **New — I2C1 SCL** |
| CTP_RST | any free GPIO | Optional reset |
| CTP_SDA | BCM 2 (pin 3) | **New — I2C1 SDA** |
| CTP_INT | any free GPIO | Touch interrupt (optional, can poll) |
| SD_CS | unconnected | Not used |

## What needs to change

### 1. `display_loop.py` — replace `_open_device()` with ST7796U init

`luma.lcd` does not have a built-in ST7796U device. Options:
- Subclass `luma.lcd.device.__init__` and send the ST7796U init sequence over SPI. The ST7796U is very similar to ST7789 (already in luma) and ILI9488. The init sequence is well-documented. Native resolution is 320×480 portrait; use MADCTL to set landscape if needed, or render 480×320 and let the chip handle it.
- The current code at `display_loop.py:66-76` creates an `ili9341` device. Replace with the ST7796U equivalent.
- **Important:** ST7796U uses 18-bit color (RGB666) over SPI, not 16-bit like ILI9341. The SPI pixel format command (`0x3A`) needs to be set to `0x66` for RGB666 or `0x55` for RGB565 — check what works. RGB565 is simpler (same as ILI9341) and the ST7796U supports it.
- Keep `GPIO_DC = 24`, `GPIO_RST = 25`, `SPI_PORT = 0`, `SPI_DEV = 0` — those don't change.

### 2. `touch.py` — rewrite for FT6336U over I2C

The current `touch.py` is entirely XPT2046-specific (SPI, 12-bit ADC reads, pressure threshold, analog calibration). Replace with an FT6336U I2C driver:
- FT6336U sits at I2C address `0x38`
- Read touch points from register `0x02` (touch data starts there): `0x03` = X high + event flag, `0x04` = X low, `0x05` = Y high + touch ID, `0x06` = Y low
- Number of touch points at register `0x02`
- No pressure calibration needed — capacitive touch is binary (touched or not)
- No axis calibration should be needed for a 480×320 panel, but keep the invert/swap options in .env in case the orientation is flipped
- Use `smbus2` or raw `ioctl` on `/dev/i2c-1` — lazy import like the current spidev pattern
- Keep the same public interface: `TouchReader` class with `read_raw_with_pressure()` (return `(x, y, 1)` or `None`), `read_averaged()`, `to_pixels()`, `close()`. The `point_in_rect()` helper stays as-is.
- `to_pixels()` becomes simpler — FT6336U reports in display pixel coordinates directly (0–479, 0–319), so calibration is just optional axis swap/invert, no linear scaling from ADC range.
- The `run_calibration()` flow can be simplified or removed — cap touch shouldn't need it. If you keep it, adapt for I2C.
- **Touch resolution is 320×480 per the spec sheet**, matching the display.

### 3. `display.py` — scale layout from 320×240 to 480×320

- Change `WIDTH = 480`, `HEIGHT = 320` (landscape orientation)
- `HEATER_BUTTON_RECT` at `(10, 22, 150, 92)` — scale proportionally or redesign to use the extra space. The button should be larger / more tappable on the bigger screen.
- All hardcoded pixel positions in `render()` need adjusting: section headers, rule lines at y=105 and y=200, the footer at y=213, progress bar coordinates, right-aligned text at `WIDTH - 10`, etc.
- Font sizes can go up slightly — more pixels to work with.
- The mockup generator at the bottom (`if __name__ == "__main__"`) should use the new dimensions.
- `display_loop.py:_poll_touch` calls `display.HEATER_BUTTON_RECT` — that'll pick up the new value automatically.

### 4. `display_loop.py` — update `_open_touch()`

Currently imports `touch` and creates `touch.TouchReader()` which opens SPI. After the rewrite it should open I2C instead. The surrounding code (`_poll_touch`, `_sleep_with_touch`) shouldn't need changes if the `TouchReader` interface stays the same.

### 5. Wiring docs — update `docs/display-install.html`

Update the wiring table and instructions to reflect:
- 5V power instead of 3V3
- I2C touch pins (CTP_SDA, CTP_SCL, CTP_INT, CTP_RST) replacing the XPT2046 SPI pins (T_CS, T_CLK, T_DIN, T_DO)
- New display driver chip name
- Note that I2C must be enabled (`raspi-config` → Interface → I2C, or `dtparam=i2c_arm=on` in `/boot/config.txt`)

## What should NOT change

- `display_state.py` — the state dict shape is unchanged
- `config.py` — no GPIO or DB changes
- `display.service` — systemd unit stays the same
- The `--once`, `--dry-run`, `--calibrate`, `--no-touch` CLI flags should all still work
- The heater toggle via `_toggle_heater()` (GPIO 17) is unchanged

## Key constraints

- Pi Zero W — limited CPU/RAM. Keep SPI clock reasonable (~32 MHz max for ST7796U, current ILI9341 runs fine at luma's default).
- Lazy imports everywhere — `smbus2`/`spidev`/`luma` are only imported inside functions so the module loads on dev machines without those packages.
- The display must coexist with: heater relay (BCM 17), fan relay (BCM 27), DS18B20 temp probe (BCM 4 / 1-wire), and the HVAC dongle (WiFi/TCP, no GPIO conflict).
