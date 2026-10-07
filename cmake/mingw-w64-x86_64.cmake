# Cross-compile for Windows x64 with MinGW-w64.
#   macOS (Homebrew mingw-w64):  cmake -S . -B build-win64 -G Ninja -DCMAKE_TOOLCHAIN_FILE=cmake/mingw-w64-x86_64.cmake
#   Debian/Ubuntu (apt mingw-w64): add -DMINGW_SUFFIX=-posix (the default win32 thread model lacks std::mutex)
set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)
set(MINGW_SUFFIX "" CACHE STRING "Compiler name suffix, e.g. -posix on Debian/Ubuntu")
set(CMAKE_C_COMPILER x86_64-w64-mingw32-gcc${MINGW_SUFFIX})
set(CMAKE_CXX_COMPILER x86_64-w64-mingw32-g++${MINGW_SUFFIX})
set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
# Self-contained DLLs: no libgcc / libstdc++ / winpthread dependencies on the target machine.
set(CMAKE_SHARED_LINKER_FLAGS_INIT "-static -static-libgcc -static-libstdc++")
set(CMAKE_MODULE_LINKER_FLAGS_INIT "-static -static-libgcc -static-libstdc++")
set(CMAKE_EXE_LINKER_FLAGS_INIT "-static -static-libgcc -static-libstdc++")
