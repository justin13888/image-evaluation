#include <webp/encode.h>

#include <stdexcept>
#include <vector>

#include "benchmark_harness.hpp"

class LibWebpEncodeBench : public BenchmarkImplementation {
 public:
  std::string name() const override { return "libwebp-encode"; }

  void prepare(const Args &args) override {
    RGBImage img = decode_ppm_rgb8(args.input);
    width = img.width;
    height = img.height;
    input_data = std::move(img.data);

    // Tunables: quality (0-100), method/effort (0-6), lossless flag.
    quality = static_cast<float>(param_double(args, "quality", 75.0));
    method = param_int(args, "method", 4);
    lossless = param_bool(args, "lossless", false);
  }

  std::vector<uint8_t> run(const Args &args) override {
    WebPConfig config;
    if (!WebPConfigInit(&config)) {
      throw std::runtime_error("WebPConfigInit failed");
    }

    if (lossless) {
      config.lossless = 1;
      config.method = method;
      config.quality = quality;
    } else {
      config.lossless = 0;
      config.quality = quality;
      config.method = method;
    }

    if (!WebPValidateConfig(&config)) {
      throw std::runtime_error("Invalid WebP config");
    }

    WebPPicture picture;
    if (!WebPPictureInit(&picture)) {
      throw std::runtime_error("WebPPictureInit failed");
    }

    picture.width = width;
    picture.height = height;
    picture.use_argb = lossless;

    if (!WebPPictureImportRGB(&picture, input_data.data(), width * 3)) {
      WebPPictureFree(&picture);
      throw std::runtime_error("WebPPictureImportRGB failed");
    }

    WebPMemoryWriter writer;
    WebPMemoryWriterInit(&writer);
    picture.writer = WebPMemoryWrite;
    picture.custom_ptr = &writer;

    if (!WebPEncode(&config, &picture)) {
      WebPPictureFree(&picture);
      WebPMemoryWriterClear(&writer);
      throw std::runtime_error("WebPEncode failed");
    }

    std::vector<uint8_t> output(writer.mem, writer.mem + writer.size);

    WebPPictureFree(&picture);
    WebPMemoryWriterClear(&writer);

    return output;
  }

 private:
  std::vector<uint8_t> input_data;
  int width;
  int height;
  float quality;
  int method;
  bool lossless;
};

int main(int argc, char **argv) {
  LibWebpEncodeBench bench;
  return run_benchmark(argc, argv, bench);
}
