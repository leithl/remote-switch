"""
aggregate.py — Core aggregation logic for remote-switch.

Shared by switch.py (live chart rendering) and log_temp.py (monthly rollup).
"""

from collections import deque
from statistics import mean


def compute(rows, cutoff, chart_end):
    """
    Aggregate temperature, heater, and ambient readings into chart-ready data.

    Args:
        rows:      Iterable of (epoch, temp_c, heater_state, ambient_c).
                   Any value except epoch may be None.
        cutoff:    Start epoch (inclusive). Rows before this are ignored.
        chart_end: End epoch (exclusive). Rows at/after this are ignored.

    Returns dict with keys:
        chart_data      list of {x: epoch_ms, y: temp_f}  (15-min bucket avg)
        ambient_data    list of {x: epoch_ms, y: temp_f}  (15-min bucket, 4-bucket rolling avg)
        heater_ranges   list of {xMin: epoch_ms, xMax: epoch_ms}
        cold_ranges     list of {xMin: epoch_ms, xMax: epoch_ms}  (temp <= 48°F / 8.89°C)
        temp_stats      dict or None
        ambient_stats   dict or None
        runtime_stats   dict or None
    """
    chart_buckets = {}   # bucket_epoch -> [temp_c]
    amb_buckets = {}     # bucket_epoch -> [ambient_c]
    heater_ranges = []
    cold_ranges = []

    temp_vals = []
    amb_vals = []
    on_mins = 0
    total_temp_mins = 0
    total_heater_mins = 0

    in_heater = False
    in_cold = False
    heater_start = heater_last = 0
    cold_start = cold_last = 0

    last_epoch = None

    for row in sorted(rows, key=lambda r: r[0]):
        epoch, temp_c, heater_state, ambient_c = row

        if epoch < cutoff or epoch >= chart_end:
            continue

        last_epoch = epoch
        b = (epoch // 900) * 900

        # --- temperature ---
        if temp_c is not None:
            chart_buckets.setdefault(b, []).append(temp_c)
            temp_vals.append(temp_c)
            total_temp_mins += 1

            cold = temp_c <= 8.89
            if cold and not in_cold:
                cold_start = epoch
                in_cold = True
            elif not cold and in_cold:
                cold_ranges.append({
                    "xMin": cold_start * 1000,
                    "xMax": (cold_last + 60) * 1000,
                })
                in_cold = False
            if cold:
                cold_last = epoch

        # --- heater state ---
        if heater_state is not None:
            total_heater_mins += 1
            on = heater_state == 1
            if on:
                on_mins += 1
            if on and not in_heater:
                heater_start = epoch
                in_heater = True
            elif not on and in_heater:
                heater_ranges.append({
                    "xMin": heater_start * 1000,
                    "xMax": (heater_last + 60) * 1000,
                })
                in_heater = False
            if on:
                heater_last = epoch

        # --- ambient ---
        if ambient_c is not None:
            amb_buckets.setdefault(b, []).append(ambient_c)
            amb_vals.append(ambient_c)

    # Close open ranges (still on/cold at end of data)
    if in_heater:
        heater_ranges.append({
            "xMin": heater_start * 1000,
            "xMax": (heater_last + 60) * 1000,
        })
    if in_cold:
        cold_ranges.append({
            "xMin": cold_start * 1000,
            "xMax": (cold_last + 60) * 1000,
        })

    # --- chart data (temp buckets -> °F) ---
    chart_data = [
        {"x": b * 1000, "y": round(mean(vs) * 1.8 + 32, 1)}
        for b, vs in sorted(chart_buckets.items())
    ]

    # --- ambient data (4-bucket rolling average) ---
    window = deque(maxlen=4)
    ambient_data = []
    for b, vs in sorted(amb_buckets.items()):
        window.append(mean(vs) * 1.8 + 32)
        ambient_data.append({"x": b * 1000, "y": round(mean(window), 1)})

    # --- stats ---
    temp_stats = None
    if temp_vals:
        cold_mins = sum(1 for t in temp_vals if t <= 8.89)
        avg_c = mean(temp_vals)
        temp_stats = {
            "avg_f": round(avg_c * 1.8 + 32, 1),
            "avg_c": round(avg_c, 1),
            "min_f": round(min(temp_vals) * 1.8 + 32, 1),
            "min_c": round(min(temp_vals), 1),
            "max_f": round(max(temp_vals) * 1.8 + 32, 1),
            "max_c": round(max(temp_vals), 1),
            "cold_hrs": round(cold_mins / 60, 1),
        }

    ambient_stats = None
    if amb_vals:
        cold_mins = sum(1 for t in amb_vals if t <= 8.89)
        avg_c = mean(amb_vals)
        ambient_stats = {
            "avg_f": round(avg_c * 1.8 + 32, 1),
            "avg_c": round(avg_c, 1),
            "min_f": round(min(amb_vals) * 1.8 + 32, 1),
            "min_c": round(min(amb_vals), 1),
            "max_f": round(max(amb_vals) * 1.8 + 32, 1),
            "max_c": round(max(amb_vals), 1),
            "cold_hrs": round(cold_mins / 60, 1),
        }

    runtime_stats = None
    possible_mins = (chart_end - cutoff) / 60
    if total_temp_mins > 0:
        days = (chart_end - cutoff) / 86400
        runtime_stats = {
            "on_hrs": round(on_mins / 60, 1),
            "avg_hrs_day": round((on_mins / 60) / days, 1) if days > 0 else 0.0,
            "temp_coverage_pct": round(total_temp_mins / possible_mins * 100, 1) if possible_mins > 0 else 0.0,
            "heater_coverage_pct": round(total_heater_mins / possible_mins * 100, 1) if possible_mins > 0 else 0.0,
        }

    return {
        "chart_data": chart_data,
        "ambient_data": ambient_data,
        "heater_ranges": heater_ranges,
        "cold_ranges": cold_ranges,
        "temp_stats": temp_stats,
        "ambient_stats": ambient_stats,
        "runtime_stats": runtime_stats,
    }
