/*
 * sdr_dsp CPU 基准：按实时速率喂 2.4 MSPS 的合成 IQ，量每通道的核占比。
 *
 * 构建（盒子/Linux）: gcc -O3 -ffast-math -o dsp_bench tools/sdr_dsp_bench.c sdr_dsp.c -lm
 * 用法: ./dsp_bench [通道数] [模式] [带宽Hz] [秒]
 *   模式: 0=AM 1=USB 2=LSB 3=CW 4=NFM 5=WFM
 */
#include "sdr_dsp.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double proc_time(void)
{
    struct timespec t;
    clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t);
    return (double)t.tv_sec + (double)t.tv_nsec * 1e-9;
}

#define FS      2400000.0
#define OUT     48000.0
#define BLK     4800          /* 2 ms */
#define MAXCH   32
#define STRIDE  8192

int main(int argc, char **argv)
{
    int    nch  = argc > 1 ? atoi(argv[1]) : 4;
    int    mode = argc > 2 ? atoi(argv[2]) : 0;
    double bw   = argc > 3 ? atof(argv[3]) : 6000.0;
    double secs = argc > 4 ? atof(argv[4]) : 10.0;
    if (nch < 1) nch = 1;
    if (nch > MAXCH) nch = MAXCH;

    sdr_dsp_t *d = sdr_dsp_create(FS, OUT, MAXCH);
    if (!d) { fprintf(stderr, "create failed\n"); return 1; }
    for (int i = 0; i < nch; i++) {
        double off = (nch > 1) ? (-800e3 + 1600e3 * i / (nch - 1)) : 0.0;
        if (sdr_dsp_set_channel(d, i, off, mode, bw) != 0) {
            fprintf(stderr, "set_channel %d failed\n", i);
            return 1;
        }
    }

    /* 合成输入：噪声 + 每通道位置附近一个音 */
    short *iq = (short *)malloc(sizeof(short) * BLK * 2);
    short *out = (short *)malloc(sizeof(short) * MAXCH * STRIDE);
    if (!iq || !out) return 1;
    double t = 0.0;
    for (int i = 0; i < BLK; i++) {
        double v = 0.0;
        for (int c = 0; c < nch; c++) {
            double f = (nch > 1) ? (-800e3 + 1600e3 * c / (nch - 1)) : 0.0;
            v += 3000.0 * sin(2.0 * M_PI * (f + 700.0) * t);
        }
        v += 800.0 * sin(2.0 * M_PI * 12345.0 * t);
        if (v >  30000.0) v =  30000.0;
        if (v < -30000.0) v = -30000.0;
        iq[2 * i]     = (short)v;
        iq[2 * i + 1] = (short)(v * 0.5);
        t += 1.0 / FS;
    }

    long blocks = (long)(secs * FS / BLK);
    int out_n = 0;
    float rms[MAXCH];
    double t0 = proc_time();
    for (long b = 0; b < blocks; b++) {
        sdr_dsp_process(d, iq, BLK, nch, out, STRIDE, &out_n, rms);
    }
    double dt = proc_time() - t0;
    double core = dt / secs;

    printf("mode=%-2d bw=%7.0f Hz  channels=%2d  %5.1f s of 2.4 MSPS IQ\n",
           mode, bw, nch, secs);
    printf("  cpu = %7.1f ms  -> %.3f core total, %.4f core/channel  (out_n=%d rms0=%.3f)\n",
           dt * 1000.0, core, core / nch, out_n, rms[0]);
    sdr_dsp_destroy(d);
    free(iq); free(out);
    return 0;
}
