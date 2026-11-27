cmake_minimum_required(VERSION 3.22.1)
project(gt4recomp C)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -O2 -w -march=armv8-a+simd")

file(GLOB LIFTED_SOURCES "${LIFTED_DIR}/generated*.c")

add_library(gt4recomp SHARED
${LIFTED_SOURCES}
${LIFTED_DIR}/func_table.c
${LIFTED_DIR}/ps2rt.c
${CMAKE_CURRENT_SOURCE_DIR}/../runtime/ps2_sif_hle.c
${CMAKE_CURRENT_SOURCE_DIR}/../runtime/ps2_hw_hle.c
jni/ps2_gs_gl.c
jni/ps2_android_bridge.c
)

target_include_directories(gt4recomp PRIVATE
${LIFTED_DIR}
${CMAKE_CURRENT_SOURCE_DIR}/../runtime
${CMAKE_CURRENT_SOURCE_DIR}/jni
)
target_link_libraries(gt4recomp android EGL GLESv3 aaudio log m)
