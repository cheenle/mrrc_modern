cmake_minimum_required(VERSION 3.22.1)
project(opus_jni)

# vendored libopus（其自带 CMakeLists，产出 `opus` target）
add_subdirectory(opus)

add_library(opus_jni SHARED opus_jni.c)
target_link_libraries(opus_jni opus)
