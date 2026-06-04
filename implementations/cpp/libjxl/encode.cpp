#include <jxl/encode.h>
#include <jxl/encode_cxx.h>
#include <jxl/thread_parallel_runner.h>
#include <jxl/thread_parallel_runner_cxx.h>

#include <stdexcept>
#include <vector>

#include "benchmark_harness.hpp"

class LibJxlEncodeBench : public BenchmarkImplementation {
 public:
  std::string name() const override { return "libjxl-encode"; }

  void prepare(const Args &args) override {
    // Initialize thread pool once
    runner = JxlThreadParallelRunnerMake(
        nullptr, args.threads > 0
                     ? args.threads
                     : JxlThreadParallelRunnerDefaultNumWorkerThreads());

    // Load and parse PPM input via the shared harness helper (8-bit pipeline)
    RGBImage img = decode_ppm_rgb8(args.input);
    width = img.width;
    height = img.height;
    input_data = std::move(img.data);

    // Tunables: Butteraugli distance (0 = lossless) and effort (1-9).
    distance = static_cast<float>(param_double(args, "distance", 1.0));
    effort = param_int(args, "effort", 7);
    lossless = (distance <= 0.0f);
  }

  std::vector<uint8_t> run(const Args &args) override {
    auto enc = JxlEncoderMake(nullptr);

    if (JXL_ENC_SUCCESS != JxlEncoderSetParallelRunner(enc.get(),
                                                       JxlThreadParallelRunner,
                                                       runner.get())) {
      throw std::runtime_error("JxlEncoderSetParallelRunner failed");
    }

    JxlPixelFormat pixel_format = {3, JXL_TYPE_UINT8, JXL_LITTLE_ENDIAN, 0};

    JxlBasicInfo basic_info;
    JxlEncoderInitBasicInfo(&basic_info);
    basic_info.xsize = width;
    basic_info.ysize = height;
    basic_info.bits_per_sample = 8;
    basic_info.uses_original_profile = JXL_TRUE;

    if (JXL_ENC_SUCCESS != JxlEncoderSetBasicInfo(enc.get(), &basic_info)) {
      throw std::runtime_error("JxlEncoderSetBasicInfo failed");
    }

    JxlColorEncoding color_encoding = {};
    JxlColorEncodingSetToSRGB(&color_encoding, /*is_gray=*/JXL_FALSE);
    if (JXL_ENC_SUCCESS !=
        JxlEncoderSetColorEncoding(enc.get(), &color_encoding)) {
      throw std::runtime_error("JxlEncoderSetColorEncoding failed");
    }

    JxlEncoderFrameSettings *frame_settings =
        JxlEncoderFrameSettingsCreate(enc.get(), nullptr);

    if (lossless) {
      JxlEncoderSetFrameLossless(frame_settings, JXL_TRUE);
    } else {
      JxlEncoderSetFrameLossless(frame_settings, JXL_FALSE);
      JxlEncoderSetFrameDistance(frame_settings, distance);
    }

    JxlEncoderFrameSettingsSetOption(frame_settings,
                                     JXL_ENC_FRAME_SETTING_EFFORT, effort);

    if (JXL_ENC_SUCCESS !=
        JxlEncoderAddImageFrame(frame_settings, &pixel_format,
                                const_cast<uint8_t *>(input_data.data()),
                                input_data.size())) {
      throw std::runtime_error("JxlEncoderAddImageFrame failed");
    }

    JxlEncoderCloseInput(enc.get());

    std::vector<uint8_t> compressed;
    std::vector<uint8_t> chunk(4096);
    uint8_t *next_out = chunk.data();
    size_t avail_out = chunk.size();

    JxlEncoderStatus process_result = JXL_ENC_NEED_MORE_OUTPUT;
    while (process_result == JXL_ENC_NEED_MORE_OUTPUT) {
      process_result =
          JxlEncoderProcessOutput(enc.get(), &next_out, &avail_out);
      if (process_result == JXL_ENC_ERROR) {
        throw std::runtime_error("JxlEncoderProcessOutput failed");
      }
      size_t bytes_written = chunk.size() - avail_out;
      compressed.insert(compressed.end(), chunk.data(),
                        chunk.data() + bytes_written);
      next_out = chunk.data();
      avail_out = chunk.size();
    }

    return compressed;
  }

 private:
  std::vector<uint8_t> input_data;
  int width;
  int height;
  float distance;
  int effort;
  bool lossless;
  JxlThreadParallelRunnerPtr runner{nullptr};
};

int main(int argc, char **argv) {
  LibJxlEncodeBench bench;
  return run_benchmark(argc, argv, bench);
}
