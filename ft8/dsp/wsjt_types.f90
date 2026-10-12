module wsjt_types
  use iso_c_binding, only: c_char, c_float, c_int32_t, c_int64_t
  implicit none
  private

  integer(c_int32_t), parameter, public :: WSJT_ABI_VERSION = 1_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_FT8_RX_RATE = 12000_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_FT8_RX_SAMPLES = 180000_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_FT8_TX_RATE = 48000_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_FT8_TX_SAMPLES = 606720_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_RESULT_CAPACITY = 256_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_TEXT_BYTES = 38_c_int32_t

  integer(c_int32_t), parameter, public :: WSJT_OK = 0_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_E_NULL = 1_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_E_ABI = 2_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_E_RATE = 3_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_E_SHAPE = 4_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_E_CONFIG = 5_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_E_CAPACITY = 6_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_E_ENCODE = 7_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_E_INTERNAL = 8_c_int32_t

  integer(c_int32_t), parameter, public :: WSJT_FLAG_AP = 1_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_FLAG_LOW_THRESHOLD = 2_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_FLAG_WIDE_DX = 4_c_int32_t
  integer(c_int32_t), parameter, public :: WSJT_FLAG_HIDE_DUPES = 8_c_int32_t

  type, bind(C), public :: wsjt_abi_info
    integer(c_int32_t) :: abi_version
    integer(c_int32_t) :: struct_size
    integer(c_int32_t) :: result_size
    integer(c_int32_t) :: result_capacity
    integer(c_int32_t) :: ft8_rx_rate
    integer(c_int32_t) :: ft8_rx_samples
    integer(c_int32_t) :: ft8_tx_rate
    integer(c_int32_t) :: ft8_tx_samples
    integer(c_int32_t) :: improved_profiles
    integer(c_int32_t) :: max_threads
    integer(c_int32_t) :: max_cycles
    integer(c_int32_t) :: reserved
  end type wsjt_abi_info

  type, bind(C), public :: wsjt_decode_config
    integer(c_int32_t) :: struct_size
    integer(c_int32_t) :: sample_rate
    integer(c_int32_t) :: sample_count
    integer(c_int32_t) :: profile
    integer(c_int32_t) :: threads
    integer(c_int32_t) :: cycles
    integer(c_int32_t) :: sensitivity
    integer(c_int32_t) :: flags
    integer(c_int32_t) :: qso_progress
    integer(c_int32_t) :: rx_frequency
    integer(c_int32_t) :: tx_frequency
    integer(c_int32_t) :: low_frequency
    integer(c_int32_t) :: high_frequency
    integer(c_int32_t) :: ap_width
    integer(c_int32_t) :: utc_hhmmss
    integer(c_int32_t) :: reserved
    character(kind=c_char) :: my_call(13)
    character(kind=c_char) :: dx_call(13)
    character(kind=c_char) :: dx_grid(7)
    character(kind=c_char) :: padding(3)
  end type wsjt_decode_config

  type, bind(C), public :: wsjt_decode_result
    integer(c_int64_t) :: slot_id
    real(c_float) :: sync
    real(c_float) :: dt
    real(c_float) :: frequency
    real(c_float) :: quality
    integer(c_int32_t) :: snr
    integer(c_int32_t) :: ap_type
    integer(c_int32_t) :: flags
    integer(c_int32_t) :: reserved
    character(kind=c_char) :: text(38)
    character(kind=c_char) :: padding(2)
  end type wsjt_decode_result
end module wsjt_types
