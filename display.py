"""
display.py — Renderer for the hangar Pi's 3.5" ST7796U IPS SPI dashboard.

Produces a 480x320 PIL.Image from a state dict. Pushed to the physical display
by display_loop.py (driven via luma.lcd over SPI). Distinct from switch.py's
web UI — both surface the same underlying data, but the OLED is glance-only
and emphasises different things: heater state + tappable toggle, multi-stage
FlashAir status, schedule preview. Hangar temp / HVAC are demoted to a footer
since they're redundant with the HVAC remote and wall unit.

Native 480x320 (3:2) layout — replaces the previous 320x240 (4:3) build that
ran on the MSP3218 ILI9341 panel. Positions and font sizes are tuned for the
new panel; coordinate constants are NOT scaled from the old layout at render
time, they're authored directly.

State dict shape:
    {
      "heater": {"on": bool, "next_event": {action, epoch, label} | None},
      "flashair": {
          "epoch": int,
          "stage": "idle" | "scanning"
                 | "downloading_logs" | "downloading_shots"
                 | "uploading_logs"   | "uploading_shots"
                 | "error",
          "current_ssid": str | None,        # None → "no wifi" red indicator
          "files_done": int,                 # progress within current stage
          "files_total": int,
          "session_csv_n": int,              # logs being processed this cycle
          "session_shots_n": int,            # shots being processed this cycle
          "current_file": str | None,
          "last_sync_epoch": int | None,
          "last_sync_files_n": int,
          "last_shot_sync_epoch": int | None,
          "last_shot_sync_files_n": int,
          "stale": bool,
      } | None,
      "hvac": {"mode": str, "target_f": float|None, "power_w": int} | None,
      "hangar_f": float | None,
      "now_epoch": int,
    }

PIL is a lazy import so this module can be imported on a Pi that hasn't
installed Pillow yet (matches hvac.py's msmart pattern).
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIDTH  = 480
HEIGHT = 320

# Sync within this window renders as the bright "done" treatment; older becomes
# a muted "idle" line. 10 min is long enough to catch the user's post-flight
# walk to the hangar door but short enough that "done" feels meaningful.
DONE_WINDOW_SECS = 600

# Past this, the "updated Xs ago" indicator in the FlashAir header turns red —
# the display can't see fresh status from flashair-sync (the service itself
# may have stopped, the cache file may be missing).
STALE_AFTER_SECS = 120

COLOR = {
    "bg":              (250, 250, 252),
    "fg":              (20, 22, 28),
    "dim":             (110, 115, 125),
    "very_dim":        (175, 180, 190),
    "rule":            (215, 220, 230),
    "heater_on_bg":    (235, 40, 45),    # punchy saturated red — heater is ON
    "heater_on_fg":    (255, 255, 255),
    "heater_off_bg":   (25, 155, 70),    # deep emerald green — heater is OFF (safe)
    "heater_off_fg":   (255, 255, 255),
    "heater_border":   (160, 165, 175),
    "sched":           (50, 55, 65),
    "flash_active":    (215, 130, 25),   # amber/orange, readable on white
    "flash_done":      (35, 160, 75),    # green, mirrors OFF for "good state"
    "flash_idle":      (135, 140, 150),
    "flash_error":     (215, 55, 50),    # red, mirrors ON for "attention"
    "flash_stale":     (215, 55, 50),
    "flash_context":   (95, 100, 110),
    "ssid_hangar":     (110, 115, 125),
    "ssid_flashair":   (215, 130, 25),
    "ssid_none":       (215, 55, 50),
    "hvac_heat":       (220, 80, 50),
    "hvac_cool":       (40, 130, 220),
    "hvac_freeze":     (215, 150, 30),
}

# Font search paths. Pi/Debian first (the deployment target), macOS second
# (for local mockup generation during development). PIL's default bitmap font
# is the last-resort fallback so this module always renders something.
_SANS_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
_SANS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
_MONO = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]


# ---------------------------------------------------------------------------
# Font loading — lazy, cached per (path, size)
# ---------------------------------------------------------------------------

_font_cache = {}


def _font(paths, size):
    from PIL import ImageFont
    key = (tuple(paths), size)
    if key in _font_cache:
        return _font_cache[key]
    font = None
    for p in paths:
        if Path(p).exists():
            try:
                font = ImageFont.truetype(p, size)
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def fmt_age(secs):
    if secs is None:
        return "—"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d"


def has_seconds_resolution(state):
    """True if any field in this state would render fmt_age() in the seconds
    branch (< 60s). The display loop uses this to tighten the refresh interval
    so the visible 'Xs ago' counter ticks every second instead of jumping in
    5-second steps. Only the FlashAir block has seconds-resolution callers."""
    fa = state.get("flashair")
    if fa is None:
        return False
    now = state.get("now_epoch", 0)
    for key in ("epoch", "last_sync_epoch"):
        t = fa.get(key)
        if t is not None and 0 <= now - t < 60:
            return True
    return False


def fmt_until(secs):
    if secs is None or secs < 0:
        return ""
    if secs < 3600:
        return f"in {secs // 60}m"
    h = secs // 3600
    m = (secs % 3600) // 60
    return f"in {h}h{m:02d}m"


# ---------------------------------------------------------------------------
# FlashAir status → renderable text + visual treatment
# ---------------------------------------------------------------------------

def flashair_lines(fa):
    """
    Returns (headline, color, sub, context, has_progress_bar) for the FlashAir section.

    - sub: filename of the in-flight transfer, or the "go look at the problem"
      message for error/stale states. Rendered in dim mono just under the headline.
    - context: the queued-or-done line ("N shots queued" / "N logs done") that
      surfaces the other pipeline's state during active stages. Rendered in
      flash_context color below the filename. None when there's no useful
      cross-pipeline signal.

    `fa` may be None (FlashAir not configured) — caller skips rendering.
    """
    if fa is None:
        return ("not configured", COLOR["flash_idle"], None, None, False)

    if fa.get("stale"):
        return ("sync service down", COLOR["flash_stale"], "go look at the problem", None, False)

    stage = fa.get("stage", "idle")
    fd = fa.get("files_done", 0)
    ft = fa.get("files_total", 0)
    cf = fa.get("current_file")
    last = fa.get("last_sync_epoch")
    now = fa.get("epoch", 0)
    age_since_sync = (now - last) if last else None
    n = fa.get("last_sync_files_n", 0)
    shot_n = fa.get("last_shot_sync_files_n", 0)
    session_csv_n = fa.get("session_csv_n", 0)
    session_shots_n = fa.get("session_shots_n", 0)

    if stage == "idle":
        if last is None and not shot_n:
            return ("idle  (no sync yet)", COLOR["flash_idle"], None, None, False)
        # Compose the breakdown — show shots only when they happened this cycle
        log_unit = "log" if n == 1 else "logs"
        if shot_n:
            shot_unit = "shot" if shot_n == 1 else "shots"
            done_text = f"{n} {log_unit} + {shot_n} {shot_unit}"
        else:
            done_text = f"{n} {log_unit}"
        # Age goes on the sub line — the headline ran past available width on
        # the old 320 panel once the breakdown carried both logs and shots
        # ("done — 6 logs + 1 shot, 28s ago" clipped). 480 has room but we
        # keep the split for consistency and to leave headroom for longer
        # breakdowns in future contracts.
        ago = f"{fmt_age(age_since_sync)} ago" if age_since_sync is not None else None
        if age_since_sync is not None and age_since_sync < DONE_WINDOW_SECS:
            return (f"done — {done_text}",
                    COLOR["flash_done"], ago, None, False)
        return (f"idle — {done_text}",
                COLOR["flash_idle"], ago, None, False)
    if stage == "scanning":
        return ("scanning card", COLOR["flash_active"], None, None, False)
    if stage == "downloading_logs":
        shot_word = "shot" if session_shots_n == 1 else "shots"
        ctx = f"{session_shots_n} {shot_word} queued" if session_shots_n else None
        return (f"downloading  {fd} of {ft} logs",
                COLOR["flash_active"], cf, ctx, True)
    if stage == "downloading_shots":
        log_word = "log" if session_csv_n == 1 else "logs"
        ctx = f"{session_csv_n} {log_word} done" if session_csv_n else None
        return (f"downloading  {fd} of {ft} shots",
                COLOR["flash_active"], cf, ctx, True)
    if stage == "uploading_logs":
        shot_word = "shot" if session_shots_n == 1 else "shots"
        ctx = f"{session_shots_n} {shot_word} queued" if session_shots_n else None
        return (f"uploading  {fd} of {ft} logs",
                COLOR["flash_active"], cf, ctx, True)
    if stage == "uploading_shots":
        log_word = "log" if session_csv_n == 1 else "logs"
        ctx = f"{session_csv_n} {log_word} done" if session_csv_n else None
        return (f"uploading  {fd} of {ft} shots",
                COLOR["flash_active"], cf, ctx, True)
    # Back-compat: pre-stage v0 contract inferred "downloading" from the
    # transferring boolean (CSV-only). Render the same way the old display did.
    if stage == "downloading":
        return (f"downloading from card  {fd} of {ft}",
                COLOR["flash_active"], cf, None, True)
    if stage == "uploading":
        return (f"uploading to remote  {fd} of {ft}",
                COLOR["flash_active"], cf, None, True)
    if stage == "error":
        return ("error", COLOR["flash_error"], "go look at the problem", None, False)
    # Unknown stage value — render literally so it's visible during dev.
    return (stage, COLOR["fg"], None, None, False)


def ssid_color(ssid, flashair_pattern=None):
    """
    Pick a color for the SSID label.

    `flashair_pattern` is an env-configurable substring; if the SSID contains
    it (case-insensitive), the SSID renders as 'active' (amber). Anything else
    is 'normal' (grey). A null SSID renders red — wifi is down entirely.
    """
    if ssid is None:
        return COLOR["ssid_none"]
    if flashair_pattern and flashair_pattern.lower() in ssid.lower():
        return COLOR["ssid_flashair"]
    return COLOR["ssid_hangar"]


# Stages where the daemon is working the FlashAir card directly. During these
# the WiFi indicator reads "connected flashair" regardless of a transient None
# SSID (a re-association blip), rather than flashing a misleading "no wifi".
_ON_CARD_STAGES = {"checking_logs", "downloading_logs", "downloading_shots"}


def _ssid_indicator(ssid, stage, flashair_pattern=None):
    """Return (label, color) for the WiFi line in the FlashAir header.

    The raw card SSID is noise — the user already configured it — and the bare
    None the radio reports mid-association rendered as an alarming "no wifi".
    So collapse the card's networks to plain status text:

    - on the card (SSID matches the pattern, or mid-transfer from it)
        → "connected flashair"
    - hopping onto the card (scanning with the radio between networks)
        → "connecting flashair"
    - otherwise → the home SSID ('on "Hangar-WiFi"'), or "no wifi" if the
        radio is genuinely down outside a card session.
    """
    on_card = bool(
        flashair_pattern and ssid and flashair_pattern.lower() in ssid.lower()
    )
    if on_card or stage in _ON_CARD_STAGES:
        return ("connected flashair", COLOR["ssid_flashair"])
    if stage == "scanning" and not ssid:
        return ("connecting flashair", COLOR["ssid_flashair"])
    label = f'on "{ssid}"' if ssid else "no wifi"
    return (label, ssid_color(ssid, flashair_pattern))


def hvac_label(hvac):
    """Returns (label, color) for the HVAC footer, or (None, None) if HVAC is off / not configured."""
    if hvac is None:
        return (None, None)
    mode = hvac.get("mode", "off")
    target = hvac.get("target_f")
    pw = hvac.get("power_w", 0)
    color = {
        "heat":   COLOR["hvac_heat"],
        "cool":   COLOR["hvac_cool"],
        "freeze": COLOR["hvac_freeze"],
        "off":    COLOR["dim"],
    }.get(mode, COLOR["fg"])
    label = f"{mode.upper()}→{target:.0f}°F · {pw}W" if target else f"{mode.upper()} · {pw}W"
    return (label, color)


# ---------------------------------------------------------------------------
# Main render entry point
# ---------------------------------------------------------------------------

# Geometry for the heater touch button — exposed so the touch handler in
# display_loop.py can hit-test taps against the same rectangle. The
# capacitive FT6336U returns coords in this same 480x320 space, so no
# scaling is needed in display_loop.py.
HEATER_BUTTON_RECT = (15, 30, 225, 122)  # (x1, y1, x2, y2) — 210×92 button

# Filename truncation in the FlashAir "sub" line. f_mono_sm at 14pt is ~8.5px
# per char; with 15px left padding and 15px right safety, we have ~450px of
# usable width → ~52 chars. 50 leaves a margin for unusually wide glyphs.
_SUB_MAX_CHARS = 50


def render(state, flashair_ssid_pattern=None):
    """
    Build the 480x320 dashboard image. `state` shape documented in module
    docstring. `flashair_ssid_pattern` is the .env-configured substring used
    to color SSIDs that match (e.g. "flashair") as active.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT), COLOR["bg"])
    d = ImageDraw.Draw(img)

    f_label      = _font(_SANS_REG, 16)
    f_btn        = _font(_SANS_BOLD, 48)
    f_sched      = _font(_SANS_REG, 19)
    f_sched_sm   = _font(_SANS_REG, 14)
    f_flash_head = _font(_SANS_BOLD, 24)
    f_flash_ctx  = _font(_SANS_REG, 16)
    f_mono_sm    = _font(_MONO, 14)
    f_footer     = _font(_SANS_REG, 17)
    f_tiny       = _font(_SANS_REG, 13)

    now = state.get("now_epoch", 0)

    # ----- Heater section --------------------------------------------------
    d.text((15, 8), "ENGINE HEATER", fill=COLOR["dim"], font=f_label)

    heater = state.get("heater", {}) or {}
    h_on = bool(heater.get("on"))
    btn_bg = COLOR["heater_on_bg"] if h_on else COLOR["heater_off_bg"]
    btn_fg = COLOR["heater_on_fg"] if h_on else COLOR["heater_off_fg"]
    x1, y1, x2, y2 = HEATER_BUTTON_RECT
    d.rectangle([(x1, y1), (x2, y2)], fill=btn_bg,
                outline=COLOR["heater_border"], width=2)
    text = "ON" if h_on else "OFF"
    # anchor="mm" puts the text's actual ink center at the given x,y. The
    # previous manual textbbox math ignored bbox[0]/bbox[1] (the offset from
    # the text origin to the inked area), so the glyph drifted off-center
    # by a few px vertically. PIL ≥ 8.0 supports anchor for TTF fonts.
    d.text(((x1 + x2) / 2, (y1 + y2) / 2),
           text, fill=btn_fg, font=f_btn, anchor="mm")

    next_evt = heater.get("next_event")
    d.text((240, 30), "Schedule", fill=COLOR["dim"], font=f_label)
    if next_evt is None:
        d.text((240, 55), "no events", fill=COLOR["dim"], font=f_sched)
    else:
        d.text((240, 55), f"→ {next_evt['label']}", fill=COLOR["sched"], font=f_sched)
        until = fmt_until(next_evt["epoch"] - now)
        if until:
            d.text((240, 82), until, fill=COLOR["dim"], font=f_sched_sm)

    d.line([(15, 140), (WIDTH - 15, 140)], fill=COLOR["rule"], width=1)

    # ----- FlashAir section ------------------------------------------------
    d.text((15, 150), "FLASHAIR", fill=COLOR["dim"], font=f_label)

    fa = state.get("flashair")
    if fa is not None:
        # SSID indicator: only rendered when the contract carries `current_ssid`.
        # Absent key (v0 contract) → suppress indicator. None value (v1, wifi
        # actually down) → "no wifi" in red.
        if "current_ssid" in fa:
            ssid_label, ssid_lbl_color = _ssid_indicator(
                fa["current_ssid"], fa.get("stage", "idle"),
                flashair_ssid_pattern)
            d.text((100, 150), "·", fill=COLOR["very_dim"], font=f_label)
            d.text((115, 150), ssid_label, fill=ssid_lbl_color, font=f_label)

        age = now - fa.get("epoch", now)
        fresh_text = f"updated {fmt_age(age)} ago"
        fresh_color = COLOR["flash_stale"] if age > STALE_AFTER_SECS else COLOR["very_dim"]
        bbox = d.textbbox((0, 0), fresh_text, font=f_tiny)
        fw = bbox[2] - bbox[0]
        d.text((WIDTH - 15 - fw, 152), fresh_text, fill=fresh_color, font=f_tiny)

        headline, hcolor, sub, context, has_bar = flashair_lines(fa)
        d.text((15, 174), headline, fill=hcolor, font=f_flash_head)
        # When context is present, shift the filename + bar down to fit
        # the extra line. Otherwise keep the original two-row layout.
        if context:
            sub_y, context_y, bar_y = 204, 224, 246
        else:
            sub_y, context_y, bar_y = 210, None, 238

        if sub:
            sub_display = sub if len(sub) <= _SUB_MAX_CHARS else sub[: _SUB_MAX_CHARS - 1] + "…"
            d.text((15, sub_y), sub_display, fill=COLOR["dim"], font=f_mono_sm)
        if context:
            d.text((15, context_y), context,
                   fill=COLOR["flash_context"], font=f_flash_ctx)

        if has_bar:
            fd = fa.get("files_done", 0)
            ft = max(fa.get("files_total", 1), 1)
            frac = max(0.0, min(1.0, fd / ft))
            bar_right = WIDTH - 15
            bar_w = bar_right - 15
            d.rectangle([(15, bar_y), (bar_right, bar_y + 12)],
                        outline=COLOR["rule"], width=1)
            d.rectangle([(16, bar_y + 1), (16 + int((bar_w - 2) * frac), bar_y + 11)],
                        fill=COLOR["flash_active"])
    else:
        d.text((15, 174), "not configured", fill=COLOR["flash_idle"], font=f_flash_head)

    d.line([(15, 268), (WIDTH - 15, 268)], fill=COLOR["rule"], width=1)

    # ----- HVAC + hangar temp footer --------------------------------------
    # anchor="ls" (left-baseline) means all three texts baseline-align at
    # FOOTER_BASELINE_Y regardless of point size — no magic per-font y-offset
    # to maintain. Previous version used top-left anchor with hand-tuned y
    # values (HVAC label at 286 / FREEZE value at 283 / Hangar at 286) which
    # drifted visibly because the offsets weren't right for the actual font
    # ascents.
    FOOTER_BASELINE_Y = 300
    label, color = hvac_label(state.get("hvac"))
    if label is not None:
        d.text((15, FOOTER_BASELINE_Y), "HVAC", fill=COLOR["dim"], font=f_label, anchor="ls")
        d.text((65, FOOTER_BASELINE_Y), label, fill=color, font=f_footer, anchor="ls")
    hangar_f = state.get("hangar_f")
    if hangar_f is not None:
        d.text((345, FOOTER_BASELINE_Y), f"Hangar {hangar_f:.0f}°F",
               fill=COLOR["dim"], font=f_footer, anchor="ls")

    return img


# ---------------------------------------------------------------------------
# CLI — generate mockup PNGs for design review without needing the display
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time

    NOW = int(time.time())

    def fa_state(stage, **kw):
        out = {
            "epoch": NOW - 4,
            "stage": stage,
            "current_ssid": "Hangar-WiFi",
            "files_done": 0, "files_total": 0,
            "session_csv_n": 0, "session_shots_n": 0,
            "current_file": None,
            "last_sync_epoch": NOW - 3600,
            "last_sync_files_n": 12,
            "last_shot_sync_epoch": None,
            "last_shot_sync_files_n": 0,
            "stale": False,
        }
        out.update(kw)
        return out

    BASE = {
        "now_epoch": NOW,
        "heater":   {"on": False, "next_event": None},
        "hvac":     {"mode": "freeze", "target_f": 46, "power_w": 146},
        "hangar_f": 47.1,
    }

    fname = "log_YYYYMMDD_HHMMSS_KXXX.csv"
    shot = "shot_001.bmp"
    states = {
        "idle_done":           {**BASE, "flashair": fa_state(
            "idle", last_sync_epoch=NOW - 90, last_sync_files_n=7,
            last_shot_sync_epoch=NOW - 80, last_shot_sync_files_n=5)},
        "connecting":          {**BASE, "flashair": fa_state(
            "scanning", current_ssid=None)},
        "downloading_logs":    {**BASE, "flashair": fa_state(
            "downloading_logs", current_ssid="FlashAir-Card",
            files_done=3, files_total=7,
            session_csv_n=7, session_shots_n=5,
            current_file=fname)},
        "downloading_shots":   {**BASE, "flashair": fa_state(
            "downloading_shots", current_ssid="FlashAir-Card",
            files_done=2, files_total=5,
            session_csv_n=7, session_shots_n=5,
            current_file=shot)},
        "uploading_logs":      {**BASE, "flashair": fa_state(
            "uploading_logs",
            files_done=4, files_total=7,
            session_csv_n=7, session_shots_n=5,
            current_file=fname)},
        "uploading_shots":     {**BASE, "flashair": fa_state(
            "uploading_shots",
            files_done=3, files_total=5,
            session_csv_n=7, session_shots_n=5,
            current_file=shot)},
        "sync_down":           {**BASE, "flashair": fa_state("idle", stale=True, epoch=NOW - 600)},
        "no_flashair":         {**BASE, "flashair": None},
        "heater_on":           {**BASE, "flashair": fa_state(
            "idle", last_sync_epoch=NOW - 60, last_sync_files_n=7,
            last_shot_sync_epoch=NOW - 50, last_shot_sync_files_n=5),
            "heater": {"on": True,
                       "next_event": {"action": "off", "epoch": NOW + 4320,
                                      "label": "OFF at 7:30 AM"}}},
    }

    out_dir = os.environ.get("DISPLAY_PREVIEW_DIR", "/tmp")
    for name, st in states.items():
        img = render(st, flashair_ssid_pattern="flashair")
        # Doubled-up preview so design review at native screen res isn't
        # squinting at a 480px image at 2x retina scaling.
        img.resize((WIDTH * 2, HEIGHT * 2)).save(f"{out_dir}/display-preview-{name}.png")
        print(f"{out_dir}/display-preview-{name}.png")
