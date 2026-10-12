module wsjt_batch
  use iso_c_binding, only: c_char, c_float, c_int32_t, c_int64_t, &
                           c_null_char
  use wsjt_types, only: WSJT_FLAG_AP, WSJT_RESULT_CAPACITY, &
                        WSJT_TEXT_BYTES, wsjt_decode_result
  implicit none
  private

  type(wsjt_decode_result) :: batch(WSJT_RESULT_CAPACITY)
  integer(c_int32_t) :: batch_count = 0_c_int32_t
  integer(c_int32_t) :: batch_overflow = 0_c_int32_t
  integer(c_int64_t) :: batch_slot = 0_c_int64_t

  public :: append_improved, append_standard, batch_copy, &
            batch_deduplicate, batch_reset

contains

  subroutine batch_reset(slot_id)
    integer(c_int64_t), intent(in) :: slot_id
    integer :: index

    batch_count = 0_c_int32_t
    batch_overflow = 0_c_int32_t
    batch_slot = slot_id
    batch%slot_id = 0_c_int64_t
    batch%sync = 0.0_c_float
    batch%dt = 0.0_c_float
    batch%frequency = 0.0_c_float
    batch%quality = 0.0_c_float
    batch%snr = 0_c_int32_t
    batch%ap_type = 0_c_int32_t
    batch%flags = 0_c_int32_t
    batch%reserved = 0_c_int32_t
    do index = 1, int(WSJT_RESULT_CAPACITY)
      batch(index)%text = c_null_char
      batch(index)%padding = c_null_char
    end do
  end subroutine batch_reset

  subroutine append_standard(sync, snr, dt, freq, text, nap, qual)
    real(c_float), intent(in) :: sync, dt, freq, qual
    integer(c_int32_t), intent(in) :: snr, nap
    character(len=*), intent(in) :: text

    call append_result(sync, snr, dt, freq, text, nap, qual)
  end subroutine append_standard

  subroutine append_improved(snr, dt, freq, text, nap, qual)
    real(c_float), intent(in) :: dt, freq, qual
    integer(c_int32_t), intent(in) :: snr, nap
    character(len=*), intent(in) :: text

    call append_result(0.0_c_float, snr, dt, freq, text, nap, qual)
  end subroutine append_improved

  subroutine append_result(sync, snr, dt, freq, text, nap, qual)
    real(c_float), intent(in) :: sync, dt, freq, qual
    integer(c_int32_t), intent(in) :: snr, nap
    character(len=*), intent(in) :: text
    integer :: index, text_index, text_length

    !$omp critical(wsjt_batch_append)
    if (batch_count < WSJT_RESULT_CAPACITY) then
      batch_count = batch_count + 1_c_int32_t
      index = int(batch_count)
      batch(index)%slot_id = batch_slot
      batch(index)%sync = sync
      batch(index)%dt = dt
      batch(index)%frequency = freq
      batch(index)%quality = qual
      batch(index)%snr = snr
      batch(index)%ap_type = nap
      batch(index)%flags = merge(WSJT_FLAG_AP, 0_c_int32_t, nap /= 0)
      batch(index)%reserved = 0_c_int32_t
      batch(index)%text = c_null_char
      batch(index)%padding = c_null_char
      text_length = min(len_trim(text), int(WSJT_TEXT_BYTES) - 1)
      do text_index = 1, text_length
        batch(index)%text(text_index) = text(text_index:text_index)
      end do
    else
      batch_overflow = 1_c_int32_t
    end if
    !$omp end critical(wsjt_batch_append)
  end subroutine append_result

  subroutine batch_copy(output, capacity, count, overflow)
    type(wsjt_decode_result), intent(out) :: output(:)
    integer(c_int32_t), intent(in) :: capacity
    integer(c_int32_t), intent(out) :: count, overflow

    count = 0_c_int32_t
    overflow = 0_c_int32_t
    if (capacity < WSJT_RESULT_CAPACITY) return

    count = batch_count
    overflow = batch_overflow
    if (batch_count > 0_c_int32_t) then
      output(1:int(batch_count)) = batch(1:int(batch_count))
    end if
  end subroutine batch_copy

  subroutine batch_deduplicate()
    integer :: candidate, existing, write_index
    logical :: duplicate

    write_index = 0
    do candidate = 1, int(batch_count)
      duplicate = .false.
      do existing = 1, write_index
        if (same_decode_key(batch(candidate), batch(existing))) then
          duplicate = .true.
          exit
        end if
      end do
      if (.not. duplicate) then
        write_index = write_index + 1
        if (write_index /= candidate) batch(write_index) = batch(candidate)
      end if
    end do
    batch_count = int(write_index, c_int32_t)
  end subroutine batch_deduplicate

  pure logical function same_decode_key(left, right)
    type(wsjt_decode_result), intent(in) :: left, right

    same_decode_key = all(left%text == right%text) .and. &
                      nint(left%frequency) == nint(right%frequency) .and. &
                      nint(10.0_c_float * left%dt) == &
                      nint(10.0_c_float * right%dt)
  end function same_decode_key
end module wsjt_batch
