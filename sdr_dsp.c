/*
 * sdr_dsp -- SDR 信道化 + 解调的纯计算实现。
 *
 * 设计：docs/superpowers/specs/2026-10-18-sdr-receiver-design.md
 * 计划：docs/superpowers/plans/2026-10-18-sdr-receiver.md
 *
 * 两级信道化（单级做不到"既抗混叠又有边带选择性"——抽头数会爆炸）：
 *   第一级  fs_in -> fs_mid(96 kHz)：融合 NCO（把载波搬到 0）+ 中等低通，
 *           只需挡住 fs_mid 以上的混叠，过渡带很宽，抽头很少；
 *   第二级  fs_mid -> fs_out(48 kHz)：低速率上的尖锐复数滤波。
 *           USB/LSB/CW 用单边带通（复数抽头带频移 f0），选择性在这里；
 *           AM/NFM/WFM 用对称低通（f0 = 0），因此比 SSB 便宜得多。
 *
 * 两级都做了块间连续：输入尾部历史 + phasor 相位推进，避免块边界的相位跳变。
 */

#include "sdr_dsp.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define MAXCH   32
#define MAXTAP  4096
#define MAXBLK  65536

typedef struct {
    int     active, mode;
    double  offset, bw;

    /* 第一级 */
    float  *t1re, *t1im;
    int     dec1, nt1;
    float  *h1re, *h1im;
    int     h1n;
    double  p1re, p1im, p1sre, p1sim;

    /* 第二级 */
    float  *t2re, *t2im;
    int     dec2, nt2;
    float  *h2re, *h2im;
    int     h2n;
    double  p2re, p2im, p2sre, p2sim;

    /* WFM 第三条管线：鉴频后的音频抽取（实数） */
    float  *t3, *h3;
    int     dec3, nt3, h3n;

    float   dc, agc;
    float   prev_i, prev_q;
} chan_t;

struct sdr_dsp {
    double  fs_in, fs_mid, fs_out;
    int     dec1, dec2, max_ch, max_nt1, max_nt2;
    chan_t  ch[MAXCH];

    float  *in_re, *in_im;       /* hist1 + 本块 */
    float  *mid_re, *mid_im;     /* 第一级输出 */
    float  *m2_re, *m2_im;       /* 第二级输入（hist2 + 第一级输出） */
    float  *aud;                 /* WFM 音频暂存 */
    float  *ore, *oim;           /* 第二级输出 */
    int     cap;                 /* in_* 容量（复样本） */
};

static void chan_free(chan_t *c)
{
    free(c->t1re); free(c->t1im); free(c->h1re); free(c->h1im);
    free(c->t2re); free(c->t2im); free(c->h2re); free(c->h2im);
    free(c->t3);   free(c->h3);
    c->t1re = c->t1im = c->h1re = c->h1im = NULL;
    c->t2re = c->t2im = c->h2re = c->h2im = NULL;
    c->t3 = c->h3 = NULL;
    c->h1n = c->h2n = c->h3n = 0;
}

sdr_dsp_t *sdr_dsp_create(double fs_in, double fs_out, int max_ch)
{
    if (!(fs_in > 0.0) || !(fs_out > 0.0)) return NULL;
    if (max_ch < 1) return NULL;
    if (max_ch > MAXCH) max_ch = MAXCH;

    /* 中间速率固定为 2x 输出速率：两级都是整数抽取（2.4M/96k=25，96k/48k=2） */
    double fs_mid = fs_out * 2.0;
    double r1 = fs_in / fs_mid;
    int dec1 = (int)lround(r1);
    if (dec1 < 2 || fabs(r1 - dec1) > 1e-6 || dec1 > 512) return NULL;

    sdr_dsp_t *d = (sdr_dsp_t *)calloc(1, sizeof(*d));
    if (!d) return NULL;
    d->fs_in = fs_in;
    d->fs_mid = fs_mid;
    d->fs_out = fs_out;
    d->dec1 = dec1;
    d->dec2 = 2;
    d->max_ch = max_ch;
    d->cap = MAXBLK;
    d->in_re  = (float *)calloc((size_t)d->cap, sizeof(float));
    d->in_im  = (float *)calloc((size_t)d->cap, sizeof(float));
    d->mid_re = (float *)calloc((size_t)d->cap, sizeof(float));
    d->mid_im = (float *)calloc((size_t)d->cap, sizeof(float));
    d->m2_re  = (float *)calloc((size_t)d->cap, sizeof(float));
    d->m2_im  = (float *)calloc((size_t)d->cap, sizeof(float));
    d->aud    = (float *)calloc((size_t)d->cap, sizeof(float));
    d->ore    = (float *)calloc((size_t)d->cap, sizeof(float));
    d->oim    = (float *)calloc((size_t)d->cap, sizeof(float));
    if (!d->in_re || !d->in_im || !d->mid_re || !d->mid_im || !d->m2_re || !d->m2_im || !d->aud || !d->ore || !d->oim) {
        sdr_dsp_destroy(d);
        return NULL;
    }
    return d;
}

void sdr_dsp_destroy(sdr_dsp_t *d)
{
    if (!d) return;
    for (int i = 0; i < MAXCH; i++) chan_free(&d->ch[i]);
    free(d->in_re);  free(d->in_im);
    free(d->mid_re); free(d->mid_im);
    free(d->m2_re);  free(d->m2_im);
    free(d->aud);
    free(d->ore);    free(d->oim);
    free(d);
}

/* 窗函数 sinc 低通，直流增益 1。fc 为归一化截止（周期/样本）。 */
static void design_lowpass(double *h, int ntap, double fc)
{
    double sum = 0.0;
    for (int i = 0; i < ntap; i++) {
        double x = (double)i - (double)(ntap - 1) / 2.0;
        double s = (fabs(x) < 1e-12) ? (2.0 * fc) : (sin(2.0 * M_PI * fc * x) / (M_PI * x));
        double w = 0.5 - 0.5 * cos(2.0 * M_PI * (double)i / (double)(ntap - 1));
        h[i] = s * w;
        sum += h[i];
    }
    if (fabs(sum) < 1e-12) sum = 1.0;
    for (int i = 0; i < ntap; i++) h[i] /= sum;
}

/* Kaiser 经验式：过渡带宽度 ft(Hz) 下需要的抽头数，向上取整到 dec 的倍数 */
static int taps_for(double fs, double ft_hz, int dec, int min_tp, int max_tp)
{
    if (ft_hz < 1.0) ft_hz = 1.0;
    int tp = (int)ceil(4.5 * fs / (ft_hz * (double)dec));
    if (tp < min_tp) tp = min_tp;
    if (tp > max_tp) tp = max_tp;
    return tp * dec;
}

int sdr_dsp_set_channel(sdr_dsp_t *d, int ch, double offset_hz, int mode, double bw_hz)
{
    if (!d || ch < 0 || ch >= d->max_ch) return -1;
    if (mode < SDR_AM || mode > SDR_WFM) return -1;
    if (!(bw_hz > 0.0)) return -1;
    if (mode != SDR_WFM && bw_hz >= d->fs_out) return -1;
    if (mode == SDR_WFM && bw_hz >= d->fs_in * 0.5) return -1;
    if (fabs(offset_hz) > d->fs_in * 0.5) return -1;

    /* 第二级的滤波器中心：SSB 靠它选边带，CW 靠它给拍频，其余为 0 */
    double f0 = 0.0;
    if (mode == SDR_CW)  f0 = 600.0;
    if (mode == SDR_USB) f0 = bw_hz * 0.5;
    if (mode == SDR_LSB) f0 = -bw_hz * 0.5;

    chan_t *c = &d->ch[ch];
    chan_free(c);

    /*
     * WFM 走第三条管线：200 kHz 的信道装不进 96 kHz 的中间速率，
     * 所以先抽取到 10x 输出速率（480 kHz）就鉴频，音频再抽取到 48 kHz。
     */
    int    dec1, dec2, dec3 = 1, nt3 = 0;
    double fs_mid_ch;
    if (mode == SDR_WFM) {
        double r = d->fs_in / (d->fs_out * 10.0);
        dec1 = (int)lround(r);
        if (dec1 < 1 || fabs(r - (double)dec1) > 1e-6) return -1;
        fs_mid_ch = d->fs_in / (double)dec1;
        dec2 = 1;
        dec3 = (int)lround(fs_mid_ch / d->fs_out);
        if (dec3 < 1 || fabs(fs_mid_ch / (double)dec3 - d->fs_out) > 1e-6) return -1;
    } else {
        dec1 = d->dec1;
        fs_mid_ch = d->fs_mid;
        dec2 = d->dec2;
    }

    /* ---- 第一级：抗混叠到 fs_mid_ch，过渡带宽 = fs_mid_ch - bw ---- */
    double ft1 = fs_mid_ch - bw_hz;
    if (ft1 < 1000.0) ft1 = 1000.0;
    int nt1 = taps_for(d->fs_in, ft1, dec1, 1, 64);
    if (nt1 > MAXTAP) { nt1 = MAXTAP - MAXTAP % dec1; }
    if (nt1 < dec1 * 2) nt1 = dec1 * 2;

    /* ---- 第二级：选择性。SSB/CW 需要单边，过渡带取 bw/2；其余用对称 ---- */
    int nt2 = 0;
    if (mode != SDR_WFM) {
        double ft2 = (mode == SDR_USB || mode == SDR_LSB || mode == SDR_CW)
                         ? bw_hz * 0.5 : (d->fs_out * 0.5 - bw_hz * 0.5);
        if (ft2 <= 0.0) ft2 = d->fs_out * 0.25;
        nt2 = taps_for(fs_mid_ch, ft2, dec2, 2, 1024);
        if (nt2 > MAXTAP) { nt2 = MAXTAP - MAXTAP % dec2; }
    } else {
        /* 音频低通：通带 15 kHz，阻带从 fs_out-15k 起 */
        nt3 = taps_for(fs_mid_ch, d->fs_out - 15000.0 - 15000.0, dec3, 2, 512);
        nt3 = (nt3 / dec3) * dec3;
        if (nt3 < dec3 * 2) nt3 = dec3 * 2;
    }

    c->t1re = (float *)calloc((size_t)nt1, sizeof(float));
    c->t1im = (float *)calloc((size_t)nt1, sizeof(float));
    c->h1re = (float *)calloc((size_t)nt1, sizeof(float));
    c->h1im = (float *)calloc((size_t)nt1, sizeof(float));
    if (nt2 > 0) {
        c->t2re = (float *)calloc((size_t)nt2, sizeof(float));
        c->t2im = (float *)calloc((size_t)nt2, sizeof(float));
        c->h2re = (float *)calloc((size_t)nt2, sizeof(float));
        c->h2im = (float *)calloc((size_t)nt2, sizeof(float));
    }
    if (nt3 > 0) {
        c->t3 = (float *)calloc((size_t)nt3, sizeof(float));
        c->h3 = (float *)calloc((size_t)nt3, sizeof(float));
    }
    double *h1 = (double *)calloc((size_t)nt1, sizeof(double));
    double *h2 = nt2 > 0 ? (double *)calloc((size_t)nt2, sizeof(double)) : NULL;
    double *h3 = nt3 > 0 ? (double *)calloc((size_t)nt3, sizeof(double)) : NULL;
    int ok = c->t1re && c->t1im && c->h1re && c->h1im && h1;
    if (nt2 > 0) ok = ok && c->t2re && c->t2im && c->h2re && c->h2im && h2;
    if (nt3 > 0) ok = ok && c->t3 && c->h3 && h3;
    if (!ok) { free(h1); free(h2); free(h3); chan_free(c); return -1; }

    /* 第一级：低通(bw/2) × 载波混频（exp(-j2π offset p / fs_in)） */
    design_lowpass(h1, nt1, bw_hz * 0.5 / d->fs_in);
    for (int i = 0; i < nt1; i++) {
        double ph =  2.0 * M_PI * offset_hz * (double)i / d->fs_in;
        c->t1re[i] = (float)(h1[i] * cos(ph));
        c->t1im[i] = (float)(h1[i] * sin(ph));
    }
    if (nt2 > 0) {
        /* 第二级：低通(bw/2) × 频移（exp(-j2π f0 p / fs_mid)）—— 单边带就是靠它 */
        design_lowpass(h2, nt2, bw_hz * 0.5 / fs_mid_ch);
        for (int i = 0; i < nt2; i++) {
            double ph = -2.0 * M_PI * f0 * (double)i / fs_mid_ch;
            c->t2re[i] = (float)(h2[i] * cos(ph));
            c->t2im[i] = (float)(h2[i] * sin(ph));
        }
    }
    if (nt3 > 0) {
        design_lowpass(h3, nt3, 15000.0 / fs_mid_ch);
        for (int i = 0; i < nt3; i++) c->t3[i] = (float)h3[i];
    }
    free(h1); free(h2); free(h3);

    c->dec1 = dec1; c->nt1 = nt1;
    c->dec2 = dec2; c->nt2 = nt2;
    c->dec3 = dec3; c->nt3 = nt3; c->h3n = 0;
    c->mode = mode; c->offset = offset_hz; c->bw = bw_hz;
    c->h1n = c->h2n = 0;
    c->p1re = 1.0; c->p1im = 0.0;
    c->p1sre = cos( 2.0 * M_PI * offset_hz * (double)d->dec1 / d->fs_in);
    c->p1sim = sin( 2.0 * M_PI * offset_hz * (double)d->dec1 / d->fs_in);
    c->p2re = 1.0; c->p2im = 0.0;
    c->p2sre = cos(-2.0 * M_PI * f0 * (double)dec2 / fs_mid_ch);
    c->p2sim = sin(-2.0 * M_PI * f0 * (double)dec2 / fs_mid_ch);
    c->dc = 0.0f; c->agc = 1.0f; c->prev_i = c->prev_q = 0.0f;
    c->active = 1;

    if (nt1 > d->max_nt1) d->max_nt1 = nt1;
    if (nt2 > d->max_nt2) d->max_nt2 = nt2;
    return 0;
}

int sdr_dsp_clear_channel(sdr_dsp_t *d, int ch)
{
    if (!d || ch < 0 || ch >= d->max_ch) return -1;
    d->ch[ch].active = 0;
    return 0;
}

/*
 * 通用多相抽取：y[k] = Σ_{t,p} x[s+t*D+p] * h[t*D+p]，窗口起点相位由 phasor 补上。
 * phasor 每输出样本推进一档，保证块间连续。
 */
static int polyphase(const float *restrict tre, const float *restrict tim,
                     int ntap, int D,
                     const float *restrict re, const float *restrict im, int n,
                     double *phre, double *phim, double sre, double sim,
                     float *restrict ore, float *restrict oim)
{
    const int tp = ntap / D;
    const int d4 = D & ~3;
    int k = 0;
    for (int s = 0; s + ntap <= n; s += D) {
        float ar = 0.0f, ai = 0.0f;
        for (int t = 0; t < tp; t++) {
            const float *restrict xr4 = re + s + t * D;
            const float *restrict xi4 = im + s + t * D;
            const float *restrict hr4 = tre + t * D;
            const float *restrict hi4 = tim + t * D;
            int p = 0;
            for (; p < d4; p += 4) {          /* 4 路展开，帮 GCC 走向量化 */
                for (int q = 0; q < 4; q++) {
                    float xr = xr4[p + q], xi = xi4[p + q];
                    float hr = hr4[p + q], hi = hi4[p + q];
                    ar += xr * hr - xi * hi;
                    ai += xr * hi + xi * hr;
                }
            }
            for (; p < D; p++) {
                float xr = xr4[p], xi = xi4[p];
                float hr = hr4[p], hi = hi4[p];
                ar += xr * hr - xi * hi;
                ai += xr * hi + xi * hr;
            }
        }
        float pr = (float)(*phre), pi = (float)(*phim);
        ore[k] = ar * pr - ai * pi;
        oim[k] = ar * pi + ai * pr;
        k++;
        double nr = (*phre) * sre - (*phim) * sim;
        double ni = (*phre) * sim + (*phim) * sre;
        *phre = nr; *phim = ni;
    }
    return k;
}

/* 实数多相抽取：音频用 */
static int real_polyphase(const float *restrict t, int ntap, int D,
                          const float *restrict x, int n, float *restrict out)
{
    const int tp = ntap / D;
    int k = 0;
    for (int s = 0; s + ntap <= n; s += D) {
        float a = 0.0f;
        for (int tt = 0; tt < tp; tt++) {
            const float *restrict xp = x + s + tt * D;
            const float *restrict hp = t + tt * D;
            for (int p = 0; p < D; p++) a += xp[p] * hp[p];
        }
        out[k++] = a;
    }
    return k;
}

int sdr_dsp_process(sdr_dsp_t *d, const short *iq, int n, int nch,
                    short *out, int stride, int *out_n, float *rms)
{
    if (!d || !iq || n <= 0 || !out || !out_n) return -1;
    if (nch > d->max_ch) nch = d->max_ch;
    if (n > d->cap - d->max_nt1) n = d->cap - d->max_nt1;
    if (n <= 0) return -1;

    int nmin = -1;
    for (int ch = 0; ch < nch; ch++) {
        chan_t *c = &d->ch[ch];
        if (!c->active) continue;

        /* 第一级：hist1 + 本块 */
        int hn = c->h1n;
        int total = hn + n;
        if (total > d->cap) total = d->cap;
        memcpy(d->in_re, c->h1re, (size_t)hn * sizeof(float));
        memcpy(d->in_im, c->h1im, (size_t)hn * sizeof(float));
        for (int i = 0; i < n; i++) {
            d->in_re[hn + i] = (float)iq[2 * i]     / 32768.0f;
            d->in_im[hn + i] = (float)iq[2 * i + 1] / 32768.0f;
        }
        int nmid = polyphase(c->t1re, c->t1im, c->nt1, c->dec1,
                             d->in_re, d->in_im, total,
                             &c->p1re, &c->p1im, c->p1sre, c->p1sim,
                             d->mid_re, d->mid_im);
        int keep1 = c->nt1 - 1;
        if (keep1 > total) keep1 = total;
        memmove(c->h1re, d->in_re + (total - keep1), (size_t)keep1 * sizeof(float));
        memmove(c->h1im, d->in_im + (total - keep1), (size_t)keep1 * sizeof(float));
        c->h1n = keep1;

        int nout = 0;
        if (c->mode == SDR_WFM) {
            /* 第三条管线：先在 fs_mid 上鉴频，再抽取音频 */
            for (int i2 = 0; i2 < nmid; i2++) {
                float zr = d->mid_re[i2], zi = d->mid_im[i2];
                float cr = zr * c->prev_i + zi * c->prev_q;   /* dot   */
                float ci = zi * c->prev_i - zr * c->prev_q;   /* cross */
                d->aud[i2] = atan2f(ci, cr);
                c->prev_i = zr; c->prev_q = zi;
            }
            int hn3 = c->h3n;
            int total3 = hn3 + nmid;
            if (total3 > d->cap) total3 = d->cap;
            memcpy(d->m2_re, c->h3, (size_t)hn3 * sizeof(float));
            memcpy(d->m2_re + hn3, d->aud, (size_t)nmid * sizeof(float));
            nout = real_polyphase(c->t3, c->nt3, c->dec3, d->m2_re, total3, d->ore);
            int keep3 = c->nt3 - 1;
            if (keep3 > total3) keep3 = total3;
            memmove(c->h3, d->m2_re + (total3 - keep3), (size_t)keep3 * sizeof(float));
            c->h3n = keep3;
        } else {
            /* 第二级：hist2 + 第一级输出 */
            int hn2 = c->h2n;
            int total2 = hn2 + nmid;
            if (total2 > d->cap) total2 = d->cap;
            memcpy(d->m2_re, c->h2re, (size_t)hn2 * sizeof(float));
            memcpy(d->m2_im, c->h2im, (size_t)hn2 * sizeof(float));
            memcpy(d->m2_re + hn2, d->mid_re, (size_t)nmid * sizeof(float));
            memcpy(d->m2_im + hn2, d->mid_im, (size_t)nmid * sizeof(float));

            nout = polyphase(c->t2re, c->t2im, c->nt2, c->dec2,
                             d->m2_re, d->m2_im, total2,
                             &c->p2re, &c->p2im, c->p2sre, c->p2sim,
                             d->ore, d->oim);
            int keep2 = c->nt2 - 1;
            if (keep2 > total2) keep2 = total2;
            memmove(c->h2re, d->m2_re + (total2 - keep2), (size_t)keep2 * sizeof(float));
            memmove(c->h2im, d->m2_im + (total2 - keep2), (size_t)keep2 * sizeof(float));
            c->h2n = keep2;
        }

        short *o = out + (size_t)ch * (size_t)stride;
        float sum = 0.0f;
        for (int i = 0; i < nout; i++) {
            float v;
            switch (c->mode) {
            case SDR_AM:
                v = sqrtf(d->ore[i] * d->ore[i] + d->oim[i] * d->oim[i]);
                break;
            case SDR_USB:
            case SDR_LSB:
            case SDR_CW:
                v = d->ore[i];
                break;
            case SDR_WFM:
                v = d->ore[i];          /* 已经过了鉴频与音频抽取 */
                break;
            case SDR_NFM: {
                float ph = atan2f(d->oim[i], d->ore[i]);
                float dph = ph - atan2f(c->prev_q, c->prev_i);
                while (dph >  (float)M_PI) dph -= 2.0f * (float)M_PI;
                while (dph < -(float)M_PI) dph += 2.0f * (float)M_PI;
                v = dph * 0.7f;
                break;
            }
            default:
                v = d->ore[i];
                break;
            }
            c->prev_i = d->ore[i];
            c->prev_q = d->oim[i];

            c->dc += 0.0005f * (v - c->dc);
            v -= c->dc;

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
        if (rms) rms[ch] = nout ? sqrtf(sum / (float)nout) : 0.0f;
        if (nmin < 0 || nout < nmin) nmin = nout;
    }
    *out_n = (nmin > 0) ? nmin : 0;
    return 0;
}
