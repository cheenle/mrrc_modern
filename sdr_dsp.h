#ifndef SDR_DSP_H
#define SDR_DSP_H

/*
 * sdr_dsp -- 纯计算的 SDR 信道化 + 解调库。
 *
 * 输入 2.4/4.8 MSPS 的交错 int16 I/Q，输出每通道 48 kHz int16 PCM。
 * 没有任何 I/O、没有全局可变状态、不依赖 libsdrplay —— 单测直接用合成信号。
 *
 * 信道化 = 融合 NCO + 多相抽取：抽头里已经包含 exp(-j2πf p/fs)，
 * 所以每个输入样本只做一次复数 MAC（而不是"先混频再滤波"两次）。
 * 块间相位由 set_channel 建立的 phasor 连续推进，避免每块边界的相位跳变。
 */

typedef enum {
    SDR_AM  = 0,
    SDR_USB = 1,
    SDR_LSB = 2,
    SDR_CW  = 3,
    SDR_NFM = 4,
    SDR_WFM = 5
} sdr_mode_t;

typedef struct sdr_dsp sdr_dsp_t;

sdr_dsp_t *sdr_dsp_create(double fs_in, double fs_out, int max_ch);
void       sdr_dsp_destroy(sdr_dsp_t *d);

/* offset_hz 相对中心频率；bw_hz 是通道带宽（决定抗混叠滤波器的过渡带）。 */
int sdr_dsp_set_channel(sdr_dsp_t *d, int ch, double offset_hz, int mode, double bw_hz);
int sdr_dsp_clear_channel(sdr_dsp_t *d, int ch);

/*
 * iq: 交错 int16 (I,Q,I,Q,...)，n 个复样本。
 * out: 每通道 int16 PCM，通道 c 写在 out + c*stride。
 * out_n: 本次每通道产生的样本数（所有活跃通道相同）。
 * rms: 每通道解调后 RMS（0..1），供静噪与电平显示。
 */
int sdr_dsp_process(sdr_dsp_t *d, const short *iq, int n, int nch,
                    short *out, int stride, int *out_n, float *rms);

#endif /* SDR_DSP_H */
