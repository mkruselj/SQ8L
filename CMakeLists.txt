cmake_minimum_required(VERSION 3.22)
project(sq8l VERSION 0.1.0 LANGUAGES C CXX)
if(APPLE)
  enable_language(OBJC OBJCXX)
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# Bit-faithful floating point: no FMA contraction, honor the dynamic rounding
# mode (the engine switches to round-toward-zero like the original x87 code).
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  set(SQ8L_FP_FLAGS -ffp-model=strict -ffp-contract=off)
elseif(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
  set(SQ8L_FP_FLAGS -frounding-math -fsignaling-nans -ffp-contract=off)
elseif(MSVC)
  set(SQ8L_FP_FLAGS /fp:strict)
endif()

file(GLOB SQ8L_ENGINE_SOURCES CONFIGURE_DEPENDS src/engine/*.cpp src/engine/data/*.cpp)
# Correctly rounded exp/log/sin/cos/tan (identical results on every platform).
set(SQ8L_CRMATH_SOURCES third_party/core-math/exp.c third_party/core-math/log.c third_party/core-math/sin.c
    third_party/core-math/cos.c third_party/core-math/tan.c)
if(WIN32)
  list(APPEND SQ8L_CRMATH_SOURCES third_party/core-math/roundeven_shim.c)
endif()
add_library(sq8l_engine STATIC ${SQ8L_ENGINE_SOURCES} ${SQ8L_CRMATH_SOURCES})
target_include_directories(sq8l_engine PUBLIC src/engine)
target_compile_options(sq8l_engine PRIVATE ${SQ8L_FP_FLAGS} -Wall -Wextra)
add_subdirectory(src/gui)
add_subdirectory(src/gui/logic)

# C API used by the Python differential tests against the emulated original.
file(GLOB SQ8L_TESTAPI_SOURCES CONFIGURE_DEPENDS tests/capi*.cpp)
add_library(sq8l_testapi SHARED ${SQ8L_TESTAPI_SOURCES})
target_link_libraries(sq8l_testapi PRIVATE sq8l_engine sq8l_gui sq8l_gui_logic)
target_compile_options(sq8l_testapi PRIVATE ${SQ8L_FP_FLAGS})

# ---------------------------------------------------------------- plugin (DPF)
option(SQ8L_BUILD_PLUGIN "Build the SQ8L plugin (VST2/VST3/AU/CLAP)" ON)
if(SQ8L_BUILD_PLUGIN)
  # Local DPF patches (kept out of the submodule): apply once if not yet applied.
  foreach(patch dpf-vst2-compat.patch)
    execute_process(COMMAND git apply --reverse --check ${CMAKE_SOURCE_DIR}/patches/${patch}
                    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}/third_party/DPF
                    RESULT_VARIABLE already OUTPUT_QUIET ERROR_QUIET)
    if(NOT already EQUAL 0)
      execute_process(COMMAND git apply ${CMAKE_SOURCE_DIR}/patches/${patch}
                      WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}/third_party/DPF
                      RESULT_VARIABLE failed)
      if(failed)
        message(FATAL_ERROR "could not apply patches/${patch} to third_party/DPF")
      endif()
    endif()
  endforeach()
  add_subdirectory(third_party/DPF)
  dpf_add_plugin(sq8l
    TARGETS vst2 vst3 au clap
    UI_TYPE opengl
    FILES_DSP src/plugin/SQ8LPlugin.cpp
    FILES_UI src/plugin/SQ8LUI.cpp
             $<$<PLATFORM_ID:Darwin>:${CMAKE_SOURCE_DIR}/src/plugin/mac/PlatformUiMac.mm>
             $<$<PLATFORM_ID:Windows>:${CMAKE_SOURCE_DIR}/src/plugin/win/PlatformUiWin.cpp>)
  target_include_directories(sq8l PUBLIC src/plugin)
  target_link_libraries(sq8l PUBLIC sq8l_engine)
  target_link_libraries(sq8l-dsp PUBLIC sq8l_engine)
  target_link_libraries(sq8l-ui PUBLIC sq8l_gui sq8l_gui_logic sq8l_engine)
  if(WIN32)
    target_link_libraries(sq8l-ui PUBLIC comdlg32 gdi32 user32)
  endif()
  if(APPLE)
    set_source_files_properties(src/plugin/mac/PlatformUiMac.mm PROPERTIES COMPILE_FLAGS -fobjc-arc)
    target_link_libraries(sq8l-ui PUBLIC "-framework Cocoa" "-framework UniformTypeIdentifiers")
  endif()
  target_include_directories(sq8l-ui PUBLIC src/plugin src/gui)
  target_compile_options(sq8l-dsp PRIVATE ${SQ8L_FP_FLAGS})
endif()

# Native full-render checker (tests/render_check.cpp), see tests/export_golden_raw.py.
add_executable(sq8l_render_check tests/render_check.cpp)
target_link_libraries(sq8l_render_check PRIVATE sq8l_engine)
target_compile_options(sq8l_render_check PRIVATE ${SQ8L_FP_FLAGS})
