#include <stdint.h>
#include <stdio.h>

#include "wsjt_core.h"

_Static_assert(sizeof(struct wsjt_abi_info) == 48, "wsjt_abi_info ABI size");
_Static_assert(sizeof(struct wsjt_decode_config) == 100,
               "wsjt_decode_config ABI size");
_Static_assert(sizeof(struct wsjt_decode_result) == 80,
               "wsjt_decode_result ABI size");

int main(void) {
    struct wsjt_abi_info info = {0};
    int32_t status;

    status = wsjt_get_abi_info(NULL);
    if (status != WSJT_E_NULL) {
        fprintf(stderr,
                "NULL query status mismatch: expected=%d actual=%d\n",
                WSJT_E_NULL,
                status);
        return 1;
    }

    status = wsjt_get_abi_info(&info);
    if (status != WSJT_OK) {
        fprintf(stderr,
                "capability query status mismatch: expected=%d actual=%d\n",
                WSJT_OK,
                status);
        return 2;
    }
    if (info.struct_size != (int32_t)sizeof(struct wsjt_abi_info)) {
        fprintf(stderr,
                "abi_info size mismatch: expected=%zu actual=%d\n",
                sizeof(struct wsjt_abi_info),
                info.struct_size);
        return 3;
    }
    if (info.result_size != (int32_t)sizeof(struct wsjt_decode_result)) {
        fprintf(stderr,
                "decode_result size mismatch: expected=%zu actual=%d\n",
                sizeof(struct wsjt_decode_result),
                info.result_size);
        return 4;
    }

    printf("abi=%d results=%d rx=%d/%d tx=%d/%d profiles=0x%x\n",
           info.abi_version,
           info.result_capacity,
           info.ft8_rx_rate,
           info.ft8_rx_samples,
           info.ft8_tx_rate,
           info.ft8_tx_samples,
           info.improved_profiles);
    return 0;
}
