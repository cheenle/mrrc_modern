#!/usr/bin/env python3
"""
FT-710 Power Cycle — sends PS0/PS1 via the running server's WebSocket API.
Usage: python3 _power_cycle_ws.py
"""
import asyncio
import json
import time
import requests
import websockets
import sys

SERVER = "http://localhost:8888"
WS_URL = "ws://localhost:8888/WSradio"
PASSWORD = "mrrc"  # default — change if you set MRRC_WEB_PASSWORD


def get_token() -> str:
    """Authenticate and get a WebSocket token."""
    resp = requests.post(
        f"{SERVER}/api/auth/login",
        json={"password": PASSWORD},
        timeout=5,
    )
    if resp.status_code != 200:
        print(f"Login failed: {resp.status_code} {resp.text}")
        sys.exit(1)
    data = resp.json()
    if not data.get("ok"):
        print(f"Login failed: {data}")
        sys.exit(1)
    token = data["token"]
    print(f"Got token: {token[:8]}...")
    return token


async def connect_ws(token: str, timeout: float = 5.0):
    """Connect to the radio WebSocket."""
    uri = f"{WS_URL}?token={token}"
    ws = await asyncio.wait_for(websockets.connect(uri), timeout=timeout)
    # Read initial fullState
    initial = await asyncio.wait_for(ws.recv(), timeout=5.0)
    data = json.loads(initial)
    if data.get("type") == "fullState":
        ps = data["data"].get("power_on", "?")
        print(f"Connected. Radio power state: {ps}")
    return ws


async def send_power(ws, on: bool):
    """Send a power on/off command via WebSocket."""
    cmd = {"type": "set", "field": "power", "value": on}
    await ws.send(json.dumps(cmd))
    # Wait for response (state update)
    try:
        resp = await asyncio.wait_for(ws.recv(), timeout=3.0)
        data = json.loads(resp)
        if "power_on" in str(data):
            print(f"  Power {'ON' if on else 'OFF'} acknowledged: {data}")
        else:
            print(f"  Response: {resp[:200]}")
    except asyncio.TimeoutError:
        print(f"  No immediate response (radio may be powering {'down' if not on else 'up'})")


async def power_cycle():
    print("=" * 50)
    print("FT-710 Power Cycle (via WebSocket API)")
    print("=" * 50)

    # Step 0: Auth
    print("\nAuthenticating...")
    token = get_token()

    # Step 1: Connect and turn OFF
    print("\n[1/2] Turning OFF...")
    ws = await connect_ws(token)
    await send_power(ws, False)
    await ws.close()

    # Wait for radio to power down
    wait = 4
    print(f"\nWaiting {wait} seconds for radio to power down...")
    await asyncio.sleep(wait)

    # Step 2: Reconnect and turn ON
    print("\n[2/2] Turning ON...")
    try:
        ws2 = await connect_ws(token, timeout=10.0)
        await send_power(ws2, True)
        await ws2.close()
        print("\n✅ Power cycle complete! FT-710 should be restarting.")
    except Exception as e:
        print(f"\n⚠️  Could not reconnect to turn ON: {e}")
        print("The server may have lost serial connection after PS0.")
        print("Try turning the radio on manually, or restart the server.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(power_cycle())
