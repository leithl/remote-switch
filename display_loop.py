#!/usr/bin/env python3
"""
display_loop.py — Drives the hangar Pi's 3.2" ILI9341 SPI dashboard.

Reads display_state.get_state() every UPDATE_INTERVAL_SECS, renders it via
display.render(), and pushes the result to the ILI9341 panel via luma.lcd
on SPI0. Polls the XPT2046 touch chip every TOUCH_POLL_INTERVAL_SECS; a tap
within display.HEATER_BUTTON_RECT toggles GPIO 17 (the engine-block heater).

Runs under systemd (see display.service).

CLI flags:
    --once          render one frame, push, exit. Useful for spot checks.
    --dry-run       skip SPI init; write the render to /tmp/display-current.png
                    each iteration. Set if DISPLAY_DRY_RUN=1 is in env too.
    --calibrate     run the 4-corner touch calibration walkthrough, print
                    suggested TOUCH_* values for .env, then exit.
    --no-touch      skip the touch panel entirely (in case the chip isn't
                    wired but you want to drive the display).
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


UPDATE_INTERVAL_SECS      = 5
UPDATE_INTERVAL_FAST_SECS = 1      # used while a 'Xs ago' counter is on screen
TOUCH_POLL_INTERVAL_SECS  = 0.05   # 20 Hz touch polling between display refreshes
TOUCH_DEBOUNCE_SECS      = 1.0     # ignore taps within this window of the last one
DRY_RUN_OUT              = Path("/tmp/display-current.png")

# ILI9341 control pins. Match display.py's pin map and the wiring docs.
GPIO_DC  = 24
GPIO_RST = 25
SPI_PORT = 0
SPI_DEV  = 0   # SPI0 CE0 — the display's chip select (BCM 8)


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
    """Open the ILI9341 over SPI. Lazy import so the module works without luma installed."""
    from luma.core.interface.serial import spi
    from luma.lcd.device import ili9341

    serial = spi(port=SPI_PORT, device=SPI_DEV, gpio_DC=GPIO_DC, gpio_RST=GPIO_RST)
    # The ili9341 chip is natively portrait (240×320). luma.lcd swaps
    # width/height on odd rotate values, so rotate=1 expects a 240×320
    # image — but our renderer produces 320×240 landscape. Use rotate=0
    # (no swap) so the device matches our image dimensions. If the panel
    # is physically mounted upside-down, switch to rotate=2 (180° flip).
    return ili9341(serial, rotate=0, width=display.WIDTH, height=display.HEIGHT)


def _open_touch():
    """Open the XPT2046 over SPI0 CE1. Returns None on import / open failure."""
    try:
        import touch  # noqa: E402 — local module
        return touch.TouchReader()
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
    tap lands inside HEATER_BUTTON_RECT. Cheap to call at 20 Hz — a no-touch
    read is one SPI transaction for the Z value."""
    global _last_tap_epoch
    try:
        raw = touch_reader.read_raw_with_pressure()
    except Exception as e:
        sys.stderr.write(f"display: touch read failed: {e}\n")
        return
    if raw is None:
        return
    now = time.time()
    if now - _last_tap_epoch < TOUCH_DEBOUNCE_SECS:
        return  # still in cooldown from previous tap

    # Re-read averaged to filter the wild first sample of a press
    avg = touch_reader.read_averaged(n=4)
    if avg is None:
        return
    x_px, y_px = touch_reader.to_pixels(*avg)
    import touch  # local module; cheap re-import is cached
    if touch.point_in_rect(x_px, y_px, display.HEATER_BUTTON_RECT):
        global _redraw_now
        _last_tap_epoch = now
        _toggle_heater()
        _redraw_now = True   # wake _sleep_with_touch so the button color flips immediately


def _sleep_with_touch(secs, touch_reader):
    """Sleep for `secs` total, polling touch every TOUCH_POLL_INTERVAL_SECS
    and breaking early on SIGTERM or when a tap requests an immediate redraw.
    Replaces the previous plain time.sleep chunks in the main loop."""
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
        time.sleep(TOUCH_POLL_INTERVAL_SECS)


def _run_calibration():
    """Delegate to touch.run_calibration()."""
    try:
        import touch
    except ImportError as e:
        sys.stderr.write(f"calibration: touch module unavailable: {e}\n")
        sys.exit(1)
    touch.run_calibration()


def main():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if "--calibrate" in sys.argv:
        _run_calibration()
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
            _sleep_with_touch(interval, touch_reader)
    finally:
        if touch_reader is not None:
            touch_reader.close()


if __name__ == "__main__":
    main()
