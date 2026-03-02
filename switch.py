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
from datetime import date, datetime
from pathlib import Path

# Ensure local modules are importable when run as WSGI/CGI
sys.path.insert(0, str(Path(__file__).parent))

import aggregate
import config

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
# Stats row builders
# ---------------------------------------------------------------------------

def _temp_row(mk, label, ts, pct):
    """Build a clickable <tr> for monthly temp stats."""
    cov_label = label if pct >= 100 else f"{label} ({pct:.1f}%)"
    return Markup(
        f'<tr onclick="location=\'switch.py?range={html.escape(mk)}\'" style="cursor:pointer">'
        f'<td>{html.escape(cov_label)}</td>'
        f'<td>{ts["avg_f"]:.1f} / {ts["avg_c"]:.1f}</td>'
        f'<td>{ts["min_f"]:.1f} / {ts["min_c"]:.1f}</td>'
        f'<td>{ts["max_f"]:.1f} / {ts["max_c"]:.1f}</td>'
        f'<td>{ts["cold_hrs"]:.1f}</td>'
        f'</tr>'
    )


def _ambient_row(mk, label, as_, onclick=True):
    """Build an 'Outdoor' sub-row for ambient stats."""
    onclick_attr = (
        f' onclick="location=\'switch.py?range={html.escape(mk)}\'" style="cursor:pointer"'
        if onclick else ""
    )
    return Markup(
        f'<tr{onclick_attr}>'
        f'<td class="ps-3 text-muted small">Outdoor</td>'
        f'<td>{as_["avg_f"]:.1f} / {as_["avg_c"]:.1f}</td>'
        f'<td>{as_["min_f"]:.1f} / {as_["min_c"]:.1f}</td>'
        f'<td>{as_["max_f"]:.1f} / {as_["max_c"]:.1f}</td>'
        f'<td>{as_["cold_hrs"]:.1f}</td>'
        f'</tr>'
    )


def _runtime_row(label, rs, pct):
    """Build a <tr> for monthly runtime stats."""
    cov_label = label if pct >= 100 else f"{label} ({pct:.1f}%)"
    return Markup(
        f'<tr><td>{html.escape(cov_label)}</td>'
        f'<td>{rs["on_hrs"]:.1f}</td>'
        f'<td>{rs["avg_hrs_day"]:.1f}</td></tr>'
    )


# ---------------------------------------------------------------------------
# Old-cache row helpers (from migrate.py, pre-rendered HTML from .dat files)
# ---------------------------------------------------------------------------

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

    # --- Schedule add ---
    now_epoch = int(datetime.now().timestamp())
    sched_msg = Markup("")

    sched_dt_raw = qs.get("sched_dt", "")
    sched_action = qs.get("sched_action", "")
    if sched_dt_raw and sched_action in ("0", "1"):
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
            else:
                conn = config.get_db()
                conn.execute(
                    "INSERT OR REPLACE INTO schedules (created_epoch, execute_epoch, action) "
                    "VALUES (?, ?, ?)",
                    (now_epoch, sched_epoch, sched_action),
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

    # --- Ambient label ---
    _, _, ambient_label = config.get_location()

    # --- Pending schedules ---
    conn = config.get_db()
    pending_rows = conn.execute(
        "SELECT created_epoch, execute_epoch, action FROM schedules ORDER BY execute_epoch"
    ).fetchall()
    conn.close()

    pending_sched_rows = []
    for created_epoch, execute_epoch, action in pending_rows:
        action_label = "Turn ON" if action == "1" else "Turn OFF"
        sched_time = datetime.fromtimestamp(execute_epoch).strftime("%b %d, %Y %I:%M %p")
        cancel_url = f"switch.py?cancel_id={created_epoch}"
        pending_sched_rows.append(Markup(
            f'<tr><td>{html.escape(sched_time)}</td>'
            f'<td>{html.escape(action_label)}</td>'
            f'<td><a href="{html.escape(cancel_url)}" '
            f'class="btn btn-sm btn-outline-danger py-0">Cancel</a></td></tr>'
        ))

    # --- Range / chart ---
    range_param = qs.get("range", "7d")
    if range_param not in ("7d", "30d") and not _is_valid_month(range_param):
        range_param = "7d"

    if range_param == "30d":
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
    ambient_data = []
    combined_stats_rows = []
    runtime_rows = []

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
                ambient_data = cached_result.get("ambient_data", [])
                chart_from_cache = True

        if not chart_from_cache:
            rows = config.query_readings(conn, chart_cutoff, chart_end_epoch)
            live_result = aggregate.compute(rows, chart_cutoff, chart_end_epoch)
            chart_data = live_result["chart_data"]
            heater_ranges = live_result["heater_ranges"]
            cold_ranges = live_result["cold_ranges"]
            ambient_data = live_result["ambient_data"]

        # Build per-month stats (13 months) with batch queries
        month_meta = []
        for i in range(12, -1, -1):
            md = config.subtract_months(cur_month_date, i)
            mk = md.strftime("%Y-%m")
            lbl = _month_label(md)
            m_start, m_end = _month_bounds(md)
            is_current = md == cur_month_date
            effective_end = min(m_end, now_epoch) if is_current else m_end
            month_meta.append((mk, lbl, m_start, m_end, effective_end, is_current))

        # Fetch all cache rows in one query
        all_mks = [m[0] for m in month_meta]
        placeholders = ",".join("?" * len(all_mks))
        cache_map = {
            row[0]: json.loads(row[1])
            for row in conn.execute(
                f"SELECT month, data FROM monthly_cache WHERE month IN ({placeholders})",
                all_mks,
            ).fetchall()
        }

        # Fetch all live-month rows in one batch query
        live_months = [
            (mk, lbl, m_start, effective_end)
            for mk, lbl, m_start, m_end, effective_end, is_current in month_meta
            if is_current or mk not in cache_map
        ]

        live_rows_by_month = {}
        if live_months:
            batch_start = min(m[2] for m in live_months)
            batch_end = max(m[3] for m in live_months)
            all_live_rows = config.query_readings(conn, batch_start, batch_end)
            for mk, lbl, m_start, effective_end in live_months:
                live_rows_by_month[mk] = [
                    r for r in all_live_rows if m_start <= r[0] < effective_end
                ]

        for mk, lbl, m_start, m_end, effective_end, is_current in month_meta:
            cached = cache_map.get(mk) if not is_current else None

            if cached:
                # Old format (from migrate.py .dat import) has _html_* keys
                if "_html_temp" in cached:
                    tr = cached.get("_html_temp", "")
                    if tr:
                        combined_stats_rows.append(_old_cache_temp_row(mk, tr))
                    ar = cached.get("_html_ambient_stat", "")
                    if ar:
                        combined_stats_rows.append(_old_cache_ambient_row(mk, ar))
                    rr = cached.get("_html_runtime", "")
                    if rr:
                        runtime_rows.append(Markup(
                            rr.replace(
                                "<tr>",
                                f'<tr onclick="location=\'switch.py?range={html.escape(mk)}\'" '
                                f'style="cursor:pointer">',
                                1,
                            )
                        ))
                else:
                    # New format from log_temp.py rollup
                    ts = cached.get("temp_stats")
                    as_ = cached.get("ambient_stats")
                    rs = cached.get("runtime_stats")
                    if ts and rs:
                        t_pct = ts and rs and (
                            rs.get("temp_coverage_pct", 0)
                        )
                        combined_stats_rows.append(_temp_row(mk, lbl, ts, t_pct or 0))
                    if as_:
                        combined_stats_rows.append(_ambient_row(mk, lbl, as_))
                    if rs:
                        h_pct = rs.get("heater_coverage_pct", 0)
                        runtime_rows.append(_runtime_row(lbl, rs, h_pct))
            else:
                # Live computation (rows pre-fetched in batch above)
                rows = live_rows_by_month.get(mk, [])
                result = aggregate.compute(rows, m_start, effective_end)
                ts = result["temp_stats"]
                as_ = result["ambient_stats"]
                rs = result["runtime_stats"]

                if ts and rs:
                    t_pct = rs.get("temp_coverage_pct", 0)
                    combined_stats_rows.append(_temp_row(mk, lbl, ts, t_pct))
                if as_:
                    combined_stats_rows.append(_ambient_row(mk, lbl, as_))
                if rs:
                    h_pct = rs.get("heater_coverage_pct", 0)
                    runtime_rows.append(_runtime_row(lbl, rs, h_pct))

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
        temp_display=temp_display,
        ambient_label=ambient_label,
        range=range_param,
        chart_title=chart_title,
        chart_data=chart_data,
        heater_ranges=heater_ranges,
        cold_ranges=cold_ranges,
        ambient_data=ambient_data,
        combined_stats_rows=combined_stats_rows,
        runtime_rows=runtime_rows,
        sched_msg=sched_msg,
        pending_sched_rows=pending_sched_rows,
        now_dt_min=now_dt_min,
        now_str=now_str,
    )

    return (
        "200 OK",
        [("Content-Type", "text/html; charset=utf-8")],
        body.encode("utf-8"),
    )


def _is_valid_month(s):
    if len(s) != 7:
        return False
    try:
        datetime.strptime(s + "-01", "%Y-%m-%d")
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    # CGI fallback: wraps the WSGI app for direct invocation / testing.
    from wsgiref.handlers import CGIHandler
    CGIHandler().run(application)
