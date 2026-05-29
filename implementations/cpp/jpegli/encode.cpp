#include <stdexcept>
#include <vector>

#include "benchmark_harness.hpp"
// jpegli's C API mirrors libjpeg but with jpegli_* names; the jpeg_*_struct
// types come from jpegli's bundled <jpeglib.h> (pulled in transitively).
#include "lib/jpegli/encode.h"

class JpegliEncodeBench : public BenchmarkImplementation {
 public:
  std::string name() const override { return "jpegli-encode"; }

  void prepare(const Args &args) override {
    RGBImage img = decode_ppm_rgb8(args.input);
    width = img.width;
    height = img.height;
    input_data = std::move(img.data);

    // Mirror the quality tiers used by libjpeg-turbo and mozjpeg so jpegli is
    // directly comparable within the JPEG group.
    if (args.quality == "web-low") {
      quality = 50;
      progressive = false;
      use_444 = false;
    } else if (args.quality == "web-high") {
      quality = 80;
      progressive = true;
      use_444 = false;
    } else {  // archival
      quality = 95;
      progressive = false;
      use_444 = true;
    }
  }

  std::vector<uint8_t> run(const Args &args) override {
    jpeg_compress_struct cinfo;
    jpeg_error_mgr jerr;

    cinfo.err = jpegli_std_error(&jerr);
    jpegli_create_compress(&cinfo);

    unsigned char *outbuffer = nullptr;
    unsigned long outsize = 0;

    jpegli_mem_dest(&cinfo, &outbuffer, &outsize);

    cinfo.image_width = width;
    cinfo.image_height = height;
    cinfo.input_components = 3;
    cinfo.in_color_space = JCS_RGB;

    jpegli_set_defaults(&cinfo);
    jpegli_set_quality(&cinfo, quality, TRUE);

    if (progressive) {
      jpegli_simple_progression(&cinfo);
    }

    // Set subsampling for archival (4:4:4)
    if (use_444) {
      cinfo.comp_info[0].h_samp_factor = 1;
      cinfo.comp_info[0].v_samp_factor = 1;
      cinfo.comp_info[1].h_samp_factor = 1;
      cinfo.comp_info[1].v_samp_factor = 1;
      cinfo.comp_info[2].h_samp_factor = 1;
      cinfo.comp_info[2].v_samp_factor = 1;
    }

    jpegli_start_compress(&cinfo, TRUE);

    int row_stride = width * 3;
    while (cinfo.next_scanline < cinfo.image_height) {
      uint8_t *row_pointer = const_cast<uint8_t *>(
          input_data.data() + cinfo.next_scanline * row_stride);
      jpegli_write_scanlines(&cinfo, &row_pointer, 1);
    }

    jpegli_finish_compress(&cinfo);

    std::vector<uint8_t> output(outbuffer, outbuffer + outsize);

    if (outbuffer) {
      free(outbuffer);
    }

    jpegli_destroy_compress(&cinfo);

    return output;
  }

 private:
  std::vector<uint8_t> input_data;
  int width;
  int height;
  int quality;
  bool progressive;
  bool use_444;
};

int main(int argc, char **argv) {
  JpegliEncodeBench bench;
  return run_benchmark(argc, argv, bench);
}
