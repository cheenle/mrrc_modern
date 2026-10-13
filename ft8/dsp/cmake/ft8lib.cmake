# ft8/dsp/cmake/ft8lib.cmake
# ft8_lib（kiss-fft 版 FT8 解码器）的最小 C 子集，供 wsjt_ft8_decode_fast 使用。
# 源列表与 ft8/dsp/vendor-ft8lib.sha256 一一对应；新增文件必须同时更新两者。
set(FT8LIB_LIB "${CMAKE_CURRENT_SOURCE_DIR}/vendor-ft8lib")
set(FT8LIB_SOURCES
    ${FT8LIB_LIB}/ft8/constants.c
    ${FT8LIB_LIB}/ft8/crc.c
    ${FT8LIB_LIB}/ft8/decode.c
    ${FT8LIB_LIB}/ft8/encode.c
    ${FT8LIB_LIB}/ft8/ldpc.c
    ${FT8LIB_LIB}/ft8/message.c
    ${FT8LIB_LIB}/ft8/text.c
    ${FT8LIB_LIB}/common/monitor.c
    ${FT8LIB_LIB}/fft/kiss_fft.c
    ${FT8LIB_LIB}/fft/kiss_fftr.c
)
