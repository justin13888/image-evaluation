cmake_minimum_required(VERSION 3.24)
project(bench-rav1d)

set(CMAKE_CXX_STANDARD 17)

# Aggressive optimization flags per README
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -fstrict-aliasing -fomit-frame-pointer -march=native -DNDEBUG")
set(CMAKE_BUILD_TYPE Release)

include_directories(../../../harness/cpp)

# rav1d is the Rust port of dav1d, built as a static lib exporting the dav1d_*
# C ABI. Find the VENDORED archive explicitly.
find_library(RAV1D_LIB NAMES rav1d
             PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib64 lib NO_DEFAULT_PATH REQUIRED)

# Use the VENDORED static libavif via pkg-config (not find_package, which would
# resolve a system/Homebrew libavif). Link the full codec set EXCEPT dav1d:
# rav1d supplies the dav1d_* symbols that libavif's codec_dav1d.o references.
find_package(PkgConfig REQUIRED)
pkg_check_modules(AVIF REQUIRED IMPORTED_TARGET libavif)
pkg_check_modules(AOM REQUIRED IMPORTED_TARGET aom)
pkg_check_modules(SVT_AV1 REQUIRED IMPORTED_TARGET SvtAv1Enc)
pkg_check_modules(LIBGAV1 REQUIRED IMPORTED_TARGET libgav1)
find_package(ZLIB REQUIRED)
find_package(mimalloc REQUIRED)

# Decoder binary. WHOLE_ARCHIVE forces every rav1d object (including all dav1d_*
# definitions) into the link up front, so libavif's codec_dav1d.o references
# resolve to rav1d rather than the real dav1d (which is not linked here).
# Without it, ld would drop librav1d.a before libavif demands those symbols.
add_executable(bench-rav1d-decode decode.cpp)
target_link_libraries(bench-rav1d-decode
    "$<LINK_LIBRARY:WHOLE_ARCHIVE,${RAV1D_LIB}>"
    PkgConfig::AVIF PkgConfig::AOM PkgConfig::SVT_AV1 PkgConfig::LIBGAV1
    ZLIB::ZLIB "$<LINK_LIBRARY:WHOLE_ARCHIVE,mimalloc-static>" pthread dl m)
