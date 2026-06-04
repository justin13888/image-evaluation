#include <png.h>
#include <zlib.h>

#include <cstring>
#include <stdexcept>
#include <vector>

#include "benchmark_harness.hpp"

class LibPngEncodeBench : public BenchmarkImplementation {
 public:
  std::string name() const override { return "libpng-encode"; }

  void prepare(const Args &args) override {
    RGBImage img = decode_ppm_rgb8(args.input);
    width = img.width;
    height = img.height;
    input_data = std::move(img.data);

    // PNG is lossless, compression level varies by quality tier
    const std::string tier = param_str(args, "quality-tier", "web-high");
    if (tier == "web-low") {
      compression_level = Z_BEST_SPEED;
    } else if (tier == "web-high") {
      compression_level = Z_DEFAULT_COMPRESSION;
    } else {  // archival
      compression_level = Z_BEST_COMPRESSION;
    }
  }

  std::vector<uint8_t> run(const Args &args) override {
    std::vector<uint8_t> output;

    png_structp png_ptr = png_create_write_struct(PNG_LIBPNG_VER_STRING,
                                                  nullptr, nullptr, nullptr);
    if (!png_ptr) throw std::runtime_error("png_create_write_struct failed");

    png_infop info_ptr = png_create_info_struct(png_ptr);
    if (!info_ptr) {
      png_destroy_write_struct(&png_ptr, nullptr);
      throw std::runtime_error("png_create_info_struct failed");
    }

    if (setjmp(png_jmpbuf(png_ptr))) {
      png_destroy_write_struct(&png_ptr, &info_ptr);
      throw std::runtime_error("PNG encoding error");
    }

    // Custom write function
    png_set_write_fn(
        png_ptr, &output,
        [](png_structp png_ptr, png_bytep data, png_size_t length) {
          auto *vec =
              static_cast<std::vector<uint8_t> *>(png_get_io_ptr(png_ptr));
          vec->insert(vec->end(), data, data + length);
        },
        [](png_structp) {});

    png_set_IHDR(png_ptr, info_ptr, width, height, 8, PNG_COLOR_TYPE_RGB,
                 PNG_INTERLACE_NONE, PNG_COMPRESSION_TYPE_DEFAULT,
                 PNG_FILTER_TYPE_DEFAULT);

    png_set_compression_level(png_ptr, compression_level);

    png_write_info(png_ptr, info_ptr);

    for (int y = 0; y < height; y++) {
      png_write_row(png_ptr,
                    const_cast<uint8_t *>(input_data.data() + y * width * 3));
    }

    png_write_end(png_ptr, nullptr);
    png_destroy_write_struct(&png_ptr, &info_ptr);

    return output;
  }

 private:
  std::vector<uint8_t> input_data;
  int width;
  int height;
  int compression_level;
};

int main(int argc, char **argv) {
  LibPngEncodeBench bench;
  return run_benchmark(argc, argv, bench);
}
