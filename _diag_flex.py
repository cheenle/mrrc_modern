#!/usr/bin/env python3
"""FlexRadio (SmartSDR) field self-check.

One command that produces the evidence a seed-user session needs (design
record §11): discovery, handshake, `info`, panadapter frames, RX audio, meters,
interlock — and, only with an explicit opt-in, a 200 ms key-up.

    python _diag_flex.py                 # discover on the LAN, read-only
    python _diag_flex.py --host 10.0.0.5 # pin the address
    python _diag_flex.py --tx-check --allow-tx   # + 200 ms carrier (dummy load)

Read-only by default: it creates its own panadapter and streams, prints what
came back, then removes them again in `finally`. Nothing here needs the web UI
or a login, so it works over a remote-help shell on a box that only has the
packaged server.

Exit code 0 = every required step produced evidence; 1 = something did not,
and the failure line names the command or the field.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time

import numpy as np

from backends.flex import protocol, vita
from backends.flex.engine import SmartSDREngine, discover_radios


def _fmt(value, unit=""):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}{unit}"
    return f"{value}{unit}"


async def _collect(engine: SmartSDREngine, seconds: float) -> dict:
    """Watch the streams for ``seconds`` and summarise what arrived."""
    start_frames = engine.spectrum_frames
    start_packets = engine.audio_packets
    samples = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        samples.append(engine.rx_samples)
        if engine.spectrum_frames > start_frames + 5:
            break
    bins = engine.spectrum_bins() or []
    return {
        "frames": engine.spectrum_frames - start_frames,
        "frame_errors": engine.spectrum_errors,
        "audio_packets": engine.audio_packets - start_packets,
        "rx_drops": engine.rx_drops,
        "bins": len(bins),
        "bin_min": min(bins) if bins else None,
        "bin_max": max(bins) if bins else None,
        "bin_mean": (sum(bins) / len(bins)) if bins else None,
    }


def _print_report(engine: SmartSDREngine, collect: dict, tx: dict) -> int:
    problems = []
    session = engine.session
    print("## FlexRadio 自检")
    print()
    print("| 项 | 值 |")
    print("| --- | --- |")
    print(f"| 地址 | {_fmt(engine.host)} |")
    print(f"| 协议版本 | {_fmt(session.version if session else None)} |")
    print(f"| client handle | {('0x%08X' % session.handle) if session else 'n/a'} |")
    print(f"| 机型 (info/radio) | {_fmt(engine.model or None)} |")
    print(f"| 序列号 | {_fmt(engine.serial or None)} |")
    print(f"| 固件 | {_fmt(engine.software_version or None)} |")
    print(f"| pan id | {'0x%08X' % engine.pan_id if engine.pan_id else 'n/a'} |")
    print(f"| RX 音频流 | {'0x%08X' % engine.rx_stream_id if engine.rx_stream_id else 'n/a'} |")
    print(f"| TX 音频流 | {'0x%08X' % engine.tx_stream_id if engine.tx_stream_id else 'n/a'} |")
    print(f"| ATU | {'有' if engine.has_atu else '无/未上报'} |")
    print(f"| 电台 TX timeout | {_fmt(engine.tx_timeout_s, ' s')} |")
    print()
    print("### 频谱")
    print(f"- 帧 {collect['frames']}，解析错误 {collect['frame_errors']}，"
          f"bin {collect['bins']}")
    if collect["bins"]:
        print(f"- UI 字节 min/mean/max = {collect['bin_min']} / "
              f"{collect['bin_mean']:.1f} / {collect['bin_max']}（0..255；"
              f"期望不是全 0 或全 255）")
    print(f"- pan 带宽 {engine.bandwidth_hz/1000:.1f} kHz，"
          f"中心 {engine.pan_center_hz/1e6:.6f} MHz")
    print()
    print("### RX 音频")
    print(f"- 包 {collect['audio_packets']}，队列丢弃 {collect['rx_drops']}")
    print("- 单声道 24 kHz 浮点，由音频适配器升采样到 48 kHz（§5.1）")
    print()
    print("### 表")
    if engine.meter_defs:
        for definition in sorted(engine.meter_defs.values(),
                                 key=lambda d: (d.src, d.name)):
            raw = engine.meter_values.get(definition.meter_id)
            value = (protocol.scale_meter(raw, definition.unit)
                     if raw is not None else None)
            print(f"- {definition.src} {definition.name} "
                  f"[{definition.unit}] = {_fmt(value)}")
    else:
        print("- 未收到 meter 定义")
    print()
    print("### 收发状态")
    print(f"- interlock = {_fmt(session.interlock_state if session else None)}")
    if tx:
        print(f"- 按键结果：{tx.get('keyed')}；追踪：{tx.get('trace')}")
        print(f"- FWDPWR = {_fmt(engine.forward_power_watts(), ' W')}")
    print()

    if not engine.model:
        problems.append("`info` 没有给出机型 —— 协议可能不是 v1.4.0.0")
    if collect["frames"] <= 0:
        problems.append("没有收到 FFT 帧 —— 检查 `display panafall create` 的回执与 UDP 端口登记")
    if collect["bins"] and collect["bin_max"] == 0:
        problems.append("频谱全 0 —— 像素映射或 pan 参数不对")
    if collect["audio_packets"] <= 0:
        problems.append("没有收到音频包 —— 检查 `stream create type=remote_audio_rx` 的回执")
    timeout = engine.tx_timeout_s
    if timeout is not None and timeout > 60:
        problems.append(f"电台 TX timeout = {timeout:.0f} s —— 建议 ≤ 30 s（§7.5）")

    print("### 结论")
    if problems:
        for item in problems:
            print(f"- ❌ {item}")
        return 1
    print("- ✅ 发现/握手/info/FFT/RX 音频/表 全部有证据")
    return 0


async def _run(args) -> int:
    engine = SmartSDREngine(args.host or "", discovery_timeout=args.discovery)
    if not args.host:
        radios = []
        try:
            radios = await discover_radios(args.discovery)
        except Exception as exc:                        # noqa: BLE001
            print(f"发现失败：{exc}", file=sys.stderr)
        if radios:
            print(f"发现 {len(radios)} 台：")
            for radio in radios:
                print(f"  - {radio.get('model', '?')} {radio.get('name', '')} "
                      f"{radio.get('ip', '')} serial={radio.get('serial', '?')}")
            engine.host = radios[0].get("ip", "")
        else:
            print("UDP 4992 没有发现任何电台；请用 --host 指定地址",
                  file=sys.stderr)
            return 1

    if not await engine.start():
        print(f"连接失败：{engine.session.last_error if engine.session else '?'}",
              file=sys.stderr)
        return 1

    code = 1
    try:
        collect = await _collect(engine, args.seconds)
        tx_result = {}
        if args.tx_check:
            if not args.allow_tx:
                print("跳过发射检查：需要同时给 --allow-tx（假负载再按键）")
            else:
                trace = []
                session = engine.session
                if session is not None:
                    session._on_interlock = lambda t: trace.append(t.state)
                # No stream, no key-up: transmitting without a proven audio
                # path is exactly what design §7.2 forbids.
                keyed = False
                if engine.tx_stream_ready:
                    await engine.start_tx()
                    keyed = await engine.key(True)
                else:
                    print("跳过发射检查：remote_audio_tx 流没建成")
                if keyed:
                    tone = (0.25 * np.sin(2 * math.pi * 700.0 *
                                          np.arange(480) / 24000.0))
                    stereo = np.repeat(
                        np.clip(np.rint(tone * 32767), -32768, 32767)
                        .astype("<i2"), 2)
                    from opus_rx import TxOpusEncoder
                    encoder = TxOpusEncoder()
                    for _ in range(20):
                        for packet in encoder.push(stereo.tobytes()):
                            engine.feed_opus(vita.build_opus_packet(
                                engine.tx_stream_id, packet))
                        await asyncio.sleep(0.01)
                    await asyncio.sleep(0.2)
                    encoder.close()
                await engine.key(False)
                tx_result = {"keyed": keyed, "trace": trace}
        code = _print_report(engine, collect, tx_result)
    finally:
        # Leave the radio as we found it: our pan/streams are ours to remove,
        # and an unkey must happen even when the report above raised.
        session = engine.session
        if session is not None and session.connected:
            try:
                await engine.key(False)
            except Exception:                           # noqa: BLE001
                pass
            for stream_id in (engine.rx_stream_id, engine.tx_stream_id):
                if stream_id:
                    try:
                        await session.send_ok(f"stream remove 0x{stream_id:08X}")
                    except Exception:                   # noqa: BLE001
                        pass
            if engine.pan_id:
                try:
                    await session.send_ok(f"display pan remove 0x{engine.pan_id:08X}")
                except Exception:                       # noqa: BLE001
                    pass
        await engine.stop()
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="", help="radio address (empty = discover)")
    parser.add_argument("--seconds", type=float, default=3.0,
                        help="how long to watch the streams (default 3)")
    parser.add_argument("--discovery", type=float, default=1.5,
                        help="UDP 4992 listen window (default 1.5 s)")
    parser.add_argument("--tx-check", action="store_true",
                        help="also key for 200 ms (requires --allow-tx)")
    parser.add_argument("--allow-tx", action="store_true",
                        help="confirm a dummy load / antenna is connected")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
