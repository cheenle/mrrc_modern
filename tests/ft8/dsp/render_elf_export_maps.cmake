include("${ROOT}/ft8/dsp/cmake/elf-export-map.cmake")

mrrc_configure_elf_export_map("${OUTPUT_DIR}/production.map" OFF)
mrrc_configure_elf_export_map("${OUTPUT_DIR}/test-hooks.map" ON)
