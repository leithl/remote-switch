# Durastar/Midea LAN Freeze-Prevention Research

Investigation: can we engage the Durastar DRAW33F2A's real ~46 °F (8 °C)
internal regulator — the one the IR remote's "FP" button activates — over
LAN, instead of via IR?

Date: 2026-05-07. Verified against msmart-ng 2025.12.0.

## TL;DR

**No.** The IR-remote FP feature does not have a known LAN equivalent. The
SetState bit that msmart-ng exposes as `freeze_protection` is the only public
write channel, and on this Durastar firmware it's cosmetic — it sets the "FP"
icon and clamps target to 16 °C, but does not engage the hidden ~8 °C
regulator. Three independent Midea LAN libraries (msmart-ng, midea-local,
midea-beautiful-air) all converge on the same single bit. The OEM NetHome Plus
weeX plugin's decompiled bytecode shows no extra channel either. **Stop
digging — this is IR-only on this hardware.**

## What was already known going in

We had already, byte-for-byte, diffed:

- IR-FP wire state (after pressing HEAT, target=60 °F, ▽▽ within 1 s) vs
- LAN-FP wire state (`AC.freeze_protection = True` via msmart)

Result: the SetState payloads are identical modulo byte 15 (temperature
fraction) and a sequence counter. The bit at byte 21 / 0x80 is set in both.
Yet the unit holds 46 °F under IR-FP and 60 °F under LAN-FP. The hangar floor
sat at 45.7 °F with 32 °F ambient through April under IR-FP — proof the
regulator is real, just unreachable from LAN.

We had also tried PropertyId `0x0213` (the user-facing
"PRESET_FREEZE_PROTECTION" name) via the `0xB1` property mechanism — got
size=0 with the error bit. That experiment was on the wrong premise (see
finding #2 below).

## Findings

### 1. Three libraries, same single bit

All three public Midea LAN implementations write the same byte / same bit, and
expose it as the only freeze knob:

| Library | Class | Byte | Bit | Direction |
| --- | --- | --- | --- | --- |
| [msmart-ng](https://github.com/mill1000/midea-msmart/blob/main/msmart/device/AC/command.py) | `SetStateCommand` / `StateResponse` | 20 / 21 | 0x80 | RW |
| [midea-local](https://github.com/midea-lan/midea-local/blob/main/midealocal/devices/ac/message.py) | `MessageGeneralSet` / `XA0MessageBody` / `XC0MessageBody` | 21 | 0x80 | RW |
| [midea-beautiful-air](https://github.com/nbogojevic/midea-beautiful-air/blob/main/midea_beautiful/command.py) | `AirConditionerSetCommand` / `AirConditionerResponse` | 31* | 0x80 | RW |

\*midea-beautiful-air counts from a different header offset, but it's the same
logical bit.

In wuwentao/midea_ac_lan, `PRESET_AWAY` simply calls
`set_attribute("frost_protect", True)` — a direct alias for the bit above. No
secret sauce.

### 2. `0x0213` is a CapabilityId, not a PropertyId

Confirmed in [`msmart/device/AC/command.py`](https://github.com/mill1000/midea-msmart/blob/main/msmart/device/AC/command.py):

- `CapabilityId.PRESET_FREEZE_PROTECTION = 0x0213` — read via the `0xB5`
  capability-query message; tells you whether the unit *says* it supports FP.
- `PropertyId` enum — has BREEZELESS, IECO, ANION, RATE_SELECT, JET_COOL,
  CASCADE, etc. **No FREEZE entry.**

That's why our earlier `0x0213` property query returned size=0 with the error
bit: capabilities and properties live on different message types. We can
verify the unit reports `supports_freeze_protection: True` via capabilities,
but there is no separate property channel to *control* it.

### 3. Sub-17 °C target encoding exists, but doesn't reach 8 °C

[mill1000/midea-msmart#77](https://github.com/mill1000/midea-msmart/issues/77)
+ [PR #78](https://github.com/mill1000/midea-msmart/pull/78) added an
"alternate target temperature" byte:

```python
if 17 <= integral_temp <= 30:
    temperature = (integral_temp - 16) & 0xF      # primary (byte 1)
    temperature_alt = 0
else:
    temperature = 0
    temperature_alt = (integral_temp - 12) & 0x1F # alternate (byte ~13)
```

Range 12–43 °C theoretically. But:

- The Durastar dongle still reports `min_target_temperature: 16` and clamps.
- Even if we bypassed the clamp, this is just a normal target setpoint — it
  doesn't engage the freeze regulator. The unit would heat to 12 °C/54 °F at
  best, not hold at 8 °C/46 °F.
- mill1000's own comment on #77: *"Interestingly I have a device capable of
  16 C with the current code, even though the references I've seen say that
  should be invalid"* — i.e. the floor is firmware-dependent and undocumented.

### 4. Most damning: the project owner already concluded this

[mill1000/midea-ac-py#267 — "Frost Protection not working"](https://github.com/mill1000/midea-ac-py/issues/267):

> mill1000: *"Most likely this means the device doesn't expose the FP control
> and there's nothing we can do."* — closed as stale.

That's the maintainer of the most-active Midea LAN integration saying it,
about a user whose unit (like ours) responds to FP only via IR, not via the
OEM app or LAN. The "Away" preset works on units that support it; on units
that don't, it just doesn't.

### 5. Other open / closed threads in the same neighborhood

- [mac-zhou/midea-ac-py#40 — original Frost Protection request (2020)](https://github.com/mac-zhou/midea-ac-py/issues/40)
  — 5+ years open, documents the IR sequence (HEAT + 16 °C + ▽▽), no LAN
  solution proposed.
- [mill1000/midea-msmart#76 — "Freeze protection mode capability is not recognized"](https://github.com/mill1000/midea-msmart/issues/76)
  — closed; capabilities-table fix only, no new control surface.
- [mill1000/midea-ac-py#280 — FP via the `away` preset](https://github.com/mill1000/midea-ac-py/issues/280)
  — closed; works for users whose units honor the bit.
- [wuwentao/midea_ac_lan#774 — FP mode (open)](https://github.com/wuwentao/midea_ac_lan/issues/774)
  — user wants HA to *cancel* unwanted FP that the firmware engaged on its
  own. Same single-bit ceiling on the write path.
- [wuwentao/midea_ac_lan#762 — `frost_protect` log spam (open)](https://github.com/wuwentao/midea_ac_lan/issues/762)
  — confirms `frost_protect` is reliably *readable* on at least one Q11 unit;
  doesn't change the writeability story.

### 6. The OEM weeX plugin doesn't hide a secret opcode

mill1000 keeps decompiled NetHome Plus / mSmartHome plugins in
[`mill1000/midea-msmart/reference/0xAC/`](https://github.com/mill1000/midea-msmart/tree/main/reference/0xAC).
These are the JS/Lua bytecode the official app downloads to talk to the unit.
The control logic for FP in those plugins matches what's already in the
public libraries — same SetState bit, no extra command. The OEM app and our
LAN client speak the same protocol; if the app could engage the real
regulator on this unit, msmart-ng could too.

## Recommendation

Accept "FP is IR-only on this hardware" as the answer. Practical paths:

1. **Status quo + IR remote** — keep using the IR remote when the hangar
   needs real ~46 °F holding (winter departures); use LAN for everything else.
2. **LAN floor at 60 °F** — for hands-off automation, set
   `mode=heat, target=60 °F` over LAN. Wastes energy vs 46 °F but the unit
   cycles fine and we keep full scheduling.
3. **IR blaster as a software regulator** — Broadlink RM4 / ESPHome IR
   transceiver replays the HEAT-16-▽▽ sequence. This is the well-trodden
   workaround in the threads above. Adds a dependency but actually engages
   the real 46 °F regulator from automation.

Not recommended:

- **Reverse-engineering further.** Three libraries, the OEM app's bytecode,
  and the project maintainer have all landed on the same dead end. Months of
  more reading is unlikely to produce a hidden register that nobody else has
  found.
- **Building a "fake FP" with msmart by clamping target to 16 °C and adding
  external thermostatic logic.** That's just option #2 with extra code.

## Source links

- [mill1000/midea-msmart](https://github.com/mill1000/midea-msmart) — the actual `msmart-ng` repo (the package name on PyPI differs from the GitHub name).
- [mill1000/midea-ac-py](https://github.com/mill1000/midea-ac-py) — Home Assistant integration on top of msmart-ng.
- [mac-zhou/midea-ac-py](https://github.com/mac-zhou/midea-ac-py) — original (now-unmaintained) HA integration; has the foundational FP discussion.
- [wuwentao/midea_ac_lan](https://github.com/wuwentao/midea_ac_lan) — alternative HA integration, exposes `frost_protect` and `PRESET_AWAY`.
- [midea-lan/midea-local](https://github.com/midea-lan/midea-local) — underlying protocol library used by midea_ac_lan.
- [nbogojevic/midea-beautiful-air](https://github.com/nbogojevic/midea-beautiful-air) — independent Python implementation of the same protocol.
- [openHAB Midea AC binding thread](https://community.openhab.org/t/new-oh3-binding-midea-air-conditioning-lan/116613) — long Java implementation discussion; same protocol, no FP breakthrough.
- [Decompiled NetHome Plus weeX plugins](https://github.com/mill1000/midea-msmart/tree/main/reference/0xAC) — closest thing to OEM protocol docs in the wild.
