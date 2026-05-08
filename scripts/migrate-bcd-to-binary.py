#!/usr/bin/env python3
"""
One-shot migration: rewrite ac_total_kwh and ac_power_w columns from
BCD-decoded values (msmart-ng default) to BINARY-decoded values for the
hangar Durastar DRAW33F2A.

Background (see CLAUDE.md "ac_total_kwh" column doc): the dongle's energy
fields on this Durastar are binary-encoded, but msmart-ng defaults to BCD.
Empirically (verified 2026-05-08 over 25h), BINARY's average rate matches
BINARY's instantaneous rtp (~146W within 7%); BCD's does not. The BCD
interpretation also produces -0.05 / -0.64 spurious drops at byte-nibble
and byte-wrap boundaries.

This script:
1. Reads each row with non-NULL ac_total_kwh / ac_power_w
2. For each BCD-decoded value, enumerates candidate byte tuples that
   would have produced it
3. Picks the byte tuple where the underlying raw 32-bit (or 24-bit for
   power) value is closest to the expected value, using monotonicity
   for total and a known-idle anchor for power
4. Computes the BINARY interpretation from the chosen bytes and writes
   it back

Run once on the Pi after pulling the BINARY-format change. Idempotent:
re-running on already-migrated data is a no-op (the candidate set for a
BINARY value is just one — itself — under most byte combos).

  cd /usr/lib/cgi-bin/remote-switch
  sudo python3 scripts/migrate-bcd-to-binary.py
"""

from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

RAM_DB  = Path("/run/heater.db")
DISK_DB = Path("/var/lib/heater/heater.db")

# Anchor: at probe time on 2026-05-08 the dongle reported
# BCD total=279.12 simultaneously with BINARY raw 162066 (display 16206.6).
# Used as the rate prior — at ~146W idle, raw ticks once every ~240s.
IDLE_SEC_PER_RAW_TICK = 240.0


# --- BCD decode helpers (must match msmart-ng's parse exactly) ---

def decode_bcd_byte(d: int) -> int:
    """msmart-ng's decode_bcd: 10 * (d>>4) + (d & 0xF). Range 0-165."""
    return 10 * (d >> 4) + (d & 0xF)


# Precompute reverse map: BCD output value -> list of byte values
_BCD_REVERSE: dict[int, list[int]] = {}
for _b in range(256):
    _v = decode_bcd_byte(_b)
    _BCD_REVERSE.setdefault(_v, []).append(_b)


def bytes_for_bcd_value(v: int) -> list[int]:
    """Return all byte values whose decode_bcd output is `v`."""
    return _BCD_REVERSE.get(v, [])


# --- Total energy: 4-byte field ---
# msmart-ng parse_energy:
#   bcd = 10000*bcd(d0) + 100*bcd(d1) + 1*bcd(d2) + 0.01*bcd(d3)
#   binary = ((d0<<24)|(d1<<16)|(d2<<8)|d3) / 10
# So bcd_value * 100 = 1000000*bcd(d0) + 10000*bcd(d1) + 100*bcd(d2) + bcd(d3)


def candidates_total(bcd_value: float) -> list[tuple[int, int, int, int, int]]:
    """For a BCD-decoded total_kwh value, return (b0, b1, b2, b3, raw32) tuples.
    raw32 is the underlying 32-bit binary value."""
    # Convert to integer centi-kWh
    V = round(bcd_value * 100)
    out = []
    # Each bcd(bi) is in 0..165. So V splits as:
    #   x = bcd(b0) in [0..165]   # contributes 1000000*x
    #   y = bcd(b1) in [0..165]   # contributes 10000*y
    #   z = bcd(b2) in [0..165]   # contributes 100*z
    #   w = bcd(b3) in [0..165]   # contributes w
    # For V around 27,800-28,000 (our range), x=0, y in {0,1,2}, z in 0-165, w in 0-165.
    for x in range(166):
        c0 = 1000000 * x
        if c0 > V:
            break
        rem0 = V - c0
        for y in range(166):
            c1 = 10000 * y
            if c1 > rem0:
                break
            rem1 = rem0 - c1
            for z in range(166):
                c2 = 100 * z
                if c2 > rem1:
                    break
                w = rem1 - c2
                if w > 165:
                    continue
                # Found a valid (x,y,z,w). Enumerate byte combos.
                for b0 in bytes_for_bcd_value(x):
                    for b1 in bytes_for_bcd_value(y):
                        for b2 in bytes_for_bcd_value(z):
                            for b3 in bytes_for_bcd_value(w):
                                raw32 = (b0 << 24) | (b1 << 16) | (b2 << 8) | b3
                                out.append((b0, b1, b2, b3, raw32))
    return out


# --- Real-time power: 3-byte field ---
# msmart-ng parse_power:
#   bcd = 1000*bcd(d0) + 10*bcd(d1) + 0.1*bcd(d2)
#   binary = ((d0<<16)|(d1<<8)|d2) / 10
# So bcd_value * 10 = 10000*bcd(d0) + 100*bcd(d1) + bcd(d2)


def candidates_power(bcd_value: float) -> list[tuple[int, int, int, int]]:
    """For a BCD-decoded power_w value, return (b0, b1, b2, raw24) tuples."""
    V = round(bcd_value * 10)
    out = []
    for x in range(166):
        c0 = 10000 * x
        if c0 > V:
            break
        rem0 = V - c0
        for y in range(166):
            c1 = 100 * y
            if c1 > rem0:
                break
            z = rem0 - c1
            if z > 165:
                continue
            for b0 in bytes_for_bcd_value(x):
                for b1 in bytes_for_bcd_value(y):
                    for b2 in bytes_for_bcd_value(z):
                        raw24 = (b0 << 16) | (b1 << 8) | b2
                        out.append((b0, b1, b2, raw24))
    return out


# --- Reconstruction (walk backward from live-probe anchor) ---
#
# Walking backward from a known-good endpoint is more robust than walking
# forward from an unknown start. The live probe on 2026-05-08 gave us the
# bytes (and their BINARY interpretation) at a known instant; everything
# earlier in time should have raw <= that, with a rate consistent with
# idle (~1 raw tick per 240s). We pick the candidate closest to that
# expected raw, preferring non-increasing values (since raw is monotonic).
#
# Anchors observed live on 2026-05-08:
#   total: BCD 279.12 ↔ BINARY raw 162066 (display 16206.6, kWh 1620.66)
#   power: BCD 61.4   ↔ BINARY raw 1460   (W 146.0)
TOTAL_ANCHOR_RAW = 162066
POWER_ANCHOR_RAW = 1460


def reconstruct_total(rows: list[tuple[int, int, float]]) -> dict[int, float]:
    """rows: list of (rowid, epoch, bcd_value), sorted by epoch ascending.
    Returns {rowid: binary_kwh}. Walks backward from TOTAL_ANCHOR_RAW."""
    out: dict[int, float] = {}
    next_raw = TOTAL_ANCHOR_RAW
    next_epoch = None
    # Walk in reverse time order
    for rowid, epoch, bcd in reversed(rows):
        cands = candidates_total(bcd)
        if not cands:
            continue
        if next_epoch is None:
            # Last (most recent) row — pick candidate closest to anchor.
            chosen = min((c[4] for c in cands), key=lambda r: abs(r - next_raw))
        else:
            elapsed = max(0, next_epoch - epoch)
            expected_delta = elapsed / IDLE_SEC_PER_RAW_TICK
            target = next_raw - expected_delta
            # Prefer candidates with raw <= next_raw (counter goes backward
            # in time means values were smaller earlier).
            non_increasing = [c[4] for c in cands if c[4] <= next_raw]
            if non_increasing:
                chosen = min(non_increasing, key=lambda r: abs(r - target))
            else:
                # No non-increasing option. Real counter went backward
                # (or anchor is wrong) — pick closest to target.
                chosen = min((c[4] for c in cands), key=lambda r: abs(r - target))
        kwh = chosen / 100.0
        out[rowid] = round(kwh, 2)
        next_raw = chosen
        next_epoch = epoch
    return out


def reconstruct_power(rows: list[tuple[int, int, float]]) -> dict[int, float]:
    """rows: list of (rowid, epoch, bcd_value), sorted by epoch ascending.
    Returns {rowid: binary_w}. Walks backward from POWER_ANCHOR_RAW.

    Power can change dramatically (compressor on/off), so we don't
    enforce monotonicity. Heuristic: pick candidate closest to the
    next-in-time chosen raw (continuity)."""
    out: dict[int, float] = {}
    next_raw = POWER_ANCHOR_RAW
    for rowid, epoch, bcd in reversed(rows):
        cands = candidates_power(bcd)
        if not cands:
            continue
        chosen = min((c[3] for c in cands), key=lambda r: abs(r - next_raw))
        w = chosen / 10.0
        out[rowid] = round(w, 1)
        next_raw = chosen
    return out


# --- Driver ---


def migrate(conn: sqlite3.Connection, label: str) -> None:
    cur = conn.cursor()

    rows_t = cur.execute(
        "SELECT rowid, epoch, ac_total_kwh FROM readings "
        "WHERE ac_total_kwh IS NOT NULL ORDER BY epoch"
    ).fetchall()
    print(f"[{label}] {len(rows_t)} rows with ac_total_kwh")
    if rows_t:
        new_total = reconstruct_total(rows_t)
        first_old = rows_t[0][2]
        last_old  = rows_t[-1][2]
        first_new = new_total.get(rows_t[0][0])
        last_new  = new_total.get(rows_t[-1][0])
        print(f"[{label}]   ac_total_kwh: first {first_old} -> {first_new}, "
              f"last {last_old} -> {last_new}")
        cur.executemany(
            "UPDATE readings SET ac_total_kwh = ? WHERE rowid = ?",
            [(v, rid) for rid, v in new_total.items()],
        )

    rows_p = cur.execute(
        "SELECT rowid, epoch, ac_power_w FROM readings "
        "WHERE ac_power_w IS NOT NULL ORDER BY epoch"
    ).fetchall()
    print(f"[{label}] {len(rows_p)} rows with ac_power_w")
    if rows_p:
        new_power = reconstruct_power(rows_p)
        first_old = rows_p[0][2]
        last_old  = rows_p[-1][2]
        first_new = new_power.get(rows_p[0][0])
        last_new  = new_power.get(rows_p[-1][0])
        print(f"[{label}]   ac_power_w: first {first_old} -> {first_new}, "
              f"last {last_old} -> {last_new}")
        cur.executemany(
            "UPDATE readings SET ac_power_w = ? WHERE rowid = ?",
            [(v, rid) for rid, v in new_power.items()],
        )


def main() -> None:
    for db_path in (RAM_DB, DISK_DB):
        if not db_path.exists():
            print(f"Skipping {db_path} (not found)")
            continue
        print(f"\n=== {db_path} ===")
        conn = sqlite3.connect(str(db_path))
        try:
            migrate(conn, db_path.name)
            conn.commit()
            print(f"  Committed {db_path}")
        except Exception as e:
            print(f"  ERROR on {db_path}: {e}; rolling back")
            conn.rollback()
            sys.exit(1)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
