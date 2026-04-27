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
# Discovery + cloud authentication (single call)
# ---------------------------------------------------------------------------
#
# msmart-ng 2025.12.0's Discover.discover() does the cloud handshake itself
# when given account+password (auto_connect=True is the default). The returned
# devices come back with .token and .key already populated — no separate
# get_token() round-trip needed.

async def _discover_authenticated():
    from msmart.discover import Discover

    print("Sign in with NetHome Plus credentials.")
    print("Use the SAME account you paired the dongle with on the phone app.")
    print("(NOT SmartHome — see msmart-ng issue #201.)")
    email    = input("NetHome Plus email: ").strip()
    password = getpass.getpass("NetHome Plus password: ")

    print("\nDiscovering & authenticating dongle (UDP broadcast + cloud handshake, ~10s)...")
    try:
        devices = await Discover.discover(
            account=email,
            password=password,
            auto_connect=True,
        )
    except Exception as e:
        _die(f"Discovery / cloud auth failed: {e}\n"
             "If credentials look right but auth still fails, you may be on a "
             "SmartHome account — re-register via NetHome Plus and try again.")

    if not devices:
        _die("No devices discovered. Check that the dongle is powered, paired "
             "via NetHome Plus, and on the same subnet as the Pi.")
    return devices


def _device_type_str(d):
    """Format the device type for display — handles enum or int."""
    t = getattr(d, "type", None)
    if t is None:
        return "?"
    if isinstance(t, int):
        return f"0x{t:02x}"
    return str(t)


def _pick_device(devices):
    devices = list(devices)
    if len(devices) == 1:
        d = devices[0]
        print(f"Found 1 device: id={d.id} ip={d.ip} type={_device_type_str(d)}")
        return d
    print(f"Found {len(devices)} devices:")
    for i, d in enumerate(devices):
        print(f"  [{i}] id={d.id} ip={d.ip} type={_device_type_str(d)}")
    while True:
        ans = input(f"Pick one [0-{len(devices)-1}]: ").strip()
        try:
            return devices[int(ans)]
        except (ValueError, IndexError):
            print("Invalid choice.")


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

    devices = await _discover_authenticated()
    device  = _pick_device(devices)

    if not device.token or not device.key:
        _die("Device discovered but no token/key returned. The cloud auth step "
             "didn't populate credentials — try re-pairing on NetHome Plus.")

    _update_env(device.ip, device.id, device.token, device.key)

    await _verify()
    print("\nDone. The WSGI app will pick up the new .env on its next request.")


if __name__ == "__main__":
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
