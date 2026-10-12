program a8_gate_probe
  use omp_lib, only: omp_get_thread_num
  use wsjt_a8_gate, only: clear_a8_near_rx
  implicit none

  logical :: gate, snapshot
  integer :: thread_index

  gate = .true.
  call clear_a8_near_rx(gate, 1503.0, 1500)
  if (.not. gate) error stop 1
  call clear_a8_near_rx(gate, 1497.0, 1500)
  if (.not. gate) error stop 2
  call clear_a8_near_rx(gate, 1502.999, 1500)
  if (gate) error stop 3

  gate = .true.
!$omp parallel num_threads(4) private(thread_index,snapshot) shared(gate)
  thread_index = omp_get_thread_num()
  if (thread_index == 2) then
    call clear_a8_near_rx(gate, 1498.0, 1500)
  else
    call clear_a8_near_rx(gate, 1600.0, 1500)
  endif
!$omp barrier
!$omp atomic read
  snapshot = gate
  if (snapshot) error stop 4
!$omp end parallel
end program a8_gate_probe
