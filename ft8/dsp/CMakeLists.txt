cmake_minimum_required(VERSION 3.24)

project(wsjt_core VERSION 1.0.0 LANGUAGES C CXX Fortran)

option(MRRC_FT8_TEST_HOOKS "Build non-production DSP test hooks" OFF)

include(${CMAKE_CURRENT_SOURCE_DIR}/toolchain.cmake)

find_package(OpenMP REQUIRED COMPONENTS Fortran)
# WSJT-X's crc13.cpp/crc14.cpp need <boost/crc.hpp> (header-only use).
# Declared explicitly so a missing Boost fails at configure time, not as a
# fatal compile error deep in the vendor tree.
find_package(Boost REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(FFTW3F REQUIRED IMPORTED_TARGET fftw3f)
find_library(FFTW3F_THREADS_LIBRARY NAMES fftw3f_threads REQUIRED)

set(WSJTX_LIB "${CMAKE_CURRENT_SOURCE_DIR}/vendor")
include(cmake/standard-ft8.cmake)
include(cmake/improved-ft8.cmake)
# ft8_lib fast-path C sources (vendor-ft8lib/) — see cmake/ft8lib.cmake
include(cmake/ft8lib.cmake)

add_library(wsjt_core SHARED
    wsjt_types.f90
    wsjt_batch.f90
    wsjt_partition.f90
    wsjt_a8_gate.f90
    wsjt_improved.f90
    wsjt_core_shim.f90
    ${STANDARD_FT8_SOURCES}
    ${IMPROVED_FT8_SOURCES}
    ${FT8LIB_SOURCES}
)

if(MRRC_FT8_TEST_HOOKS)
    target_sources(wsjt_core PRIVATE wsjt_test_hooks.f90)
endif()

target_include_directories(wsjt_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_include_directories(wsjt_core PRIVATE
    ${WSJTX_LIB}
    ${WSJTX_LIB}/ft8
    ${WSJTX_LIB}/ft8var
    ${FT8LIB_LIB}
    ${Boost_INCLUDE_DIRS}
)
target_link_libraries(wsjt_core PRIVATE
    OpenMP::OpenMP_Fortran
    PkgConfig::FFTW3F
    ${FFTW3F_THREADS_LIBRARY}
)

set_target_properties(wsjt_core PROPERTIES OUTPUT_NAME wsjt_core)

if(APPLE)
    set(WSJT_CORE_UNEXPORTED_SYMBOLS
        "${CMAKE_CURRENT_BINARY_DIR}/wsjt_core.unexports"
    )
    # ld64 has no version script, so symbols are hidden with a deny list.  It
    # is never complete: every new vendored source family adds names a deny
    # list cannot anticipate.  The ft8_lib fast-path C sources (cmake/
    # ft8lib.cmake) added ~66 of them, so their families are enumerated below.
    # ld64 does apply glob patterns here (verified: `_ftx_*` removed all 20
    # ftx symbols), so tight per-family globs are used where a shared prefix
    # exists and exact names otherwise.  None of these may match the four
    # planned `_wsjt_*` entry points.
    file(GENERATE OUTPUT "${WSJT_CORE_UNEXPORTED_SYMBOLS}" CONTENT [=[
___emutls_*
___gcc_nested_func_ptr_*
___*_MOD_*
_.gomp_*
__Z*
_*_
_c_init_timer
_crc*
_shmem_*
_ftx_*
_monitor_*
_kiss_fft*
_kFT*
_ldpc_*
_bp_decode
_ft4_*
_ft8_*
_trim*
_is_*
_append_string
_charn
_copy_token
_dd_to_int
_ends_with
_equals
_fmtmsg
_in_range
_int_to_dd
_nchar
_pack_basecall
_starts_with
_to_upper
]=])
    target_link_options(wsjt_core PRIVATE
        "LINKER:-unexported_symbols_list,${WSJT_CORE_UNEXPORTED_SYMBOLS}"
    )
    set_property(TARGET wsjt_core APPEND PROPERTY
        LINK_DEPENDS "${WSJT_CORE_UNEXPORTED_SYMBOLS}"
    )
elseif(UNIX)
    include(cmake/elf-export-map.cmake)
    set(WSJT_CORE_VERSION_SCRIPT
        "${CMAKE_CURRENT_BINARY_DIR}/wsjt_core.exports.map"
    )
    mrrc_configure_elf_export_map(
        "${WSJT_CORE_VERSION_SCRIPT}"
        "${MRRC_FT8_TEST_HOOKS}"
    )
    target_link_options(wsjt_core PRIVATE
        "LINKER:--version-script,${WSJT_CORE_VERSION_SCRIPT}"
    )
    set_property(TARGET wsjt_core APPEND PROPERTY
        LINK_DEPENDS "${WSJT_CORE_VERSION_SCRIPT}"
    )
endif()
