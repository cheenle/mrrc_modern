#ifndef MRRC_WSJT_CORE_H
#define MRRC_WSJT_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum {
    WSJT_ABI_VERSION = 1,
    WSJT_FT8_RX_RATE = 12000,
    WSJT_FT8_RX_SAMPLES = 180000,
    WSJT_FT8_TX_RATE = 48000,
    WSJT_FT8_TX_SAMPLES = 606720,
    WSJT_RESULT_CAPACITY = 256,
    WSJT_TEXT_BYTES = 38
};

enum wsjt_status {
    WSJT_OK = 0,
    WSJT_E_NULL = 1,
    WSJT_E_ABI = 2,
    WSJT_E_RATE = 3,
    WSJT_E_SHAPE = 4,
    WSJT_E_CONFIG = 5,
    WSJT_E_CAPACITY = 6,
    WSJT_E_ENCODE = 7,
    WSJT_E_INTERNAL = 8
};

enum wsjt_flags {
    WSJT_FLAG_AP = 1,
    WSJT_FLAG_LOW_THRESHOLD = 2,
    WSJT_FLAG_WIDE_DX = 4,
    WSJT_FLAG_HIDE_DUPES = 8
};

struct wsjt_abi_info {
    int32_t abi_version;
    int32_t struct_size;
    int32_t result_size;
    int32_t result_capacity;
    int32_t ft8_rx_rate;
    int32_t ft8_rx_samples;
    int32_t ft8_tx_rate;
    int32_t ft8_tx_samples;
    int32_t improved_profiles;
    int32_t max_threads;
    int32_t max_cycles;
    int32_t reserved;
};

struct wsjt_decode_config {
    int32_t struct_size;
    int32_t sample_rate;
    int32_t sample_count;
    int32_t profile;
    int32_t threads;
    int32_t cycles;
    int32_t sensitivity;
    int32_t flags;
    int32_t qso_progress;
    int32_t rx_frequency;
    int32_t tx_frequency;
    int32_t low_frequency;
    int32_t high_frequency;
    int32_t ap_width;
    int32_t utc_hhmmss;
    int32_t reserved;
    char my_call[13];
    char dx_call[13];
    char dx_grid[7];
    char padding[3];
};

struct wsjt_decode_result {
    int64_t slot_id;
    float sync;
    float dt;
    float frequency;
    float quality;
    int32_t snr;
    int32_t ap_type;
    int32_t flags;
    int32_t reserved;
    char text[WSJT_TEXT_BYTES];
    char padding[2];
};

int32_t wsjt_get_abi_info(struct wsjt_abi_info *out);
int32_t wsjt_ft8_encode(const char message[WSJT_TEXT_BYTES],
                        float frequency,
                        int32_t sample_rate,
                        float *wave,
                        int32_t capacity,
                        int32_t *written,
                        char sent[WSJT_TEXT_BYTES]);
int32_t wsjt_ft8_decode_standard(
    const int16_t *samples,
    const struct wsjt_decode_config *config,
    int64_t slot_id,
    struct wsjt_decode_result *results,
    int32_t capacity,
    int32_t *count,
    int32_t *overflow);
int32_t wsjt_ft8_decode_improved(
    const int16_t *samples,
    const struct wsjt_decode_config *config,
    int64_t slot_id,
    struct wsjt_decode_result *results,
    int32_t capacity,
    int32_t *count,
    int32_t *overflow);

#ifdef __cplusplus
}
#endif

#endif
