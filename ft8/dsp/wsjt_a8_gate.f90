module wsjt_a8_gate
  implicit none
  private

  public :: clear_a8_near_rx

contains

  subroutine clear_a8_near_rx(gate, frequency, rx_frequency)
    logical, intent(inout) :: gate
    real, intent(in) :: frequency
    integer, intent(in) :: rx_frequency

    if (abs(frequency - real(rx_frequency)) < 3.0) then
      !$omp atomic write
      gate = .false.
    end if
  end subroutine clear_a8_near_rx

end module wsjt_a8_gate
