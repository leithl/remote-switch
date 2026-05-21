#!/usr/bin/env python3
"""
switch.py — WSGI application (Apache mod_wsgi) with CGI fallback.

mod_wsgi keeps this process alive between requests, so Python startup and
module imports only happen once — eliminating the ~10s CGI startup cost on
a Pi Zero W.

When invoked directly (python3 switch.py) it falls back to CGI via wsgiref.
"""

import html
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

# Ensure local modules are importable when run as WSGI/CGI
sys.path.insert(0, str(Path(__file__).parent))

import aggregate
import config
import flashair
import hvac

try:
    from markupsafe import Markup
except ImportError:
    Markup = str  # type: ignore[assignment,misc]  # fallback for dev environments without jinja2

# Module-level Jinja2 environment — created once per process, reused on every request.
_jinja_env = None


def _get_jinja_env():
    global _jinja_env
    if _jinja_env is None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        _jinja_env = Environment(
            loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
            autoescape=select_autoescape(["html"]),
        )
    return _jinja_env


# ---------------------------------------------------------------------------
# Early-exit exception (replaces sys.exit + sys.stdout in CGI)
# ---------------------------------------------------------------------------

class _Response(Exception):
    """Raised to short-circuit request handling with an immediate response."""
    def __init__(self, status, headers, body=b""):
        self.status = status
        self.headers = list(headers)
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

def _qs(environ):
    """Return parsed query string dict (first value per key)."""
    raw = environ.get("QUERY_STRING", "")
    params = urllib.parse.parse_qs(raw, keep_blank_values=False)
    return {k: v[0] for k, v in params.items()}


def _redirect(location):
    raise _Response("303 See Other", [("Location", location)])


def _respond(content_type, body):
    raise _Response("200 OK", [("Content-Type", content_type)], body)


# ---------------------------------------------------------------------------
# Month helpers
# ---------------------------------------------------------------------------

def _month_bounds(d):
    """Return (start_epoch, end_epoch) for the calendar month of date d."""
    start = datetime(d.year, d.month, 1)
    end_d = config.subtract_months(d, -1)
    end = datetime(end_d.year, end_d.month, 1)
    return int(start.timestamp()), int(end.timestamp())


def _month_label(d):
    return d.strftime("%b %Y")


# ---------------------------------------------------------------------------
# Stats data builder
# ---------------------------------------------------------------------------

def _month_data(mk, label, ts, as_, rs, t_pct):
    """Return structured stats dict for one month (consumed by the transposed template table)."""
    if rs:
        heater_str = f'{rs["on_hrs"]:.1f}h ({rs["avg_hrs_day"]:.1f}/day)'
        fan_str = f'{rs["fan_on_hrs"]:.1f}h' if "fan_on_hrs" in rs else None
    else:
        heater_str = None
        fan_str = None
    return {
        "mk": mk,
        "label": label,
        "coverage_pct": t_pct,
        "hangar": ts,
        "outdoor": as_,
        "heater_str": heater_str,
        "fan_str": fan_str,
    }


# ---------------------------------------------------------------------------
# Old-cache row helpers (from migrate.py, pre-rendered HTML from .dat files)
# ---------------------------------------------------------------------------

def _compute_months_data(conn, now_epoch=None):
    """Build the 13-month stats list. Slow on Pi Zero W (~6s SQL scan), so the
    main page render skips this — clients fetch it via ?monthly_stats=1 after
    page load. Reused from both the lazy endpoint and the full-page render."""
    if now_epoch is None:
        now_epoch = int(datetime.now().timestamp())
    cur_month_date = date.today().replace(day=1)
    months_data = []

    month_meta = []
    for i in range(13):
        md = config.subtract_months(cur_month_date, i)
        mk = md.strftime("%Y-%m")
        lbl = _month_label(md)
        m_start, m_end = _month_bounds(md)
        is_current = md == cur_month_date
        effective_end = min(m_end, now_epoch) if is_current else m_end
        month_meta.append((mk, lbl, m_start, m_end, effective_end, is_current))

    all_mks = [m[0] for m in month_meta]
    placeholders = ",".join("?" * len(all_mks))
    cache_map = {
        row[0]: json.loads(row[1])
        for row in conn.execute(
            f"SELECT month, data FROM monthly_cache WHERE month IN ({placeholders})",
            all_mks,
        ).fetchall()
    }

    live_month_meta = [m for m in month_meta if m[5] or m[0] not in cache_map]
    live_batch_stats = {}
    if live_month_meta:
        batch_start = min(m[2] for m in live_month_meta)
        batch_end = max(m[4] for m in live_month_meta)
        live_batch_stats = config.query_batch_stats(conn, batch_start, batch_end)

    for mk, lbl, m_start, m_end, effective_end, is_current in month_meta:
        cached = cache_map.get(mk) if not is_current else None
        if cached:
            if "_html_temp" in cached:
                continue
            ts = cached.get("temp_stats")
            as_ = cached.get("ambient_stats")
            rs = cached.get("runtime_stats")
            if ts and rs:
                t_pct = rs.get("temp_coverage_pct", 0)
                months_data.append(_month_data(mk, lbl, ts, as_, rs, t_pct))
        else:
            sql_row = live_batch_stats.get(mk)
            if not sql_row:
                continue
            ts, as_, rs = _build_stats(sql_row, m_start, effective_end)
            if ts and rs:
                t_pct = rs.get("temp_coverage_pct", 0)
                months_data.append(_month_data(mk, lbl, ts, as_, rs, t_pct))
    return months_data


def _old_cache_temp_row(mk, html_row):
    """Add onclick to a pre-rendered HTML temp row from old .dat cache."""
    row = html_row.replace(
        "<tr>",
        f'<tr onclick="location=\'switch.py?range={html.escape(mk)}\'" style="cursor:pointer">',
        1,
    )
    return Markup(row)


def _old_cache_ambient_row(mk, html_row):
    """Add onclick and 'Outdoor' label to a pre-rendered ambient stat row."""
    import re
    row = html_row.replace(
        "<tr>",
        f'<tr onclick="location=\'switch.py?range={html.escape(mk)}\'" style="cursor:pointer">',
        1,
    )
    row = re.sub(r'<td>[^<]*</td>', '<td class="ps-3 text-muted small">Outdoor</td>', row, count=1)
    return Markup(row)


# ---------------------------------------------------------------------------
# WSGI entry point
# ---------------------------------------------------------------------------

def application(environ, start_response):
    """WSGI callable — mod_wsgi calls this for every request."""
    try:
        status, headers, body = _handle(environ)
    except _Response as r:
        start_response(r.status, r.headers)
        return [r.body]
    start_response(status, headers)
    return [body]


# ---------------------------------------------------------------------------
# Main request handler
# ---------------------------------------------------------------------------

def _handle(environ):
    """Handle one request. Returns (status_str, headers_list, body_bytes)."""
    qs = _qs(environ)

    # --- Manifest ---
    if qs.get("manifest") == "1":
        manifest = {
            "name": "Heater Control",
            "short_name": "Heater",
            "start_url": "switch.py",
            "display": "standalone",
            "background_color": "#212529",
            "theme_color": "#212529",
            "icons": [{"src": "switch.py?icon=192", "sizes": "any", "type": "image/svg+xml"}],
        }
        _respond("application/manifest+json", json.dumps(manifest))

    # --- Icon ---
    if qs.get("icon") in ("192", "512"):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
            '<rect width="512" height="512" rx="80" fill="#212529"/>'
            '<path d="M256 60c-50 0-90 70-90 150 0 60 30 110 60 130v32c0 17 13 30 30 30'
            "s30-13 30-30v-32c30-20 60-70 60-130 0-80-40-150-90-150zm0 60c25 0 50 45 50 90"
            " 0 35-15 65-35 80h-30c-20-15-35-45-35-80 0-45 25-90 50-90z"
            '" fill="#ff6420"/>'
            '<path d="M256 140c-15 0-30 30-30 60 0 25 10 45 20 55h20c10-10 20-30 20-55'
            ' 0-30-15-60-30-60z" fill="#ffc832"/>'
            '</svg>'
        )
        _respond("image/svg+xml", svg)

    # --- METAR / TAF proxy (avoids CORS; lazy-loaded by client JS) ---
    if qs.get("metar") == "1" or qs.get("taf") == "1":
        import json as _json
        import math

        is_taf = qs.get("taf") == "1"
        product = "taf" if is_taf else "metar"
        wx_lat, wx_lon, _ = config.get_location()
        wx_id = config.load_env().get("LOCATION", "").strip()
        if wx_id and len(wx_id) == 3:
            wx_id = "K" + wx_id

        result = ""

        # Try station ID first
        if wx_id:
            try:
                url = f"https://aviationweather.gov/api/data/{product}?ids={wx_id}&format=raw&hours=1"
                req = urllib.request.urlopen(url, timeout=5)
                text = req.read().decode("utf-8", errors="replace").strip()
                if text:
                    result = text.split("\n")[0] if not is_taf else text
            except Exception:
                pass

        # Bbox fallback — for METAR use raw; for TAF use json to check distance
        if not result and wx_lat and wx_lon:
            la, lo = float(wx_lat), float(wx_lon)
            d = 0.35  # ~21nm along axis
            try:
                if is_taf:
                    url = (f"https://aviationweather.gov/api/data/taf"
                           f"?bbox={la-d},{lo-d},{la+d},{lo+d}&format=json")
                    req = urllib.request.urlopen(url, timeout=5)
                    data = _json.loads(req.read().decode())
                    best, best_nm = None, 999
                    for item in data:
                        slat, slon = item.get("lat", 0), item.get("lon", 0)
                        dlat = math.radians(slat - la)
                        dlon = math.radians(slon - lo)
                        a = (math.sin(dlat/2)**2 +
                             math.cos(math.radians(la)) *
                             math.cos(math.radians(slat)) *
                             math.sin(dlon/2)**2)
                        nm = 3440.065 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
                        if nm < best_nm:
                            best_nm, best = nm, item
                    if best and best_nm <= 20:
                        result = best.get("rawTAF", best.get("rawOb", ""))
                else:
                    url = (f"https://aviationweather.gov/api/data/metar"
                           f"?bbox={la-d},{lo-d},{la+d},{lo+d}&format=raw&hours=1")
                    req = urllib.request.urlopen(url, timeout=5)
                    text = req.read().decode("utf-8", errors="replace").strip()
                    result = text.split("\n")[0] if text else ""
            except Exception:
                pass

        if not wx_id and not (wx_lat and wx_lon):
            result = ""
        _respond("text/plain", result)

    # --- Lazy-loaded HVAC card body (~3.8s for dongle round-trip) ---
    # The main page render skips hvac.get_state() and shows a spinner;
    # client-side JS fetches this and swaps it into #hvac-card-body.
    if qs.get("hvac_state") == "1":
        hvac_configured = hvac.is_configured()
        hvac_state = hvac.get_state() if hvac_configured else None
        tmpl = _get_jinja_env().get_template("_hvac_card.html")
        body_html = tmpl.render(
            hvac_configured=hvac_configured,
            hvac_state=hvac_state,
            hvac_modes=[(m, hvac.MODE_LABELS[m]) for m in hvac.ALL_MODES],
            hvac_fans=[(f, hvac.FAN_LABELS[f]) for f in hvac.ALL_FANS],
            hvac_temp_min_f=hvac.TEMP_MIN_F,
            hvac_temp_max_f=hvac.TEMP_MAX_F,
            hvac_mode_freeze=hvac.MODE_FREEZE,
        )
        power_on = bool(
            hvac_state and hvac_state.get("reported")
            and hvac_state["reported"].get("power")
        )
        _respond("application/json", json.dumps({"power_on": power_on, "html": body_html}))

    # --- Lazy-loaded monthly stats table (~6.7s SQL scan on Pi Zero W) ---
    # Same pattern: main page skips it, client JS fetches and swaps into
    # #monthly-stats-body. Drops main TTFB by ~6s.
    if qs.get("monthly_stats") == "1":
        conn = config.get_db()
        try:
            months_data = _compute_months_data(conn)
        finally:
            conn.close()
        tmpl = _get_jinja_env().get_template("_monthly_stats.html")
        _respond("text/html", tmpl.render(months_data=months_data))

    # --- GPIO check ---
    gpio_path = config._gpio_path()
    if not gpio_path.exists():
        raise _Response(
            "200 OK",
            [("Content-Type", "text/plain")],
            f"Error: GPIO pin {config.GPIO_PIN} is not configured ({gpio_path} not found)",
        )

    # --- State change (turn on/off) ---
    state = qs.get("state", "")
    if state in ("0", "1"):
        config.write_gpio(state)
        _redirect("switch.py")

    # --- Fan manual override ---
    fan_state_raw = qs.get("fan_state", "")
    if fan_state_raw in ("0", "1"):
        config.write_fan_mode("on" if fan_state_raw == "1" else "off")
        try:
            config.write_fan_gpio(fan_state_raw)
        except (PermissionError, OSError):
            pass
        _redirect("switch.py")

    fan_mode_raw = qs.get("fan_mode", "")
    if fan_mode_raw == "auto":
        config.write_fan_mode("auto")
        _redirect("switch.py")

    # --- HVAC apply (immediate state change for Hangar HVAC) ---
    if qs.get("hvac_apply") == "1" and hvac.is_configured():
        hvac_mode = qs.get("hvac_mode", "")
        if hvac_mode == hvac.MODE_FREEZE:
            hvac.set_state(mode=hvac.MODE_FREEZE)
        else:
            try:
                target_f = float(qs.get("hvac_target_f", "")) if qs.get("hvac_target_f") else None
            except ValueError:
                target_f = None
            hvac.set_state(
                power=(qs.get("hvac_power") == "1") if qs.get("hvac_power") in ("0", "1") else None,
                mode=hvac_mode if hvac_mode in hvac.ALL_MODES else None,
                target_f=target_f,
                fan_speed=qs.get("hvac_fan_speed") if qs.get("hvac_fan_speed") in hvac.ALL_FANS else None,
            )
        _redirect("switch.py")

    # --- Schedule add ---
    now_epoch = int(datetime.now().timestamp())
    sched_msg = Markup("")

    sched_dt_raw  = qs.get("sched_dt", "")
    sched_device  = qs.get("sched_device", "heater")
    sched_action  = qs.get("sched_action", "")
    if sched_dt_raw and (sched_device == "hvac" or sched_action in ("0", "1")):
        sched_dt_decoded = urllib.parse.unquote_plus(sched_dt_raw)
        try:
            sched_epoch = int(
                datetime.strptime(sched_dt_decoded, "%Y-%m-%dT%H:%M").timestamp()
            )
            if sched_epoch <= now_epoch:
                sched_msg = Markup(
                    '<div class="alert alert-warning alert-sm py-1 mb-2">'
                    "Cannot schedule in the past.</div>"
                )
            elif sched_device == "hvac":
                hvac_mode = qs.get("sched_hvac_mode", "")
                if hvac_mode not in hvac.ALL_MODES:
                    sched_msg = Markup(
                        '<div class="alert alert-danger alert-sm py-1 mb-2">'
                        "Invalid HVAC mode.</div>"
                    )
                else:
                    try:
                        sched_target_f = float(qs.get("sched_hvac_target_f", "")) \
                            if qs.get("sched_hvac_target_f") else None
                    except ValueError:
                        sched_target_f = None
                    sched_fan = qs.get("sched_hvac_fan_speed", "auto")
                    if sched_fan not in hvac.ALL_FANS:
                        sched_fan = "auto"
                    sched_power = qs.get("sched_hvac_power") == "1"
                    if hvac_mode == hvac.MODE_FREEZE:
                        params = {"mode": hvac.MODE_FREEZE}
                    else:
                        params = {
                            "power": sched_power,
                            "mode": hvac_mode,
                            "fan_speed": sched_fan,
                        }
                        if sched_target_f is not None:
                            params["target_f"] = sched_target_f
                    conn = config.get_db()
                    conn.execute(
                        "INSERT OR REPLACE INTO schedules "
                        "(created_epoch, execute_epoch, action, device, params) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (now_epoch, sched_epoch, "set", "hvac", json.dumps(params)),
                    )
                    conn.commit()
                    conn.close()
                    _redirect("switch.py")
            else:
                conn = config.get_db()
                conn.execute(
                    "INSERT OR REPLACE INTO schedules "
                    "(created_epoch, execute_epoch, action, device, params) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now_epoch, sched_epoch, sched_action, "heater", ""),
                )
                conn.commit()
                conn.close()
                _redirect("switch.py")
        except ValueError:
            sched_msg = Markup(
                '<div class="alert alert-danger alert-sm py-1 mb-2">'
                "Invalid date/time.</div>"
            )

    # --- Schedule cancel ---
    cancel_id_raw = qs.get("cancel_id", "")
    if cancel_id_raw and cancel_id_raw.isdigit():
        cancel_id = int(cancel_id_raw)
        conn = config.get_db()
        conn.execute("DELETE FROM schedules WHERE created_epoch = ?", (cancel_id,))
        conn.commit()
        conn.close()
        _redirect("switch.py")

    # -----------------------------------------------------------------------
    # Page render
    # -----------------------------------------------------------------------
    enable_temp = config.ENABLE_TEMP

    # --- Heater state ---
    heater_on = config.read_gpio() == "1"
    status = "on" if heater_on else "off"
    header_class = "text-bg-success" if heater_on else "text-bg-secondary"
    toggle_btn = Markup(
        '<button type="submit" name="state" class="btn btn-danger btn-lg" value="0">turn off</button>'
        if heater_on else
        '<button type="submit" name="state" class="btn btn-success btn-lg" value="1">turn on</button>'
    )

    # --- Temperature display ---
    temp_display = ""
    if enable_temp:
        temp_c = config.read_temp()
        if temp_c is None:
            temp_display = "Temperature probe not found"
        else:
            temp_f = temp_c * 1.8 + 32
            temp_display = f"{temp_c:.1f} \u00b0C | {temp_f:.1f} \u00b0F"

    # --- Fan state ---
    fan_on = config.read_fan_gpio() == "1"
    fan_mode = config.read_fan_mode()
    fan_status = "on" if fan_on else "off"
    fan_header_class = "text-bg-info" if fan_on else "text-bg-secondary"
    fan_toggle_btn = Markup(
        '<button type="submit" name="fan_state" class="btn btn-danger btn-lg" value="0">turn off</button>'
        if fan_on else
        '<button type="submit" name="fan_state" class="btn btn-success btn-lg" value="1">turn on</button>'
    )

    # --- Ambient label + current ambient temp ---
    amb_lat, amb_lon, _ = config.get_location()
    ambient_display = ""
    if enable_temp and amb_lat and amb_lon:
        amb_c = config.fetch_ambient(amb_lat, amb_lon)
        if amb_c is not None:
            amb_f = amb_c * 1.8 + 32
            ambient_display = f"{amb_c:.1f} \u00b0C | {amb_f:.1f} \u00b0F"

    # --- FlashAir sync status (opt-in via FLASHAIR_STATUS_URL in .env) ---
    # Reads the cron-written /run/heater-flashair.json. None when not
    # configured or no cache exists yet; the template skips the line.
    flashair_display = flashair.display_text() if flashair.is_configured() else None

    # --- HVAC state — lazy-loaded by client JS via ?hvac_state=1 ---
    # The dongle round-trip can take 3-4s on cache miss; deferring it drops
    # main TTFB. is_configured() is a cheap .env check, kept here so the
    # template can render the spinner skeleton (vs "Not configured" text).
    hvac_configured = hvac.is_configured()

    # --- Pending schedules ---
    conn = config.get_db()
    pending_rows = conn.execute(
        "SELECT created_epoch, execute_epoch, action, "
        "COALESCE(device, 'heater'), COALESCE(params, '') "
        "FROM schedules ORDER BY execute_epoch"
    ).fetchall()
    conn.close()

    pending_sched_rows = []
    for created_epoch, execute_epoch, action, device, params in pending_rows:
        if device == "hvac":
            action_label = _hvac_sched_label(params)
            badge = '<span class="badge bg-info text-dark me-1">HVAC</span>'
        else:
            action_label = "Turn ON" if action == "1" else "Turn OFF"
            badge = '<span class="badge bg-secondary me-1">Heater</span>'
        sched_time = datetime.fromtimestamp(execute_epoch).strftime("%b %d, %Y %I:%M %p")
        cancel_url = f"switch.py?cancel_id={created_epoch}"
        pending_sched_rows.append(Markup(
            f'<tr><td>{html.escape(sched_time)}</td>'
            f'<td>{badge}{html.escape(action_label)}</td>'
            f'<td><a href="{html.escape(cancel_url)}" '
            f'class="btn btn-sm btn-outline-danger py-0">Cancel</a></td></tr>'
        ))

    # --- Range / chart ---
    range_param = qs.get("range", "7d")
    if range_param not in ("1d", "7d", "30d") and not _is_valid_month(range_param):
        range_param = "7d"

    # 1d view uses 60s buckets so the power line shows compressor cycles.
    # Other ranges stay at 15-min (900s) buckets — denser would be wasted
    # pixels and slower to render.
    bucket_secs = 60 if range_param == "1d" else 900

    if range_param == "1d":
        chart_cutoff = now_epoch - 86400
        chart_end_epoch = now_epoch
        chart_title = "Temperature - Last 24 Hours"
    elif range_param == "30d":
        chart_cutoff = now_epoch - 30 * 86400
        chart_end_epoch = now_epoch
        chart_title = "Temperature - Last 30 Days"
    elif _is_valid_month(range_param):
        try:
            rd = datetime.strptime(range_param + "-01", "%Y-%m-%d").date()
            chart_cutoff, chart_end_epoch = _month_bounds(rd)
            chart_title = f"Temperature - {_month_label(rd)}"
        except ValueError:
            range_param = "7d"
            chart_cutoff = now_epoch - 7 * 86400
            chart_end_epoch = now_epoch
            chart_title = "Temperature - Last 7 Days"
    else:  # 7d
        chart_cutoff = now_epoch - 7 * 86400
        chart_end_epoch = now_epoch
        chart_title = "Temperature - Last 7 Days"

    # --- Monthly stats (13 months) ---
    today = date.today()
    cur_month_date = today.replace(day=1)

    chart_data = []
    heater_ranges = []
    cold_ranges = []
    fan_ranges = []
    door_ranges = []
    ambient_data = []
    power_data = []
    months_data = []

    if enable_temp:
        conn = config.get_db()

        # Determine whether to use cached chart data
        chart_from_cache = False
        if _is_valid_month(range_param) and range_param != cur_month_date.strftime("%Y-%m"):
            cached = conn.execute(
                "SELECT data FROM monthly_cache WHERE month = ?", (range_param,)
            ).fetchone()
            if cached:
                cached_result = json.loads(cached[0])
                chart_data = cached_result.get("chart_data", [])
                heater_ranges = cached_result.get("heater_ranges", [])
                cold_ranges = cached_result.get("cold_ranges", [])
                fan_ranges = cached_result.get("fan_ranges", [])
                ambient_data = cached_result.get("ambient_data", [])
                power_data = cached_result.get("power_data", [])
                chart_from_cache = True

        if not chart_from_cache:
            bucketed = config.query_bucketed(conn, chart_cutoff, chart_end_epoch, bucket_secs=bucket_secs)
            live_result = aggregate.compute_bucketed(
                bucketed, chart_cutoff, chart_end_epoch, bucket_secs=bucket_secs
            )
            chart_data = live_result["chart_data"]
            heater_ranges = live_result["heater_ranges"]
            cold_ranges = live_result["cold_ranges"]
            fan_ranges = live_result.get("fan_ranges", [])
            ambient_data = live_result["ambient_data"]
            power_data = live_result.get("power_data", [])

            # Door-event detection runs only on the 1d / 7d ranges. It
            # needs raw per-minute (temp_c, ac_indoor_f) pairs — bucketed
            # averages would smooth out the 5-15min divergence signal —
            # so the row count is unbounded by bucket_secs. 7d × 1440 =
            # ~10k rows is fine on the Pi Zero W; 30d at 43k pushes it
            # and the events would render as overlapping pixels anyway.
            if range_param in ("1d", "7d"):
                pairs = config.query_temp_pairs(conn, chart_cutoff, chart_end_epoch)
                door_ranges = aggregate.detect_door_events(pairs)

        # Monthly stats are lazy-loaded by client JS via ?monthly_stats=1.
        # Computing them inline adds ~6.7s on a Pi Zero W (full SQL scan over
        # current month's readings + cache lookups). Skipping here drops TTFB.

        conn.close()

    # --- Render template ---
    template = _get_jinja_env().get_template("index.html")

    now_dt_min = datetime.now().strftime("%Y-%m-%dT%H:%M")
    now_str = datetime.now().strftime("%a %b %d %H:%M:%S %Z %Y")

    body = template.render(
        enable_temp=enable_temp,
        heater_on=heater_on,
        status=status,
        header_class=header_class,
        toggle_btn=toggle_btn,
        fan_on=fan_on,
        fan_status=fan_status,
        fan_mode=fan_mode,
        fan_header_class=fan_header_class,
        fan_toggle_btn=fan_toggle_btn,
        temp_display=temp_display,
        ambient_display=ambient_display,
        range=range_param,
        chart_title=chart_title,
        chart_data=chart_data,
        heater_ranges=heater_ranges,
        cold_ranges=cold_ranges,
        fan_ranges=fan_ranges,
        door_ranges=door_ranges,
        ambient_data=ambient_data,
        power_data=power_data,
        sched_msg=sched_msg,
        pending_sched_rows=pending_sched_rows,
        now_dt_min=now_dt_min,
        now_str=now_str,
        flashair_display=flashair_display,
        # HVAC — the card body (with live state + apply form) is loaded via
        # JS through ?hvac_state=1; only the static enums needed by the
        # schedule form's dropdowns and the spinner header are passed here.
        hvac_configured=hvac_configured,
        hvac_modes=[(m, hvac.MODE_LABELS[m]) for m in hvac.ALL_MODES],
        hvac_fans=[(f, hvac.FAN_LABELS[f]) for f in hvac.ALL_FANS],
        hvac_temp_min_f=hvac.TEMP_MIN_F,
        hvac_temp_max_f=hvac.TEMP_MAX_F,
        hvac_mode_freeze=hvac.MODE_FREEZE,
    )

    return (
        "200 OK",
        [("Content-Type", "text/html; charset=utf-8")],
        body.encode("utf-8"),
    )


def _build_stats(sql_row, m_start, effective_end):
    """
    Convert a query_batch_stats() tuple to (temp_stats, ambient_stats, runtime_stats) dicts.
    Returns the same dict structure as aggregate.compute() produces for those keys.
    """
    (avg_c, min_c, max_c, cold_mins, count_temp,
     on_mins, count_heater,
     fan_mins, count_fan,
     avg_amb, min_amb, max_amb, cold_amb_mins, count_amb) = sql_row

    possible_mins = (effective_end - m_start) / 60
    days = (effective_end - m_start) / 86400

    ts = None
    if count_temp:
        ts = {
            "avg_f": round(avg_c * 1.8 + 32, 1), "avg_c": round(avg_c, 1),
            "min_f": round(min_c * 1.8 + 32, 1), "min_c": round(min_c, 1),
            "max_f": round(max_c * 1.8 + 32, 1), "max_c": round(max_c, 1),
            "cold_hrs": round(cold_mins / 60, 1),
        }

    as_ = None
    if count_amb:
        as_ = {
            "avg_f": round(avg_amb * 1.8 + 32, 1), "avg_c": round(avg_amb, 1),
            "min_f": round(min_amb * 1.8 + 32, 1), "min_c": round(min_amb, 1),
            "max_f": round(max_amb * 1.8 + 32, 1), "max_c": round(max_amb, 1),
            "cold_hrs": round(cold_amb_mins / 60, 1),
        }

    rs = None
    if count_temp:
        rs = {
            "on_hrs": round(on_mins / 60, 1),
            "avg_hrs_day": round((on_mins / 60) / days, 1) if days > 0 else 0.0,
            "temp_coverage_pct": round(count_temp / possible_mins * 100, 1) if possible_mins > 0 else 0.0,
            "heater_coverage_pct": round(count_heater / possible_mins * 100, 1) if possible_mins > 0 else 0.0,
        }
        if count_fan:
            rs["fan_on_hrs"] = round(fan_mins / 60, 1)

    return ts, as_, rs


def _is_valid_month(s):
    if len(s) != 7:
        return False
    try:
        datetime.strptime(s + "-01", "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _hvac_sched_label(params_json):
    """Render an HVAC schedule's params JSON as a human-readable string."""
    if not params_json:
        return "Set"
    try:
        p = json.loads(params_json)
    except (ValueError, TypeError):
        return "Set"
    if p.get("mode") == hvac.MODE_FREEZE:
        return hvac.MODE_LABELS[hvac.MODE_FREEZE]
    parts = []
    if p.get("power") is False:
        parts.append("Off")
    elif p.get("power") is True or p.get("mode"):
        if p.get("mode"):
            parts.append(hvac.MODE_LABELS.get(p["mode"], p["mode"]))
        if p.get("target_f") is not None:
            parts.append(f"{p['target_f']:.0f}°F")
        if p.get("fan_speed") and p["fan_speed"] != "auto":
            parts.append(f"fan {hvac.FAN_LABELS.get(p['fan_speed'], p['fan_speed']).lower()}")
    return " · ".join(parts) if parts else "Set"


if __name__ == "__main__":
    # CGI fallback: wraps the WSGI app for direct invocation / testing.
    from wsgiref.handlers import CGIHandler
    CGIHandler().run(application)
