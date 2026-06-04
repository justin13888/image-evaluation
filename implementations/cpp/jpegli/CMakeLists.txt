cmake_minimum_required(VERSION 3.24)
project(bench-jpegli)

set(CMAKE_CXX_STANDARD 17)

# Aggressive optimization flags per README
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -fstrict-aliasing -fomit-frame-pointer -march=native -DNDEBUG")
set(CMAKE_BUILD_TYPE Release)

include_directories(../../../harness/cpp)

# jpegli ships inside the libjxl source tree. Its public API headers
# (lib/jpegli/encode.h -> lib/jpegli/common.h -> lib/jxl/base/include_jpeglib.h)
# use project-root-relative quote includes, so the libjxl source root must be on
# the include path. The jpegli static lib and its generated <jpeglib.h> are
# staged into the vendor install prefix by vendor/build_vendor.py (libjxl has no
# install rule for jpegli-static).
get_filename_component(REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../.." ABSOLUTE)
set(LIBJXL_SRC "${REPO_ROOT}/vendor/libjxl")

find_library(JPEGLI_LIB NAMES jpegli-static
             PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib64 lib
             NO_DEFAULT_PATH REQUIRED)
find_library(HWY_LIB NAMES hwy
             PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib64 lib
             NO_DEFAULT_PATH REQUIRED)
find_path(JPEGLI_GEN_INCLUDE jpeglib.h
          PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES include/jpegli
          NO_DEFAULT_PATH REQUIRED)
find_package(ZLIB REQUIRED)
find_package(mimalloc REQUIRED)
find_package(Threads REQUIRED)

# Encoder binary (encode-only: jpegli's headline feature is its encoder).
# ${HWY_LIB} follows ${JPEGLI_LIB} for correct static link order.
add_executable(bench-jpegli-encode encode.cpp)
target_include_directories(bench-jpegli-encode PRIVATE ${LIBJXL_SRC} ${JPEGLI_GEN_INCLUDE})
target_link_libraries(bench-jpegli-encode ${JPEGLI_LIB} ${HWY_LIB} Threads::Threads ZLIB::ZLIB mimalloc-static)
