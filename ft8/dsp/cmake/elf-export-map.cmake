function(mrrc_configure_elf_export_map output test_hooks)
    set(WSJT_CORE_TEST_HOOK_EXPORT "")
    if(test_hooks)
        set(WSJT_CORE_TEST_HOOK_EXPORT "        wsjt_test_ft8_a8d;\n")
    endif()
    configure_file(
        "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/wsjt_core.exports.map.in"
        "${output}"
        @ONLY
    )
endfunction()
