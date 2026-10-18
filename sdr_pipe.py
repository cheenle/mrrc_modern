#!/usr/bin/env python3
"""sdr_pipe -- 独占 SDR 设备的子进程，把 IQ 变成频谱帧与每通道 Opus 音频。

两种数据源，同一条块循环：
  * --device            真设备（经 libsdr_api_shim.so → libsdrplay_api）
  * --offline FILE      离线 IQ 文件（int16 交错），用来在没有硬件的机器上跑通全链路

与 server.py 的契约见 sdr_frames.py。控制命令从 stdin 读（C1 帧）：
  {"cmd":"tune","hz":7074000} / {"cmd":"channel","chid":0,"offset_hz":...,"mode":..,"bw_hz":..}
  {"cmd":"clear","chid":0} / {"cmd":"gain","grdb":40,"lna":2} / {"cmd":"quit"}

退出码：0 正常 / 2 设备不可用 / 3 设备被拔掉（服务端据此按 1/2/5/10 s 退避重启）。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import select
import sys
import time

import numpy as np

import sdr_frames as F
from sdr_dsp_binding import Channelizer, MODE_AM

RX_RATE = 48000
BLOCK_MS = 20
FFT_N = 2048
SPEC_BINS = 1024
DB_FLOOR = -100.0
DB_CEIL = -10.0


def _now() -> float:
    return time.monotonic()


class SdrPipe:
    def __init__(self, fs_in: float, out, max_ch: int = 8, bitrate: int = 32000):
        self.fs_in = fs_in
        self.out = out
        self.max_ch = max_ch
        self.bitrate = bitrate
        self.cz = Channelizer(fs_in, RX_RATE, max_ch)
        self.block = int(fs_in * BLOCK_MS / 1000)
        self.center = 7_074_000.0
        self.gain = 40
        self.lna = 2
        self.channels: dict[int, tuple[int, int, int]] = {}
        self._encoders: dict[int, object] = {}
        self._seq_s = 0
        self._seq_a: dict[int, int] = {}
        self._window = np.hanning(FFT_N).astype(np.float32)
        self._t_start = _now()
        self._opus_ok = True
        self._blocks = 0

    # -- 输出 -------------------------------------------------------------
    def emit_status(self, extra: dict | None = None) -> None:
        obj = {
            "center_hz": int(self.center),
            "fs": int(self.fs_in),
            "gain": self.gain,
            "lna": self.lna,
            "channels": sorted(self.channels),
            "blocks": self._blocks,
            "opus": self._opus_ok,
            "uptime_s": round(_now() - self._t_start, 1),
        }
        if extra:
            obj.update(extra)
        self.out.write(F.encode_status(obj))
        self.out.flush()

    def emit_spectrum(self, iq: np.ndarray) -> None:
        if iq.size < FFT_N * 2:
            return
        z = iq[:FFT_N * 2].astype(np.float32)
        z = z[0::2] + 1j * z[1::2]
        spec = np.abs(np.fft.fftshift(np.fft.fft(z * self._window)))
        db = 20.0 * np.log10(spec + 1e-9)
        norm = (db - DB_FLOOR) / (DB_CEIL - DB_FLOOR)
        bins = np.clip(norm * 255.0, 0, 255).astype(np.uint8)
        if bins.size > SPEC_BINS:                       # 2048 → 1024，取平均
            bins = bins.reshape(SPEC_BINS, -1).mean(axis=1).astype(np.uint8)
        self._seq_s = (self._seq_s + 1) & 0xFFFF
        self.out.write(F.encode_spectrum(self._seq_s, bins.tobytes()))

    # -- 命令 -------------------------------------------------------------
    def handle_command(self, obj: dict) -> bool:
        cmd = obj.get("cmd")
        if cmd == "tune":
            hz = float(obj.get("hz", 0))
            if 10000.0 <= hz <= 2e9:
                self.center = hz
                return True
            return False
        if cmd == "channel":
            try:
                chid = int(obj["chid"])
                off = float(obj["offset_hz"])
                mode = int(obj["mode"])
                bw = float(obj["bw_hz"])
                self.cz.set_channel(chid, off, mode, bw)
            except (KeyError, ValueError, TypeError):
                return False
            self.channels[chid] = (int(off), mode, int(bw))
            return True
        if cmd == "clear":
            chid = int(obj.get("chid", -1))
            if 0 <= chid < self.max_ch:
                self.cz.clear_channel(chid)
                self.channels.pop(chid, None)
                enc = self._encoders.pop(chid, None)
                if enc is not None:
                    try:
                        enc.close()
                    except Exception:
                        pass
                return True
            return False
        if cmd == "gain":
            self.gain = int(obj.get("grdb", self.gain))
            self.lna = int(obj.get("lna", self.lna))
            return True
        if cmd == "quit":
            return False
        return False

    # -- 块处理 -----------------------------------------------------------
    def process_block(self, iq: np.ndarray, overloads: int = 0) -> None:
        self._blocks += 1
        self.emit_spectrum(iq)
        if not self.channels:
            return
        pcm, rms = self.cz.process(iq, self.max_ch)
        for chid in sorted(self.channels):
            data = pcm[chid]
            if not data:
                continue
            level = rms[chid] if chid < len(rms) else 0.0
            enc = self._encoder_for(chid)
            if enc is None:
                continue
            try:
                packets = enc.push(data)
            except Exception:
                packets = []
            seq = self._seq_a.get(chid, 0)
            for pkt in packets:
                seq = (seq + 1) & 0xFFFF
                self.out.write(F.encode_audio(chid, seq, pkt, level))
            self._seq_a[chid] = seq
        self.out.flush()

    def _encoder_for(self, chid: int):
        if not self._opus_ok:
            return None
        enc = self._encoders.get(chid)
        if enc is None:
            try:
                from opus_rx import RxOpusEncoder
                enc = RxOpusEncoder(bitrate=self.bitrate)
            except Exception as exc:                    # libopus 缺失：降级
                print(f"sdr_pipe: opus unavailable ({exc})", file=sys.stderr, flush=True)
                self._opus_ok = False
                return None
            self._encoders[chid] = enc
        return enc

    def close(self) -> None:
        for enc in self._encoders.values():
            try:
                enc.close()
            except Exception:
                pass
        self._encoders.clear()
        self.cz.close()


# -- 数据源 ------------------------------------------------------------------

def _read_exact(f, nbytes: int) -> bytes:
    out = b""
    while len(out) < nbytes:
        chunk = f.read(nbytes - len(out))
        if not chunk:
            break
        out += chunk
    return out


def run_offline(pipe: SdrPipe, path: str, seconds: float, realtime: bool) -> int:
    block_bytes = pipe.block * 4
    block_s = pipe.block / pipe.fs_in
    t0 = _now()
    emitted = 0
    with open(path, "rb") as f:
        while True:
            raw = _read_exact(f, block_bytes)
            if len(raw) < block_bytes // 2:
                break
            iq = np.frombuffer(raw, dtype=np.int16)
            pipe.process_block(iq)
            emitted += 1
            if emitted % 50 == 0:
                pipe.emit_status()
            if realtime:
                drift = emitted * block_s - (_now() - t0)
                if drift > 0:
                    time.sleep(drift)
            if seconds > 0 and emitted * block_s >= seconds:
                break
    pipe.emit_status({"offline": True, "blocks_emitted": emitted})
    return 0


def run_device(pipe: SdrPipe, shim_path: str, max_seconds: float = 0.0) -> int:
    lib = ctypes.CDLL(shim_path)
    lib.sdrshim_info.restype = ctypes.c_char_p
    lib.sdrshim_open.restype = ctypes.c_int
    lib.sdrshim_start.restype = ctypes.c_int
    lib.sdrshim_config.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_int, ctypes.c_int]
    lib.sdrshim_read.argtypes = [ctypes.POINTER(ctypes.c_short), ctypes.c_long, ctypes.c_int]
    lib.sdrshim_read.restype = ctypes.c_long
    lib.sdrshim_set_gain.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.sdrshim_set_gain.restype = ctypes.c_int
    lib.sdrshim_overloads.restype = ctypes.c_int
    lib.sdrshim_removed.restype = ctypes.c_int

    if lib.sdrshim_open() != 0:
        pipe.emit_status({"error": lib.sdrshim_info().decode(errors="replace")})
        return 2
    if lib.sdrshim_config(pipe.fs_in, pipe.center, pipe.gain, pipe.lna) != 0:
        pipe.emit_status({"error": "config rejected"})
        return 2
    if lib.sdrshim_start() != 0:
        pipe.emit_status({"error": lib.sdrshim_info().decode(errors="replace")})
        return 2

    info = lib.sdrshim_info().decode(errors="replace")
    pipe.emit_status({"device": info})
    buf = (ctypes.c_short * (pipe.block * 2))()
    parser = F.FrameParser()
    stdin = sys.stdin.buffer
    stdin_open = True          # EOF 只表示没有更多命令；父进程死了会在写 stdout 时报 BrokenPipe
    block_s = pipe.block / pipe.fs_in
    applied = {"hz": pipe.center, "grdb": pipe.gain, "lna": pipe.lna}
    t0 = _now()
    last_status = t0
    pending: list[np.ndarray] = []
    try:
        while True:
            if stdin_open and select.select([stdin], [], [], 0)[0]:
                data = os.read(stdin.fileno(), 65536)
                if not data:
                    stdin_open = False
                for frame in parser.feed(data):
                    kind, obj = frame[0], frame[1]
                    if kind != "command":
                        continue
                    if obj.get("cmd") == "quit":
                        return 0
                    if not pipe.handle_command(obj):
                        continue
                    if obj.get("cmd") == "tune":
                        applied["hz"] = pipe.center
                    if obj.get("cmd") == "gain":
                        applied["grdb"] = pipe.gain
                        applied["lna"] = pipe.lna
                        lib.sdrshim_set_gain(pipe.gain, pipe.lna)
            n = lib.sdrshim_read(buf, pipe.block, 100)
            if n > 0:
                pending.append(np.frombuffer(buf, dtype=np.int16, count=n * 2).copy())
                have = sum(chunk.size for chunk in pending) // 2
                if have >= pipe.block:
                    block = np.concatenate(pending)[: pipe.block * 2]
                    rest = np.concatenate(pending)[pipe.block * 2:]
                    pending = [k for k in [rest] if k.size] if rest.size else []
                    pipe.process_block(block, lib.sdrshim_overloads())
            if lib.sdrshim_removed():
                pipe.emit_status({"error": "device removed"})
                return 3
            if _now() - last_status > 1.0:
                pipe.emit_status({"overloads": int(lib.sdrshim_overloads())})
                last_status = _now()
            if max_seconds > 0 and (_now() - t0) > max_seconds:
                return 0
    finally:
        lib.sdrshim_stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SDR pipe (device or offline IQ file)")
    ap.add_argument("--device", action="store_true", help="use the SDRplay device via the C shim")
    ap.add_argument("--offline", metavar="FILE", help="read int16 IQ from this file instead")
    ap.add_argument("--shim", default=os.environ.get("MRRC_SDR_SHIM", "libsdr_api_shim.so"))
    ap.add_argument("--fs", type=float, default=2_400_000.0)
    ap.add_argument("--max-channels", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=0.0, help="stop after N seconds (0 = forever)")
    ap.add_argument("--no-rt", action="store_true", help="offline: do not pace at real time")
    ap.add_argument("--gain", type=int, default=int(os.environ.get("MRRC_SDR_GAIN_DB", "40")))
    ap.add_argument("--lna", type=int, default=int(os.environ.get("MRRC_SDR_LNA", "2")))
    ap.add_argument("--channels", default="0:0:0:6000",
                    help="initial channels chid:offset:mode:bw, comma separated (offline/headless tests)")
    args = ap.parse_args(argv)

    pipe = SdrPipe(args.fs, sys.stdout.buffer, max_ch=args.max_channels)
    pipe.gain = max(20, min(59, args.gain))
    pipe.lna = max(0, min(3, args.lna))       # RSP1 只有 0..3，越界 Init 会失败
    for spec in [c for c in args.channels.split(",") if c]:
        try:
            chid, off, mode, bw = (int(float(v)) for v in spec.split(":"))
        except ValueError:
            print(f"sdr_pipe: bad --channels entry {spec!r}", file=sys.stderr)
            return 2
        pipe.handle_command({"cmd": "channel", "chid": chid, "offset_hz": off,
                             "mode": mode, "bw_hz": bw})

    try:
        if args.offline:
            return run_offline(pipe, args.offline, args.seconds, realtime=not args.no_rt)
        if args.device:
            return run_device(pipe, args.shim, args.seconds)
        ap.error("one of --device / --offline is required")
        return 2
    except BrokenPipeError:
        return 0
    finally:
        pipe.close()


if __name__ == "__main__":
    sys.exit(main())
