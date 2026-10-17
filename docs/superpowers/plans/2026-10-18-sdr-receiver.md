# 独立 SDR 接收机 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推荐）或
> superpowers:executing-plans 逐任务实现。步骤用复选框（`- [ ]`）跟踪进度。

**Goal:** 在 W103D 盒子上把 SDRplay RSP1 变成一个对外发布的独立 Web 接收机——独立页面、独立口令、
全频段调谐、多人共享一台调谐器（30 s 持有锁）、每听众一路解调、50 人并发上限。

**Architecture:** 四层。C 层 `libsdr_dsp.so` 只做「2.4 MSPS → 每通道窄带 → PCM」（纯计算、无 I/O）；
进程层 `sdr_pipe.py` 独占设备（ctypes 调 `libsdrplay_api`）并把 DSP/频谱/Opus 组装成分帧的 IPC；
服务层 `sdr_hub.py` 持有仲裁、解调池、容量与扇出（纯逻辑、可无硬件测试）；接入层是 `server.py`
上的 `/WSsdr` + `/sdr` + 独立口令角色。

**Tech Stack:** C99 + libm（`gcc -O3 -ffast-math`，NEON 可选）、Python 3.11（ctypes / asyncio /
numpy / `unittest`）、浏览器端 AudioWorklet + WASM Opus（仓库既有）、SDRplay Hardware API 3.15（arm64）。

**Spec:** `docs/superpowers/specs/2026-10-18-sdr-receiver-design.md`

## Global Constraints

- **测试运行器**：仓库既有 `python -m unittest discover -s tests`；本计划新增的用例都必须能被它收集
  （文件名 `tests/test_sdr_*.py`，类继承 `unittest.TestCase`）。FT8 那套 `pytest` 与本计划无关。
- **C 库必须能在无硬件、任意平台编译**：`tests/test_sdr_dsp.py` 首次运行时用 `cc -O2 -shared -fPIC`
  把 `sdr_dsp.c` 编到临时目录再 ctypes 加载；找不到编译器则 `skipTest("no C compiler")`。
  这样 macOS 开发机与 VM 都能跑 DSP 单元测试。
- **设备只走 ctypes，且只有 `sdr_pipe.py` 一个进程碰它**（`libsdrplay_api` 是独占的）。任何测试都
  不得打开真实设备；需要用设备的用例一律 `skipUnless(os.environ.get("MRRC_SDR_HW")=="1")`。
- **不得改动** `static/ft710_main.js`、`static/ft710_ui.js`（工具守卫 + Android 端 1701 B 帧长契约），
  也不得改 `/WSspectrum` 的帧格式。SDR 用自己的帧（§Task 5 的 `0x01/0x02`）。
- **口令/令牌**：`MRRC_SDR_PASSWORD` 用 `hmac.compare_digest` 比较；令牌只经 cookie / Authorization
  传递，**绝不进 URL**（AD-024）；sdr 角色与 operator/listen 角色互不通用。
- **前缀安全**：`/sdr`、`/api/sdr/*`、`/WSsdr` 必须同时工作在 `/` 与 `/mrrc_modern/<call>/` 两种入口下
  （断言方式与 `tests/test_path_prefix.py` 一致）。
- **提交前**：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须干净。
- **平台范围 V1**：只有盒子（Linux/aarch64）启用；`MRRC_SDR=off` 时整块不装、不启动。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `sdr_dsp.h` / `sdr_dsp.c` | 融合 NCO + 多相抽取 + 解调（AM/USB/LSB/CW/NFM/WFM）+ AGC（纯计算） |
| `sdr_dsp_binding.py` | `libsdr_dsp` 的 ctypes 包装：`Channelizer` 类，一句 `process(block)` 出 PCM |
| `sdr_pipe.py` | 独占设备：Init/调谐/增益/过载、块循环、频谱 FFT、每通道 DSP+Opus、IPC 分帧、掉线退出 |
| `sdr_hub.py` | 仲裁（中心频率 + 持有锁）、解调池（引用计数）、容量闸门、频谱降档、订阅扇出 |
| `server.py` | `/sdr`、`/api/sdr/login`、`/WSsdr`、生命周期、`session_metrics` 打点 |
| `static/sdr.html` / `static/sdr.js` | 独立页面：瀑布、点选、预设、解调控件、音频播放 |
| `packaging/common/mrrc-service-layer.sh` | 装 `libsdrplay_api` + `sdrplay_apiService` + udev + blacklist；构建 `libsdr_dsp.so` |
| `docs/sdr_receiver.md` | 操作手册（天线、增益、过载、故障对照） |
| `_diag_sdr.py` | 硬件手测：识别/Init/采样率/各模式/过载/热插拔 |

---

# Phase P1 — 原生 DSP

## Task 1: `sdr_dsp` 骨架 + AM 单通道 + 构建/测试脚手架

**Files:**
- Create: `sdr_dsp.h`、`sdr_dsp.c`
- Create: `tests/test_sdr_dsp.py`

**Interfaces（后续任务全部依赖它，签名不得改）：**

```c
/* sdr_dsp.h */
typedef enum { SDR_AM=0, SDR_USB=1, SDR_LSB=2, SDR_CW=3, SDR_NFM=4, SDR_WFM=5 } sdr_mode_t;

typedef struct sdr_dsp sdr_dsp_t;

sdr_dsp_t *sdr_dsp_create(double fs_in, double fs_out, int max_ch);
void       sdr_dsp_destroy(sdr_dsp_t *d);
int        sdr_dsp_set_channel(sdr_dsp_t *d, int ch, double offset_hz, int mode, double bw_hz);
int        sdr_dsp_clear_channel(sdr_dsp_t *d, int ch);
int        sdr_dsp_process(sdr_dsp_t *d, const short *iq, int n, int nch,
                           short *out, int stride, int *out_n, float *rms);
```

- **验收（本任务）**：`fs_in=2_400_000`、`fs_out=48_000`、单通道 AM；
  1 kHz 调幅音灌进去，输出 PCM 的 1 kHz 分量幅度与理论值差 ≤ 1 dB；
  同时一个 **偏离中心 +30 kHz 的强音**（比目标信号高 40 dB）对输出 1 kHz 带内的影响 ≤ −35 dB（抗混叠）。
- `-DAC` 或 `-lstdc++`？**只要 libm**。

- [ ] **Step 1: 写失败的测试（含编译脚手架）**

`tests/test_sdr_dsp.py`：

```python
"""sdr_dsp (C) 的单元测试：合成信号，不需要 SDR 硬件。"""
import ctypes, os, shutil, subprocess, tempfile, unittest
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "sdr_dsp.c")

_lib = None


def build_lib():
    """把 sdr_dsp.c 编成临时共享库并加载；无编译器时返回 None。"""
    global _lib
    if _lib is not None:
        return _lib
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        return None
    out = os.path.join(tempfile.mkdtemp(prefix="sdrdsp"), "libsdr_dsp.so")
    cmd = [cc, "-O2", "-fPIC", "-shared", "-o", out, SRC, "-lm"]
    subprocess.run(cmd, check=True)
    lib = ctypes.CDLL(out)
    lib.sdr_dsp_create.restype = ctypes.c_void_p
    lib.sdr_dsp_create.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_int]
    lib.sdr_dsp_destroy.argtypes = [ctypes.c_void_p]
    lib.sdr_dsp_set_channel.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_double, ctypes.c_int, ctypes.c_double]
    lib.sdr_dsp_clear_channel.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.sdr_dsp_process.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_short), ctypes.c_int,
                                    ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_float)]
    _lib = lib
    return lib


class SdrDspTests(unittest.TestCase):
    FS = 2_400_000.0
    OUT = 48_000.0
    BLK = 4800                      # 2 ms @ 2.4 MSPS

    @classmethod
    def setUpClass(cls):
        cls.lib = build_lib()
        if cls.lib is None:
            raise unittest.SkipTest("no C compiler")

    def _iq(self, tones):
        """tones: [(offset_hz, amplitude), ...] -> int16 interleaved IQ."""
        t = np.arange(self.BLK) / self.FS
        z = np.zeros(self.BLK, np.complex128)
        for f, a in tones:
            z += a * np.exp(2j * np.pi * f * t)
        z = np.clip(z, -1, 1) * 12000
        iq = np.empty(self.BLK * 2, np.int16)
        iq[0::2] = z.real.astype(np.int16)
        iq[1::2] = z.imag.astype(np.int16)
        return iq

    def _run(self, tones, blocks=25, offset=0.0, mode=0, bw=6000.0):
        d = self.lib.sdr_dsp_create(self.FS, self.OUT, 4)
        self.assertIsNotNone(d)
        self.assertEqual(self.lib.sdr_dsp_set_channel(d, 0, offset, mode, bw), 0)
        out_n = ctypes.c_int(0)
        rms = (ctypes.c_float * 4)()
        out = (ctypes.c_short * (4 * 4096))()
        iq = self._iq(tones)
        c_iq = (ctypes.c_short * len(iq))(*iq)
        chunks = []
        for _ in range(blocks):
            self.assertEqual(self.lib.sdr_dsp_process(d, c_iq, self.BLK, 1, out, 4096,
                                                      ctypes.byref(out_n), rms), 0)
            chunks.append(np.array(out[:out_n.value], dtype=np.int16))
        self.lib.sdr_dsp_destroy(d)
        return np.concatenate(chunks), rms[0]

    def test_am_tone_amplitude(self):
        """AM：载波 + 1 kHz 边带，输出应含 1 kHz 且幅度合理。"""
        # 载波在中心，上边带 +1 kHz、下边带 -1 kHz，各 0.5 深度
        audio, _ = self._run([(0, 10000), (1000, 5000), (-1000, 5000)], blocks=50)
        self.assertGreater(len(audio), 48000 // 10)
        spec = np.abs(np.fft.rfft(audio.astype(np.float64) * np.hanning(len(audio))))
        freqs = np.fft.rfftfreq(len(audio), 1 / self.OUT)
        peak = freqs[np.argmax(spec)]
        self.assertAlmostEqual(peak, 1000, delta=30)

    def test_alias_rejection(self):
        """+30 kHz 的强干扰不得落进 1 kHz 带内（抗混叠）。"""
        clean, _ = self._run([(0, 10000), (1000, 2000), (-1000, 2000)], blocks=50)
        dirty, _ = self._run([(0, 10000), (1000, 2000), (-1000, 2000), (30000, 400000)], blocks=50)
        def tone_power(x):
            w = np.hanning(len(x))
            s = np.abs(np.fft.rfft(x * w))
            f = np.fft.rfftfreq(len(x), 1 / self.OUT)
            return float(np.sum(s[(f > 900) & (f < 1100)] ** 2))
        ratio_db = 10 * np.log10(tone_power(dirty) / max(tone_power(clean), 1e-9))
        self.assertLess(ratio_db, 1.0, f"alias leaked: {ratio_db:.1f} dB")
```

- [ ] **Step 2: 运行，确认失败**

```bash
python -m unittest tests.test_sdr_dsp -v
```
预期：`FileNotFoundError` / 编译失败（`sdr_dsp.c` 还不存在）。

- [ ] **Step 3: 写 `sdr_dsp.h`**

```c
#ifndef SDR_DSP_H
#define SDR_DSP_H

typedef enum { SDR_AM=0, SDR_USB=1, SDR_LSB=2, SDR_CW=3, SDR_NFM=4, SDR_WFM=5 } sdr_mode_t;

typedef struct sdr_dsp sdr_dsp_t;

sdr_dsp_t *sdr_dsp_create(double fs_in, double fs_out, int max_ch);
void       sdr_dsp_destroy(sdr_dsp_t *d);
int        sdr_dsp_set_channel(sdr_dsp_t *d, int ch, double offset_hz, int mode, double bw_hz);
int        sdr_dsp_clear_channel(sdr_dsp_t *d, int ch);
int        sdr_dsp_process(sdr_dsp_t *d, const short *iq, int n, int nch,
                           short *out, int stride, int *out_n, float *rms);

#endif
```

- [ ] **Step 4: 写 `sdr_dsp.c` 的最小实现（AM + 单级融合多相）**

```c
#include "sdr_dsp.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

#define MAXCH 32
#define MAXTAP 1024

typedef struct {
    int      active, mode;
    double   offset, bw;
    /* 融合多相：taps[t*D + p] = h[t*D+p] * exp(-j 2pi f (t*D+p) / fs) */
    float   *tre, *tim;
    int      dec, ntap;           /* ntap = D * taps_per_phase */
    float    dc, agc;             /* AM 去直流、AGC 增益 */
    float    prev_i, prev_q;      /* NFM/WFM 鉴频器状态 */
} chan_t;

struct sdr_dsp {
    double fs_in, fs_out;
    int    dec, max_ch;
    chan_t ch[MAXCH];
    float *buf_re, *buf_im;      /* 输入块（float） */
    float *ore, *oim;            /* 单通道信道化输出（复） */
    int    buf_cap;
};

/* Kaiser 窗 sinc；tp = taps per phase */
static int design_lowpass(double *h, int ntap, double fs, double bw, int tp)
{
    /* 通道带宽 bw 决定过渡带：截止在 bw/2，阻带从 fs/dec - bw/2 起（抗混叠） */
    double fc = bw * 0.5 / fs;                 /* 归一化截止（周期/样本） */
    double sum = 0.0;
    for (int i = 0; i < ntap; i++) {
        double x = (double)i - (double)(ntap - 1) / 2.0;
        double s = (fabs(x) < 1e-12) ? 2 * fc : sin(2 * M_PI * fc * x) / (M_PI * x);
        double w = 0.5 - 0.5 * cos(2 * M_PI * i / (ntap - 1));
        h[i] = s * w;
        sum += h[i];
    }
    for (int i = 0; i < ntap; i++) h[i] /= sum;
    (void)tp;
    return 0;
}

sdr_dsp_t *sdr_dsp_create(double fs_in, double fs_out, int max_ch)
{
    if (fs_in <= 0 || fs_out <= 0 || fs_in / fs_out < 2) return NULL;
    int dec = (int)lround(fs_in / fs_out);
    if (dec < 2 || dec > 512) return NULL;
    sdr_dsp_t *d = calloc(1, sizeof(*d));
    if (!d) return NULL;
    d->fs_in = fs_in; d->fs_out = fs_out; d->dec = dec;
    d->max_ch = max_ch > MAXCH ? MAXCH : max_ch;
    d->buf_cap = 1 << 16;
    d->buf_re = calloc(d->buf_cap, sizeof(float));
    d->buf_im = calloc(d->buf_cap, sizeof(float));
    d->ore    = calloc(d->buf_cap, sizeof(float));
    d->oim    = calloc(d->buf_cap, sizeof(float));
    if (!d->buf_re || !d->buf_im || !d->ore || !d->oim) { sdr_dsp_destroy(d); return NULL; }
    return d;
}

void sdr_dsp_destroy(sdr_dsp_t *d)
{
    if (!d) return;
    for (int i = 0; i < MAXCH; i++) { free(d->ch[i].tre); free(d->ch[i].tim); }
    free(d->buf_re); free(d->buf_im); free(d->ore); free(d->oim); free(d);
}

int sdr_dsp_set_channel(sdr_dsp_t *d, int ch, double offset_hz, int mode, double bw_hz)
{
    if (!d || ch < 0 || ch >= d->max_ch) return -1;
    if (mode < SDR_AM || mode > SDR_WFM) return -1;
    if (bw_hz <= 0 || bw_hz >= d->fs_out) return -1;

    chan_t *c = &d->ch[ch];
    free(c->tre); free(c->tim);
    /* taps per phase：过渡带越窄要得越多；带宽越窄越便宜 */
    double ft = (d->fs_out - bw_hz) * 0.5;            /* 半过渡带（Hz） */
    int tp = (int)ceil(4.5 * d->fs_in / (2.0 * ft * d->dec));
    if (tp < 2) tp = 2;
    if (tp > 64) tp = 64;
    int ntap = tp * d->dec;
    if (ntap > MAXTAP) { ntap = MAXTAP - MAXTAP % d->dec; tp = ntap / d->dec; }
    c->tre = calloc(ntap, sizeof(float));
    c->tim = calloc(ntap, sizeof(float));
    if (!c->tre || !c->tim) return -1;

    double *h = malloc(sizeof(double) * ntap);
    if (!h) return -1;
    design_lowpass(h, ntap, d->fs_in, bw_hz, tp);
    for (int i = 0; i < ntap; i++) {
        double ph = -2.0 * M_PI * offset_hz * i / d->fs_in;
        c->tre[i] = (float)(h[i] * cos(ph));
        c->tim[i] = (float)(h[i] * sin(ph));
    }
    free(h);
    c->ntap = ntap; c->dec = d->dec; c->active = 1; c->mode = mode;
    c->offset = offset_hz; c->bw = bw_hz;
    c->dc = 0.0f; c->agc = 1.0f; c->prev_i = c->prev_q = 0.0f;
    return 0;
}

int sdr_dsp_clear_channel(sdr_dsp_t *d, int ch)
{
    if (!d || ch < 0 || ch >= d->max_ch) return -1;
    d->ch[ch].active = 0;
    return 0;
}

/* 单通道：融合 NCO+多相抽取，输出 *nout 个复样本到 re/im */
static int channelize(chan_t *c, const float *re, const float *im, int n,
                      float *ore, float *oim, int *nout)
{
    int D = c->dec, nt = c->ntap, tp = nt / D;
    int k = 0;
    for (int s = 0; s + nt <= n; s += D) {
        float ar = 0.0f, ai = 0.0f;
        for (int t = 0; t < tp; t++) {
            const int xo = s + t * D, ho = t * D;
            for (int p = 0; p < D; p++) {
                float xr = re[xo + p], xi = im[xo + p];
                float hr = c->tre[ho + p], hi = c->tim[ho + p];
                ar += xr * hr - xi * hi;
                ai += xr * hi + xi * hr;
            }
        }
        ore[k] = ar; oim[k] = ai; k++;
    }
    *nout = k;
    return 0;
}

int sdr_dsp_process(sdr_dsp_t *d, const short *iq, int n, int nch,
                    short *out, int stride, int *out_n, float *rms)
{
    if (!d || !iq || n <= 0 || !out || !out_n) return -1;
    if (n > d->buf_cap) n = d->buf_cap;
    for (int i = 0; i < n; i++) {
        d->buf_re[i] = iq[2 * i]     / 32768.0f;
        d->buf_im[i] = iq[2 * i + 1] / 32768.0f;
    }
    int max_n = 0;
    for (int ch = 0; ch < nch && ch < d->max_ch; ch++) {
        chan_t *c = &d->ch[ch];
        if (!c->active) continue;
        int nout = 0;
        channelize(c, d->buf_re, d->buf_im, n, d->ore, d->oim, &nout);
        short *o = out + (size_t)ch * stride;
        float sum = 0.0f;
        for (int i = 0; i < nout; i++) {
            float v;
            switch (c->mode) {
            case SDR_AM:  v = sqrtf(d->ore[i]*d->ore[i] + d->oim[i]*d->oim[i]); break;
            default:      v = d->ore[i]; break;                 /* Task 2 补齐 */
            }
            /* AM 去直流（一阶高通） */
            c->dc += 0.0005f * (v - c->dc);
            v -= c->dc;
            /* 简易 AGC：目标 RMS 0.1 */
            float mag = fabsf(v);
            if (mag > 1e-4f) c->agc = 0.9995f * c->agc + 0.0005f * (0.1f / mag);
            if (c->agc > 32.0f) c->agc = 32.0f;
            if (c->agc < 0.03f) c->agc = 0.03f;
            v *= c->agc;
            if (v > 1.0f) v = 1.0f;
            if (v < -1.0f) v = -1.0f;
            o[i] = (short)(v * 32767.0f);
            sum += v * v;
        }
        if (rms) rms[ch] = nout ? sqrtf(sum / nout) : 0.0f;
        if (nout > max_n) max_n = nout;
    }
    *out_n = max_n;
    return 0;
}
```

- [ ] **Step 5: 运行测试，确认通过**

```bash
python -m unittest tests.test_sdr_dsp -v
```
预期：2 个用例 PASS。（若 `test_alias_rejection` 失败，把 `tp` 公式里的 `4.5` 调到 6–8，
再跑一次；**不许放宽测试阈值**。）

- [ ] **Step 6: 量 CPU，写入 `docs/sdr_receiver.md` 的表**

在盒子上：

```bash
gcc -O3 -ffast-math -I. -o /tmp/dsp_bench tools/sdr_dsp_bench.c sdr_dsp.c -lm && /tmp/dsp_bench
```
（`tools/sdr_dsp_bench.c` 本步创建：4 个通道、每通道 6 kHz AM，跑 10 s 实时流的 CPU 时间，打印核数。）

**门槛与实测（2026-10-18，W103D 盒子，已写入规格 §1）**：
- AM 6 kHz：**0.086 核/通道**；USB 3 kHz：**0.151 核/通道**；内循环 4 路展开只再快 3%
  （瓶颈是算术量 ≈ 4.5×fs_in，不是循环开销）。
- ⇒ `MRRC_SDR_MAX_CHANNELS` 默认 **8**（AM 0.69 核 / SSB 1.23 核），上限 16。
  想要更多通道只能换架构（共享 FFT 信道化器），已作为 R10 记入规格。
超了**不许改测试**：先调 `tp` 公式（4.5 → 5.5）再量；仍超 ⇒ 改默认值并同步规格的数。

- [ ] **Step 7: Commit**

```bash
git add sdr_dsp.h sdr_dsp.c tools/sdr_dsp_bench.c tests/test_sdr_dsp.py
git commit -m "feat(sdr): 原生信道化+AM 解调与合成信号测试"
```

## Task 2: 解调模式 SSB / CW / NFM / WFM

**Files:**
- Modify: `sdr_dsp.c`（`channelize` 之后增加按模式的后处理）
- Modify: `tests/test_sdr_dsp.py`（新增用例）

**Interfaces:** 复用 Task 1 的五个 C 函数；`mode` 语义：
`SDR_USB/LSB/CW` = 取实部（第二级是**单边**复数滤波器，选择性在这里，第一级的 NCO 只负责把载波搬到 0）；
`SDR_NFM` = 相位差分鉴频；**`SDR_WFM` 本任务不做**（见 Task 2b）。

- **验收**：USB 下 +1 kHz 音有输出、−1 kHz 音被抑制 ≥ 40 dB；NFM 下 750 Hz 调频音可恢复。

- [ ] **Step 1: 写失败测试**

在 `SdrDspTests` 里追加：

```python
    def test_ssb_sideband_rejection(self):
        """USB 模式：+1 kHz 通过，-1 kHz 至少低 40 dB。"""
        up, _ = self._run([(1000, 8000)], offset=0.0, mode=1, bw=3000)   # mode=1 -> USB
        dn, _ = self._run([(-1000, 8000)], offset=0.0, mode=1, bw=3000)
        def p(x):
            w = np.hanning(len(x)); s = np.abs(np.fft.rfft(x * w)).astype(float)
            f = np.fft.rfftfreq(len(x), 1 / self.OUT)
            return float(np.sum(s[(f > 800) & (f < 1200)] ** 2))
        self.assertGreater(p(up), 10 * p(dn), "USB did not reject the lower sideband")

    def test_nfm_discriminator(self):
        """NFM：1 kHz 调频音应恢复出 1 kHz 基带。"""
        t = np.arange(self.BLK) / self.FS
        phase = 2 * np.pi * 1000 * np.cumsum(np.sin(2 * np.pi * 750 * t)) / self.FS * 1500
        z = 12000 * np.exp(1j * phase)
        iq = np.empty(self.BLK * 2, np.int16)
        iq[0::2] = z.real.astype(np.int16); iq[1::2] = z.imag.astype(np.int16)
        d = self.lib.sdr_dsp_create(self.FS, self.OUT, 4)
        self.assertEqual(self.lib.sdr_dsp_set_channel(d, 0, 0.0, 4, 12500.0), 0)  # mode=4 -> NFM
        out_n = ctypes.c_int(0); rms = (ctypes.c_float * 4)()
        out = (ctypes.c_short * 4096)()
        c_iq = (ctypes.c_short * len(iq))(*iq)
        chunks = []
        for _ in range(50):
            self.lib.sdr_dsp_process(d, c_iq, self.BLK, 1, out, 4096,
                                     ctypes.byref(out_n), rms)
            chunks.append(np.array(out[:out_n.value], np.int16))
        self.lib.sdr_dsp_destroy(d)
        audio = np.concatenate(chunks).astype(float)
        spec = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
        f = np.fft.rfftfreq(len(audio), 1 / self.OUT)
        self.assertAlmostEqual(f[np.argmax(spec)], 750, delta=60)
```

- [ ] **Step 2: 运行，确认失败**（USB 未实现 ⇒ 边带抑制断言失败）

```bash
python -m unittest tests.test_sdr_dsp -v
```

- [ ] **Step 3: 在 `sdr_dsp_process` 的 `switch` 里补模式**

```c
            case SDR_AM:
                v = sqrtf(ore[i]*ore[i] + oim[i]*oim[i]);
                break;
            case SDR_USB:
            case SDR_LSB:
            case SDR_CW:
                v = ore[i];                                   /* NCO 已把边带搬到基带 */
                break;
            case SDR_NFM:
            case SDR_WFM: {
                float ph = atan2f(oim[i], ore[i]);
                float dph = ph - atan2f(c->prev_q, c->prev_i);
                while (dph >  M_PI) dph -= 2 * M_PI;
                while (dph < -M_PI) dph += 2 * M_PI;
                v = dph * 0.7f;                               /* 鉴频 + 去重音近似 */
                break;
            }
            default: v = ore[i]; break;
```
并在循环尾部更新鉴频器状态：

```c
            c->prev_i = ore[i]; c->prev_q = oim[i];
```

`set_channel` 里按模式给 NCO 加偏置（CW 音调、SSB 边带选择）：

```c
    double nco = offset_hz;
    if (mode == SDR_CW)  nco += 600.0;          /* CW 拍频 */
    if (mode == SDR_LSB) nco -= bw_hz * 0.5;    /* 让下边带落进基带 */
    if (mode == SDR_USB) nco += bw_hz * 0.5;
```

- [ ] **Step 4: 运行，确认通过**

```bash
python -m unittest tests.test_sdr_dsp -v
```
预期：6 个用例 PASS（AM 频响、AM 非静音、抗混叠、SSB 边带抑制、NFM 鉴频、非法参数）。

> **实现记录（2026-10-18）**：这一步踩到两个真缺陷，改法已落在代码里：
> ① 单级滤波器不可能"既抗混叠又选边带"——抽头数按抗混叠定就选不出边带，选定边带就抽头爆炸；
>    改成两级（fs_in→96 kHz 中等滤波，96 kHz→48 kHz 尖锐复数滤波）。
> ② 多相实现是**相关**（抽头反向），对复数抽头等价于共轭，频移方向会反；
>    两级的 NCO 相位符号都已翻正，并由 `test_ssb_sideband_rejection` 钉住。
> 测试侧两个真问题：每块重复同一段信号会在块长的倒数处造出假谱线（NFM 测试改成连续流）；
> 干扰音幅度必须落在 int16 范围内，否则测的是削波不是混叠。
> WFM 需要"中间速率上先鉴频、再抽取音频"的第三条管线，本任务不做（Task 2b）。

- [ ] **Step 5: Commit**

```bash
git add sdr_dsp.c tests/test_sdr_dsp.py
git commit -m "feat(sdr): SSB/CW/NFM/WFM 解调与边带抑制测试"
```

## Task 2b: WFM 宽通道（第三条管线）

**Files:** Modify `sdr_dsp.c`、`tests/test_sdr_dsp.py`

**为什么单独一条**：两级抽取的中间速率是 96 kHz，装不下 200 kHz 的调频信道。WFM 必须先**在中间速率上鉴频**，
再把音频抽取到 48 kHz —— 顺序与窄带模式相反，所以不能塞进同一条路径。

**结构**：`fs_in →(dec=5)→ 480 kHz → 相位差分鉴频 → 音频 →(dec=10)→ 48 kHz`。
`set_channel` 对 `mode==SDR_WFM` 选 `dec1 = fs_in/480k`（2.4M→5）、`dec2 = 480k/48k = 10`，
第二级用**实数**低通（音频，截止 15 kHz）。中间缓冲按该通道自己的 `dec1/dec2` 用，`chan_t` 里已有字段。

- [ ] **Step 1: 写失败测试**：调频 1 kHz 正弦（频偏 75 kHz）在 100 MHz 中心，
  输出应恢复 1 kHz、且 `len(pcm)` 与 48 kHz 时间轴一致：

```python
    def test_wfm_wideband(self):
        n_all = self.BLK * 50
        t = np.arange(n_all) / self.FS
        phase = 2 * np.pi * 75000 * np.cumsum(np.sin(2 * np.pi * 1000 * t)) / self.FS
        z = 12000 * np.exp(1j * phase)
        iq_all = np.empty(n_all * 2, np.int16)
        iq_all[0::2] = z.real.astype(np.int16); iq_all[1::2] = z.imag.astype(np.int16)
        lib = self.lib
        d = lib.sdr_dsp_create(self.FS, self.OUT, 4)
        self.assertEqual(lib.sdr_dsp_set_channel(d, 0, 0.0, 5, 200000.0), 0)   # WFM
        out_n = ctypes.c_int(0); rms = (ctypes.c_float * 4)()
        out = (ctypes.c_short * 8192)()
        chunks = []
        for b in range(50):
            seg = iq_all[b * self.BLK * 2:(b + 1) * self.BLK * 2]
            c_iq = (ctypes.c_short * len(seg))(*seg)
            lib.sdr_dsp_process(d, c_iq, self.BLK, 1, out, 8192, ctypes.byref(out_n), rms)
            chunks.append(np.array(out[:out_n.value], np.int16))
        lib.sdr_dsp_destroy(d)
        audio = np.concatenate(chunks).astype(np.float64)[2400:]
        spec = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
        f = np.fft.rfftfreq(len(audio), 1 / self.OUT)
        self.assertAlmostEqual(f[int(np.argmax(spec))], 1000, delta=60)
```

- [ ] **Step 2: 跑红**（`set_channel` 返回 −1：带宽 ≥ fs_out 被拒）
- [ ] **Step 3: 实现**：`set_channel` 里按 `mode==SDR_WFM` 分支选 `dec1/dec2/fs_mid`；
  `sdr_dsp_process` 里 WFM 走"先鉴频后抽取"的顺序（第一级输出 → 鉴频 → 实数低通抽取）；
  放宽带宽守卫为 `bw_hz < fs_in * 0.5`。
- [ ] **Step 4: 跑绿** + 重新跑 `tools/sdr_dsp_bench.c` 量 WFM 的每通道成本（写进规格 §1 表）
- [ ] **Step 5: Commit** `feat(sdr): WFM 宽通道（先鉴频后抽取）`

## Task 3: `sdr_dsp_binding.py`（ctypes 包装）

**Files:**
- Create: `sdr_dsp_binding.py`
- Create: `tests/test_sdr_dsp_binding.py`

**Interfaces（`sdr_pipe.py` 依赖）：**

```python
class Channelizer:
    def __init__(self, fs_in: float, fs_out: float = 48000.0, max_ch: int = 32)
    def set_channel(self, chid: int, offset_hz: float, mode: int, bw_hz: float) -> None
    def clear_channel(self, chid: int) -> None
    def process(self, iq: "np.ndarray[np.int16]", nchan: int) -> tuple[list[bytes], list[float]]
        # 返回 (每通道 int16 PCM bytes, 每通道 rms)
    @staticmethod
    def locate() -> str | None      # 找 libsdr_dsp.so：MRRC_SDR_DSP_LIB → 仓库根 → /usr/local/lib
```

- **验收**：用合成 IQ 走一遍 `set_channel`+`process`，PCM 长度 = `len(iq)/2 * 48000/fs_in`（±1 样本），
  且 `locate()` 在库缺失时返回 `None` 而不是抛异常（保证桌面版能优雅降级）。

- [ ] **Step 1: 写失败测试**

```python
"""sdr_dsp_binding 的测试：库存在就跑，缺失则跳过（桌面版路径）。"""
import os, unittest
import numpy as np

from sdr_dsp_binding import Channelizer
from tests.test_sdr_dsp import build_lib


class BindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if build_lib() is None:
            raise unittest.SkipTest("no C compiler / lib")
        os.environ["MRRC_SDR_DSP_LIB"] = build_lib()._name
        cls.cz = Channelizer(2_400_000.0, 48000.0, 4)

    def test_process_returns_pcm_and_rms(self):
        self.cz.set_channel(0, 0.0, 0, 6000.0)
        t = np.arange(48000) / 2_400_000.0
        z = (12000 * np.exp(2j * np.pi * 1000 * t)).astype(np.complex64)
        iq = np.empty(96000, np.int16)
        iq[0::2] = z.real; iq[1::2] = z.imag
        pcm, rms = self.cz.process(iq, 1)
        self.assertEqual(len(pcm), 1)
        self.assertAlmostEqual(len(pcm[0]) // 2, 960, delta=40)
        self.assertGreater(rms[0], 0.0)

    def test_missing_library_is_graceful(self):
        os.environ["MRRC_SDR_DSP_LIB"] = "/nonexistent/libsdr_dsp.so"
        self.assertIsNone(Channelizer.locate())
```

- [ ] **Step 2: 运行，确认失败**（模块不存在）
- [ ] **Step 3: 实现 `sdr_dsp_binding.py`**（ctypes 类型、`process` 里用 `np.ctypeslib` 传 buffer、返回 `bytes`）
- [ ] **Step 4: 运行，确认通过**
- [ ] **Step 5: Commit** `feat(sdr): ctypes 绑定`

---

# Phase P2 — 设备进程

## Task 4: `sdr_pipe.py`（设备 + 块循环 + IPC + `--offline`）

**Files:**
- Create: `sdr_pipe.py`
- Create: `tests/test_sdr_pipe.py`

**接口（IPC，服务端与 pipe 之间的唯一契约）：**

| 方向 | 帧 | 说明 |
|---|---|---|
| pipe → server | `b"P1" + u32 len + json` | 状态：`{"center_hz", "fs", "gain", "overload_per_min", "serial"}` |
| pipe → server | `b"S1" + u16 seq + u16 n + u8*n` | 频谱（n = 1024） |
| pipe → server | `b"A1" + u8 chid + u16 seq + u16 len + opus` | 音频（`len` = opus 包长度） |
| server → pipe | `b"C1" + u32 len + json` | 命令：`{"cmd":"tune","hz":…}` / `channel` / `gain` / `quit` |

- [ ] **Step 1: 写失败测试（离线模式，不需要硬件）**

```python
"""sdr_pipe 的离线模式测试：喂 IQ 文件，断言帧序列。"""
import json, os, struct, subprocess, sys, tempfile, unittest
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class OfflinePipeTests(unittest.TestCase):
    def test_offline_emits_status_spectrum_audio(self):
        path = os.path.join(tempfile.mkdtemp(), "iq.iq")
        n = 48000 * 2
        t = np.arange(n) / 2_400_000.0
        z = 12000 * np.exp(2j * np.pi * 1000 * t)
        iq = np.empty(n * 2, np.int16)
        iq[0::2] = z.real; iq[1::2] = z.imag
        iq.tofile(path)

        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "sdr_pipe.py"),
             "--offline", path, "--seconds", "0.2", "--no-rt"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err.decode()[-500:])

        kinds, status = set(), None
        i = 0
        while i + 6 <= len(out):
            tag = out[i:i+2]
            if tag == b"P1":
                ln = struct.unpack_from("<I", out, i+2)[0]
                status = json.loads(out[i+6:i+6+ln]); i += 6 + ln; kinds.add("P1")
            elif tag == b"S1":
                n = struct.unpack_from("<H", out, i+4)[0]
                i += 6 + n; kinds.add("S1")
            elif tag == b"A1":
                ln = struct.unpack_from("<H", out, i+5)[0]
                i += 7 + ln; kinds.add("A1")
            else:
                self.fail(f"unknown frame tag {tag!r} at {i}")
        self.assertEqual(kinds, {"P1", "S1", "A1"})
        self.assertIn("center_hz", status)
```

- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现 `sdr_pipe.py`**
  - `--offline FILE`：按 `fs` 读文件、按 20 ms 块切、`--no-rt` 时不 sleep（测试用）。
  - 设备模式：ctypes 加载 `libsdrplay_api.so.3`（沿用本设计验证脚本的结构：
    `Open/GetDevices/SelectDevice/GetDeviceParams/Init`，`LNAstate` 必须 ≤ 3（RSP1 上限，
    实测越界会 `Init=1` 且服务日志打印 `gain LNAstate out of range`）。
  - 回调里只做拷贝进环形缓冲；主循环每 20 ms 取一块：FFT（1024 bin，`np.abs` → u8 归一）
    + `Channelizer.process` + `opus_rx` 编码。
  - stdin 线程读 `C1` 帧，改中心频率/通道/增益。
  - 设备掉线（`sdrplay_api_DeviceRemoved` 事件）⇒ 退出码 3；服务端据此重启。
  - 过载事件 ⇒ `sdrplay_api_Update(Ctrl_OverloadMsgAck)` + 计数；每分钟 > 20 次时自动 `gRdB -= 6`（下限 20）。
- [ ] **Step 4: 运行，确认通过**
- [ ] **Step 5: Commit**

## Task 5: `sdr_hub.py`（仲裁 / 解调池 / 容量 / 降档）

**Files:**
- Create: `sdr_hub.py`
- Create: `tests/test_sdr_hub.py`

**核心数据结构（后续任务依赖）：**

```python
@dataclass(frozen=True)
class ChannelKey:
    offset_hz: int
    mode: int
    bw_hz: int

class SdrHub:
    def __init__(self, pipe, clock=time.monotonic, hold_s=30.0,
                 max_clients=50, max_channels=24, spectrum_tier="auto")
    def attach(self, session_id: int) -> "Subscriber"      # 建立订阅；超限抛 CapacityError
    def detach(self, sub: "Subscriber") -> None
    def tune(self, sub, center_hz: int) -> tuple[bool, int]  # (ok, retry_after_s)
    def subscribe_channel(self, sub, key: ChannelKey) -> int  # 返回 chid，满则抛 ChannelFull
    def unsubscribe_channel(self, sub, chid: int) -> None
    def on_spectrum(self, seq: int, bins: bytes) -> None     # 扇出
    def on_audio(self, chid: int, payload: bytes) -> None
    def snapshot(self) -> dict
```

- **验收（全部为纯逻辑测试）**：
  1. `tune` 首次成功、第二次（别的会话）在 30 s 内被拒（`retry_after` 正确递减）；
  2. 持有者 `detach` 后立即可抢；
  3. 两个订阅者请求同一 `ChannelKey` ⇒ 同一 `chid` 且 `active_channels == 1`；两个都退订后归零；
  4. 第 25 个不同 key ⇒ `ChannelFull`；
  5. 第 51 个客户端 ⇒ `CapacityError`；
  6. 客户端数 12 → 13 时频谱档从 1024 切到 512；
  7. `tune` 后每个订阅者的 `abs_freq` 不变（按绝对频率重算 offset），越界被钳到窗边。

- [ ] **Step 1–5**：按顺序写测试 → 跑红 → 实现 → 跑绿 → commit。每个用例对应上面 7 条验收之一，测试里用
  `FakePipe`（记录调用）与 `FakeClock`（手动推进），**不得 sleep**。

## Task 6: `server.py` 接线（鉴权 / 端点 / 指标 / 生命周期）

**Files:**
- Modify: `server.py`（新增约 260 行，位置：`/WSspectrum` 之后、`/WSatr1000` 之前）
- Create: `tests/test_server_sdr.py`

**要点：**
- `MRRC_SDR_PASSWORD` 为空 ⇒ 不注册 `/sdr`、`/api/sdr/login`、`/WSsdr`（返回 404/403），不建 pipe。
- `POST /api/sdr/login`：`hmac.compare_digest` 比较；成功下发 `mrrc_sdr` cookie（30 天，
  存入既有 `data/auth-tokens.json`，`role: "sdr"`）。
- `_token_from_ws_handshake()` 增加角色过滤：`sdr` 角色只能进 `/WSsdr`；operator/listen 角色进不去。
- `/sdr`：`FileResponse(static/sdr.html)`，`Cache-Control: no-cache`（与 HTML 一致）。
- `/WSsdr`：`accept()` → `hello` → 注册进 hub；异常/断开发 `detach`。
- 生命周期：lifespan 里 `SdrHub.start()`（惰性：第一个客户端来了才起 pipe）；`MRRC_SDR=off` 或库缺失
  或设备不可用 ⇒ `snapshot()["available"] = False` + `reason`，页面显示红条。
- `session_metrics`：新增 `kind` 为 `"sdr"` 的 socket 计数与 `audio_sdr`/`spectrum_sdr` 上行字节。

- [ ] **Step 1: 写失败测试**（用 `starlette.testclient`，与既有 `tests/test_server_ws_protocol.py` 同风格）
  - `MRRC_SDR_PASSWORD` 未设 ⇒ `/sdr` 404；
  - 口令错误 ⇒ 403 且不下发 cookie；
  - 口令正确 ⇒ cookie `mrrc_sdr` 存在，用它连 `/WSsdr` 收到 `hello`，用同一 cookie 连 `/WSradio`
    **被拒**；operator token 连 `/WSsdr` **被拒**；
  - 客户端数超限 ⇒ 收到 `4003`；
  - `/sdr` 在两种前缀下都能取到（前缀安全）；
  - 令牌不得出现在任何 URL（AST/字符串断言，与 `tests/test_ws_token_transport.py` 同法）。
- [ ] **Step 2–5**：跑红 → 实现 → 跑绿 → commit。

---

# Phase P3 — 前端

## Task 7: `static/sdr.html` + `static/sdr.js`

**Files:**
- Create: `static/sdr.html`、`static/sdr.js`
- Modify: `static/sw.js`（`CACHE` 版本 bump，**一行 diff**）
- Create: `tests/test_sdr_frontend.py`（契约测试）

**页面元素（契约测试逐条断言 id 存在）：** `#wf`（canvas 瀑布）、`#freq-scale`、`#center-readout`、
`#offset-readout`、`#presets`（按钮条）、`#mode`、`#bw`、`#squelch`、`#volume`、`#level`、
`#status`（红条/正常）、`#listeners`、`#lock`（倒计时）。

**行为：**
- 连 `/WSsdr`（cookie 自动带）；`hello` → 填能力；`state` → 更新读数与锁倒计时；
- 点瀑布 = 选 offset（本地立即高亮，`channel` 请求防抖 120 ms）；
- 拖动 = `tune`（锁定时按钮置灰 + 显示剩余秒）；
- 滚轮 = ±步进（SSB 100 Hz / AM 1 kHz / FM 5 kHz）；
- 二进制帧解析：`0x01` 画瀑布（1024 或 512 bin 自适应）、`0x02` 喂 `rx_worklet_processor.js`；
- 断线重连（指数退避，最多 5 次）后重放当前 `channel`；
- 无设备：`#status` 显示 `reason`，隐藏控件。

- [ ] **Step 1: 写失败测试**（`tests/test_sdr_frontend.py`：读文件、断言 id 与关键调用、断言
  `sdr.js` 不含 `token=`、断言所有资源是相对路径）
- [ ] **Step 2–5**：跑红 → 实现 → 跑绿 → commit

---

# Phase P4 — 打包与验收

## Task 8: 打包（`MRRC_SDR` 开关 + 服务层）

**Files:**
- Modify: `packaging/common/mrrc-service-layer.sh`（装 API 库/daemon/udev/blacklist；构建 `libsdr_dsp.so`）
- Modify: `linux/mrrc_update.sh`（幂等）
- Modify: `packaging/box/README.md`、`docs/w103d_pack.md`
- Modify: `tests/test_packaging_parity.py`（新增断言）

**验收：** 服务层脚本在 `MRRC_SDR=off` 时**不写**任何 SDR 文件；在 `on` 时写齐 6 项
（`/usr/local/lib/libsdrplay_api.so.3.15`、`/opt/sdrplay_api/sdrplay_apiService`、
`/etc/systemd/system/sdrplay.service`、`/etc/udev/rules.d/66-sdrplay.rules`、
`/etc/modprobe.d/mrrc-sdrplay.conf`、`/usr/local/lib/libsdr_dsp.so`），且 `mrrc_update.sh`
第二次运行时文件内容不变（幂等）。

- [ ] **Step 1–5**：测试（用临时目录 + 假 root 重定向）→ 实现 → 跑绿 → commit

## Task 9: 文档同步 + 硬件验收

**Files:**
- Create: `docs/sdr_receiver.md`、`_diag_sdr.py`
- Modify: `AGENTS.md`、`README.md`、`tests/README.md`、`SDD/07`、`SDD/08`（AD-029）、`SDD/09`、
  `SDD/10`、`SDD/12`、`SDD/14`、`SDD/README.md`（Quick Facts）

**硬件验收清单（`_diag_sdr.py` 逐条打印）：**
1. `lsusb` 见 `1df7:2500`；`msi2500` 未加载；
2. `GetDevices` 报型号与序列号；
3. `Init` 成功；`2.4 / 4.8 MSPS` 各收 2 s；
4. 每个解调模式在已知载波上出音频（本地 FM 台 / 标准时间信号）；
5. 过载事件 → 自动降 6 dB；
6. 拔插 USB → pipe 退出码 3，服务端 1/2/5/10 s 退避重启，恢复后回到原中心频率；
7. 12 客户端 + 4 通道时 pipe 的 CPU（`systemctl show sdrplay -p CPUUsageNSec` 前后差 + `/proc`）。

- [ ] **Step 1–5**：文档 → 手测 → 记录实测数 → commit

---

## Self-check（对照规格）

| 规格条目 | 落在哪个 Task |
|---|---|
| §3.1 全频段调谐、1 Hz 步进 | Task 4（`tune` 命令）+ Task 7（输入） |
| §3.2 2/4 MSPS 窗、10 MSPS 手动 | Task 4（`fs` 参数）+ Task 5（10 MSPS 时通道上限 8） |
| §3.3 五种解调 + 带宽档 + AGC/静噪/音量 | Task 2（窄带解调）、Task 2b（WFM）、Task 5（静噪/音量在扇出时） |
| §3.4 ≤400 ms | Task 7（220 ms 抖动缓冲沿用既有 worklet） |
| §3.5 50 人 / 24 路 / 复用 | Task 5 |
| §3.6 带宽预算 | Task 5（频谱降档）+ Task 6（指标） |
| §3.7 口令 / 角色隔离 / 前缀安全 / 不暴露电台 | Task 6 |
| §3.8 隔离（SDR 崩不影响电台） | Task 4（独立进程）+ Task 6（生命周期） |
| §5 协议 | Task 4（pipe IPC）、Task 6（WS） |
| §6 持有锁 / 绝对频率重算 | Task 5 |
| §7 容量 / 降级 | Task 4（重连、过载）、Task 5（闸门）、Task 6（提示）；通道上限 8 由 Task 1 实测钉住 |
| §9 打包 | Task 8 |
| §10 测试 | 每个 Task 的测试 + Task 9 硬件清单 |
| §11 文档同步 | Task 9 |
