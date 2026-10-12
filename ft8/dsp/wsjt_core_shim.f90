module wsjt_core_shim
  use iso_c_binding, only: c_associated, c_char, c_f_pointer, c_float, &
                           c_float_complex, c_int, c_int8_t, c_int16_t, &
                           c_int32_t, c_int64_t, c_null_char, c_ptr, &
                           c_size_t, c_sizeof
  use ft8_decode, only: ft8_decoder
  use wsjt_batch, only: append_standard, batch_copy, batch_deduplicate, &
                        batch_reset
  use wsjt_improved, only: run_improved_profile
  use wsjt_types, only: WSJT_ABI_VERSION, WSJT_E_ABI, WSJT_E_NULL, &
                        WSJT_E_CAPACITY, WSJT_E_CONFIG, WSJT_E_ENCODE, &
                        WSJT_E_RATE, WSJT_E_SHAPE, WSJT_FLAG_AP, &
                        WSJT_FT8_RX_RATE, WSJT_FT8_RX_SAMPLES, &
                        WSJT_FT8_TX_RATE, WSJT_FT8_TX_SAMPLES, WSJT_OK, &
                        WSJT_RESULT_CAPACITY, WSJT_TEXT_BYTES, wsjt_abi_info, &
                        wsjt_decode_config, wsjt_decode_result
  implicit none
  private

  public :: wsjt_ft8_decode_improved, wsjt_ft8_decode_standard, &
            wsjt_ft8_encode, wsjt_get_abi_info

  interface
    subroutine genft8(msg, i3, n3, msgsent, msgbits, itone)
      import :: c_int, c_int8_t
      character(len=37) :: msg, msgsent
      integer(c_int) :: i3, n3
      integer(c_int8_t) :: msgbits(77)
      integer(c_int) :: itone(79)
    end subroutine genft8

    subroutine gen_ft8wave(itone, nsym, nsps, bt, fsample, f0, cwave, &
                           wave, icmplx, nwave)
      import :: c_float, c_float_complex, c_int
      integer(c_int) :: nsym, nsps, icmplx, nwave
      integer(c_int) :: itone(nsym)
      real(c_float) :: bt, fsample, f0, wave(nwave)
      complex(c_float_complex) :: cwave(nwave)
    end subroutine gen_ft8wave
  end interface

contains

  function wsjt_get_abi_info(out) result(status) bind(C, name='wsjt_get_abi_info')
    type(c_ptr), value :: out
    integer(c_int32_t) :: status
    type(wsjt_abi_info), pointer :: info
    type(wsjt_abi_info) :: info_probe
    type(wsjt_decode_config) :: config_probe
    type(wsjt_decode_result) :: result_probe

    if (.not. c_associated(out)) then
      status = WSJT_E_NULL
      return
    end if

    if (c_sizeof(config_probe) /= int(100, c_size_t)) then
      status = WSJT_E_ABI
      return
    end if

    call c_f_pointer(out, info)
    info%abi_version = WSJT_ABI_VERSION
    info%struct_size = int(c_sizeof(info_probe), c_int32_t)
    info%result_size = int(c_sizeof(result_probe), c_int32_t)
    info%result_capacity = WSJT_RESULT_CAPACITY
    info%ft8_rx_rate = WSJT_FT8_RX_RATE
    info%ft8_rx_samples = WSJT_FT8_RX_SAMPLES
    info%ft8_tx_rate = WSJT_FT8_TX_RATE
    info%ft8_tx_samples = WSJT_FT8_TX_SAMPLES
    info%improved_profiles = int(z'1f', c_int32_t)
    info%max_threads = 12_c_int32_t
    info%max_cycles = 3_c_int32_t
    info%reserved = 0_c_int32_t
    status = WSJT_OK
  end function wsjt_get_abi_info

  function wsjt_ft8_encode(message, frequency, sample_rate, wave_out, &
                           capacity, written_out, sent_out) result(status) &
      bind(C, name='wsjt_ft8_encode')
    type(c_ptr), value :: message, wave_out, written_out, sent_out
    real(c_float), value :: frequency
    integer(c_int32_t), value :: sample_rate, capacity
    integer(c_int32_t) :: status
    character(kind=c_char), pointer :: message_chars(:), sent_chars(:)
    real(c_float), pointer :: output_wave(:)
    integer(c_int32_t), pointer :: written
    character(len=37) :: message_text, sent_text
    integer(c_int) :: i3, n3, itone(79)
    integer(c_int8_t) :: message_bits(77)
    real(c_float), allocatable :: generated_wave(:)
    complex(c_float_complex), allocatable :: complex_scratch(:)
    logical :: message_valid, wave_valid, written_valid, sent_valid
    logical :: rate_valid, capacity_valid
    integer :: i, sent_length

    message_valid = c_associated(message)
    wave_valid = c_associated(wave_out)
    written_valid = c_associated(written_out)
    sent_valid = c_associated(sent_out)
    rate_valid = sample_rate == WSJT_FT8_TX_RATE
    capacity_valid = capacity >= WSJT_FT8_TX_SAMPLES

    status = WSJT_OK
    if (.not. message_valid .or. .not. wave_valid .or. &
        .not. written_valid .or. .not. sent_valid) then
      status = WSJT_E_NULL
    else if (.not. rate_valid) then
      status = WSJT_E_RATE
    else if (.not. capacity_valid) then
      status = WSJT_E_CAPACITY
    end if

    if (written_valid) then
      call c_f_pointer(written_out, written)
      written = 0_c_int32_t
    end if
    if (sent_valid) then
      call c_f_pointer(sent_out, sent_chars, [WSJT_TEXT_BYTES])
      sent_chars = c_null_char
    end if
    if (status /= WSJT_OK) return

    call c_f_pointer(message, message_chars, [WSJT_TEXT_BYTES])
    call c_f_pointer(wave_out, output_wave, [capacity])

    message_text = ' '
    do i = 1, 37
      if (message_chars(i) == c_null_char) exit
      message_text(i:i) = message_chars(i)
    end do

    call genft8(message_text, i3, n3, sent_text, message_bits, itone)
    if (sent_text(1:19) == '*** bad message ***') then
      status = WSJT_E_ENCODE
      return
    end if

    allocate(generated_wave(WSJT_FT8_TX_SAMPLES))
    allocate(complex_scratch(WSJT_FT8_TX_SAMPLES))
    call gen_ft8wave(itone, 79_c_int, 7680_c_int, 2.0_c_float, &
                     48000.0_c_float, frequency, complex_scratch, &
                     generated_wave, 0_c_int, &
                     int(WSJT_FT8_TX_SAMPLES, c_int))

    output_wave(1:WSJT_FT8_TX_SAMPLES) = generated_wave
    sent_length = min(len_trim(sent_text), 37)
    do i = 1, sent_length
      sent_chars(i) = sent_text(i:i)
    end do
    written = WSJT_FT8_TX_SAMPLES
    status = WSJT_OK
  end function wsjt_ft8_encode

  function wsjt_ft8_decode_standard(samples, config_pointer, slot_id, &
                                     results, capacity, count_out, &
                                     overflow_out) result(status) &
      bind(C, name='wsjt_ft8_decode_standard')
    type(c_ptr), value :: samples, config_pointer, results
    type(c_ptr), value :: count_out, overflow_out
    integer(c_int64_t), value :: slot_id
    integer(c_int32_t), value :: capacity
    integer(c_int32_t) :: status
    integer(c_int16_t), pointer :: input_pointer(:)
    integer(c_int16_t) :: caller_samples(WSJT_FT8_RX_SAMPLES)
    integer(c_int16_t) :: staged_samples(WSJT_FT8_RX_SAMPLES)
    integer(c_int32_t), pointer :: count, overflow
    type(wsjt_decode_config), pointer :: config
    type(wsjt_decode_config) :: config_probe
    type(wsjt_decode_result), pointer :: output_results(:)
    type(ft8_decoder) :: decoder
    character(len=12) :: my_call, dx_call
    character(len=6) :: dx_grid
    logical :: samples_valid, config_valid, results_valid
    logical :: count_valid, overflow_valid, new_data, ap_enabled
    logical(kind=1) :: disk_data
    integer, parameter :: decode_stages(3) = [41, 47, 50]
    integer :: stage, staged_count

    samples_valid = c_associated(samples)
    config_valid = c_associated(config_pointer)
    results_valid = c_associated(results)
    count_valid = c_associated(count_out)
    overflow_valid = c_associated(overflow_out)

    status = WSJT_OK
    if (.not. samples_valid .or. .not. config_valid .or. &
        .not. results_valid .or. .not. count_valid .or. &
        .not. overflow_valid) then
      status = WSJT_E_NULL
    end if

    if (count_valid) then
      call c_f_pointer(count_out, count)
      count = 0_c_int32_t
    end if
    if (overflow_valid) then
      call c_f_pointer(overflow_out, overflow)
      overflow = 0_c_int32_t
    end if
    if (status /= WSJT_OK) return

    call c_f_pointer(config_pointer, config)
    if (config%struct_size /= int(c_sizeof(config_probe), c_int32_t)) then
      status = WSJT_E_ABI
    else if (config%sample_rate /= WSJT_FT8_RX_RATE) then
      status = WSJT_E_RATE
    else if (config%sample_count /= WSJT_FT8_RX_SAMPLES) then
      status = WSJT_E_SHAPE
    else if (capacity < WSJT_RESULT_CAPACITY) then
      status = WSJT_E_CAPACITY
    else if (.not. valid_standard_config(config)) then
      status = WSJT_E_CONFIG
    end if
    if (status /= WSJT_OK) return

    call c_f_pointer(samples, input_pointer, [int(WSJT_FT8_RX_SAMPLES)])
    call c_f_pointer(results, output_results, [int(WSJT_RESULT_CAPACITY)])
    caller_samples = input_pointer
    call copy_c_field(config%my_call, my_call)
    call copy_c_field(config%dx_call, dx_call)
    call copy_c_field(config%dx_grid, dx_grid)

    call batch_reset(slot_id)
    ap_enabled = iand(config%flags, WSJT_FLAG_AP) /= 0_c_int32_t
    disk_data = .true._1
    do stage = 1, size(decode_stages)
      staged_samples = 0_c_int16_t
      staged_count = min(decode_stages(stage) * 3456, &
                         int(WSJT_FT8_RX_SAMPLES))
      staged_samples(1:staged_count) = caller_samples(1:staged_count)
      new_data = .true.
      call decoder%decode(standard_decoded, staged_samples, &
                          config%qso_progress, config%rx_frequency, &
                          config%tx_frequency, new_data, config%utc_hhmmss, &
                          config%low_frequency, config%high_frequency, &
                          decode_stages(stage), config%sensitivity, 0.0, 0, &
                          .false., ap_enabled, .false., .false., &
                          config%ap_width, my_call, dx_call, dx_grid, disk_data)
    end do
    call batch_copy(output_results, capacity, count, overflow)
    status = WSJT_OK
  end function wsjt_ft8_decode_standard

  function wsjt_ft8_decode_improved(samples, config_pointer, slot_id, &
                                     results, capacity, count_out, &
                                     overflow_out) result(status) &
      bind(C, name='wsjt_ft8_decode_improved')
    type(c_ptr), value :: samples, config_pointer, results
    type(c_ptr), value :: count_out, overflow_out
    integer(c_int64_t), value :: slot_id
    integer(c_int32_t), value :: capacity
    integer(c_int32_t) :: status
    integer(c_int16_t), pointer :: input_pointer(:)
    integer(c_int16_t) :: caller_samples(WSJT_FT8_RX_SAMPLES)
    integer(c_int32_t), pointer :: count, overflow
    type(wsjt_decode_config), pointer :: config
    type(wsjt_decode_config) :: config_probe
    type(wsjt_decode_result), pointer :: output_results(:)
    logical :: samples_valid, config_valid, results_valid
    logical :: count_valid, overflow_valid

    samples_valid = c_associated(samples)
    config_valid = c_associated(config_pointer)
    results_valid = c_associated(results)
    count_valid = c_associated(count_out)
    overflow_valid = c_associated(overflow_out)

    status = WSJT_OK
    if (.not. samples_valid .or. .not. config_valid .or. &
        .not. results_valid .or. .not. count_valid .or. &
        .not. overflow_valid) then
      status = WSJT_E_NULL
    end if

    if (count_valid) then
      call c_f_pointer(count_out, count)
      count = 0_c_int32_t
    end if
    if (overflow_valid) then
      call c_f_pointer(overflow_out, overflow)
      overflow = 0_c_int32_t
    end if
    if (status /= WSJT_OK) return

    call c_f_pointer(config_pointer, config)
    if (config%struct_size /= int(c_sizeof(config_probe), c_int32_t)) then
      status = WSJT_E_ABI
    else if (config%sample_rate /= WSJT_FT8_RX_RATE) then
      status = WSJT_E_RATE
    else if (config%sample_count /= WSJT_FT8_RX_SAMPLES) then
      status = WSJT_E_SHAPE
    else if (capacity < WSJT_RESULT_CAPACITY) then
      status = WSJT_E_CAPACITY
    else if (.not. valid_improved_config(config)) then
      status = WSJT_E_CONFIG
    end if
    if (status /= WSJT_OK) return

    call c_f_pointer(samples, input_pointer, [int(WSJT_FT8_RX_SAMPLES)])
    call c_f_pointer(results, output_results, [int(WSJT_RESULT_CAPACITY)])
    caller_samples = input_pointer

    call batch_reset(slot_id)
    call run_improved_profile(caller_samples, config, status)
    if (status /= WSJT_OK) return
    call batch_deduplicate()
    call batch_copy(output_results, capacity, count, overflow)
    status = WSJT_OK
  end function wsjt_ft8_decode_improved

  subroutine standard_decoded(this, sync, snr, dt, freq, decoded, nap, qual)
    class(ft8_decoder), intent(inout) :: this
    real, intent(in) :: sync, dt, freq, qual
    integer, intent(in) :: snr, nap
    character(len=37), intent(in) :: decoded

    call append_standard(real(sync, c_float), int(snr, c_int32_t), &
                         real(dt, c_float), real(freq, c_float), decoded, &
                         int(nap, c_int32_t), real(qual, c_float))
  end subroutine standard_decoded

  subroutine copy_c_field(source, destination)
    character(kind=c_char), intent(in) :: source(:)
    character(len=*), intent(out) :: destination
    integer :: index, length_to_copy

    destination = ' '
    length_to_copy = min(size(source), len(destination))
    do index = 1, length_to_copy
      if (source(index) == c_null_char) exit
      destination(index:index) = achar(iachar(source(index)))
    end do
  end subroutine copy_c_field

  pure logical function valid_standard_config(config)
    type(wsjt_decode_config), intent(in) :: config
    integer(c_int64_t) :: frequency_width

    valid_standard_config = .false.
    if (config%qso_progress < 0_c_int32_t .or. &
        config%qso_progress > 5_c_int32_t) return
    if (config%sensitivity < 1_c_int32_t .or. &
        config%sensitivity > 3_c_int32_t) return
    if (.not. valid_hhmmss(config%utc_hhmmss)) return
    if (config%low_frequency < 100_c_int32_t) return
    if (config%high_frequency > 4910_c_int32_t) return
    if (config%high_frequency <= config%low_frequency) return
    frequency_width = int(config%high_frequency, c_int64_t) - &
                      int(config%low_frequency, c_int64_t)
    if (frequency_width < 100_c_int64_t) return
    valid_standard_config = .true.
  end function valid_standard_config

  pure logical function valid_improved_config(config)
    type(wsjt_decode_config), intent(in) :: config

    valid_improved_config = .false.
    if (.not. valid_standard_config(config)) return
    if (config%profile < 0_c_int32_t .or. &
        config%profile > 4_c_int32_t) return
    if (config%threads < 1_c_int32_t .or. &
        config%threads > 12_c_int32_t) return
    if (config%cycles < 1_c_int32_t .or. &
        config%cycles > 3_c_int32_t) return
    valid_improved_config = .true.
  end function valid_improved_config

  pure logical function valid_hhmmss(value)
    integer(c_int32_t), intent(in) :: value
    integer(c_int32_t) :: hour, minute, second

    valid_hhmmss = .false.
    if (value < 0_c_int32_t .or. value > 235959_c_int32_t) return
    hour = value / 10000_c_int32_t
    minute = mod(value / 100_c_int32_t, 100_c_int32_t)
    second = mod(value, 100_c_int32_t)
    if (hour > 23_c_int32_t) return
    if (minute > 59_c_int32_t) return
    if (second > 59_c_int32_t) return
    valid_hhmmss = .true.
  end function valid_hhmmss
end module wsjt_core_shim
