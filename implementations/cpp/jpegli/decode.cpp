#include <jxl/memory_manager.h>
#include <jxl/types.h>

#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <vector>

#include "benchmark_harness.hpp"
// jpegli's decoder (via jxl::extras::DecodeJpeg) is the only JPEG decoder here
// that can invert XYB: it reads the embedded XYB ICC and hands back the raw
// samples, then the CMS conversion below turns them into sRGB. A plain libjpeg
// decode of an XYB JPEG yields wrong colours, so XYB jpegli/zenjpeg outputs are
// routed to this decoder for metric scoring (see REFERENCE_DECODERS override).
#include "lib/extras/dec/jpegli.h"
#include "lib/extras/packed_image.h"
#include "lib/extras/packed_image_convert.h"
#include "lib/jxl/codec_in_out.h"
#include "lib/jxl/color_encoding_internal.h"

namespace {
void *BenchAlloc(void * /*opaque*/, size_t size) { return malloc(size); }
void BenchFree(void * /*opaque*/, void *address) { free(address); }
// A JxlMemoryManager backed by malloc/free, mirroring tools/no_memory_manager.
JxlMemoryManager g_memory_manager{nullptr, &BenchAlloc, &BenchFree};
}  // namespace

class JpegliDecodeBench : public BenchmarkImplementation {
 public:
  std::string name() const override { return "jpegli-decode"; }

  void prepare(const Args &args) override {
    input_data = read_binary_file(args.input);
  }

  std::vector<uint8_t> run(const Args &args) override {
    jxl::extras::PackedPixelFile ppf;
    jxl::extras::JpegDecompressParams dparams;
    dparams.output_data_type = JXL_TYPE_UINT8;
    dparams.force_rgb = true;
    if (!jxl::extras::DecodeJpeg(input_data, dparams, nullptr, &ppf)) {
      throw std::runtime_error("jpegli decode: DecodeJpeg failed");
    }

    // DecodeJpeg returns raw samples (XYB, for XYB JPEGs) plus the embedded
    // ICC; it does not colour-manage. Convert to 8-bit sRGB through the CMS so
    // the PPM handed to iqa-cli is correct sRGB regardless of the encode
    // colorspace. For an ordinary YCbCr JPEG the ICC is sRGB, so this is a
    // no-op transform.
    jxl::CodecInOut io{&g_memory_manager};
    if (!jxl::extras::ConvertPackedPixelFileToCodecInOut(ppf, nullptr, &io)) {
      throw std::runtime_error("jpegli decode: ppf -> CodecInOut failed");
    }

    JxlPixelFormat format = {/*num_channels=*/3, JXL_TYPE_UINT8,
                             JXL_NATIVE_ENDIAN, /*align=*/0};
    jxl::extras::PackedPixelFile srgb;
    if (!jxl::extras::ConvertCodecInOutToPackedPixelFile(
            io, format, jxl::ColorEncoding::SRGB(/*is_gray=*/false), nullptr,
            &srgb)) {
      throw std::runtime_error("jpegli decode: convert to sRGB failed");
    }

    const jxl::extras::PackedImage &color = srgb.frames.at(0).color;
    int w = static_cast<int>(color.xsize);
    int h = static_cast<int>(color.ysize);
    std::vector<uint8_t> rgb(color.pixels_size);
    std::memcpy(rgb.data(), color.pixels(), color.pixels_size);
    return encode_ppm_rgb8(w, h, rgb);
  }

 private:
  std::vector<uint8_t> input_data;
};

int main(int argc, char **argv) {
  JpegliDecodeBench bench;
  return run_benchmark(argc, argv, bench);
}
