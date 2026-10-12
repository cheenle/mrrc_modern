module wsjt_improved
  use iso_c_binding, only: c_float, c_int, c_int16_t, c_int32_t
  use FFTW3, only: fftwf_init_threads
  use ft8_decode, only: ft8_decoder
  use ft8_decodevar, only: ft8_decodervar, ltry_a8
  use ft8_mod1, only: allfreq, allmessages, allsnrs, avexdt, calldteven, &
                      calldtodd, dd8, even, evencopy, evencq, evenmyc, &
                      evenqso, hisbcall, hiscall, hisgrid, hisgrid4, &
                      incall, lagcc, lagccbail, lapmyc, lastrxmsg, &
                      lenabledxcsearch, lhound, lmultinst, lqsomsgdcd, &
                      lskiptx1, ltxing, lwidedxcsearch, msgincall, msgroot, &
                      msgrootlen, mybcall, mycall, mycalllen1, ncandallthr, &
                      ndecodes, nfawide, nfbwide, nft8cycles, nincallthr, &
                      nintcount, nlasttx, nmsg, odd, oddcopy, oddcq, &
                      oddmyc, oddqso, sumxdtt, xdtincall
  use omp_lib, only: omp_get_num_threads, omp_get_thread_num, omp_set_dynamic
  use packjt77, only: calls10var, calls12var, calls22var, dxcall13_0var, &
                      dxcall13_setvar, dxcall13var, ihash22var, &
                      itxhash22var, lcommonft8b, mycall13_0var, &
                      mycall13_setvar, mycall13var, n28avar, n28bvar, &
                      nlast_callsvar, nzhashvar, nztxhashvar, &
                      recent_callsvar, txcalls10var, txcalls12var, &
                      txcalls22var
  use wsjt_batch, only: append_improved, append_standard
  use wsjt_a8_gate, only: clear_a8_near_rx
  use wsjt_partition, only: compute_band
  use wsjt_types, only: WSJT_FLAG_AP, WSJT_FLAG_HIDE_DUPES, &
                        WSJT_FLAG_LOW_THRESHOLD, WSJT_FLAG_WIDE_DX, &
                        WSJT_E_INTERNAL, WSJT_FT8_RX_SAMPLES, WSJT_OK, &
                        wsjt_decode_config
  implicit none
  private

  logical, save :: tables_ready = .false.
  integer(c_int32_t), save :: request_rx_frequency = 0_c_int32_t

  public :: run_improved_profile

contains

  subroutine run_improved_profile(samples, config, status)
    integer(c_int16_t), intent(in) :: samples(WSJT_FT8_RX_SAMPLES)
    type(wsjt_decode_config), intent(in) :: config
    integer(c_int32_t), intent(out) :: status
    type(ft8_decoder) :: standard_decoder
    integer :: improved_pass

    status = WSJT_OK
    call initialize_algorithm_tables()
    call initialize_request_state(config)

    select case (config%profile)
    case (0_c_int32_t)
      call run_standard_stage(standard_decoder, samples, config, 41)
      improved_pass = 49
    case (1_c_int32_t)
      call run_standard_stage(standard_decoder, samples, config, 41)
      call run_standard_stage(standard_decoder, samples, config, 46)
      improved_pass = 50
    case (2_c_int32_t)
      improved_pass = 48
    case (3_c_int32_t)
      improved_pass = 49
    case default
      improved_pass = 50
    end select

    call run_improved_pass(samples, config, improved_pass, status)
  end subroutine run_improved_profile

  subroutine initialize_algorithm_tables()
    integer(c_int) :: threads_status

    if (tables_ready) return
    threads_status = fftwf_init_threads()
    if (threads_status == 0_c_int) error stop 'FFTW thread initialization failed'
    call cwfilter(.true.)
    tables_ready = .true.
  end subroutine initialize_algorithm_tables

  subroutine initialize_request_state(config)
    type(wsjt_decode_config), intent(in) :: config
    integer :: history_index, thread_index
    logical :: my_call_standard, dx_call_standard
    logical(kind=1) :: ap_enabled

    call copy_c_field(config%my_call, mycall)
    call copy_c_field(config%dx_call, hiscall)
    call copy_c_field(config%dx_grid, hisgrid)
    mybcall = mycall
    hisbcall = hiscall
    hisgrid4 = hisgrid(1:4)

    call stdcall(mycall, my_call_standard)
    call stdcall(hiscall, dx_call_standard)
    ap_enabled = iand(config%flags, WSJT_FLAG_AP) /= 0_c_int32_t
    request_rx_frequency = config%rx_frequency

    dd8 = 0.0
    ndecodes = 0
    allmessages = ''
    allsnrs = 0
    allfreq = 0.0
    nmsg = 0
    odd%freq = 0.0
    odd%dt = 0.0
    odd%msg = ''
    odd%lstate = .false.
    even%freq = 0.0
    even%dt = 0.0
    even%msg = ''
    even%lstate = .false.
    oddcopy%freq = 0.0
    oddcopy%dt = 0.0
    oddcopy%msg = ''
    oddcopy%lstate = .false.
    evencopy%freq = 0.0
    evencopy%dt = 0.0
    evencopy%msg = ''
    evencopy%lstate = .false.
    calldteven%dt = 0.0
    calldteven%call2 = ''
    calldtodd%dt = 0.0
    calldtodd%call2 = ''
    incall%xdt = 0.0
    incall%msg = ''
    lastrxmsg%xdt = 0.0
    lastrxmsg%lastmsg = ''
    lastrxmsg%lstate = .false.
    msgincall = ''
    xdtincall = 0.0
    ncandallthr = 0
    nincallthr = 0
    avexdt = 0.0
    sumxdtt = 0.0
    nintcount = 0
    lqsomsgdcd = .false.
    evencq%freq = 6000.0
    evencq%xdt = 0.0
    oddcq%freq = 6000.0
    oddcq%xdt = 0.0
    evenmyc%freq = 6000.0
    evenmyc%xdt = 0.0
    oddmyc%freq = 6000.0
    oddmyc%xdt = 0.0
    evenqso%freq = 6000.0
    evenqso%xdt = 0.0
    oddqso%freq = 6000.0
    oddqso%xdt = 0.0
    do thread_index = 1, size(evencq, 2)
      do history_index = 1, size(evencq, 1)
        evencq(history_index, thread_index)%cs = (0.0, 0.0)
        oddcq(history_index, thread_index)%cs = (0.0, 0.0)
      end do
      do history_index = 1, size(evenmyc, 1)
        evenmyc(history_index, thread_index)%cs = (0.0, 0.0)
        oddmyc(history_index, thread_index)%cs = (0.0, 0.0)
      end do
      evenqso(1, thread_index)%cs = (0.0, 0.0)
      oddqso(1, thread_index)%cs = (0.0, 0.0)
    end do
    nft8cycles = config%cycles
    nfawide = config%low_frequency
    nfbwide = config%high_frequency
    nlasttx = 0
    mycalllen1 = len_trim(mycall) + 1
    msgroot = trim(mycall)//' '//trim(hiscall)//' '
    msgrootlen = len_trim(msgroot)
    lapmyc = ap_enabled
    lagcc = .false.
    lagccbail = .false.
    lhound = .false.
    lenabledxcsearch = ap_enabled .and. len_trim(hiscall) >= 3
    lwidedxcsearch = iand(config%flags, WSJT_FLAG_WIDE_DX) /= 0_c_int32_t
    lmultinst = .false.
    lskiptx1 = .false.
    ltxing = .false.
    ltry_a8 = ap_enabled .and. len_trim(hiscall) >= 3 .and. &
              len_trim(hisgrid4) >= 4

    calls10var = ''
    calls12var = ''
    calls22var = ''
    txcalls10var = ''
    txcalls12var = ''
    txcalls22var = ''
    ihash22var = -1
    itxhash22var = -1
    nzhashvar = 0
    nztxhashvar = 0
    nlast_callsvar = 0
    mycall13var = ''
    dxcall13var = ''
    mycall13_0var = ''
    dxcall13_0var = ''
    mycall13_setvar = .false.
    dxcall13_setvar = .false.
    lcommonft8b = .true.

    call fillhashvar(int(config%threads), .false.)
    call ft8apsetvar(logical(my_call_standard, kind=1), &
                     logical(dx_call_standard, kind=1), int(config%threads))
    if (len_trim(hiscall) >= 3) then
      call tone8(logical(my_call_standard, kind=1), &
                 logical(dx_call_standard, kind=1))
    end if
    if (my_call_standard .and. len_trim(mycall) >= 3) call tone8myc()
  end subroutine initialize_request_state

  subroutine run_standard_stage(decoder, samples, config, stage)
    type(ft8_decoder), intent(inout) :: decoder
    integer(c_int16_t), intent(in) :: samples(WSJT_FT8_RX_SAMPLES)
    type(wsjt_decode_config), intent(in) :: config
    integer, intent(in) :: stage
    integer(c_int16_t) :: staged_samples(WSJT_FT8_RX_SAMPLES)
    integer :: staged_count
    logical :: ap_enabled, new_data
    logical(kind=1) :: disk_data
    character(len=12) :: standard_my_call, standard_dx_call
    character(len=6) :: standard_dx_grid

    staged_samples = 0_c_int16_t
    staged_count = min(stage * 3456, int(WSJT_FT8_RX_SAMPLES))
    staged_samples(1:staged_count) = samples(1:staged_count)
    call copy_c_field(config%my_call, standard_my_call)
    call copy_c_field(config%dx_call, standard_dx_call)
    call copy_c_field(config%dx_grid, standard_dx_grid)
    new_data = .true.
    ap_enabled = iand(config%flags, WSJT_FLAG_AP) /= 0_c_int32_t
    disk_data = .true._1
    call decoder%decode(standard_decoded, staged_samples, &
                        config%qso_progress, config%rx_frequency, &
                        config%tx_frequency, new_data, config%utc_hhmmss, &
                        config%low_frequency, config%high_frequency, stage, &
                        config%sensitivity, 0.0, 0, .false., ap_enabled, &
                        .false., .false., config%ap_width, standard_my_call, &
                        standard_dx_call, standard_dx_grid, disk_data)
  end subroutine run_standard_stage

  subroutine run_improved_pass(samples, config, pass, status)
    integer(c_int16_t), intent(in) :: samples(WSJT_FT8_RX_SAMPLES)
    type(wsjt_decode_config), intent(in) :: config
    integer, intent(in) :: pass
    integer(c_int32_t), intent(out) :: status
    type(ft8_decodervar) :: decoder
    integer :: actual_threads, band_high, band_low, index, populated
    integer :: npatience, fftw_threads
    logical :: my_call_standard, dx_call_standard
    logical(kind=1) :: a8_owner, ap_enabled, hide_dupes, low_threshold, subpass
    common /patience/ npatience, fftw_threads

    populated = min(int(WSJT_FT8_RX_SAMPLES), pass * 3456)
    dd8 = 0.0
    dd8(1:populated) = real(samples(1:populated))
    call stdcall(mycall, my_call_standard)
    call stdcall(hiscall, dx_call_standard)
    ap_enabled = iand(config%flags, WSJT_FLAG_AP) /= 0_c_int32_t
    hide_dupes = iand(config%flags, WSJT_FLAG_HIDE_DUPES) /= 0_c_int32_t
    low_threshold = iand(config%flags, WSJT_FLAG_LOW_THRESHOLD) /= 0_c_int32_t
    subpass = config%sensitivity == 3_c_int32_t
    npatience = 0
    fftw_threads = config%threads
    status = WSJT_OK
    actual_threads = 0
    call omp_set_dynamic(.false.)

    !$omp parallel default(shared) &
    !$omp& private(decoder,index,band_low,band_high,a8_owner) &
    !$omp& num_threads(config%threads) copyin(dd8)
      index = omp_get_thread_num() + 1
    !$omp single
      actual_threads = omp_get_num_threads()
      if (actual_threads /= int(config%threads)) status = WSJT_E_INTERNAL
    !$omp end single
    !$omp barrier
    if (status == WSJT_OK) then
      recent_callsvar = ''
      n28avar = 0
      n28bvar = 0
      call compute_band(int(config%low_frequency), int(config%high_frequency), &
                        int(config%threads), index, band_low, band_high)
      a8_owner = config%rx_frequency >= band_low .and. &
                 config%rx_frequency <= band_high
      if (index == 1 .and. &
          (config%rx_frequency < config%low_frequency .or. &
           config%rx_frequency > config%high_frequency)) a8_owner = .true._1
      call decoder%decodevar(improved_decoded, config%qso_progress, &
           config%rx_frequency, config%sensitivity, config%tx_frequency, &
           config%utc_hhmmss, band_low, band_high, 100, 0, &
           mod(config%utc_hhmmss, 100_c_int32_t), config%ap_width, &
           logical(my_call_standard, kind=1), &
           logical(dx_call_standard, kind=1), .false._1, index, &
           int(config%threads), .false., low_threshold, subpass, hide_dupes, &
           ap_enabled, 0, a8_owner)
    end if
    !$omp end parallel
  end subroutine run_improved_pass

  subroutine standard_decoded(this, sync, snr, dt, freq, decoded, nap, qual)
    class(ft8_decoder), intent(inout) :: this
    real, intent(in) :: sync, dt, freq, qual
    integer, intent(in) :: snr, nap
    character(len=37), intent(in) :: decoded

    call clear_a8_near_rx(ltry_a8, freq, int(request_rx_frequency))
    call append_standard(real(sync, c_float), int(snr, c_int32_t), &
                         real(dt, c_float), real(freq, c_float), decoded, &
                         int(nap, c_int32_t), real(qual, c_float))
  end subroutine standard_decoded

  subroutine improved_decoded(this, snr, dt, freq, decoded, nap, qual)
    class(ft8_decodervar), intent(inout) :: this
    integer, intent(in) :: snr, nap
    real, intent(in) :: dt, freq, qual
    character(len=37), intent(in) :: decoded

    call clear_a8_near_rx(ltry_a8, freq, int(request_rx_frequency))
    call append_improved(int(snr, c_int32_t), real(dt, c_float), &
                         real(freq, c_float), decoded, int(nap, c_int32_t), &
                         real(qual, c_float))
  end subroutine improved_decoded

  subroutine copy_c_field(source, destination)
    use iso_c_binding, only: c_char, c_null_char
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
end module wsjt_improved
