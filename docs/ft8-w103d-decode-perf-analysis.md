# FT8 解码在 W103D 上的深度优化分析

- **日期**：2026-10-13
- **代码**：mrrc_modern `feat/ft8`
- **机器**：ZTE W103D（Amlogic S905L3A，G12A 家族，**4×Cortex-A53 顺序核 @ ~1.8 GHz**）、
  2 GB RAM、Armbian（Debian 12）/ 内核 6.18.54 / aarch64
- **性质**：只读分析，**未改动任何代码**
- **数据来源**：实测数字全部引自 `docs/ft8-m1-baseline.md` §4 与 `docs/ft8-m5-acceptance.md`；
  结构性结论来自对 `ft8/dsp/`（vendor 子集 + patched）逐行阅读，均在文中标注 file:line
- **修订**：2026-10-13 晚追加 **§7 第二轮：解码内核逐行分析 + 反汇编实测**，
  以及 **§8 第三轮：与 `kgoba/ft8_lib` 的 A/B 实测**。
  §1–§6 为第一轮（宿主/构建/算法结构层），保持原样未改；第二轮下沉到内核，
  并**证伪了第一轮之后提出的两个假设**（见 §7.3）；第三轮在 W103D 上做同机同输入的
  双解码器对照（见 §8）。

---

## 0. 先纠正一个前提：盒子**不缺算力**，缺的是**延迟余量**

实测（`docs/ft8-m1-baseline.md:44-59`，profile 3 / threads 3 / cycles 1）：

| 指标 | 实测 | 判据 |
| --- | --- | --- |
| 单时隙解码 | **2.140 s** | M1 判据 ≤8 s → 余量 3.7× |
| FT8 模式 CPU | 平台 ~14% + DSP worker ~5% | `docs/ft8-m5-acceptance.md:17` |

但真正在用的闸门不是 8 s，而是 `ft8/engine/orchestrator.py:35` 的
**`DEFAULT_DECISION_CUTOFF_SECONDS = 2.5`**：解码完成晚于 `时隙结束 + 2.5 s`
即判 `late`，**只显示、不喂给 sequencer**（`orchestrator.py:258,264-271`）——自动 QSO 直接失效。

配合提前解码（dispatch 在 `时隙结束 − 1.2 s`，`ft8/runtime.py:26`），完成点
≈ `时隙结束 + 0.94 s`。**对 2.5 s 截止的余量只有约 1.15×。**

直接后果：`cycles=3` 实测 **6.144 s**（`ft8-m1-baseline.md:57`）→ 必然 `late` → 全部只显示。
**当前灵敏度旋钮实际上是焊死的。**

> 所以"深度优化"的价值不是救活一个跑不动的盒子，而是：
> **压掉解码耗时 → 打开 cycles/profile 空间 → 多解码**（FT8 的真正收益），
> 同时给繁忙频段的最坏情况留安全边际。

---

## 1. 关键证据：线程**完全不可扩展**

| threads | 1 | 2 | **3（生产 Auto）** | 4 | 6 | 12 |
| --- | --- | --- | --- | --- | --- | --- |
| median (s) | 3.148 | 2.458 | **2.140** | 2.292 | 2.454 | **4.141** |

1→3 只换来 **1.47×**（3 个核）；12 线程 **比单线程还慢**。

通常解释是"超订"，但代码阅读表明不是。真因是
**同一个计算被每个线程原样重做了一遍**——加核只是把重复劳动再复制一份，
而 4×A53 共享的 L2 / 内存带宽先饱和。

---

## 2. 根因（三条，均可从代码量化）

### 根因 A：sync 阶段每线程算一遍完整结果

`run_improved_pass`（`ft8/dsp/wsjt_improved.f90:267-296`）用 `compute_band`
把频段切开，每个线程拿一个子带（`ft8/dsp/wsjt_partition.f90:9-18`）。
但子带**只用在最后挑候选**：

- **符号谱 FFT**：`sync8var` 里 `do j=1,NHSYM … call four2avar(cx,NFFT1,1,-1,0)`
  （`ft8/dsp/vendor/ft8var/sync8var.f90:20-63`）从 `dd8` 全量构造，**没有任何频段条件**。
- **sync2d 相关**：`do i=iaw,ibw`，而 `iaw/ibw` 来自
  **`nfawide/nfbwide` = 全局 200–3000 Hz**（`wsjt_improved.f90:163-164`），不是线程子带。
- 线程子带 `ia/ib` 只出现在 `call indexx(red(ia:ib),…)`（`sync8var.f90:181`）。

**结论：每个线程都完整计算 372 个符号谱 + 897 bin × 125 时偏的 sync2d，
3 线程 = 3 份逐字节相同的计算。**

其中 `NHSYM = NMAX/NSTEP − 3 = 180000/480 − 3 = 372`，`NFFT1 = 2*NSPS = 3840`
（`ft8/dsp/vendor/ft8/ft8_params.f90:9-11`）。

### 根因 B：同一个 FFT 在一时隙内重算 3 遍

`npass=3`（`ft8/dsp/patched/ft8_decodevar.f90:202-211`，cycles=1）。
三个 pass 的**唯一差别是幅度公式**：

| ipass | 公式 | 位置 |
| --- | --- | --- |
| 1 | `s = sqrt(re²+im²)` | `sync8var.f90:30-32` |
| 2 | `s = re²+im²` | `sync8var.f90:45-47` |
| 3 | `s = \|re\|+\|im\|` | `sync8var.f90:60-62` |

FFT 输入完全相同。而 `dd8` 只在 ipass 4 / 7 被改写
（`ft8_decodevar.f90:227-249`），**这两个分支在 npass=3 时根本不执行** ——
所以 cycles=1 时三个 pass 的 `dd8` 逐字节一致。

**账**：每线程每时隙 `372 × 3 = 1116` 次 3840 点 FFT；最小只需 **372** 次。
叠加根因 A（×3 线程）：

> **全机每时隙 3348 次大 FFT，理论最小 372 次 —— 9×。**

### 根因 C：工作集全部落在内存里

每线程私有工作集（`sync8var` 是并行区内的局部数组）：

| 数组 | 大小 | 出处 |
| --- | --- | --- |
| `dd8` `!$omp threadprivate` | 720 KB | `ft8/dsp/patched/ft8_mod1.f90:5-6` |
| `s(NH1,NHSYM)` = real(1920,372) | **2.86 MB** | `sync8var.f90:6` |
| `sync2d(1920,−62:62)` | 0.96 MB | 同上 |
| `syncq(1920,−62:62)` logical | 0.24 MB | 同上 |

≈ **4.8 MB/线程**，而 4×A53 的 L2 约 512 KB/簇。`s` 每 pass 写一遍再读一遍。

**这同时解释了那条铁律**：`OMP_STACKSIZE=10M`（`ft8/core/binding.py:7-9`、
`ft8/core/worker.py:63`）不是空穴来风 —— `s` 就是 2.86 MB 的栈上数组，每线程一份。

而且 `s` 只用到 `iaw..ibw`（约 897 / 1920 bins），却按 1920 维分配，
**白扔一半 footprint**。

**加核变慢（3→4→6→12）的直接原因就是内存带宽，不是核不够。**

### 根因 D（次要）：候选收尾被串行化

`!$omp critical(find_dupes)` 把每个候选的去重 + 回调 + 数组更新整段串起
（`ft8_decodevar.f90:291-417`）。候选多的繁忙频段，这里是争用点。

---

## 3. 顺带发现的两个隐患

### ① FFTW 嵌套超订的地雷已埋好，只是没踩到

`ft8/dsp/vendor/ft8var/filbigvar.f90:71` 会调 `fftwf_plan_with_nthreads(nthreads-1)`
（`nthreads` = OMP 线程数）——那是 N×(N−1) 超订。但全树 grep
**找不到任何 `call filbig` 调用点**（这个移植已改用 `ft8_downsample`，
见 `vendor/ft8/ft8b.f90:106`），所以 FFTW 实际是单线程，**目前是安全的**。

代价是：`fftwf_init_threads()` 白调（`wsjt_improved.f90:75`）、
`fftw3f_threads` 白链（`ft8/dsp/CMakeLists.txt:16,47`）、
`filbigvar.f90` 白编译（`ft8/dsp/cmake/improved-ft8.cmake:19`）。
**一旦有人误接回 filbigvar，盒子上立刻超订。**

### ② 灵敏度的真正瓶颈

`ft8_downsamplevar`（`ft8/dsp/patched/ft8_downsamplevar.f90:31`）的 192k r2c FFT
是 `cxx` + `threadprivate`（`:17-18`），**每线程每 pass 算一次**（3×3=9 次，最小 3 次）。

---

## 4. 优化清单（按性价比排序）

### 第 0 梯队 — 先测量，别猜（约 10 分钟）

跑同一时隙 `threads=1,2,3,4`，**看 CPU 时间而不只是 wall**：

```bash
perf stat -e task-clock …        # 或 /usr/bin/time -v
```

**如果总 CPU 时间随线程数上升 → 根因 A/B 坐实**（工作被复制）。
这是决定性的、可证伪的一步，也是推荐的第一步：
`docs/ft8-m1-baseline.md:44` 那张表**只有 wall time，没有 CPU time**。

### 第 1 梯队 — 宿主 / 构建层，几乎零风险

| 项 | 现状 | 动作 | 预期 |
| --- | --- | --- | --- |
| **编译目标** | 只有 `-O3`（`ft8/dsp/toolchain.cmake:6`），aarch64 默认 `-mcpu=generic` | 设备上原生编译 → `-mcpu=cortex-a53 -mtune=cortex-a53`（比 `-march=native` 更稳，不受将来改到构建机编译影响）+ `-funroll-loops` | A53 上典型 **5–15%** |
| **FFTW wisdom** | `npatience=0` 硬编码 → 全部 `FFTW_ESTIMATE`（`wsjt_improved.f90:261`） | 离线生成 wisdom 存盘，worker 启动 `fftwf_import_wisdom_from_filename` → 对 3840/192000/3200 用 MEASURE/PATIENT，**零运行时规划代价** | 大 FFT 段 1.3–2× |
| **cpufreq / 散热** | `packaging/`、`linux/` **完全没有任何** governor/thermal 配置 | `governor=performance` 或抬 `min_freq`；**实测 SoC 温度**确认无降频 | 削掉 burst 起手的低频段 |
| **OMP 自旋** | 无 | `OMP_WAIT_POLICY=passive`（4 核上 3 个 OMP 线程自旋会和音频线程/asyncio 抢核） | 改善音频卡顿与抖动 |
| **systemd** | 单元无 `Nice`/`CPUAffinity`（`packaging/common/mrrc-service-layer.sh:172-199`） | 给 worker 一个 low nice，保住音频线程 | 抗抖动 |

`-ffast-math` **不建议**：会改变 SNR 数值，而仓库有解码回归测试
（`tests/ft8/dsp/`）；先上 `-fno-math-errno -fno-trapping-math`。

### 第 2 梯队 — 算法层，收益最大，需谨慎

1. **让 sync 阶段只算一次**（根因 A）。两条路：
   - 把 372 个符号谱**在并行区外 / 协作式**算一次共享（每线程分 `j` 的切片，
     用共享数组），候选提取再按线程子带并行；
   - 或保留 `s` 但只在 `iaw-16..ibw` 上分配（`s(897,372)` → 1.34 MB），
     砍掉一半 footprint。

   **这是唯一能让线程真正扩展的改法**，也是把 2.140 s 往下压的主要来源。

2. **三个 pass 共用一次 FFT**（根因 B）。同一个复谱派生三种幅度 →
   省掉 2/3 的 372 次 FFT。代价是要留 3 份 `s`（≈8.6 MB，2 GB 内存够，
   但会更受带宽限制）—— 需要实测权衡"重算 vs 重存"。

3. **192k downsample FFT 跨线程共享**（隐患 ②）。它只依赖 `dd8`，
   同 pass 内线程间完全相同。

4. **收紧 `critical(find_dupes)`**，把去重与回调拆开。

### 第 3 梯队 — Python 侧

- **soxr HQ 降级 + 缩小 read block**：当前链条稳定 +0.8 s、**TX 后 +1.8 s**
  （`ft8/runtime.py:158-174`），逼出了 `delivery_grace=3.0` 这个已经出过事故的
  经验常数（commit `7451836`「发射一次 CQ 后就不解码了」）。降 soxr 到 MQ
  能**同时降 CPU 和降流水线延迟** → grace 可以变小、余量变大。比继续调 grace 更治本。
- 瀑布（3.5 lines/s × rfft(4096)，`ft8/engine/waterfall.py:23-24,132-147`）
  可忽略，不用管。
- worker 冷启动（spawn + numpy import + CDLL + `cwfilter`）发生在进 FT8 模式时
  —— **量一下首时隙是否偏慢**。

### 不要做

- ❌ **加线程**：`threads>3` 实测更慢（根因 A/C），当前 Auto=3 已是这个结构下的最优点，
  不是需要调优的参数。
- ❌ 大部分标量微优化：内存带宽才是墙。

---

## 5. 顺带的运维风险

镜像里**没有** gfortran / libfftw3-dev / libboost-dev / cmake
（`packaging/common/packages.txt` 只有 7 个 apt 包），DSP 是
**设备上手工编译、不烘焙进镜像**，`linux/mrrc_update.sh` 也不重建。

→ **更新的 Python 可能配上旧的 `.so`**（静默行为不一致）。
建议把 DSP 构建烘焙进镜像 —— 顺带也就把 `-mcpu` 固化下来了，一举两得。
（`docs/ft8-m5-acceptance.md:71` 已把这项列为待办。）

---

## 6. 建议的下一步

按这个顺序，**先做第 0 梯队那个 10 分钟的 CPU-time 测量** ——
它用一条命令就能确认（或推翻）本分析的大部分判断，再决定投不投第 2 梯队的重构。

1. 写一个可复现的测量脚本 / 基准（threads × CPU-time），把根因 A/B 坐实；
2. 或直接把第 1 梯队（`-mcpu`、wisdom、governor）做成一个低风险 PR
   —— 这部分不碰 vendor 代码，可完全回归验证。

---

# 第二轮：解码内核逐行分析 + 反汇编实测（2026-10-13 晚）

第一轮停在"宿主/构建/算法结构"层（§1–§6）。第二轮下沉到解码内核本身，
把能查询的都变成**可核验的测量**，而不是估算。

## 7.1 方法

本机 `ft8/dsp/build/CMakeFiles/wsjt_core.dir/*.o` 是 **Mach-O arm64** 目标文件
——与 Cortex-A53 同属 AArch64，代码生成（向量化决策、库调用）是同族的良好代理。
手段三件：

1. `nm -u <obj>`：目标文件**实际调用了哪些库函数**（malloc / libm / gfortran 运行期）；
2. `otool -tv`：反汇编，数标量 `fadd s` 与向量 `fadd v`、NEON `q` 寄存器操作；
3. `/tmp/ft8probe/` 里的**丢弃式最小复现**：`gfortran-mp-13 -O3 -S` 编译内核片段，
   直接看内层循环生成了什么。（探针是丢弃物，仓库零改动。）

## 7.2 实测结果：编译器向量化了什么、没向量化什么

| 内核 | 标量 `fadd s` | 向量 `fadd v` | NEON `q` 操作 | 结论 |
| --- | --- | --- | --- | --- |
| `vendor/ft8var/sync8var.f90.o` | **128** | **0** | 19 | 浮点归约**全是标量** |
| `patched/osd174_91var.f90.o` | — | — | **528** | `integer*1` 异或**已 NEON 批量** |
| `vendor/ft8var/bpdecode174_91var.f90.o` | 5 | 0 | — | 主体是查表/分支，浮点少 |

**这条是整份分析里最关键的 CPU 专属事实**：

> `gfortran -O3` 在这个仓库里**把 GF(2) 的字节数组向量化了（OSD 528 条 q 操作），
> 却把所有浮点归约留在了标量**。挡住向量化的**不是 CPU 不支持 SIMD**
> ——aarch64 的 NEON/ASIMD 是 ARMv8-A 基线的一部分，永远可用——
> **而是浮点归约不能重结合**（需要 `-ffast-math` 一类许可）。

对 A53 的意义：它是**顺序核**，FP add 延迟约 4 周期，没有乱序窗口去掩盖依赖链。
`sync8var` 内层的 `s0 = s0 + sN` 是**串行累加链**——17 个元素的窗口求和
≈ 17×4 = **68 周期纯延迟**，而不是吞吐限制。**优化方向是"打断依赖链"，不是"减少指令数"。**

## 7.3 证伪：第一轮之后我提出的两个假设都是错的

记录下来，避免后续重复走这两条死路。

### ❌ 假设 1：`sum(s(i:i+16,k))` 每次会分配临时数组

`sync8var.f90:119-152` 里每对 (i,j) 有约 30 次 `sum(s(i:i+16,k))`，
`nm -u` 显示 `sync8var.f90.o` 确实导入 `_malloc`/`_free`（11 处重定位），
所以"每对 (i,j) 分配 30 次 17 元素临时数组"看起来很合理。

**实测否掉**：探针例程共 **109 条指令、0 条 `bl`** —— 没有任何库调用。
gfortran 把它内联成 `ldp` 连续装载 + `fadd` 的展开循环。

真正的 `_malloc` 来自 `sync8var.f90:179,181` 的两次
`call indexx(red(iaw:ibw),…)` **数组片段实参**（每 pass 各一次），**不是内层循环**。

### ❌ 假设 2：`bpdecode` 的掩码 `product` 会分配临时数组

`vendor/ft8var/bpdecode174_91var.f90:107`：
`Tmn=product(tanhtoc(1:nrw(ichk),ichk),mask=Nm(1:nrw(ichk),ichk).ne.j)`
——典型的"掩码归约分配临时数组"地雷，且最坏每次迭代被调 174×3 次。

**实测否掉**：该目标文件的未定义符号只有 `_memset _platanh_ _tanhf`，
**没有 `_gfortran_*`、没有 malloc**。掩码 `product` 已被内联。

### ⚠️ 但同一份符号表暴露了别的东西

- **`_tanhf` 是真的 libm 调用**：M=87 个校验节点 × 7 = **每次迭代 609 次 `tanhf`**
  （`:101`），而 `max_iterations=30`（`ft8bvar.f90:85`）。
  仓库里已有手写快近似 `platanh`（`:108` 在用），`tanh` 却没换。
- **`_malloc` 在 `osd174_91var` 里是真的**（见 §7.5）。

### 附带更正：`s(i:i+16,k)` 是**连续**的，不是跨步的

`i` 是 Fortran 的最快变化维，所以 `s(i:i+16,k)` 的 17 个浮点在内存里连续。
第一轮口头分析里"跨步访问"的说法不成立——它是**顺序流式**访问
（这对滑动窗口强度削减是**好消息**：装载可 `ldp` 配对，缓存友好）。

## 7.4 调用倍率：2.14 s 的量级来源

```
ft8_decodevar.f90:251   call sync8var(...)          ← 每个 ipass 调一次，返回 ncand 个候选
ft8_decodevar.f90:253   do icand = 1, ncand
ft8_decodevar.f90:273      call ft8bvar(...)
ft8bvar.f90:1058           do isubp1 = 1, nsubpasses    ← nsubpasses = 3…11（ft8bvar.f90:993-1052）
ft8bvar.f90:1309              do isubp2 = 1, 31
ft8bvar.f90:2076                 call bpdecode174_91var(...)   ← 每个候选必调
ft8bvar.f90:2094                 call osd174_91var(...)        ← BP 失败就调（绝大多数）
```

外层还有一个 `ipass = 1..npass`（`ft8_decodevar.f90:202-211`，npass=3）。

> **每时隙 BP/OSD 调用次数 ≈ 3 × ncand × (3…11) × 31**
> —— 被 `syncavemax` 之类的闸门削掉一部分，量级仍是**数百到数千次**。

这解释了 2.14 s：静态推算里 sync (§2 根因 A/C) ≈ 0.1 s、FFT ≈ 0.25 s/线程，
都远填不满 2.14 s。**时间必然落在 per-candidate 路径上。**

## 7.5 OSD 每次调用的固定开销（`patched/osd174_91var.f90:213-273`）

`ndeep` 默认 3（`:2080`），走的正是 `npre2=1` 分支：

| 动作 | 量 | 出处 |
| --- | --- | --- |
| `allocate(indexes(5000,2), fp(0:525000), np(5000))` | **≈2.14 MB** | `:214-219` |
| `fp=-1` 全量清零（525 001 个 int） | **≈2.1 MB** | `:336` (`boxit91var`) |
| 同一清零**再执行一遍**（`reset=.true.` 在 `:232` 又设一次） | **≈2.1 MB** | `:220` / `:232` |
| `boxit` 建 4095 个 (i1,i2) 表 | 4095 次 | `:222-228` |
| `deallocate` | | `:268-273` |

即**每次 OSD 调用 = ≈2.14 MB malloc + ≈4.2 MB memset + ≈2.14 MB free**。
glibc 对 >128 KB 的请求走 `mmap`，所以每次都是 mmap/munmap 系统调用
+ 约 525 次缺页。按数百–数千次/时隙算，是**GB 级 memset 与百万级缺页**。

## 7.6 针对 Cortex-A53 的机会（排序）

### T0 — 先量，别猜（**仍未做**）

诚实说明：**本分析没有 profile。** §7.4 的"时间在 per-candidate 路径"是**推断**——
而这一轮已经证伪了我自己两个假设，正说明静态阅读不足以定位热点。

- 设备镜像**没有 `perf`**（`packaging/common/packages.txt` 只有 8 个 apt 包）。
  装 `linux-perf` 后 `perf record -g` 跑一个时隙即可，`.so` 符号齐全。
- 更省事的替代：`dev_tools/bench_mrrc.py` 只测服务器 rx/tx/spectrum，
  **没有解码分阶段计时**；但 worker 协议本就返回 `elapsed_seconds`
  （`tests/ft8/dsp/test_i9_benchmark.py:175`），而 `timer_module` 已在
  `ft8_decodevar.f90:29` 引入——在 `run_improved_pass` 里加几个 `timer(...)`
  就能把 sync / BP / OSD 分开计时。

### T1 — 零数值变更的确定性浪费

1. **OSD 三个大数组挪出每次调用**：`indexes/fp/np` 改 `save` / 模块级 / 按 `nthr` 分槽。
   代价 +2.14 MB × 线程数常驻（3 线程 = 6.4 MB，2 GB 内存够）。
   收益：消掉每时隙数百次 mmap/munmap + 缺页。**数值完全不变。**
2. **`fp=-1` 不全量清零**：用 epoch 标记，或只清上一轮 `boxit` 写过的槽位。
   **数值完全不变。**

### T2 — FP 向量化：A53 上真正的杠杆

1. **手拆累加链**：`t0a = t0a + sum(...)` 拆成 4 个部分和再合并。
   A53 上 17 元素串行链的 68 周期延迟 → 约四分之一。**改变舍入**。
2. **滑动窗口强度削减**（`sync8var.f90:119-152`，窗口在 i 上滑 1）：
   维护 30 条 running sum，每对 (i,j) 装载从 ~570 次降到 ~60 次。
   **计算量 −8×、内存流量 −17×**，同时打掉延迟与带宽两个瓶颈。
   同样改变舍入；用 double 累加或每 64 步重锚可把误差压到 ~1e-11。
3. **`s` 只分配 `iaw-16..ibw` 的行**（64…976 / 1920）→ 2.86 MB → ~1.4 MB。

以上三项都改变浮点结果 → **必须过 `tests/ft8/dsp/` 的解码回归**。

### T3 — 构建/宿主层（补 A53 专属的更正）

- **`-mcpu=cortex-a53` 的收益来自调度/tune，不是向量化**（见 §7.2）。
  预期仍是 5–15% 量级，不是数量级——第一轮 §4 的说法需要按这条打折理解。
- FFTW wisdom、governor、`OMP_WAIT_POLICY=passive`：同第一轮。

### BP 的 `tanhf`（中等收益）

609 次 libm `tanhf`/迭代。换成 `platanh` 同风格的代数近似可省一截，
但**改数值**，同样要回归。

## 7.7 第二轮的小结

- **已实测**：sync 的浮点归约 100% 标量，OSD 的 GF(2) 已 NEON 化；
  两个"看起来很值钱"的假设（临时数组分配）都是错的。
- **量级来源**：BP/OSD 被 `ipass × ncand × nsubpasses × 31` 放大到数百–数千次/时隙，
  而每次 OSD 固定烧掉 ~2.14 MB malloc + ~4.2 MB memset。
- **A53 专属杠杆**：不是 `-mcpu`，而是**打断浮点累加依赖链**——
  这是顺序核上唯一还没被利用的算力。

---

# 第三轮：A/B 实测 —— kgoba/ft8_lib vs 本仓解码器

前提：前两轮都停在"应该怎么改"。这一轮换一个问法——**换一个轻量解码器到底要付多少代价？**
`kgoba/ft8_lib`（C、kiss-fft、无 OSD/AP/多 pass）是这条路线的代表作，
在 `/Users/cheenle/HAM/ft8_lib` 有一份 clone（HEAD `9fec6ca`）。

## 8.1 方法

- **同一台机器**：ZTE W103D（4×Cortex-A53 @1.8 GHz，Armbian 12）。
- **同一串输入**：用本仓自己的 `libwsjt_core`（`wsjt_ft8_encode`）合成
  `tests/ft8/dsp/test_i9_benchmark.py::busy_slot` 的那 5 个 CQ，
  落成 12 kHz 单声道 16-bit WAV，两个解码器读**同一个文件**。
  即复现 I9 基准的确定性信号，数字可与 `docs/ft8-m1-baseline.md` 直接对照。
- ft8_lib 侧：`make CFLAGS="-O3 -DHAVE_STPCPY -I."`，跑 `decode_ft8 <wav>`。
  （macOS 上需补 `-D_DARWIN_C_SOURCE`：上游源文件第 1 行的
  `#define _POSIX_C_SOURCE 199309L` 会挡掉 `snprintf` 声明。Linux/glibc 不受影响。）
- 本仓侧：`WorkerSupervisor` + 生产 `libwsjt_core.so`，profile 3 / threads 3 / cycles 1，
  即生产默认配置。
- 全部实验在盒子 `/tmp/ft8ab/` 下，**未改动 `/opt/mrrc_modern`**。

## 8.2 结果（A53，同机同输入）

先看等强度档位（5 个 CQ 全都在）。CPU 是**实测**的进程 CPU（ft8_lib 用 bash
内建 `time` 的 user+sys；本仓用 `RUSAGE_CHILDREN` 在 worker 回收后取差，
3 次请求取平均，含 worker 启动）：

| 配置 | wall | **CPU/请求（实测）** | 解出 | 并行效率 |
| --- | --- | --- | --- | --- |
| **ft8_lib**（单线程） | **145 ms** | **0.144 s** | **5/5** | 1.00× |
| 本仓 profile3/**threads1** | 3233 ms | **3.674 s** | 5/5 | 1.00× |
| 本仓 profile3/**threads3**（生产默认） | 2218 ms | **5.880 s** | 5/5 | 2.65× |

> **同一个答案：**
> - 比 wall：2218 / 145 ≈ **15.3×**
> - 比单核工作量：5.880 / 0.144 ≈ **41×**（对 1 线程配置是 3.674 / 0.144 ≈ 25×）

**这张表顺手实测坐实了第一轮 §1 的判断**：3 线程比 1 线程**多烧 1.6× 的总 CPU**
（5.880 vs 3.674），wall 却只快 1.46×——那多出来的 1.6× 就是被每线程原样复制的
重复劳动（根因 A/B），也解释了并行效率只有 2.65×/3。

注意 ft8_lib **单线程**、本仓用 3 个核。

## 8.3 灵敏度的代价：扫描到断点

把信号幅度逐级下调（噪声固定），两边都跑：

| 相对档位 | ft8_lib 耗时 / 解出 | 本仓 耗时 / 解出 |
| --- | --- | --- |
| 0 | 151 ms / **5** | 2185 ms / **5** |
| −6 | 156 ms / 5 | 1736 ms / 5 |
| −12 | 142 ms / 5 | 1508 ms / 5 |
| −18 | 137 ms / 5 | 1574 ms / 5 |
| −24 | 123 ms / 5 | 1523 ms / 5 |
| **−30** | 96 ms / **0** | 2136 ms / **3** |
| −36 | 93 ms / 0 | 1084 ms / 0 |
| −42 | 97 ms / 0 | 1078 ms / 0 |
| −48 | 100 ms / 0 | 1078 ms / 0 |
| −54 | 93 ms / 0 | 1316 ms / 0 |

**断点出现在 −24 与 −30 之间，而且只在一个 6 dB 台阶上分胜负。**

## 8.4 这个差距能不能"调回来"？——不能

ft8_lib 有个同步门限 `kMin_score`（默认 10，`demo/decode_ft8.c`）。
如果 −30 dB 的 0/5 只是门限太紧，那代价就是可买的。实测否定：

| `kMin_score` | −24 dB | −30 dB | −36 dB |
| --- | --- | --- | --- |
| 10（默认） | 5/5 (128 ms) | 0/5 (104 ms) | 0/5 (95 ms) |
| 8 | 5/5 (302 ms) | 0/5 (300 ms) | 0/5 (298 ms) |
| 6 | 5/5 (302 ms) | 0/5 (319 ms) | 0/5 (314 ms) |
| 4 | 5/5 (306 ms) | 0/5 (314 ms) | 0/5 (315 ms) |

降到 4（候选数已经多到让耗时翻倍）**仍然 0/5**。
所以那 3 条不是被同步门限挡住的——**是本仓那套 OSD + 多 pass + AP 结构买来的真实增益**。
（顺带确认默认 10 选得好：−24 dB 档放宽门限只涨时间、不涨条数。）

## 8.5 这个 A/B 没有回答什么（务必连着结论一起读）

1. **"−30 dB"不是 WSJT-X 意义上的 −30 dB SNR。** 那些数是**我合成信号幅度的相对衰减量**，
   绝对 SNR 未标定；两边打印的 SNR 也来自各自的粗略估计（ft8_lib 的
   `snr = cand->score * 0.5f` 作者自己标了 TODO），不能互相比较。所以**只能读"谁在哪个台阶掉队"，
   不能读"−30 dB"这个数值本身。**
2. **断点宽度 = 我的步长（6 dB）。** 真实跳变可能更窄；−30 dB 那格 3 vs 0 是
   **3 条消息**的小样本，每条约占 20%。
3. **AP 的增益完全没被测到。** 合成信号没有 QSO 上下文（`my_call`/`dx_call` 空），
   而 AP 靠的正是已知呼号。真实 QSO 里本仓的优势应比这里更大。
4. **没有重叠信号、没有不同起始时刻、没有衰落。** 5 个 CQ 互不干扰、同时开始——
   这是比真实频段**干净得多**的场景。
5. 只有 5 条消息，统计很粗。

## 8.6 对决策的含义

- **在容易到中等的信号上（本实测 ≥ −24 档），ft8_lib 用 1/15 的 wall / 约 1/41 的单核工作量，
  换来完全相同的 5/5。**
- **代价集中在一个约 6 dB 宽的窗口**（本例 −24…−30 之间），那里只有本仓能解出。
- 结合 §0 的核心矛盾（真正的闸门是 **2.5 s 决策截止**，profile 3 只剩 ~1.15× 余量）：
  这条路线的第一价值不是"换掉解码器"，而是**它证明了把候选路径从 2.4 s 压到 0.15 s 在物理上是可能的**——
  第二轮 §7.5 找到的 OSD 固定开销（每次 ~2.14 MB malloc + ~4.2 MB memset）
  和 §7.4 的调用倍率，正是这 41× 差距里可以攻击的部分。
- **不建议**直接换成 ft8_lib：FT8 的价值恰恰在弱信号，砍掉 OSD/AP 等于砍掉
  DX 与低功率场景，与整个项目目标相反。正确读法是**"上限在哪"**，不是"照抄它"。

## 8.7 复现

```bash
# 合成信号（任一台装了 scipy 的机器，需本仓 libwsjt_core）
.venv/bin/python /tmp/ft8ab/make_slot.py      # -> slot12k.wav
ssh w103d.vlsc.net 'cd /tmp/ft8ab/ft8_lib && ./decode_ft8 /tmp/ft8ab/slot12k.wav'
ssh w103d.vlsc.net 'cd /opt/mrrc_modern && ./venv/bin/python /tmp/ft8ab/mrrc_ab.py'
```

盒子上的实验材料都在 `/tmp/ft8ab/`（约 2 MB），可随时删除；
`/opt/mrrc_modern` 全程只读。注意 W103D 是**在跑的生产服务**，
本轮测量时 load 为 0.18（空闲），耗时数字与该状态下一致。

---

# 过程记录：替换解码器的决策（2026-10-13）

- **决定**：操作者选择「用 `ft8_lib` 替换现在的解码器」，并要求评估 ft8_lib 在 A53 上的进一步优化空间。
- **分类**：**架构级**改动——它重写 DSP 构建、解码接口，并改变 FT8 的核心能力。
  按本仓 SDD-guardian 与 brainstorming 的硬门，需先出 spec 再动产品代码；
  本文档此节只记录**决策过程与已知事实**，不含实现。

**接线面（只读勘察 `ft8/`）**

- FFI 边界是 `ft8/core/binding.py`（唯一的 ctypes 锁定点）；C ABI 在 `ft8/dsp/wsjt_core.h`：
  `wsjt_ft8_decode_standard` / `wsjt_ft8_decode_improved` / `wsjt_ft8_encode`。
- `_DecodeConfig` 携带**完整 AP 上下文**——`my_call` / `dx_call` / `dx_grid` /
  `ap_width` / `qso_progress`（`binding.py:78-88`），结果里带 `ap_type`（`binding.py:101`）。
  即**AP（先验呼号辅助解码）是一等公民、全链路打通**，不是可选装饰。
- 两条解码路径 `DecodePath.STANDARD / IMPROVED`，默认 IMPROVED（`ft8/core/models.py:45`）。
- 测试面：`tests/ft8/` 共 **419** 个用例（core 75 / dsp 43 / engine 242 / web 26 /
  platform 21 / 顶层 12），其中 I9 延迟验收门（`tests/ft8/dsp/test_i9_benchmark.py:234`）
  直接断言 headline 耗时 ≤ 决策截止。

**未决的关键问题（必须先回答，才谈设计）**

- `ft8_lib` **没有 AP、没有 OSD、没有多 pass**。全量替换 = 永久放弃
  弱信号能力（第三轮实测 −30 档 3/5 → 0/5）**并且**放弃 AP 增益（QSO 上下文）——
  而 AP 的增益在本轮 A/B 里**根本没被测到**（合成信号无 QSO 上下文，见 §8.5 第 3 条）。
- 所以「替换」有两种截然不同的含义，对应两套完全不同的设计：
  - **全量替换**：接受弱信号 + AP 的能力回退，换取 15× wall / 41× 单核。
  - **混合 / 分层**：ft8_lib 做快路径，WSJT-X 的 OSD/AP 只对难例兜底，**保留 AP**。
- **状态**：等待操作者回答上述问题，之后才进入「2–3 方案 → 设计 → spec」。

---

## 一句话总结

盒子对 8 s 判据有 3.7× 余量，但对实际生效的 **2.5 s 决策截止只有 ~1.15×**，
而且**加核买不到时间** —— 因为 sync 阶段被每线程原样复制、同一个 FFT
每时隙重算 3 遍（全机 3348 次 vs 最小 372 次）。
先花 10 分钟量 CPU time 坐实，再决定是走"零风险的构建/宿主优化"
还是"消除冗余的算法重构"。

第二轮补上了内核层的实测：**A53 上真正的杠杆是打断浮点累加依赖链**
（`sync8var` 128 条标量 `fadd` / 0 条向量），**而 `-mcpu=cortex-a53` 只是调度调优**；
同时 OSD 每次调用固定烧掉 ~2.14 MB malloc + ~4.2 MB memset，是**零数值风险**的第一刀。

第三轮做了 A/B：与轻量 C 解码器 `kgoba/ft8_lib` 在盒子上同机同输入对照，
**在 ≥ −24 档两者都 5/5，但本仓 wall 慢 15.3×、单核工作量多约 41×
（CPU/请求实测 5.880 s vs 0.144 s）**；
代价只出现在约 6 dB 宽的弱信号窗口（−30 档：本仓 3/5，ft8_lib 0/5），
而且**降同步门限到 1/2 也换不回来**——所以那是算法结构买来的真实增益，不是可调参数。
同一张表还实测坐实了第一轮 §1：3 线程比 1 线程多烧 1.6× 总 CPU，wall 只快 1.46×。
结论：这条路线的价值是**证明候选路径有 ~10× 的压缩空间**（对上第二轮 §7.4/§7.5 的浪费），
而不是替换解码器本身。
