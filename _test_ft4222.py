"""Test: call exact same code as scope_handler._setup_device but inline"""
import sys, ctypes
sys.path.insert(0, '.')
from ctypes import c_void_p, c_uint32, c_uint8, POINTER, byref, CDLL, create_string_buffer

from backends.ft710.scope_libraries import require_ftdi_libraries
from backends.ft710.scope_libraries import get_ft4222_clock_divider

ft4222_path, ftd2xx_path = require_ftdi_libraries()

_lib_ft4222 = CDLL(str(ft4222_path))
_lib_ftd2xx = CDLL(str(ftd2xx_path))

# Setup signatures exactly like _load_library
d2xx = _lib_ftd2xx
d2xx.FT_OpenEx.argtypes = [c_void_p, c_uint32, POINTER(c_void_p)]; d2xx.FT_OpenEx.restype = c_uint32
d2xx.FT_Close.argtypes = [c_void_p]; d2xx.FT_Close.restype = c_uint32
d2xx.FT_SetTimeouts.argtypes = [c_void_p, c_uint32, c_uint32]; d2xx.FT_SetTimeouts.restype = c_uint32
d2xx.FT_SetLatencyTimer.argtypes = [c_void_p, c_uint8]; d2xx.FT_SetLatencyTimer.restype = c_uint32

f4 = _lib_ft4222
f4.FT4222_UnInitialize.argtypes = [c_void_p]; f4.FT4222_UnInitialize.restype = c_uint32
f4.FT4222_SPIMaster_Init.argtypes = [c_void_p, c_uint32, c_uint32, c_uint32, c_uint32, c_uint8]
f4.FT4222_SPIMaster_Init.restype = c_uint32
f4.FT4222_SetClock.argtypes = [c_void_p, c_uint32]; f4.FT4222_SetClock.restype = c_uint32

FT_OK = 0; FT4222_OK = 0; FT_OPEN_BY_DESCRIPTION = 2
SPI_IO_SINGLE = 1; CLK_IDLE_HIGH = 1; CLK_LEADING = 0

# Now run exactly the same code as _setup_device
ft_handle = c_void_p()
desc = create_string_buffer(b"FT4222 A")
s = d2xx.FT_OpenEx(desc, FT_OPEN_BY_DESCRIPTION, byref(ft_handle))

# Add the SAME log line
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test")

if s != FT_OK:
    logger.warning("FT_OpenEx failed (error %d)", s)
else:
    logger.info("FT4222 opened, handle=%#x", ft_handle.value)

if d2xx.FT_SetTimeouts(ft_handle, 100, 100) != FT_OK:
    logger.warning("FT_SetTimeouts failed")
if d2xx.FT_SetLatencyTimer(ft_handle, 2) != FT_OK:
    logger.warning("FT_SetLatencyTimer failed")

# THE KEY LINE - same as _setup_device
s_spi = f4.FT4222_SPIMaster_Init(
    ft_handle, SPI_IO_SINGLE, get_ft4222_clock_divider(), CLK_IDLE_HIGH, CLK_LEADING, 0x01,
)
print(f'SPI_Init result from replicated _setup_device: {s_spi}')

if s_spi != FT4222_OK:
    logger.warning("FT4222_SPIMaster_Init failed (error %d)", s_spi)

f4.FT4222_UnInitialize(ft_handle)
d2xx.FT_Close(ft_handle)
