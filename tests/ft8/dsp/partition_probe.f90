program partition_probe
  use wsjt_partition, only: compute_band
  implicit none

  integer :: band_high, band_low, expected_low
  integer :: high_frequency, index, low_frequency, threads

  do low_frequency = 100, 300, 100
    do high_frequency = low_frequency + 100, 4910
      do threads = 1, 12
        expected_low = low_frequency
        do index = 1, threads
          call compute_band(low_frequency, high_frequency, threads, index, &
                            band_low, band_high)
          if (band_low /= expected_low) error stop 1
          if (band_high < band_low) error stop 2
          expected_low = band_high + 1
        end do
        if (expected_low /= high_frequency + 1) error stop 3
      end do
    end do
  end do
end program partition_probe
