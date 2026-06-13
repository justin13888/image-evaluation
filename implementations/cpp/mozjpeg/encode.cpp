#include <stdexcept>
#include <vector>

#include "benchmark_harness.hpp"
#include "jpeg_wrapper.h"

class MozjpegEncodeBench : public BenchmarkImplementation {
 public:
  std::string name() const override { return "mozjpeg-encode"; }

  void prepare(const Args &args) override {
    RGBImage img = decode_ppm_rgb8(args.input);
    width = img.width;
    height = img.height;
    input_data = std::move(img.data);

    // Tunables: quality (1-100), progressive scan, chroma subsampling, and
    // mozjpeg's trellis quantization (on by default; issue #4 tests it off).
    quality = param_int(args, "quality", 80);
    progressive = param_bool(args, "progressive", true);
    use_444 = (param_str(args, "subsampling", "420") == "444");
    trellis = param_bool(args, "trellis", true);
  }

  std::vector<uint8_t> run(const Args &args) override {
    jpeg_compress_struct cinfo;
    jpeg_error_mgr jerr;

    cinfo.err = jpeg_std_error(&jerr);
    jpeg_create_compress(&cinfo);

    unsigned char *outbuffer = nullptr;
    unsigned long outsize = 0;

    jpeg_mem_dest(&cinfo, &outbuffer, &outsize);

    cinfo.image_width = width;
    cinfo.image_height = height;
    cinfo.input_components = 3;
    cinfo.in_color_space = JCS_RGB;

    jpeg_set_defaults(&cinfo);
    jpeg_set_quality(&cinfo, quality, TRUE);

    // Trellis quantization is a mozjpeg extension (on by default). Toggle both
    // the AC and DC trellis passes so trellis-off is a clean comparison point.
    jpeg_c_set_bool_param(&cinfo, JBOOLEAN_TRELLIS_QUANT,
                          trellis ? TRUE : FALSE);
    jpeg_c_set_bool_param(&cinfo, JBOOLEAN_TRELLIS_QUANT_DC,
                          trellis ? TRUE : FALSE);

    if (progressive) {
      jpeg_simple_progression(&cinfo);
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

    jpeg_start_compress(&cinfo, TRUE);

    int row_stride = width * 3;
    while (cinfo.next_scanline < cinfo.image_height) {
      uint8_t *row_pointer = const_cast<uint8_t *>(
          input_data.data() + cinfo.next_scanline * row_stride);
      jpeg_write_scanlines(&cinfo, &row_pointer, 1);
    }

    jpeg_finish_compress(&cinfo);

    std::vector<uint8_t> output(outbuffer, outbuffer + outsize);

    if (outbuffer) {
      free(outbuffer);
    }

    jpeg_destroy_compress(&cinfo);

    return output;
  }

 private:
  std::vector<uint8_t> input_data;
  int width;
  int height;
  int quality;
  bool progressive;
  bool use_444;
  bool trellis;
};

int main(int argc, char **argv) {
  MozjpegEncodeBench bench;
  return run_benchmark(argc, argv, bench);
}
