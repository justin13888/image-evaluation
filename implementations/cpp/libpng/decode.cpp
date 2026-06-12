#include <png.h>

#include <cstring>
#include <stdexcept>
#include <vector>

#include "benchmark_harness.hpp"

class LibPngBench : public BenchmarkImplementation {
 public:
  std::string name() const override { return "libpng-decode"; }

  void prepare(const Args &args) override {
    input_data = read_binary_file(args.input);
  }

  struct MemReader {
    const uint8_t *data;
    size_t size;
    size_t offset;
  };

  static void read_data(png_structp png_ptr, png_bytep data,
                        png_size_t length) {
    MemReader *reader = static_cast<MemReader *>(png_get_io_ptr(png_ptr));
    if (reader->offset + length > reader->size) {
      png_error(png_ptr, "Read error");
    }
    memcpy(data, reader->data + reader->offset, length);
    reader->offset += length;
  }

  std::vector<uint8_t> run(const Args &args) override {
    return decode(input_data);
  }

 private:
  std::vector<uint8_t> decode(const std::vector<uint8_t> &data) {
    png_structp png_ptr =
        png_create_read_struct(PNG_LIBPNG_VER_STRING, NULL, NULL, NULL);
    if (!png_ptr) throw std::runtime_error("png_create_read_struct failed");

    png_infop info_ptr = png_create_info_struct(png_ptr);
    if (!info_ptr) {
      png_destroy_read_struct(&png_ptr, NULL, NULL);
      throw std::runtime_error("png_create_info_struct failed");
    }

    if (setjmp(png_jmpbuf(png_ptr))) {
      png_destroy_read_struct(&png_ptr, &info_ptr, NULL);
      throw std::runtime_error("libpng error");
    }

    MemReader reader = {data.data(), data.size(), 0};
    png_set_read_fn(png_ptr, &reader, read_data);

    png_read_info(png_ptr, info_ptr);

    int width = png_get_image_width(png_ptr, info_ptr);
    int height = png_get_image_height(png_ptr, info_ptr);
    png_byte color_type = png_get_color_type(png_ptr, info_ptr);
    png_byte bit_depth = png_get_bit_depth(png_ptr, info_ptr);

    // Expand palette (indexed) images to RGB. Encoders under test may legitimately
    // emit a palette PNG (e.g. zune-png/zenpng picks an indexed colortype when the
    // image has <=256 colours); without this libpng returns 1 byte/pixel (the
    // palette index) and encode_ppm_rgb8() sees a third of the RGB bytes it expects.
    if (color_type == PNG_COLOR_TYPE_PALETTE) png_set_palette_to_rgb(png_ptr);

    // Expand low-bit-depth grayscale images to 8 bits
    if (color_type == PNG_COLOR_TYPE_GRAY && bit_depth < 8)
      png_set_expand_gray_1_2_4_to_8(png_ptr);

    // Transform transparency to alpha
    if (png_get_valid(png_ptr, info_ptr, PNG_INFO_tRNS))
      png_set_tRNS_to_alpha(png_ptr);

    // Convert 16-bit to 8-bit (benchmarking standard 8-bit mostly)
    if (bit_depth == 16) png_set_strip_16(png_ptr);

    // Convert grayscale to RGB
    if (color_type == PNG_COLOR_TYPE_GRAY ||
        color_type == PNG_COLOR_TYPE_GRAY_ALPHA)
      png_set_gray_to_rgb(png_ptr);

    // Strip alpha channel so output is always RGB (not RGBA).
    // Required for PNGs with alpha (e.g. alpha_gradient_4k.png) — without this,
    // png_get_rowbytes() returns width*4 but encode_ppm_rgb8() expects width*3.
    png_set_strip_alpha(png_ptr);

    png_read_update_info(png_ptr, info_ptr);

    // Allocate output buffer
    size_t rowbytes = png_get_rowbytes(png_ptr, info_ptr);
    std::vector<uint8_t> output(rowbytes * height);
    std::vector<png_bytep> row_pointers(height);
    for (int y = 0; y < height; y++) {
      row_pointers[y] = output.data() + y * rowbytes;
    }

    png_read_image(png_ptr, row_pointers.data());
    png_read_end(png_ptr, NULL);

    png_destroy_read_struct(&png_ptr, &info_ptr, NULL);

    return encode_ppm_rgb8(width, height, output);
  }

  std::vector<uint8_t> input_data;
};

int main(int argc, char **argv) {
  LibPngBench bench;
  return run_benchmark(argc, argv, bench);
}
