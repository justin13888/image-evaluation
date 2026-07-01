#include <cstring>
#include <stdexcept>
#include <vector>

#include "benchmark_harness.hpp"
// jpegli's C API mirrors libjpeg but with jpegli_* names; the jpeg_*_struct
// types come from jpegli's bundled <jpeglib.h> (pulled in transitively).
#include "lib/jpegli/encode.h"
// XYB mode needs the sRGB->linear color transform and the XYB ICC profile,
// which live in libjxl's color-management library, not in jpegli-static.
// jxl::extras:: EncodeJpeg (the exact path cjpegli --xyb uses) wraps all of it,
// so the XYB variant routes through it instead of the raw C API below.
#include <jxl/encode.h>  // JxlColorEncodingSetToSRGB

#include "lib/extras/enc/jpegli.h"
#include "lib/extras/packed_image.h"

class JpegliEncodeBench : public BenchmarkImplementation {
 public:
  std::string name() const override { return "jpegli-encode"; }

  void prepare(const Args &args) override {
    RGBImage img = decode_ppm_rgb8(args.input);
    width = img.width;
    height = img.height;
    input_data = std::move(img.data);

    // Tunables mirror the other JPEG encoders so jpegli is directly comparable
    // within the group: quality (1-100), progressive scan, chroma subsampling.
    // Two jpegli-specific knobs expose its real differentiators as variants:
    //   quality_control=distance -> native butteraugli distance quantization
    //                               (vs the libjpeg-style integer quality path)
    //   color=xyb                -> XYB perceptual colorspace
    quality = param_int(args, "quality", 80);
    progressive = param_bool(args, "progressive", true);
    subsampling = param_str(args, "subsampling", "420");
    use_distance =
        (param_str(args, "quality_control", "quality") == "distance");
    use_xyb = (param_str(args, "color", "ycbcr") == "xyb");
  }

  std::vector<uint8_t> run(const Args &args) override {
    if (use_xyb) {
      return run_xyb();
    }
    return run_ycbcr();
  }

 private:
  // The classic path: raw jpegli C API, libjpeg-compatible. quality_control and
  // full 4:4:4/4:4:0/4:2:2/4:2:0 chroma are honoured here; XYB is not (it needs
  // the color-management stack and goes through run_xyb()).
  std::vector<uint8_t> run_ycbcr() {
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

    if (use_distance) {
      // jpegli's native quality control: map the swept quality onto a
      // butteraugli distance and quantize to it (its DC/AC-split perceptual
      // scaling), rather than libjpeg-style integer-quality table scaling.
      jpegli_set_distance(&cinfo, jpegli_quality_to_distance(quality), TRUE);
    } else {
      jpegli_set_quality(&cinfo, quality, TRUE);
    }

    if (progressive) {
      jpegli_simple_progression(&cinfo);
    }

    // Chroma subsampling via luma sample factors (chroma comps stay 1x1):
    // 4:4:4 -> (1,1), 4:4:0 -> (1,2), 4:2:2 -> (2,1), 4:2:0 -> (2,2).
    int h0 = 2, v0 = 2;
    if (subsampling == "444") {
      h0 = 1;
      v0 = 1;
    } else if (subsampling == "440") {
      h0 = 1;
      v0 = 2;
    } else if (subsampling == "422") {
      h0 = 2;
      v0 = 1;
    } else if (subsampling == "420") {
      h0 = 2;
      v0 = 2;
    } else {
      throw std::runtime_error("Unknown subsampling: " + subsampling);
    }
    cinfo.comp_info[0].h_samp_factor = h0;
    cinfo.comp_info[0].v_samp_factor = v0;
    for (int i = 1; i < cinfo.num_components; ++i) {
      cinfo.comp_info[i].h_samp_factor = 1;
      cinfo.comp_info[i].v_samp_factor = 1;
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

  // XYB perceptual colorspace via jxl::extras::EncodeJpeg. Feeds the 8-bit sRGB
  // input as a PackedPixelFile; EncodeJpeg converts sRGB->linear->XYB,
  // quantizes to the distance derived from `quality`, and embeds the APP2 XYB
  // ICC profile so an XYB-aware decoder (bench-jpegli-decode) can recover sRGB.
  std::vector<uint8_t> run_xyb() {
    jxl::extras::PackedPixelFile ppf;
    ppf.info.xsize = static_cast<uint32_t>(width);
    ppf.info.ysize = static_cast<uint32_t>(height);
    ppf.info.bits_per_sample = 8;
    ppf.info.exponent_bits_per_sample = 0;
    ppf.info.num_color_channels = 3;
    ppf.info.alpha_bits = 0;
    JxlColorEncodingSetToSRGB(&ppf.color_encoding, /*is_gray=*/JXL_FALSE);
    ppf.primary_color_representation =
        jxl::extras::PackedPixelFile::kColorEncodingIsPrimary;

    JxlPixelFormat format = {/*num_channels=*/3, JXL_TYPE_UINT8,
                             JXL_NATIVE_ENDIAN, /*align=*/0};
    auto image_or = jxl::extras::PackedImage::Create(width, height, format);
    if (!image_or.ok()) {
      throw std::runtime_error("jpegli xyb: PackedImage::Create failed");
    }
    jxl::extras::PackedImage image = std::move(image_or).value_();
    std::memcpy(image.pixels(), input_data.data(), input_data.size());
    ppf.frames.emplace_back(std::move(image));

    jxl::extras::JpegSettings settings;
    settings.xyb = true;
    // quality>0 makes EncodeJpeg pick distance = jpegli_quality_to_distance(q),
    // so the XYB curve is swept over the same quality axis as the other series.
    settings.quality = static_cast<float>(quality);
    settings.progressive_level = progressive ? 2 : 0;
    settings.use_adaptive_quantization = true;
    // Leave chroma_subsampling empty: XYB uses jpegli's recommended default
    // (B channel subsampled), matching zenjpeg's XybSubsampling::BQuarter.

    std::vector<uint8_t> compressed;
    if (!jxl::extras::EncodeJpeg(ppf, settings, nullptr, &compressed)) {
      throw std::runtime_error("jpegli xyb: EncodeJpeg failed");
    }
    return compressed;
  }

  std::vector<uint8_t> input_data;
  int width;
  int height;
  int quality;
  bool progressive;
  std::string subsampling;
  bool use_distance;
  bool use_xyb;
};

int main(int argc, char **argv) {
  JpegliEncodeBench bench;
  return run_benchmark(argc, argv, bench);
}
