cmake_minimum_required(VERSION 3.24)
project(bench-libavif)

set(CMAKE_CXX_STANDARD 17)

# Aggressive optimization flags per README
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -fstrict-aliasing -fomit-frame-pointer -march=native -DNDEBUG")
set(CMAKE_BUILD_TYPE Release)

include_directories(../../../harness/cpp)

# Use the VENDORED static libavif via pkg-config. Do NOT use
# find_package(libavif): the vendored static build installs no CMake config
# (libavif only exports one for shared/VCPKG builds), so find_package would
# silently resolve a system/Homebrew libavif instead. The vendored libavif.a
# is built with the aom, dav1d, SVT-AV1 and libgav1 codecs, and its codec
# registry references all of them, so every AVIF harness must link the full
# codec set explicitly (non-static pkg-config doesn't follow Requires.private).
find_package(PkgConfig REQUIRED)
pkg_check_modules(AVIF REQUIRED IMPORTED_TARGET libavif)
pkg_check_modules(AOM REQUIRED IMPORTED_TARGET aom)
pkg_check_modules(DAV1D REQUIRED IMPORTED_TARGET dav1d)
pkg_check_modules(SVT_AV1 REQUIRED IMPORTED_TARGET SvtAv1Enc)
pkg_check_modules(LIBGAV1 REQUIRED IMPORTED_TARGET libgav1)
find_package(ZLIB REQUIRED)
find_package(mimalloc REQUIRED)

set(AVIF_LINK_LIBS
    PkgConfig::AVIF PkgConfig::AOM PkgConfig::DAV1D PkgConfig::SVT_AV1 PkgConfig::LIBGAV1
    ZLIB::ZLIB "$<LINK_LIBRARY:WHOLE_ARCHIVE,mimalloc-static>" pthread dl m)

# Decoder binary
add_executable(bench-libavif-decode decode.cpp)
target_link_libraries(bench-libavif-decode ${AVIF_LINK_LIBS})

# Encoder binary
add_executable(bench-libavif-encode encode.cpp)
target_link_libraries(bench-libavif-encode ${AVIF_LINK_LIBS})
