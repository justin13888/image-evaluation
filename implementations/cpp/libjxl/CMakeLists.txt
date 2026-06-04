cmake_minimum_required(VERSION 3.24)
project(bench-libjxl)

set(CMAKE_CXX_STANDARD 17)

# Aggressive optimization flags per README
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -fstrict-aliasing -fomit-frame-pointer -march=native -DNDEBUG")
set(CMAKE_BUILD_TYPE Release)

include_directories(../../../harness/cpp)

find_package(PkgConfig REQUIRED)
pkg_check_modules(JXL REQUIRED IMPORTED_TARGET libjxl)
pkg_check_modules(JXL_THREADS REQUIRED IMPORTED_TARGET libjxl_threads)
find_package(ZLIB REQUIRED)
find_package(mimalloc REQUIRED)
# libbrotlienc/libbrotlidec (pulled in by libjxl) depend on libbrotlicommon but
# only list it under Requires.private, which pkg_check_modules doesn't follow.
# CMake's link-order algorithm places plain-path items before the imported
# targets' transitive deps (brotlienc/brotlidec), so brotlicommon ends up too
# early in the link command and its symbols are unseen when needed.
# Fix: WHOLE_ARCHIVE forces all brotlicommon symbols into the linker's table
# unconditionally, so they are available when brotlienc/brotlidec pull them in.
find_library(BROTLICOMMON_LIBRARY NAMES brotlicommon
             PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib64 lib
             NO_DEFAULT_PATH REQUIRED)

# Decoder binary
add_executable(bench-libjxl-decode decode.cpp)
target_link_libraries(bench-libjxl-decode PkgConfig::JXL PkgConfig::JXL_THREADS ZLIB::ZLIB "$<LINK_LIBRARY:WHOLE_ARCHIVE,${BROTLICOMMON_LIBRARY}>" "$<LINK_LIBRARY:WHOLE_ARCHIVE,mimalloc-static>")

# Encoder binary
add_executable(bench-libjxl-encode encode.cpp)
target_link_libraries(bench-libjxl-encode PkgConfig::JXL PkgConfig::JXL_THREADS ZLIB::ZLIB "$<LINK_LIBRARY:WHOLE_ARCHIVE,${BROTLICOMMON_LIBRARY}>" "$<LINK_LIBRARY:WHOLE_ARCHIVE,mimalloc-static>")
