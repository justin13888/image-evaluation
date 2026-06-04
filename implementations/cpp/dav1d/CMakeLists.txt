cmake_minimum_required(VERSION 3.24)
project(bench-dav1d)

set(CMAKE_CXX_STANDARD 17)

# Aggressive optimization flags per README
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -fstrict-aliasing -fomit-frame-pointer -march=native -DNDEBUG")
set(CMAKE_BUILD_TYPE Release)

include_directories(../../../harness/cpp)

# Use the VENDORED static libavif via pkg-config (not find_package, which would
# resolve a system/Homebrew libavif). libavif's codec registry pulls in every
# compiled-in codec object, so link the full vendored codec set explicitly.
find_package(PkgConfig REQUIRED)
pkg_check_modules(AVIF REQUIRED IMPORTED_TARGET libavif)
pkg_check_modules(AOM REQUIRED IMPORTED_TARGET aom)
pkg_check_modules(DAV1D REQUIRED IMPORTED_TARGET dav1d)
pkg_check_modules(SVT_AV1 REQUIRED IMPORTED_TARGET SvtAv1Enc)
pkg_check_modules(LIBGAV1 REQUIRED IMPORTED_TARGET libgav1)
find_package(ZLIB REQUIRED)
find_package(mimalloc REQUIRED)

# Decoder binary
add_executable(bench-dav1d-decode decode.cpp)
target_link_libraries(bench-dav1d-decode
    PkgConfig::AVIF PkgConfig::AOM PkgConfig::DAV1D PkgConfig::SVT_AV1 PkgConfig::LIBGAV1
    ZLIB::ZLIB mimalloc-static pthread dl m)
