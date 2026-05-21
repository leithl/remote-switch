#!/usr/bin/env python3
"""
display_loop.py — Drives the hangar Pi's 3.2" ILI9341 SPI dashboard.

Reads display_state.get_state() every UPDATE_INTERVAL_SECS, renders it via
display.render(), and pushes the result to the ILI9341 panel via luma.lcd
on SPI0.

Runs under systemd (see display.service). Touch handling is intentionally
deferred to a follow-up — this loop is render-only, so the rest of the path
can land + be verified on hardware before XPT2046 calibration adds a moving
part. The button geometry already lives in display.HEATER_BUTTON_RECT for
the touch handler to consume when it lands.

CLI flags:
    --once          render one frame, push, exit. Useful for spot checks.
    --dry-run       skip SPI init; write the render to /tmp/display-current.png
                    each iteration. Sets if DISPLAY_DRY_RUN=1 is in env too.
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


UPDATE_INTERVAL_SECS = 5
DRY_RUN_OUT          = Path("/tmp/display-current.png")

# ILI9341 control pins. Match display.py's pin map and the wiring docs.
GPIO_DC  = 24
GPIO_RST = 25
SPI_PORT = 0
SPI_DEV  = 0   # SPI0 CE0 — the display's chip select (BCM 8)


_stop = False


def _on_signal(_signum, _frame):
    global _stop
    _stop = True


def _is_dry_run():
    return "--dry-run" in sys.argv or os.environ.get("DISPLAY_DRY_RUN") == "1"


def _open_device():
    """Open the ILI9341 over SPI. Lazy import so the module works without luma installed."""
    from luma.core.interface.serial import spi
    from luma.lcd.device import ili9341

    serial = spi(port=SPI_PORT, device=SPI_DEV, gpio_DC=GPIO_DC, gpio_RST=GPIO_RST)
    # rotate=1 → landscape orientation (320 wide × 240 tall). The renderer
    # produces images in that orientation; rotate=0 would render sideways.
    return ili9341(serial, rotate=1, width=display.WIDTH, height=display.HEIGHT)


def _flashair_ssid_pattern():
    """Substring used to color a matching SSID as 'active' (amber). Default 'flashair'."""
    env = config.load_env()
    return env.get("FLASHAIR_SSID_PATTERN", "flashair").strip() or "flashair"


def _push(device, ssid_pattern):
    """Render one frame and push it. Errors logged, never raised."""
    try:
        state = display_state.get_state()
        img = display.render(state, flashair_ssid_pattern=ssid_pattern)
    except Exception as e:
        sys.stderr.write(f"display: state/render failed: {e}\n")
        return

    try:
        if device is None:
            img.save(DRY_RUN_OUT)
        else:
            device.display(img)
    except Exception as e:
        # Transient SPI hiccups shouldn't kill the loop — systemd Restart handles real crashes.
        sys.stderr.write(f"display: push failed: {e}\n")


def main():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    device = None if _is_dry_run() else _open_device()
    ssid_pattern = _flashair_ssid_pattern()

    if "--once" in sys.argv:
        _push(device, ssid_pattern)
        if _is_dry_run():
            print(f"wrote {DRY_RUN_OUT}")
        return

    while not _stop:
        _push(device, ssid_pattern)
        # Sleep in 100ms chunks so SIGTERM (systemctl stop) is responsive.
        for _ in range(UPDATE_INTERVAL_SECS * 10):
            if _stop:
                break
            time.sleep(0.1)


if __name__ == "__main__":
    main()
