"""
backlight.py — PWM control of the dashboard panel's LED/backlight pin.

The 3.5" ST7796 board's LED pin is moved off the always-on VCC jumper onto
BACKLIGHT_GPIO (default GPIO18, a hardware-PWM pin) so the display can sleep
when no one's in the hangar — extending LED-backlight life and cutting heat.
presence.py decides *when*; this just does on/off with a short fade.

Lazy import of gpiozero so the module loads on macOS dev. If the pin can't be
opened (gpiozero missing, not on a Pi), every call is a silent no-op and the
panel stays however it's wired — i.e. on. The feature is fail-safe-open.

Wiring: GPIO18 must drive the LED pin. If that pin is a logic-level backlight
*enable* (common on these boards) a GPIO drives it directly; if it's the raw
LED drawing more than ~16 mA, put an N-channel MOSFET between them
(gate <- GPIO18, drain <- LED pin, source <- GND).
"""

import time

GPIO_DEFAULT      = 18
FADE_SECS_DEFAULT = 0.4
_FADE_STEPS       = 16


class Backlight:
    def __init__(self, gpio=GPIO_DEFAULT, fade_secs=FADE_SECS_DEFAULT):
        self.gpio = int(gpio)
        self.fade_secs = float(fade_secs)
        self._led = None
        self.available = False
        self._level = 1.0
        try:
            from gpiozero import PWMLED
            self._led = PWMLED(self.gpio)
            self._led.value = 1.0          # start on
            self.available = True
        except Exception as e:
            print(f"backlight: GPIO{self.gpio} unavailable ({e}); "
                  f"backlight left as-wired (on)")

    def _ramp(self, target):
        if not self.available:
            return
        target = max(0.0, min(1.0, float(target)))
        if self.fade_secs <= 0:
            self._led.value = target
            self._level = target
            return
        start = self._level
        for i in range(1, _FADE_STEPS + 1):
            self._led.value = start + (target - start) * (i / _FADE_STEPS)
            time.sleep(self.fade_secs / _FADE_STEPS)
        self._level = target

    def wake(self):
        self._ramp(1.0)

    def sleep(self):
        self._ramp(0.0)

    @property
    def is_on(self):
        return self._level > 0.0

    def close(self):
        # Leave the panel ON when the service stops — a dark panel on a
        # stopped service looks broken.
        try:
            if self.available:
                self._led.value = 1.0
                self._led.close()
        except Exception:
            pass
