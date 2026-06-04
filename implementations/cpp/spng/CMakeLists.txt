cmake_minimum_required(VERSION 3.24)
project(bench-spng)

set(CMAKE_CXX_STANDARD 17)

find_package(PkgConfig REQUIRED)

# Aggressive optimization flags per README
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -fstrict-aliasing -fomit-frame-pointer -march=native -DNDEBUG")
set(CMAKE_BUILD_TYPE Release)

include_directories(../../../harness/cpp)

find_package(ZLIB REQUIRED)
find_package(mimalloc REQUIRED)
pkg_check_modules(SPNG spng REQUIRED IMPORTED_TARGET)

# Decoder binary
add_executable(bench-spng-decode decode.cpp)
target_link_libraries(bench-spng-decode PkgConfig::SPNG ZLIB::ZLIB mimalloc-static)

# Encoder binary
add_executable(bench-spng-encode encode.cpp)
target_link_libraries(bench-spng-encode PkgConfig::SPNG ZLIB::ZLIB mimalloc-static)
