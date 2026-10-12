module wsjt_partition
  implicit none
  private

  public :: compute_band

contains

  pure subroutine compute_band(low_frequency, high_frequency, threads, index, &
                               band_low, band_high)
    integer, intent(in) :: low_frequency, high_frequency, threads, index
    integer, intent(out) :: band_low, band_high
    integer :: total_frequencies

    total_frequencies = high_frequency - low_frequency + 1
    band_low = low_frequency + ((index - 1) * total_frequencies) / threads
    band_high = low_frequency + (index * total_frequencies) / threads - 1
  end subroutine compute_band

end module wsjt_partition
