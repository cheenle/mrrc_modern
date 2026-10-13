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
#include <ft8/encode.h>
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

/* 本估计与 WSJT-X 的 SNR 定义（2500 Hz 参考带宽）之间的经验残差。
   由 tools/ft8/calibrate_fast_snr.py 在合成的 +24…−18 dB 扫频上把本函数的原始
   输出与 standard 路径逐点相减定出来：残差中位数 0、最大 +1，输出电平先归一化到
   0.9 满量程（不这样做，弱信号档的噪声会把时隙削成方波，量到的是限幅器 —— 见该
   脚本的模块注释）。−18 dB 及以下两条路径会**一起**偏乐观（standard 自己也 +1），
   那是检测门限附近的性质，不是这个常数的偏差，所以别按最弱档去调它。 */
#define FAST_SNR_CALIBRATION_DB 1.0

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

/* 快路径的 SNR。
 *
 * 上游 ft8_lib 只在 candidate.score 里给了一个**归一化**的 Costas 同步分，
 * 本文件原先照抄的 `score/2` 就是它。它不是"差一点的 dB"，是**饱和**：
 * 实测（合成扫频，真实 SNR 由 standard/improved 给出）在 ≥ −6 dB 的整个
 * 区间里它压在 17 不动，−6 dB 才 16、−12 dB 才 14。任何偏移/查表都救不
 * 回来，因为信息在归一化时已经被丢掉了。
 *
 * 而这不是显示问题：orchestrator 把 snr 喂给 sequencer，sequencer 把它编进
 * **实际发射的报文**（sequencer.py:180/234/287，REPORT / ROGER_REPORT 状态），
 * 也就是说发到空中、写进 QSO 日志的"收你多少 dB"会是一个饱和的同步分。
 *
 * 信息其实一直都在：ft8_lib 存的水声图幅度**本身就是功率 dB**
 * （WF_ELEM_MAG(x) = x*0.5 − 120，见 ft8/decode.h；写入见 monitor.c:170
 * 的 `db = 10*log10f(1E-12f + mag2)`），而 mon.wf 就在这里 —— 不需要再加
 * 一次 FFT。于是按 WSJT-X `ft8_snr` 的思路，在解出的 79 个音位置上量
 * 信/噪即可：每个符号有一个信号音、七个噪声音，两者都是单音带宽 6.25 Hz，
 * 比值再换算到 2500 Hz 参考带宽（+10*log10(2500/6.25) = +26.02 dB）。
 *
 * 音序由报文反推（ft8_encode），索引与 ft8_extract_symbol 完全一致：
 * 都用 kFT8_Gray_map[tone] 取 bin，符号 s 落在 base + s*block_stride。
 * 标定常数由 tools 下的扫频脚本对齐 standard 得出，见 FAST_SNR_CALIBRATION_DB。 */
/* uint8 水声图幅度 → 线性功率。整幅图只算一次，之后全是查表加法；
   直接对每个元素调 pow() 是 28 万次超越函数调用，那会把快路径变慢。 */
static void fast_power_lut(double lut[256])
{
    for (int i = 0; i < 256; ++i)
        lut[i] = pow(10.0, (double)WF_ELEM_MAG((WF_ELEM_T)i) * 0.1);
}

/* 每 6.25 Hz bin 的噪声功率 —— 整幅水声图的**中位数**，不是均值。
   均值会被信号自己毁掉：一个符号只有一个音在响，但它是高出噪底几十 dB 的
   强 bin；整幅图里 ~1.8% 的 bin 携带信号，就足以把均值抬高 30 dB，于是
   ratio 被压平（实测：真实 24 dB 只读到 24 dB 的比值，而物理上应是 50）。
   中位数对这些强 bin 免疫。uint8 值域只有 256，所以直方图一趟 O(n) 求中位数，
   不用排序 —— 每槽只算一次，不在候选循环里。 */
static double fast_noise_floor(const ftx_waterfall_t *wf, const double lut[256])
{
    const size_t total = (size_t)wf->num_blocks * (size_t)wf->block_stride;
    if (total == 0)
        return 0.0;

    uint32_t hist[256];
    memset(hist, 0, sizeof(hist));
    for (size_t i = 0; i < total; ++i)
        ++hist[wf->mag[i]];

    const size_t half = total / 2;
    size_t cumulative = 0;
    for (int i = 0; i < 256; ++i) {
        cumulative += hist[i];
        if (cumulative > half)
            return lut[i];
    }
    return lut[255];
}

static int32_t fast_snr_db(const ftx_waterfall_t *wf,
                           const ftx_candidate_t *cand,
                           const uint8_t *payload,
                           const double lut[256],
                           double noise_floor)
{
    if (noise_floor <= 0.0)
        return 0;

    uint8_t tones[FT8_NN];
    ft8_encode(payload, tones);

    /* 与 get_cand_mag()（ft8/decode.c:52）逐字相同 —— 这是水声图的索引
       约定，写错一位就是拿隔壁频点的功率当信号。 */
    int offset = cand->time_offset;
    offset = (offset * wf->time_osr) + cand->time_sub;
    offset = (offset * wf->freq_osr) + cand->freq_sub;
    offset = (offset * wf->num_bins) + cand->freq_offset;
    const WF_ELEM_T *base = wf->mag + offset;

    double sig = 0.0;
    int used = 0;

    for (int s = 0; s < FT8_NN; ++s) {
        const int block = cand->time_offset + s;
        if (block < 0 || block >= wf->num_blocks)
            continue;
        const WF_ELEM_T *sym = base + (size_t)s * (size_t)wf->block_stride;
        sig += lut[sym[kFT8_Gray_map[tones[s] & 0x07]]];
        ++used;
    }

    if (used <= 0)
        return 0;

    /* 信号音功率 / 每 bin 噪声功率，再换算到 2500 Hz 参考带宽：
       10*log10(2500/6.25) = 26.02 dB。这一步是**物理**的，不是拟合 ——
       SNR 定义本身就是 0 dB 时该比值等于 400。 */
    const double snr_db = 10.0 * log10((sig / (double)used) / noise_floor) -
                          10.0 * log10(2500.0 / 6.25);
    return (int32_t)lround(snr_db + FAST_SNR_CALIBRATION_DB);
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

/* `config->flags` 在本入口里**从不读取**：AP（WSJT_FLAG_AP）、
 * LOW_THRESHOLD、WIDE_DX、HIDE_DUPES 在这里全是空操作，因为 ft8_lib 既没有
 * AP 上下文也没有重复抑制 —— 那些正是快路径拿掉换时间的东西。调用方选择
 * 走哪条路径，靠的是 `decode_paths` 位图（`wsjt_abi_info` 报出来的那个），
 * 不是 flags。别照抄 upstream 的 `if (flags & ...)`：这个入口里没有对应实现。
 */
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

    double snr_lut[256];
    fast_power_lut(snr_lut);
    const double snr_noise = fast_noise_floor(&mon.wf, snr_lut);

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
        /* 由水声图量出的真 dB（见 fast_snr_db）。上一位作者照抄的
           `score/2` 是同步分、会饱和，已废弃 —— 那个值会被当作"收你多少
           dB"发到空中，所以它必须是真的。 */
        out->snr = fast_snr_db(&mon.wf, &cand[i], message.payload, snr_lut,
                               snr_noise);
        out->flags = 0;
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
