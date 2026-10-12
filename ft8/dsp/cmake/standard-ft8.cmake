set(STANDARD_FT8_SOURCES
  ${WSJTX_LIB}/crc.f90 ${WSJTX_LIB}/fftw3mod.f90
  ${WSJTX_LIB}/hashing.f90 ${WSJTX_LIB}/iso_c_utilities.f90
  ${WSJTX_LIB}/packjt.f90
  ${WSJTX_LIB}/77bit/packjt77.f90 ${WSJTX_LIB}/timer_module.f90
  ${WSJTX_LIB}/timer_impl.f90 ${WSJTX_LIB}/timer_C_wrapper.f90
  ${WSJTX_LIB}/shmem.f90 ${WSJTX_LIB}/jt65_mod6.f90
  ${WSJTX_LIB}/ft8_decode.f90 ${WSJTX_LIB}/ft8/ft8_a7.f90
  ${WSJTX_LIB}/ft8/ft8_a8d.f90 ${WSJTX_LIB}/ft8/baseline.f90
  ${WSJTX_LIB}/ft8/bpdecode174_91.f90 ${WSJTX_LIB}/ft8/chkcrc13a.f90
  ${WSJTX_LIB}/ft8/chkcrc14a.f90 ${WSJTX_LIB}/ft8/compress.f90
  ${WSJTX_LIB}/ft8/decode174_91.f90 ${WSJTX_LIB}/ft8/encode174_91.f90
  ${WSJTX_LIB}/ft8/encode174_91_nocrc.f90 ${WSJTX_LIB}/ft8/filt8.f90
  ${WSJTX_LIB}/ft8/ft8apset.f90 ${WSJTX_LIB}/ft8/ft8b.f90
  ${WSJTX_LIB}/ft8/ft8_downsample.f90 ${WSJTX_LIB}/ft8/genft8.f90
  ${WSJTX_LIB}/ft8/gen_ft8wave.f90 ${WSJTX_LIB}/ft8/get_crc14.f90
  ${WSJTX_LIB}/ft8/get_spectrum_baseline.f90 ${WSJTX_LIB}/ft8/h1.f90
  ${WSJTX_LIB}/ft8/osd174_91.f90 ${WSJTX_LIB}/ft8/subtractft8.f90
  ${WSJTX_LIB}/ft8/sync8.f90 ${WSJTX_LIB}/ft8/sync8d.f90
  ${WSJTX_LIB}/ft8/twkfreq1.f90 ${WSJTX_LIB}/ft2/gfsk_pulse.f90
  ${WSJTX_LIB}/db.f90 ${WSJTX_LIB}/determ.f90 ${WSJTX_LIB}/four2a.f90
  ${WSJTX_LIB}/chkcall.f90 ${WSJTX_LIB}/deg2grid.f90 ${WSJTX_LIB}/fmtmsg.f90
  ${WSJTX_LIB}/grid2deg.f90 ${WSJTX_LIB}/indexx.f90
  ${WSJTX_LIB}/nuttal_window.f90 ${WSJTX_LIB}/pctile.f90 ${WSJTX_LIB}/peakup.f90
  ${WSJTX_LIB}/platanh.f90 ${WSJTX_LIB}/polyfit.f90 ${WSJTX_LIB}/prog_args.f90
  ${WSJTX_LIB}/smo.f90 ${WSJTX_LIB}/smo121.f90
  ${WSJTX_LIB}/shell.f90 ${CMAKE_CURRENT_SOURCE_DIR}/ft8_stdcall.f90
  ${WSJTX_LIB}/crc13.cpp ${WSJTX_LIB}/crc14.cpp
  ${CMAKE_CURRENT_SOURCE_DIR}/shmem_stub.c)
