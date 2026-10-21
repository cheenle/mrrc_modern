"""Spawn-only DSP Worker loop using bounded JSON and shared memory."""

from __future__ import annotations

import os
import mmap
from pathlib import Path
import sys
import threading
import time
from typing import Callable, NoReturn

from ft8.core.protocol import MAX_CONTROL_FRAME, decode_frame, encode_frame

#: Bounded file the Worker's own stdout/stderr is duplicated onto.
#:
#: The Fortran DSP aborts its **host process** on several conditions the Python
#: side cannot catch (``error stop`` / ``stop`` in wsjt_improved.f90, four2avar
#: and packjt).  On Windows the Worker inherits the server's std handles, and the
#: launcher's startup tee stops writing to a file once the server answers HTTP
#: (``launcher_log.StartupTee.stop``), so the single line that says *why* the
#: Worker died reached no log at all — the field bundle of 2026-10-16 carried
#: only ``worker_exit: worker process exited with status None`` per slot.  This
#: file is where that line now lands; ``support_bundle`` collects it.
WORKER_LOG_NAME = "ft8-worker.log"
WORKER_LOG_MAX_BYTES = 2 * 1024 * 1024

#: Stack the request loop runs on.  The Fortran decoder puts its work arrays on
#: the *calling thread's* stack: `sync8var` alone declares
#: `real s(NH1,NHSYM)` = 1920 × 372 real*4 ≈ **2.7 MB**, plus
#: `sync2d`/`syncq` over the ±62 dt window (another ~1.2 MB) — ≈ 4 MB in one
#: frame (`ft8/dsp/vendor/ft8/ft8_params.f90:9-11`,
#: `patched/ft8_decodevar.f90:199-251`).
#:
#: Windows gives a thread the exe's PE stack reserve (**2 MB**), and a stack
#: overflow there is an immediate process death with no traceback — which is
#: exactly the 2026-10-21 field shape: one `worker_exit: … status None` per
#: deep decode, Windows only, while macOS (8 MB main-thread stack) passes the
#: same suite locally.  `OMP_STACKSIZE` does **not** cover this: it sizes
#: OpenMP *worker* threads, and the decoder is entered from this loop.
#: 64 MB is reserved address space, committed lazily — it costs nothing on the
#: small boxes.
WORKER_STACK_BYTES = 64 * 1024 * 1024


def _run_with_large_stack(target: "Callable[[], None]") -> None:
    """Run ``target`` on a thread whose stack is ``WORKER_STACK_BYTES``.

    ``threading.stack_size`` is process-global and only affects threads created
    afterwards, so it is set right before ``start()`` (which is where the OS
    thread — and therefore the stack — is created) and restored immediately
    after, leaving every other thread in this process with the default.
    """
    failure: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            target()
        except BaseException as exc:          # noqa: BLE001 - re-raised in the caller
            failure["error"] = exc

    previous = threading.stack_size(WORKER_STACK_BYTES)
    try:
        thread = threading.Thread(target=runner, name="ft8-dsp-request-loop")
        thread.start()
    finally:
        threading.stack_size(previous)
    thread.join()
    error = failure.get("error")
    if error is not None:
        raise error


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


#: The Fortran callsign database.  `prog_args.f90:4` is literally
#: `share_dir = '.'`, and `cwfilter` opens `trim(share_dir)//'/ALLCALL7.TXT'` on
#: the decoder's first call — i.e. **relative to the working directory**, with no
#: setter.  From a source checkout the cwd *is* the repo root, which is why this
#: never showed up locally; in a frozen app the cwd is wherever the launcher was
#: started, so the callsign DB came up empty and the deep decoder downgraded to
#: `ldbvalid = .false.` (`ALLCALL7.TXT is too short or broken?` in the 2026-10-21
#: bundle — one line, then a whole night of weaker deep decodes).
ALLCALL_FILE = "ALLCALL7.TXT"


def share_dir() -> Path:
    """The directory the decoder must treat as ``share_dir``.

    ``app_root()`` when it holds the file (installers copy it next to the DSP
    library, under PyInstaller's ``_internal``), otherwise the current cwd so a
    source run behaves exactly as before.
    """
    from ft8.core.paths import app_root
    root = app_root()
    if (root / ALLCALL_FILE).is_file():
        return root
    return Path.cwd()


def _worker_log_path() -> Path | None:
    """Where to duplicate the Worker's stdout/stderr, or None to leave it alone.

    ``MRRC_LOG_DIR`` is what the desktop launchers and systemd set, and it is
    what ``server.py`` builds ``LOG_DIR`` from — resolved from the environment
    here on purpose: ``ft8/core`` must not import the application modules, and
    a missing/read-only directory is not worth failing a decode over.
    """
    base = os.environ.get("MRRC_LOG_DIR")
    if not base:
        return None
    try:
        path = Path(base) / WORKER_LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path


def _rotate_worker_log(path: Path, max_bytes: int = WORKER_LOG_MAX_BYTES) -> None:
    """Keep one previous generation once the file grows past `max_bytes`."""
    try:
        if path.stat().st_size <= max_bytes:
            return
    except OSError:
        return
    try:
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def _redirect_worker_output() -> None:
    """Send the Worker's own fd 1/fd 2 to a bounded file (best effort).

    Only fd numbers matter to the native code: the Fortran runtime writes
    through fd 2, so replacing ``sys.stderr`` would capture nothing.  Failure
    is silently ignored — losing the log must never lose a decode.
    """
    path = _worker_log_path()
    if path is None:
        return
    _rotate_worker_log(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return
    try:
        for target in (1, 2):
            try:
                os.dup2(fd, target)
            except OSError:
                pass
    finally:
        if fd not in (1, 2):
            try:
                os.close(fd)
            except OSError:
                pass


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
    _redirect_worker_output()
    # The Fortran resolves ALLCALL7.TXT relative to the cwd and offers no setter,
    # so this process has to *be* in the share directory before the first decode.
    # It does no other relative-path I/O (library path and worker log are
    # absolute), so changing it here is safe.
    try:
        os.chdir(share_dir())
    except OSError:
        pass                     # keep the inherited cwd; decoding still works
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

    def serve() -> None:
        """The request loop — see WORKER_STACK_BYTES for why it is on its own thread."""
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
            except DspStatusError as error:
                # Sanitized, but not silent: the status is what says *which* native
                # check failed (wsjt_improved.f90 now returns one instead of
                # aborting the Worker), and it is the only reason the operator sees
                # a DecodeError here rather than a vanished process.
                send_error(frame, "dsp_error",
                           f"DSP operation failed (status {error.status})")
            else:
                connection.send_bytes(encode_frame(response))  # type: ignore[attr-defined]

    _run_with_large_stack(serve)
