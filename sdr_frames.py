"""`sdr_pipe` 与 `sdr_hub` 之间的分帧契约。

与 `scope_pipe` 同一套路：2 字节标签 + 长度 + 负载；JSON 帧带 u32 长度，
二进制帧把序号/通道号放在固定位置。两种进程都只依赖这个模块，避免各写一份。

    P1 <u32 len> <json>            pipe → server  状态
    S1 <u16 seq> <u16 n> <u8*n>    pipe → server  频谱
    A1 <u8 chid> <u16 seq> <u8 rms> <u16 len> <opus>   pipe → server  音频（rms 供静噪/电平）
    C1 <u32 len> <json>            server → pipe  命令

解析器是流式的：喂任意切片，凑够一帧就吐一帧。
"""

from __future__ import annotations

import json
import struct

TAG_STATUS = b"P1"
TAG_SPECTRUM = b"S1"
TAG_AUDIO = b"A1"
TAG_COMMAND = b"C1"

MAX_JSON = 1 << 20        # 1 MB 上限，坏帧不至于把内存吃光


def encode_status(obj: dict) -> bytes:
    body = json.dumps(obj, separators=(",", ":")).encode()
    return TAG_STATUS + struct.pack("<I", len(body)) + body


def encode_spectrum(seq: int, bins: bytes) -> bytes:
    if len(bins) > 0xFFFF:
        raise ValueError("spectrum payload too large")
    return TAG_SPECTRUM + struct.pack("<HH", seq & 0xFFFF, len(bins)) + bins


def encode_audio(chid: int, seq: int, payload: bytes, rms: float = 0.0) -> bytes:
    if not 0 <= chid <= 0xFF:
        raise ValueError("chid out of range")
    if len(payload) > 0xFFFF:
        raise ValueError("audio payload too large")
    level = max(0, min(255, int(round(float(rms) * 255.0))))
    return TAG_AUDIO + struct.pack("<BBHH", chid, seq & 0xFFFF, level, len(payload)) + payload


def encode_command(obj: dict) -> bytes:
    body = json.dumps(obj, separators=(",", ":")).encode()
    return TAG_COMMAND + struct.pack("<I", len(body)) + body


class FrameParser:
    """流式解析器：feed() 返回本轮解出的帧列表。"""

    def __init__(self):
        self._buf = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> list[tuple]:
        self._buf.extend(data)
        out: list[tuple] = []
        while True:
            frame = self._take_one()
            if frame is None:
                return out
            if frame == "skip":      # 丢掉一个字节后继续，不等下一次 feed
                continue
            out.append(frame)

    def _take_one(self):
        buf = self._buf
        if len(buf) < 2:
            return None
        tag = bytes(buf[:2])
        if tag in (TAG_STATUS, TAG_COMMAND):
            if len(buf) < 6:
                return None
            (ln,) = struct.unpack_from("<I", buf, 2)
            if ln > MAX_JSON:
                self._resync()
                return "skip"
            if len(buf) < 6 + ln:
                return None
            body = bytes(buf[6:6 + ln])
            del buf[:6 + ln]
            try:
                obj = json.loads(body)
            except Exception:
                self.errors += 1
                obj = {}
            return ("status" if tag == TAG_STATUS else "command", obj)

        if tag == TAG_SPECTRUM:
            if len(buf) < 6:
                return None
            seq, n = struct.unpack_from("<HH", buf, 2)
            if len(buf) < 6 + n:
                return None
            bins = bytes(buf[6:6 + n])
            del buf[:6 + n]
            return ("spectrum", seq, bins)

        if tag == TAG_AUDIO:
            if len(buf) < 8:
                return None
            chid, seq, level, ln = struct.unpack_from("<BBHH", buf, 2)
            if len(buf) < 8 + ln:
                return None
            payload = bytes(buf[8:8 + ln])
            del buf[:8 + ln]
            return ("audio", chid, seq, level / 255.0, payload)

        # 未知标签：丢一个字节重新对齐（与 scope_pipe 的容错一致）
        self.errors += 1
        del buf[:1]
        return "skip"

    def _resync(self):
        self.errors += 1
        del self._buf[:1]
