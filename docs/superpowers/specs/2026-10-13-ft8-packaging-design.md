# FT8 四平台打包设计

- **日期**：2026-10-13
- **状态**：待评审（本文件通过后进入实现）
- **范围**：把 `feat/ft8` 的 FT8 工作模式**装进四个平台的产物**——W103D 盒子镜像、树莓派镜像、
  macOS DMG、Windows 安装包；同时修掉 `ft8/dsp` 在 MinGW 上编不过的两处可移植性缺陷
- **配套**：`2026-10-12-ft8-work-mode-design.md`（本体）、`2026-10-13-ft8-fast-path-cascade-design.md`（解码路径）
  ——两份都明确把打包推迟了（前者 §7 风险 8：「DSP 编译是否烘焙进镜像 / mrrc-update 后自动重建
  留待 packaging 合并评估」）；本文件就是那次评估的结论
- **依据**：`docs/ft8-w103d-decode-perf-analysis.md` §5（"把 DSP 构建烘焙进镜像"的建议）；
  `docs/ft8-m1-baseline.md`（aarch64 编译 2 分 02 秒、依赖可用）
- **相关代码**：`packaging/common/packages.txt`、`packaging/common/mrrc-service-layer.sh`、
  `linux/mrrc_update.sh`、`packaging/macos/build.sh`、`packaging/windows/build.ps1`、
  `packaging/pyinstaller/mrrc_modern_server.spec`、`ft8/dsp/CMakeLists.txt`、`ft8/core/worker.py`
- **相关文档**：SDD `09-architecture-overview.md`、`12-operational-model.md`；
  `docs/PROJECT_MAP.md`、`docs/DOCUMENT_INDEX.md`、`AGENTS.md`

---

## 1. 背景：缺口在哪

FT8 的全部代码（`ft8/{core,engine,dsp,platform,web}` + `static/ft8*`）都在树里，
四个平台的打包器却只搬 Python 与静态资源——**没有任何一个产物会编译或携带 DSP 原生库**：

| 平台 | 现状 | 后果 |
| --- | --- | --- |
| W103D 盒子 | `packages.txt` 只有 7 个 apt 包（无 gfortran/cmake/fftw/boost），DSP 靠 M5 验收时**手工**编译 | 刷机或 `mrrc-update` 之后 FT8 直接不可用（库不存在）；镜像里那份是验收时留下的 |
| 树莓派 | 与盒子共用服务层 ⇒ 同样没有 | 同上（Pi 镜像从未编译过 DSP） |
| macOS | `build.sh` 不编译 `libwsjt_core.dylib`，spec 不打包它 | 冻结包按 `app_root()/wsjt_core.dll` 找库——**路径本身就是错的** |
| Windows | 同上，且 `ft8/dsp` 在这台 VM 上**编不过**（见 §3） | 同上 |

缺口不是"忘了拷文件"，而是三件独立的事：**原生库谁来编**、**依赖怎么带**、
**升级之后怎么保证库和 Python 同源**。

---

## 2. 决策

### D-1：两条镜像线**在镜像里编译** DSP（不 vendored 二进制、不在首启编译）

- `packaging/common/packages.txt` 增加构建期依赖：`gfortran`、`g++`、`cmake`、`pkg-config`、
  `libfftw3-dev`（运行时库 `libgfortran5` / `libgomp1` / `libfftw3-single3` 由它们拉进来）。
- `packaging/common/mrrc-service-layer.sh` 在 `install.sh` 之后跑 `bash ft8/dsp/build.sh`，
  并断言 `ft8/dsp/build/libwsjt_core.so` 存在。
- `linux/mrrc_update.sh` 在 `pip install` 之后跑同一个脚本（`build.sh` 自带源码摘要，
  没变就秒退），这样**升级永远不可能出现"新 Python + 旧 .so"**——那正是
  `docs/ft8-w103d-decode-perf-analysis.md` §5 点名的风险。

不选"把 aarch64 的 .so 提交进 `vendor/`"的理由：库与源码之间会多出一份需要人工同步的二进制
（本仓的 `vendor/ftdi/` 是硬件厂商的**只读**库，不是自家可编译产物，性质不同），
而 `build.sh` 的摘要机制本来就把"是否需要重编"变成了可判定的事。

不选"首启编译"的理由：首启已经在做必须碰硬件的四件事（口令/串口/声卡/证书），
再塞一个两分钟的 Fortran 编译既拖慢首启，又把"镜像是否完整"变成运行期才知道的事。

**实测代价**（本机 Docker，Debian bookworm arm64，9 核）：编译 **7.7 秒**；
工具链进镜像约 **+70 MB 安装后**（gfortran 30 / cmake 25 / libfftw3-dev 9 / 其余若干），
压缩后的 `.img.xz` 增量约 20–25 MB。

### D-2：**Boost 头文件 vendored**，四平台都不再需要 Boost 安装

`<boost/crc.hpp>` 是本仓唯一的 Boost 用途（`vendor/crc13.cpp`、`crc14.cpp`），
而 Debian 只把它装在 `libboost1.74-dev` 里——**138 MB 的头文件**，两个镜像都要背，
macOS/Windows 构建机也要装。改为把这两个头的**递归包含闭包**（实测量：83 个头 + 许可证，
720 KB）落到 `ft8/dsp/vendor-boost/`，由 `tools/ft8/vendor_boost_headers.sh` 从一份 Boost
安装里抽取并生成 `vendor-boost.sha256`；`CMakeLists.txt` 去掉 `find_package(Boost)`，
把 `${FT8_BOOST_INCLUDE}` 加进包含路径。

这是本仓既有的 vendor 模式（`vendor.sha256`、`vendor-ft8lib.sha256` 的第三例），
并有 `tests/ft8/dsp/test_vendor_boost_policy.py` 钉住清单与闭包完整性。

### D-3：Windows 用 MinGW-w64 在**打包时**编译，运行时闭包一起进包

VM（`C:\msys64\mingw64`）已有 gfortran 13 / gcc / cmake / ninja / fftw3f（含
`libfftw3f-3.dll` 与 `libfftw3f_threads-3.dll`）。`packaging/windows/build.ps1` 新增一步：

1. `cmake -G Ninja` 构建 `ft8/dsp/build/wsjt_core.dll`（`-D_GNU_SOURCE` 供 ft8_lib 的 `strcasecmp`）；
2. 把以下 9 个文件拷进 `build/ft8/windows/`（PyInstaller 的 datas 源）：

   `wsjt_core.dll`（4.7 MB）+ 传递依赖 `libfftw3f-3.dll` / `libfftw3f_threads-3.dll` /
   `libgfortran-5.dll` / `libgomp-1.dll` / `libquadmath-0.dll` / `libgcc_s_seh-1.dll` /
   `libstdc++-6.dll` / `libwinpthread-1.dll`（合计 ~16 MB）。

3. `MRRC-Modern.iss` 的体积随之上升 ~16 MB，属预期。

**两处可移植性修复**（`ft8/dsp`，本设计新增，Linux/macOS 不受影响）：

| 症状 | 根因 | 修法 |
| --- | --- | --- |
| `message.c` 编不过：`implicit declaration of function 'stpcpy'` | MSYS2 的 mingw-w64 **没有任何** `stpcpy` 声明（`grep -r stpcpy include/*.h` 只命中 libunistring），MSVCRT/UCRT 也没有这个函数；GCC 14 起隐式声明是**错误**，而且隐式声明会把它当成返回 `int`——x64 上就是把指针截成 32 位 | 不 patch vendor（那是摘要清单的全部意义）：`ft8/dsp/win_stpcpy.h` 提供 `static inline` 定义，`CMakeLists.txt` 用 `-include` 只在 Windows 的 C 编译里强制包含 |
| 导出符号为空 | Linux 用 version script、macOS 用 deny list，Windows 两条分支都不走，MinGW 默认**不导出**任何符号；DLL 名还会带 `lib` 前缀 | `WIN32` 分支：`PREFIX ""` + `WINDOWS_EXPORT_ALL_SYMBOLS ON` |

### D-4：macOS 在**打包时**编译，dylib 与它的 5 个 MacPorts 依赖一起进包

本机 gfortran 来自 MacPorts，编出来的 `libwsjt_core.dylib` 链接到
`/opt/local/lib/{libfftw3f_threads.3,libfftw3f.3}.dylib` 与
`/opt/local/lib/libgcc/{libgomp.1,libgfortran.5,libquadmath.0}.dylib`——**用户机器上没有这些**。

`packaging/macos/build.sh` 新增一步：编译 → 把 6 个 dylib 收进 `build/ft8/macos/` →
`install_name_tool` 把每个依赖改写成 `@loader_path/<名字>`（并把被依赖库自身的 `-id` 也改成
`@loader_path/<名字>`，否则它们之间互相找不到）→ 交给 PyInstaller 作为 data 进
`_internal/`（随后由既有的 `_internal → Contents/Resources` 搬运与符号链接机制落到
`app_root()`）。构建脚本**在打包前断言**：`otool -L` 的每个依赖要么是系统库
（`/usr/lib/*`、`/System/*`），要么指向 `@loader_path/`，否则构建失败。

### D-5：PyInstaller 两个 spec：原生库 + FT8 的 Python 依赖

- `datas`：`build/ft8/windows/`（或 `build/ft8/macos/`）→ 目标 `.`（包根），
  这样 `app_root()/wsjt_core.dll` 与 `app_root()/libwsjt_core.dylib` 正好是运行时查的路径。
  目录不存在时**不报错**（保持"缺什么就退化成什么"的既有风格），但构建脚本会先断言它存在。
- `hiddenimports`：`ft8`、`ft8.core.*`、`ft8.engine.*`、`ft8.platform.*`、`ft8.web.*`
  （`server.py` 是静态 import，PyInstaller 能跟到；仍然显式列出——v1.24.0 的 `cloud_hub`
  事故就是"静态 import 没被收进包"）、`scipy`、`soxr`。

### D-6：冻结态的库路径按平台分辨

`ft8/core/worker.py:default_library_path()` 现在冻结态一律返回 `wsjt_core.dll`——
macOS 冻结包里那是**错的**。改为按 `sys.platform` 返回 `.dll` / `.dylib` / `.so`，
并在 `tests/ft8/core/test_worker.py` 里各钉一条。

---

## 3. 不做什么

- 不动 `ft8/{core,engine,dsp/vendor*}` 的任何算法或数据；
- 不给 Windows/macOS 装第三方 DSP 依赖包（Boost 已 vendored，FFTW 在 Windows 上来自 MSYS2、
  在 macOS 上来自 MacPorts 并随包携带）；
- 不改 `build.sh` 的摘要语义（它是 D-1 的幂等保证）；
- 不改 `stage-tree.sh` 的排除清单（`ft8/`、`vendor/` 本来就进树；`build/` 被排除，
  这是 D-1 每次升级都要重编的原因）。

---

## 4. 验证

| # | 平台 | 判据 |
| --- | --- | --- |
| 1 | aarch64（容器 = 镜像同基底） | `build.sh` 通过；`readelf -d` 的 NEEDED 全在 bookworm；`nm -D` 有 5 个 `wsjt_*`；ctypes 载入后 `wsjt_get_abi_info` 返回 `abi=2 / decode_paths=0x7`；`wsjt_ft8_encode` 返回 0 |
| 2 | Windows（VM） | 同上，用 `objdump -p` / `nm -g`；9 个 DLL 齐；冻结包剥字节码后能看到 `ft8` 模块与 `wsjt_core.dll` |
| 3 | macOS | `otool -L` 无 `/opt/local` 残留；DMG 里的 app **干净室真跑**：切换 FT8 模式、引擎起来、`/api/ft8/health` 200 |
| 4 | 两个镜像 | 产物四步验证的 ④（arm64 chroot 真跑）追加：`ctypes.CDLL("/opt/mrrc_modern/ft8/dsp/build/libwsjt_core.so")` 成功且 ABI=2 |
| 5 | 升级路径 | 盒子 `mrrc-update` 后台本自带 `build.sh`（摘要未变时秒退） |

---

## 5. 风险

| # | 风险 | 处置 |
| --- | --- | --- |
| 1 | 镜像体积增长 | 实测：安装后 +70 MB（Boost 已通过 D-2 省掉 138 MB）；产物 `.img.xz` 增量 20–25 MB，写入 CHANGELOG |
| 2 | MinGW 工具链版本漂移（VM 上是 MSYS2 滚动版） | 构建脚本断言 `wsjt_core.dll` 存在且 `objdump -p` 列出了全部 9 个 DLL；符号走查按既有四层验证做 |
| 3 | macOS 依赖改写漏一个 ⇒ 用户机器上 `dlopen` 失败 | 构建脚本硬门禁：`otool -L` 出现 `/opt/local` 即失败 |
| 4 | vendored Boost 与设备上的编译器不兼容 | 闭包来自 Debian 1.74（设备同源）；编不过会在镜像构建期**响亮**失败，不会静默 |
