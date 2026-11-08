#!/usr/bin/env python3
"""SunSDR2 DX field check; read-only unless --tx-check --allow-tx is given."""
from __future__ import annotations

import argparse
import asyncio
import sys

import numpy as np

import config
from backends.sunsdr.engine import SunSDREngine


def tx_check_permitted(tx_check: bool, allow_tx: bool) -> bool:
    return bool(tx_check and allow_tx)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=config.SUNSDR_HOST,
                        help="radio address (default: MRRC_SUNSDR_HOST)")
    parser.add_argument("--local-host", default=config.SUNSDR_LOCAL_HOST,
                        help="local UDP bind address (default: auto)")
    parser.add_argument("--control-port", type=int,
                        default=config.SUNSDR_CONTROL_PORT)
    parser.add_argument("--iq-port", type=int, default=config.SUNSDR_IQ_PORT)
    parser.add_argument("--sample-rate", type=int,
                        default=config.SUNSDR_SAMPLE_RATE,
                        choices=(39062, 78125, 156250, 312500))
    parser.add_argument("--wait", type=float, default=2.0,
                        help="seconds to observe IQ/telemetry (default: 2)")
    parser.add_argument("--tx-check", action="store_true",
                        help="key a 200 ms test (also requires --allow-tx)")
    parser.add_argument("--allow-tx", action="store_true",
                        help="explicit consent for a dummy-load TX check")
    return parser


def format_report(engine: SunSDREngine, tx_result: str) -> str:
    telemetry = engine.latest_telemetry or {}
    spectrum = engine.get_spectrum()
    lines = [
        "# SunSDR2 DX field report",
        "",
        f"- radio: `{engine.host}`; local: `{engine.local_host or 'auto'}`",
        f"- UDP control/IQ: `{engine.control_port}/{engine.iq_port}`",
        f"- connected: **{engine.connected}**; sample rate: `{engine.sample_rate}`",
        f"- packets/bytes: `{engine.recv_packets}/{engine.recv_bytes}`; bad: `{engine.bad_packets}`",
        f"- spectrum: `{len(spectrum)} bins`, range `{min(spectrum)}..{max(spectrum)}`",
        f"- S-meter (uncalibrated): `{engine.s_meter}`",
        f"- forward W / average W: `{telemetry.get('forward_w')}` / `{telemetry.get('avg_w')}`",
        f"- supply V / PA C: `{telemetry.get('volts')}` / `{telemetry.get('pa_temp_c')}`",
        f"- PTT requested/confirmed: `{engine.ptt_requested}/{engine.ptt_confirmed}`",
        f"- drops RX/TX; underruns: `{engine.rx_drops}/{engine.tx_drops}; {engine.underruns}`",
        f"- TX check: **{tx_result}**",
        "",
        "No reverse-power field exists; SWR is deliberately not reported.",
    ]
    return "\n".join(lines) + "\n"


async def run(args) -> int:
    engine = SunSDREngine(args.host, local_host=args.local_host,
                          control_port=args.control_port, iq_port=args.iq_port,
                          sample_rate=args.sample_rate)
    if not await engine.start():
        print("cannot initialize the SunSDR2 DX UDP session", file=sys.stderr)
        return 2
    tx_result = "not requested"
    failed = False
    try:
        await asyncio.sleep(max(0.0, args.wait))
        if args.tx_check and not args.allow_tx:
            tx_result = "refused: --tx-check also requires --allow-tx"
        elif tx_check_permitted(args.tx_check, args.allow_tx):
            try:
                if not await engine.set_ptt(True):
                    raise RuntimeError("PTT assert was not sent")
                await engine.start_tx()
                for frame in range(10):
                    t = (np.arange(960) + frame * 960) / 48000.0
                    pcm = np.rint(0.2 * 32767 * np.sin(2 * np.pi * 700 * t))
                    engine.feed_tx_audio(pcm.astype("<i2").tobytes())
                    await asyncio.sleep(0.02)
                tx_result = "200 ms / 700 Hz tone; observe on a dummy load/monitor"
            except Exception as exc:
                failed = True
                tx_result = f"failed: {exc}"
            finally:
                await engine.set_ptt(False)
                await engine.stop_tx()
        print(format_report(engine, tx_result), end="")
    except Exception as exc:
        failed = True
        print(format_report(engine, f"diagnostic error: {exc}"), end="")
    finally:
        if engine.ptt_requested or engine.ptt_confirmed:
            await engine.set_ptt(False)
        await engine.stop()
    return 1 if failed or not engine.recv_packets else 0


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
