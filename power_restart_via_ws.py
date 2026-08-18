#!/usr/bin/env python3
"""Power-cycle the FT-710 through the running server's WebSocket API.

Uses the server's own power handlers, which implement the field-tested
safety rules: PS0 rejected mid-boot (15 s window), PS1 retried <=3 and
verified via FA read-back.  Usage: power_restart_via_ws.py <token>
"""
import asyncio
import json
import ssl
import sys
import time

import websockets

TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""
URI = f"wss://[::1]:8888/WSradio?token={TOKEN}"

# Self-signed cert is expected; disable verification for the loopback client.
_SSL = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


async def send_set(ws, field, value):
    await ws.send(json.dumps({"type": "set", "field": field, "value": value}))
    print(f">>> set {field} = {value}")


async def drain(ws, seconds):
    """Read and print messages (esp. error/state) for `seconds`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=min(1.0, deadline - loop.time()))
            try:
                d = json.loads(msg)
                t = d.get("type")
                if t == "error":
                    print("!!! ERROR:", d.get("message", ""))
                elif t == "state":
                    data = d.get("data", {})
                    print(f"    state: power_on={data.get('power_on')} "
                          f"serial={data.get('serial_connected')} "
                          f"tx={data.get('tx_status')}")
                else:
                    print(f"    {t}: {msg[:180]}")
            except Exception:
                print("    raw:", msg[:180])
        except asyncio.TimeoutError:
            continue
        except websockets.ConnectionClosed as e:
            print("    WS closed:", e)
            return


async def main():
    async with websockets.connect(URI, ssl=_SSL, max_size=None) as ws:
        print("WS connected. Waiting for fullState ...")
        await drain(ws, 3)  # absorb the initial fullState push

        # 1) Power OFF
        print("\n[1/2] Power OFF (PS0)")
        await send_set(ws, "power", False)
        await drain(ws, 5)

        # 2) Power ON (server retries PS1 and verifies via FA)
        print("\n[2/2] Power ON (PS1) — server retries & verifies, up to ~40 s")
        await send_set(ws, "power", True)
        await drain(ws, 45)

    print("\nWS session ended.")


if __name__ == "__main__":
    asyncio.run(main())
