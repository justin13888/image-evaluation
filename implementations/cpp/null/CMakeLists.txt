cmake_minimum_required(VERSION 3.24)
project(bench-null)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Aggressive optimization flags per README
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -fstrict-aliasing -fomit-frame-pointer -march=native -DNDEBUG")
set(CMAKE_BUILD_TYPE Release)

include_directories(../../../harness/cpp)

find_package(ZLIB REQUIRED)
find_package(mimalloc REQUIRED)

# Decoder binary
add_executable(bench-null-decode decode.cpp)
target_link_libraries(bench-null-decode ZLIB::ZLIB "$<LINK_LIBRARY:WHOLE_ARCHIVE,mimalloc-static>")

# Encoder binary
add_executable(bench-null-encode encode.cpp)
target_link_libraries(bench-null-encode ZLIB::ZLIB "$<LINK_LIBRARY:WHOLE_ARCHIVE,mimalloc-static>")
