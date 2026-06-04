cmake_minimum_required(VERSION 3.24)
project(bench-libjpeg-turbo)

set(CMAKE_CXX_STANDARD 17)

# Aggressive optimization flags per README
set(CMAKE_C_FLAGS_RELEASE "-O3 -march=native -DNDEBUG")
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -fstrict-aliasing -fomit-frame-pointer -march=native -DNDEBUG")
set(CMAKE_BUILD_TYPE Release)

include_directories(../../../harness/cpp)

include(FetchContent)

find_package(libjpeg-turbo REQUIRED)
find_package(ZLIB REQUIRED)
find_package(mimalloc REQUIRED)

# Decoder binary
add_executable(bench-libjpeg-turbo-decode decode.cpp)
target_link_libraries(bench-libjpeg-turbo-decode libjpeg-turbo::jpeg-static ZLIB::ZLIB mimalloc-static)

# Encoder binary
add_executable(bench-libjpeg-turbo-encode encode.cpp)
target_link_libraries(bench-libjpeg-turbo-encode libjpeg-turbo::jpeg-static ZLIB::ZLIB mimalloc-static)
