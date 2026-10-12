# FT8 M1 盒子基线（W103D × FT-710）

- **日期**：2026-10-12
- **目的**：规格 §7 的三大风险定论——aarch64 编译、44.1k→12k 重采样链、4×A53 解码性能
- **机器**：ZTE W103D（Amlogic S905L3A，4×Cortex-A53，2 GB）、Armbian（Debian 12）、
  内核 aarch64；盒子 `192.168.1.217`，FT-710 接于 `/dev/ttyUSB0` + C-Media USB 音频（44.1 kHz）
- **代码**：mrrc_modern `feat/ft8`（分支起点 `5bc0f27` + 本里程碑提交）

## 1. 构建（`ft8/dsp/build.sh`，设备原生编译）

| 项 | 值 |
| --- | --- |
| 工具链 | gfortran 12.2.0（Debian 12.2.0-14+deb12u1）、cmake 3.25.1、libfftw3-dev 3.3.10 |
| 依赖补装 | **`libboost-dev`**（`crc13/14.cpp` 需要 `<boost/crc.hpp>`——原 CMake 未声明，已改为 `find_package(Boost REQUIRED)`，缺 Boost 现在在 configure 期报错） |
| 全量编译 | **2 分 02 秒**（4 jobs；user 4m03s） |
| 产物 | `ft8/dsp/build/libwsjt_core.so`，989 840 B，ELF class 2 / **machine 183（AArch64）** |
| 幂等 | 源 digest 未变时秒回；`build/` 从别的树同步过来（CMakeCache 路径不符）会自动清理重建 |

软件源可用性：`gfortran`/`libfftw3-dev`/`cmake`/`libboost-dev` 全部在 Armbian 自带源中，
无需第三方源。Python 侧 **arm64 wheel 全部可用**：scipy 1.17.1（盒子 venv 实测安装）、
soxr 1.1.0、pytest 9.1.1。

## 2. 解码回归（合成信号）

```
tests/ft8/dsp/test_ft8_standard_decode.py + test_ft8_encode.py  →  33 passed（4m04s，含 fresh build）
tests/ft8/dsp/ 全量（本地 macOS）                                →  104 passed, 1 skipped, 1 xfailed
tests/ft8/core/ 全量（本地 macOS，含真实 worker spawn）          →  218 passed
```

## 3. 重采样链验证（**规格 §7-2 的判据**）

`binding.encode("CQ BG1SB ON80", 1200 Hz, 48k)` → 两条链解码：

| 链路 | 结果 | 耗时 |
| --- | --- | --- |
| direct 48k→12k（对照） | `['CQ BG1SB ON80']` | 1.83 s |
| **device chain 48k→44.1k→12k**（真实：TX 模型 → FT-710 设备率 → RX tap） | `['CQ BG1SB ON80']` | 1.97 s |

**结论：设备链解码成立**——44.1 kHz 设备率不是解码障碍。

## 4. 单时隙解码性能（i9 基准，盒子实测）

完整矩阵（profile × threads，cycles=1，每格 5/5 成功）：

| profile | threads | median 耗时 |
| --- | --- | --- |
| 2 | 2 | 2.480 s |
| 2 | 4 | 2.306 s |
| **3** | **1** | 3.148 s |
| **3** | **2** | 2.458 s |
| **3** | **3（生产 Auto = 4核−1）** | **2.140 s** |
| 3 | 4 | 2.292 s |
| 3 | 6 | 2.454 s |
| 3 | 12 | 4.141 s |
| 4 | 3（无中间行） | ≈2.2–2.5 s（4×4=2.322 s） |
| 3 | 3（cycles=3） | 6.144 s |

**判据（规格 §8 M1）**：profile 3 + Auto(3) 单时隙 ≤ 8 s → **实测 2.14 s，余量 ≈3.7×** ✅

生产默认保持：`profile 3, threads = auto_thread_count() = 3, cycles = 1`。
cycles=3（更激进的多轮）在盒子上 6.14 s，仍在 8 s 内，可作为后续调优选项但首版不启用。

## 5. 与计划的偏离（均已落在代码里）

| # | 偏离 | 原因 |
| --- | --- | --- |
| 1 | 任务 1 先复制 `dsp/cmake` + `dsp/patched`（抽取脚本的输入），任务 2 复制其余 | 抽取脚本的清单来自 CMake，属执行顺序修正 |
| 2 | 新增 `ft8/dsp/vendor-refs/`（7 个 patched 原件 + `q65_set_list.f90`） | patched 副本替代了原件，原件不在编译清单里；逆替换回归需要原始基线 |
| 3 | `build.sh` 增加"陈旧 build 目录自动清理" | 本地 `build/` 被同步到盒子导致 cmake 拒绝（实测踩到） |
| 4 | CMakeLists 显式 `find_package(Boost REQUIRED)` | 原构建靠宿主环境的隐式 include，盒子上直接编译失败 |
| 5 | 仓库根补 `ALLCALL7.TXT` | DSP 运行时读取（呼号校验表），缺失时打印 `ALLCALL7.TXT is too short or broken?` 警告 |
| 6 | M1 spike 用内联 rsync 完成，未产出独立 `deploy_box.sh` | 正式部署脚本在任务 20 交付，避免两份同步逻辑 |
| 7 | 契约测试的记录清单改为本仓设计文档 | 上游引用的 `2026-08-01-ft8-dsp-worker.md` 不在本仓 |

## 6. 结论

**M1 全部出口判据通过，放行 M2**（引擎移植与平台适配）。
遗留观察项（不阻塞）：

- `ALLCALL7.TXT` 警告在补文件后应消失（M2 真机验证时确认）；
- 盒子 2 GB 内存下 FT8 引擎 + mrrc_modern 共存的内存水位，M5 真机验收（规格 §6-6）观察。
