# FT8 快路径 + 自适应级联解码 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每个 FT8 时隙默认用轻量 C 解码器（`ft8_lib`）完成解码，仅在命中判据时对同一时隙补跑现有 WSJT-X 深度路径，从而把典型解码 wall 从 ~2.2 s 降到 ≤ 0.3 s，同时**不损失任何解出条数**。

**Architecture:** 三层。native 层把 ft8_lib 编进既有 `libwsjt_core` 并新增 C ABI 入口 `wsjt_ft8_decode_fast`；contract 层 `ft8/core/binding.py` 仍是唯一的 ctypes 边界，只多一个 `DecodePath.FAST` 分支；engine 层新增纯 Python 的 `CascadingDecoder`（包住 `SupervisorDecoder`），级联策略、合并与预算守卫全部住在这里，可脱原生代码单测。

**Tech Stack:** Fortran/C（CMake + OpenMP + FFTW3F，既有）＋ ft8_lib（C + kiss-fft，新增 vendor 子集）＋ Python 3.13（ctypes / asyncio / multiprocessing）＋ `unittest`（仓库既有测试运行器）。

**Spec:** `docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md`

## Global Constraints

- **测试运行器：`tests/ft8/` 下是 `pytest`，不是 `unittest`**（已核实：该目录**没有**任何文件使用 `unittest.TestCase`；`unittest discover` 收集到 **0** 个 FT8 用例，`pytest` 收集到 **660** 个）。命令：
  `.venv/bin/python -m pytest tests/ft8 -q`（单个文件/单条用例同理加路径）。
  **注意 PATH 上没有 `python`**，一律写 `.venv/bin/python`。
  仓库级的 `python -m unittest discover -s tests` 只是**非 ft8 套件**的总闸，对本计划的用例一条都收不到——用它"验证通过"等于没验证。
  新增测试文件叫 `test_*.py`，放在既有对应目录（`tests/ft8/core/`、`tests/ft8/dsp/`、`tests/ft8/engine/`），写成 pytest 风格的模块级函数 + fixture。
- **FT8 铁律（AGENTS.md）**：`os.environ.setdefault("OMP_STACKSIZE", "10M")` 必须在导入 NumPy / 加载 `libwsjt_core` 之前执行。**Task 内不得移动或重排这些 import**（`ft8/core/binding.py:9`、`ft8/core/worker.py:63` 是现有位置）。
- **唯一 ctypes 边界**：不得新增第二个 `CDLL`。所有原生调用经 `ft8/core/binding.py`。
- **平台耦合只在边缘**：级联策略只用纯 Python（stdlib + 既有 dataclass）。
- **vendor 子树必须可溯源**：新 vendor 目录附 `README.md`（上游 URL + 钉住的 commit）与 SHA-256 清单，并由测试校验磁盘文件与清单一致。
- **三平台都要编过**：macOS `libwsjt_core.dylib`、Windows `wsjt_core.dll`（MinGW）、Linux `libwsjt_core.so`。CMake 的 source list 与 include dir 必须平台无关。
- **abi 版本同步三处**：`ft8/dsp/wsjt_core.h`、`ft8/dsp/wsjt_types.f90`、`ft8/core/binding.py` 的 `_ABI_VERSION` 本次一起从 1 → 2。
- **提交前**：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须干净；commit 用短祈使句。
- **Python 风格**：4 空格缩进、共享状态带类型注解、模块常量 `UPPER_CASE`、类 `PascalCase`。
- **不得改动** `static/ft710_main.js` / `static/ft710_ui.js`（工具守卫），也不得动 `atr1000_tuner.json` / `mem_channels.json` / `website/js/global-nav.js`。

## Review Focus

spec 是愿景文档；以下几类输入/失败模式它没有逐条写，但一个合理的人会期望程序正确处理。每条都在其所属 Task 的测试里固定住：

1. **同一信号被两条路径解出不同文本**（spec R-2）：fast 用 `<HASH>` 形式，deep 会还原呼号 → 去重键会把它们当两条，用户看到重复行。期望：显示不重复。→ Task 9。
2. **非法 `wsjt_decode_config`**（如 `low_frequency >= high_frequency`、`sample_count` 不符）：期望返回 `WSJT_E_CONFIG`/`WSJT_E_SHAPE`，**不崩溃**。→ Task 4。
3. **库文件是旧 ABI（无 `decode_paths`）或符号缺失**：期望 `CoreBinding` 启动即拒绝（`AbiCompatibilityError`），**不静默走错路径**。→ Task 5。
4. **升级决策落在预算边缘**：`remaining` 恰好在 `deep_lower_bound_s` 附近抖动时，期望决策稳定（同一输入同一结果），且**宁可不升级也不迟到**。→ Task 10。
5. **解码条数超过 `RESULT_CAPACITY`（256）**：期望 `overflow` 置位而非静默截断。→ Task 4。

---

# Phase P1 — 原生快路径 + 直连

## Task 1: vendor ft8_lib 源码子集 + 溯源

**Files:**
- Create: `ft8/dsp/vendor-ft8lib/README.md`
- Create: `ft8/dsp/vendor-ft8lib/ft8/{constants,crc,decode,encode,ldpc,message,text}.{c,h}`、`ft8/debug.h`
- Create: `ft8/dsp/vendor-ft8lib/common/{monitor,common}.{c,h}`
- Create: `ft8/dsp/vendor-ft8lib/fft/{kiss_fft,kiss_fftr}.{c,h}`、`fft/_kiss_fft_guts.h`
- Create: `tools/ft8/vendor_ft8lib.sh`
- Create: `ft8/dsp/vendor-ft8lib.sha256`
- Test: `tests/ft8/dsp/test_vendor_ft8lib_policy.py`

**Interfaces:**
- Consumes: 无（首个 Task）。
- Produces: 磁盘上的 vendor 树 `ft8/dsp/vendor-ft8lib/`，供 Task 2 的 CMake include。

- [ ] **Step 1: 写失败测试**

```python
# tests/ft8/dsp/test_vendor_ft8lib_policy.py
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).parents[3]
VENDOR = ROOT / "ft8" / "dsp" / "vendor-ft8lib"
MANIFEST = ROOT / "ft8" / "dsp" / "vendor-ft8lib.sha256"

# 快路径只需要这些翻译单元；不 vendor audio.c / wave.c（设备/文件 IO）。
REQUIRED_SOURCES = {
    "ft8/constants.c", "ft8/crc.c", "ft8/decode.c", "ft8/encode.c",
    "ft8/ldpc.c", "ft8/message.c", "ft8/text.c",
    "common/monitor.c",
    "fft/kiss_fft.c", "fft/kiss_fftr.c",
}


def test_required_sources_are_vendored() -> None:
    present = {
        str(p.relative_to(VENDOR))
        for p in VENDOR.rglob("*.c")
    }
    assert REQUIRED_SOURCES <= present, REQUIRED_SOURCES - present


def test_manifest_matches_disk() -> None:
    recorded = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        recorded[rel] = digest
    for rel, digest in recorded.items():
        actual = hashlib.sha256((VENDOR / rel).read_bytes()).hexdigest()
        assert actual == digest, rel


def test_readme_records_provenance() -> None:
    text = (VENDOR / "README.md").read_text(encoding="utf-8")
    assert "github.com/kgoba/ft8_lib" in text
    assert "commit" in text.lower()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.ft8.dsp.test_vendor_ft8lib_policy -v`
Expected: FAIL（`FileNotFoundError` / 断言集合非空差）

- [ ] **Step 3: 写 vendor 脚本并落盘**

```bash
# tools/ft8/vendor_ft8lib.sh
#!/usr/bin/env bash
# 从钉住的 ft8_lib 检出复制快路径所需的最小 C 子集到 ft8/dsp/vendor-ft8lib/，
# 并生成 SHA-256 清单。用法: tools/ft8/vendor_ft8lib.sh <上游检出目录>
set -euo pipefail
UPSTREAM="${1:?usage: vendor_ft8lib.sh <ft8_lib checkout>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="$REPO_ROOT/ft8/dsp/vendor-ft8lib"

mkdir -p "$DEST/ft8" "$DEST/common" "$DEST/fft"
cp "$UPSTREAM"/ft8/{constants,crc,decode,encode,ldpc,message,text}.c "$DEST/ft8/"
cp "$UPSTREAM"/ft8/{constants,crc,decode,encode,ldpc,message,text}.h "$DEST/ft8/"
cp "$UPSTREAM"/ft8/debug.h "$DEST/ft8/"
cp "$UPSTREAM"/common/monitor.c "$DEST/common/"
cp "$UPSTREAM"/common/{monitor,common}.h "$DEST/common/"   # 注意：common 只有 .h，没有 .c
cp "$UPSTREAM"/fft/{kiss_fft,kiss_fftr}.c "$DEST/fft/"
cp "$UPSTREAM"/fft/{kiss_fft,kiss_fftr}.h "$DEST/fft/"
cp "$UPSTREAM"/fft/_kiss_fft_guts.h "$DEST/fft/"

cd "$DEST"
find . -type f \( -name '*.c' -o -name '*.h' \) | sed 's|^\./||' | sort \
  | xargs shasum -a 256 > "$DEST.sha256"
echo "wrote $DEST and $DEST.sha256"
```

运行：
```bash
chmod +x tools/ft8/vendor_ft8lib.sh
tools/ft8/vendor_ft8lib.sh /Users/cheenle/HAM/ft8_lib   # 上游检出，HEAD 9fec6ca
```

`ft8/dsp/vendor-ft8lib/README.md` 内容：
```markdown
# vendor-ft8lib — 快路径解码器（ft8_lib 的最小 C 子集）

- **上游**：https://github.com/kgoba/ft8_lib
- **钉住的 commit**：9fec6ca（`--depth 50` 检出，快路径仅依赖该 commit 的 ft8/、common/、fft/）
- **许可证**：MIT（见上游 LICENSE；本目录为原样复制，未做本地修改）
- **派生方式**：`tools/ft8/vendor_ft8lib.sh <checkout>` 复制并生成 `../vendor-ft8lib.sha256`
- **为什么是这些文件**：只需要「PCM → 瀑布 → 候选 → LDPC → 文本」这条链；
  `common/audio.c`、`common/wave.c`、`demo/`、`test/` 与本集成无关，故不 vendor。
- **本地修改**：无。任何需要改上游的地方必须走 patch 并在本节记录。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.ft8.dsp.test_vendor_ft8lib_policy -v`
Expected: PASS（3 个用例）

- [ ] **Step 5: 提交**

```bash
git add ft8/dsp/vendor-ft8lib ft8/dsp/vendor-ft8lib.sha256 tools/ft8/vendor_ft8lib.sh tests/ft8/dsp/test_vendor_ft8lib_policy.py
git commit -m "feat(ft8): vendor ft8_lib minimal C subset for the fast decode path"
```

---

## Task 2: 把 ft8_lib 编进 libwsjt_core

**Files:**
- Create: `ft8/dsp/cmake/ft8lib.cmake`
- Modify: `ft8/dsp/CMakeLists.txt:19-48`

**Interfaces:**
- Consumes: `ft8/dsp/vendor-ft8lib/`（Task 1）。
- Produces: `FT8LIB_SOURCES` 变量；`libwsjt_core` 的三平台产物里出现 ft8_lib 的目标文件。

- [ ] **Step 1: 写失败测试**

```python
# tests/ft8/dsp/test_ft8lib_cmake_wiring.py
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[3]


def test_ft8lib_cmake_included_and_sourced() -> None:
    root = (ROOT / "ft8" / "dsp" / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "cmake/ft8lib.cmake" in root
    assert "${FT8LIB_SOURCES}" in root.split("add_library(wsjt_core")[1].split(")")[0]

    module = (ROOT / "ft8" / "dsp" / "cmake" / "ft8lib.cmake").read_text(encoding="utf-8")
    assert "set(FT8LIB_SOURCES" in module
    for needed in ("ft8/decode.c", "common/monitor.c", "fft/kiss_fftr.c"):
        assert needed in module


def test_ft8lib_include_dir_present() -> None:
    root = (ROOT / "ft8" / "dsp" / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "vendor-ft8lib" in root
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.ft8.dsp.test_ft8lib_cmake_wiring -v`
Expected: FAIL（找不到 `cmake/ft8lib.cmake`，`assert "cmake/ft8lib.cmake" in root`）

- [ ] **Step 3: 写 cmake 模块并接线**

```cmake
# ft8/dsp/cmake/ft8lib.cmake
# ft8_lib（kiss-fft 版 FT8 解码器）的最小 C 子集，供 wsjt_ft8_decode_fast 使用。
# 源列表与 ft8/dsp/vendor-ft8lib.sha256 一一对应；新增文件必须同时更新两者。
set(FT8LIB_LIB "${CMAKE_CURRENT_SOURCE_DIR}/vendor-ft8lib")
set(FT8LIB_SOURCES
    ${FT8LIB_LIB}/ft8/constants.c
    ${FT8LIB_LIB}/ft8/crc.c
    ${FT8LIB_LIB}/ft8/decode.c
    ${FT8LIB_LIB}/ft8/encode.c
    ${FT8LIB_LIB}/ft8/ldpc.c
    ${FT8LIB_LIB}/ft8/message.c
    ${FT8LIB_LIB}/ft8/text.c
    ${FT8LIB_LIB}/common/monitor.c
    ${FT8LIB_LIB}/fft/kiss_fft.c
    ${FT8LIB_LIB}/fft/kiss_fftr.c
)
```

`ft8/dsp/CMakeLists.txt`：在 `include(cmake/improved-ft8.cmake)`（第 20 行）后加
```cmake
include(cmake/ft8lib.cmake)
```
在 `add_library(wsjt_core SHARED ...)` 的源列表里（`${IMPROVED_FT8_SOURCES}` 之后）加
```cmake
    ${FT8LIB_SOURCES}
```
在 `target_include_directories(wsjt_core PRIVATE ...)` 块里加
```cmake
    ${FT8LIB_LIB}
```
注意：`ft8_lib` 的 C 用 `#include <ft8/decode.h>` 形式，所以 include 根是
`${FT8LIB_LIB}`（其下有 `ft8/`、`common/`、`fft/` 子目录），**不要**加 `${FT8LIB_LIB}/ft8`。

**编译开关：一律不加（spike 结论）。** 2026-10-13 A53 spike 实测（同一输入、各 7 次取中位）：
`-O3` 基线 153 ms；`-ffast-math` 149 ms（−2.6%）；`-mcpu=cortex-a53 -mtune=cortex-a53`
**162 ms（反而慢 6%）**——objdump 显示三者的向量化完全相同，慢的是 A53 的顺序调度；
`-march=armv8-a+simd` 与基线相同（aarch64 早已默认 NEON）。因此：

- **不要**加 `-mcpu`/`-mtune=cortex-a53`：实测负收益。
- **不要**加 `-ffast-math`：为 4 ms 给一个解码器放松 FP 语义，性价比与风险都不划算
  （spike 只证明了它在合成强信号语料上不改变任何一条解码，**没有**证明弱信号下安全）。
- 若日后确实要试，必须 `set_source_files_properties(${FT8LIB_SOURCES} PROPERTIES
  COMPILE_OPTIONS -ffast-math)` **只作用于这 10 个 C 文件**，绝不能加到
  `wsjt_core` 目标级——那会波及 Fortran 的 WSJT-X 深度路径。

- [ ] **Step 4: 跑测试并构建**

Run:
```bash
python -m unittest tests.ft8.dsp.test_ft8lib_cmake_wiring -v
bash ft8/dsp/build.sh            # 或仓库既有的构建入口
```
Expected: 测试 PASS；构建成功产出 `ft8/dsp/build/libwsjt_core.dylib`（macOS）。

- [ ] **Step 5: 提交**

```bash
git add ft8/dsp/cmake/ft8lib.cmake ft8/dsp/CMakeLists.txt tests/ft8/dsp/test_ft8lib_cmake_wiring.py
git commit -m "build(ft8): compile the ft8_lib fast path into libwsjt_core"
```

---

## Task 3: ABI 扩展 —— decode_paths + 导出符号

**Files:**
- Modify: `ft8/dsp/wsjt_core.h:11,39-52,107-114`
- Modify: `ft8/dsp/wsjt_types.f90:6,29-42`
- Modify: `ft8/dsp/wsjt_core_shim.f90:71-74`
- Modify: `ft8/dsp/cmake/wsjt_core.exports.map.in:1-7`
- Modify: `tests/ft8/dsp/test_cmake_export_map.py:9-14`
- Modify: `tests/ft8/dsp/abi_smoke.c`（能力串里加 `decode_paths`）
- Modify: `tests/ft8/dsp/test_abi_smoke.py:21-30`（`EXPECTED_CAPABILITIES` + `EXPECTED_DYNAMIC_EXPORTS`）
- Create: `ft8/dsp/wsjt_fast.c`（Step 4 先放最小桩，Task 4 换成真实现）
- Test: `tests/ft8/dsp/test_abi_smoke.py`（扩展）

> **`test_abi_smoke.py` / `abi_smoke.c` 里有三处会被这次 ABI 改动打破，一处都不能漏：**
> 1. `tests/ft8/dsp/abi_smoke.c` —— 打印能力串（`printf`）。
> 2. `test_abi_smoke.py:21-22` `EXPECTED_CAPABILITIES` —— 把该串**逐字节**钉死为
>    `"abi=1 results=256 rx=12000/180000 tx=48000/606720 profiles=0x1f"`。
> 3. `test_abi_smoke.py:25-30` `EXPECTED_DYNAMIC_EXPORTS` —— 导出符号集合
>    （现在 4 个），必须加上 `wsjt_ft8_decode_fast`。
>
> 第 1、2 处一起改：`abi=1` → `abi=2`，并**在末尾追加** `decode_paths`——
> `abi_smoke.c:48` 的 printf 格式串尾加 `" decode_paths=0x%x"`、实参表尾加 `info.decode_paths`；
> `EXPECTED_CAPABILITIES` 尾加 `" decode_paths=0x7"`。**位置必须在末尾**：两个文件各改一处，
> 顺序只要不一致就是逐字节不等。改完后完整期望串应为：
> `"abi=2 results=256 rx=12000/180000 tx=48000/606720 profiles=0x1f decode_paths=0x7"`。
> 不动这三处，本 Task 的 Step 4 会直接失败。
>
> **`decode_paths` 是「替换」`reserved`，不是「追加」——这条不能改。**
> `abi_smoke.c:6` 有一条编译期断言 `_Static_assert(sizeof(struct wsjt_abi_info) == 48, ...)`，
> 而 `wsjt_core_shim.f90:65` 的 `info%struct_size = c_sizeof(info_probe)` 是跟着结构体实际
> 大小走的。若在 `max_cycles` 后**追加**第 13 个 int32，`sizeof` 变 52，`abi_smoke.c` **编译
> 都过不去**，Step 4 必然失败；同时 `wsjt_core.h:51` / `wsjt_types.f90:41` / `binding.py` 三处
> 镜像也得跟着改大小。
> 而 `reserved` 是**只写不读**的死字段（全仓唯一引用是 `wsjt_core_shim.f90:74` 的
> `info%reserved = 0`，没有任何读取者），它存在的意义就是给这种增长留位。所以：**占用
> `reserved` 的位置**，`sizeof` 保持 48，`abi_smoke.c` 的 `_Static_assert` **一行都不用动**
> （`abi_smoke.c` 只改 `printf`）。不兼容性由 `abi_version = 2` 声明，这正是版本号的作用。
> 因此 Step 1 的 `_AbiInfo` 是 **12 个字段**（`max_cycles` 之后直接是 `decode_paths`，
> 没有 `reserved`），并断言 `ctypes.sizeof(_AbiInfo) == 48`。

**Interfaces:**
- Consumes: 无（纯 ABI 声明）。
- Produces: `wsjt_abi_info.decode_paths`（位掩码，bit0 standard / bit1 improved / bit2 fast）；`wsjt_ft8_decode_fast` 的 C 原型（Task 4 实现）；ABI 版本 2。

- [ ] **Step 1: 写失败测试**（在 `tests/ft8/dsp/test_abi_smoke.py` 追加）

用该文件**自己的** `built_library` fixture，**不要**用 conftest 的 `raw_library`：本文件现在
根本不引用 `raw_library`，而那个 fixture 会另起一次完整的 cmake configure+build（session 级、
建到 tmp 目录里），等于在同一个文件里把构建跑两遍。`built_library` 正是本文件另两个用例已经在
检查的那个产物，模型一致；而且它按当前源码构建，所以 Step 2 一定先失败（能看到 `abi_version == 1`）。

```python
import ctypes


class _AbiInfo(ctypes.Structure):
    # 12 个字段 —— decode_paths 占用原 reserved 的位置，sizeof 保持 48。
    _fields_ = [
        ("abi_version", ctypes.c_int32), ("struct_size", ctypes.c_int32),
        ("result_size", ctypes.c_int32), ("result_capacity", ctypes.c_int32),
        ("ft8_rx_rate", ctypes.c_int32), ("ft8_rx_samples", ctypes.c_int32),
        ("ft8_tx_rate", ctypes.c_int32), ("ft8_tx_samples", ctypes.c_int32),
        ("improved_profiles", ctypes.c_int32), ("max_threads", ctypes.c_int32),
        ("max_cycles", ctypes.c_int32), ("decode_paths", ctypes.c_int32),
    ]


def test_abi_reports_fast_path_and_version_two(built_library) -> None:
    library = ctypes.CDLL(str(built_library))
    library.wsjt_get_abi_info.argtypes = [ctypes.POINTER(_AbiInfo)]
    library.wsjt_get_abi_info.restype = ctypes.c_int32
    info = _AbiInfo()
    assert library.wsjt_get_abi_info(ctypes.byref(info)) == 0
    assert info.abi_version == 2
    assert info.decode_paths & 0b100, "fast path bit must be set"
    assert ctypes.sizeof(_AbiInfo) == 48, "reserved 被替换而非追加，大小必须不变"
    assert ctypes.sizeof(_AbiInfo) == info.struct_size
    assert hasattr(library, "wsjt_ft8_decode_fast")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/dsp/test_abi_smoke.py -v`
Expected: FAIL（`abi_version == 1`，且 `decode_paths` 读到旧 `reserved` 的 0）

- [ ] **Step 3: 同步 ABI（`decode_paths` 占用 `reserved` 的位置，不新增字段）**

`ft8/dsp/wsjt_core.h`：
```c
    WSJT_ABI_VERSION = 2,   /* was 1: added decode_paths + wsjt_ft8_decode_fast */
```
`wsjt_abi_info` 的**最后一字段改名**（不是插入新字段——见上方警示框，追加会让
`abi_smoke.c:6` 的 `_Static_assert(sizeof == 48)` 编译失败）：
```c
    int32_t decode_paths;   /* bit0 standard, bit1 improved, bit2 fast;
                               replaces the unused `reserved` slot, size stays 48 */
```
`wsjt_flags` 枚举追加一位（现为 AP=1 / LOW_THRESHOLD=2 / WIDE_DX=4 / HIDE_DUPES=8，
下一个空位是 16）：
```c
    WSJT_FLAG_SNR_PROVISIONAL = 16   /* snr 字段不是 dB，见 wsjt_fast.c */
```
若 `ft8/dsp/wsjt_types.f90` 里也声明了 flags 常量，同步加一个同名 parameter。
文件末尾追加原型：
```c
int32_t wsjt_ft8_decode_fast(
    const int16_t *samples,
    const struct wsjt_decode_config *config,
    int64_t slot_id,
    struct wsjt_decode_result *results,
    int32_t capacity,
    int32_t *count,
    int32_t *overflow);
```

`ft8/dsp/wsjt_types.f90`：`WSJT_ABI_VERSION = 2_c_int32_t`；`wsjt_abi_info` 的
`reserved` 字段**改名**（`wsjt_types.f90:41`）：
```fortran
    integer(c_int32_t) :: decode_paths
```
位置同 C（末字段），`sizeof` 保持 48。

`ft8/dsp/wsjt_core_shim.f90`（`wsjt_get_abi_info` 里，`max_cycles` 赋值之后；
把 `wsjt_core_shim.f90:74` 的 `info%reserved = 0_c_int32_t` **换成**）：
```fortran
    info%decode_paths = int(z'07', c_int32_t)   /* standard | improved | fast */
```

`ft8/dsp/cmake/wsjt_core.exports.map.in`：在 `wsjt_ft8_decode_improved;` 后加
```
        wsjt_ft8_decode_fast;
```

`tests/ft8/dsp/test_cmake_export_map.py` 的 `PRODUCTION_EXPORTS` 加 `"wsjt_ft8_decode_fast;"`。

- [ ] **Step 4: 跑测试确认通过**（先给一个临时桩实现让链接通过）

在 `ft8/dsp/wsjt_fast.c` 先写最小桩（Task 4 会替换为真实现）：
```c
#include "wsjt_core.h"
int32_t wsjt_ft8_decode_fast(const int16_t *samples,
                             const struct wsjt_decode_config *config,
                             int64_t slot_id,
                             struct wsjt_decode_result *results,
                             int32_t capacity, int32_t *count, int32_t *overflow)
{
    (void)samples; (void)config; (void)slot_id; (void)results;
    (void)capacity;
    if (!count || !overflow) return WSJT_E_NULL;
    *count = 0; *overflow = 0;
    return WSJT_E_INTERNAL;   /* not yet implemented */
}
```
在 `ft8/dsp/CMakeLists.txt` 的 `add_library` 源列表里加 `wsjt_fast.c`。

Run:
```bash
.venv/bin/python -m pytest tests/ft8/dsp/test_cmake_export_map.py tests/ft8/dsp/test_abi_smoke.py -v
```
Expected: PASS。若 `test_cmake_export_map` 报 `cmake` 找不到，用 `/opt/local/bin/cmake`
（macOS 上 cmake 不在 PATH 的默认位置，`conftest` 之外没有兜底）。

**还要确认 macOS 这边是 5 个导出**（这一步 Task 2 修不了，因为新符号此刻才出现）：
```bash
nm -gU ft8/dsp/build/libwsjt_core.dylib | awk '{print $NF}' | sort
```
Expected：恰好 `_wsjt_ft8_decode_fast`、`_wsjt_ft8_decode_improved`、`_wsjt_ft8_decode_standard`、
`_wsjt_ft8_encode`、`_wsjt_get_abi_info` 五个。若多出别的，说明 Task 2 往 macOS 的
**拒绝清单**（`ft8/dsp/CMakeLists.txt` 的 `if(APPLE)` 块）里加的模式误伤了新符号或漏了新符号 ——
拒绝清单不会自动隐藏新符号，这是它与 Linux 允许清单（`wsjt_core.exports.map.in`）的关键差别。

- [ ] **Step 5: 提交**

```bash
git add ft8/dsp/wsjt_core.h ft8/dsp/wsjt_types.f90 ft8/dsp/wsjt_core_shim.f90 ft8/dsp/wsjt_fast.c ft8/dsp/cmake/wsjt_core.exports.map.in ft8/dsp/CMakeLists.txt tests/ft8/dsp/test_cmake_export_map.py tests/ft8/dsp/test_abi_smoke.py
git commit -m "feat(ft8): extend the DSP ABI with decode_paths and the fast entry point"
```

> **留给 Task 5 的接口变更**：`ft8/core/binding.py:64` 的 `("reserved", ctypes.c_int32)`
> 现在是 `decode_paths` —— 本次改名后，Task 5 必须把这一行一起改掉，否则 Python 侧读到的
> 仍是旧的字段名（`_AbiInfo` 的字节布局相同，所以不会报错，只会读不到）。

---

## Task 4: 实现 wsjt_ft8_decode_fast

**Files:**
- Modify: `ft8/dsp/wsjt_fast.c`（替换 Task 3 的桩）
- Test: `tests/ft8/dsp/test_ft8_fast_decode.py`

> **`_Config` / `_Result` 的字段顺序以 `ft8/dsp/wsjt_core.h` 为准，别照下面重打。**
> 下面是**示意**；`wsjt_decode_config` 必须正好 100 字节（`wsjt_core_shim.f90:57`
> 有 `c_sizeof(config_probe) == 100` 断言），顺序错了 `struct_size` 校验就会失败。
> 两个更稳的做法，任选其一：
> 1. 打开 `ft8/dsp/wsjt_core.h` 照着抄字段顺序；
> 2. 直接从 `ft8/core/binding.py` `import` 它已有的 `_DecodeConfig` / `_DecodeResult`
>    （它是同一份 ABI 的权威 ctypes 镜像，不会漂移）。
> 无论哪种，写完先断言 `ctypes.sizeof(_Config) == 100` 再往下做。

**Interfaces:**
- Consumes: `wsjt_abi_info`/`wsjt_decode_config`/`wsjt_decode_result`（Task 3）；vendor ft8_lib（Task 2）。
- Produces: 可用的 `wsjt_ft8_decode_fast`：读同一 12 kHz int16 时隙，写同一 `wsjt_decode_result` 数组与 `count/overflow`，返回 `wsjt_status`。

- [ ] **Step 1: 写失败测试**

```python
# tests/ft8/dsp/test_ft8_fast_decode.py
from __future__ import annotations

import ctypes
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parents[3]


class _Config(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_int32), ("sample_rate", ctypes.c_int32),
        ("sample_count", ctypes.c_int32), ("profile", ctypes.c_int32),
        ("threads", ctypes.c_int32), ("cycles", ctypes.c_int32),
        ("sensitivity", ctypes.c_int32), ("flags", ctypes.c_int32),
        ("qso_progress", ctypes.c_int32), ("rx_frequency", ctypes.c_int32),
        ("tx_frequency", ctypes.c_int32), ("low_frequency", ctypes.c_int32),
        ("high_frequency", ctypes.c_int32), ("ap_width", ctypes.c_int32),
        ("utc_hhmmss", ctypes.c_int32), ("reserved", ctypes.c_int32),
        ("my_call", ctypes.c_char * 13), ("dx_call", ctypes.c_char * 13),
        ("dx_grid", ctypes.c_char * 7), ("padding", ctypes.c_char * 3),
    ]


class _Result(ctypes.Structure):
    _fields_ = [
        ("slot_id", ctypes.c_int64), ("sync", ctypes.c_float),
        ("dt", ctypes.c_float), ("frequency", ctypes.c_float),
        ("quality", ctypes.c_float), ("snr", ctypes.c_int32),
        ("ap_type", ctypes.c_int32), ("flags", ctypes.c_int32),
        ("reserved", ctypes.c_int32), ("text", ctypes.c_char * 38),
        ("padding", ctypes.c_char * 2),
    ]


def _call_fast(library, samples: np.ndarray, capacity: int = 256):
    cfg = _Config()
    cfg.struct_size = ctypes.sizeof(_Config)
    cfg.sample_rate = 12_000
    cfg.sample_count = 180_000
    cfg.low_frequency = 200
    cfg.high_frequency = 3_000
    results = (_Result * capacity)()
    count = ctypes.c_int32()
    overflow = ctypes.c_int32()
    status = library.wsjt_ft8_decode_fast(
        samples.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        ctypes.byref(cfg), 1, results, capacity,
        ctypes.byref(count), ctypes.byref(overflow),
    )
    return status, results, count.value, overflow.value


def test_fast_path_decodes_the_busy_slot(raw_library, busy_slot) -> None:
    status, results, count, overflow = _call_fast(raw_library, busy_slot)
    assert status == 0
    assert count >= FAST_FLOOR, f"expected the 5 CQ signals, got {count}"
    texts = {results[i].text.decode().rstrip("\x00").strip() for i in range(count)}
    assert any("CQ" in t for t in texts)


def test_fast_path_reports_no_overflow_at_full_capacity(raw_library, busy_slot) -> None:
    """`*overflow` 在快路径上恒为 0：候选上限 140 < 结果容量 256 <= capacity。"""
    status, _results, _count, overflow = _call_fast(raw_library, busy_slot)
    assert status == 0
    assert overflow == 0


def test_fast_path_rejects_inverted_frequency_window(raw_library, busy_slot) -> None:
    cfg = _Config()
    cfg.struct_size = ctypes.sizeof(_Config)
    cfg.sample_rate = 12_000
    cfg.sample_count = 180_000
    cfg.low_frequency = 3_000
    cfg.high_frequency = 200          # inverted
    results = (_Result * 256)()
    count = ctypes.c_int32()
    overflow = ctypes.c_int32()
    status = raw_library.wsjt_ft8_decode_fast(
        busy_slot.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        ctypes.byref(cfg), 1, results, 256,
        ctypes.byref(count), ctypes.byref(overflow),
    )
    assert status == 5, f"expected WSJT_E_CONFIG, got {status}"


def test_fast_path_rejects_capacity_below_result_capacity(raw_library, busy_slot) -> None:
    """注意：这是**前置条件**检查（capacity < WSJT_RESULT_CAPACITY），
    不是 overflow 路径 —— 见 `test_fast_path_reports_no_overflow_at_full_capacity`。"""
    status, _results, _count, _overflow = _call_fast(raw_library, busy_slot, capacity=1)
    assert status == 6, f"expected WSJT_E_CAPACITY for capacity < 256, got {status}"


def test_fast_path_keeps_upstream_default_knobs() -> None:
    """旋钮必须停在上游默认值 —— spike 证明任何下调都是拿弱信号换时间。"""
    source = (ROOT / "ft8" / "dsp" / "wsjt_fast.c").read_text(encoding="utf-8")
    assert "#define FAST_MIN_SCORE 10" in source
    assert "#define FAST_LDPC_ITERATIONS 25" in source
    assert "#define FAST_TIME_OSR 2" in source
    assert "#define FAST_FREQ_OSR 2" in source
    # 候选搜索必须真的传 min_score，而不是 0（score=0 会让候选饱和到上限，
    # 实测把 154 ms 翻倍成 281 ms —— 快路径反而比深路径慢）
    assert "cand, FAST_MIN_SCORE)" in source
    assert "cand, 0)" not in source
```

`FAST_FLOOR` 是**量出来的，不是猜的** —— 见 Step 3 之后的「先量后钉」。`busy_slot` 带
σ=2000 的噪声，它到底能解出几条，只有跑一次才知道；先按下面写一个常量占位：

```python
# Step 3 之后用实测值替换 —— 不是 5，也不许写成 ">= 3" 这种拍脑袋的下界。
FAST_FLOOR = 1   # MEASURE_ME
```

两个 fixture 的来源（已核实，别照字面搬）：
- `raw_library` **已经在** `tests/ft8/dsp/conftest.py:87` 里了（`session` 作用域）—— 不用动。
- `busy_slot` 定义在 `tests/ft8/dsp/test_i9_benchmark.py:80-81`（`session` 作用域）：
  `wsjt_ft8_encode` 合成 5 个 CQ 叠加后，**乘 4800 并叠加 σ=2000 的噪声**，
  再 clip 成 int16。它和它依赖的 `_SIGNALS` 表（`test_i9_benchmark.py:45`）一起
  **搬进 `conftest.py`**，不要留两份。
  提醒：它带噪声，不是干净强信号——断言条数时别按"5 个信号就该解出 5 条"想当然。

  **怎么搬（本仓没有 `from conftest import …` 的先例，别开这个头）：**
  - `busy_slot` 原样成为 `conftest.py` 里的 fixture（`resample_poly` 与 `c` 在
    `conftest.py:15,13` 已经导入，不需要新增 import，**也不要**再加一行
    `os.environ.setdefault("OMP_STACKSIZE", "10M")` —— 它已经在 `conftest.py:9` 了）。
  - `_SIGNALS` 留在 `conftest.py` 作模块级常量，并**额外**暴露一个无依赖的
    session fixture 给它：
    ```python
    @pytest.fixture(scope="session")
    def signals() -> list[tuple[str, float]]:
        return _SIGNALS
    ```
  - `test_i9_benchmark.py`：删掉本地的 `_SIGNALS` 与 `busy_slot`，把
    `test_i9_latency_matrix(` 的签名加上 `signals: list[tuple[str, float]],`
    参数，函数体里两处 `_SIGNALS`（`:188` 的 `for s in _SIGNALS`、`:227` 的
    `len(_SIGNALS)`）改用 `signals`。两处都在该函数体内，fixture 参数够用。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/dsp/test_ft8_fast_decode.py -v`
Expected: FAIL（返回 `WSJT_E_INTERNAL`(8)，因为 Step 3 前是 Task 3 留下的桩）

- [ ] **Step 3: 写真实现**（替换 `ft8/dsp/wsjt_fast.c` 全文）

```c
/* ft8/dsp/wsjt_fast.c
 * 快路径：用 ft8_lib（kiss-fft）解码一个 12 kHz int16 时隙，
 * 输出与 standard/improved 完全相同的 ABI 结构。
 * 无 AP、无 OSD、单趟 —— 这是「快」的来源，也是它需要级联兜底的原因。
 */
#include "wsjt_core.h"

#include <math.h>
#include <stdint.h>
#include <string.h>

#include <common/monitor.h>
#include <ft8/constants.h>
#include <ft8/decode.h>
#include <ft8/message.h>

/* 全部取上游 demo/decode_ft8.c 的默认值 —— 这是 2026-10-13 A53 spike 实测过的
 * 组合（中位 153 ms、5/5）。spike 的结论是「编译开关基本无用、算法旋钮全都
 * 拿弱信号鲁棒性换时间」，而**弱信号正是级联要兜的那一段**：
 *   - kMin_score 10→5  : 候选饱和到上限 → 154→281 ms（翻倍，是反向优化）
 *   - kMin_score 10→20 : 154→85 ms，但强信号输入掩盖了它剪掉的候选
 *   - kLDPC_iterations 25→12: 153→117 ms，同样只在强信号下才 5/5
 * 所以快路径**刻意不降任何旋钮**：它就是上游 ft8_lib，质量可比、可回归
 * （Task 7 钉住）。要更快是级联的职责，不是把第一遍做残。 */
#define FAST_MAX_CANDIDATES 140
#define FAST_MIN_SCORE 10
#define FAST_LDPC_ITERATIONS 25
#define FAST_TIME_OSR 2
#define FAST_FREQ_OSR 2

/* 快路径没有跨时隙的呼号哈希表：查不到就保留 <HASH> 形式，
   深度路径升级时再还原。 */
static bool fast_lookup_hash(ftx_callsign_hash_type_t type, uint32_t hash,
                             char *callsign)
{
    (void)type;
    (void)hash;
    callsign[0] = '\0';
    return false;
}

static void fast_save_hash(const char *callsign, uint32_t n22)
{
    (void)callsign;
    (void)n22;
}

static void copy_text(const char *src, char *dst, size_t dst_len)
{
    size_t i = 0;
    while (i + 1 < dst_len && src[i] != '\0') {
        dst[i] = src[i];
        ++i;
    }
    dst[i] = '\0';
}

int32_t wsjt_ft8_decode_fast(const int16_t *samples,
                             const struct wsjt_decode_config *config,
                             int64_t slot_id,
                             struct wsjt_decode_result *results,
                             int32_t capacity,
                             int32_t *count,
                             int32_t *overflow)
{
    if (!samples || !config || !results || !count || !overflow)
        return WSJT_E_NULL;
    *count = 0;
    *overflow = 0;

    if (config->struct_size != (int32_t)sizeof(struct wsjt_decode_config))
        return WSJT_E_ABI;
    if (config->sample_rate != WSJT_FT8_RX_RATE)
        return WSJT_E_RATE;
    if (config->sample_count != WSJT_FT8_RX_SAMPLES)
        return WSJT_E_SHAPE;
    if (capacity < WSJT_RESULT_CAPACITY)
        return WSJT_E_CAPACITY;
    if (config->low_frequency < 100 || config->high_frequency > 4910 ||
        config->high_frequency <= config->low_frequency)
        return WSJT_E_CONFIG;

    monitor_config_t mon_cfg = {
        .f_min = (float)config->low_frequency,
        .f_max = (float)config->high_frequency,
        .sample_rate = WSJT_FT8_RX_RATE,
        .time_osr = FAST_TIME_OSR,
        .freq_osr = FAST_FREQ_OSR,
        .protocol = FTX_PROTOCOL_FT8,
    };
    monitor_t mon;
    monitor_init(&mon, &mon_cfg);

    /* 逐块转换，**不要**为整个时隙开 float[180000]（= 720 KB）：sample_count
       是定死的 180000，所以那行一旦写成栈数组，栈帧就固定 720 KB。上游 demo 敢
       在 main() 的 8 MB 栈上开整槽 VLA（demo/decode_ft8.c:303），这里不行 ——
       本函数在共享库里被 ctypes 调用，可能落在 512 KB 的次线程上，而本仓对这条
       有明确契约：FT8 路径上栈不足的表现是段错误，不是异常
       （AGENTS.md FT8 铁律 / OMP_STACKSIZE 那条）。
       monitor_process() 每次恰好消耗 mon.block_size 个样本
       （time_osr * subblock_size，见 common/monitor.c:138-150），所以一块
       转换缓冲足够。FT8 @12 kHz 的 block_size 是 1920，2048 够；下面的守卫让
       几何一旦变化就报错返回，而不是再次踩栈。 */
    float block[2048];
    if (mon.block_size <= 0 ||
        (size_t)mon.block_size > sizeof(block) / sizeof(block[0])) {
        monitor_free(&mon);
        return WSJT_E_INTERNAL;
    }

    for (int pos = 0; pos + mon.block_size <= WSJT_FT8_RX_SAMPLES;
         pos += mon.block_size) {
        for (int i = 0; i < mon.block_size; ++i)
            block[i] = (float)samples[pos + i] * (1.0f / 32768.0f);
        monitor_process(&mon, block);
    }

    ftx_candidate_t cand[FAST_MAX_CANDIDATES];
    int num_cand =
        ftx_find_candidates(&mon.wf, FAST_MAX_CANDIDATES, cand, FAST_MIN_SCORE);

    ftx_callsign_hash_interface_t hash_if = {
        .lookup_hash = fast_lookup_hash,
        .save_hash = fast_save_hash,
    };

    /* 去重用本地表，**不要**借用 wsjt_decode_result 的 reserved/padding：
       FT8 的 payload 是 10 字节，而 padding 只有 2 —— 写进去会溢出结构体、
       踩掉下一个 result。本地表也不进 ABI，改了不影响任何人。 */
    struct {
        uint16_t hash;
        uint8_t payload[sizeof(((ftx_message_t *)0)->payload)];
    } seen[WSJT_RESULT_CAPACITY];

    int written = 0;
    for (int i = 0; i < num_cand; ++i) {
        ftx_message_t message;
        ftx_decode_status_t status;
        if (!ftx_decode_candidate(&mon.wf, &cand[i], FAST_LDPC_ITERATIONS,
                                  &message, &status))
            continue;

        /* 与上游 demo 相同的去重：hash 相等且 payload 逐字节相同才算重复。 */
        bool duplicate = false;
        for (int k = 0; k < written; ++k) {
            if (seen[k].hash == message.hash &&
                memcmp(seen[k].payload, message.payload,
                       sizeof(message.payload)) == 0) {
                duplicate = true;
                break;
            }
        }
        if (duplicate)
            continue;

        char text[FTX_MAX_MESSAGE_LENGTH + 1];
        ftx_message_offsets_t offsets;
        if (ftx_message_decode(&message, &hash_if, text, &offsets) !=
            FTX_MESSAGE_RC_OK)
            continue;

        struct wsjt_decode_result *out = &results[written];
        memset(out, 0, sizeof(*out));
        out->slot_id = slot_id;
        out->frequency = (float)(mon.min_bin + cand[i].freq_offset +
                                 (float)cand[i].freq_sub / mon.wf.freq_osr) /
                         mon.symbol_period;
        out->dt = (float)(cand[i].time_offset +
                          (float)cand[i].time_sub / mon.wf.time_osr) *
                  mon.symbol_period;
        /* 上游**没有**可信 SNR：demo 里 score*0.5 是作者留的 TODO 估计，
           不是 dB。这里如实照抄该估计，并置 WSJT_FLAG_SNR_PROVISIONAL，
           好让调用方知道不能拿它当 dB 用（级联判据就据此跳过峰值判据，
           见 spec D-7 与 Task 8）。 */
        out->snr = (int32_t)(cand[i].score / 2);
        out->flags = WSJT_FLAG_SNR_PROVISIONAL;
        out->ap_type = 0;
        out->quality = 0.0f;
        copy_text(text, out->text, WSJT_TEXT_BYTES);
        seen[written].hash = (uint16_t)message.hash;
        memcpy(seen[written].payload, message.payload,
               sizeof(message.payload));
        ++written;
    }

    monitor_free(&mon);
    *count = written;
    /* 恒为 0，且是**可证明**的：本函数已在前面拒绝了 capacity < 256，
       而每候选最多产出 1 条结果、候选上限 FAST_MAX_CANDIDATES = 140，
       故 written <= 140 <= 256 <= capacity —— 写满的路径不可达。这里不写
       "if (written >= capacity)" 分支（死代码）。若日后把候选上限提到 256
       以上，那条分支必须连测试一起补回来。
       调用方读 *overflow 仍然有意义：它让 ABI 与 standard/improved 保持
       同一形状，级联那边不必分路径处理。 */
    *overflow = 0;
    return WSJT_OK;
}
```

> **注意**：`out->freq` 字段名按 `wsjt_core.h` 的 `wsjt_decode_result` 定义书写——
> 该结构体的字段是 `frequency`，不是 `freq`；实现时以头文件为准（此处保留结构体真名
> `out->frequency`）。
>
> 去重表是**函数内的局部表**，绝不借用 `wsjt_decode_result.reserved` / `padding`：
> FT8 的 payload 是 10 字节而 `padding` 只有 2 —— 写进去就是越过结构体末尾 8 字节、
> 踩掉下一条结果。这是本计划前一版真实写错的 bug，别改回去。
>
> `config->flags` **整个被忽略**：快路径没有 AP、没有 OSD、只有单趟，`WSJT_FLAG_AP` /
> `LOW_THRESHOLD` / `WIDE_DX` / `HIDE_DUPES` 一个都不生效。这是设计（spec 的混合分层：
> 弱信号与 AP 归深路径），不是遗漏 —— 所以代码里要留一行注释写明，让调用方从
> `decode_paths` 位掩码知道该走哪条路，而不是误以为传 `flags` 就能拿到 AP。

- [ ] **Step 3b: 先量后钉 —— 把 `FAST_FLOOR` 换成实测值**

`busy_slot` 是 5 个 CQ 叠加 σ=2000 噪声的 int16，**它到底能解出几条只有跑一次才知道**。
实现完成后先跑一次拿到真实的 `count`，再把 `FAST_FLOOR` 写成那个数（若同一输入多次运行
不稳定，写实测最小值，并在报告里说明波动范围）。
**不许**照字面写成 `5`（那是干净语料的数字），也不许写成 `>= 3` 这类没有依据的下界。
报告里必须给出实测 `count` 及对应的消息文本。

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
bash ft8/dsp/build.sh
.venv/bin/python -m pytest tests/ft8/dsp/test_ft8_fast_decode.py -v
```
Expected: PASS（5 个用例）

- [ ] **Step 5: 提交**

```bash
git add ft8/dsp/wsjt_fast.c tests/ft8/dsp/test_ft8_fast_decode.py tests/ft8/dsp/conftest.py tests/ft8/dsp/test_i9_benchmark.py
git commit -m "feat(ft8): implement wsjt_ft8_decode_fast on top of ft8_lib"
```
（`conftest.py` 与 `test_i9_benchmark.py` 也在本任务里改了 —— `busy_slot`/`_SIGNALS`
搬家。漏掉它们会留下一个未提交的脏工作区，下一个 Task 的 review package 会把它算进
别人的 diff。）

---

## Task 5: Python binding 接 fast path

**Files:**
- Modify: `ft8/core/models.py:29-33`
- Modify: `ft8/core/binding.py:33,51-65,200-215,322-335,361-366`
- Modify: `tests/ft8/core/test_binding.py:30-42,353,366,667-673`（**四处既有内容必须改**：
  模块级 `EXPECTED_ABI`、参数化表的首行与末行、`_AbiInfo` 偏移断言的字段名 —— 见警示框，
  缺一处就整片红）
- Test: `tests/ft8/core/test_binding.py`（扩展）

**Interfaces:**
- Consumes: `wsjt_ft8_decode_fast`（Task 4）、`wsjt_abi_info.decode_paths`（Task 3）。
- Produces: `DecodePath.FAST`；`CoreBinding.decode(..., DecodeConfig(path=DecodePath.FAST), ...)` 可用；旧 ABI 库被拒。

> **`reserved` 在 Task 3 里被 `decode_paths` 顶掉了，不是新增了第 13 个字段。**
> 所以这里每一处都是**改名/替换**，凡是要你「插入」或「在 reserved 之前加」的说法都作废。
> 下面五处**缺一不可**（前三处漏掉任何一处，`tests/ft8/core/test_binding.py` 会**整片**变红，
> 不是个别用例 —— 因为它所有用例都建在 `FakeNative.abi_info()` 返回的那份 dict 上）：
> 1. `_AbiInfo._fields_`（`binding.py:51-65`）：把 `("reserved", ctypes.c_int32)` **换成**
>    `("decode_paths", ctypes.c_int32),`。字段数仍是 12，`ctypes.sizeof(_AbiInfo) == 48`。
> 2. `binding.py:322-335` 的 `_EXPECTED_ABI`：把 `"reserved": 0` **换成**
>    `"decode_paths": _FAST_PATH_MASK,`（`_FAST_PATH_MASK = 0b111`），并把
>    `"abi_version": _ABI_VERSION` 保持引用常量。
> 3. **`test_binding.py:30-42` 的模块级 `EXPECTED_ABI`**（两处 `FakeNative` 在 `:60`/`:101`
>    用它构造假 ABI）：`"abi_version": 1` → `2`，`"reserved": 0` → `"decode_paths": 0b111`。
>    **漏掉的后果**：`_validate_abi`（`binding.py:361-366`）逐个 `_EXPECTED_ABI` 键取
>    `actual.get(field)` 比对，`reserved` 已不在期望表里、`decode_paths` 又拿不到 →
>    `value=None != 0b111` → **每一个用 `FakeNative` 的用例都抛
>    `AbiCompatibilityError: decode_paths mismatch`**。而 `abi_version` 那条更早触发。
> 4. `test_binding.py:353` 的参数化表首行 `("abi_version", 2)` —— 这个「坏值」在
>    `_ABI_VERSION` 升到 2 之后**变成了合法值**，用例会因为「预期抛异常却没抛」而失败。
>    改成 `("abi_version", 1)`。
> 5. `test_binding.py:366` 的末行 `("reserved", 1)` —— 字段已不存在。改成
>    `("decode_paths", 0b010)`（0b010 ≠ 0b111 → 照常抛，且 `match=field` 能匹配到
>    `decode_paths`）。
> 6. `test_binding.py:667-673` 的 `_AbiInfo` 偏移断言里，名字列表含 `"reserved"`、
>    期望 `{"reserved": 44}` —— `getattr(_AbiInfo, "reserved")` 会 AttributeError。
>    改成 `"decode_paths"`，**偏移仍是 44**（是改名不是挪位）。

- [ ] **Step 1: 写失败测试**（在 `tests/ft8/core/test_binding.py` 追加）

```python
def test_fast_path_is_a_valid_decode_path() -> None:
    from ft8.core.models import DecodePath
    assert DecodePath("fast") is DecodePath.FAST


def test_validate_abi_rejects_missing_fast_bit() -> None:
    """掩码里没有 fast 位的库必须被拒 —— 复用该文件既有的 FakeNative 夹具。"""
    from ft8.core.binding import AbiCompatibilityError

    with pytest.raises(AbiCompatibilityError, match="decode_paths"):
        CoreBinding.for_test(FakeNative(abi_overrides={"decode_paths": 0b011}))
```

> **不要**写「断言 `_EXPECTED_ABI["decode_paths"] & 0b100` 且 `_ABI_VERSION == 2`」这种用例：
> 它只是把常量念回给自己听，永远不会失败，等于没有测试。真正的断言只有一条 ——
> **缺 fast 位的 ABI 会被 `CoreBinding` 拒绝**，就是上面那个用例。
> `FakeNative` 是该文件已有的夹具（`CoreBinding.for_test` 走 `_TestAdapter`，不碰 CDLL）；
> 不要另造一个 `_Stale` 类。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/core/test_binding.py -v`
Expected: FAIL（`DecodePath("fast")` 抛 `ValueError`；`match="decode_paths"` 匹配不到）
**同时注意**：Step 3 改完 `_EXPECTED_ABI` 之后，`("reserved", 1)` 那条参数化用例也会开始失败 ——
这就是警示框第 3 点的原因。

- [ ] **Step 3: 实现**

`ft8/core/models.py`：
```python
class DecodePath(StrEnum):
    """Native FT8 decoder implementation selected for one request."""

    FAST = "fast"
    STANDARD = "standard"
    IMPROVED = "improved"
```

`ft8/core/binding.py`：
- `_ABI_VERSION = 2`
- `_FAST_PATH_MASK = 0b111`（standard | improved | fast，与 `wsjt_core_shim.f90` 的 `z'07'` 一致）
- `_AbiInfo._fields_`：把 `("reserved", ctypes.c_int32)` **换成**
  `("decode_paths", ctypes.c_int32),`
- `_EXPECTED_ABI`：把 `"reserved": 0` **换成** `"decode_paths": _FAST_PATH_MASK,`
- `_CtypesAdapter.__init__` 追加 —— **必须包在 try/except 里**：
```python
        # 快路径符号在 ABI 2 才存在。若加载到的是更旧的库，ctypes 抛的是
        # AttributeError，而这一句在 CoreBinding._validate_abi() 之前执行 ——
        # 不包的话，版本错配会以一个来自适配器深处的 AttributeError 冒出去，
        # 而不是文档承诺的 AbiCompatibilityError。
        try:
            library.wsjt_ft8_decode_fast.argtypes = _DECODE_ARGTYPES
            library.wsjt_ft8_decode_fast.restype = ctypes.c_int32
        except AttributeError as exc:
            raise AbiCompatibilityError(
                "decode_paths mismatch: library has no wsjt_ft8_decode_fast"
            ) from exc
```
  （`AbiCompatibilityError` 定义在 `binding.py:145`，在 `_CtypesAdapter`（`:180`）之前，可以直接用。
  断言串里带上 `decode_paths`，与 `_validate_abi` 的报错口径一致。）
- `_CtypesAdapter.decode` 的路径选择改为三选一：
```python
        if path is DecodePath.STANDARD:
            function = self._library.wsjt_ft8_decode_standard
        elif path is DecodePath.FAST:
            function = self._library.wsjt_ft8_decode_fast
        else:
            function = self._library.wsjt_ft8_decode_improved
```
- `_native_config` 的 `_require_integer("profile", ...)` 等对 fast 仍成立（fast 忽略
  profile/threads/cycles，但校验照旧，保持两路径的入参契约一致）。
- `tests/ft8/core/test_binding.py` 四处（对应警示框 3–6）：
  ```python
  # :31 模块级 EXPECTED_ABI（FakeNative 在 :60/:101 用它）
      "abi_version": 2,
  # :42
      "decode_paths": 0b111,        # 原 "reserved": 0
  # :353 参数化表首行
          ("abi_version", 1),       # 原 ("abi_version", 2) —— 2 已变成合法值
  # :366 末行
          ("decode_paths", 0b010),  # 原 ("reserved", 1)
  # :667-673
      assert {
          name: getattr(binding_module._AbiInfo, name).offset
          for name in ("abi_version", "result_size", "improved_profiles", "decode_paths")
      } == {
          "abi_version": 0,
          "result_size": 8,
          "improved_profiles": 32,
          "decode_paths": 44,       # 偏移不变：是改名，不是挪位
      }
  ```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
.venv/bin/python -m pytest tests/ft8/core/ tests/ft8/dsp/test_abi_smoke.py -v
```
Expected: PASS

**整目录跑，不是只跑 `test_binding.py`。** 这不是"顺手多跑一点"：Task 3 把 shim 的
`abi_version` 升到 2 之后，`tests/ft8/core/test_worker.py`（它用**真实库**、不是假夹具）
现在是 **8 failed + 6 errors**（2026-10-13 于 599d91d 实测：`8 failed, 210 passed, 6 errors`，
全部落在 `test_worker.py`）。本任务就是让它归零的那一步 —— 只跑 `test_binding.py`
会看到一片绿，而真正被这次 ABI 改动打断的那 14 个用例一个都没被验证。

- [ ] **Step 5: 提交**

```bash
git add ft8/core/models.py ft8/core/binding.py tests/ft8/core/test_binding.py
git commit -m "feat(ft8): expose DecodePath.FAST through the locked ctypes boundary"
```

---

## Task 6: protocol / worker 放行 path=fast

**Files:**
- Modify: `ft8/core/protocol.py:295-297`（`_decode_path` 的取值白名单）
- Modify: `ft8/core/binding.py:479`（**过期的报错串**，见 R66 说明）
- Modify: `tests/ft8/core/test_protocol.py:275-276`（**既有用例把 `"fast"` 当非法值的例子**，见警示框）
- Test: `tests/ft8/core/test_protocol.py`、`tests/ft8/core/test_worker.py`（各追加一个用例）

**Interfaces:**
- Consumes: `DecodePath.FAST`（Task 5）。
- Produces: 控制帧 `config.path == "fast"` 能通过校验并被 worker 正确构造为 `DecodeConfig`。

> **`ft8/core/binding.py:479` 的报错串也是「只认两条路径」时代的话，一并改掉。**
> 那一行是 `_native_config` 里的 `if not isinstance(config.path, DecodePath): raise
> ValueError("config path must be standard or improved")` —— 判断本身**已经**是泛型的
> （任何 `DecodePath` 成员都过，Task 5 加 `FAST` 之后就自动接受），所以这不是功能缺陷，
> 只有消息在说谎：它列出两个合法值，而实际有三个。改成
> `"config path must be a DecodePath"`（这句才准确描述那个 isinstance），
> 改完 `tests/ft8/core/` 必须仍然 225 passed —— 若有用例 `match="standard or improved"`，
> 它就是在断言一句错话，把匹配串一起改。
> （发现方式：`grep -rn '"standard".*"improved"\|standard or improved' --include='*.py' .`
> 全仓命中三行 —— `protocol.py:296`、`:297`、`binding.py:479` —— 而计划只写了前两行。
> 这就是 R64/R65/R43 那一类：**同一条约束的连带面比 grep 的第一眼宽**。）

> **`ft8/core/worker.py` 不改。** `build_config` 里本来就是 `path=DecodePath(value["path"])`
> （`worker.py:88`）—— 泛型转换，`DecodePath` 一有 `FAST` 它就通。别为了"改一下"而改它，
> 也别把它写进 `git add`（本任务不动这个文件）。

> **⚠️ 有一个既有用例正拿 `"fast"` 当非法值的例子。**
> `tests/ft8/core/test_protocol.py:275-276` 的参数化分支 `mutation == "path"` 写的是
> `config["path"] = "fast"`，随后 `assert_rejected(frame, "config")`。
> 本任务的**全部目的**就是让 `"fast"` 合法 —— 不改这一行，它会红。
> 改成仍然非法的值（`"turbo"`），并**另加**一个正向用例证明 `"fast"` 现在通过。

- [ ] **Step 1: 写失败测试**

（`tests/ft8/core/test_protocol.py` —— **先改既有那一行，再加一个正向用例**：）

```python
    elif mutation == "path":
        config["path"] = "turbo"   # 原为 "fast" —— 它现在是合法值了，换一个仍然非法的
```

```python
def test_decode_config_accepts_the_fast_path() -> None:
    """`"fast"` 在 Task 5 之后是合法值，控制帧必须放行它并原样往返。

    既有的 test_decode_config_has_every_exact_field_and_strict_scalar_types
    用的是**反向**断言（assert_rejected），所以它只能证明「非法值被拒」，
    证明不了「合法值被放行」—— 少了这个正向用例，把白名单整个删掉也全绿。
    """
    frame = valid_frames()[2]
    config = frame["config"]
    assert isinstance(config, dict)
    config["path"] = "fast"

    assert decode_frame(encode_frame(frame)) == frame
```

（`tests/ft8/core/test_worker.py` 追加 —— 复用该文件**真实**的 `running_worker`
夹具，它没有 `fake_connection` 这种东西；照
`test_worker_decodes_known_cq_from_shared_memory_without_audio_in_frame`（`:294`）的
骨架写，只把 `config()` 换成 `config(path="fast")`、把两条期望值跟着改：）

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/core/test_protocol.py tests/ft8/core/test_worker.py -v`
Expected: FAIL（`test_decode_config_accepts_the_fast_path` 被拒：`_decode_path`
（`protocol.py:295-297`）只认 `{"standard", "improved"}`，抛
`FrameError("config path must be standard or improved")`）

- [ ] **Step 3: 实现**

白名单**确实存在**，而且只有这一处（`ft8/core/protocol.py:295-297`）：

```python
def _decode_path(value: object, name: str) -> None:
    if type(value) is not str or value not in {"standard", "improved", "fast"}:
        raise FrameError(f"{name} must be standard, improved or fast")
```

报错串一并改 —— 它同时是 `test_protocol.py` 里若干 `match="path"` 断言的锚点，
留着旧串会与实际的取值表自相矛盾。

`worker.py` 不动（见顶部警示框）。

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
.venv/bin/python -m pytest tests/ft8/core/ -v
```
Expected: PASS —— 整个 `tests/ft8/core/` **零失败**。不是"这两个文件绿了就行"：
Task 3 把 shim 的 ABI 版本升到 2 之后 `test_worker.py` 一度是 **8 failed + 6 errors**
（见 Task 5 的 Step 4），这里要确认那 14 个用例已经被 Task 5 修回来，
而不是被本任务新增的用例盖过去（总数是涨的，失败数必须是 0）。

- [ ] **Step 5: 提交**

```bash
git add ft8/core/protocol.py ft8/core/binding.py tests/ft8/core/test_protocol.py tests/ft8/core/test_worker.py
git commit -m "feat(ft8): accept the fast decode path over the worker protocol"
```
（**没有 `ft8/core/worker.py`** —— 本任务不动它。`ft8/core/binding.py` 只改 `:479`
那一句报错串，改动量一行；若你认为「为一句消息单独改一个已经通过审查的文件」不划算，
**不要**默默跳过 —— 在报告里写明并给出你的理由，由我裁定。）

---

## Task 7: 一致性验收 —— fast 直连 vs 参考二进制

**Files:**
- Test: `tests/ft8/dsp/test_ft8_fast_parity.py`
- Create: `tests/ft8/dsp/fixtures/ft8lib_reference.json`（一次性生成，纳入版本控制）

**Interfaces:**
- Consumes: `wsjt_ft8_decode_fast`（Task 4）。
- Produces: 一条回归线——**同样的输入，fast 路径的解出条数不得低于**上游 `decode_ft8` 参考实现。

- [ ] **Step 1: 写失败测试**

```python
# tests/ft8/dsp/test_ft8_fast_parity.py
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[3]
FIXTURE = Path(__file__).parent / "fixtures" / "ft8lib_reference.json"


def test_fast_path_matches_reference_decode_count(
    call_fast, raw_library, busy_slot
) -> None:
    status, _results, count, _overflow = call_fast(raw_library, busy_slot)
    assert status == 0
    reference = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert count >= reference["decode_count"], (
        f"fast path decoded {count}, reference decoded {reference['decode_count']}"
    )
```

> **`_call_fast` 必须做成 fixture，不能靠"conftest 里的普通函数"自动可见。**
> pytest 只把 conftest 里的 **fixture** 注入测试；同名的普通函数不会出现在测试的作用域里，
> 直接写 `_call_fast(...)` 会 `NameError`。所以在 `tests/ft8/dsp/conftest.py` 里加：
> ```python
> @pytest.fixture
> def call_fast():
>     def _call(library, samples, capacity=256):
>         ...   # 内容就是 Task 4 的 _call_fast，逐行搬过来
>     return _call
> ```
> 并且 **`test_ft8_fast_decode.py` 也改成从这里取**（把它的 `_call_fast` 删掉、
> 测试签名改成 `(call_fast, raw_library, busy_slot)`），不要留两份实现。
> `busy_slot` 此时已由 Task 4 搬到 conftest。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/dsp/test_ft8_fast_parity.py -v`
Expected: FAIL（`fixtures/ft8lib_reference.json` 不存在）

- [ ] **Step 3: 生成参考夹具**

在上游检出上对**同一**输入跑一次参考二进制，记录条数。

**先核对检出的 commit，再构建。** 基线只有在与 vendor 子集**同一个 commit** 上才有意义
（`ft8/dsp/vendor-ft8lib/README.md` 钉的是 `9fec6ca`）：
```bash
cd /Users/cheenle/HAM/ft8_lib && git log --oneline -1
# 必须印出：9fec6ca non-standard callsigns; special CQ; field type annotation (#47)
```
不是这个 commit 就先 `git fetch && git checkout 9fec6ca`，**不要**在一个漂移的检出上
造基线 —— 那样比的是两份不同的上游代码，而测试自己不会知道。

```bash
cd /tmp && rm -rf ft8lib-ref && cp -r /Users/cheenle/HAM/ft8_lib ft8lib-ref && cd ft8lib-ref
make CFLAGS="-O3 -DHAVE_STPCPY -I." -j4
./decode_ft8 <与 busy_slot 逐字节相同的那个 WAV>
```

**条数从哪一行读（别数错）：** 参考 demo 会打印很多东西，其中两类容易被误当条数。
真正的那一行是 `LOG_INFO`：

```
Decoded N messages, callsign hashtable size M
```
（`demo/decode_ft8.c:230`；`decode_ft8.c:18` 写了 `#define LOG_LEVEL LOG_INFO`，所以它**会**
输出，落在 stderr。）**`N` 就是 `decode_count`。**

交叉校验：stdout 上每个解出的消息还有一行普通 `printf`
（`%02d%02d%02d %+05.1f %+4.2f %4.0f ~  %s`，`demo/decode_ft8.c:225`），
**stdout 的行数应当等于 `N`**。两者对不上说明读错了地方（别去数进度用的 `#`，
那是 `demo/decode_ft8.c:375` 的进度点）。报告里给出这两条实际输出。

> **「同一输入」必须是可证明的，不是"看起来一样"。**
> 参考二进制只吃 WAV，而 `busy_slot` 是内存里的 int16 数组，所以中间必然有一次落盘读回。
> 这一步必须自己证明它无损：用 `wave` 模块把 `busy_slot` 写成 12 kHz / 单声道 / 16-bit PCM，
> 读回来，断言 `np.array_equal(reload, busy_slot)` 为真（int16 小端与 WAV 的 PCM_16 表示一致，
> 这个往返是恒等的）。**这个断言和生成命令一起写进夹具的 `"generated_by"` 字段**，
> 否则测试比的是两个不同的输入，而它自己不会知道。
>
> 三个量已经**核对过**，与我们的实现完全一致，比对是等价的：
> - 频率窗口 `f_min = 200, f_max = 3000`（`demo/decode_ft8.c:327-328`），
>   与 `_call_fast` 的 `low_frequency = 200 / high_frequency = 3000` 相同；
> - 定点→浮点缩放 `raw_data[i] / 32768.0f`（`common/wave.c:125`），
>   与 `wsjt_fast.c` 的 `* (1.0f / 32768.0f)` 相同；
> - 四个算法旋钮 `kMin_score = 10` / `kLDPC_iterations = 25` / `kFreq_osr = 2` /
>   `kTime_osr = 2`（`demo/decode_ft8.c:21,23,27,28`），与 `wsjt_fast.c` 的
>   `FAST_MIN_SCORE` / `FAST_LDPC_ITERATIONS` / `FAST_FREQ_OSR` / `FAST_TIME_OSR`
>   四个 `#define` 逐一同值。
> 其中任何一个不同，`decode_count` 就不具可比性。

把**实测**输出条数写进 `tests/ft8/dsp/fixtures/ft8lib_reference.json`：
```json
{"input": "busy_slot (5 synthetic CQ + noise, 12 kHz int16)",
 "upstream_commit": "9fec6ca",
 "generated_by": "<落盘命令 + 上面那个 array_equal 往返校验>",
 "decode_count": "<参考二进制实测条数，不要预设 5>"}
```
**`decode_count` 必须是参考二进制在真实输入上跑出来的数字** —— 它是本 Task 唯一一处
**必须实测**的值，和 Task 4 的 `FAST_FLOOR` 同理。不是"5 个信号所以 5 条"。
`busy_slot` 叠了 σ=2000 的噪声（Task 4 的注记），参考实现解出的条数可能低于 5；
把它**量出来**再写进去——写死一个猜的数要么让测试无谓失败，要么把一个参考实现自己都
达不到的门槛钉在快路径上。

> **只钉条数，不要钉文本。** 参考 demo 会 `hashtable_init()` 并随解码累积呼号哈希
> （`demo/decode_ft8.c:335`），所以同一个已被哈希的呼号，参考实现可能渲染成真实呼号，
> 而快路径的 `fast_lookup_hash` 恒返回 false、只会渲染成 `<HASH>` 形式。
> 那是**渲染**差异，不是解码能力差异 —— 别在 parity 测试里加文本相等断言，
> 它会失败，而且失败得没有意义。

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
bash ft8/dsp/build.sh
.venv/bin/python -m pytest tests/ft8/dsp/ -q
```
Expected: PASS —— 整个 `tests/ft8/dsp/`，不只是这两个文件。本任务改了
`conftest.py`（把 `_call_fast` 提成 fixture，并让 `test_ft8_fast_decode.py` 也改用它），
那是个**所有** dsp 用例都依赖的共享文件，只跑两个文件看不出把它改坏的后果。
基线：Task 4 结束时 `tests/ft8/dsp/` 是全绿的，这里必须是「全绿 + 本任务新增的 1 个用例」。

- [ ] **Step 5: 提交**

```bash
git add tests/ft8/dsp/test_ft8_fast_parity.py tests/ft8/dsp/fixtures/ft8lib_reference.json tests/ft8/dsp/conftest.py
git commit -m "test(ft8): pin the fast path against the upstream reference decoder"
```

**P1 完成标志**：
1. **验收线（A53 box 上实测）**：`wsjt_ft8_decode_fast` 在第三轮 A/B 的
   `slot12k.wav` 上**中位 wall ≤ 200 ms 且 5/5**（上游参考实现实测中位 153 ms；
   留 ~30% 余量给 int16→float 拷贝与 ABI 包装）。**不达标就不要进 P2** ——
   整条级联的预算算术（0.15 + 2.22 < 2.5 s）建立在快路径真的快之上。
2. `DecodePath.FAST` 可端到端跑通（engine 仍走 improved）；
3. `.venv/bin/python -m pytest tests/ft8 -q` 全绿。

---

# Phase P2 — CascadingDecoder

## Task 8: CascadePolicy + should_escalate

**Files:**
- Create: `ft8/engine/cascade.py`
- Test: `tests/ft8/engine/test_cascade_policy.py`

**Interfaces:**
- Consumes: `DecodeBatch`/`DecodeResult`（既有）。
- Produces:
  - `CascadePolicy`（frozen dataclass，字段见下）
  - `should_escalate(policy, batch_fast, *, my_call, qso_progress, requested) -> bool`

- [ ] **Step 1: 写失败测试**

```python
# tests/ft8/engine/test_cascade_policy.py
from __future__ import annotations

from ft8.core.models import DecodeBatch, DecodePath, DecodeResult
from ft8.engine.cascade import (
    FLAG_SNR_PROVISIONAL,
    CascadePolicy,
    should_escalate,
)


def _batch(
    *snrs: int,
    path: DecodePath = DecodePath.FAST,
    flags: int = FLAG_SNR_PROVISIONAL,
) -> DecodeBatch:
    """默认造一批**快路径**结果 —— snr 是 score 估计值，故带上 provisional 标志。"""
    results = tuple(
        DecodeResult(slot_id=1, sync=0.0, snr=s, dt=0.0, frequency=1500.0,
                     text="CQ TEST", ap_type=0, quality=0.0, flags=flags)
        for s in snrs
    )
    return DecodeBatch(slot_id=1, path=path, results=results,
                       overflow=False, elapsed_seconds=0.15)


def _deep_batch(*snrs: int) -> DecodeBatch:
    """深路径结果：snr 是真 dB，不带 provisional 标志。"""
    return _batch(*snrs, path=DecodePath.IMPROVED, flags=0)


DEFAULT = CascadePolicy()


def test_no_escalation_when_strong_and_no_qso() -> None:
    assert should_escalate(DEFAULT, _batch(-5, -7), my_call="BA4EG",
                           qso_progress=0, requested=False) is False


def test_escalates_on_active_qso() -> None:
    assert should_escalate(DEFAULT, _batch(-5), my_call="BA4EG",
                           qso_progress=2, requested=False) is True


def test_no_escalation_on_qso_when_ap_context_disabled() -> None:
    policy = CascadePolicy(escalate_on_ap_context=False)
    assert should_escalate(policy, _batch(-5), my_call="BA4EG",
                           qso_progress=2, requested=False) is False


def test_escalates_when_too_few_decodes() -> None:
    assert should_escalate(DEFAULT, _batch(), my_call="", qso_progress=0,
                           requested=False) is True


def test_provisional_snr_never_drives_escalation() -> None:
    """快路径的 snr 不是 dB —— 再"低"也不能拿它当弱信号判据。

    否则升级与否就由 score 的任意缩放决定了（见 spec D-7）。这些用例里
    只有 min_decodes 该起作用：有结果就不升级。
    """
    assert should_escalate(DEFAULT, _batch(-28, -30), my_call="", qso_progress=0,
                           requested=False) is False
    assert should_escalate(DEFAULT, _batch(-5, -30), my_call="", qso_progress=0,
                           requested=False) is False


def test_escalates_on_real_weak_snr_from_a_deep_batch() -> None:
    """snr 可信（无 provisional 标志）时，峰值判据必须真的起作用。"""
    assert should_escalate(DEFAULT, _deep_batch(-28, -30), my_call="",
                           qso_progress=0, requested=False) is True
    # 判据看的是**峰值**：一条强的就够压住它。
    assert should_escalate(DEFAULT, _deep_batch(-5, -30), my_call="",
                           qso_progress=0, requested=False) is False


def test_does_not_escalate_at_the_measured_complete_point() -> None:
    """-24 dB 是第三轮 A/B 里 fast 与深路径同为 5/5 的点：此时不该升级。"""
    assert should_escalate(DEFAULT, _deep_batch(-24), my_call="", qso_progress=0,
                           requested=False) is False
    assert DEFAULT.min_peak_snr < -24, "阈值必须低于实测完整的点，否则全是白烧"


def test_explicit_request_escalates_only_when_enabled() -> None:
    assert should_escalate(DEFAULT, _batch(-5), my_call="", qso_progress=0,
                           requested=True) is False
    policy = CascadePolicy(escalate_on_request=True)
    assert should_escalate(policy, _batch(-5), my_call="", qso_progress=0,
                           requested=True) is True


def test_all_criteria_off_never_escalates() -> None:
    policy = CascadePolicy(escalate_on_ap_context=False, escalate_on_weak=False)
    assert should_escalate(policy, _batch(), my_call="BA4EG", qso_progress=3,
                           requested=False) is False


def test_provisional_flag_matches_the_c_header() -> None:
    """Python 的 provisional 位必须与 C 头里的 `WSJT_FLAG_SNR_PROVISIONAL` 同值。

    这两处一旦漂移，`_snr_is_trustworthy` 就会恒为 True，拿 score 估计值去比 dB 阈值 ——
    正是 spec D-7 要挡掉的那个 bug，而且它会静默地重新出现（测试全绿、行为变差）。

    **把数字解析出来比，不要断言子串。** 头里的行是
    `WSJT_FLAG_SNR_PROVISIONAL = 16   /* snr 字段不是 dB … */`，写
    `f"… = {FLAG_SNR_PROVISIONAL}" in header` 看着够用，其实挡不住本用例唯一
    要挡的那类漂移：常量改成 **1**（= `WSJT_FLAG_AP` 的值，最像会手滑的写法）时，
    要找的串变成 `WSJT_FLAG_SNR_PROVISIONAL = 1`，而它**是** `… = 16` 的前缀 ——
    断言照样通过。我实测过这个变异体：`in header` 返回 True。
    解析成 int 再比，`1 != 16` 当场就红。
    """
    header = (ROOT / "ft8" / "dsp" / "wsjt_core.h").read_text(encoding="utf-8")
    line = next(
        ln for ln in header.splitlines()
        if ln.strip().startswith("WSJT_FLAG_SNR_PROVISIONAL")
    )
    declared = int(line.split("=", 1)[1].split()[0])
    assert declared == FLAG_SNR_PROVISIONAL, (
        f"C header declares {declared}, Python has {FLAG_SNR_PROVISIONAL}"
    )
```

（该文件顶部需要 `from pathlib import Path` 与 `ROOT = Path(__file__).parents[3]`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_cascade_policy.py -v`
Expected: FAIL（`ModuleNotFoundError: ft8.engine.cascade`）

- [ ] **Step 3: 实现**

```python
# ft8/engine/cascade.py
"""Adaptive cascade policy: when a slot's fast decode needs the deep path.

Pure Python and clock-free so the policy is testable without the native
library.  See docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from ft8.core.models import DecodeBatch

# 与 ft8/dsp/wsjt_core.h 的 WSJT_FLAG_SNR_PROVISIONAL 同值（bit 4）。
FLAG_SNR_PROVISIONAL = 16

DEFAULT_MIN_DECODES = 1
# 第三轮 A/B（docs/ft8-w103d-decode-perf-analysis.md §8）：fast 在 0…-24 dB 与
# 深路径同为 5/5，到 -30 dB 才塌成 0/5。所以峰 SNR 判据要卡在这道崖的上方一点点、
# 而不是 -20 —— 落在 -24 只会对着一个「实测已知解码完整」的时隙每次白烧 2.2 s。
DEFAULT_MIN_PEAK_SNR = -26


@dataclass(frozen=True, slots=True)
class CascadePolicy:
    """Which conditions trigger a same-slot deep re-decode."""

    escalate_on_ap_context: bool = True
    escalate_on_weak: bool = True
    escalate_on_request: bool = False
    min_decodes: int = DEFAULT_MIN_DECODES
    min_peak_snr: int = DEFAULT_MIN_PEAK_SNR


def should_escalate(
    policy: CascadePolicy,
    batch: DecodeBatch,
    *,
    my_call: str,
    qso_progress: int,
    requested: bool,
) -> bool:
    """Return True when the slot should also run the deep (WSJT-X) path."""

    if policy.escalate_on_request and requested:
        return True
    if policy.escalate_on_ap_context and my_call and qso_progress > 0:
        return True
    if policy.escalate_on_weak:
        if len(batch.results) < policy.min_decodes:
            return True
        if _snr_is_trustworthy(batch):
            if max(r.snr for r in batch.results) < policy.min_peak_snr:
                return True
    return False


def _snr_is_trustworthy(batch: DecodeBatch) -> bool:
    """Whether this batch's ``snr`` values are dB (so comparable to S_MIN).

    The fast path has no calibrated SNR — it reports an upstream
    score-derived estimate and marks every result with
    ``FLAG_SNR_PROVISIONAL``.  Comparing that number against a dB threshold
    would produce escalation decisions driven by noise, so the peak-SNR
    criterion is skipped for such a batch and ``min_decodes`` carries the
    weak-signal detection instead (spec D-7).  A future fast path that
    computes a real SNR simply stops setting the flag and re-enables this.
    """

    if not batch.results:
        return False
    return all(not (r.flags & FLAG_SNR_PROVISIONAL) for r in batch.results)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_cascade_policy.py -v`
Expected: PASS（10 个用例）

- [ ] **Step 5: 提交**

```bash
git add ft8/engine/cascade.py tests/ft8/engine/test_cascade_policy.py
git commit -m "feat(ft8): add the cascade escalation policy"
```

---

## Task 9: 合并 fast + deep 两批结果

**Files:**
- Modify: `ft8/core/models.py:95-103`（`DecodeBatch` 加两个字段）
- Modify: `ft8/engine/cascade.py`
- Test: `tests/ft8/engine/test_cascade_merge.py`

**Interfaces:**
- Consumes: `should_escalate`（Task 8）。
- Produces:
  - `merge_batches(fast: DecodeBatch, deep: DecodeBatch) -> DecodeBatch` —— 并集，deep 胜出。
  - `DecodeBatch.escalated: bool = False` 与 `DecodeBatch.fast_elapsed_seconds: float = 0.0`
    （spec §5.3）。这两个字段是**下游唯一的升级证据**：Task 13 的 `escalations` 计数与
    运维判断「阈值是不是太松」都要靠它。

> **为什么这两个字段必须在这里加，而不是在 Task 13。** `Orchestrator` 只拿得到 `decode()`
> 的**返回值**（`orchestrator.py:242` 的 `batch = await self._decoder.decode(...)`），它手上
> 没有 `CascadingDecoder` 的引用，所以「这一槽升级了吗」只能由返回值携带。Task 13 的
> `escalations` 就是从 `batch.escalated` 读的 —— 少了这一步，Task 13 无从实现。

- [ ] **Step 1: 写失败测试**

```python
# tests/ft8/engine/test_cascade_merge.py
from __future__ import annotations

from ft8.core.models import DecodeBatch, DecodePath, DecodeResult
from ft8.engine.cascade import merge_batches


def _r(text: str, freq: float = 1500.0, dt: float = 0.1, snr: int = -5,
       ap_type: int = 0) -> DecodeResult:
    return DecodeResult(slot_id=1, sync=0.0, snr=snr, dt=dt, frequency=freq,
                        text=text, ap_type=ap_type, quality=0.0, flags=0)


def _b(path: DecodePath, *results: DecodeResult) -> DecodeBatch:
    return DecodeBatch(slot_id=1, path=path, results=results, overflow=False,
                       elapsed_seconds=0.2)


def test_union_keeps_both_sides() -> None:
    # **两条结果必须落在不同的信号上**（频率拉开 > _FREQ_TOL）。用 `_r` 的默认值
    # 会让两条都停在 1500.0 / 0.1，`_same_signal` 判定为同一条 → 快路径那条被当作
    # 重复丢掉，并集只剩 "CQ BBB"，这条用例就永远不可能通过 —— 而"修"它的错误方式
    # （把断言改成 {"CQ BBB"}）恰好会把「并集」这个唯一被测的性质删掉。
    fast = _b(DecodePath.FAST, _r("CQ AAA", freq=1000.0))
    deep = _b(DecodePath.IMPROVED, _r("CQ BBB", freq=2000.0))
    merged = merge_batches(fast, deep)
    assert {r.text for r in merged.results} == {"CQ AAA", "CQ BBB"}
    assert merged.path is DecodePath.IMPROVED


def test_deep_wins_on_same_key_including_snr_and_ap_type() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA", snr=99, ap_type=0))
    deep = _b(DecodePath.IMPROVED, _r("CQ AAA", snr=-5, ap_type=1))
    merged = merge_batches(fast, deep)
    assert len(merged.results) == 1
    assert merged.results[0].snr == -5
    assert merged.results[0].ap_type == 1


def test_hash_placeholder_is_merged_not_duplicated() -> None:
    # fast 保留 <HASH> 文本，deep 还原了呼号：同一 freq/dt 应视为同一条（Review Focus #1）
    fast = _b(DecodePath.FAST, _r("CQ <...>", freq=1200.0, dt=0.2))
    deep = _b(DecodePath.IMPROVED, _r("CQ BA4EG", freq=1200.0, dt=0.2))
    merged = merge_batches(fast, deep)
    assert len(merged.results) == 1
    assert merged.results[0].text == "CQ BA4EG"


def test_empty_deep_returns_fast_only() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    merged = merge_batches(fast, _b(DecodePath.IMPROVED))
    assert merged.path is DecodePath.FAST
    assert [r.text for r in merged.results] == ["CQ AAA"]


def test_overflow_is_logical_or() -> None:
    fast = DecodeBatch(1, DecodePath.FAST, (_r("A"),), True, 0.1)
    deep = DecodeBatch(1, DecodePath.IMPROVED, (_r("B"),), False, 0.2)
    assert merge_batches(fast, deep).overflow is True


def test_merging_two_different_slots_is_an_error() -> None:
    """并的是**同一个**时隙的两条路径结果 —— 不同 slot 的合并是凭空造 QSO。"""
    fast = DecodeBatch(7, DecodePath.FAST, (_r("CQ AAA"),), False, 0.1)
    deep = DecodeBatch(8, DecodePath.IMPROVED, (_r("CQ BBB"),), False, 0.2)
    with pytest.raises(ValueError, match="slot"):
        merge_batches(fast, deep)


def test_merge_marks_the_escalation_and_keeps_each_passes_elapsed() -> None:
    """合并结果要能回答两件事：这一槽升级过吗？各路径各花了多久？

    单看 ``elapsed_seconds`` 是答不出来的（它是两段之和），而下游 Task 13 的
    ``escalations`` 计数完全依赖 ``escalated``。
    """
    fast = DecodeBatch(1, DecodePath.FAST, (_r("CQ AAA"),), False, 0.16)
    deep = DecodeBatch(1, DecodePath.IMPROVED, (_r("CQ BBB"),), False, 2.2)
    merged = merge_batches(fast, deep)
    assert merged.escalated is True
    assert merged.fast_elapsed_seconds == 0.16
    assert merged.elapsed_seconds == pytest.approx(2.36)  # spec §5.3：两段之和


def test_escalation_is_recorded_even_when_the_deep_pass_found_nothing() -> None:
    """深路径跑了但一条都没多解出来，仍然是**发生过**的升级 —— 正是运维要看的那个信号。

    如果把 ``escalated`` 绑在「deep 有结果」上，那么阈值调得过松的时隙全都隐形，
    而「每槽白烧 2.2s、一条没多」恰恰是最该被发现的那种配置。
    """
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    merged = merge_batches(fast, _b(DecodePath.IMPROVED))
    assert merged.escalated is True
    assert merged.path is DecodePath.FAST
    assert merged.fast_elapsed_seconds == 0.2
```

（该文件顶部需要 `import pytest`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_cascade_merge.py -v`
Expected: FAIL（`ImportError: cannot import name 'merge_batches'`）

- [ ] **Step 3: 实现**

先给 `ft8/core/models.py:95-103` 的 `DecodeBatch` 追加两个带默认值的字段（spec §5.3）——
**必须带默认值且排在 `deadline_missed` 之后**，因为既有代码到处用 5 个位置参数构造它：
```python
@dataclass(frozen=True, slots=True)
class DecodeBatch:
    """One completed decode request, possibly the merge of both paths."""

    slot_id: int
    path: DecodePath
    results: tuple[DecodeResult, ...]
    overflow: bool
    elapsed_seconds: float
    deadline_missed: bool = False
    # 两条路径的结果合并时置 True —— 升级**发生过**，与「深路径多解出东西了吗」
    # 无关（跑了两秒一条没多，恰恰是最该被看见的配置）。
    escalated: bool = False
    # 升级前的快路径耗时，供运维分辨「慢是慢在哪一段」。
    fast_elapsed_seconds: float = 0.0
```

再追加到 `ft8/engine/cascade.py`：

```python
_FREQ_TOL = 5.0     # Hz — 两条路径对同一信号的频率估计允许的偏差
_DT_TOL = 0.15      # s  — 同上，时间偏差


def _same_signal(a: DecodeResult, b: DecodeResult) -> bool:
    return (
        abs(a.frequency - b.frequency) <= _FREQ_TOL
        and abs(a.dt - b.dt) <= _DT_TOL
    )


def merge_batches(fast: DecodeBatch, deep: DecodeBatch) -> DecodeBatch:
    """Union of two batches; the deep result wins on any shared signal.

    Matching is by (frequency, dt) within tolerance, **not** by text: the
    fast path leaves hashed callsigns as ``<...>`` while the deep path
    restores them, so identical text is not the right key (spec R-2).
    """

    if fast.slot_id != deep.slot_id:
        raise ValueError(
            f"cannot merge different slots: {fast.slot_id} != {deep.slot_id}"
        )

    merged = list(deep.results)
    for candidate in fast.results:
        if any(_same_signal(candidate, kept) for kept in merged):
            continue
        merged.append(candidate)
    path = DecodePath.IMPROVED if deep.results else DecodePath.FAST
    return DecodeBatch(
        slot_id=deep.slot_id,
        path=path,
        results=tuple(merged),
        overflow=fast.overflow or deep.overflow,
        elapsed_seconds=fast.elapsed_seconds + deep.elapsed_seconds,
        escalated=True,
        fast_elapsed_seconds=fast.elapsed_seconds,
    )
```

`escalated=True` 是**无条件**的：能走到 `merge_batches` 的每一槽都已经真的跑过深路径，
深路径一条没多解出也是升级发生过。把它绑在 `deep.results` 上就正好把「阈值太松」
那类时隙藏起来了（`test_escalation_is_recorded_even_when_the_deep_pass_found_nothing`）。

> 那个 `slot_id` 检查是**不变量守卫**，不是多余的：合并两个不同时隙的结果会凭空造出
> 一个不存在于任何时隙里的 QSO，而那是几乎不可能在看板上发现的错误。
> `CascadingDecoder` 永远传同一个 `slot_id`，所以它只在有人日后误用时才会触发。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_cascade_merge.py -v`
Expected: PASS（8 个用例）

- [ ] **Step 5: 提交**

```bash
git add ft8/core/models.py ft8/engine/cascade.py tests/ft8/engine/test_cascade_merge.py
git commit -m "feat(ft8): merge fast and deep decode batches without duplicates"
```

提交前确认 `tests/ft8` 全绿 —— `DecodeBatch` 是全仓共享的模型，两个新字段带默认值，
既有构造点一个都不该动：`grep -rn "DecodeBatch(" ft8/ tests/ | grep -v "escalated"` 的结果里
不该出现新的改动。

---

## Task 10: CascadingDecoder（含预算守卫）

**Files:**
- Modify: `ft8/engine/cascade.py`
- Test: `tests/ft8/engine/test_cascading_decoder.py`

**Interfaces:**
- Consumes: `should_escalate`、`merge_batches`（Task 8/9）；`SlotDecoder` 协议（`SupervisorDecoder.decode` 的签名，`ft8/engine/dsp_decode.py:85`）。
- Produces: `CascadingDecoder(inner, policy, *, boundary_epoch_for, clock=time.time, cutoff, deep_threads, deep_lower_bound_s, safety_margin_s, ap_context, requested)`，`async decode(slot_id, samples) -> DecodeBatch`，`set_policy(policy) -> None`（运行期换策略，Task 14 的设置保存路径靠它）。`boundary_epoch_for` 与 `cutoff` **都是必填**的关键字参数（无默认值 —— 见下方实现里的两段注记；两者漏掉都会让预算判断静默出错）。

**时钟基准（必须与 orchestrator 一致）**：`orchestrator.py:125` 的既有 `clock: Callable[[], float] = time.time`，而 `boundary_epoch`（`orchestrator.py:225`）就是 epoch 秒。所以本类的 `clock` 默认**也必须是 `time.time`**，不能是 `time.monotonic`：两者混用会让 `clock() - boundary` 变成约 −1.7e9，预算恒为正的巨数，**守卫在真实运行中永不触发**，而所有测试因为总是成对注入而全绿。这类"守卫看着在、实际从不生效"的错是最贵的一种。

- [ ] **Step 1: 写失败测试**

```python
# tests/ft8/engine/test_cascading_decoder.py
from __future__ import annotations

import asyncio

import pytest

from ft8.core.models import DecodeBatch, DecodeConfig, DecodePath, DecodeResult
from ft8.engine.cascade import CascadePolicy, CascadingDecoder


def _r(text: str, snr: int = -5) -> DecodeResult:
    return DecodeResult(1, 0.0, snr, 0.1, 1500.0, text, 0, 0.0, 0)


def _b(path: DecodePath, *rs: DecodeResult, elapsed: float = 0.15) -> DecodeBatch:
    return DecodeBatch(1, path, rs, False, elapsed)


class PathAwareFake:
    def __init__(self, by_path: dict[DecodePath, DecodeBatch]) -> None:
        self.by_path = by_path
        self.seen: list[tuple[DecodePath, int, DecodeConfig | None]] = []

    async def decode_path(self, slot_id, samples, path, *, threads=None):
        self.seen.append((path, threads, None))
        return self.by_path[path]


@pytest.mark.parametrize(
    ("kwargs", "missing"),
    [
        ({"cutoff": 2.5}, "boundary_epoch_for"),
        ({"boundary_epoch_for": lambda s: 100.0}, "cutoff"),
    ],
)
def test_omitting_a_required_injection_is_an_error(kwargs, missing) -> None:
    """两个必填注入项**各自**都要证明「漏了会当场 TypeError」。

    它们故意都不给默认值，且理由是同一条：漏掉的后果不是异常，是预算判断
    静悄悄地错。`boundary_epoch_for` 若默认成 `slot * 15.0`，就把时隙号和
    epoch 时钟混着算；`cutoff` 若在本模块自带一个 2.5，就等于让决策截止这条
    物理量有了第二个来源（唯一来源是 `orchestrator.py:35`）。

    参数化而不是合成一个用例：一个「两个都漏」的用例在只补回其中一个默认值时
    仍然会红，于是它证明不了**是哪一个**被钉住了 —— 那正是 R67 的形状。
    """
    with pytest.raises(TypeError, match=missing):
        CascadingDecoder(PathAwareFake({}), CascadePolicy(), **kwargs)


def test_strong_slot_returns_fast_without_escalating() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA", snr=-5))
    inner = PathAwareFake({DecodePath.FAST: fast})
    decoder = CascadingDecoder(inner, CascadePolicy(escalate_on_weak=False),
                               cutoff=2.5,
                               clock=lambda: 100.0, boundary_epoch_for=lambda s: 100.0)
    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.FAST
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST]


def test_escalates_and_merges_during_a_qso() -> None:
    """升级由 AP 上下文触发 —— 快路径的 snr 不可信，通联中才是真实触发场景。"""
    fast = _b(DecodePath.FAST, _r("CQ AAA"), elapsed=0.16)
    deep = _b(DecodePath.IMPROVED, _r("CQ AAA", snr=-12), _r("CQ BBB"),
              elapsed=2.2)
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    decoder = CascadingDecoder(inner, CascadePolicy(),
                               cutoff=2.5,
                               boundary_epoch_for=lambda s: 100.0,
                               clock=lambda: 100.0,
                               ap_context=lambda: ("BA4EG", 2))
    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.IMPROVED
    assert {r.text for r in out.results} == {"CQ AAA", "CQ BBB"}
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST, DecodePath.IMPROVED]
    assert inner.seen[1][1] >= 3, "deep pass must run multi-threaded (spec D-4)"


def test_budget_guard_skips_escalation_when_time_is_short() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA"), elapsed=0.16)
    inner = PathAwareFake({DecodePath.FAST: fast})
    # 时隙边界 100.0、cutoff 2.5、时钟已走到 100.4 → 剩 2.5−0.4−0.05(安全边际)=2.05s，
    # < deep 下界 2.3s。（快路径耗时已含在 clock()−boundary 里，实现里不再另减。）
    decoder = CascadingDecoder(inner, CascadePolicy(),
                               cutoff=2.5,
                               boundary_epoch_for=lambda s: 100.0,
                               clock=lambda: 100.4,
                               deep_lower_bound_s=2.3,
                               ap_context=lambda: ("BA4EG", 2))
    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.FAST
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST]
    assert decoder.deadline_risk == 1, "被预算挡下的升级要计数（Task 13 依赖它）"


def test_fast_failure_falls_back_to_deep() -> None:
    deep = _b(DecodePath.IMPROVED, _r("CQ AAA"))

    class Failing(PathAwareFake):
        async def decode_path(self, slot_id, samples, path, *, threads=None):
            self.seen.append((path, threads, None))
            if path is DecodePath.FAST:
                raise RuntimeError("fast path unavailable")
            return self.by_path[path]

    failing = Failing({DecodePath.IMPROVED: deep})
    decoder = CascadingDecoder(failing, CascadePolicy(), cutoff=2.5,
                               clock=lambda: 100.0, boundary_epoch_for=lambda s: 100.0)
    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.IMPROVED
    assert [r.text for r in out.results] == ["CQ AAA"]


def test_requested_flag_reaches_the_policy() -> None:
    """「深度解码本槽」那个一次性开关必须真的接到策略上。

    如果它靠 getattr 兜底去内层对象上摸一个并不存在的属性，拿不到时不会报错，
    只会永远不升级 —— 功能静默死亡，是最坏的失败方式。
    """
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    deep = _b(DecodePath.IMPROVED, _r("CQ AAA", snr=-12), _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    decoder = CascadingDecoder(
        inner, CascadePolicy(escalate_on_request=True),
        cutoff=2.5,
        boundary_epoch_for=lambda s: 100.0,
        clock=lambda: 100.0,
        requested=lambda: True,
    )
    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.IMPROVED
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST, DecodePath.IMPROVED]


def test_set_policy_takes_effect_on_the_next_slot() -> None:
    """运行期改策略必须立刻生效（Task 14 的设置保存路径靠它）。

    若 set_policy 只是把策略存进一个构造期副本、或干脆不存在，保存设置后
    要重启引擎才生效 —— 用户按了保存、行为不变，无从判断是不是按到了。
    """
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    deep = _b(DecodePath.IMPROVED, _r("CQ AAA", snr=-12), _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    decoder = CascadingDecoder(
        inner, CascadePolicy(escalate_on_weak=False),
        cutoff=2.5,
        boundary_epoch_for=lambda s: 100.0,
        clock=lambda: 100.0,
        requested=lambda: True,
    )
    # 旧策略 escalate_on_request=False → 就算 requested() 说 True 也不升级。
    assert asyncio.run(decoder.decode(1, b"\x00" * 360_000)).path is DecodePath.FAST
    inner.seen.clear()

    decoder.set_policy(CascadePolicy(escalate_on_request=True))

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.IMPROVED
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST, DecodePath.IMPROVED]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_cascading_decoder.py -v`
Expected: FAIL（`ImportError: cannot import name 'CascadingDecoder'`）

- [ ] **Step 3: 实现**（追加到 `ft8/engine/cascade.py`）

先把需要的 import 补进 `ft8/engine/cascade.py` 的**既有模块头部**（Task 8 已建该文件，`from __future__ import annotations` 就在首行，不要重复声明，也不要在文件末尾再来一次 import）：

```python
import logging
import time
from collections.abc import Callable

from ft8.core.models import DecodeBatch, DecodePath
```

然后**追加**到文件末尾：

```python
logger = logging.getLogger(__name__)

# **没有 `DEFAULT_CUTOFF_SECONDS`。** 决策截止这个量在 `orchestrator.py:35` 已经有
# 唯一的定义（`DEFAULT_DECISION_CUTOFF_SECONDS = 2.5`），而 `Orchestrator(decision_cutoff=…)`
# 是个**活跃的旋钮**（`tests/ft8/engine/test_orchestrator.py:163` 传 0.0、`:170` 传
# 整个时隙）。在这里再放一个同值常量，就让同一条物理量有了两个来源：生产代码今天
# 恰好两边都是默认值，所以看不出来；某天有人给 orchestrator 传了别的截止，
# 级联的预算守卫仍在拿 2.5 算 —— 守卫被静默地重新标定，而**没有任何测试会红**。
# 这正是 Task 10 给 `boundary_epoch_for` 写「必填、故意不给默认值」的同一个理由，
# 所以 `cutoff` 也照办：必填，由接线处传 orchestrator 的那一个值。
DEFAULT_DEEP_THREADS = 3
DEFAULT_DEEP_LOWER_BOUND_SECONDS = 2.3
DEFAULT_SAFETY_MARGIN_SECONDS = 0.05


class CascadingDecoder:
    """Run the fast path by default and escalate to the deep path on demand.

    ``inner`` is a :class:`SupervisorDecoder` (or any object exposing
    ``decode_path(slot_id, samples, path, *, threads=None)``).  Both the
    clock and the slot-boundary mapping are injectable so the policy is
    fully testable without the native library.
    """

    def __init__(
        self,
        inner: object,
        policy: CascadePolicy,
        *,
        boundary_epoch_for: Callable[[int], float],
        # 必填，理由与 boundary_epoch_for 相同：决策截止的唯一来源是
        # `orchestrator.py:35` 的 DEFAULT_DECISION_CUTOFF_SECONDS（或传给
        # Orchestrator 的那个值）。本模块**不提供**自己的默认值 —— 详见上面
        # 「没有 DEFAULT_CUTOFF_SECONDS」那段。两处各自 2.5 的生产代码今天看起来
        # 是对的，而守卫恰恰是在有人改了 orchestrator 之后才悄悄失准的。
        cutoff: float,
        # 默认必须是 time.time（epoch 秒）—— orchestrator.py:125 的 `clock` 与
        # orchestrator.py:225 的 `boundary_epoch` 都是这个基准。若写成 time.monotonic，
        # clock() − boundary 会是约 −1.7e9，预算恒为正的巨数，**守卫在真实运行里永不
        # 触发**；而测试因为总是成对注入同一基准而全绿。时钟基准只允许有一处。
        clock: Callable[[], float] = time.time,
        deep_threads: int = DEFAULT_DEEP_THREADS,
        deep_lower_bound_s: float = DEFAULT_DEEP_LOWER_BOUND_SECONDS,
        safety_margin_s: float = DEFAULT_SAFETY_MARGIN_SECONDS,
        ap_context: Callable[[], tuple[str, int]] | None = None,
        requested: Callable[[], bool] | None = None,
    ) -> None:
        if deep_threads < 1:
            raise ValueError("deep_threads must be positive")
        self._inner = inner
        self._policy = policy
        self._clock = clock
        self._cutoff = cutoff
        # 必填，故意不给默认值：`slot * 15.0` 这种"默认"会把单调时钟和
        # 时隙号混在一起算，忘了注入的后果是预算判断静默出错（该升级时不升级、
        # 或反之），而不是报错。宁可构造时就 TypeError。
        self._boundary_epoch_for = boundary_epoch_for
        self._deep_threads = deep_threads
        self._deep_lower_bound_s = deep_lower_bound_s
        self._safety_margin_s = safety_margin_s
        self._ap_context = ap_context or (lambda: ("", 0))
        self._requested = requested or (lambda: False)
        self.deadline_risk = 0

    def set_policy(self, policy: CascadePolicy) -> None:
        """替换升级策略，从下一个时隙起生效。

        只换策略对象，不碰任何在跑的状态 —— 所以保存设置时可以直接推给活着的
        解码器，不必重建它（重建会丢掉 in-flight 的时隙）。
        """
        self._policy = policy

    def _remaining_budget(self, slot_id: int) -> float:
        """本时隙还剩几秒可以拿去跑深路径。

        只在**快路径跑完之后**调用，所以 ``clock() − boundary`` 已经把快路径的耗时
        算进去了，不再另减 ``batch_fast.elapsed_seconds`` —— 那会把同一段时间扣两次。
        """
        boundary = self._boundary_epoch_for(slot_id)
        return self._cutoff - (self._clock() - boundary) - self._safety_margin_s

    async def decode(self, slot_id: int, samples: bytes) -> DecodeBatch:
        try:
            batch_fast = await self._inner.decode_path(
                slot_id, samples, DecodePath.FAST
            )
        except Exception:
            # 快路径整个不可用时没有结果可退，只能在明知超预算的情况下补跑一次深路径；
            # 但线程数仍必须是 deep_threads —— threads=1 要 3.23s，塞不进 2.5s 窗口
            # （spec D-4），那不是"慢一点"，是必定错过截止。
            logger.warning(
                "fast decode path failed; running the deep path unbudgeted", exc_info=True
            )
            return await self._inner.decode_path(
                slot_id, samples, DecodePath.IMPROVED, threads=self._deep_threads
            )

        my_call, qso_progress = self._ap_context()
        if not should_escalate(
            self._policy, batch_fast, my_call=my_call,
            qso_progress=qso_progress,
            requested=self._requested(),
        ):
            return batch_fast

        if self._remaining_budget(slot_id) < self._deep_lower_bound_s:
            self.deadline_risk += 1
            logger.info(
                "skipping deep escalation for slot %s: budget too tight", slot_id
            )
            return batch_fast

        try:
            batch_deep = await self._inner.decode_path(
                slot_id, samples, DecodePath.IMPROVED, threads=self._deep_threads
            )
        except Exception:
            logger.warning("deep decode path failed; keeping the fast results", exc_info=True)
            return batch_fast

        return merge_batches(batch_fast, batch_deep)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_cascading_decoder.py -v`
Expected: PASS（7 个用例函数，其中 `test_omitting_a_required_injection_is_an_error`
参数化成 2 条 → pytest 收 8 条）

- [ ] **Step 5: 提交**

```bash
git add ft8/engine/cascade.py tests/ft8/engine/test_cascading_decoder.py
git commit -m "feat(ft8): add the cascading decoder with a time-budget guard"
```

---

## Task 11: `SupervisorDecoder.decode_path` + engine 接线

**Files:**
- Modify: `ft8/engine/dsp_decode.py:85-139`（加 `decode_path`）
- Modify: `ft8/engine/orchestrator.py:57` 附近（加 `boundary_epoch_for_slot` 助手）
- Modify: `ft8/runtime.py:175` 附近（装配处，构造 `CascadingDecoder`）
- Test: `tests/ft8/engine/test_dsp_decode.py`、`tests/ft8/engine/test_orchestrator.py`（扩展）

**Interfaces:**
- Consumes: `CascadingDecoder`（Task 10）、`SupervisorDecoder`（既有）、`slot_start_epoch`（`orchestrator.py:57`）、`Sequencer.my_call` / `Sequencer.state`（`sequencer.py:108,110`）。
- Produces: `SupervisorDecoder.decode_path(slot_id, samples, path, *, threads=None) -> DecodeBatch`；`orchestrator.boundary_epoch_for_slot(slot_id, period) -> float`；runtime 通过 `CascadingDecoder` 解码。

> **装配点在 `ft8/runtime.py:175`，不是 `orchestrator.py`。** `Orchestrator.__init__` 里是
> `self._decoder = decoder` —— 解码器由调用方注入，orchestrator 自己不构造它。真正的组合根是
> `runtime.py:175` 的 `Orchestrator(self.decoder, read_slot, sequencer, …)`。
> （`tests/ft8/engine/test_orchestrator.py` 里那些 `Orchestrator(...)` 是测试自己的假解码器。）

> **边界必须是「本时隙的结束」，即 `slot_start_epoch(slot_id + 1)`，不是 `slot_start_epoch(slot_id)`。**
> 依据就在 orchestrator 自己的代码里：`_slot_ended(current, slot_end, samples)`（`:207`）传进去的是
> `slot_end = slot_start_epoch(current + 1, …)`（`:179`），而 `_slot_ended` 用
> `late = finished > boundary_epoch + self._decision_cutoff`（`:258`）—— 决策截止正是从时隙
> **结束**算起。若误用 `slot_start_epoch(slot_id)`，边界会早整整一个时隙（15 s），
> `clock() − boundary ≈ 15`，`_remaining_budget ≈ 2.5 − 15 − 0.05 < 0` **恒小于 deep 下界 →
> 升级永不发生**，而 `deadline_risk` 每槽照增 —— 看上去在工作，实际从不生效，与 Task 10 的
> 时钟基准（R20）是同一种失败。因此这个映射**不许写成内联 lambda**，要写成有名字、可单测的函数。

- [ ] **Step 1: 写失败测试**

在 `tests/ft8/engine/test_orchestrator.py` 追加（钉住那个 15 秒的 off-by-one）：
```python
def test_boundary_epoch_for_slot_is_the_slots_end() -> None:
    """决策截止从时隙**结束**算起，不是从开始算起。"""

    assert boundary_epoch_for_slot(100, 15.0) == slot_start_epoch(101, 15.0)
    assert boundary_epoch_for_slot(100, 15.0) != slot_start_epoch(100, 15.0)
    assert boundary_epoch_for_slot(100, 15.0) - boundary_epoch_for_slot(99, 15.0) == 15.0
```
（该文件顶部已经导入了 `slot_start_epoch` 的话就直接用；没有就在 import 行里加。）

在 `tests/ft8/engine/test_dsp_decode.py` 追加。**先读一遍该文件既有的假 supervisor fixture**，
看它怎么记录发给 worker 的 frame，再照它的风格断言 —— 下面是**要断言的内容**，不是可照抄的代码：
- `decode_path(slot_id, samples, DecodePath.FAST)` 发出的 frame 里 `config["path"] == "fast"`；
- `decode_path(slot_id, samples, DecodePath.IMPROVED, threads=3)` 发出的
  `config["path"] == "improved"` 且 `config["threads"] == 3`；
- **不传 `threads` 时 frame 里根本没有 `threads` 这个键**（沿用 `self._config` 的原值 ——
  见下面实现里的 `if threads is not None`，这正是「默认不动」的落点）；
- `decode(slot_id, samples)` 与 `decode_path(slot_id, samples, self._config.path)` 产出的
  `DecodeBatch` 相等（薄封装不得改变任何行为）。

在 `tests/ft8/test_server_wiring.py` 追加（`runtime.py` 的接线是本任务唯一一处构造
`CascadingDecoder` 的地方，钉住它）：
```python
def test_the_cascade_guard_shares_the_orchestrators_decision_cutoff() -> None:
    """预算守卫的截止值必须**来自** orchestrator 的那个常量，不是另写一遍 2.5。

    两个数字今天相等，所以这条用例盯的**不是值、是来源**：改了
    ``DEFAULT_DECISION_CUTOFF_SECONDS``，守卫必须跟着动。`CascadingDecoder`
    已经把 ``cutoff`` 设成必填（模块内没有默认值），所以唯一还能出错的地方
    就是接线处又把 ``2.5`` 写了一遍 —— 这条就钉在那里。

    为什么用源码断言而不是构造整个 `Ft8Runtime`：为了钉住一个关键字实参而把
    backend / CAT / 音频全部搭起来，代价与收益不成比例；而「这个模块引进了
    那个常量、并且在调用里用了它」是这两个断言真正要保证的东西。
    与 `test_early_decode_is_wired_into_the_runtime` 同一风格（直接断言模块常量）。
    """
    from pathlib import Path

    from ft8 import runtime as runtime_mod
    from ft8.engine.orchestrator import DEFAULT_DECISION_CUTOFF_SECONDS

    # 1) 来源是 orchestrator 的那一个，不是 runtime 自己又定义的一个同值常量。
    assert runtime_mod.DEFAULT_DECISION_CUTOFF_SECONDS is DEFAULT_DECISION_CUTOFF_SECONDS

    # 2) 它真的被用在了构造调用里（不是引进来放着）。
    source = Path(runtime_mod.__file__).read_text(encoding="utf-8")
    assert "cutoff=DEFAULT_DECISION_CUTOFF_SECONDS" in source
```

> **`is` 而不是 `==`**：这里要证明的是**同一个对象**（同一个来源），不是"两个
> 浮点数碰巧相等"。写 `==` 的话，runtime 里重新定义一个 `2.5` 也照样通过 ——
> 那就正好放过了本用例要挡的那种漂移。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_orchestrator.py tests/ft8/engine/test_dsp_decode.py -v`
Expected: FAIL（`ImportError: cannot import name 'boundary_epoch_for_slot'`；以及
`AttributeError: 'SupervisorDecoder' object has no attribute 'decode_path'`）

- [ ] **Step 3: 实现**

`ft8/engine/dsp_decode.py` 把现有 `decode` 变为薄封装，新增 `decode_path`：
```python
    async def decode(self, slot_id: int, samples: bytes) -> DecodeBatch:
        return await self.decode_path(slot_id, samples, self._config.path)

    async def decode_path(
        self,
        slot_id: int,
        samples: bytes,
        path: DecodePath,
        *,
        threads: int | None = None,
    ) -> DecodeBatch:
        if len(samples) != SLOT_SAMPLES_NBYTES:
            raise ValueError(f"decode requires exactly {SLOT_SAMPLES_NBYTES} bytes")
        shm = self._ensure_segment()
        shm.buf[: len(samples)] = samples
        changes: dict[str, object] = {
            "utc_hhmmss": slot_utc_hhmmss(slot_id, self._period),
            "path": path,
        }
        if threads is not None:
            changes["threads"] = threads
        config = self._config.replace(**changes)
        # ... 其余与现有 decode 相同（构造 frame、to_thread、解析响应） ...
```

`ft8/engine/orchestrator.py`，紧挨着 `slot_start_epoch`（`:57`）加：
```python
def boundary_epoch_for_slot(slot_id: int, period: float = FT8_PERIOD_SECONDS) -> float:
    """本时隙**结束**的 epoch —— 决策截止就是从这里往后算的。

    等于下一个时隙的开始（`slot_start_epoch(slot_id + 1)`）。`_slot_ended` 的
    `late = finished > boundary_epoch + decision_cutoff`（`:258`）用的就是这个量；
    写成 `slot_start_epoch(slot_id)` 会早整整一个时隙（15 s），预算守卫从此永不放行。
    """
    return slot_start_epoch(slot_id + 1, period)
```

`ft8/runtime.py`，在 `self.orchestrator = Orchestrator(...)`（`:175`）**之前**插：
```python
        # 级联解码：默认走快路径，按策略在同一时隙内升级到 WSJT-X 深路径。
        # clock 一律不传 —— CascadingDecoder 的默认 time.time 与 orchestrator 的
        # clock（`orchestrator.py:125`）同源，这正是 R20 要钉住的那个不变量。
        # cutoff **必须显式传**，且必须传 orchestrator 的那一个常量：级联的预算守卫
        # 与 orchestrator 判定 deadline miss 用的是同一条物理量，各自 2.5 只会在
        # 有人改了 `decision_cutoff` 之后悄悄分叉（见 cascade.py 里那段注记）。
        self._cascade = CascadingDecoder(
            self.decoder,
            CascadePolicy(),
            cutoff=DEFAULT_DECISION_CUTOFF_SECONDS,
            boundary_epoch_for=boundary_epoch_for_slot,
            ap_context=lambda: (sequencer.my_call, _qso_progress(sequencer.state)),
            requested=lambda: self._deep_decode_requested,
        )
```
`:175` 那句 `Orchestrator(` 的第一个实参从 `self.decoder` 改成 `self._cascade`。

> **不要写 `self.decoder = CascadingDecoder(self.decoder, …)`。** `runtime.py:319-322` 在关停时调
> `self.decoder.close()`，而 `CascadingDecoder` 没有 `close()`；那一行还裹在
> `with suppress(Exception)` 里，于是 AttributeError 会被**静默吞掉** —— worker 和共享内存段
> 就这样泄漏掉，不留任何痕迹。`self.decoder` 保持是那个可关闭的 `SupervisorDecoder`，
> 级联对象另存 `self._cascade`，只喂给 orchestrator。

`ft8/runtime.py` 顶部 import：`:19` 的 `from ft8.engine.orchestrator import FT8_PERIOD_SECONDS`
改成 `from ft8.engine.orchestrator import (DEFAULT_DECISION_CUTOFF_SECONDS, FT8_PERIOD_SECONDS,
boundary_epoch_for_slot)`；
新增 `from ft8.engine.cascade import CascadePolicy, CascadingDecoder`；
`:36` 的 `from ft8.engine.sequencer import Sequencer` 改成
`from ft8.engine.sequencer import QSOState, Sequencer`。

模块级加一个函数（`should_escalate` 只看 `qso_progress > 0`）：
```python
def _qso_progress(state: QSOState) -> int:
    """AP 上下文只在通联进行中成立：IDLE / DONE 记 0，其余阶段都算进行中。"""
    return 0 if state in (QSOState.IDLE, QSOState.DONE) else 2
```

`ft8/runtime.py` 的 `__init__` 里加一个字段（Task 14 的「深度解码本槽」开关会翻它；
默认 `False`，让 `should_escalate` 的 `escalate_on_request and requested` 一路保持关闭）：
```python
        self._deep_decode_requested = False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_dsp_decode.py tests/ft8/engine/test_orchestrator.py tests/ft8/test_server_wiring.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ft8/engine/dsp_decode.py ft8/engine/orchestrator.py ft8/runtime.py tests/ft8/engine/test_dsp_decode.py tests/ft8/engine/test_orchestrator.py
git commit -m "feat(ft8): wire the cascading decoder into the engine"
```

**P2 完成标志**：端到端跑一槽；弱信号触发升级；`deadline_risk` 计数可见；
`.venv/bin/python -m pytest tests/ft8 -q` 全绿。

---

# Phase P3 — 观测 / 配置 / 文档

## Task 12: latency 直方图加路径维度

**Files:**
- Modify: `ft8/engine/latency.py:29-72`
- Modify: `ft8/engine/dsp_decode.py:115-118`（唯一调用点；**Task 11 之后行号已漂移**，见下）
- Test: `tests/ft8/engine/test_latency.py`（扩展）

**Interfaces:**
- Consumes: `DecodeConfig.path`/`profile`/`threads`（`dsp_decode.py:114` 手上就有）。
- Produces: `LatencyHistogram.record(path: str, profile: int, threads: int, elapsed_seconds: float)`；
  快照键形如 `"fast/p3/t1"`、`"improved/p3/t3"`。

> **`dsp_decode.py` 的行号在 Task 11 之后已经漂了 —— 按内容找，不要按行号找。**
> Task 11 把原来的 `decode`（`:85-139`）拆成两行薄封装 + 一个新的 `decode_path`，
> 于是那段 `if self._histogram is not None: … record(…)` 整体下移了大约十行，
> `:115-118` 现在指在别处。**唯一可靠的定位方式是
> `grep -n "_histogram" ft8/engine/dsp_decode.py`** —— 生产代码里 `.record(` 只有
> 这一个调用点（我核过：`grep -rn "\.record(" ft8/` 只命中
> `dsp_decode.py:116`，`tests/test_support_board.py` 里那些是不相干的另一个
> `record`），而 `LatencyHistogram()` 的构造点在 `runtime.py:78`，本任务**不动**它。
> 顺带：这段代码搬进 `decode_path` 之后，`started = self._monotonic()` 也必须跟着搬
> —— 它和 `record` 是配对的两半，拆开就是一个恒为 0 或恒为负的测量值。

> **不要按 spec §10 的字面写 `record(path, escalated, elapsed)`。** 那个签名在这里
> **不可能有正确的调用点**：直方图唯一被记录的地方是 `SupervisorDecoder.decode` 内部
> （`dsp_decode.py:115-118`），而它跑在 `CascadingDecoder` **决定是否升级之前** —— 那一行
> 永远不知道本槽最终升没升级。真按 spec 写，`escalated` 只会恒为 `False`，
> `"deep+escalated"` 这个桶永远空着，而测试却因为直接调 `record` 而全绿：
> 一个「看着有、从不出现」的维度。升级率由 Task 13 的 `escalations` 计数回答，
> 单槽总耗时由 `DecodeBatch.elapsed_seconds`（Task 9 的两段之和）回答。
>
> **也不要丢掉 `profile`/`threads`。** NFR-002 原本要回答的就是「这个 profile/线程数
> 会不会威胁 2.5 s 截止」；把它们换掉，深路径调参就失去了唯一的线上证据。
> 所以是**加一个 path 维度**，不是换掉旧维度。

- [ ] **Step 1: 写失败测试**

追加到 `tests/ft8/engine/test_latency.py`：
```python
def test_histogram_separates_fast_from_deep() -> None:
    """同一次运行里两条路径各自的分布必须分开，否则快路径会把深路径的水位冲淡。"""

    histogram = LatencyHistogram()
    histogram.record("fast", 3, 3, 0.15)
    histogram.record("improved", 3, 3, 2.2)

    assert set(histogram.snapshot()) == {"fast/p3/t3", "improved/p3/t3"}
    assert histogram.snapshot()["fast/p3/t3"]["max_seconds"] == 0.15


def test_histogram_keeps_profile_and_threads_as_dimensions() -> None:
    """深路径调参要看得见 —— 同一个 path 下不同 profile/threads 仍是不同的桶。"""

    histogram = LatencyHistogram()
    histogram.record("improved", 3, 3, 2.2)
    histogram.record("improved", 1, 1, 3.23)

    assert set(histogram.snapshot()) == {"improved/p3/t3", "improved/p1/t1"}
```
并把既有用例的调用点同步改掉（**这一步是必须的，`record` 是位置参数**）：
- `test_histogram_snapshot_per_configuration`（`:54-57`）的三条 `record(3, 7, …)` /
  `record(0, 1, …)` → `record("improved", 3, 7, …)` / `record("improved", 0, 1, …)`，
  断言键 `{"p3/t7", "p0/t1"}` → `{"improved/p3/t7", "improved/p0/t1"}`；
- `test_histogram_rejects_negative_elapsed`（`:78`）→ `record("improved", 3, 7, -0.1)`；
- `test_supervisor_decoder_records_wall_elapsed`（`:84-99`）的断言键
  `["p3/t7"]` → `["improved/p3/t7"]`（`DecodeConfig(profile=3, threads=7)` 的默认
  `path` 是 `DecodePath.IMPROVED`）。

同文件末尾还有**两个带 `@pytest.mark.skip(reason="web layer arrives with the REST移植
(task 12)")` 的用例**（`test_health_exposes_latency_and_deadline_misses` 与
`test_health_omits_latency_when_not_wired`）：本 Task 一并**去掉 skip**，并把前者的
`histogram.record(3, 7, 0.4)` 改成 `histogram.record("improved", 3, 7, 0.4)`、断言键
`["p3/t7"]` 改成 `["improved/p3/t7"]`。

> **这两个用例现在**不可能**通过，原因不是 skip 里写的那个 —— 是它们引用了三个
> 本仓根本没有的名字。**（这是我核过的：`grep -rn "LeaseService" --include='*.py' .`
> 全仓**只**命中这两条被跳过的用例本身，没有任何定义或导入；`AuthService` 与
> `hash_password` 同样如此 —— `ft8/web/auth_shim.py:55` 有一句
> 「`AuthService`-shaped adapter」的注释，说的是 `PlatformAuthAdapter`，
> 名字只是形似。`AppState.lease` 的定义行更写着 `# unused: the platform's
> control role replaces the FT8 lease`。）
>
> 所以去 skip 之后看到的是 `NameError`，**不是**「这个文件里的 `_health` 不是那一个」——
> 正确的诊断是：这两条用例写在一个已被取代的 FT8 独立 auth/lease API 上。
> **不要**去找一个"能用"的 `_health`，也不要去实现 `LeaseService`。改成按
> **真实**的 `AppState` 字段形状造 state（`ft8/web/ft8_api.py:145-180`），
> 只补一个 import：
> ```python
> from ft8.web.ft8_api import _health
> ```
> 然后把两条用例的 state 构造换成（这是我在解释器里**实跑过**的形状，
> `_health` 对它的输出就是两条用例要的断言值）：
> ```python
>     state = SimpleNamespace(
>         safety=SimpleNamespace(health={"armed": False}),
>         lease=None,                                     # 平台的角色取代了 FT8 lease
>         auth=SimpleNamespace(session_count=lambda: 0),  # _health 只调 session_count()
>         latency=histogram,                              # 未接线的用例写 None
>         orchestrator=SimpleNamespace(counters=SimpleNamespace(deadline_misses=2)),
>     )                                                   # 未接线的用例写 None
> ```
> （实测这两条会得到 `{'decode_latency': {'p3/t7': {...'count': 1...}}, 'deadline_misses': 2}`
> 与 `{'safety': …, 'lease_held': False, 'sessions': 0}`，正好对得上它们原有的断言 ——
> **断言一个字都不用改**，改的只是"造 state 的那几行"。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_latency.py -v`
Expected: FAIL（`TypeError: record() missing 1 required positional argument`）

- [ ] **Step 3: 实现**

`ft8/engine/latency.py`：
```python
class LatencyHistogram:
    """Thread-safe per-configuration decode-latency counters (NFR-002)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._series: dict[tuple[str, int, int], _Series] = {}

    def record(
        self, path: str, profile: int, threads: int, elapsed_seconds: float
    ) -> None:
        """Add one wall-clock decode measurement for a configuration.

        ``path`` 是必需的维度，不是修饰：默认的 fast 路径约 0.15 s、深路径约 2.2 s，
        混在一个桶里会让快路径把深路径的尾部分布整个冲淡，而 NFR-002 要回答的
        恰恰是深路径离 2.5 s 截止还有多远。
        """

        if elapsed_seconds < 0:
            raise ValueError("elapsed must not be negative")
        with self._lock:
            series = self._series.setdefault((path, profile, threads), _Series())
            series.buckets[bucket_index(elapsed_seconds)] += 1
            series.count += 1
            series.max_seconds = max(series.max_seconds, elapsed_seconds)

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Return a JSON-ready copy keyed by ``"<path>/p<profile>/t<threads>"``."""

        labels = [f"<{bound:g}s" for bound in BUCKET_BOUNDS_SECONDS] + [
            f">={BUCKET_BOUNDS_SECONDS[-1]:g}s"
        ]
        with self._lock:
            return {
                f"{path}/p{profile}/t{threads}": {
                    "buckets": dict(zip(labels, series.buckets)),
                    "count": series.count,
                    "max_seconds": round(series.max_seconds, 3),
                }
                for (path, profile, threads), series in sorted(self._series.items())
            }
```
同时把类与 `_Series` 的 docstring 里 `(profile, threads)` 的说法改成
`(path, profile, threads)`（`latency.py:4-5`、`:31`），别留下与代码不符的注释。

`ft8/engine/dsp_decode.py:115-118` 的调用点改成：
```python
        if self._histogram is not None:
            self._histogram.record(
                str(config.path),
                config.profile,
                config.threads,
                self._monotonic() - started,
            )
```
（`DecodePath` 是 `StrEnum`，`str(config.path)` 就是 worker 协议里那个 `"fast"`/`"improved"`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_latency.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ft8/engine/latency.py ft8/engine/dsp_decode.py tests/ft8/engine/test_latency.py
git commit -m "feat(ft8): break decode latency down by path as well as profile and threads"
```

---

## Task 13: 级联计数器

**Files:**
- Modify: `ft8/engine/orchestrator.py:94-103`（`OrchestratorCounters` 加两个字段）与
  `:262` 附近（`decodes += 1` 那一处旁边累加）
- Modify: `ft8/runtime.py`（`cascade_stats` 只读属性）
- Modify: `ft8/web/ft8_api.py:840-845`（`_snapshot` 的 `orchestrator` 段）与 `:848-863`（`_health`）
- Test: `tests/ft8/engine/test_orchestrator.py`、`tests/ft8/engine/test_latency.py`（各扩展一条）

**Interfaces:**
- Consumes: `DecodeBatch.escalated`/`.path`（Task 9）、`CascadingDecoder.deadline_risk`（Task 10）。
- Produces:
  - `OrchestratorCounters.fast_decodes: int = 0`、`OrchestratorCounters.escalations: int = 0`
  - `Ft8Runtime.cascade_stats -> dict[str, int] | None`
  - `GET /api/ft8/snapshot` 的 `orchestrator` 段多两个键；`GET /api/ft8/health` 多一个 `cascade` 段

> **计数器必须放在「知道这个事实的那一层」，不要硬凑到一个对象上。**
> `Orchestrator` 手上没有 `CascadingDecoder` 的引用（`orchestrator.py:242` 只拿到
> `await self._decoder.decode(...)` 的返回值），所以它能数的**只有**返回值里带的东西：
> `batch.escalated` 和 `batch.path`。而「预算太紧所以没升级」这件事只发生在
> `CascadingDecoder._remaining_budget` 的 if 里（Task 10 `:1983-1988`），orchestrator
> 永远看不见。因此：
> - **orchestrator 数** `fast_decodes`、`escalations`；
> - **`CascadingDecoder` 数** `deadline_risk`（Task 10 已有），由 `runtime.cascade_stats` 暴露；
> - **不设 `escalation_skipped_budget`**：spec §10 把它和 `deadline_risk` 并列，但在当前
>   实现里两者每次都是同一个事件、同一个自增量（Task 10 `:1984` 是唯一增量点）——
>   两个名字数一个数，读的人只会以为漏了一个。等真的出现第二种跳过原因（例如
>   deep 路径超时）再拆。Task 15 负责把 spec §10 改成只列 `deadline_risk`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/ft8/engine/test_orchestrator.py`。先给既有的 `make_batch` 与 `FakeDecoder`
各加两个参数（**带默认值，既有 20 多处调用点一个都不动**）：
```python
def make_batch(
    slot_id: int, texts: list[str] | None = None, *,
    path: DecodePath = DecodePath.IMPROVED, escalated: bool = False,
) -> DecodeBatch:
    return DecodeBatch(
        slot_id=slot_id,
        path=path,
        results=tuple(make_result(text, slot_id=slot_id) for text in (texts or [])),
        overflow=False,
        elapsed_seconds=0.25,
        deadline_missed=False,
        escalated=escalated,
    )


class FakeDecoder:
    def __init__(self, clock, *, texts=None, delay=0.0, error=None,
                 wrong_slot=False, path=DecodePath.IMPROVED,
                 escalated=False) -> None:
        ...
        self.path = path
        self.escalated = escalated
```
并把 `FakeDecoder.decode` 末尾的 `return make_batch(..., self.texts)` 改成
`return make_batch(slot_id + 1 if self.wrong_slot else slot_id, self.texts,
path=self.path, escalated=self.escalated)`。

然后加用例：
```python
def test_counters_split_fast_from_escalated_slots() -> None:
    """fast/escalated 必须是两个互斥的桶 —— 升级率 = escalations / decodes。"""

    harness = Harness(epoch=7.5)
    harness.decoder.path = DecodePath.FAST
    harness.decoder.escalated = False
    harness.decoder.texts = ["CQ K1ABC FN42"]
    harness.run(decodes=3)

    counters = harness.orchestrator.counters
    assert counters.decodes == 3
    assert counters.fast_decodes == 3
    assert counters.escalations == 0


def test_escalated_slot_is_counted_as_escalation_not_as_fast() -> None:
    harness = Harness(epoch=7.5)
    # 合并后的批次：path 仍是 FAST（深路径一条没多），但升级**发生过**
    harness.decoder.path = DecodePath.FAST
    harness.decoder.escalated = True
    harness.decoder.texts = ["CQ K1ABC FN42"]
    harness.run(decodes=1)

    counters = harness.orchestrator.counters
    assert counters.escalations == 1
    assert counters.fast_decodes == 0, (
        "升级过的槽不算 fast —— 否则 fast_decodes 失去意义，等于 decodes"
    )
```
追加到 `tests/ft8/engine/test_latency.py`（沿用该文件里 `_health` 的导入与 `state` 造法
—— **Task 12 已经把两条被跳过的用例改成真实 `AppState` 形状了，照它们的写法来**）：
```python
def test_health_exposes_cascade_counters_when_the_cascade_is_wired() -> None:
    """预算挡下升级的次数必须能从 health 读到 —— 这正是「阈值是不是太紧」的证据。"""

    state = SimpleNamespace(
        safety=SimpleNamespace(health={"armed": False}),
        lease=None,
        auth=SimpleNamespace(session_count=lambda: 0),
        latency=None,
        orchestrator=None,
        # **普通 dict，不是 lambda。** `_health` 用 `getattr(runtime, "cascade_stats", None)`
        # 取值后**直接**塞进 `health["cascade"]`（见下面实现），而 `cascade_stats` 在
        # `Ft8Runtime` 上是 `@property`。写成 lambda 的话 `_health` 原样返回一个函数对象，
        # 断言 `["deadline_risk"]` 就变成 `TypeError: 'function' object is not subscriptable`
        # —— 一个从第一行就注定跑不过的用例（R77 的形状）。轻量替身只需长得像**读完之后的结果**。
        runtime=SimpleNamespace(decoder=None, cascade_stats={"deadline_risk": 4}),
    )
    assert _health(state)["cascade"]["deadline_risk"] == 4


def test_cascade_stats_property_covers_both_branches() -> None:
    """`cascade_stats` 的两个分支都要被**这个属性本身**跑到，而不是被替身绕过。

    上面那条用例用的是 SimpleNamespace，证明不了 `Ft8Runtime.cascade_stats` 存在。
    引擎未启动时 `_cascade` 还没有（`object.__new__` 构造出的实例就是这种形状，
    `tests/ft8/test_server_wiring.py:136` 已有先例），必须返回 None 而不是抛
    `AttributeError` —— 否则 `/api/ft8/health` 在引擎没起来时 500。

    用 `getattr(self, "_cascade", None)` 而不是 `self._cascade is None` 就是为了这个：
    Task 11 的 `__init__` 建了 `self._cascade`，但「引擎未启动」这个状态在真实运行里
    是存在的（构造到 `start()` 之间、以及构造路径中途失败），不能只靠 `__init__` 兜。
    """

    from ft8.runtime import Ft8Runtime

    unwired = object.__new__(Ft8Runtime)
    assert unwired.cascade_stats is None

    wired = object.__new__(Ft8Runtime)
    wired._cascade = SimpleNamespace(deadline_risk=4)
    assert wired.cascade_stats == {"deadline_risk": 4}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_orchestrator.py tests/ft8/engine/test_latency.py -v`
Expected: FAIL（`AttributeError: 'OrchestratorCounters' object has no attribute 'fast_decodes'`；
`KeyError: 'cascade'`）

- [ ] **Step 3: 实现**

`ft8/engine/orchestrator.py:94-103`：
```python
@dataclass(slots=True)
class OrchestratorCounters:
    """Running totals behind the NFR-002 deadline visibility."""

    slots_started: int = 0
    slots_skipped: int = 0
    decodes: int = 0
    deadline_misses: int = 0
    decode_errors: int = 0
    # 快路径**单独**解完的槽数（没有升级，或升级了但结果仍走快路径的情况见下）。
    fast_decodes: int = 0
    # 真的跑过深路径的槽数。升级率 = escalations / decodes，是判断阈值松紧的主指标。
    escalations: int = 0
```
在 `self.counters.decodes += 1`（`:262`）旁边累加 —— 必须在 `batch.slot_id` 校验**之后**，
和一个合法批次同生共死：
```python
        self.counters.decodes += 1
        if batch.escalated:
            self.counters.escalations += 1
        elif batch.path is DecodePath.FAST:
            # 只在**没升级且答案是快路径**时才算 fast。快路径整个失败而补跑深路径
            # 的那种批次（CascadingDecoder 的 fallback）两者都不算 —— 它不是快路径的
            # 功劳，也没升级，所以 fast_decodes + escalations <= decodes 是有意的。
            self.counters.fast_decodes += 1
```
（`orchestrator.py` 顶部要加 `from ft8.core.models import DecodePath`，若还没有。）

`ft8/runtime.py` 加只读属性（`_cascade` 是 Task 11 建的私有字段，**不要**让 web 层直接碰它）：
```python
    @property
    def cascade_stats(self) -> dict[str, int] | None:
        """级联解码器的运行期计数；引擎未启动时为 None。"""

        cascade = getattr(self, "_cascade", None)
        if cascade is None:
            return None
        return {"deadline_risk": cascade.deadline_risk}
```
> **`getattr(self, "_cascade", None)` 而不是直接 `self._cascade`**：上面那条
> `test_cascade_stats_property_covers_both_branches` 用 `object.__new__(Ft8Runtime)`
> 造了一个「`__init__` 还没跑完」的实例来钉「未启动时返回 None」。直接读属性会在那里
> 抛 `AttributeError` —— 而真实运行里同样的形状是存在的（构造路径中途失败、`start()`
> 之前），读者拿到的是一个 500 而不是一个没有 `cascade` 段的 health。

`ft8/web/ft8_api.py`：`_snapshot` 的 `orchestrator` 段（`:840-845`）加两个键；
`_health`（`:848-863`）加一段 —— 用 `getattr` 而不是 `isinstance`，这样没装级联
（或测试里的轻量替身）时是「不出现」而不是抛错：
```python
    health["decode_latency"] = ...          # 既有
    ...
    runtime = getattr(state, "runtime", None)
    cascade_stats = getattr(runtime, "cascade_stats", None)
    if cascade_stats is not None:
        health["cascade"] = cascade_stats
```
（**`cascade_stats` 是 property，不是方法** —— 这一点没有第二种写法：`_health` 用
`getattr` 取到值后直接放进 `health["cascade"]`，取到的必须是那个 dict 本身；
`test_health_exposes_cascade_counters_when_the_cascade_is_wired` 的替身也因此给的是
普通 dict。`_snapshot` 里 `counters.fast_decodes` / `counters.escalations` 直接进 dict。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/ft8/engine/test_orchestrator.py tests/ft8/engine/test_latency.py tests/ft8/web/test_ft8_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ft8/engine/orchestrator.py ft8/runtime.py ft8/web/ft8_api.py tests/ft8/engine/test_orchestrator.py tests/ft8/engine/test_latency.py
git commit -m "feat(ft8): count fast decodes and escalations, expose the cascade budget counter"
```

---

## Task 14: 设置项与「深度解码本槽」开关

**Files:**
- Modify: `ft8/platform/settings.py:22-45`（`Ft8Settings` 加 CascadePolicy 的可持久化子集，
  并扩展 `validate()`）
- Modify: `ft8/web/ft8_api.py:382-383`（`allowed` 设置键）+ 紧随其后的逐键类型校验
- Modify: `ft8/runtime.py`（把设置接成活的 `CascadePolicy`；`_on_batch` 里清一次性标志）
- Modify: `static/ft8/settings.js`、`static/ft8/main.js`（设置项 + 一次性开关）
- Test: `tests/ft8/platform/test_settings.py`、`tests/ft8/web/test_ft8_api.py`（扩展）

**Interfaces:**
- Consumes: `CascadePolicy` 字段（Task 8）；`CascadingDecoder.set_policy`（Task 10）；
  `runtime._deep_decode_requested` 与 `_cascade`（Task 11）。
- Produces: 设置可读写 `escalate_on_ap_context`/`escalate_on_weak`/`min_decodes`/`min_peak_snr`
  （外加 `escalate_on_request`，见下）；一条让「深度解码本槽」生效的路径。

> **这里的三个「必须」，缺一个这个 Task 就是装饰性的：**
> 1. **设置必须接到活的 `CascadePolicy` 上。** Task 11 装配时写的是 `CascadePolicy()`（全默认值）——
>    如果 Task 14 只是往 `settings.py` 加字段、改 `allowed` 集合，那么改设置对解码**没有任何影响**，
>    功能看着有、其实不通。所以本 Task 要动 `ft8/runtime.py`：装配那行改成从设置构造策略，
>    并在设置变更时调 `self._cascade.set_policy(...)`（`set_policy` 由 Task 10 提供）。
> 2. **一次性开关必须真的是一次性的。** `requested` 只在 `should_escalate` 里以
>    `policy.escalate_on_request and requested` 的形式起作用，`escalate_on_request` 默认 `False` ——
>    所以那个持久开关必须把它打开，否则按钮按下去毫无效果。更关键的是**清理**：置 True 之后
>    必须有人清回 False，否则从那一刻起**每个时隙**都升级，每槽稳定多付 ~2.2 s 深解码 ——
>    一个「按一次、此后永久变慢」的按钮。清理点选 `runtime._on_batch`（`runtime.py:209`，
>    每批次一次）：批次回来就说明这一槽的 `decode` 已经结束，语义正好是「只对这一槽有效」。
>    该槽被跳过（`decode` 根本没跑）时标志留到下一个真正解码的槽 —— 可接受，用户按的是本槽，
>    而当时没有槽可解。
> 3. **类名是 `Ft8Settings`。** `ft8/platform/settings.py` 里没有 `FTSettings`，而且它是
>    `@dataclass(frozen=True)`，**没有** `.replace()` 方法 —— 改值要用模块级
>    `dataclasses.replace(...)`。

- [ ] **Step 1: 写失败测试**

```python
import dataclasses


def test_policy_settings_round_trip() -> None:
    from ft8.platform.settings import Ft8Settings

    base = Ft8Settings()          # 全部字段都有默认值（settings.py:23-35）
    # Ft8Settings 是 frozen dataclass，**没有** .replace() —— 用模块级 dataclasses.replace。
    updated = dataclasses.replace(base, escalate_on_weak=False, min_peak_snr=-18)
    assert updated.escalate_on_weak is False
    assert updated.min_peak_snr == -18


def test_new_policy_defaults_match_cascade_policy() -> None:
    """「从未存过设置」与「存了一份默认值」不能走出两条不同的升级策略。"""

    from ft8.engine.cascade import CascadePolicy
    from ft8.platform.settings import Ft8Settings

    settings = Ft8Settings()
    policy = CascadePolicy()
    assert settings.escalate_on_ap_context is policy.escalate_on_ap_context
    assert settings.escalate_on_weak is policy.escalate_on_weak
    assert settings.min_decodes == policy.min_decodes
    assert settings.min_peak_snr == policy.min_peak_snr
    assert settings.escalate_on_request is policy.escalate_on_request


@pytest.mark.parametrize("bad", [{"min_peak_snr": 1}, {"min_peak_snr": -200},
                                 {"min_decodes": -1}])
def test_out_of_range_policy_settings_are_rejected(bad: dict) -> None:
    """越界值必须在**写**的时候就被拒 —— min_peak_snr 写成正数会让每槽都升级。"""

    from ft8.platform.settings import Ft8Settings

    settings = dataclasses.replace(Ft8Settings(), **bad)
    with pytest.raises(ValueError):
        settings.validate()
```
追加到 `tests/ft8/web/test_ft8_api.py`，照既有的 `test_station_always_on_round_trip`
（`:176-181`）写 —— **三个端点面都要断言**，这才是那三处里少改一处时的证伪：
```python
def test_station_cascade_policy_round_trip():
    client = _client()
    assert client.get("/api/ft8/station").json()["min_peak_snr"] == DEFAULT_MIN_PEAK_SNR

    r = client.put("/api/ft8/station", json={"min_peak_snr": -18,
                                             "escalate_on_weak": False})
    assert r.status_code == 200
    # PUT 的返回体也必须带上 —— 少了它前端拿返回值刷新界面就会回弹
    assert r.json()["min_peak_snr"] == -18

    after = client.get("/api/ft8/station").json()
    assert after["min_peak_snr"] == -18
    assert after["escalate_on_weak"] is False


def test_station_rejects_out_of_range_cascade_policy():
    assert _client().put("/api/ft8/station",
                         json={"min_peak_snr": 5}).status_code == 422
    assert _client().put("/api/ft8/station",
                         json={"escalate_on_weak": "yes"}).status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/ft8/platform/test_settings.py -v`
Expected: FAIL（`TypeError: replace() got an unexpected keyword argument 'escalate_on_weak'`）

- [ ] **Step 3: 实现**

`ft8/platform/settings.py`：在 `Ft8Settings` 里加五个字段。默认值**直接引用 `CascadePolicy`
的默认常量**，不要各抄一份数字 —— 抄成两份，日后改一处就漂：
```python
    # 级联升级策略（CascadePolicy 的可持久化子集，见 ft8/engine/cascade.py）。
    escalate_on_ap_context: bool = True
    escalate_on_weak: bool = True
    min_decodes: int = DEFAULT_MIN_DECODES
    min_peak_snr: int = DEFAULT_MIN_PEAK_SNR
    escalate_on_request: bool = False   # 允许「深度解码本槽」一次性按钮
```
（`from ft8.engine.cascade import DEFAULT_MIN_DECODES, DEFAULT_MIN_PEAK_SNR`。
`cascade.py` 只依赖 `ft8.core.models`，不会与 settings 成环。）

**同时扩展 `Ft8Settings.validate()`（`settings.py:37-45`）** —— `save()`（`:71`）会调它，
而 `load()`（`:56`）不会，所以它是持久化写的唯一一道闸。新增四个值的约束检查：
```python
        if not isinstance(self.escalate_on_ap_context, bool):
            raise ValueError("escalate_on_ap_context must be a boolean")
        if not isinstance(self.escalate_on_weak, bool):
            raise ValueError("escalate_on_weak must be a boolean")
        if not isinstance(self.escalate_on_request, bool):
            raise ValueError("escalate_on_request must be a boolean")
        if self.min_decodes < 0:
            raise ValueError("min_decodes must not be negative")
        # 上限 0 是刻意的：正数的 min_peak_snr 意味着「任何真实信号都算弱」，
        # 也就是每个时隙都升级 —— 那不是策略，是 bug。
        if not -120 <= self.min_peak_snr <= 0:
            raise ValueError("min_peak_snr must be between -120 and 0")
```
`min_peak_snr` 的范围不是装饰：它是 `should_escalate` 里唯一一个「越小越容易升级」的
阈值，写成正数会让**每个时隙**都升级（每槽白付 ~2.2 s），而这一点在运行时毫无征兆。

`ft8/web/ft8_api.py` 里要改的是**三处**，少一处就有一个具体的坏症状：

1. `:382-383` 的 `allowed` 集合加这五个键 —— 少这处：前端一保存就被
   `422 invalid_setting` 打回（该端点是白名单语义）。
2. `:362-369` 的 `station_view` GET 返回字典加这五个键 —— 少这处：设置**存进去了、
   但刷新页面后全是默认值**，用户会以为没保存成功，然后反复重按。
3. `:421-426` 的 `station_update` 成功响应 `_ok({...})` 加这五个键 —— 少这处：
   PUT 的返回体与 GET 不一致，前端如果拿 PUT 的返回去更新界面就会回弹。

三处都用与既有键相同的 `getattr(settings, "<name>", <默认常量>)` 写法
（默认值直接引 `cascade.DEFAULT_MIN_DECODES` / `DEFAULT_MIN_PEAK_SNR`，别抄数字）。

并在逐键校验里照 `engine_always_on` 的写法把布尔/整型钉住 ——
`save()` 的 `ValueError` 已经被 `:399-401` 的 `except ValueError` 转成 422 了，
所以 `validate()` 那道闸是接得上的。

`ft8/runtime.py`：加一个显式映射（字段名一一对应，**不做任何 getattr 兜底** ——
名字写错要当场 AttributeError，而不是静默用回默认值，那正是 R23 的教训）：
```python
def _policy_from_settings(settings: Ft8Settings) -> CascadePolicy:
    """设置里的五个升级策略字段 → CascadePolicy。"""

    return CascadePolicy(
        escalate_on_ap_context=settings.escalate_on_ap_context,
        escalate_on_weak=settings.escalate_on_weak,
        min_decodes=settings.min_decodes,
        min_peak_snr=settings.min_peak_snr,
        escalate_on_request=settings.escalate_on_request,
    )
```
再加一个入口，把新策略推给**正在跑**的级联解码器：
```python
    def set_cascade_policy(self, settings: Ft8Settings) -> None:
        """把持久设置里的升级策略推给活着的级联解码器。"""

        if self._cascade is not None:
            self._cascade.set_policy(_policy_from_settings(settings))
```
调用点两处：

1. **`/station` 保存成功之后** —— 具体位置是 `ft8_api.py:400-402` 那一小段，即
   `store.save(updated)` 的 `try/except` 之后、`runtime = getattr(state, "runtime", None)`
   拿到 runtime 的那一行下面，和 `runtime.sequencer.my_call = …` 并排。放在那里而不是
   函数开头，是因为只有保存**成功**了才该把新策略推下去。
2. **引擎启动时** —— 否则每次重启都退回 `CascadePolicy()` 的默认值，用户改过的阈值只在本次
   进程内有效。这一处正是上面「必须 1」说的问题：Task 11 装配时写死的就是 `CascadePolicy()`。

> **但「在 `runtime.start()` 里读设置」是做不到的 —— `Ft8Runtime` 手上根本没有设置。**
> 我核过：`ft8/runtime.py` 全文对 `settings` **零引用**；`Ft8SettingsStore` 是
> `server.py:3103` 造出来、挂在 `AppState.settings_store` 上的（`ft8_api.py:159`）。
> 所以照字面写，实现者要么造一个 `` Ft8SettingsStore(self._data_dir / "settings.json") ``
> —— 那是**第二个 store 实例**，路径今天恰好等价（`_ft8_data_dir` 就是
> `_writable_runtime_dir()/"ft8"`）而明天不等价，一个静默的双源头；要么把 `start()`
> 改成接受参数，那又和「启动由 mode controller 触发」的既有结构打架。
>
> **按这个文件既有的注入惯例来。** `server.py:3179` 的 `Ft8Runtime(...)` 已经在注入
> `on_decode` / `on_waterfall` / `queued_frames`，紧挨着它的 `_ft8_always_on()`
> （`server.py:3190-3195`）就是「runtime 需要一个设置派生的旋钮」的现成样板：
> 一个读 store 的闭包，带 try/except 兜底。照抄那个形状：
> - `Ft8Runtime.__init__` 增加一个**带默认值**的关键字参数
>   `cascade_policy_provider: Callable[[], CascadePolicy] | None = None`
>   （带默认值是因为 `tests/ft8/test_server_wiring.py:136` 用 `object.__new__` 构造，
>   但 `server.py:3179` 是**唯一**一处真实构造点 —— 我 grep 过全仓，所以这个签名改动
>   的爆炸半径就是一个文件）；
> - `runtime.start()` 里构造级联时用 `self._cascade_policy_provider` 的返回值，
>   provider 缺席时退回 `CascadePolicy()`；
> - `server.py:3179` 传
>   `cascade_policy_provider=lambda: _policy_from_settings(_ft8_settings_store.load())`。
>
> `set_cascade_policy(settings)` 保留，给 `/station` 保存路径用 —— 那条路径手上**有**
> settings（`updated`），推送给活着的解码器即可，不需要 provider。

`self._cascade` 在引擎未启动时是 `None`，所以判空不能省 —— 与 `self.decoder`
同样的空值约定（`_policy_from_settings` 本身不需要判空，它只做字段搬运）。

前端：`settings.js` 加这几项；「深度解码本槽」按钮走**一次性动作**，不复用 `PUT /station`
（那存的是持久设置，而这个标志是运行时的一次性事件）：加一条小端点
（如 `POST /engine/deep-slot`）把 `runtime._deep_decode_requested` 置 `True`，
由 `runtime._on_batch`（`runtime.py:209`）清回 `False`。

> **新端点照抄邻居的守卫，不要自创一套。** 路由挂在 `APIRouter(prefix="/api/ft8")`
> （`ft8_api.py:327`）下，所以最终路径是 `/api/ft8/engine/deep-slot`。它是个**变更**动作
> （按一下就会让本槽多烧 ~2.2 s 的 CPU），所以两处都要与 `station_update`/`set_mode` 一致：
> 依赖用 `Depends(require_lease)`（**不是** `require_session` —— 后者只要求登录，
> 会把「谁都能按」开出去），函数体第一行 `await validate_mutation(request)`。
> `require_session` → `require_lease` 是本 Task 最容易少写的一处，而少写了不会报错，
> 只会让端点在角色上比它的每一个邻居都松。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/ft8/platform/test_settings.py tests/ft8/web/test_ft8_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ft8/platform/settings.py ft8/web/ft8_api.py ft8/runtime.py static/ft8/settings.js static/ft8/main.js tests/ft8/platform/test_settings.py tests/ft8/web/test_ft8_api.py
git commit -m "feat(ft8): make the cascade policy configurable from the UI"
```

---

## Task 15: 文档与 SDD 同步

**Files:**
- Modify: `docs/ft8-w103d-decode-perf-analysis.md`（回填 spike 数字与最终 `S_MIN`）
- Modify: `docs/PROJECT_MAP.md`、`docs/DOCUMENT_INDEX.md`（登记 spec 与本计划）
- Modify: `AGENTS.md`（`ft8/` 结构表增补 fast path 与 `CascadingDecoder`）
- Modify: `tests/README.md`（用例/模块计数按实际更新）
- Modify: `SDD/09-*.md`、`SDD/13-*.md`（解码路径与性能决策）
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: 前 14 个 Task 的成果。
- Produces: 文档与实现一致，`release_check.py` 与 `sdd_context.py check --staged` 干净。

- [ ] **Step 1: 更新文档**

逐项写入实际数字。2026-10-13 A53 spike（`ssh w103d.vlsc.net`，`/tmp/ft8ab/`，全部
只读生产机、`nice -n 19`）已经给出这些，直接引用：

- **编译开关扫描**（同输入各 7 次中位）：`-O3` 153 ms / `-ffast-math` 149 ms /
  `-mcpu=cortex-a53 -mtune=cortex-a53` **162 ms（负收益）** / `-march=armv8-a+simd` 152 ms。
  objdump：三者向量化完全相同；kiss_fft 靠 gcc 自动向量化拿到 NEON，
  `ft8/decode.c`（Costas 同步搜索）在任何开关下都保持标量。
- **相位拆分**（计时器插桩，非 perf——盒子上没有 perf）：
  LDPC 候选循环 71.9 ms（50.7%）、FFT 累加 45.6 ms（32.1%）、同步搜索 24.4 ms（17.2%）。
- **旋钮扫描**：全部 11 个变体在合成强信号上都是 5/5 —— 正因如此，
  「解码条数不变」**不能**当作任何旋钮安全的证据；写入文档时必须连带这句限定。
- **线程上限（Amdahl 估算，未实现）**：只并行 LDPC → 4 线程约 99 ms（1.55×）；
  连同步搜索一起并行 → 约 81 ms（1.9×）。**这条是估算不是实测**，文档里必须标注。
- **快路径最终旋钮**：全部停在上游默认（`min_score=10`、LDPC 25、osr 2/2），
  理由见 Task 4 的注释块。
- **升级阈值**：`min_peak_snr = -26`，由第三轮 A/B 的 −24 dB 完整点与 −30 dB 崩塌点夹出。
- 真实机上实测的 fast / 级联 wall 与 `escalations` 计数。

同时把 spike 的「结论」写进 `docs/ft8-w103d-decode-perf-analysis.md` 的新一节
（§10 或并入 §8），并更新 spec 的 O-1 / O-2 状态。

**还要把 spec 正文里三处与实现不符的地方改掉** —— 它们是实现期间由计划作出的裁定，
不回填的话，下一个读 spec 的人会照着已经证伪的说法再写一遍：

1. §10 观测（spec `:248-250`）：现在写的是「`LatencyHistogram` 增加维度：每条记录带
   `path`（fast/deep/escalated）与 `escalated` 标志」。**这条要整段改写**，因为
   `path (fast/deep/escalated)` 与 `escalated` 标志**都不是真的**：
   - 实际的 path 取值是 `fast` / `improved`（`DecodePath` 的两个值），**没有 `escalated`**
     —— `escalated` 是**一个布尔字段**（Task 9 加在 `DecodeBatch` 上），不是 path 的一种；
   - 直方图里也**没有 `escalated` 维度**，而且不能有：唯一记录它的地方在
     `SupervisorDecoder.decode` 内部，那是 `CascadingDecoder` **决定是否升级之前**，
     那一行永远不知道本槽最终升没升级。真按 spec 写，这个桶会永远空着 ——
     「看着有、从不出现」（Task 12 的说明块整段讲的就是这件事）。
   所以改成：`LatencyHistogram.record(path, profile, threads, elapsed_seconds)`，
   桶键 `<path>/p<profile>/t<threads>`（`path ∈ {fast, improved}`），
   并写明升级率由**另一个**量回答 —— `escalations` 计数，不是直方图维度。
   同一条里**删掉 `escalation_skipped_budget`**（spec `:252`），只留 `deadline_risk`：
   当前实现里两者每次都是同一个事件、同一个自增量（Task 10 唯一的增量点），
   两个名字数一个数只会让读的人以为漏了一个（理由见 Task 13 的说明块）。
2. §5.4（spec `:150` 写的就是 `clock: Callable[[], float] = time.monotonic`）：
   `CascadingDecoder` 的 `clock` 默认必须是 **`time.time`** ——
   `orchestrator.py:125` 的 `clock` 与 `:225` 的 `boundary_epoch` 都是 epoch 秒，
   混用会让预算守卫在真实运行里永不触发（Task 10 的说明块）。
3. §6（spec `:196` 与 `:244` 的表格两处都写着 `(frequency, dt, text)`）：合并键是
   **`(frequency, dt)` 带容差**（`_FREQ_TOL=5.0` Hz / `_DT_TOL=0.15` s），
   不是 `(frequency, dt, text)` —— 快路径把没还原的呼号写成 `<...>`、深路径还原成真呼号，
   文本本来就会不同，拿文本当键等于把同一条信号数两遍（Task 9 的
   `test_hash_placeholder_is_merged_not_duplicated` 钉住了这一点）。**两处都要改。**

- [ ] **Step 2: 跑文档一致性检查**

Run:
```bash
.venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
.venv/bin/python .agents/skills/dual-platform-release/harness/release_check.py
```
Expected: 均干净

（用 `.venv/bin/python`：这台机器上没有裸 `python`，`python3` 也未必是装了依赖的那一个。）

- [ ] **Step 3: 全量回归**

Run（两条都要，它们覆盖互不相交的套件）：
```bash
.venv/bin/python -m pytest tests/ft8 -q          # FT8 套件（unittest 收不到）
.venv/bin/python -m unittest discover -s tests   # 仓库其余套件
```
Expected: 全绿。**不要在这里写死用例数** —— 把 `tests/README.md` 的模块/用例计数更新成
实测值即可（AGENTS.md 明确要求本文件不复述那个数字，因为它曾经长期停在 1369/67）。

- [ ] **Step 4: 提交**

```bash
git add docs/ft8-w103d-decode-perf-analysis.md docs/PROJECT_MAP.md \
        docs/DOCUMENT_INDEX.md AGENTS.md tests/README.md SDD/09-architecture-overview.md \
        SDD/13-feasibility-assessment.md CHANGELOG.md \
        docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md \
        docs/superpowers/plans/2026-10-13-ft8-fast-path-cascade.md
git commit -m "docs(ft8): sync SDD, AGENTS and the perf analysis with the fast path"
```

> **不要用 `git add -A`。** 仓库根有一个**与本工作无关的未跟踪目录 `fleet/`**，
> 里面是 14.8 MB 的 `frpc` 二进制（2026-10-11 出现，从未被提交过）。`git add -A`
> 会把它一并扫进这个文档提交 —— 一个 14 MB 的无关二进制进了仓库历史就再也拿不掉了。
> 所以上面逐个点名：本 Task 确实要改的文件（含 spec 与 plan —— spec 的 O-1/O-2 与
> 三处实现订正就在 Step 1 里，plan 一直未提交）。
> 提交前用 `git status --porcelain` 核一遍：**暂存区里不该出现 `fleet/`**，
> 也不该出现任何 `ft8/dsp/` 下的文件（那些是 Task 7 的产物，早已提交）。


---

## 自审记录

- **spec 覆盖**：§4 架构 → Task 2/3/5；§5.1 native → Task 1/2/3/4；§5.2/5.3 contract → Task 3/5；
  §5.4 `CascadingDecoder` → Task 8/9/10；§5.5 接线 → Task 11；§6 合并 → Task 9；
  §7 判据 → Task 8；§8 预算守卫 → Task 10；§9 错误处理 → Task 4/5/10；
  §10 观测 → Task 12/13；§11 测试 → 每个 Task 的 Step 1；§12 决策 → 各 Task 的注释与测试；
  §13 O-1/O-2 与 R-1..R-4 → Task 7/10/15 与 Review Focus。
- **类型一致性**：`DecodePath.FAST`（Task 5 定义，Task 5/6/8/9/10/11 使用）；
  `CascadePolicy`/`should_escalate`/`merge_batches`/`CascadingDecoder`（Task 8/9/10 定义与使用）；
  `decode_path(slot_id, samples, path, *, threads)`（Task 10 消费，Task 11 定义）。
- **Review Focus 落点**：①→Task 9；②→Task 4；③→Task 5；④→Task 10；⑤→Task 4。
- **spike 回填（2026-10-13）**：初稿在 Task 4 里写了 `ftx_find_candidates(..., min_score=0)`
  ——spike 证明这正是**把快路径做慢**的写法（score 降到 5 就让候选饱和到上限、
  154→281 ms）。已改为 `FAST_MIN_SCORE 10`（上游默认）并加测试钉死。
  同时按 spike 结论**明确不采用**任何编译开关（`-mcpu=cortex-a53` 实测负收益、
  `-ffast-math` 风险不值 4 ms），并把 `S_MIN` 从 -20 校准到 **-26**
  （第三轮 A/B：-24 完整、-30 崩塌）——-20 会在实测已知完整的时隙上白烧 2.2 s。
