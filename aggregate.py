"""
aggregate.py — Core aggregation logic for remote-switch.

Shared by switch.py (live chart rendering) and log_temp.py (monthly rollup).
"""

from collections import deque
from statistics import mean

# Watts threshold for "HVAC actually running" — below this is standby
# (controller + indoor fan + dongle, baseline ~146W on this Durastar with
# the BINARY-format energy reads). Used to filter the chart's power line and
# to count compressor-on minutes in runtime_stats. 201W is on; 200W is off.
# Margin above the 146W idle covers normal idle-power jitter; compressor
# kicking in pushes well past 200W.
POWER_ON_THRESHOLD_W = 200

# Door-detection thresholds. The Pi DS18B20 sits at ~4 ft (floor) and
# ac_indoor_f reads from the unit's ceiling thermistor at ~12 ft. A door
# opening lets denser air infiltrate one stratum while the other stays put:
# cold air sinks to the floor in winter (Pi drops, unit flat); hot air rises
# to the ceiling in summer (unit climbs, Pi flat). Detection signal is
# |Δfloor| - |Δceiling| over a 5-min window — natural mixing and HVAC heat
# (where ceiling leads floor) yield a small or negative signal.
DOOR_THRESHOLD_F   = 1.0   # Min (|Δfloor|-|Δceiling|) to flag, °F
DOOR_WINDOW_MIN    = 5     # Sliding window for rate-of-change
DOOR_MERGE_GAP_MIN = 10    # Adjacent flagged minutes within this gap merge to one event
DOOR_MAX_EVENT_MIN = 30    # Cap event length — anything longer is likely sustained draft, not a door cycle


def compute(rows, cutoff, chart_end):
    """
    Aggregate temperature, heater, ambient, fan, and HVAC power readings
    into chart-ready data. Used by log_temp.py for the monthly rollup
    cache; live chart rendering goes through compute_bucketed instead.

    Args:
        rows:      Iterable of (epoch, temp_c, heater_state, ambient_c, fan_state, ac_power_w).
                   Any value except epoch may be None. Older 5-column rows still parse
                   (ac_power_w defaults to None).
        cutoff:    Start epoch (inclusive). Rows before this are ignored.
        chart_end: End epoch (exclusive). Rows at/after this are ignored.

    Returns dict with keys:
        chart_data      list of {x: epoch_ms, y: temp_f}  (15-min bucket avg)
        ambient_data    list of {x: epoch_ms, y: temp_f}  (15-min bucket, 4-bucket rolling avg)
        power_data      list of {x: epoch_ms, y: watts}   (15-min bucket avg of ac_power_w)
        heater_ranges   list of {xMin: epoch_ms, xMax: epoch_ms}
        cold_ranges     list of {xMin: epoch_ms, xMax: epoch_ms}  (temp <= 48°F / 8.89°C)
        temp_stats      dict or None
        ambient_stats   dict or None
        runtime_stats   dict or None
    """
    chart_buckets = {}   # bucket_epoch -> [temp_c]
    amb_buckets = {}     # bucket_epoch -> [ambient_c]
    power_buckets = {}   # bucket_epoch -> [watts]
    heater_ranges = []
    cold_ranges = []

    temp_vals = []
    amb_vals = []
    on_mins = 0
    fan_on_mins = 0
    hvac_running_mins = 0
    total_temp_mins = 0
    total_heater_mins = 0
    total_fan_mins = 0
    total_hvac_mins = 0

    in_heater = False
    in_cold = False
    heater_start = heater_last = 0
    cold_start = cold_last = 0

    last_epoch = None

    for row in sorted(rows, key=lambda r: r[0]):
        epoch       = row[0]
        temp_c      = row[1]
        heater_state = row[2]
        ambient_c   = row[3]
        fan_state   = row[4] if len(row) > 4 else None
        ac_power_w  = row[5] if len(row) > 5 else None

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

        # --- fan state ---
        if fan_state is not None:
            total_fan_mins += 1
            if fan_state == 1:
                fan_on_mins += 1

        # --- HVAC power — only running minutes contribute to the chart line.
        # Standby (~60W) gets filtered out so the chart only shows actual
        # heating/cooling activity rather than a constant low baseline.
        if ac_power_w is not None:
            total_hvac_mins += 1
            if ac_power_w > POWER_ON_THRESHOLD_W:
                power_buckets.setdefault(b, []).append(ac_power_w)
                hvac_running_mins += 1

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

    # --- power data (avg watts per 15-min bucket) ---
    power_data = [
        {"x": b * 1000, "y": round(mean(vs), 1)}
        for b, vs in sorted(power_buckets.items())
    ]

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
        if total_fan_mins > 0:
            runtime_stats["fan_on_hrs"] = round(fan_on_mins / 60, 1)
        if total_hvac_mins > 0:
            # hvac_on_hrs now reflects "minutes the compressor was actually
            # drawing > 100W", not just "minutes the unit was powered on in
            # some mode". Old monthly_cache rows from before this change use
            # the old looser definition; that's acceptable drift in stats.
            runtime_stats["hvac_on_hrs"] = round(hvac_running_mins / 60, 1)

    return {
        "chart_data": chart_data,
        "ambient_data": ambient_data,
        "power_data": power_data,
        "heater_ranges": heater_ranges,
        "cold_ranges": cold_ranges,
        "temp_stats": temp_stats,
        "ambient_stats": ambient_stats,
        "runtime_stats": runtime_stats,
    }


def compute_bucketed(rows, cutoff, chart_end, bucket_secs=900):
    """
    Fast chart-data computation from pre-bucketed rows returned by config.query_bucketed().

    Processes ~N/bucket_minutes rows than compute() by using SQL GROUP BY to
    pre-aggregate per-minute readings into time buckets before Python sees them.

    Args:
        rows:        Iterable of (bucket_epoch, avg_temp_c, avg_ambient_c, avg_heater_state,
                                  avg_fan_state, avg_ac_power_w)
                     as returned by config.query_bucketed(), sorted by bucket_epoch.
                     avg_ac_power_w is the average compressor draw over the bucket; NULL
                     for buckets logged before ac_power_w existed.
        cutoff:      Start epoch (inclusive).
        chart_end:   End epoch (exclusive).
        bucket_secs: Width of each bucket in seconds. Must match what was passed to
                     config.query_bucketed for the same data. Default 900 (15 min);
                     pass 60 for the 1d zoom view to surface compressor cycles.

    Returns dict with the chart-rendering keys:
        chart_data      list of {x: epoch_ms, y: temp_f}
        ambient_data    list of {x: epoch_ms, y: temp_f}  (4-bucket rolling avg)
        power_data      list of {x: epoch_ms, y: watts}   (avg HVAC draw per bucket)
        heater_ranges   list of {xMin: epoch_ms, xMax: epoch_ms}
        cold_ranges     list of {xMin: epoch_ms, xMax: epoch_ms}
        fan_ranges      list of {xMin: epoch_ms, xMax: epoch_ms}
    """
    chart_data = []
    ambient_data = []
    power_data = []
    heater_ranges = []
    cold_ranges = []
    fan_ranges = []

    amb_window = deque(maxlen=4)
    in_heater = False
    in_cold = False
    in_fan = False
    heater_start = heater_last = 0
    cold_start = cold_last = 0
    fan_start = fan_last = 0

    for row in rows:
        b = row[0]
        avg_temp_c    = row[1]
        avg_ambient_c = row[2]
        avg_heater    = row[3]
        avg_fan       = row[4]
        avg_ac_power_w = row[5] if len(row) > 5 else None

        if b < cutoff or b >= chart_end:
            continue

        # Temperature / cold ranges
        if avg_temp_c is not None:
            chart_data.append({"x": b * 1000, "y": round(avg_temp_c * 1.8 + 32, 1)})
            cold = avg_temp_c <= 8.89
            if cold and not in_cold:
                cold_start = b
                in_cold = True
            elif not cold and in_cold:
                cold_ranges.append({"xMin": cold_start * 1000, "xMax": (cold_last + bucket_secs) * 1000})
                in_cold = False
            if cold:
                cold_last = b

        # Heater ranges (avg_heater > 0 means heater was on for part of this bucket)
        if avg_heater is not None:
            on = avg_heater > 0
            if on and not in_heater:
                heater_start = b
                in_heater = True
            elif not on and in_heater:
                heater_ranges.append({"xMin": heater_start * 1000, "xMax": (heater_last + bucket_secs) * 1000})
                in_heater = False
            if on:
                heater_last = b

        # Fan ranges (avg_fan > 0 means fan was on for part of this bucket)
        if avg_fan is not None:
            fan_on = avg_fan > 0
            if fan_on and not in_fan:
                fan_start = b
                in_fan = True
            elif not fan_on and in_fan:
                fan_ranges.append({"xMin": fan_start * 1000, "xMax": (fan_last + bucket_secs) * 1000})
                in_fan = False
            if fan_on:
                fan_last = b

        # HVAC power line — only plot buckets where the avg watts crossed the
        # "running" threshold. Buckets that averaged at or below standby get
        # no point (they'd just be a constant ~60W floor of noise).
        if avg_ac_power_w is not None and avg_ac_power_w > POWER_ON_THRESHOLD_W:
            power_data.append({"x": b * 1000, "y": round(avg_ac_power_w, 1)})

        # Ambient (4-bucket rolling avg)
        if avg_ambient_c is not None:
            amb_window.append(avg_ambient_c * 1.8 + 32)
            ambient_data.append({"x": b * 1000, "y": round(sum(amb_window) / len(amb_window), 1)})

    # Close open ranges
    if in_heater:
        heater_ranges.append({"xMin": heater_start * 1000, "xMax": (heater_last + bucket_secs) * 1000})
    if in_cold:
        cold_ranges.append({"xMin": cold_start * 1000, "xMax": (cold_last + bucket_secs) * 1000})
    if in_fan:
        fan_ranges.append({"xMin": fan_start * 1000, "xMax": (fan_last + bucket_secs) * 1000})

    return {
        "chart_data": chart_data,
        "ambient_data": ambient_data,
        "power_data": power_data,
        "heater_ranges": heater_ranges,
        "cold_ranges": cold_ranges,
        "fan_ranges": fan_ranges,
    }


def detect_door_events(rows,
                       threshold_f=DOOR_THRESHOLD_F,
                       window_min=DOOR_WINDOW_MIN,
                       merge_gap_min=DOOR_MERGE_GAP_MIN,
                       max_event_min=DOOR_MAX_EVENT_MIN):
    """
    Detect hangar-door-open events from rapid floor temp changes that
    aren't matched by the ceiling-mounted unit thermistor.

    Args:
        rows: Iterable of (epoch, temp_c, ac_indoor_f) per-minute, sorted by epoch.
              temp_c is °C; ac_indoor_f is °F. Either may be None.
        threshold_f / window_min / merge_gap_min / max_event_min: see module constants.

    Returns:
        list of {xMin: epoch_ms, xMax: epoch_ms, peak_f: float, direction: str}
        sorted by xMin. direction is "infiltration" if outdoor air came in
        (floor cooled OR floor warmed both qualify); the sign hints at season.
    """
    paired = [(e, t, ai) for (e, t, ai) in rows if t is not None and ai is not None]
    if len(paired) < window_min + 1:
        return []

    by_epoch = {e: (t, ai) for (e, t, ai) in paired}
    epochs = sorted(by_epoch.keys())

    flagged = []  # (epoch, signal_f, signed_floor_delta_f)
    for i in range(window_min, len(epochs)):
        e_now = epochs[i]
        e_then = epochs[i - window_min]
        # Skip windows that span data gaps — the row 5 indices back may be
        # 30 minutes ago, not 5.
        if e_now - e_then > window_min * 60 + 30:
            continue
        t_now, ai_now = by_epoch[e_now]
        t_then, ai_then = by_epoch[e_then]
        floor_delta_f = (t_now - t_then) * 9.0 / 5.0
        ceil_delta_f  = ai_now - ai_then
        signal = abs(floor_delta_f) - abs(ceil_delta_f)
        if signal >= threshold_f:
            flagged.append((e_now, signal, floor_delta_f))

    if not flagged:
        return []

    events = []
    cur_start = flagged[0][0]
    cur_end = flagged[0][0]
    cur_peak = flagged[0][1]
    cur_dir = flagged[0][2]
    for e, s, d in flagged[1:]:
        if e - cur_end <= merge_gap_min * 60:
            cur_end = e
            if s > cur_peak:
                cur_peak = s
                cur_dir = d
        else:
            events.append(_door_event(cur_start, cur_end, cur_peak, cur_dir, window_min))
            cur_start = e
            cur_end = e
            cur_peak = s
            cur_dir = d
    events.append(_door_event(cur_start, cur_end, cur_peak, cur_dir, window_min))

    # Drop sustained events — likely a long-open door (which we can't usefully
    # call out as a single point) or a non-door drift the heuristic misclassified.
    return [e for e in events
            if (e["xMax"] - e["xMin"]) <= max_event_min * 60 * 1000]


def _door_event(start_epoch, end_epoch, peak_f, signed_floor_delta_f, window_min):
    # Pull start back by the window — the signal is computed at the END of
    # each window, so the actual event onset preceded the first flag.
    return {
        "xMin": (start_epoch - window_min * 60) * 1000,
        "xMax": (end_epoch + 60) * 1000,
        "peak_f": round(peak_f, 1),
        "direction": "cooling" if signed_floor_delta_f < 0 else "warming",
    }
