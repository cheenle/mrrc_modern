"""Spawn-only DSP Worker loop using bounded JSON and shared memory."""

from __future__ import annotations

import os
import mmap
from pathlib import Path
import sys
import time
from typing import NoReturn

from ft8.core.protocol import MAX_CONTROL_FRAME, decode_frame, encode_frame


class _RequestError(Exception):
    """Expected request failure safe to report without exception disclosure."""

    __slots__ = ("code", "detail")

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def default_library_path() -> Path:
    """Return the DSP library path for this platform / mode.

    Frozen installers bundle the shared library (and, on Windows/macOS, the
    runtime DLLs/dylibs it links against) at the PyInstaller ``_internal``
    root — ``app_root()`` — with the platform's own naming: ``wsjt_core.dll``
    on Windows, ``libwsjt_core.dylib`` on macOS.  Source builds keep the
    conventional ``dsp/build`` CMake output.

    The frozen name must follow the platform rather than a single literal:
    the macOS bundle is signed and the library is loaded through ctypes from
    ``Contents/Resources``, so a hard-coded ``.dll`` there is a path that can
    never exist (and did, until the FT8 packaging work made macOS a target).
    """
    if getattr(sys, "frozen", False):
        from ft8.core.paths import app_root
        return app_root() / _library_name()
    base = Path(__file__).resolve().parents[2] / "ft8" / "dsp" / "build"
    return base / _library_name()


def _library_name() -> str:
    if sys.platform == "darwin":
        return "libwsjt_core.dylib"
    if sys.platform == "win32":
        return "wsjt_core.dll"
    return "libwsjt_core.so"


def _expected_shm_size(logical_nbytes: int) -> int:
    """Return the observable segment size for an exact logical allocation.

    Darwin ``mmap`` and Windows ``CreateFileMapping`` both round the allocation
    up to the system page size, and ``SharedMemory.size`` reports that rounded
    size; other platforms report the exact logical size.
    """

    if sys.platform in ("darwin", "win32"):
        page_size = mmap.PAGESIZE
        return ((logical_nbytes + page_size - 1) // page_size) * page_size
    return logical_nbytes


def worker_main(
    connection: object,
    generation: int,
    library_path: str | os.PathLike[str] | None,
) -> None:
    os.environ.setdefault("OMP_STACKSIZE", "10M")
    import numpy as np
    from ft8.core.binding import CoreBinding, DspStatusError
    from ft8.core.models import DecodeConfig, DecodePath
    from multiprocessing.shared_memory import SharedMemory

    binding = CoreBinding(default_library_path() if library_path is None else library_path)

    def response_base(frame: dict[str, object], frame_type: str) -> dict[str, object]:
        return {
            "v": 1,
            "type": frame_type,
            "generation": frame["generation"],
            "request_id": frame["request_id"],
        }

    def send_error(frame: dict[str, object], code: str, detail: str) -> None:
        connection.send_bytes(  # type: ignore[attr-defined]
            encode_frame({**response_base(frame, "error"), "code": code, "detail": detail})
        )

    def build_config(frame: dict[str, object]) -> DecodeConfig:
        value = frame["config"]
        assert isinstance(value, dict)
        return DecodeConfig(
            path=DecodePath(value["path"]),
            sample_rate=value["sample_rate"],
            sample_count=value["sample_count"],
            profile=value["profile"],
            threads=value["threads"],
            cycles=value["cycles"],
            sensitivity=value["sensitivity"],
            ap=value["ap"],
            low_threshold=value["low_threshold"],
            wide_dx=value["wide_dx"],
            hide_duplicates=value["hide_duplicates"],
            qso_progress=value["qso_progress"],
            rx_frequency=value["rx_frequency"],
            tx_frequency=value["tx_frequency"],
            low_frequency=value["low_frequency"],
            high_frequency=value["high_frequency"],
            ap_width=value["ap_width"],
            utc_hhmmss=value["utc_hhmmss"],
            my_call=value["my_call"],
            dx_call=value["dx_call"],
            dx_grid=value["dx_grid"],
        )

    def handle_decode(frame: dict[str, object]) -> dict[str, object]:
        descriptor = frame["shm"]
        assert isinstance(descriptor, dict)
        shm = SharedMemory(name=descriptor["name"], create=False)
        samples = None
        try:
            if shm.size != _expected_shm_size(descriptor["nbytes"]):
                raise _RequestError(
                    "shared_memory_size", "shared memory size does not match descriptor"
                )
            samples = np.ndarray((180_000,), dtype="<i2", buffer=shm.buf)
            samples.setflags(write=False)
            # CPU time is measured *here*, in the Worker process, not in the
            # parent and not in ``CoreBinding``: the Worker does nothing but
            # DSP, so its process CPU is attributable to this one decode, while
            # ``time.process_time()`` in the parent would fold in the audio and
            # spectrum threads.  ``RUSAGE_CHILDREN`` is no substitute either —
            # it does not update for a child that is still alive.
            cpu_started = time.process_time()
            batch = binding.decode(samples, build_config(frame), frame["slot_id"])
            cpu_seconds = time.process_time() - cpu_started
            return {
                **response_base(frame, "decode_ok"),
                "slot_id": batch.slot_id,
                "path": batch.path.value,
                "results": [
                    {
                        "slot_id": result.slot_id,
                        "sync": result.sync,
                        "snr": result.snr,
                        "dt": result.dt,
                        "frequency": result.frequency,
                        "text": result.text,
                        "ap_type": result.ap_type,
                        "quality": result.quality,
                        "flags": result.flags,
                    }
                    for result in batch.results
                ],
                "overflow": batch.overflow,
                "elapsed_seconds": batch.elapsed_seconds,
                "cpu_seconds": cpu_seconds,
                "deadline_missed": time.monotonic() > frame["deadline_monotonic"],
            }
        finally:
            if samples is not None:
                del samples
            shm.close()

    def handle_encode(frame: dict[str, object]) -> dict[str, object]:
        descriptor = frame["shm"]
        assert isinstance(descriptor, dict)
        shm = SharedMemory(name=descriptor["name"], create=False)
        output = None
        try:
            if shm.size != _expected_shm_size(descriptor["nbytes"]):
                raise _RequestError(
                    "shared_memory_size", "shared memory size does not match descriptor"
                )
            output = np.ndarray((606_720,), dtype="<f4", buffer=shm.buf)
            encoded = binding.encode(
                frame["message"], frame["frequency"], frame["sample_rate"], output
            )
            return {
                **response_base(frame, "encode_ok"),
                "message": encoded.message,
                "sample_rate": encoded.sample_rate,
                "sample_count": encoded.sample_count,
            }
        finally:
            if output is not None:
                del output
            shm.close()

    while True:
        raw = connection.recv_bytes(MAX_CONTROL_FRAME + 1)  # type: ignore[attr-defined]
        frame = decode_frame(raw)
        if frame["generation"] != generation:
            send_error(frame, "stale_generation", "request generation is not current")
            continue

        frame_type = frame["type"]
        if frame_type == "ping":
            connection.send_bytes(encode_frame(response_base(frame, "pong")))  # type: ignore[attr-defined]
            continue
        if frame_type == "shutdown":
            connection.send_bytes(encode_frame(response_base(frame, "stopped")))  # type: ignore[attr-defined]
            return

        try:
            if frame_type == "decode":
                response = handle_decode(frame)
            elif frame_type == "encode":
                response = handle_encode(frame)
            else:
                raise _RequestError(
                    "unsupported_type", "frame type is not a Worker request"
                )
        except _RequestError as error:
            send_error(frame, error.code, error.detail)
        except OSError:
            send_error(frame, "shared_memory_unavailable", "shared memory is unavailable")
        except ValueError:
            send_error(frame, "invalid_request", "DSP request validation failed")
        except DspStatusError:
            send_error(frame, "dsp_error", "DSP operation failed")
        else:
            connection.send_bytes(encode_frame(response))  # type: ignore[attr-defined]
