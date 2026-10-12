module wsjt_test_hooks
  use iso_c_binding, only: c_associated, c_char, c_f_pointer, c_float, &
                           c_int16_t, c_int32_t, c_null_char, c_ptr
  use wsjt_types, only: WSJT_E_NULL, WSJT_FT8_RX_SAMPLES, WSJT_OK, &
                        WSJT_TEXT_BYTES
  implicit none
  private

  public :: wsjt_test_ft8_a8d

contains

  function wsjt_test_ft8_a8d(samples, rx_frequency, text_out) result(status) &
      bind(C, name='wsjt_test_ft8_a8d')
    type(c_ptr), value :: samples, text_out
    integer(c_int32_t), value :: rx_frequency
    integer(c_int32_t) :: status
    integer(c_int16_t), pointer :: input(:)
    character(kind=c_char), pointer :: output(:)
    real :: dd(WSJT_FT8_RX_SAMPLES)
    real :: f1, fbest, plog, xdt, xsnr
    character(len=12) :: dx_call, my_call
    character(len=6) :: dx_grid
    character(len=37) :: message
    integer :: index, message_length

    if (.not. c_associated(samples) .or. .not. c_associated(text_out)) then
      status = WSJT_E_NULL
      return
    end if
    call c_f_pointer(samples, input, [int(WSJT_FT8_RX_SAMPLES)])
    call c_f_pointer(text_out, output, [int(WSJT_TEXT_BYTES)])
    output = c_null_char
    dd = real(input)
    my_call = 'N0CALL'
    dx_call = 'K1ABC'
    dx_grid = 'FN42'
    f1 = real(rx_frequency)
    call ft8_a8d(dd, my_call, dx_call, dx_grid, f1, xdt, fbest, xsnr, &
                 plog, message)
    message_length = min(len_trim(message), int(WSJT_TEXT_BYTES) - 1)
    do index = 1, message_length
      output(index) = message(index:index)
    end do
    status = WSJT_OK
  end function wsjt_test_ft8_a8d

end module wsjt_test_hooks
