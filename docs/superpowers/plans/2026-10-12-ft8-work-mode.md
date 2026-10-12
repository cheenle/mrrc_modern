# FT8 工作模式实现计划（M1–M5）

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在 mrrc_modern 上新增 FT8 工作模式——菜单 SSB ↔ FT8 一键切换，FT8 引擎（WSJT-X Improved DSP + 编排层）从 `HAM/ft8`（MRRC-FT8）移植，复用平台的声卡（AudioHandler）、电台控制（CatController）与 TX 门控。

**架构：** `ft8/{core,engine,dsp}` 从 MRRC-FT8 verbatim 移植；平台耦合全部收敛在 `ft8/platform/`（rx_tap / tx_sink / cat / settings）+ `ft8/web/` + `ft8/mode.py`；`server.py` 只做接线（音频 tap、TX 门控泛化、模式生命周期）。规格：`docs/superpowers/specs/2026-10-12-ft8-work-mode-design.md`。

**技术栈：** Python 3.11（asyncio/FastAPI）、Fortran（gfortran + OpenMP + FFTW3f，设备上原生编译）、scipy（重采样）、vanilla JS 前端。

**执行前准备（本地）：** 本仓的开发环境是 `.venv/`（Python 3.13，含 pytest 9）；FT8 测试用 pytest 跑（`.venv/bin/python -m pytest tests/ft8/`），平台主套件仍用 unittest（`.venv/bin/python -m unittest discover -s tests`）。本计划在任务 7 引入 `soxr`、任务 6 引入 `scipy`：先执行

```bash
.venv/bin/pip install 'scipy>=1.12' 'soxr>=0.4'
```

**执行环境提醒：** 工作目录为 mrrc_modern 仓库根（分支 `feat/ft8`）。M1 的任务 5–6（盒子编译与基准）在 W103D 盒子上执行：`ssh root@192.168.1.217`（口令在操作者手里，用 `sshpass -e` 传入）。上游源码树在本机 `/Users/cheenle/HAM/ft8`。

---

## 文件结构（创建 / 修改）

| 路径 | 职责 |
| --- | --- |
| `ft8/dsp/` | vendor 抽取脚本产物（WSJT-X FT8 子集）+ CMake + 7 个补丁副本 + `build.sh` |
| `tools/ft8/extract_wsjtx_subset.sh` | 从 WSJT-X checkout 抽取 FT8 源码子集到 `ft8/dsp/vendor/`（清单唯一出处 = CMake 源列表） |
| `ft8/core/` | DSP Worker：ctypes 绑定、Shm 协议、监督进程、Worker 子进程入口（移植自 `HAM/ft8 server/core/`） |
| `ft8/engine/` | 编排层：orchestrator / sequencer / safety / tx_driver / dsp_decode / dsp_encode / msgparse / qso_log / repository / bands / tx_frequency / waterfall / latency / cq_loop / dxcc / audio_rx（UtcRing）/ audio_tx（常量） |
| `ft8/platform/rx_tap.py` | 44.1k→12k 流式重采样 + UTC 锚定 + UtcRing/瀑布投递 |
| `ft8/platform/tx_sink.py` | `PlatformTxPlayer`（48k 波形 → `feed_tx_audio`）+ 平台 TX 门控客户端 |
| `ft8/platform/cat.py` | `PlatformRig`：`CatController` → 引擎期望的 rig 接口 |
| `ft8/platform/settings.py` | FT8 设置（呼号/Grid/射频模式/频点…）读写 |
| `ft8/mode.py` | `Ft8ModeController`：SSB ↔ FT8 状态机、快照/恢复、引擎生命周期 |
| `ft8/web/ft8_api.py` | `/api/ft8/*` REST 端点（认证复用平台 token） |
| `ft8/web/ft8_ws.py` | `/WSft8` 解码/状态/TX 事件流 |
| `server.py`（修改） | TX 门控泛化（`_cq_key/_cq_unkey` → `TxSession`）、`_audio_rx_loop` 挂 tap、FT8 生命周期接线、`work_mode` 广播 |
| `static/ft8.html` + `static/ft8/*.js` | FT8 界面（从 MRRC-FT8 PWA 移植：解码列表/瀑布/TX 面板/QSO 面板/设置） |
| `static/index.html`（修改） | 主菜单加 "FT8" 项 |
| `tests/ft8/**` | 移植的 core/dsp/engine 测试 + 新适配层测试 |
| `requirements.txt`（修改） | 新增 `scipy` |

---

# 里程碑 M1：盒子 DSP spike（风险定论）

## 任务 1：vendor 抽取脚本与完整性测试

**文件：**
- 创建：`tools/ft8/extract_wsjtx_subset.sh`
- 创建：`tests/ft8/dsp/test_vendor_completeness.py`
- 生成：`ft8/dsp/vendor/**`、`ft8/dsp/vendor.sha256`

- [ ] **步骤 1：编写抽取脚本**

```bash
mkdir -p tools/ft8
```

写入 `tools/ft8/extract_wsjtx_subset.sh`：

```bash
#!/usr/bin/env bash
# Extract the WSJT-X FT8 source subset used by ft8/dsp into ft8/dsp/vendor/.
#
# The file list is NOT duplicated here.  It is derived from the CMake source
# lists in ft8/dsp/cmake/ (the files the build actually compiles) plus the
# recursive Fortran `include` closure of those sources and of the patched
# copies in ft8/dsp/patched/.
#
# usage: extract_wsjtx_subset.sh <wsjtx-checkout-root> [out-dir]
set -euo pipefail

WSJTX="${1:?usage: extract_wsjtx_subset.sh <wsjtx-checkout-root> [out-dir]}"
OUT="${2:-ft8/dsp/vendor}"
LIB="$WSJTX/lib"
[ -d "$LIB" ] || { echo "no lib/ under $WSJTX" >&2; exit 1; }

python3 - "$WSJTX" "$OUT" <<'PY'
import re
import shutil
import sys
from pathlib import Path

wsjtx, out = Path(sys.argv[1]), Path(sys.argv[2])
lib = wsjtx / "lib"
cmake_files = [Path("ft8/dsp/cmake/standard-ft8.cmake"),
               Path("ft8/dsp/cmake/improved-ft8.cmake")]
patched = Path("ft8/dsp/patched")

# 1. Sources referenced by the build's CMake lists (the single source of truth).
refs = set()
for cm in cmake_files:
    text = cm.read_text()
    refs.update(m.group(1) for m in re.finditer(r"\$\{WSJTX_LIB\}/([^ )]+)", text))

# 2. Recursive include closure, resolved against the compiler's -I paths
#    (${WSJTX_LIB}, ${WSJTX_LIB}/ft8, ${WSJTX_LIB}/ft8var are on the include path).
include_re = re.compile(r"^\s*include\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
search_dirs = [lib, lib / "ft8", lib / "ft8var"]
seen, queue = set(), set(refs)
while queue:
    rel = queue.pop()
    src = lib / rel
    if not src.is_file():
        raise SystemExit(f"referenced source missing: {src}")
    for line in src.read_text(errors="replace").splitlines():
        m = include_re.match(line)
        if not m:
            continue
        name = m.group(1)
        found = False
        for d in search_dirs:
            candidate = d / name
            if candidate.is_file():
                rel_inc = str(candidate.relative_to(lib))
                if rel_inc not in seen:
                    seen.add(rel_inc)
                    queue.add(rel_inc)
                found = True
                break
        if not found:
            raise SystemExit(f"include {name!r} of {rel} not found under {lib}")

for rel in sorted(refs | seen):
    src, dst = lib / rel, out / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"vendor/{rel}")
PY

# 3. Digest over the extracted tree (sorted, path-relative → stable).
python3 - "$OUT" <<'PY'
import hashlib
import sys
from pathlib import Path

out = Path(sys.argv[1])
rows = []
for p in sorted(out.rglob("*")):
    if p.is_file():
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        rows.append(f"{digest}  {p.relative_to(out)}")
(out.parent / "vendor.sha256").write_text("\n".join(rows) + "\n")
print(f"vendor.sha256: {len(rows)} files")
PY
```

- [ ] **步骤 2：运行抽取**

```bash
chmod +x tools/ft8/extract_wsjtx_subset.sh
./tools/ft8/extract_wsjtx_subset.sh /Users/cheenle/HAM/ft8/wsjtx-3.0.2
```

预期输出：约 105 行 `vendor/...` 路径（95 个 CMake 源 + include 闭包），末行 `vendor.sha256: <N> files`，N 与上面行数一致。

- [ ] **步骤 3：编写完整性测试（先失败）**

创建 `tests/ft8/dsp/test_vendor_completeness.py`：

```python
"""The vendored WSJT-X subset must cover everything the DSP build compiles."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DSP = ROOT / "ft8" / "dsp"
VENDOR = DSP / "vendor"

INCLUDE_RE = re.compile(r"^\s*include\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


def _cmake_refs() -> set[str]:
    refs: set[str] = set()
    for cm in sorted((DSP / "cmake").glob("*.cmake")):
        refs.update(m.group(1) for m in re.finditer(r"\$\{WSJTX_LIB\}/([^ )]+)", cm.read_text()))
    return refs


def test_every_cmake_referenced_source_is_vendored() -> None:
    missing = sorted(r for r in _cmake_refs() if not (VENDOR / r).is_file())
    assert missing == []


def test_include_closure_is_vendored() -> None:
    search_dirs = [VENDOR, VENDOR / "ft8", VENDOR / "ft8var"]
    queue = set(_cmake_refs()) | {
        str(p.relative_to(VENDOR)) for p in (DSP / "patched").glob("*.f90")
    }
    seen: set[str] = set()
    while queue:
        rel = queue.pop()
        src = VENDOR / rel
        assert src.is_file(), f"missing vendored source {rel}"
        for line in src.read_text(errors="replace").splitlines():
            m = INCLUDE_RE.match(line)
            if not m:
                continue
            name = m.group(1)
            hit = next((d / name for d in search_dirs if (d / name).is_file()), None)
            assert hit is not None, f"include {name!r} of {rel} not vendored"
            rel_inc = str(hit.relative_to(VENDOR))
            if rel_inc not in seen:
                seen.add(rel_inc)
                queue.add(rel_inc)


def test_vendor_digest_matches_tree() -> None:
    rows = []
    for p in sorted(VENDOR.rglob("*")):
        if p.is_file():
            rows.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(VENDOR)}")
    assert (DSP / "vendor.sha256").read_text() == "\n".join(rows) + "\n"
```

- [ ] **步骤 4：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/ft8/dsp/test_vendor_completeness.py -v`
预期：`test_every_cmake_referenced_source_is_vendored` FAIL（`ft8/dsp/cmake/` 还不存在——文件尚未移植）；其余收集报错。这是预期的（任务 2 提供 CMake 清单）。

- [ ] **步骤 5：Commit**

```bash
git add tools/ft8/extract_wsjtx_subset.sh tests/ft8/dsp/test_vendor_completeness.py
git commit -m "feat(ft8): WSJT-X FT8 子集抽取脚本与 vendor 完整性测试"
```

（`ft8/dsp/vendor/**` 与 `vendor.sha256` 在任务 2 移植 CMake 清单后一并提交——它们由步骤 2 生成，先留在工作区。）

---

## 任务 2：移植 DSP 构建系统与补丁副本

**文件：**
- 创建：`ft8/dsp/`（CMakeLists.txt、toolchain.cmake、cmake/*.cmake、`wsjt_*.f90`、`shmem_stub.c`、`wsjt_core.h`、`patched/`、`build.sh`）
- 修改：`ft8/dsp/CMakeLists.txt`（`WSJTX_LIB` 指向 `vendor/`）
- 创建：`tests/ft8/dsp/`（从 `HAM/ft8 tests/dsp/` 移植）

- [ ] **步骤 1：复制 DSP 源与构建文件**

```bash
mkdir -p ft8/dsp
cd /Users/cheenle/HAM/ft8/dsp
cp CMakeLists.txt toolchain.cmake wsjt_*.f90 shmem_stub.c wsjt_core.h /Users/cheenle/HAM/mrrc_modern/ft8/dsp/
cp -r cmake patched /Users/cheenle/HAM/mrrc_modern/ft8/dsp/
mkdir -p /Users/cheenle/HAM/mrrc_modern/tests/ft8/dsp
cd /Users/cheenle/HAM/ft8
cp -r tests/dsp/*.py tests/dsp/*.f90 tests/dsp/*.c tests/dsp/*.cmake /Users/cheenle/HAM/mrrc_modern/tests/ft8/dsp/
```

- [ ] **步骤 2：改 vendor 路径**

在 `ft8/dsp/CMakeLists.txt` 中：

```cmake
set(WSJTX_LIB "${CMAKE_CURRENT_SOURCE_DIR}/vendor")
```

（替换原 `set(WSJTX_LIB "${CMAKE_CURRENT_SOURCE_DIR}/../wsjtx-3.0.2/lib")`。）

- [ ] **步骤 3：修正移植测试的路径假设**

`tests/ft8/dsp/conftest.py` 与相关测试原本位于 `tests/dsp/`（`ROOT = Path(__file__).parents[2]`）。新位置深一层，逐处修正：

```bash
cd /Users/cheenle/HAM/mrrc_modern/tests/ft8/dsp
grep -rln 'parents\[2\]' . | xargs sed -i '' 's/parents\[2\]/parents[3]/g'          # macOS
grep -rln 'tests/dsp\|dsp/build\|"dsp"' . | head   # 人工核对剩余路径引用
```

逐文件核对 `conftest.py`、`test_vendor_policy.py`、`test_toolchain_contract.py`、`test_cmake_export_map.py` 中的 `ROOT / "dsp"` 类路径，改为 `ROOT / "ft8" / "dsp"`。

- [ ] **步骤 4：写 `ft8/dsp/build.sh`（幂等构建）**

```bash
#!/usr/bin/env bash
# Build libwsjt_core for the host platform.  Idempotent: skips when the build
# inputs are unchanged and the library already exists.  This is the script the
# box runs after mrrc-update (design §5 step 4).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$HERE/build"
DIGEST_FILE="$BUILD/.source-digest"
JOBS="${FT8_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)}"

digest() {
  python3 - "$HERE" "$BUILD" <<'PY'
import hashlib, sys
from pathlib import Path
here, build = Path(sys.argv[1]), Path(sys.argv[2])
h = hashlib.sha256()
exts = {".f90", ".c", ".h", ".cmake", ".txt"}
for p in sorted(here.rglob("*")):
    if build in p.parents or not p.is_file() or p.suffix not in exts:
        continue
    h.update(str(p.relative_to(here)).encode())
    h.update(p.read_bytes())
print(h.hexdigest())
PY
}

LIB="$BUILD/libwsjt_core.so"
[ "$(uname -s)" = "Darwin" ] && LIB="$BUILD/libwsjt_core.dylib"

if [ -f "$LIB" ] && [ -f "$DIGEST_FILE" ] && [ "$(cat "$DIGEST_FILE")" = "$(digest)" ]; then
  echo "ft8/dsp: build up to date ($LIB)"
  exit 0
fi

FORTRAN="${FT8_FORTRAN_COMPILER:-}"
if [ -z "$FORTRAN" ]; then
  for c in gfortran gfortran-mp-13 gfortran-13 gfortran-12; do
    command -v "$c" >/dev/null && { FORTRAN="$c"; break; }
  done
fi
[ -n "$FORTRAN" ] || { echo "ft8/dsp: no gfortran found (apt install gfortran)" >&2; exit 1; }

echo "ft8/dsp: building with $FORTRAN ($JOBS jobs)"
cmake -S "$HERE" -B "$BUILD" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_Fortran_COMPILER="$FORTRAN" >/dev/null
cmake --build "$BUILD" -j "$JOBS"
digest > "$DIGEST_FILE"
echo "ft8/dsp: built $LIB"
```

```bash
chmod +x ft8/dsp/build.sh
```

- [ ] **步骤 5：本地构建 + 移植测试全绿**

运行：
```bash
./ft8/dsp/build.sh
.venv/bin/python -m pytest tests/ft8/dsp/ -x -q
```
预期：构建成功；测试通过（解码/编码/并发/政策回归）。若 `test_vendor_policy.py` 因 vendor 路径失败，修正其 vendor 树根常量后重跑。

- [ ] **步骤 6：运行完整性测试（任务 1 的测试现在应通过）**

运行：`.venv/bin/python -m pytest tests/ft8/dsp/test_vendor_completeness.py -v`
预期：3 个测试全部 PASS。

- [ ] **步骤 7：Commit**

```bash
git add ft8/dsp tests/ft8/dsp
git commit -m "feat(ft8): 移植 WSJT-X FT8 DSP 构建系统与 vendor 子集（含 7 个补丁副本）"
```

---

## 任务 3：移植 DSP Worker 核心（ft8/core）

**文件：**
- 创建：`ft8/core/{__init__,binding,models,paths,protocol,supervisor,worker}.py`
- 修改：`ft8/core/worker.py`（库路径 → `ft8/dsp/build/`）
- 创建：`tests/ft8/core/`（从 `HAM/ft8 tests/core/` 移植）

- [ ] **步骤 1：复制 core 与测试**

```bash
mkdir -p ft8/core tests/ft8/core
cp /Users/cheenle/HAM/ft8/server/core/*.py ft8/core/
cp /Users/cheenle/HAM/ft8/tests/core/test_*.py tests/ft8/core/
```

- [ ] **步骤 2：改库路径与包内导入**

`ft8/core/worker.py` 中 `default_library_path()`：

```python
    base = Path(__file__).resolve().parents[2] / "ft8" / "dsp" / "build"
```

（`parents[2]` 在本仓库仍是仓库根：`ft8/core/worker.py → ft8/core → ft8 → 仓库根`。核对原行 `... / "dsp" / "build"` 改为 `... / "ft8" / "dsp" / "build"`。）

```bash
cd ft8/core
grep -rln "server\.core" . | xargs sed -i '' 's/server\.core/ft8.core/g'
cd /Users/cheenle/HAM/mrrc_modern/tests/ft8/core
grep -rln "server\.core" . | xargs sed -i '' 's/server\.core/ft8.core/g'
```

- [ ] **步骤 3：跑移植的 core 测试**

运行：`.venv/bin/python -m pytest tests/ft8/core/ -q`
预期：全部 PASS（`test_worker.py` 会真实加载 `ft8/dsp/build/libwsjt_core.dylib`——任务 2 已构建）。

- [ ] **步骤 4：核心冒烟——真实解码**

新建 `tests/ft8/core/test_integration_decode.py`：

```python
"""One end-to-end decode through the real Worker + vendored DSP library."""
from __future__ import annotations

import os

os.environ.setdefault("OMP_STACKSIZE", "10M")

import numpy as np
import pytest

from ft8.core.supervisor import WorkerSupervisor


def test_worker_decodes_synthetic_slot(tmp_path):
    lib = __import__("ft8.core.worker", fromlist=["default_library_path"]).default_library_path()
    if not lib.exists():
        pytest.skip("libwsjt_core not built (run ft8/dsp/build.sh)")
    # 由编码器生成一个已知消息的波形，再交给解码器（往返）。
    from ft8.core.binding import CoreBinding

    with CoreBinding(lib) as core:
        wave = core.encode("CQ BG1SB ON80", 1500.0)          # float32 @48k
        audio = np.zeros(12_000 * 15, dtype=np.int16)
        decimated = wave[::4][: 12_000 * 15]                  # 粗糙 4:1，仅冒烟
        audio[: decimated.size] = np.clip(decimated * 1000, -32768, 32767).astype(np.int16)
        results = core.decode(audio, profile=0, threads=1)
    texts = [r.text for r in results]
    assert any("BG1SB" in t for t in texts), texts
```

（若 `CoreBinding` 的实际方法名不同——以 `ft8/core/binding.py` 的公开方法为准调整；本测试的目的是"真实库、真实解码"的最小冒烟。）

- [ ] **步骤 5：运行冒烟测试**

运行：`.venv/bin/python -m pytest tests/ft8/core/test_integration_decode.py -v`
预期：PASS（或按真实接口微调后 PASS）。

- [ ] **步骤 6：Commit**

```bash
git add ft8/core tests/ft8/core
git commit -m "feat(ft8): 移植 DSP Worker 核心（ft8/core）并通过真实解码冒烟"
```

---

## 任务 4：盒子准备与 DSP 编译（W103D）

**文件：**
- 创建：`tools/ft8/deploy_box.sh`（把源码树同步到盒子并触发构建）

- [ ] **步骤 1：盒子安装构建依赖（一次性）**

```bash
export SSHPASS='<盒子 root 口令>'
sshpass -e ssh -o ConnectTimeout=10 root@192.168.1.217 \
  'apt-get update -qq && apt-get install -y -qq --no-install-recommends gfortran libfftw3-dev cmake && gfortran --version | head -1'
```

预期：`GNU Fortran (Debian 12.2.0-14) 12.2.0`（版本号可不同，但必须 ≥12）。

- [ ] **步骤 2：写同步脚本**

`tools/ft8/deploy_box.sh`：

```bash
#!/usr/bin/env bash
# Sync the FT8 tree to the W103D box and build libwsjt_core there.
# usage: deploy_box.sh [root@host]
set -euo pipefail

HOST="${1:-root@192.168.1.217}"
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
STAGE=/root/ft8-stage

ssh "$HOST" "rm -rf $STAGE && mkdir -p $STAGE"
rsync -az --delete \
  --exclude '__pycache__/' --exclude 'build/' --exclude '*.pyc' \
  "$HERE/ft8/dsp" "$HOST:$STAGE/dsp"
# ft8/core + tests are copied whole-tree by mrrc-update later; the spike only
# needs the DSP subset plus the core modules for the command-line decode test.
rsync -az --delete --exclude '__pycache__/' "$HERE/ft8/core" "$HOST:$STAGE/core"
rsync -az --delete --exclude '__pycache__/' "$HERE/tests/ft8" "$HOST:$STAGE/tests-ft8"
ssh "$HOST" "cd $STAGE && ./dsp/build.sh"
```

```bash
chmod +x tools/ft8/deploy_box.sh
```

- [ ] **步骤 3：在盒子上构建**

运行：
```bash
export SSHPASS='<盒子 root 口令>'
sshpass -e ./tools/ft8/deploy_box.sh
```

预期末行：`ft8/dsp: built /root/ft8-stage/dsp/build/libwsjt_core.so`。记录编译耗时（4 核 A53 预期 2–6 分钟）。

- [ ] **步骤 4：验证产物是 aarch64**

```bash
sshpass -e ssh root@192.168.1.217 \
  'python3 - <<PY
import struct
p="/root/ft8-stage/dsp/build/libwsjt_core.so"
h=open(p,"rb").read(20)
print("ELF", h[4], "machine", struct.unpack("<H", h[18:20])[0])  # 183 = AArch64
PY'
```

预期：`ELF 2 machine 183`。

- [ ] **步骤 5：Commit**

```bash
git add tools/ft8/deploy_box.sh
git commit -m "feat(ft8): 盒子同步与构建脚本（M1 spike）"
```

---

## 任务 5：盒子解码回归与性能基准（M1 出口）

**文件：**
- 创建：`docs/ft8-m1-baseline.md`（实测记录，M1 出口证据）

- [ ] **步骤 1：盒子上跑 Docker-free 解码回归**

```bash
export SSHPASS='<盒子 root 口令>'
sshpass -e ssh root@192.168.1.217 'cd /root/ft8-stage && OMP_STACKSIZE=10M \
  PYTHONPATH=/root/ft8-stage/.. python3 -m pytest tests-ft8/dsp/test_ft8_standard_decode.py tests-ft8/dsp/test_ft8_improved_decode.py -q' 
```

说明：`PYTHONPATH` 指到 stage 根使 `ft8.core` / `ft8.dsp` 可导入；若本机 python3 缺 scipy，先在盒子 venv 里 `pip install scipy pytest` 并用该解释器。

预期：解码测试 PASS（合成信号 12k 路径）。

- [ ] **步骤 2：44.1k→12k 重采样链的解码验证**

在盒子 venv 里运行（脚本 `tests-ft8/dsp/` 中的 i9 基准已用 `scipy.signal.resample_poly` 做过同类验证，此处显式跑一次 147:40）：

```bash
sshpass -e ssh root@192.168.1.217 '/opt/mrrc_modern/venv/bin/python - <<PY
import sys; sys.path.insert(0, "/root/ft8-stage")
import os; os.environ.setdefault("OMP_STACKSIZE", "10M")
import numpy as np
from scipy.signal import resample_poly
from ft8.core.binding import CoreBinding
from ft8.core.worker import default_library_path

lib = default_library_path()
with CoreBinding(lib) as core:
    wave48 = core.encode("CQ BG1SB ON80", 1200.0)      # float32 @48k
    a441 = resample_poly(wave48, 147, 160)              # 48k -> 44.1k (设备端)
    a12 = resample_poly(a441, 40, 147)                  # 44.1k -> 12k (接收链)
    audio = np.zeros(180000, dtype=np.int16)
    audio[:a12.size] = np.clip(a12*1200, -32768, 32767).astype(np.int16)
    res = core.decode(audio, profile=0, threads=1)
print([r.text for r in res])
assert any("BG1SB" in r.text for r in res), "resampled chain failed to decode"
print("RESAMPLE_CHAIN_OK")
PY'
```

预期：末行 `RESAMPLE_CHAIN_OK`。**这一步不通过则 M1 停止**（按设计 §7-2 处置）。

- [ ] **步骤 3：单时隙性能基准**

```bash
sshpass -e ssh root@192.168.1.217 'cd /root/ft8-stage && OMP_STACKSIZE=10M \
  MRRC_FT8_I9_BENCHMARK=1 MRRC_FT8_I9_JSON=/root/ft8-stage/i9-box.json \
  /opt/mrrc_modern/venv/bin/python -m pytest tests-ft8/dsp/test_i9_benchmark.py -q -s' 2>&1 | tail -40
```

预期：输出各 profile/thread 的耗时表；**判据：profile 3 + Auto(3) 单时隙 ≤ 8 s**（15 s 时隙留出 TX 决策窗口）。不达标 → 按设计 §7-1 降 profile 或缩窄解码带宽，并把结论写进步骤 4 的记录。

- [ ] **步骤 4：记录基线**

创建 `docs/ft8-m1-baseline.md`，写入：盒子型号/内核、gfortran 与 fftw 版本、编译耗时、解码回归结果、重采样链结果、i9 基准表（profile×threads 的 min/median）、结论（是否放行 M2）。

- [ ] **步骤 5：Commit**

```bash
git add docs/ft8-m1-baseline.md
git commit -m "docs(ft8): M1 盒子基线——DSP 编译/解码回归/重采样链/单时隙性能"
```

**M1 出口：** 上述四项全绿方可进入 M2。

---

（M2–M5 见续文）

---

# 里程碑 M2：引擎移植与平台适配（无 UI，测试驱动）

## 任务 6：移植 engine（含 rig 协议化）

**文件：**
- 创建：`ft8/engine/*.py`（从 `HAM/ft8 server/engine/` 移植）
- 修改：`ft8/engine/rig.py`（重写为协议 + 异常；rigctld 客户端删除）
- 修改：`ft8/engine/audio_rx.py`（保留 UtcRing/常量；删除 AudioCapture/RxConverter）
- 创建：`tests/ft8/engine/`（从 `HAM/ft8 tests/engine/` 移植）

**背景（已核实）：** engine 对 rig 的依赖只有 `safety.py` 的两处 `self._rig.set_ptt(True/False)`；`sequencer`/`orchestrator`/`tx_driver` 完全不依赖 rig。因此 `rig.py` 只需保留 `RigError` 与接口形状。

- [ ] **步骤 1：复制 engine 与测试**

```bash
cp /Users/cheenle/HAM/ft8/server/engine/*.py ft8/engine/
rm ft8/engine/{adif.py,adif_import.py,band_hunter.py,capture_proc.py,device_config.py}
cp /Users/cheenle/HAM/ft8/tests/engine/*.py tests/ft8/engine/
rm tests/ft8/engine/{test_adif.py,test_adif_import.py,test_band_hunter.py,test_capture_proc.py,test_device_config.py,test_rig.py}
```

- [ ] **步骤 2：重写 `ft8/engine/rig.py` 为协议 + 异常**

```python
"""Rig seam expected by the safety controller.

The production implementation is ``ft8.platform.cat.PlatformRig`` (the
platform's CatController + TX gate).  The previous rigctld client from
MRRC-FT8 is deliberately not carried over: the platform already owns the
serial link, and the FT-710 hamlib workarounds it contained do not apply.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class RigError(Exception):
    """A rig command failed; carries a stable code for interlocks."""

    def __init__(self, code: str, detail: str, rprt: int | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.rprt = rprt


@runtime_checkable
class RigClient(Protocol):
    """The only surface the engine uses (safety PTT plus mode/frequency)."""

    async def set_ptt(self, transmit: bool) -> None: ...
    async def get_ptt(self) -> bool: ...
    async def get_frequency(self) -> int: ...
    async def set_frequency(self, frequency_hz: int) -> None: ...
    async def get_mode(self) -> tuple[str, int]: ...
    async def set_mode(self, mode: str, passband_hz: int) -> None: ...
    async def set_filter_width(self, hz: int) -> None: ...
```

- [ ] **步骤 3：改 audio_rx.py（只留 ring 与常量）**

从 `ft8/engine/audio_rx.py` 中删除 `AudioCapture`（sounddevice 采集）与 `RxConverter`（48k 专用），保留 `UtcRing`、`RingMetrics`、`DECODER_SAMPLE_RATE`、`SLOT_SAMPLES`、`RESYNC_THRESHOLD_SECONDS`、`RX_DTYPE`；文件头 docstring 改为说明"采集由 ft8/platform/rx_tap 提供"。

同样处理 `ft8/engine/audio_tx.py`：删除 `TxPlayer`（本地声卡播放），保留 `TX_SAMPLE_RATE`、`MAX_TX_SAMPLES`、`validate_tx_buffer`、`TxAudioError`、`PlayResult`（`PlatformTxPlayer` 返回它）。

- [ ] **步骤 4：批量修导入**

```bash
cd ft8/engine
grep -rln "server\.core" . | xargs sed -i '' 's/server\.core/ft8.core/g'
grep -rn "capture_proc\|device_config\|from \.rig import\|AudioCapture\|RxConverter\|TxPlayer" . | head -20
```

预期：仅剩 `safety.py` 的 `from .rig import RigClient, RigError`（保留）与 `waterfall.py` 对 `audio_rx` 常量的引用（保留）。逐处核对无残留。

- [ ] **步骤 5：复制 DXCC 数据与 QSO 库路径约定**

```bash
mkdir -p ft8/data
cp /Users/cheenle/HAM/ft8/cty.dat ft8/data/cty.dat
```

`ft8/engine/dxcc.py` 里的 `cty.dat` 解析路径改为 `<repo>/ft8/data/cty.dat`（源文件用仓库根；逐处核对 `Path(__file__)` 推导）；QSO 库 `repository.py` 的 SQLite 路径不在模块内决定——由任务 11 的 runtime 传入 `<writable runtime>/ft8/qso.db`（仓库既有 `_writable_runtime_dir()` 惯例）。

- [ ] **步骤 6：跑移植的 engine 测试**

```bash
.venv/bin/python -m pytest tests/ft8/engine/ -q
```

预期：PASS（`test_safety.py` 用内存假 rig/player，不受协议化影响；`test_audio_rx.py`/`test_audio_tx.py` 中已删除类的用例一并删除，保留 UtcRing 与 validate 的用例）。

- [ ] **步骤 7：Commit**

```bash
git add ft8/engine ft8/data tests/ft8/engine
git commit -m "feat(ft8): 移植 engine 编排层；rig 协议化，去掉 rigctld 与本地声卡采集"
```

---

## 任务 7：RX tap（44.1k → 12k 流式重采样 + UTC 锚定）

**文件：**
- 创建：`ft8/platform/__init__.py`、`ft8/platform/rx_tap.py`
- 测试：`tests/ft8/platform/test_rx_tap.py`

**实现选择：** 流式重采样用 `soxr.ResampleStream`（专为流式设计；分块结果与整段一致，无 polyphase 相位陷阱）。依赖新增 `soxr`。

- [ ] **步骤 1：编写失败的测试**

```python
"""RX tap: device-rate blocks in, 12 kHz ring/spectrum out."""
from __future__ import annotations

import numpy as np
import pytest

from ft8.engine.audio_rx import SLOT_SAMPLES, UtcRing
from ft8.platform.rx_tap import RxTap


def _tone(rate: int, seconds: float, hz: float) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (0.5 * np.sin(2 * np.pi * hz * t) * 32767).astype(np.int16)


def test_output_rate_is_12k_and_continuous_across_blocks() -> None:
    ring = UtcRing(seconds=10)
    tap = RxTap(ring, dev_rate=44100)
    blocks = [b for b in np.array_split(_tone(44100, 2.0, 1000.0), 97) if b.size]
    epoch = 1000.0
    for b in blocks:
        tap.feed(b.tobytes(), epoch)
        epoch += b.size / 44100.0
    # 2 s in => ~24000 samples at 12 kHz, within a block's worth of slack.
    assert abs((ring.high_water - ring.base) - 24000) < 400
    assert ring.gap_count == 0


def test_block_split_matches_whole_buffer() -> None:
    """Feeding in chunks must equal feeding the same signal at once."""
    whole = RxTap(None, dev_rate=44100)
    y_whole = np.concatenate([whole.feed(b.tobytes(), 0.0) for b in [_tone(44100, 1.0, 700.0)]])
    split = RxTap(None, dev_rate=44100)
    parts = [split.feed(b.tobytes(), 0.0) for b in np.array_split(_tone(44100, 1.0, 700.0), 61)]
    y_split = np.concatenate(parts)
    assert abs(y_whole.size - y_split.size) <= 1
    n = min(y_whole.size, y_split.size)
    assert np.max(np.abs(y_whole[:n].astype(np.int32) - y_split[:n].astype(np.int32))) <= 2


def test_spectrum_hook_receives_the_same_12k_blocks() -> None:
    seen: list[tuple[int, float]] = []
    ring = UtcRing(seconds=10)
    tap = RxTap(ring, dev_rate=44100, on_block=lambda s, e: seen.append((s.size, e)))
    tap.feed(_tone(44100, 0.2, 500.0).tobytes(), 500.0)
    assert seen and all(size > 0 for size, _ in seen)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/ft8/platform/test_rx_tap.py -v`
预期：collection ERROR `ModuleNotFoundError: No module named 'ft8.platform.rx_tap'`。

- [ ] **步骤 3：实现 `ft8/platform/rx_tap.py`**

```python
"""FT8 receive tap on the platform's existing RX audio stream.

Takes the device-rate int16 blocks that ``server.py::_audio_rx_loop`` already
reads, resamples them to the decoder's 12 kHz domain in a streaming fashion
(block-boundary continuous), and delivers them to the UTC ring and the
optional spectrum hook.  No new device stream is opened.

UTC anchoring: the caller passes the epoch of each input block's first
sample; output blocks carry ``in_epoch + consumed_samples / dev_rate``, so
the epoch chain follows the input sample count (the hardware clock is the
timebase; the ring's resync absorbs scheduling jitter).
"""
from __future__ import annotations

import logging

import numpy as np

from ft8.engine.audio_rx import DECODER_SAMPLE_RATE, UtcRing

log = logging.getLogger("mrrc.ft8.rx_tap")

SOXR_QUALITY = "HQ"


class RxTap:
    """Device-rate int16 in → 12 kHz int16 out (ring + optional hook)."""

    def __init__(self, ring: UtcRing | None, *, dev_rate: int = 44_100,
                 out_rate: int = DECODER_SAMPLE_RATE,
                 on_block=None) -> None:
        if dev_rate <= 0:
            raise ValueError("dev_rate must be positive")
        import soxr

        self._ring = ring
        self._dev_rate = dev_rate
        self._on_block = on_block
        self._stream = soxr.ResampleStream(
            dev_rate, out_rate, 1, dtype="float32", quality=SOXR_QUALITY
        )
        self._base_epoch: float | None = None
        self._consumed = 0            # input samples fed so far

    @property
    def consumed_input_samples(self) -> int:
        return self._consumed

    def feed(self, pcm: bytes | np.ndarray, block_epoch: float) -> np.ndarray:
        """Resample one device-rate block; returns the 12 kHz int16 samples."""
        x = np.frombuffer(pcm, dtype="<i2") if isinstance(pcm, (bytes, bytearray)) else np.asarray(pcm)
        if x.dtype != np.float32:
            x = x.astype(np.float32) / 32768.0
        if x.size == 0:
            return np.zeros(0, dtype="<i2")

        if self._base_epoch is None:
            self._base_epoch = block_epoch
        out_epoch = self._base_epoch + self._consumed / self._dev_rate
        self._consumed += x.size

        y = self._stream.resample_chunk(x, last=False)
        block = np.clip(y * 32767.0, -32768.0, 32767.0).astype("<i2")
        if block.size:
            if self._ring is not None:
                self._ring.write(block, out_epoch)
            if self._on_block is not None:
                self._on_block(block, out_epoch)
        return block
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/ft8/platform/test_rx_tap.py -v`
预期：3 个 PASS。

- [ ] **步骤 5：Commit**

```bash
git add ft8/platform tests/ft8/platform/test_rx_tap.py
git commit -m "feat(ft8): RX tap——设备率→12k 流式重采样与 UTC 锚定投递"
```

---

## 任务 8：PlatformTxPlayer 与 PlatformRig

**文件：**
- 创建：`ft8/platform/tx_sink.py`、`ft8/platform/cat.py`
- 测试：`tests/ft8/platform/test_tx_sink.py`、`tests/ft8/platform/test_cat.py`

- [ ] **步骤 1：编写失败的测试**

`tests/ft8/platform/test_tx_sink.py`：

```python
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from ft8.platform.tx_sink import PlatformTxPlayer
from ft8.engine.audio_tx import MAX_TX_SAMPLES


class FakeAudio:
    def __init__(self):
        self.frames: list[bytes] = []
        self.peak = 0

    def tx_queue_frames(self) -> int:
        return 0

    def feed_tx_audio(self, pcm: bytes) -> None:
        self.frames.append(pcm)


def test_play_converts_float32_to_int16_20ms_frames():
    audio = FakeAudio()
    player = PlatformTxPlayer(audio, queued_frames=lambda: audio.tx_queue_frames())
    wave = np.zeros(960 * 3, dtype=np.float32)
    wave[0] = 1.0
    asyncio.run(player.play(wave))
    assert all(len(f) == 960 * 2 for f in audio.frames)      # 20 ms @48k int16
    assert len(audio.frames) == 3
    first = np.frombuffer(audio.frames[0], dtype="<i2")
    assert first[0] == 32767


def test_play_rejects_wrong_shape():
    player = PlatformTxPlayer(FakeAudio(), queued_frames=lambda: 0)
    with pytest.raises(ValueError):
        asyncio.run(player.play(np.zeros(MAX_TX_SAMPLES + 1, dtype=np.float32)))
```

`tests/ft8/platform/test_cat.py`：

```python
from __future__ import annotations

import asyncio

import pytest

from ft8.platform.cat import PlatformRig


class FakeCat:
    def __init__(self):
        self.freq = 14_074_000
        self.mode = 0x02
        self.filter_index = 20
        self.commands: list[tuple] = []

    async def get_frequency(self, vfo="A"):
        return self.freq

    async def set_frequency(self, hz, vfo="A"):
        self.commands.append(("freq", hz))
        self.freq = hz
        return True

    async def get_mode(self, timeout=None):
        return self.mode

    async def set_mode(self, mode_num: int):
        self.commands.append(("mode", mode_num))
        self.mode = mode_num
        return True

    async def set_filter_width(self, index: int):
        self.commands.append(("width", index))
        self.filter_index = index
        return True


class FakeGate:
    def __init__(self):
        self.keyed = False
        self.calls: list[str] = []

    async def key(self) -> bool:
        self.calls.append("key")
        self.keyed = True
        return True

    async def unkey(self, graceful: bool = False) -> None:
        self.calls.append("unkey")
        self.keyed = False


def test_mode_mapping_data_u_and_usb():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="DATA-U")
    asyncio.run(rig.set_mode("DATA-U", 3000))
    assert cat.mode == 0x0C
    rig2 = PlatformRig(cat, gate, rig_mode="USB")
    asyncio.run(rig2.set_mode("USB", 3000))
    assert cat.mode == 0x02


def test_filter_width_hz_maps_to_nearest_narrow_index():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="DATA-U")
    asyncio.run(rig.set_filter_width(3000))
    assert ("width", 18) in cat.commands        # FILTER_WIDTHS_NARROW: 18 → 3000 Hz


def test_ptt_goes_through_the_platform_gate():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="DATA-U")
    asyncio.run(rig.set_ptt(True))
    assert gate.keyed is True
    asyncio.run(rig.set_ptt(False))
    assert gate.keyed is False
    assert gate.calls == ["key", "unkey"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/ft8/platform/test_tx_sink.py tests/ft8/platform/test_cat.py -v`
预期：`ModuleNotFoundError`（两个模块未创建）。

- [ ] **步骤 3：实现 `ft8/platform/tx_sink.py`**

```python
"""TX sink: the FT8 waveform's way into the platform's TX path.

``PlatformTxPlayer`` is injected into the engine's safety controller in place
of MRRC-FT8's local-soundcard ``TxPlayer``.  It feeds the 48 kHz float32
waveform into ``AudioHandler.feed_tx_audio`` (the codec domain the platform's
CQ player already uses) as 20 ms int16 frames, paced by the TX queue depth.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import numpy as np

from ft8.engine.audio_tx import PlayResult, validate_tx_buffer

CODEC_RATE = 48_000
FRAME_SAMPLES = 960                      # 20 ms at 48 kHz — same frame as CQ
FRAME_SECONDS = FRAME_SAMPLES / CODEC_RATE
LOW_WATER_FRAMES = 4                     # keep at most this much queued
PACING_TIMEOUT_S = 2.0


class PlatformTxPlayer:
    """Play one bounded waveform through the platform TX queue."""

    def __init__(self, audio, *, queued_frames: Callable[[], int]) -> None:
        self._audio = audio
        self._queued = queued_frames

    async def play(self, samples: np.ndarray) -> PlayResult:
        pcm = validate_tx_buffer(samples)
        pcm16 = np.clip(pcm, -1.0, 1.0)
        pcm16 = (pcm16 * 32767.0).astype("<i2").tobytes()
        started = time.monotonic()
        try:
            offset = 0
            deadline = started + len(pcm) / CODEC_RATE + PACING_TIMEOUT_S
            while offset < len(pcm16):
                while self._queued() > LOW_WATER_FRAMES:
                    if time.monotonic() > deadline:
                        break
                    await asyncio.sleep(0.005)
                self._audio.feed_tx_audio(pcm16[offset:offset + FRAME_SAMPLES * 2])
                offset += FRAME_SAMPLES * 2
            # Let the device drain what is still queued before returning.
            remaining = (len(pcm) / CODEC_RATE) - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return PlayResult(samples_written=0, cancelled=True,
                              elapsed_seconds=time.monotonic() - started)
        return PlayResult(samples_written=int(pcm.size), cancelled=False,
                          elapsed_seconds=time.monotonic() - started)
```

- [ ] **步骤 4：实现 `ft8/platform/cat.py`**

```python
"""PlatformRig: the engine's rig seam over the platform's CatController.

Mode/frequency/filter widths go straight to ``CatController`` (the platform
already owns the serial link; no hamlib).  PTT does NOT touch CAT directly —
it goes through the platform TX gate, which owns the keying sequence, the
audio stream start/stop and the TX owner bookkeeping.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

from backends.ft710.config_ft710 import FILTER_WIDTHS_NARROW
from ft8.engine.rig import RigError

FT710_MODE_NUM = {"LSB": 0x01, "USB": 0x02, "CW-U": 0x03, "FM": 0x04, "AM": 0x05,
                  "RTTY-L": 0x06, "CW-L": 0x07, "DATA-L": 0x08, "RTTY-U": 0x09,
                  "DATA-FM": 0x0A, "FM-N": 0x0B, "DATA-U": 0x0C, "AM-N": 0x0D,
                  "PSK": 0x0E, "DATA-FM-N": 0x0F}
_FT710_MODE_NAME = {v: k for k, v in FT710_MODE_NUM.items()}

DEFAULT_PASSBAND_HZ = 3000


class TxGate(Protocol):
    async def key(self) -> bool: ...
    async def unkey(self, graceful: bool = False) -> None: ...
    @property
    def keyed(self) -> bool: ...


class PlatformRig:
    """``RigClient`` over ``CatController`` + the platform TX gate."""

    def __init__(self, cat: Any, tx_gate: TxGate, *, rig_mode: str = "DATA-U",
                 passband_hz: int = DEFAULT_PASSBAND_HZ) -> None:
        self._cat = cat
        self._gate = tx_gate
        self._rig_mode = rig_mode
        self._passband_hz = passband_hz

    # ---- PTT: the platform gate is the only path -------------------------
    async def set_ptt(self, transmit: bool) -> None:
        if transmit:
            ok = await self._gate.key()
            if not ok:
                raise RigError("PTT", "platform TX gate refused to key")
        else:
            await self._gate.unkey(graceful=False)

    async def get_ptt(self) -> bool:
        return bool(self._gate.keyed)

    # ---- frequency -------------------------------------------------------
    async def get_frequency(self) -> int:
        value = await self._cat.get_frequency()
        if value is None:
            raise RigError("CAT", "no frequency reply")
        return int(value)

    async def set_frequency(self, frequency_hz: int) -> None:
        if not await self._cat.set_frequency(int(frequency_hz)):
            raise RigError("CAT", f"set_frequency({frequency_hz}) refused")

    # ---- mode ------------------------------------------------------------
    async def get_mode(self) -> tuple[str, int]:
        num = await self._cat.get_mode()
        name = _FT710_MODE_NAME.get(int(num), "USB") if num is not None else "USB"
        return name, self._passband_hz

    async def set_mode(self, mode: str, passband_hz: int = DEFAULT_PASSBAND_HZ) -> None:
        if mode in ("", None):
            mode = self._rig_mode
        num = FT710_MODE_NUM.get(mode.upper())
        if num is None:
            raise RigError("CAT", f"unknown mode {mode!r}")
        self._passband_hz = int(passband_hz) or DEFAULT_PASSBAND_HZ
        if not await self._cat.set_mode(num):
            raise RigError("CAT", f"set_mode({mode}) refused")

    async def set_filter_width(self, hz: int) -> None:
        index = min(FILTER_WIDTHS_NARROW, key=lambda row: abs(row[1] - int(hz)))[0]
        if not await self._cat.set_filter_width(index):
            raise RigError("CAT", f"set_filter_width({hz} Hz) refused")
```

- [ ] **步骤 5：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/ft8/platform/test_tx_sink.py tests/ft8/platform/test_cat.py -v`
预期：全部 PASS。

- [ ] **步骤 6：Commit**

```bash
git add ft8/platform/tx_sink.py ft8/platform/cat.py tests/ft8/platform/
git commit -m "feat(ft8): PlatformTxPlayer 与 PlatformRig——TX 波形注入与电台控制适配"
```

---

## 任务 9：Ft8ModeController（SSB ↔ FT8 状态机）

**文件：**
- 创建：`ft8/mode.py`
- 测试：`tests/ft8/test_mode.py`

**接口假设（任务 12 的接线会提供）：** 构造入参是一组可注入的协程回调——
`start_engine()/stop_engine()`（拉起/停止 DSP+编排，任务 12 实现）、
`apply_ft8_radio()`/`snapshot_ssb()/restore_ssb()`（CAT 操作，走 PlatformRig）、
`broadcast(mode)`、`disable_browser_tx(bool)`。

- [ ] **步骤 1：编写失败的测试**

```python
from __future__ import annotations

import asyncio

import pytest

from ft8.mode import Ft8ModeController, WorkMode


class Harness:
    def __init__(self):
        self.radio = {"freq": 14_200_000, "mode": 0x02, "width": 20}
        self.snapshot = None
        self.engine_running = False
        self.browser_tx_enabled = True
        self.broadcasts: list[str] = []
        self.fail_on_start = False

    async def snapshot_ssb(self):
        self.snapshot = dict(self.radio)
        return self.snapshot

    async def apply_ft8_radio(self):
        self.radio = {"freq": 14_074_000, "mode": 0x0C, "width": 18}

    async def restore_ssb(self, snap):
        self.radio = dict(snap)

    async def start_engine(self):
        if self.fail_on_start:
            raise RuntimeError("dsp worker failed")
        self.engine_running = True

    async def stop_engine(self):
        self.engine_running = False

    def set_browser_tx(self, enabled: bool):
        self.browser_tx_enabled = enabled

    async def broadcast(self, mode: str):
        self.broadcasts.append(mode)


def _controller(h: Harness) -> Ft8ModeController:
    return Ft8ModeController(
        snapshot_ssb=h.snapshot_ssb, apply_ft8_radio=h.apply_ft8_radio,
        restore_ssb=h.restore_ssb, start_engine=h.start_engine,
        stop_engine=h.stop_engine, set_browser_tx=h.set_browser_tx,
        broadcast=h.broadcast,
    )


def test_switch_round_trip_restores_every_field():
    h = Harness()
    ctl = _controller(h)
    asyncio.run(ctl.switch_to_ft8())
    assert ctl.mode is WorkMode.FT8 and h.engine_running
    assert h.radio == {"freq": 14_074_000, "mode": 0x0C, "width": 18}
    assert h.browser_tx_enabled is False
    asyncio.run(ctl.switch_to_ssb())
    assert ctl.mode is WorkMode.SSB and not h.engine_running
    assert h.radio == h.snapshot
    assert h.browser_tx_enabled is True
    assert h.broadcasts == ["ft8", "ssb"]


def test_engine_start_failure_rolls_back_to_ssb():
    h = Harness()
    h.fail_on_start = True
    ctl = _controller(h)
    with pytest.raises(RuntimeError):
        asyncio.run(ctl.switch_to_ft8())
    assert ctl.mode is WorkMode.SSB
    assert h.radio == {"freq": 14_200_000, "mode": 0x02, "width": 20}
    assert h.browser_tx_enabled is True


def test_double_switch_is_a_noop():
    h = Harness()
    ctl = _controller(h)
    asyncio.run(ctl.switch_to_ft8())
    asyncio.run(ctl.switch_to_ft8())
    assert h.broadcasts == ["ft8"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/ft8/test_mode.py -v`
预期：`ModuleNotFoundError: No module named 'ft8.mode'`。

- [ ] **步骤 3：实现 `ft8/mode.py`**

```python
"""FT8 work mode: the SSB ↔ FT8 switch, snapshot and engine lifecycle.

The mode is a server-wide singleton (design §4.5): one owner of the radio at
a time, all sessions observe the same state.  Switching is serialized; a
failed transition always lands back in SSB, never half-started.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum

log = logging.getLogger("mrrc.ft8.mode")


class WorkMode(StrEnum):
    SSB = "ssb"
    FT8 = "ft8"


class Ft8ModeController:
    def __init__(self, *, snapshot_ssb: Callable[[], Awaitable[dict]],
                 apply_ft8_radio: Callable[[], Awaitable[None]],
                 restore_ssb: Callable[[dict], Awaitable[None]],
                 start_engine: Callable[[], Awaitable[None]],
                 stop_engine: Callable[[], Awaitable[None]],
                 set_browser_tx: Callable[[bool], None],
                 broadcast: Callable[[str], Awaitable[None]]) -> None:
        self._snapshot_ssb = snapshot_ssb
        self._apply_ft8_radio = apply_ft8_radio
        self._restore_ssb = restore_ssb
        self._start_engine = start_engine
        self._stop_engine = stop_engine
        self._set_browser_tx = set_browser_tx
        self._broadcast = broadcast
        self._mode = WorkMode.SSB
        self._lock = asyncio.Lock()
        self._snapshot: dict | None = None

    @property
    def mode(self) -> WorkMode:
        return self._mode

    async def switch_to_ft8(self) -> None:
        async with self._lock:
            if self._mode is WorkMode.FT8:
                return
            self._snapshot = await self._snapshot_ssb()
            self._set_browser_tx(False)
            try:
                await self._apply_ft8_radio()
                await self._start_engine()
            except Exception:
                log.exception("FT8 start failed; rolling back to SSB")
                await self._safe_rollback()
                raise
            self._mode = WorkMode.FT8
            await self._broadcast("ft8")

    async def switch_to_ssb(self) -> None:
        async with self._lock:
            if self._mode is WorkMode.SSB:
                return
            # Safety first: stop TX/engine before restoring the SSB radio.
            await self._stop_engine()
            if self._snapshot is not None:
                await self._restore_ssb(self._snapshot)
            self._set_browser_tx(True)
            self._mode = WorkMode.SSB
            await self._broadcast("ssb")

    async def _safe_rollback(self) -> None:
        try:
            await self._stop_engine()
        except Exception:
            log.exception("engine stop failed during rollback")
        if self._snapshot is not None:
            try:
                await self._restore_ssb(self._snapshot)
            except Exception:
                log.exception("SSB restore failed during rollback")
        self._set_browser_tx(True)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/ft8/test_mode.py -v`
预期：3 个 PASS。

- [ ] **步骤 5：Commit**

```bash
git add ft8/mode.py tests/ft8/test_mode.py
git commit -m "feat(ft8): 模式状态机——SSB↔FT8 切换、快照恢复与失败回滚"
```

---

## 任务 10：FT8 设置存储（platform/settings.py）

**文件：**
- 创建：`ft8/platform/settings.py`
- 测试：`tests/ft8/platform/test_settings.py`

- [ ] **步骤 1：编写失败的测试**

```python
from __future__ import annotations

import json

from ft8.platform.settings import Ft8Settings, Ft8SettingsStore


def test_defaults_are_usable_without_a_file(tmp_path):
    store = Ft8SettingsStore(tmp_path / "settings.json")
    s = store.load()
    assert s == Ft8Settings(my_call="", my_grid="", rig_mode="DATA-U",
                            tx_audio_frequency=1500, settings_revision=0)


def test_round_trip_and_validation(tmp_path):
    path = tmp_path / "settings.json"
    store = Ft8SettingsStore(path)
    store.save(Ft8Settings(my_call="BA4EG", my_grid="PM01", rig_mode="USB",
                           tx_audio_frequency=1200))
    again = store.load()
    assert again.my_call == "BA4EG" and again.rig_mode == "USB"


def test_bad_rig_mode_is_rejected_on_save(tmp_path):
    store = Ft8SettingsStore(tmp_path / "settings.json")
    import pytest
    with pytest.raises(ValueError):
        store.save(Ft8Settings(rig_mode="FM"))
```

- [ ] **步骤 2：实现并跑通**

运行：`.venv/bin/python -m pytest tests/ft8/platform/test_settings.py -v` → 先 FAIL（模块缺失），实现后 PASS。

`ft8/platform/settings.py`：

```python
"""FT8 settings — independent of the platform's own config (design §4.9).

Stores the operator's FT8 identity and radio preferences in a single JSON
file under the platform's writable runtime dir.  No secrets here.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("mrrc.ft8.settings")

RIG_MODES = ("DATA-U", "USB")
DEFAULT_TX_AUDIO_HZ = 1500


@dataclass(frozen=True)
class Ft8Settings:
    my_call: str = ""
    my_grid: str = ""
    rig_mode: str = "DATA-U"
    tx_audio_frequency: int = DEFAULT_TX_AUDIO_HZ
    settings_revision: int = 0

    def validate(self) -> None:
        if self.rig_mode not in RIG_MODES:
            raise ValueError(f"rig_mode must be one of {RIG_MODES}")
        if not 200 <= int(self.tx_audio_frequency) <= 2800:
            raise ValueError("tx_audio_frequency must be 200..2800 Hz")


class Ft8SettingsStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Ft8Settings:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return Ft8Settings()
        except (OSError, ValueError) as exc:
            log.warning("ft8 settings unreadable (%s); using defaults", exc)
            return Ft8Settings()
        known = {f.name for f in dataclasses.fields(Ft8Settings)}
        return Ft8Settings(**{k: v for k, v in raw.items() if k in known})

    def save(self, settings: Ft8Settings) -> None:
        settings.validate()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = dataclasses.asdict(settings)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".ft8-settings-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._path)          # atomic
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
```

- [ ] **步骤 3：Commit**

```bash
git add ft8/platform/settings.py tests/ft8/platform/test_settings.py
git commit -m "feat(ft8): FT8 设置存储（独立 JSON、原子写、值域校验）"
```

---

## 任务 11：server.py 接线（TxSession 泛化 + RX tap + 生命周期）

**文件：**
- 修改：`server.py`（`_cq_key`/`_cq_unkey` → `TxSession`；`_audio_rx_loop` 挂 tap；FT8 引擎生命周期；`work_mode` 入 state）
- 修改：`requirements.txt`（加 `scipy`、`soxr`）
- 测试：`tests/ft8/test_server_wiring.py`

- [ ] **步骤 1：泛化 TX 门控为 `TxSession`**

在 `server.py` 中把 `_cq_key(owner_token)` / `_cq_unkey(graceful)` 的实现体提取为一个类（保留原函数名作为薄包装，`CQPlayer` 的绑定不变）：

```python
class TxSession:
    """The platform's one TX keying sequence (CQ player and FT8 share it)."""

    def __init__(self) -> None:
        self._keyed = False

    @property
    def keyed(self) -> bool:
        return self._keyed

    async def key(self, owner_token: Optional[str] = None) -> bool:
        if backend is not None and backend.capabilities.tx_gated:
            return False
        _claim_tx_owner_for_token(owner_token)
        try:
            await cat.set_ptt(True)
            radio.update(tx_status=1)
            if audio and not await asyncio.to_thread(audio.start_tx):
                logger.error("TX: audio stream failed — unkeying radio")
                await cat.set_ptt(False)
                radio.update(tx_status=0)
                return False
        except Exception as e:
            logger.error("TX: keying failed (%s) — releasing", e)
            try:
                await cat.set_ptt(False)
            except Exception:
                pass
            radio.update(tx_status=0)
            return False
        self._keyed = True
        return True

    async def unkey(self, graceful: bool = False) -> None:
        self._keyed = False
        if audio:
            await asyncio.to_thread(audio.stop_tx, graceful)
        if cat is not None and cat.connected:
            try:
                await cat.set_ptt(False)
            except Exception as e:
                logger.warning("TX: unkey failed: %s", e)
        radio.update(tx_status=0, power_meter=0, alc_meter=0, swr_meter=0,
                     comp_meter=0, id_meter=0)
        scheduler and scheduler.skip_next_poll("tx_status", 1.0)
```

`_cq_key`/`_cq_unkey` 改为调用它（`_tx_session` 全局，lifespan 里创建）；`_cq_key` 里原有的 `_tx_cq_mic_drops = 0` 与 unkey 的 tx_stats 日志保留在原包装里。

- [ ] **步骤 2：`_audio_rx_loop` 挂 FT8 tap**

在 `pcm = audio.read_rx_chunk()` 的成功分支（`_rec_tap_rx` 旁边）加：

```python
                if _ft8_runtime is not None and _ft8_runtime.rx_tap is not None:
                    _ft8_runtime.rx_tap.feed(pcm, time.time() - len(pcm) / 2 / audio._rx_dev_rate)
```

（int16 单声道：`len(pcm)/2` 是样本数。）

- [ ] **步骤 3：FT8 引擎生命周期与 mode 控制器**

新增 `ft8/runtime.py`（把散布在 server.py 的组装收进一个类，供 mode 控制器调用）：

```python
"""FT8 runtime: builds the engine pieces and owns their lifecycle."""
class Ft8Runtime:
    def __init__(self, *, settings_store, platform_rig, tx_player, spectrum_fanout):
        ...
    @property
    def rx_tap(self): ...
    async def start(self) -> None:
        """Start DSP worker, orchestrator, tx_driver; wire the ring."""
    async def stop(self) -> None:
        """Stop TX/disarm, then orchestrator, then the worker."""
    async def request_stop_tx(self, reason: str) -> None: ...
```

（实现按 `HAM/ft8 server/main.py` 的 lifespan 组装顺序移植：WorkerSupervisor → SupervisorDecoder → SafetyController(PlatformRig, PlatformTxPlayer) → Sequencer → TxDriver → Orchestrator → SpectrumComputer。`rx_tap` 在 `start()` 构建、`stop()` 置空。）

在 `server.py` 的 lifespan 里创建 `Ft8ModeController`，其 `start_engine`/`stop_engine` 调 `Ft8Runtime.start/stop`，`broadcast` 走既有 `_broadcast_state()`（`stateUpdate` 增加 `"work_mode": ctrl.mode.value`）。

- [ ] **步骤 4：写接线测试**

```python
"""server.py wiring: shared TX gate, work_mode in state, inert tap in SSB."""
from __future__ import annotations

import asyncio
import types

import server as server_mod


class FakeCat:
    def __init__(self):
        self.ptt = 0
        self.connected = True

    async def set_ptt(self, tx):
        self.ptt = 1 if tx else 0
        return True


class FakeAudio:
    def __init__(self):
        self.tx_started = False

    def start_tx(self):
        self.tx_started = True
        return True

    def stop_tx(self, graceful=False):
        self.tx_started = False


class FakeRadio:
    def __init__(self):
        self.tx_status = 0

    def update(self, **kw):
        self.tx_status = kw.get("tx_status", self.tx_status)


def _session():
    return server_mod.TxSession()


def test_tx_session_keys_and_unkeys_once(monkeypatch):
    cat, audio, radio = FakeCat(), FakeAudio(), FakeRadio()
    monkeypatch.setattr(server_mod, "cat", cat)
    monkeypatch.setattr(server_mod, "audio", audio)
    monkeypatch.setattr(server_mod, "radio", radio)
    monkeypatch.setattr(server_mod, "backend", types.SimpleNamespace(
        capabilities=types.SimpleNamespace(tx_gated=False)))
    monkeypatch.setattr(server_mod, "_claim_tx_owner_for_token", lambda tok: None)
    monkeypatch.setattr(server_mod, "scheduler", types.SimpleNamespace(
        skip_next_poll=lambda name, secs: None))
    s = _session()
    assert asyncio.run(s.key()) is True
    assert cat.ptt == 1 and audio.tx_started and s.keyed
    asyncio.run(s.unkey(graceful=False))
    assert cat.ptt == 0 and not audio.tx_started and not s.keyed


def test_tx_session_refuses_when_backend_gated(monkeypatch):
    cat, audio = FakeCat(), FakeAudio()
    monkeypatch.setattr(server_mod, "cat", cat)
    monkeypatch.setattr(server_mod, "audio", audio)
    monkeypatch.setattr(server_mod, "backend", types.SimpleNamespace(
        capabilities=types.SimpleNamespace(tx_gated=True)))
    assert asyncio.run(_session().key()) is False
    assert cat.ptt == 0


def test_work_mode_is_in_full_state_snapshot(monkeypatch):
    ctrl = types.SimpleNamespace(mode=types.SimpleNamespace(value="ssb"))
    monkeypatch.setattr(server_mod, "_ft8_mode", ctrl)
    snap = server_mod._snapshot()
    assert snap["work_mode"] == "ssb"
```

（若 `_snapshot()` 需要更多全局对象，monkeypatch 补齐即可；断言点不变。）

- [ ] **步骤 5：跑全量回归**

运行：
```bash
.venv/bin/python -m pytest tests/ft8/ -q
.venv/bin/python -m unittest discover -s tests   # 平台主套件（unittest）
```
预期：两组全绿（平台 113 个测试不受影响）。

- [ ] **步骤 6：Commit**

```bash
git add server.py ft8/runtime.py requirements.txt tests/ft8/test_server_wiring.py
git commit -m "feat(ft8): server 接线——TxSession 泛化、RX tap 挂载与引擎生命周期"
```

**M2 出口：** 本地测试全绿；盒子命令行可完成"起引擎 → 解码 → 编码发射"（任务 12 的部署验证）。


---

# 里程碑 M3：Web 层（REST + WS + 模式切换）

## 任务 12：FT8 REST API（ft8/web/ft8_api.py）

**文件：**
- 创建：`ft8/web/__init__.py`、`ft8/web/ft8_api.py`
- 修改：`server.py`（`include_router` + 依赖注入）
- 测试：`tests/ft8/web/test_ft8_api.py`

**移植策略：** 把 `HAM/ft8 server/web/api.py` 整体复制为起点，按下表逐项改造。改造点只有三类：认证、AppState 装配、路径前缀。业务端点（状态/解码/操作/日志/DXCC）逻辑不动。

| 源（MRRC-FT8） | 目标（本仓） | 改造 |
| --- | --- | --- |
| `@app.get("/")` 静态挂载 | 删除 | mrrc_modern 自己的静态与路由已存在 |
| `session/login|logout|reauth` | 删除 | 用平台 `/api/login`（cookie 已在用户登录 `/` 时建立） |
| 认证 `state.auth.authenticate(cookie)` | `_verify_auth(request)` | 平台既有函数（Bearer 或 `AUTH_COOKIE`） |
| `create_app(state)` | `create_ft8_router(state: Ft8ApiState)` | `APIRouter(prefix="/api/ft8")`，由 `server.py` include |
| `state.sequencer/safety/orchestrator/...` | 同一批对象，来自 `ft8/runtime.py`（任务 11） | 装配点变化 |
| `/lease/*` | 暂不暴露（单租约由平台控制权承担） | 首版省略；操作端点按控制角色校验 |

- [ ] **步骤 1：复制并建立骨架**

```bash
mkdir -p ft8/web tests/ft8/web
cp /Users/cheenle/HAM/ft8/server/web/api.py ft8/web/ft8_api.py
cp /Users/cheenle/HAM/ft8/tests/web/test_api.py tests/ft8/web/test_ft8_api.py
cp /Users/cheenle/HAM/ft8/tests/web/conftest.py tests/ft8/web/conftest.py
```

- [ ] **步骤 2：改造认证与装配（核心 diff）**

`ft8/web/ft8_api.py` 顶部改为：

```python
"""FT8 REST surface, mounted by the platform at /api/ft8.

Authentication is the platform's (cookie or Bearer, same as /api/*); the
FT8 surface adds no second session system.  ``listen``-role sessions get
read-only access — TX and mode switching require the control role.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request

from ft8.mode import Ft8ModeController, WorkMode


@dataclass
class Ft8ApiState:
    repository: object
    sequencer: object
    safety: object
    runtime: object
    mode: Ft8ModeController
    settings_store: object
    auth_check: object          # callable(request) -> bool
    role_of: object             # callable(request) -> "control" | "listen"


def create_ft8_router(state: Ft8ApiState) -> APIRouter:
    router = APIRouter(prefix="/api/ft8")

    def require_auth(request: Request) -> None:
        if not state.auth_check(request):
            raise HTTPException(status_code=401, detail="unauthenticated")

    def require_control(request: Request, what: str) -> None:
        require_auth(request)
        if state.role_of(request) != "control":
            raise HTTPException(status_code=403, detail=f"{what} requires the control role")

    # …原有端点函数体逐字保留，只改装饰器与守卫：
    @router.get("/state", dependencies=[Depends(require_auth)])
    async def get_state() -> dict: ...

    @router.get("/decodes", dependencies=[Depends(require_auth)])
    async def get_decodes(limit: int = 200) -> dict: ...

    @router.post("/operation/reply")
    async def operation_reply(request: Request, payload: dict) -> dict:
        require_control(request, "reply")
        # …原 body
    # …（operation/cq、operation/stop、operation/clear-fault、settings 读写、
    #      logs/qsos、logs/adif、dxcc 同法；每个写端点首行 require_control）

    return router
```

保留的端点（首版）：`GET /state`、`GET /decodes`、`GET /health`、
`POST /operation/select|reply|cq|stop|enable_tx_off|clear-fault`、
`GET/PUT /settings`、`GET /logs/qsos`、`GET /logs/adif`、`GET /dxcc`。
删除：`session/*`、`lease/*`、`radio/band|mode|filter|rig/level`（电台控制归平台 UI，FT8 页面不重复造）、
`band-hunt`、`diagnostics/health`（并入 `GET /health`）。

- [ ] **步骤 3：在 server.py 装配**

lifespan 中（FT8 runtime 建好后）：

```python
    ft8_router = create_ft8_router(Ft8ApiState(
        repository=ft8_runtime.repository,
        sequencer=ft8_runtime.sequencer,
        safety=ft8_runtime.safety,
        runtime=ft8_runtime,
        mode=ft8_mode,
        settings_store=ft8_settings_store,
        auth_check=_verify_auth,
        role_of=lambda req: "listen" if _is_listen_request(req) else "control",
    ))
    app.include_router(ft8_router)
```

- [ ] **步骤 4：写 API 测试**

```python
def test_ft8_state_requires_auth(client_noauth):
    r = client_noauth.get("/api/ft8/state")
    assert r.status_code == 401


def test_ft8_state_with_control_cookie(client_control):
    r = client_control.get("/api/ft8/state")
    assert r.status_code == 200
    body = r.json()
    assert "mode" in body and body["mode"] in ("ssb", "ft8")


def test_listen_role_cannot_reply(client_listen):
    r = client_listen.post("/api/ft8/operation/reply", json={"index": 0})
    assert r.status_code == 403
```

（fixture 沿用平台既有测试的 cookie 生成方式，见 `tests/test_cloud_endpoints.py` 的 client 构造。）

- [ ] **步骤 5：跑测试**

运行：`.venv/bin/python -m pytest tests/ft8/web/test_ft8_api.py -q`
预期：PASS。

- [ ] **步骤 6：Commit**

```bash
git add ft8/web server.py tests/ft8/web
git commit -m "feat(ft8): REST 面挂到平台认证（/api/ft8，listen 只读）"
```

---

## 任务 13：FT8 WebSocket（ft8/web/ft8_ws.py）

**文件：**
- 创建：`ft8/web/ft8_ws.py`
- 修改：`server.py`（注册路由）
- 测试：`tests/ft8/web/test_ft8_ws.py`

- [ ] **步骤 1：实现（从 `HAM/ft8 server/web/ws.py` 移植广播器，换握手认证）**

```python
"""FT8 streams: decodes, mode/TX events.  One WS endpoint at /WSft8.

Handshake authentication is the platform's (`_token_from_ws_handshake`:
header, cookie, then query).  Clients receive JSON frames:
  {"type": "hello",   "mode": "ssb"|"ft8"}
  {"type": "decodes", "slot": int, "items": [...]}
  {"type": "tx",      "state": "armed"|"tx"|"idle"|"refused", "detail": str}
  {"type": "mode",    "mode": "ssb"|"ft8"}
"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


def create_ft8_ws_router(state) -> APIRouter:
    router = APIRouter()

    @router.websocket("/WSft8")
    async def ft8_stream(ws: WebSocket) -> None:
        token = state.token_from_handshake(ws)
        if not state.auth_check_token(token):
            await ws.accept()
            await ws.close(code=4401, reason="unauthenticated")
            return
        await ws.accept()
        sub = state.decode_bus.subscribe()
        try:
            await ws.send_json({"type": "hello", "mode": str(state.mode.mode)})
            async for frame in sub:
                await ws.send_json(frame)
        except WebSocketDisconnect:
            pass
        finally:
            state.decode_bus.unsubscribe(sub)

    return router
```

`DecodeBus`（同文件内，~40 行 `asyncio.Queue` 扇出，掉帧丢最旧——照 `HAM/ft8 server/web/ws.py` 的 `DecodeBroadcaster` 移植）；`server.py` 里把 orchestrator 的解码回调接进 bus，并在模式切换时广播 `{"type": "mode", ...}`。

- [ ] **步骤 2：测试**

```python
def test_ws_requires_auth(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/WSft8") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_hello_reports_mode(client_auth):
    with client_auth.websocket_connect("/WSft8") as ws:
        hello = ws.receive_json()
    assert hello["type"] == "hello" and hello["mode"] == "ssb"
```

- [ ] **步骤 3：跑测试 + Commit**

```bash
.venv/bin/python -m pytest tests/ft8/web/test_ft8_ws.py -q
git add ft8/web/ft8_ws.py server.py tests/ft8/web/test_ft8_ws.py
git commit -m "feat(ft8): /WSft8 解码与模式事件流（平台握手认证）"
```

---

## 任务 14：模式切换端点与 work_mode 广播

**文件：**
- 修改：`ft8/web/ft8_api.py`（`POST /mode`）、`server.py`（state 广播加字段）
- 测试：`tests/ft8/web/test_mode_endpoint.py`

- [ ] **步骤 1：加端点**

```python
    @router.post("/mode")
    async def set_mode(request: Request, payload: dict) -> dict:
        require_control(request, "mode switch")
        target = str(payload.get("mode", "")).lower()
        if target == "ft8":
            await state.mode.switch_to_ft8()
        elif target == "ssb":
            await state.mode.switch_to_ssb()
        else:
            raise HTTPException(status_code=400, detail="mode must be 'ssb' or 'ft8'")
        return {"ok": True, "mode": str(state.mode.mode)}
```

- [ ] **步骤 2：`stateUpdate` 加 `work_mode`**

`server.py` 的 `_broadcast_state()` 载荷里加 `"work_mode": str(_ft8_mode.mode)`；`fullState` 同样（`_snapshot()` 处）。SSB 前端读它做提示条。

- [ ] **步骤 3：测试**

```python
def test_mode_switch_round_trip_through_api(client_control):
    assert client_control.post("/api/ft8/mode", json={"mode": "ft8"}).json()["mode"] == "ft8"
    assert client_control.post("/api/ft8/mode", json={"mode": "ssb"}).json()["mode"] == "ssb"


def test_bad_mode_is_400(client_control):
    assert client_control.post("/api/ft8/mode", json={"mode": "cw"}).status_code == 400
```

- [ ] **步骤 4：跑测试 + Commit**

```bash
.venv/bin/python -m pytest tests/ft8/ -q
git add ft8/web/ft8_api.py server.py tests/ft8/web/test_mode_endpoint.py
git commit -m "feat(ft8): 模式切换端点与 work_mode 状态广播"
```

**M3 出口：** `pytest tests/ft8/` 全绿；curl 能完成 `mode=ft8 → /api/ft8/state → mode=ssb` 往返。

---

# 里程碑 M4：FT8 前端

## 任务 15：FT8 页面骨架与客户端改造

**文件：**
- 创建：`static/ft8.html`、`static/ft8/api.js`、`static/ft8/streams.js`、`static/ft8/state.js`
- 复制改造自：`HAM/ft8 server/web/static/{index.html,js/api.js,js/streams.js,js/state.js}`

**改造映射（api.js / streams.js）：**

| 源 | 目标值 |
| --- | --- |
| `const response = await fetch(\`/api/v1${path}\`, ...)` | `fetch(\`/api/ft8${path}\`, {credentials: "same-origin"})` |
| `location.reload()` on 401 | 保留（未登录时回登录页） |
| `login/logout/currentSession` 端点 | 删除（认证由平台页面负责） |
| `new WebSocket(...\`/ws/v1/state\`)` 等三条 | 单一 `new WebSocket(\`${scheme}://${location.host}/WSft8\`)`，按 `msg.type` 分发 |
| 三路 connected 状态 | 单路，保留退避表 `[500,1000,2000,5000]` |

- [ ] **步骤 1：生成页面骨架**

```bash
mkdir -p static/ft8
cp /Users/cheenle/HAM/ft8/server/web/static/js/api.js static/ft8/api.js
cp /Users/cheenle/HAM/ft8/server/web/static/js/streams.js static/ft8/streams.js
cp /Users/cheenle/HAM/ft8/server/web/static/js/state.js static/ft8/state.js
cp /Users/cheenle/HAM/ft8/server/web/static/js/main.js static/ft8/main.js
cp -r /Users/cheenle/HAM/ft8/server/web/static/css/app.css static/ft8/app.css
cp /Users/cheenle/HAM/ft8/server/web/static/index.html static/ft8.html
```

然后按下表逐文件改造（用编辑器，不要盲改）：

```bash
cd static/ft8
sed -i '' 's|/api/v1|/api/ft8|g' api.js
sed -i '' 's|/ws/v1/state|/WSft8|g; s|/ws/v1/decodes|/WSft8|g; s|/ws/v1/waterfall|/WSft8|g' streams.js
grep -n "session/login\|currentSession\|location.reload" api.js main.js   # 逐处删除/改写
```

`static/ft8.html` 中：`<link>`/`<script>` 路径改 `/static/ft8/...`；删除登录视图（`login-view`）；`<title>` 改 "MRRC FT8"；顶部加两个按钮：

```html
<header class="ft8-header">
  <a class="ft8-back" href="/">← SSB</a>
  <span class="ft8-title">FT8</span>
  <button id="ft8-settings-btn" type="button">设置</button>
</header>
```

- [ ] **步骤 2：`static/ft8/main.js` 引导改造**

```javascript
import { api } from "./api.js";
import { applySnapshot, subscribe } from "./state.js";
import { startStreams } from "./streams.js";

async function boot() {
  const res = await fetch("/api/ft8/state", { credentials: "same-origin" });
  if (res.status === 401 || res.status === 403) {
    location.href = "/";           // platform login lives on the root page
    return;
  }
  applySnapshot(await res.json());
  startStreams();
  document.getElementById("ft8-back")?.addEventListener("click", async () => {
    await api.request("/mode", { method: "POST", body: { mode: "ssb" } });
    location.href = "/";
  });
}
boot();
```

- [ ] **步骤 3：验证（手动）**

```bash
# 本地起服务（开发机有 FT-710 或无硬件也可只开页面）
OMP_STACKSIZE=10M python3 server.py &   # 或仓库既有启动方式
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8888/static/ft8.html   # 200
```

浏览器打开 `/static/ft8.html`：已登录时进入 FT8 页面、未登录时跳 `/`；Network 面板看到 `/WSft8` 101 与 `hello` 帧。

- [ ] **步骤 4：Commit**

```bash
git add static/ft8.html static/ft8
git commit -m "feat(ft8): FT8 页面骨架与流客户端（平台认证，单 WS）"
```

## 任务 16：解码列表与高亮

**文件：**
- 创建：`static/ft8/decodes.js`；改造 `static/ft8/candidates.js`（从源 PWA 复制）

- [ ] **步骤 1：接入 `decodes` 帧**

```javascript
import { subscribe } from "./state.js";

export function createDecodeList(root) {
  subscribe((state) => {
    const items = state.decodes || [];
    root.replaceChildren(...items.map((d) => row(d)));
  });

  function row(d) {
    const el = document.createElement("button");
    el.className = `decode-row highlight-${d.highlight || "none"}`;
    el.textContent = `${d.text}  ${d.snr >= 0 ? "+" : ""}${d.snr}  ${d.freq_hz.toFixed(0)}`;
    el.dataset.index = d.index;
    el.addEventListener("click", () => select(d.index));       // 单击=选择（永不发射）
    el.addEventListener("dblclick", () => reply(d.index));     // 双击=回复（移动 PWA 惯例）
    return el;
  }
  function select(i) { document.dispatchEvent(new CustomEvent("ft8-select", { detail: i })); }
  async function reply(i) { await api.request("/operation/reply", { method: "POST", body: { index: i } }); }
}
```

高亮类名与源 PWA 的 `highlight` 字段一致（`my_call` / `new_dxcc` / `new_call` / `new_grid` / `worked`），CSS 从 `HAM/ft8 server/web/static/css/app.css` 复制对应规则块。

- [ ] **步骤 2：手动验证**：真机或重放的解码出现后，行文本、SNR、高亮、单击/双击行为符合 MRRC-FT8 的既有语义。

- [ ] **步骤 3：Commit**

```bash
git add static/ft8/decodes.js static/ft8/app.css
git commit -m "feat(ft8): 解码列表与 JTDX 风格高亮（单击选择/双击回复）"
```

## 任务 17：TX 控制面板与 QSO 面板

**文件：**
- 创建：`static/ft8/txpanel.js`、`static/ft8/qso.js`（移植源 PWA 的 `safety.js`/候选栏逻辑）

- [ ] **步骤 1：TX 面板**（回复/CQ/停止/音频频率/发射使能/CQ 循环开关），全部按钮 → `/api/ft8/operation/*`；停止按钮任何角色可用（紧急 STOP）。CQ 循环开关对接 `ft8/engine/cq_loop.py` 的状态事件（同一条 `tx`/`cq` 事件帧驱动按钮的回显）。
- [ ] **步骤 2：QSO 面板**渲染 `state.qso`（对方呼号、状态行、TX 频率偏移、进度）。
- [ ] **步骤 3：手动验证**：`tx` 事件帧驱动按钮状态（armed/发射中/拒绝），STOP 立即生效。
- [ ] **步骤 4：Commit**

```bash
git add static/ft8/txpanel.js static/ft8/qso.js
git commit -m "feat(ft8): TX 控制面板与 QSO 面板"
```

## 任务 18：瀑布与设置面板

**文件：**
- 创建：`static/ft8/waterfall.js`（从源 PWA 移植，数据源改为 `/WSft8` 的 `waterfall` 帧或轮询 `/api/ft8/decodes` 的频谱字段）、`static/ft8/settings.js`

- [ ] **步骤 1：瀑布**：canvas 绘制与源 PWA 相同；`state.spectrum` 帧到达即绘。
- [ ] **步骤 2：设置面板**：字段 = `my_call`、`my_grid`、`rig_mode`（select：DATA-U/USB）、`tx_audio_frequency`；`PUT /api/ft8/settings` 保存，显示"保存成功/失败"可见反馈。
- [ ] **步骤 3：Commit**

```bash
git add static/ft8/waterfall.js static/ft8/settings.js
git commit -m "feat(ft8): 瀑布与 FT8 设置面板（呼号/Grid/射频模式/音频频率）"
```

## 任务 19：菜单集成与 SSB 侧模式提示

**文件：**
- 修改：`static/index.html`（菜单项）、`static/ft710_main.js` 或 `ft710_ui.js`（模式提示条 + TX 禁用）

- [ ] **步骤 1：菜单加项**（`main-menu` 的 `menu-list`）：

```html
            <li>
              <a href="#" class="menu-item" data-action="ft8-mode">FT8</a>
            </li>
```

在菜单 action 处理处加：

```javascript
		case "ft8-mode":
			fetch("/api/ft8/mode", {
				method: "POST", credentials: "same-origin",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ mode: "ft8" }),
			}).then((r) => { if (r.ok) location.href = "/static/ft8.html"; });
			break;
```

- [ ] **步骤 2：SSB 侧提示与禁用**（主界面 `stateUpdate.work_mode === "ft8"` 时）：显示提示条 `FT8 模式中 — 发射由 FT8 引擎控制`，并给 TX/PTT 控件加 `disabled`；`work_mode` 回 `ssb` 时移除。实现点：`ft710_ui.js` 的 `applyState`/`updateUI` 一处集中处理。

- [ ] **步骤 3：手动验证**

1. `/` 登录 → 菜单点 FT8 → 跳到 `/static/ft8.html` 且 state 显示 `ft8`
2. FT8 页点 "← SSB" → 回 `/` 且 TX 控件恢复可用
3. 两个浏览器会话：A 切 FT8，B 的 SSB 页面立即出现提示条并禁用 TX

- [ ] **步骤 4：Commit**

```bash
git add static/index.html static/ft710_main.js static/ft710_ui.js
git commit -m "feat(ft8): 主菜单 FT8 入口与 SSB 侧模式提示/禁用"
```

**M4 出口：** 浏览器里能完成一次完整 QSO 流程（选择→回复→序列→73），SSB ↔ FT8 双向切换可用。

---

# 里程碑 M5：部署、真机验收与文档

## 任务 20：盒子部署

**文件：**
- 创建：`tools/ft8/deploy_box.sh`（沿用 M1 版本，扩展为正式部署：rsync → mrrc-update → build → restart）

- [ ] **步骤 1：正式部署流程（在开发机上）**

```bash
# 1) 同步整树到盒子（排除构建产物与本地状态，由 mrrc-update 的 stage-tree 再筛一遍）
export SSHPASS='<盒子 root 口令>'
rsync -az --delete --exclude 'venv/' --exclude '__pycache__/' --exclude 'build/' \
  --exclude 'certs/' --exclude 'logs/' --exclude '.git/' \
  ./ root@192.168.1.217:/root/mrrc-modern-stage/

# 2) 盒子上执行既有更新器（会补装 requirements.txt 新增项：scipy/soxr）
sshpass -e ssh root@192.168.1.217 \
  'MRRC_UPDATE_SRC=/root/mrrc-modern-stage mrrc-update'

# 3) 构建 DSP（幂等；首次数分钟）
sshpass -e ssh root@192.168.1.217 \
  'cd /opt/mrrc_modern && ./ft8/dsp/build.sh'

# 4) 重启服务并看日志
sshpass -e ssh root@192.168.1.217 \
  'systemctl restart mrrc-modern && sleep 3 && journalctl -u mrrc-modern -n 30 --no-pager'
```

预期：服务 active；日志无 `ModuleNotFoundError` / `libwsjt_core` 加载失败。

- [ ] **步骤 2：验证页面与模式切换（浏览器）**

1. `https://192.168.1.217:8888/` 登录（平台口令）
2. 菜单 → FT8 → 页面出现、`/WSft8` 连接、解码开始滚动（真实波段）
3. 切回 SSB → 主界面恢复、无 TX 残留（电台表头 PTT 灯灭）

- [ ] **步骤 3：Commit**

```bash
git add tools/ft8/deploy_box.sh
git commit -m "feat(ft8): 盒子正式部署流程（rsync → mrrc-update → DSP 构建 → 重启）"
```

## 任务 21：真机验收（规格 §6）

- [ ] **步骤 1–6：逐项执行并记录**（写入 `docs/ft8-m5-acceptance.md`）

1. **解码**：切 FT8 收到实台解码；高亮（my_call / new DXCC / new grid）正确
2. **TX**：CQ 一轮 → 电台键控、功率输出、SWR 正常；波形时长 12.6 s（用录音或 `date` 误差 <0.2 s）
3. **QSO**：完成一次真 QSO 并落库（`sqlite3 /opt/mrrc_modern/.../qso.db 'select count(*) from qso'`）
4. **切换往返 ×10**：无 PTT 残留（每次切回后 `cat.get_ptt() == 0`）、SSB 台况逐字段恢复（频率/模式/滤波/功率/AGC/ATT/PREAMP）、SSB 听音与频谱不断流
5. **紧急 STOP**：FT8 TX 进行中，另一会话点 STOP → PTT 立即释放
6. **性能**：切 FT8 后连续 10 分钟监视：单时隙解码完成有裕量、SSB 音频不断（`journalctl` 无 underrun/overrun 报警）

- [ ] **步骤 2：Commit**

```bash
git add docs/ft8-m5-acceptance.md
git commit -m "docs(ft8): M5 真机验收记录（盒子 + FT-710）"
```

## 任务 22：文档同步与收尾

**文件：**
- 修改：`SDD/09-architecture-overview.md`、`SDD/11-component-model.md`、`SDD/12-operational-model.md`、`SDD/14-version-history.md`、`CHANGELOG.md`、`AGENTS.md`（模块表）
- 修改：`docs/superpowers/specs/2026-10-12-ft8-work-mode-design.md`（状态 → 已实现）

- [ ] **步骤 1：SDD 与 CHANGELOG**：按仓库惯例（sdd-guardian 技能）在对应章节记录：新组件（`ft8/` 包）、模式状态机、TX 门控泛化、部署步骤（盒子 DSP 编译）、NFR 边界（不新增声卡/串口所有权）。
- [ ] **步骤 2：AGENTS.md 模块表**加 `ft8/` 一行（职责、移植来源、边界）。
- [ ] **步骤 3：规格状态改为已实现**，并把 M1 基线与 M5 验收记录链接进去。
- [ ] **步骤 4：全量回归 + Commit**

```bash
python3 -m pytest tests/ -q
git add SDD CHANGELOG.md AGENTS.md docs/superpowers/specs/2026-10-12-ft8-work-mode-design.md
git commit -m "docs(ft8): SDD/CHANGELOG/模块表同步，规格状态更新为已实现"
```

**M5 出口：** 验收记录归档、文档同步完成、全量测试绿。

---

# 执行顺序与依赖

```
M1(1→2→3→4→5) ──► M2(6→7→8→9→10→11) ──► M3(12→13→14) ──► M4(15→16→17→18→19) ──► M5(20→21→22)
```

- 任务 1–3 在本地完成（macOS 可直接构建 DSP 并跑全部解码回归）。
- 任务 4–5 需要盒子在线；**任务 5 的重采样链验证不通过则全线暂停**（规格 §7-2）。
- 任务 6–11 期间盒子不参与（纯本地）。
- 任务 12 起每次部署前在本地跑 `.venv/bin/python -m pytest tests/ft8/ -q`。
