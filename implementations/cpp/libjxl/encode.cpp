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

    // Issue #4: JXL progressive / quality-constraint knobs. -1 = leave the
    // encoder's own default (so omitting the --param is a true no-op); 0/1(/2)
    // force the setting. decoding_speed defaults to 0 (libjxl's own default).
    progressive = param_int(args, "progressive", -1);
    modular = param_int(args, "modular", -1);
    responsive = param_int(args, "responsive", -1);
    progressive_dc = param_int(args, "progressive_dc", -1);
    decoding_speed = param_int(args, "decoding_speed", 0);
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
    // XYB (uses_original_profile=FALSE) is the correct high-quality lossy path;
    // the libjxl header notes original-profile should be FALSE for most lossy
    // use cases. Forcing JXL_TRUE disables XYB and collapses quality at low
    // distance. Lossless keeps the original profile, since XYB is not
    // bit-exact.
    basic_info.uses_original_profile = lossless ? JXL_TRUE : JXL_FALSE;

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

    // Progressive / quality-constraint knobs (issue #4). Each -1 leaves the
    // encoder default untouched; decoding_speed (0..4) is always applied (0 =
    // the libjxl default, a no-op).
    if (progressive != -1) {
      JxlEncoderFrameSettingsSetOption(
          frame_settings, JXL_ENC_FRAME_SETTING_PROGRESSIVE_AC, progressive);
    }
    if (progressive_dc != -1) {
      JxlEncoderFrameSettingsSetOption(
          frame_settings, JXL_ENC_FRAME_SETTING_PROGRESSIVE_DC, progressive_dc);
    }
    if (responsive != -1) {
      JxlEncoderFrameSettingsSetOption(
          frame_settings, JXL_ENC_FRAME_SETTING_RESPONSIVE, responsive);
    }
    if (modular != -1) {
      JxlEncoderFrameSettingsSetOption(frame_settings,
                                       JXL_ENC_FRAME_SETTING_MODULAR, modular);
    }
    JxlEncoderFrameSettingsSetOption(
        frame_settings, JXL_ENC_FRAME_SETTING_DECODING_SPEED, decoding_speed);

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
  int progressive;
  int modular;
  int responsive;
  int progressive_dc;
  int decoding_speed;
  JxlThreadParallelRunnerPtr runner{nullptr};
};

int main(int argc, char **argv) {
  LibJxlEncodeBench bench;
  return run_benchmark(argc, argv, bench);
}
