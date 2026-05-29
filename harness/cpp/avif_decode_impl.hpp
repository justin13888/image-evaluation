// Shared AVIF decode base for libavif backends (aom, dav1d, rav1d, libgav1).
// The codec backend is selected via the constructor parameter.
#pragma once

#include <avif/avif.h>

#include <cstring>
#include <stdexcept>
#include <vector>

#include "benchmark_harness.hpp"

class AvifDecodeBenchBase : public BenchmarkImplementation {
 public:
  explicit AvifDecodeBenchBase(avifCodecChoice codec) : codec_(codec) {}

  void prepare(const Args &args) override {
    input_data = read_binary_file(args.input);
    threads = args.threads;
  }

  std::vector<uint8_t> run(const Args &args) override {
    avifDecoder *decoder = avifDecoderCreate();
    if (!decoder) throw std::runtime_error("avifDecoderCreate failed");

    decoder->codecChoice = codec_;
    if (threads > 0) {
      decoder->maxThreads = threads;
    }

    struct DecoderGuard {
      avifDecoder *d;
      ~DecoderGuard() { avifDecoderDestroy(d); }
    } guard{decoder};

    avifResult result =
        avifDecoderSetIOMemory(decoder, input_data.data(), input_data.size());
    if (result != AVIF_RESULT_OK) {
      throw std::runtime_error("avifDecoderSetIOMemory failed");
    }

    result = avifDecoderParse(decoder);
    if (result != AVIF_RESULT_OK) {
      throw std::runtime_error("avifDecoderParse failed");
    }

    result = avifDecoderNextImage(decoder);
    if (result != AVIF_RESULT_OK) {
      throw std::runtime_error("avifDecoderNextImage failed");
    }

    avifRGBImage rgb;
    avifRGBImageSetDefaults(&rgb, decoder->image);
    rgb.format = AVIF_RGB_FORMAT_RGB;
    rgb.depth = 8;

    avifRGBImageAllocatePixels(&rgb);
    result = avifImageYUVToRGB(decoder->image, &rgb);
    if (result != AVIF_RESULT_OK) {
      avifRGBImageFreePixels(&rgb);
      throw std::runtime_error("avifImageYUVToRGB failed");
    }

    std::vector<uint8_t> rgb_data;
    rgb_data.reserve(rgb.width * rgb.height * 3);
    for (uint32_t y = 0; y < rgb.height; ++y) {
      uint8_t *row = rgb.pixels + (y * rgb.rowBytes);
      rgb_data.insert(rgb_data.end(), row, row + (rgb.width * 3));
    }

    avifRGBImageFreePixels(&rgb);

    return encode_ppm_rgb8(rgb.width, rgb.height, rgb_data);
  }

 private:
  avifCodecChoice codec_;
  std::vector<uint8_t> input_data;
  int threads = 0;
};
