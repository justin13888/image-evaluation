// Shared AVIF encode implementation for libavif with selectable codec backend.
// The codec backend is selected via the constructor parameter.
#pragma once

#include <avif/avif.h>

#include <stdexcept>
#include <vector>

#include "benchmark_harness.hpp"

class AvifEncodeBenchBase : public BenchmarkImplementation {
 public:
  explicit AvifEncodeBenchBase(avifCodecChoice codec) : codec_(codec) {}

  void prepare(const Args &args) override {
    RGBImage img = decode_ppm_rgb8(args.input);
    width = img.width;
    height = img.height;
    input_data = std::move(img.data);

    // Configure quality settings per README spec
    const std::string tier = param_str(args, "quality-tier", "web-high");
    if (tier == "web-low") {
      quality = 65;
      speed = 6;
      yuv_format = AVIF_PIXEL_FORMAT_YUV420;
    } else if (tier == "web-high") {
      quality = 65;
      speed = AVIF_SPEED_DEFAULT;
      yuv_format = AVIF_PIXEL_FORMAT_YUV420;
      // TODO: grain synthesis for web-high not yet implemented (see README)
    } else {  // archival
      quality = 85;
      speed = AVIF_SPEED_DEFAULT;
      yuv_format = AVIF_PIXEL_FORMAT_YUV444;
    }
  }

  std::vector<uint8_t> run(const Args &args) override {
    avifImage *image = avifImageCreate(width, height, 8, yuv_format);
    if (!image) {
      throw std::runtime_error("avifImageCreate failed");
    }

    // Convert RGB to YUV
    avifRGBImage rgb;
    avifRGBImageSetDefaults(&rgb, image);
    rgb.format = AVIF_RGB_FORMAT_RGB;
    rgb.pixels = const_cast<uint8_t *>(input_data.data());
    rgb.rowBytes = width * 3;

    if (avifImageRGBToYUV(image, &rgb) != AVIF_RESULT_OK) {
      avifImageDestroy(image);
      throw std::runtime_error("avifImageRGBToYUV failed");
    }

    avifEncoder *encoder = avifEncoderCreate();
    if (!encoder) {
      avifImageDestroy(image);
      throw std::runtime_error("avifEncoderCreate failed");
    }

    encoder->codecChoice = codec_;
    encoder->quality = quality;
    encoder->qualityAlpha = quality;
    encoder->speed = speed;
    if (args.threads > 0) {
      encoder->maxThreads = args.threads;
    }

    avifRWData output = AVIF_DATA_EMPTY;
    avifResult result = avifEncoderWrite(encoder, image, &output);

    std::vector<uint8_t> output_vec;
    if (result == AVIF_RESULT_OK) {
      output_vec.assign(output.data, output.data + output.size);
    }

    avifRWDataFree(&output);
    avifEncoderDestroy(encoder);
    avifImageDestroy(image);

    if (result != AVIF_RESULT_OK) {
      throw std::runtime_error("avifEncoderWrite failed");
    }

    return output_vec;
  }

 private:
  avifCodecChoice codec_;
  std::vector<uint8_t> input_data;
  int width;
  int height;
  int quality;
  int speed;
  avifPixelFormat yuv_format;
};
