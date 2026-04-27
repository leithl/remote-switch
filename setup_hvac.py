#!/usr/bin/env python3
"""
setup_hvac.py — One-time pairing helper for the Durastar HVAC WiFi dongle.

Run on the Pi AFTER:
  1. The Midea US-OSK105 dongle is plugged into the indoor unit's WiFi port.
  2. You have paired the dongle once via the NetHome Plus app on your phone
     (NOT SmartHome — its get_token endpoint is currently broken; see msmart-ng
     issue #201).
  3. msmart-ng is installed on the Pi:  pip install msmart-ng

What this does:
  - Discovers the dongle on the LAN (UDP broadcast).
  - Prompts for your NetHome Plus email/password.
  - Fetches the local token + key from Midea's cloud (one-time round-trip).
  - Writes HVAC_DONGLE_IP / HVAC_DEVICE_ID / HVAC_TOKEN / HVAC_KEY to .env.
  - Tests a refresh against the dongle and prints the current HVAC state.

After this completes successfully, the WSGI app picks up the .env values on
the next request — no Apache restart needed.
"""

import asyncio
import getpass
import sys
from pathlib import Path

import config


def _die(msg, code=1):
    print(f"setup_hvac: {msg}", file=sys.stderr)
    sys.exit(code)


def _check_msmart_installed():
    try:
        import msmart  # noqa: F401
    except ImportError:
        _die("msmart-ng not installed. Run: pip install msmart-ng")


def _existing_keys():
    e = config.load_env()
    return {k: e.get(k, "").strip() for k in (
        "HVAC_DONGLE_IP", "HVAC_DEVICE_ID", "HVAC_TOKEN", "HVAC_KEY"
    )}


def _confirm_overwrite():
    have = _existing_keys()
    if any(have.values()):
        print("Existing HVAC keys found in .env:")
        for k, v in have.items():
            shown = (v[:8] + "…") if len(v) > 12 else v
            print(f"  {k} = {shown or '(empty)'}")
        ans = input("\nOverwrite? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            sys.exit(0)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

async def _discover():
    from msmart.discover import Discover
    print("Scanning LAN for Midea/Durastar dongle (UDP broadcast, ~5s)...")
    devices = await Discover.discover()
    if not devices:
        _die("No devices found. Check that the dongle is powered, paired via "
             "NetHome Plus, and on the same subnet as the Pi.")
    return devices


def _pick_device(devices):
    devices = list(devices)
    if len(devices) == 1:
        d = devices[0]
        print(f"Found 1 device: id={d.id} ip={d.ip} type=0x{d.type:02x}")
        return d
    print(f"Found {len(devices)} devices:")
    for i, d in enumerate(devices):
        print(f"  [{i}] id={d.id} ip={d.ip} type=0x{d.type:02x}")
    while True:
        ans = input(f"Pick one [0-{len(devices)-1}]: ").strip()
        try:
            return devices[int(ans)]
        except (ValueError, IndexError):
            print("Invalid choice.")


# ---------------------------------------------------------------------------
# Cloud token fetch (NetHome Plus)
# ---------------------------------------------------------------------------

async def _fetch_token_key(device_id):
    from msmart.cloud import Cloud

    print("\nFetching local token+key from NetHome Plus cloud (one-time).")
    print("Use the SAME credentials you used in the NetHome Plus phone app.")
    email    = input("NetHome Plus email: ").strip()
    password = getpass.getpass("NetHome Plus password: ")

    cloud = Cloud(email, password, account="NetHomePlus")
    try:
        await cloud.login()
    except Exception as e:
        _die(f"Cloud login failed: {e}\n"
             "Verify credentials. If you registered through SmartHome, that "
             "account type currently fails — re-register via NetHome Plus.")

    try:
        token, key = await cloud.get_token(str(device_id))
    except Exception as e:
        _die(f"Token fetch failed for device {device_id}: {e}")

    return token, key


# ---------------------------------------------------------------------------
# .env update
# ---------------------------------------------------------------------------

def _update_env(ip, device_id, token, key):
    """Replace or append HVAC_* keys in .env. Preserves other keys."""
    env_file = config.SCRIPT_DIR / ".env"
    lines = []
    if env_file.exists():
        with env_file.open() as f:
            for line in f:
                stripped = line.strip()
                if any(stripped.startswith(k + "=") for k in (
                    "HVAC_DONGLE_IP", "HVAC_DEVICE_ID", "HVAC_TOKEN", "HVAC_KEY"
                )):
                    continue
                lines.append(line.rstrip("\n"))

    if lines and lines[-1] != "":
        lines.append("")
    lines += [
        f"HVAC_DONGLE_IP={ip}",
        f"HVAC_DEVICE_ID={device_id}",
        f"HVAC_TOKEN={token}",
        f"HVAC_KEY={key}",
    ]
    env_file.write_text("\n".join(lines) + "\n")
    print(f"\nWrote 4 HVAC_* keys to {env_file}")


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

async def _verify():
    import hvac
    print("\nVerifying — fetching current state from dongle...")
    state = hvac.get_state(force=True)
    if state is None or state.get("reported") is None:
        _die("Verification failed — could not read state from dongle.")
    r = state["reported"]
    print("OK.")
    print(f"  Power:   {'ON' if r.get('power') else 'OFF'}")
    print(f"  Mode:    {r.get('mode')}")
    print(f"  Target:  {r.get('target_f')}°F ({r.get('target_c')}°C)")
    print(f"  Fan:     {r.get('fan_speed')}")
    if r.get("indoor_f") is not None:
        print(f"  Indoor:  {r.get('indoor_f')}°F ({r.get('indoor_c')}°C)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main_async():
    _check_msmart_installed()
    _confirm_overwrite()

    devices = await _discover()
    device  = _pick_device(devices)

    token, key = await _fetch_token_key(device.id)
    _update_env(device.ip, device.id, token, key)

    await _verify()
    print("\nDone. The WSGI app will pick up the new .env on its next request.")


if __name__ == "__main__":
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
