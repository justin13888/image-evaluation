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

# XYB mode (jpegli encode) and the XYB-aware scoring decoder go through
# jxl::extras::EncodeJpeg / DecodeJpeg + the CMS, which live in libjxl's
# jxl_extras-internal archive and its jxl / jxl_cms dependencies. As static
# archives these carry all the internal jxl symbols the extras code needs;
# vendor/build_vendor.py stages jxl_extras-internal alongside the others.
find_library(JXL_EXTRAS_LIB NAMES jxl_extras-internal
             PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib64 lib
             NO_DEFAULT_PATH REQUIRED)
find_library(JXL_LIB NAMES jxl
             PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib64 lib
             NO_DEFAULT_PATH REQUIRED)
find_library(JXL_CMS_LIB NAMES jxl_cms
             PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib64 lib
             NO_DEFAULT_PATH REQUIRED)
find_library(JXL_THREADS_LIB NAMES jxl_threads
             PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib64 lib
             NO_DEFAULT_PATH REQUIRED)
find_library(BROTLI_ENC_LIB NAMES brotlienc
             PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib64 lib
             NO_DEFAULT_PATH REQUIRED)
find_library(BROTLI_DEC_LIB NAMES brotlidec
             PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib64 lib
             NO_DEFAULT_PATH REQUIRED)
find_library(BROTLI_COMMON_LIB NAMES brotlicommon
             PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib64 lib
             NO_DEFAULT_PATH REQUIRED)

# The internal jxl static archives cross-reference each other, so wrap them in a
# linker group. ${JPEGLI_LIB}/${HWY_LIB} follow for correct static link order.
set(JXL_EXTRAS_LINK
  -Wl,--start-group
  ${JXL_EXTRAS_LIB} ${JXL_LIB} ${JXL_CMS_LIB} ${JXL_THREADS_LIB}
  ${JPEGLI_LIB} ${HWY_LIB}
  ${BROTLI_ENC_LIB} ${BROTLI_DEC_LIB} ${BROTLI_COMMON_LIB}
  -Wl,--end-group)

# Encoder binary. The classic YCbCr / distance / chroma paths use jpegli's raw C
# API; the XYB variant uses jxl::extras::EncodeJpeg, hence the extras link.
add_executable(bench-jpegli-encode encode.cpp)
target_compile_definitions(bench-jpegli-encode PRIVATE JXL_STATIC_DEFINE)
target_include_directories(bench-jpegli-encode PRIVATE
  ${LIBJXL_SRC} ${JPEGLI_GEN_INCLUDE} ${CMAKE_PREFIX_PATH}/include)
target_link_libraries(bench-jpegli-encode ${JXL_EXTRAS_LINK}
  Threads::Threads ZLIB::ZLIB "$<LINK_LIBRARY:WHOLE_ARCHIVE,mimalloc-static>")

# XYB-aware scoring decoder (jxl::extras::DecodeJpeg + CMS -> sRGB). Only used to
# score XYB jpegli/zenjpeg outputs; a plain libjpeg decode would mis-colour them.
add_executable(bench-jpegli-decode decode.cpp)
target_compile_definitions(bench-jpegli-decode PRIVATE JXL_STATIC_DEFINE)
target_include_directories(bench-jpegli-decode PRIVATE
  ${LIBJXL_SRC} ${JPEGLI_GEN_INCLUDE} ${CMAKE_PREFIX_PATH}/include)
target_link_libraries(bench-jpegli-decode ${JXL_EXTRAS_LINK}
  Threads::Threads ZLIB::ZLIB "$<LINK_LIBRARY:WHOLE_ARCHIVE,mimalloc-static>")
