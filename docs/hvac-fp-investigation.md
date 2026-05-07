# HVAC Freeze Prevention — setpoint mystery

The hangar Durastar DRAW33F2A (Midea OEM, US-OSK105 dongle) has two ways to enable Freeze Prevention. Both flip the same `freeze_protection` flag visible over the LAN, but they hold different setpoints. The mechanism for reaching the lower setpoint via LAN — if one exists — is the open question.

This is a planning document. The next step happens in person at the hangar with the IR remote in hand; nothing here can be confirmed remotely.

## Observed behavior

- **IR remote, "down twice from 60°F"** — engages an internal regulator that holds the indoor floor at ~46°F (~8°C). Confirmed 2026-05-06 overnight: hangar floor held 45–46°F against 32°F ambient, per `readings.indoor_f` in the disk DB. The unit displays "FP" while in this state.
- **LAN `dev.freeze_protection = True`** ([hvac.py:316](hvac.py:316)) — tested at the hangar Tuesday before 2026-05-07. The flag turned on (unit displayed "FP"), but the indoor target settled at ~60°F, which is the dongle-reported `min_target_temperature: 16` (°C) clamp. The 46°F regulator did not engage.

So both paths report `freeze_protection: True` over LAN, but only the IR path reaches 46°F. Earlier framing in CLAUDE.md ("the same feature the IR remote drives, the firmware holds a minimum heat output ~46°F internally") was overstated — that's true for IR, not for LAN on this unit.

## The open question

Is there any LAN-reachable command that engages the 46°F regulator on this firmware, or is the regulator strictly an IR-side feature?

Two scenarios are consistent with what we've seen:
1. **LAN-reachable, but not via the standard SetState path** — e.g. property command 0xB1 with `PropertyId 0x0213` (PRESET_FREEZE_PROTECTION), or a target temperature below the dongle-reported clamp.
2. **IR-only** — the regulator is held by the indoor unit's own controller, not the WiFi dongle, and the LAN path only ever flips the cosmetic flag plus drops to the dongle's minimum target.

Either answer is fine; we just need to know which one we have.

## Hypotheses worth testing

Each is independent — running them in order and stopping at the first one that holds 46°F is fine.

- **Send target=8°C alongside `freeze_protection=True`.** msmart-ng may clamp client-side before the wire hits the dongle; if it doesn't, the firmware may honor a sub-`min_target_temperature` value when FP is engaged.
- **Direct property write of 0x0213 via 0xB1.** PR [#9](https://github.com/leithl/remote-switch/pull/9) recorded a *read* attempt on this property returning size=0 / error. A *write* has not been tried. Even if the read fails, a write may succeed — Midea property tables are inconsistent across firmwares.
- **Capture the dongle's reported state at the moment the IR remote engages FP.** If `target_temperature` drops below 16, that tells us the firmware is willing to hold a sub-clamp value and the dongle just won't accept one as input. If some other attribute changes that we're not currently caching, that's the wire signal we'd want to mimic.
- **Diff `vars(dev)` immediately after IR-FP-set vs immediately after LAN-FP-set.** Any attribute that differs is a candidate for what to send.

## In-person test plan

The user runs this at the hangar with the IR remote and an SSH session to the Pi. All steps are read-only over the LAN; nothing here issues an `apply()` on its own.

Each step records a snapshot. Save the JSON output of step 1 as the baseline; later snapshots are compared against it.

1. **Baseline — unit fully off, no FP.** Power the unit off via the IR remote. Wait 30s. On the Pi:
   ```bash
   sudo curl -s 'http://localhost/cgi-bin/remote-switch/switch.py?hvac_state=1' >/dev/null   # forces a fresh refresh
   sudo cat /run/heater-hvac.json | python3 -m json.tool
   ```
   Save that output. Confirm `freeze_protection: false`, `power: false`.

2. **Engage IR-FP via the remote.** Press power on, then "down twice from 60°F" until the panel shows "FP". Wait 60s for the dongle's reported state to catch up. On the Pi:
   ```bash
   sudo cat /run/heater-hvac.json | python3 -m json.tool
   ```
   Diff against step 1's snapshot. **Record:**
   - Does `freeze_protection` go to `true`? (Expected: yes.)
   - What is `target_c` / `target_f`? (Critical: if it drops below 16°C / 60°F, the firmware *is* holding a sub-clamp target — that's the smoking gun.)
   - What is `mode` (operational_mode)? Heat? Auto? Something else?
   - Does any other field change in a way that wasn't expected?

3. **Inspect the raw msmart device.** Get more attributes than `_device_to_dict` exposes:
   ```bash
   sudo python3 -c '
   import asyncio, sys
   sys.path.insert(0, "/usr/lib/cgi-bin/remote-switch")
   from hvac import _open_device
   async def main():
       d = await _open_device()
       await d.refresh()
       for k in sorted(vars(d)):
           print(f"{k}={getattr(d, k)!r}")
   asyncio.run(main())
   ' | tee /tmp/hvac-ir-fp.txt
   ```
   Save `/tmp/hvac-ir-fp.txt`.

4. **Disengage IR-FP.** On the IR remote, press the power button to turn the unit off. Wait 60s. Confirm via `/run/heater-hvac.json` that `freeze_protection: false`.

5. **Engage LAN-FP.** Open the web UI's Hangar HVAC card and click the Freeze Prevention preset. (Or `?hvac_apply=1&hvac_mode=freeze` directly.) Wait 60s.

6. **Repeat the snapshot from step 3** into `/tmp/hvac-lan-fp.txt`:
   ```bash
   sudo python3 -c '
   import asyncio, sys
   sys.path.insert(0, "/usr/lib/cgi-bin/remote-switch")
   from hvac import _open_device
   async def main():
       d = await _open_device()
       await d.refresh()
       for k in sorted(vars(d)):
           print(f"{k}={getattr(d, k)!r}")
   asyncio.run(main())
   ' | tee /tmp/hvac-lan-fp.txt
   ```

7. **Diff the two snapshots:**
   ```bash
   diff /tmp/hvac-ir-fp.txt /tmp/hvac-lan-fp.txt
   ```
   Any attribute that differs is a candidate. Pay special attention to `target_temperature`, `_target_temperature`, anything `freeze`-named, and any property-cache dicts.

8. **(Optional) Try LAN target below the clamp.** Only if step 7 shows IR-FP holds a `target_temperature` < 16:
   ```bash
   # READ-ONLY first — confirm what value to send.
   # Then, with explicit user OK each time, try writing it.
   ```
   This step writes to the dongle. **Stop here and ask before executing the write.** Per the standing rule, no LAN apply() to the hangar HVAC without per-action consent.

9. **(Optional) 0xB1 property write.** Same caveat — this is a write. Document the property ID and value to send, ask, then run.

## Code references

- [hvac.py:42](hvac.py:42) — `MODE_FREEZE = "freeze"` and the rest of the stable mode tokens.
- [hvac.py:280-293](hvac.py:280) — the `set_state()` branch that flips `set_freeze` and clears mode/target/fan when the user picks freeze.
- [hvac.py:316-319](hvac.py:316) — the actual `dev.freeze_protection = ...` assignment that goes over the wire.
- [hvac.py:139-166](hvac.py:139) — `_device_to_dict()`, where `freeze_protection` is read back from the dongle and `MODE_FREEZE` is surfaced as the effective mode.
- `CLAUDE.md` — "HVAC module specifics" → Freeze Prevention paragraph (the one this doc replaces the overclaim in).
- `README.md` — "What is 'Freeze Prevention?'" — public-facing description; should be updated alongside any code change that resolves this question.

## Why we documented this instead of testing remotely

The hangar Durastar's state is held by the IR remote between visits, and any LAN `apply()` is a full SetState that can blow away an IR-set FP regulator (this happened on 2026-05-05 and required a physical visit to recover). The standing rule on this project is that mutating LAN HVAC commands need per-action consent from the user, not "this is part of a debugging task" authorization. So this is a plan written from existing code, git history, and the dongle's documented protocol — not a remote experiment.

## Related PRs

- [#9](https://github.com/leithl/remote-switch/pull/9) — reaches the conclusion "LAN-FP is purely cosmetic." That framing is wrong (LAN-FP does turn on the flag), but the underlying observation that LAN can't reach 46°F is real. Open; the user is handling separately.
- [#10](https://github.com/leithl/remote-switch/pull/10) — research note based on the same flawed framing as #9. Open.
- [#11](https://github.com/leithl/remote-switch/pull/11) — adds per-minute energy logging. Unrelated, but if it merges before this is tested, watching the logged power draw during step 2 vs step 6 would corroborate whether the regulator is producing more heat than the LAN-FP minimum.
