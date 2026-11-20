// Shared AVIF encode implementation for libavif with selectable codec backend.
// The codec backend is selected via the constructor parameter.
#pragma once

#include <avif/avif.h>

#include <stdexcept>
#include <string>
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

    // Tunables: quality (0-100), speed preset, chroma subsampling (YUV format).
    quality = param_int(args, "quality", 65);
    speed = param_int(args, "speed", 6);
    yuv_format = (param_str(args, "yuv", "420") == "444")
                     ? AVIF_PIXEL_FORMAT_YUV444
                     : AVIF_PIXEL_FORMAT_YUV420;
    // libaom `--tune` (iq, ssim, ssimulacra2, psnr, ...). Empty = libavif's
    // default, which for a lossy YUV still on libaom >= 3.13 is already `iq`.
    // Only the aom backend understands it; other backends ignore the knob.
    tune = param_str(args, "tune", "");
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
    if (!tune.empty() && codec_ == AVIF_CODEC_CHOICE_AOM) {
      // Forwarded to aom_codec_set_option(); an unknown value fails the
      // avifEncoderWrite below rather than silently encoding with the default.
      if (avifEncoderSetCodecSpecificOption(encoder, "tune", tune.c_str()) !=
          AVIF_RESULT_OK) {
        avifEncoderDestroy(encoder);
        avifImageDestroy(image);
        throw std::runtime_error("avifEncoderSetCodecSpecificOption failed");
      }
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
  std::string tune;
};
