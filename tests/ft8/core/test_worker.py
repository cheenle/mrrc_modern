from __future__ import annotations

import ast
import multiprocessing as mp
import os
import subprocess
import sys
from multiprocessing.connection import Connection
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from typing import Iterator

os.environ.setdefault("OMP_STACKSIZE", "10M")

import numpy as np
import pytest
from scipy.signal import resample_poly

from ft8.core.worker import default_library_path, worker_main
from ft8.core.protocol import MAX_CONTROL_FRAME, decode_frame, encode_frame


ROOT = Path(__file__).parents[3]


def config(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "path": "improved",
        "sample_rate": 12_000,
        "sample_count": 180_000,
        "profile": 3,
        "threads": 1,
        "cycles": 1,
        "sensitivity": 2,
        "ap": True,
        "low_threshold": False,
        "wide_dx": False,
        "hide_duplicates": True,
        "qso_progress": 0,
        "rx_frequency": 1500,
        "tx_frequency": 1500,
        "low_frequency": 200,
        "high_frequency": 3000,
        "ap_width": 50,
        "utc_hhmmss": 120000,
        "my_call": "N0CALL",
        "dx_call": "",
        "dx_grid": "",
    }
    value.update(changes)
    return value


def decode_descriptor(shm: SharedMemory) -> dict[str, object]:
    return {
        "name": shm.name,
        "dtype": "<i2",
        "shape": [180_000],
        "nbytes": 360_000,
    }


def encode_descriptor(shm: SharedMemory) -> dict[str, object]:
    return {
        "name": shm.name,
        "dtype": "<f4",
        "shape": [606_720],
        "nbytes": 2_426_880,
    }


def start_worker(
    library_path: Path, generation: int = 3
) -> tuple[Connection, mp.Process]:
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = context.Process(
        target=worker_main,
        name="dsp-worker-test",
        args=(child, generation, library_path),
    )
    process.start()
    child.close()
    return parent, process


def receive(connection: Connection, timeout: float = 30.0) -> dict[str, object]:
    assert connection.poll(timeout), "worker did not reply before the test timeout"
    return decode_frame(connection.recv_bytes(MAX_CONTROL_FRAME + 1))


def stop_worker(
    connection: Connection, process: mp.Process, generation: int = 3
) -> None:
    if process.is_alive():
        connection.send_bytes(
            encode_frame(
                {
                    "v": 1,
                    "type": "shutdown",
                    "generation": generation,
                    "request_id": 999,
                }
            )
        )
        assert receive(connection)["type"] == "stopped"
    process.join(10)
    connection.close()
    assert process.exitcode == 0


@pytest.fixture(scope="module")
def worker_library_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    build = tmp_path_factory.mktemp("ft8-worker-build")
    subprocess.run(
        [
            "cmake",
            "-S",
            str(ROOT / "ft8" / "dsp"),
            "-B",
            str(build),
            f"-DCMAKE_Fortran_COMPILER={'gfortran-mp-13' if sys.platform == 'darwin' else 'gfortran'}",
            "-DCMAKE_BUILD_TYPE=Release",
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(["cmake", "--build", str(build), "-j"], cwd=ROOT, check=True)
    suffix = ".dll" if os.name == "nt" else (".dylib" if sys.platform == "darwin" else ".so")
    path = build / f"libwsjt_core{suffix}"
    assert path.is_file()
    if os.name == "nt":
        # The spawned worker resolves deps via CoreBinding's add_dll_directory
        # (its DLL dir); copy the MinGW runtime DLLs next to the fresh build so
        # the child can load libwsjt_core.dll without them on the search path.
        import shutil

        compiler = shutil.which("gfortran")
        assert compiler, "gfortran must be on PATH to copy runtime DLLs"
        bin_dir = Path(compiler).resolve().parent
        for dll in (
            "libgfortran-5.dll",
            "libgomp-1.dll",
            "libquadmath-0.dll",
            "libwinpthread-1.dll",
            "libgcc_s_seh-1.dll",
            "libstdc++-6.dll",
            "libfftw3f-3.dll",
            "libfftw3f_threads-3.dll",
        ):
            shutil.copy(bin_dir / dll, build)
    return path


@pytest.fixture()
def running_worker(
    worker_library_path: Path,
) -> Iterator[tuple[Connection, mp.Process]]:
    connection, process = start_worker(worker_library_path)
    try:
        yield connection, process
    finally:
        stop_worker(connection, process)


def test_worker_module_defers_numpy_and_binding_until_after_stack_default() -> None:
    path = ROOT / "ft8/core/worker.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    module_imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert not any(
        (isinstance(node, ast.Import) and any(alias.name == "numpy" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "ft8.core.binding")
        for node in module_imports
    )

    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "worker_main"
    )
    stack_line = source.index('os.environ.setdefault("OMP_STACKSIZE", "10M")')
    numpy_line = source.index("import numpy", stack_line)
    binding_line = source.index("from ft8.core.binding import", stack_line)
    assert function.body[0].lineno == source[:stack_line].count("\n") + 1
    assert stack_line < numpy_line < binding_line


def test_worker_output_log_path_prefers_mrrc_log_dir(monkeypatch, tmp_path) -> None:
    import ft8.core.worker as worker

    monkeypatch.setenv("MRRC_LOG_DIR", str(tmp_path))
    assert worker._worker_log_path() == tmp_path / worker.WORKER_LOG_NAME

    monkeypatch.delenv("MRRC_LOG_DIR", raising=False)
    assert worker._worker_log_path() is None


def test_worker_output_log_rotation_keeps_one_generation(tmp_path) -> None:
    import ft8.core.worker as worker

    path = tmp_path / worker.WORKER_LOG_NAME
    path.write_bytes(b"x" * 64)
    worker._rotate_worker_log(path, max_bytes=32)
    assert (tmp_path / (worker.WORKER_LOG_NAME + ".1")).read_bytes() == b"x" * 64
    assert not path.exists()

    path.write_bytes(b"y" * 8)
    worker._rotate_worker_log(path, max_bytes=32)
    assert path.read_bytes() == b"y" * 8


def test_worker_output_redirect_captures_what_fd2_receives(tmp_path) -> None:
    """A real child process, because the redirect is a process-global dup2.

    The Fortran decoder kills its host through fd 2 on a platform whose launcher
    tee has already stopped writing to a file (field report 2026-10-16).  This
    is the only test that proves the message would now be on disk instead of
    only on a drained pipe.
    """
    code = (
        "import sys\n"
        "from ft8.core.worker import _redirect_worker_output\n"
        "_redirect_worker_output()\n"
        "sys.stderr.write('wsjt_improved: FFTW thread initialization failed\\n')\n"
        "sys.stderr.flush()\n"
    )
    env = dict(os.environ, MRRC_LOG_DIR=str(tmp_path))
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    log = (tmp_path / "ft8-worker.log").read_text(encoding="utf-8")
    assert "FFTW thread initialization failed" in log
    # The message must leave the inherited pipe, not merely be copied off it.
    assert "FFTW thread initialization failed" not in proc.stderr


def test_default_library_path_is_project_build_for_platform() -> None:
    expected = ROOT / "ft8" / "dsp" / "build"
    if sys.platform == "darwin":
        expected = expected / "libwsjt_core.dylib"
    elif os.name == "nt":
        expected = expected / "wsjt_core.dll"  # MinGW builds prefix-free for packaging
    else:
        expected = expected / "libwsjt_core.so"
    assert default_library_path() == expected


def test_default_library_path_win32_source(monkeypatch) -> None:
    monkeypatch.setattr("ft8.core.worker.sys.platform", "win32")
    from ft8.core.worker import default_library_path
    assert default_library_path().name == "wsjt_core.dll"


def test_default_library_path_is_macos_dylib_when_frozen(monkeypatch) -> None:
    """The frozen name follows the platform, not a single literal.

    The macOS bundle is signed and the library is loaded through ctypes from
    Contents/Resources; a hard-coded ``wsjt_core.dll`` there is a path that can
    never exist.
    """
    monkeypatch.setattr("ft8.core.worker.sys.platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    import ft8.core.paths as paths
    monkeypatch.setattr(paths.sys, "_MEIPASS", "/virtual/_internal", raising=False)
    from ft8.core.worker import default_library_path
    assert str(default_library_path()) == str(
        Path("/virtual/_internal") / "libwsjt_core.dylib")


def test_default_library_path_is_so_on_linux(monkeypatch) -> None:
    monkeypatch.setattr("ft8.core.worker.sys.platform", "linux")
    from ft8.core.worker import default_library_path
    assert default_library_path().name == "libwsjt_core.so"


def test_default_library_path_frozen_uses_app_root(monkeypatch) -> None:
    monkeypatch.setattr("ft8.core.worker.sys.platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    import ft8.core.paths as paths
    monkeypatch.setattr(paths.sys, "_MEIPASS", "/virtual/_internal", raising=False)
    from ft8.core.worker import default_library_path
    assert str(default_library_path()) == str(Path("/virtual/_internal") / "wsjt_core.dll")


def test_spawned_worker_sends_no_ready_and_matches_ping_and_shutdown(
    worker_library_path: Path,
) -> None:
    connection, process = start_worker(worker_library_path)
    try:
        assert not connection.poll(0.25)
        connection.send_bytes(
            encode_frame(
                {"v": 1, "type": "ping", "generation": 3, "request_id": 41}
            )
        )
        assert receive(connection) == {
            "v": 1,
            "type": "pong",
            "generation": 3,
            "request_id": 41,
        }
        connection.send_bytes(
            encode_frame(
                {"v": 1, "type": "shutdown", "generation": 3, "request_id": 42}
            )
        )
        assert receive(connection) == {
            "v": 1,
            "type": "stopped",
            "generation": 3,
            "request_id": 42,
        }
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        connection.close()


def test_worker_encode_writes_parent_owned_tx_segment_and_returns_only_metadata(
    running_worker: tuple[Connection, mp.Process],
) -> None:
    connection, _process = running_worker
    shm = SharedMemory(create=True, size=2_426_880)
    try:
        waveform = np.ndarray((606_720,), dtype="<f4", buffer=shm.buf)
        waveform.fill(0.0)
        request = {
            "v": 1,
            "type": "encode",
            "generation": 3,
            "request_id": 1,
            "message": "CQ K1ABC FN42",
            "frequency": 1500.0,
            "sample_rate": 48_000,
            "shm": encode_descriptor(shm),
        }
        raw = encode_frame(request)
        assert len(raw) < 2048
        assert b"606720" in raw and b"waveform" not in raw

        connection.send_bytes(raw)
        response = receive(connection)

        assert response == {
            "v": 1,
            "type": "encode_ok",
            "generation": 3,
            "request_id": 1,
            "message": "CQ K1ABC FN42",
            "sample_rate": 48_000,
            "sample_count": 606_720,
        }
        assert 0.90 <= float(np.max(np.abs(waveform))) <= 1.0
        assert np.count_nonzero(waveform) > 500_000
        del waveform
    finally:
        shm.close()
        shm.unlink()


def test_worker_decodes_known_cq_from_shared_memory_without_audio_in_frame(
    running_worker: tuple[Connection, mp.Process],
) -> None:
    connection, _process = running_worker
    tx_shm = SharedMemory(create=True, size=2_426_880)
    rx_shm = SharedMemory(create=True, size=360_000)
    try:
        wave48 = np.ndarray((606_720,), dtype="<f4", buffer=tx_shm.buf)
        connection.send_bytes(
            encode_frame(
                {
                    "v": 1,
                    "type": "encode",
                    "generation": 3,
                    "request_id": 1,
                    "message": "CQ K1ABC FN42",
                    "frequency": 1500.0,
                    "sample_rate": 48_000,
                    "shm": encode_descriptor(tx_shm),
                }
            )
        )
        assert receive(connection)["type"] == "encode_ok"
        wave12 = resample_poly(wave48, 1, 4).astype(np.float32)
        slot = np.zeros(180_000, dtype=np.float32)
        slot[6_000 : 6_000 + wave12.size] = wave12
        pcm = np.ndarray((180_000,), dtype="<i2", buffer=rx_shm.buf)
        pcm[:] = np.clip(np.rint(slot * 24_000.0), -32768, 32767).astype(np.int16)
        request = {
            "v": 1,
            "type": "decode",
            "generation": 3,
            "request_id": 2,
            "slot_id": 11_908_800,
            "deadline_monotonic": 999_999_999.0,
            "shm": decode_descriptor(rx_shm),
            "config": config(),
        }
        raw = encode_frame(request)
        assert len(raw) < 2048
        assert "samples" not in request and "audio" not in request
        assert b"samples" not in raw and b"audio" not in raw

        connection.send_bytes(raw)
        response = receive(connection)

        assert response["type"] == "decode_ok"
        assert response["generation"] == 3
        assert response["request_id"] == 2
        assert response["slot_id"] == 11_908_800
        assert response["path"] == "improved"
        assert response["deadline_missed"] is False
        assert any(
            item["text"] == "CQ K1ABC FN42" for item in response["results"]  # type: ignore[index]
        )
        del pcm, slot, wave12, wave48
    finally:
        rx_shm.close()
        rx_shm.unlink()
        tx_shm.close()
        tx_shm.unlink()


def test_worker_decodes_with_the_fast_path_over_the_wire(
    running_worker: tuple[Connection, mp.Process],
) -> None:
    """`path="fast"` 要能穿过协议校验、worker 的 build_config、binding，一路到库，
    并且返回帧里的 `path` 说的是 `"fast"` 而不是默认的 `"improved"`。

    只测 protocol 不够：那条链上任何一环把 path 丢回默认值，都不会被
    test_protocol.py 发现。
    """
    connection, _process = running_worker
    tx_shm = SharedMemory(create=True, size=2_426_880)
    rx_shm = SharedMemory(create=True, size=360_000)
    try:
        wave48 = np.ndarray((606_720,), dtype="<f4", buffer=tx_shm.buf)
        connection.send_bytes(
            encode_frame(
                {
                    "v": 1,
                    "type": "encode",
                    "generation": 3,
                    "request_id": 1,
                    "message": "CQ K1ABC FN42",
                    "frequency": 1500.0,
                    "sample_rate": 48_000,
                    "shm": encode_descriptor(tx_shm),
                }
            )
        )
        assert receive(connection)["type"] == "encode_ok"
        wave12 = resample_poly(wave48, 1, 4).astype(np.float32)
        slot = np.zeros(180_000, dtype=np.float32)
        slot[6_000 : 6_000 + wave12.size] = wave12
        pcm = np.ndarray((180_000,), dtype="<i2", buffer=rx_shm.buf)
        pcm[:] = np.clip(np.rint(slot * 24_000.0), -32768, 32767).astype(np.int16)

        connection.send_bytes(
            encode_frame(
                {
                    "v": 1,
                    "type": "decode",
                    "generation": 3,
                    "request_id": 2,
                    "slot_id": 11_908_800,
                    "deadline_monotonic": 999_999_999.0,
                    "shm": decode_descriptor(rx_shm),
                    "config": config(path="fast"),
                }
            )
        )
        response = receive(connection)

        assert response["type"] == "decode_ok"
        assert response["path"] == "fast", "path 必须原样传到返回帧，不能回落到默认值"
        assert any(
            item["text"] == "CQ K1ABC FN42" for item in response["results"]  # type: ignore[index]
        )
        del pcm, slot, wave12, wave48
    finally:
        rx_shm.close()
        rx_shm.unlink()
        tx_shm.close()
        tx_shm.unlink()


def test_worker_reports_the_cpu_time_the_decode_burned(
    running_worker: tuple[Connection, mp.Process],
) -> None:
    """解码必须自报它烧掉的 CPU 时间 —— wall 时间看不出假并行。

    根因 A 只在 CPU 时间上可见：W103D 上 threads 1→3 的 wall 只快 1.46×，总 CPU
    却涨 1.6×（分析 §8.2），因为 sync 阶段被每个线程原样重算。这条钉的是
    **worker 自己量**，而不是父进程猜：父进程的 ``RUSAGE_CHILDREN`` 对还活着的
    子进程不更新，量不到单次请求的 CPU。静音槽就够 —— 解码算法照跑，CPU 必然
    消耗，``results`` 为空不影响这件事。
    """
    connection, _process = running_worker
    shm = SharedMemory(create=True, size=360_000)
    try:
        pcm = np.ndarray((180_000,), dtype="<i2", buffer=shm.buf)
        pcm[:] = 0
        connection.send_bytes(
            encode_frame(
                {
                    "v": 1,
                    "type": "decode",
                    "generation": 3,
                    "request_id": 7,
                    "slot_id": 11_908_800,
                    "deadline_monotonic": 999_999_999.0,
                    "shm": decode_descriptor(shm),
                    "config": config(path="fast"),
                }
            )
        )
        response = receive(connection)

        assert response["type"] == "decode_ok"
        cpu = response["cpu_seconds"]
        assert type(cpu) is float and cpu > 0.0, (
            "worker 必须上报本进程为这次解码消耗的 CPU 时间"
        )
        del pcm
    finally:
        shm.close()
        shm.unlink()


def test_wrong_generation_is_rejected_before_opening_shared_memory_and_loop_continues(
    running_worker: tuple[Connection, mp.Process],
) -> None:
    connection, _process = running_worker
    connection.send_bytes(
        encode_frame(
            {
                "v": 1,
                "type": "decode",
                "generation": 2,
                "request_id": 10,
                "slot_id": 1,
                "deadline_monotonic": 999_999_999.0,
                "shm": {
                    "name": "this_segment_must_not_exist",
                    "dtype": "<i2",
                    "shape": [180_000],
                    "nbytes": 360_000,
                },
                "config": config(),
            }
        )
    )
    response = receive(connection)
    assert response["type"] == "error"
    assert response["code"] == "stale_generation"
    assert response["generation"] == 2
    assert response["request_id"] == 10

    connection.send_bytes(
        encode_frame({"v": 1, "type": "ping", "generation": 3, "request_id": 11})
    )
    assert receive(connection)["type"] == "pong"


@pytest.mark.parametrize("actual_size", [344_064, 360_449])
def test_actual_shared_memory_size_must_match_and_worker_does_not_unlink(
    running_worker: tuple[Connection, mp.Process], actual_size: int
) -> None:
    connection, _process = running_worker
    shm = SharedMemory(create=True, size=actual_size)
    name = shm.name
    try:
        connection.send_bytes(
            encode_frame(
                {
                    "v": 1,
                    "type": "decode",
                    "generation": 3,
                    "request_id": actual_size,
                    "slot_id": 1,
                    "deadline_monotonic": 999_999_999.0,
                    "shm": {
                        "name": name,
                        "dtype": "<i2",
                        "shape": [180_000],
                        "nbytes": 360_000,
                    },
                    "config": config(),
                }
            )
        )
        response = receive(connection)
        assert response["type"] == "error"
        assert response["code"] == "shared_memory_size"

        connection.send_bytes(
            encode_frame(
                {"v": 1, "type": "ping", "generation": 3, "request_id": 12}
            )
        )
        assert receive(connection)["type"] == "pong"
    finally:
        shm.close()
        shm.unlink()

    with pytest.raises(FileNotFoundError):
        SharedMemory(name=name, create=False)


def test_binding_validation_error_is_sanitized_and_worker_continues(
    running_worker: tuple[Connection, mp.Process],
) -> None:
    connection, _process = running_worker
    shm = SharedMemory(create=True, size=360_000)
    try:
        connection.send_bytes(
            encode_frame(
                {
                    "v": 1,
                    "type": "decode",
                    "generation": 3,
                    "request_id": 20,
                    "slot_id": 1,
                    "deadline_monotonic": 999_999_999.0,
                    "shm": decode_descriptor(shm),
                    "config": config(profile=5),
                }
            )
        )
        response = receive(connection)
        assert response["type"] == "error"
        assert response["code"] == "invalid_request"
        assert response["detail"] == "DSP request validation failed"
        lowered = str(response).lower()
        assert "traceback" not in lowered
        assert "valueerror" not in lowered
        assert str(worker_library_path) not in str(response)

        connection.send_bytes(
            encode_frame(
                {"v": 1, "type": "ping", "generation": 3, "request_id": 21}
            )
        )
        assert receive(connection)["type"] == "pong"
    finally:
        shm.close()
        shm.unlink()


@pytest.mark.parametrize(
    "payload",
    [pytest.param(b"not json", id="malformed"), pytest.param(b" " * 65_537, id="oversize")],
)
def test_protocol_corruption_makes_spawned_worker_exit_nonzero(
    worker_library_path: Path, payload: bytes
) -> None:
    connection, process = start_worker(worker_library_path)
    try:
        connection.send_bytes(payload)
        process.join(10)
        assert process.exitcode not in (None, 0)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        connection.close()
