#!/usr/bin/env python3
"""On-box microphone: Xiaomi Bluetooth voice remote (ATVV) → TX PCM.

The remote's microphone is not a sound card.  It is Google's ATVV GATT
service (``AB5E0001-…``): the host subscribes to the audio characteristic,
answers the voice key with ``MIC_OPEN`` (``0C 00``), and receives IMA ADPCM
frames (16 kHz mono, 120 bytes / 15 ms) until ``MIC_CLOSE`` (``0D``).

This daemon decodes those frames, upsamples to the 48 kHz the server's TX
path expects, and writes raw int16 PCM to ``/run/mrrc/mic.pcm`` (0600, same
user as the server).  ``server.py`` applies the box TX-mic gain and feeds it
into the same queue the browser microphone uses.  PTT itself stays with
``local_ctl.py``: the voice key already keys the radio through the local
control socket.

Protocol references:
  goodtiger/mi-remote-linux        (atvv.py, adpcm.py, ble_client.py)
  vlln/xiaomi-remote-voice-coding-adapter (core/atvv.rs — the 0x04-without-0x08 case)
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import signal
import struct
import sys
import time

import numpy as np
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType, MessageType
from dbus_fast.message import Message

log = logging.getLogger("mrrc.remotemic")

SERVICE = "ab5e0001-5a21-4f05-bc7d-af01f617b664"
TX_CHAR = "ab5e0002-5a21-4f05-bc7d-af01f617b664"
AUDIO_CHAR = "ab5e0003-5a21-4f05-bc7d-af01f617b664"
CTRL_CHAR = "ab5e0004-5a21-4f05-bc7d-af01f617b664"

GET_CAPS = bytes([0x0A, 0x01, 0x00, 0x00, 0x03, 0x03])
OP_CAPS, OP_MIC_REQUEST, OP_AUDIO_START = 0x0B, 0x08, 0x04
OP_SYNC, OP_AUDIO_STOP = 0x0A, 0x00
CODEC_16KHZ = 0x02

MIC_SOCKET = os.environ.get("MRRC_MIC_SOCKET", "/run/mrrc/mic.pcm")
REMOTE_MAC = (os.environ.get("MRRC_REMOTE_MAC") or "").strip() or None
GAIN_DB = float(os.environ.get("MRRC_REMOTE_MIC_GAIN_DB", "12") or 12.0)
GAIN = math.pow(10.0, GAIN_DB / 20.0)
#: Decoder warm-up frames discarded after AUDIO_START (120 bytes = 15 ms).
SKIP_FRAMES = int(os.environ.get("MRRC_REMOTE_MIC_SKIP", "8") or 8)

STEP_TABLE = np.array([
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41,
    45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190,
    209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724,
    796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272,
    2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132,
    7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350,
    22385, 24623, 27086, 29794, 32767,
], dtype=np.int32)
INDEX_TABLE = np.array([-1, -1, -1, -1, 2, 4, 6, 8], dtype=np.int32)


class Decoder:
    """IMA/DVI ADPCM (high nibble first) — identical to audioop.adpcm2lin."""

    def __init__(self) -> None:
        self.predictor = 0
        self.step_index = 0

    def reset(self, predictor: int, step_index: int) -> None:
        self.predictor = max(-32768, min(32767, int(predictor)))
        self.step_index = max(0, min(88, int(step_index)))

    def decode(self, data: bytes) -> np.ndarray:
        out = np.empty(len(data) * 2, dtype=np.int16)
        p, si = self.predictor, self.step_index
        for k, byte in enumerate(data):
            for j, nib in enumerate((byte >> 4, byte & 0x0F)):
                step = int(STEP_TABLE[si])
                diff = step >> 3
                if nib & 4:
                    diff += step
                if nib & 2:
                    diff += step >> 1
                if nib & 1:
                    diff += step >> 2
                p += -diff if nib & 8 else diff
                p = max(-32768, min(32767, p))
                si = max(0, min(88, si + int(INDEX_TABLE[nib & 7])))
                out[2 * k + j] = p
        self.predictor, self.step_index = p, si
        return out


class Upsampler:
    """Continuous linear up-sampling (16 kHz → 48 kHz) across frame borders."""

    def __init__(self, ratio: int = 3) -> None:
        self.ratio = max(1, ratio)
        self.prev = 0.0

    def process(self, samples: np.ndarray) -> np.ndarray:
        n = len(samples)
        if n == 0:
            return b""
        r = self.ratio
        x = samples.astype(np.float32)
        a = np.empty(n, dtype=np.float32)
        a[0] = self.prev
        a[1:] = x[:-1]
        out = np.empty((n, r), dtype=np.float32)
        for t in range(1, r):
            out[:, t - 1] = a + (x - a) * (t / r)
        out[:, r - 1] = x
        self.prev = float(x[-1])
        return np.clip(np.rint(out.reshape(-1)), -32768, 32767).astype(np.int16)


async def find_known_device(mac: str | None) -> BLEDevice | None:
    """Resolve the paired remote through BlueZ, without scanning.

    A paired BLE HID remote is connected (or bonded) and does not advertise,
    so ``BleakScanner`` cannot find it.  The ObjectManager sees it either way;
    identify by the Xiaomi USB product id, the ATVV service UUID, or a name
    marker, never by a hard-coded MAC.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply = await bus.call(Message(
            destination="org.bluez", path="/",
            interface="org.freedesktop.DBus.ObjectManager",
            member="GetManagedObjects"))
        if reply.message_type == MessageType.ERROR or not reply.body:
            return None
        for path, interfaces in reply.body[0].items():
            raw = interfaces.get("org.bluez.Device1")
            if not raw:
                continue
            props = {k: (v.value if hasattr(v, "value") else v) for k, v in raw.items()}
            address = str(props.get("Address") or "")
            if not address:
                continue
            alias = str(props.get("Alias") or props.get("Name") or "")
            modalias = str(props.get("Modalias") or "").lower()
            uuids = {str(u).lower() for u in props.get("UUIDs") or []}
            if mac:
                match = address.lower() == mac.lower()
            else:
                match = ("v2717p32b8" in modalias or SERVICE in uuids
                         or "遥控" in alias or "mi rc" in alias.lower())
            if match:
                return BLEDevice(address, alias, {"path": path, "props": props})
        return None
    finally:
        try:
            bus.disconnect()
        except Exception:                               # noqa: BLE001
            pass


async def restore_hid(path: str | None) -> None:
    """Bring the BlueZ link back after bleak disconnects it.

    bleak's BlueZ backend calls Device1.Disconnect on the shared LE link on
    exit; that also takes the HID input node away.  Reconnecting here keeps
    the remote's keys (PTT, tuning) alive for the next press.
    """
    if not path:
        return
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            reply = await asyncio.wait_for(bus.call(Message(
                destination="org.bluez", path=path,
                interface="org.bluez.Device1", member="Connect")), timeout=8.0)
            if reply.message_type != MessageType.ERROR:
                log.info("restored BlueZ/HID link")
        finally:
            bus.disconnect()
    except Exception as exc:                            # noqa: BLE001
        log.debug("HID restore skipped: %s", exc)


class RemoteMic:
    def __init__(self) -> None:
        self.stop = False
        self.client: BleakClient | None = None
        self.tx_char = None
        self.device_path: str | None = None
        self.version = 0x0100
        self.sample_rate = 16000
        self.frame_size = 120
        self.caps_ready = False
        self.session_active = False
        self.mic_open_sent = False
        self.session_id = 0
        self.skip = 0
        self.pending = bytearray()
        self.decoder = Decoder()
        self.upsampler = Upsampler()
        self.sink: asyncio.StreamWriter | None = None
        self.disconnected = asyncio.Event()
        self.frames = 0

    # -- PCM sink -----------------------------------------------------
    async def _open_sink(self) -> None:
        if self.sink is not None:
            return
        try:
            _reader, writer = await asyncio.open_unix_connection(MIC_SOCKET)
            self.sink = writer
            log.info("PCM socket connected: %s", MIC_SOCKET)
        except OSError as exc:
            log.debug("PCM socket unavailable: %s", exc)

    async def _close_sink(self) -> None:
        writer, self.sink = self.sink, None
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:                           # noqa: BLE001
                pass

    # -- ATVV ---------------------------------------------------------
    def _handle_control(self, _sender, data: bytearray) -> None:
        raw = bytes(data)
        if not raw:
            return
        op = raw[0]
        log.debug("control op=0x%02X %s", op, raw.hex())
        if op == OP_CAPS:
            self._parse_caps(raw)
        elif op == OP_MIC_REQUEST:
            if not self.mic_open_sent:
                self.mic_open_sent = True
                asyncio.create_task(self._send_mic_open())
        elif op == OP_AUDIO_START:
            if len(raw) > 3:
                self.session_id = raw[3]
            if not self.mic_open_sent:
                # Firmware can start a session with 0x04 and no 0x08 press.
                self.mic_open_sent = True
                asyncio.create_task(self._send_mic_open())
            if not self.session_active:
                self.session_active = True
                self.decoder.reset(0, 0)
                self.pending.clear()
                self.skip = SKIP_FRAMES
                self.frames = 0
                asyncio.create_task(self._open_sink())
                log.info("voice session started (session=0x%02X)", self.session_id)
        elif op == OP_SYNC and len(raw) >= 7:
            predictor, step = struct.unpack(">h", raw[4:6])[0], raw[6]
            self.decoder.reset(predictor, step)
        elif op == OP_AUDIO_STOP:
            if self.session_active:
                self.session_active = False
                self.mic_open_sent = False
                log.info("voice session ended (%d frames)", self.frames)
                asyncio.create_task(self._end_session())

    def _parse_caps(self, raw: bytes) -> None:
        if len(raw) < 7:
            return
        version = (raw[1] << 8) | raw[2]
        codecs = raw[3]
        if not codecs & CODEC_16KHZ and len(raw) > 4 and raw[4] & CODEC_16KHZ:
            codecs = raw[4]
        self.version = version
        self.sample_rate = 16000 if codecs & CODEC_16KHZ else 8000
        self.frame_size = ((raw[5] << 8) | raw[6]) or 120
        self.upsampler = Upsampler(48000 // self.sample_rate)
        self.caps_ready = True
        log.info("ATVV caps: version=0x%04X codec=0x%02X frame=%d rate=%d",
                 version, codecs, self.frame_size, self.sample_rate)

    async def _send_mic_open(self) -> None:
        payload = bytes([0x0C, 0x00]) if self.version >= 0x0100 else bytes([0x0C, 0x00, 0x02])
        await self._write(payload, "MIC_OPEN")

    async def _end_session(self) -> None:
        payload = bytes([0x0D])
        if self.version >= 0x0100:
            payload += bytes([self.session_id & 0xFF])
        await self._write(payload, "MIC_CLOSE")
        await self._close_sink()

    async def _write(self, payload: bytes, label: str) -> None:
        if not self.client or not self.tx_char:
            return
        try:
            await self.client.write_gatt_char(self.tx_char, payload, response=False)
            log.debug("%s sent: %s", label, payload.hex())
        except Exception as exc:                        # noqa: BLE001
            log.warning("%s failed: %s", label, exc)

    def _handle_audio(self, _sender, data: bytearray) -> None:
        if not self.session_active or not self.caps_ready or not data:
            return
        self.pending.extend(data)
        size = self.frame_size
        while len(self.pending) >= size:
            frame = bytes(self.pending[:size])
            del self.pending[:size]
            self.frames += 1
            if self.skip > 0:
                self.skip -= 1
                continue
            pcm = self.decoder.decode(frame)
            self._emit(pcm)

    def _emit(self, pcm: np.ndarray) -> None:
        writer = self.sink
        if writer is None or not pcm.size:
            return
        boosted = pcm.astype(np.float32) * GAIN
        up = self.upsampler.process(np.clip(boosted, -32768, 32767).astype(np.int16))
        try:
            writer.write(up.tobytes())
        except Exception as exc:                        # noqa: BLE001
            log.warning("PCM write failed: %s", exc)
            self.sink = None

    # -- connection ---------------------------------------------------
    async def _connect(self, device: BLEDevice) -> None:
        self.device_path = device.details.get("path") if isinstance(device.details, dict) else None
        self.disconnected.clear()
        self.caps_ready = False
        self.session_active = False
        self.mic_open_sent = False
        client = BleakClient(device, timeout=15.0,
                             disconnected_callback=lambda _c: self.disconnected.set())
        self.client = client
        await client.connect()
        characters = {}
        for service in client.services:
            if service.uuid.lower() == SERVICE:
                characters = {c.uuid.lower(): c for c in service.characteristics}
                break
        tx = characters.get(TX_CHAR)
        audio = characters.get(AUDIO_CHAR)
        control = characters.get(CTRL_CHAR)
        if tx is None or audio is None or control is None:
            raise RuntimeError("ATVV characteristics missing")
        self.tx_char = tx
        await client.start_notify(audio, self._handle_audio)
        await client.start_notify(control, self._handle_control)
        await self._write(GET_CAPS, "GET_CAPS")
        log.info("connected to %s (%s)", device.address, device.name or "?")

    async def run(self) -> None:
        while not self.stop:
            device = await find_known_device(REMOTE_MAC)
            if device is None:
                log.info("remote not found; waiting for it (press a key)")
                await asyncio.sleep(5)
                continue
            try:
                await self._connect(device)
                await self.disconnected.wait()
                log.info("remote disconnected")
            except Exception as exc:                    # noqa: BLE001
                log.warning("connection failed: %s", exc)
            finally:
                self.session_active = False
                self.mic_open_sent = False
                await self._close_sink()
                client, self.client = self.client, None
                self.tx_char = None
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:                   # noqa: BLE001
                        pass
            await restore_hid(self.device_path)
            if not self.stop:
                await asyncio.sleep(2)

    def request_stop(self) -> None:
        self.stop = True
        self.disconnected.set()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("MRRC_REMOTE_MIC_LOGLEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mic = RemoteMic()

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, mic.request_stop)
            except NotImplementedError:
                pass
        await mic.run()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
