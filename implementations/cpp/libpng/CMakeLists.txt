cmake_minimum_required(VERSION 3.24)
project(bench-libpng)

set(CMAKE_CXX_STANDARD 17)

# Aggressive optimization flags per README
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -fstrict-aliasing -fomit-frame-pointer -march=native -DNDEBUG")
# Disable PIE: libpng.a may lack PIC relocations; PIE also adds indirection overhead in benchmarks
add_link_options(-no-pie)
set(CMAKE_BUILD_TYPE Release)

include_directories(../../../harness/cpp)

find_package(PNG REQUIRED)
find_package(ZLIB REQUIRED)
find_package(mimalloc REQUIRED)

# Decoder binary
add_executable(bench-libpng-decode decode.cpp)
target_link_libraries(bench-libpng-decode PNG::PNG ZLIB::ZLIB "$<LINK_LIBRARY:WHOLE_ARCHIVE,mimalloc-static>")

# Encoder binary
add_executable(bench-libpng-encode encode.cpp)
target_link_libraries(bench-libpng-encode PNG::PNG ZLIB::ZLIB "$<LINK_LIBRARY:WHOLE_ARCHIVE,mimalloc-static>")
