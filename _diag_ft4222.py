"""Diagnose FT4222 SPI data — check raw bytes without inversion"""
import ctypes, time
from ctypes import c_void_p, c_uint32, c_uint16, c_uint8, c_bool, POINTER, byref, CDLL, create_string_buffer

from backends.ft710.scope_libraries import require_ftdi_libraries

ft4222_path, ftd2xx_path = require_ftdi_libraries()
d2xx = CDLL(str(ftd2xx_path))
f4 = CDLL(str(ft4222_path))

d2xx.FT_OpenEx.argtypes = [c_void_p, c_uint32, POINTER(c_void_p)]; d2xx.FT_OpenEx.restype = c_uint32
d2xx.FT_Close.argtypes = [c_void_p]; d2xx.FT_Close.restype = c_uint32
d2xx.FT_SetTimeouts.argtypes = [c_void_p, c_uint32, c_uint32]; d2xx.FT_SetTimeouts.restype = c_uint32
d2xx.FT_SetLatencyTimer.argtypes = [c_void_p, c_uint8]; d2xx.FT_SetLatencyTimer.restype = c_uint32
f4.FT4222_UnInitialize.argtypes = [c_void_p]; f4.FT4222_UnInitialize.restype = c_uint32
f4.FT4222_SPIMaster_Init.argtypes = [c_void_p, c_uint32, c_uint32, c_uint32, c_uint32, c_uint8]
f4.FT4222_SPIMaster_Init.restype = c_uint32
f4.FT4222_SPIMaster_SingleRead.argtypes = [c_void_p, POINTER(c_uint8), c_uint16, POINTER(c_uint16), c_bool]
f4.FT4222_SPIMaster_SingleRead.restype = c_uint32
f4.FT4222_SetClock.argtypes = [c_void_p, c_uint32]; f4.FT4222_SetClock.restype = c_uint32

ft_handle = c_void_p()
desc = create_string_buffer(b"FT4222 A")
assert d2xx.FT_OpenEx(desc, 2, byref(ft_handle)) == 0
d2xx.FT_SetTimeouts(ft_handle, 100, 100)
d2xx.FT_SetLatencyTimer(ft_handle, 2)
assert f4.FT4222_SPIMaster_Init(ft_handle, 1, 7, 1, 0, 1) == 0
f4.FT4222_SetClock(ft_handle, 1)
print("FT4222 SPI ready, reading raw frames...")

buf = (c_uint8 * 4096)()
sz = c_uint16()
sync_tail = b'\xff\x01\xee\x01'
frames = 0

while frames < 5:
    status = f4.FT4222_SPIMaster_SingleRead(ft_handle, buf, 4096, byref(sz), False)
    if status == 10:  # TRANSFER_IN_PROGRESS
        continue
    if status != 0:
        time.sleep(0.01)
        continue

    frame = bytes(buf[:4096])
    if not frame.endswith(sync_tail):
        # Re-sync
        one = (c_uint8 * 1)(); osz = c_uint16()
        for _ in range(2000):
            f4.FT4222_SPIMaster_SingleRead(ft_handle, one, 1, byref(osz), False)
            if one[0] == 0xFF:
                # Check if next bytes form sync
                f4.FT4222_SPIMaster_SingleRead(ft_handle, one, 1, byref(osz), False)
                if one[0] == 0x01:
                    f4.FT4222_SPIMaster_SingleRead(ft_handle, one, 1, byref(osz), False)
                    if one[0] == 0xEE:
                        f4.FT4222_SPIMaster_SingleRead(ft_handle, one, 1, byref(osz), False)
                        if one[0] == 0x01:
                            print("  Re-synced!")
                            break
        continue

    frames += 1
    # Check raw wf1 data (first 850 bytes, NOT inverted)
    wf1_raw = frame[0:850]
    n_ff = sum(1 for b in wf1_raw if b == 0xFF)
    n_00 = sum(1 for b in wf1_raw if b == 0x00)
    n_other = 850 - n_ff - n_00

    # Check inverted data
    wf1_inv = bytes(~b & 0xFF for b in wf1_raw)
    n_inv_nonzero = sum(1 for b in wf1_inv if b > 0)

    print(f"Frame {frames}: 0xFF={n_ff}, 0x00={n_00}, other={n_other}, inv_nonzero={n_inv_nonzero}")
    print(f"  Raw wf1[0:20]:   {wf1_raw[:20].hex()}")
    print(f"  Inv wf1[0:20]:   {wf1_inv[:20].hex()}")
    print(f"  Frame tail:       {frame[-16:].hex()}")
    print(f"  Data[110] S-meter: {frame[2900+110] if len(frame) > 3010 else 'N/A'}")
    print()

f4.FT4222_UnInitialize(ft_handle)
d2xx.FT_Close(ft_handle)
