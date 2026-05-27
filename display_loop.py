#!/usr/bin/env python3
"""
display_loop.py — Drives the hangar Pi's 3.5" ST7796U SPI dashboard.

Reads display_state.get_state() every UPDATE_INTERVAL_SECS, renders it via
display.render(), and pushes the result to the ST7796U panel via luma.lcd
on SPI0. Polls the FT6336U capacitive touch chip over I2C every
TOUCH_POLL_INTERVAL_SECS; a tap within display.HEATER_BUTTON_RECT toggles
GPIO 17 (the engine-block heater).

Runs under systemd (see display.service).

CLI flags:
    --once          render one frame, push, exit. Useful for spot checks.
    --dry-run       skip SPI init; write the render to /tmp/display-current.png
                    each iteration. Set if DISPLAY_DRY_RUN=1 is in env too.
    --no-touch      skip the touch panel entirely (in case the chip isn't
                    wired but you want to drive the display).
    --scan          probe the I2C bus and print a 10s tap monitor. Replaces
                    the old XPT2046 --calibrate flow — capacitive controllers
                    report pre-calibrated pixel coords directly.
"""

import os
import signal
import sys
import time
from pathlib import Path

# Allow systemd to invoke us from any working directory.
sys.path.insert(0, str(Path(__file__).parent))

import config           # noqa: E402  (path-modifying import above)
import display          # noqa: E402
import display_state    # noqa: E402
import flashair         # noqa: E402


UPDATE_INTERVAL_SECS      = 5
UPDATE_INTERVAL_FAST_SECS = 1      # used while a 'Xs ago' counter is on screen
TOUCH_POLL_INTERVAL_SECS  = 0.05   # 20 Hz touch polling between display refreshes
TOUCH_DEBOUNCE_SECS      = 1.0     # ignore taps within this window of the last one
DRY_RUN_OUT              = Path("/tmp/display-current.png")

# ST7796U control pins on the Haldzemo 3.5" board. The board's silkscreen
# names LCD_RS for what other panels call DC (data/command select) — same
# function, different label.
GPIO_DC  = 24   # Display pin 5 (LCD_RS)
GPIO_RST = 25   # Display pin 4 (LCD_RST)
SPI_PORT = 0
SPI_DEV  = 0    # SPI0 CE0 — display chip select (BCM 8, Display pin 3 / LCD_CS)


_stop = False
_redraw_now = False    # set by _poll_touch when an action fires; consumed by _sleep_with_touch
_last_tap_epoch = 0.0


def _on_signal(_signum, _frame):
    global _stop
    _stop = True


def _is_dry_run():
    return "--dry-run" in sys.argv or os.environ.get("DISPLAY_DRY_RUN") == "1"


def _touch_enabled():
    return "--no-touch" not in sys.argv and not _is_dry_run()


def _open_device():
    """Open the ST7796U over SPI via luma.lcd's ili9488 driver.

    luma.lcd has no native ST7796 class as of 2.x. ILI9488 is the closest
    register-compatible driver: same 320x480 framebuffer, same 18bpp SPI
    write protocol, near-identical init sequence — community confirmation
    that this pairing works in practice across several MSP3520-style boards
    (Haldzemo / aceirmc / HiLetGo variants). If first power-on shows blank
    screen, garbled colors, or repeated `push failed`, see
    docs/ips-display-upgrade.html → "Troubleshooting" → "Display backlights but stays blank".

    Lazy import so the module loads without luma installed (e.g. macOS dev).
    """
    from luma.core.interface.serial import spi
    from luma.lcd.device import ili9488

    serial = spi(port=SPI_PORT, device=SPI_DEV, gpio_DC=GPIO_DC, gpio_RST=GPIO_RST)
    # ST7796U native frame is 320 wide × 480 tall (portrait). MADCTL bits
    # configured by luma's ili9488 driver rotate to landscape internally.
    # We hand it our 480×320 image with rotate=0; if it shows up 90° wrong
    # on first boot, try rotate=1. Physical-upside-down panels use rotate=2.
    return ili9488(serial, rotate=0, width=display.WIDTH, height=display.HEIGHT)


def _open_touch():
    """Open the FT6336U over I2C bus 1. Returns None on import / open failure."""
    try:
        import touch  # noqa: E402 — local module
        return touch.TouchReader(screen_w=display.WIDTH, screen_h=display.HEIGHT)
    except Exception as e:
        sys.stderr.write(f"display: touch init failed: {e} (continuing without touch)\n")
        return None


def _flashair_ssid_pattern():
    """Substring used to color a matching SSID as 'active' (amber). Default 'flashair'."""
    env = config.load_env()
    return env.get("FLASHAIR_SSID_PATTERN", "flashair").strip() or "flashair"


def _push(device, ssid_pattern):
    """Render one frame and push it. Returns (ok, next_interval_secs).
    Errors logged, never raised; failure paths fall back to the slow interval."""
    try:
        state = display_state.get_state()
        img = display.render(state, flashair_ssid_pattern=ssid_pattern)
    except Exception as e:
        sys.stderr.write(f"display: state/render failed: {e}\n")
        return False, UPDATE_INTERVAL_SECS

    next_interval = (
        UPDATE_INTERVAL_FAST_SECS
        if display.has_seconds_resolution(state)
        else UPDATE_INTERVAL_SECS
    )

    try:
        if device is None:
            img.save(DRY_RUN_OUT)
        else:
            device.display(img)
        return True, next_interval
    except Exception as e:
        # Transient SPI hiccups shouldn't kill the loop — systemd Restart handles real crashes.
        # Include exception class name so silently-stringified errors are diagnosable.
        sys.stderr.write(
            f"display: push failed: {type(e).__name__}: {e!r}\n"
        )
        return False, UPDATE_INTERVAL_SECS


def _toggle_heater():
    """Flip GPIO 17 to its inverse. Logs the action."""
    try:
        current = config.read_gpio()
        new = "0" if current == "1" else "1"
        config.write_gpio(new)
        sys.stderr.write(f"display: heater toggle {current} -> {new}\n")
    except (OSError, PermissionError) as e:
        sys.stderr.write(f"display: heater toggle failed: {e}\n")


def _poll_touch(touch_reader):
    """One tick of touch polling. Triggers heater toggle if a debounced
    tap lands inside HEATER_BUTTON_RECT.

    Capacitive controllers do their own debouncing and only report active
    contacts, so the per-poll cost is one I2C block-read returning quickly
    when no finger is on the panel. Coordinates come back pre-calibrated
    in 480×320 screen space — direct hit-test against HEATER_BUTTON_RECT,
    no scaling needed.
    """
    global _last_tap_epoch
    try:
        pt = touch_reader.read_touch()
    except Exception as e:
        sys.stderr.write(f"display: touch read failed: {e}\n")
        return
    if pt is None:
        return
    now = time.time()
    if now - _last_tap_epoch < TOUCH_DEBOUNCE_SECS:
        return  # still in cooldown from previous tap

    x_px, y_px = pt
    import touch  # local module; cheap re-import is cached
    if touch.point_in_rect(x_px, y_px, display.HEATER_BUTTON_RECT):
        global _redraw_now
        _last_tap_epoch = now
        _toggle_heater()
        _redraw_now = True   # wake _sleep_with_touch so the button color flips immediately


def _flashair_mtime_ns():
    """Returns the FlashAir status file's mtime in ns, or 0 if absent / unreadable.
    Used to wake the render loop within ~50 ms of any daemon status write, instead
    of waiting out the fixed 1s/5s cadence. The daemon writes via atomic temp+rename
    (flashair_sync._write_status), so every transition bumps mtime cleanly."""
    try:
        return flashair.FLASHAIR_STATUS_FILE.stat().st_mtime_ns
    except OSError:
        return 0


def _sleep_with_touch(secs, touch_reader, fa_mtime_baseline):
    """Sleep for `secs` total, polling touch every TOUCH_POLL_INTERVAL_SECS
    and breaking early on SIGTERM, on a tap requesting an immediate redraw,
    or when the FlashAir status file changes (daemon wrote new state)."""
    global _redraw_now
    chunks = int(secs / TOUCH_POLL_INTERVAL_SECS)
    for _ in range(chunks):
        if _stop:
            return
        if touch_reader is not None:
            _poll_touch(touch_reader)
        if _redraw_now:
            _redraw_now = False
            return
        if _flashair_mtime_ns() != fa_mtime_baseline:
            return
        time.sleep(TOUCH_POLL_INTERVAL_SECS)


def _run_scan():
    """Delegate to touch.run_scan() — I2C scan + 10s tap monitor."""
    try:
        import touch
    except ImportError as e:
        sys.stderr.write(f"scan: touch module unavailable: {e}\n")
        sys.exit(1)
    touch.run_scan()


def main():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if "--scan" in sys.argv:
        _run_scan()
        return

    device = None if _is_dry_run() else _open_device()
    touch_reader = _open_touch() if _touch_enabled() else None
    ssid_pattern = _flashair_ssid_pattern()

    if "--once" in sys.argv:
        ok, _ = _push(device, ssid_pattern)
        if _is_dry_run() and ok:
            print(f"wrote {DRY_RUN_OUT}")
        # Normal interpreter exit triggers luma.lcd's cleanup, which clears
        # the framebuffer — annoying for install-time verification where the
        # whole point of --once is "leave one frame on the panel so I can
        # eyeball it". os._exit skips Python's cleanup chain.
        os._exit(0 if ok else 1)

    try:
        while not _stop:
            _, interval = _push(device, ssid_pattern)
            # Capture mtime after rendering — the frame on screen reflects whatever
            # was in the file at that moment, so a later mtime means there's new
            # state to render.
            fa_mtime = _flashair_mtime_ns()
            _sleep_with_touch(interval, touch_reader, fa_mtime)
    finally:
        if touch_reader is not None:
            touch_reader.close()


if __name__ == "__main__":
    main()
