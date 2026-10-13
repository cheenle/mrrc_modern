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
