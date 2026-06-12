#pragma once

#include <zlib.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstring>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

struct Args {
  std::string input;
  std::string output;
  // Generic encoder/decoder tunables (key -> value). The orchestrator passes
  // these via repeated `--param key=value`. Which keys an implementation reads
  // depends on the implementation; unknown keys are ignored, so a superset can
  // be passed safely.
  std::map<std::string, std::string> params;
  int iterations = 10;
  int warmup = 2;
  int threads = 0;
  bool discard = false;
};

/// Look up a tunable by key, returning `def` if absent.
inline std::string param_str(const Args& args, const std::string& key,
                             const std::string& def) {
  auto it = args.params.find(key);
  return it != args.params.end() ? it->second : def;
}

/// Tunable as an int, or `def` if absent/unparseable.
inline int param_int(const Args& args, const std::string& key, int def) {
  auto it = args.params.find(key);
  if (it == args.params.end()) return def;
  try {
    return std::stoi(it->second);
  } catch (...) {
    return def;
  }
}

/// Tunable as a double, or `def` if absent/unparseable.
inline double param_double(const Args& args, const std::string& key,
                           double def) {
  auto it = args.params.find(key);
  if (it == args.params.end()) return def;
  try {
    return std::stod(it->second);
  } catch (...) {
    return def;
  }
}

/// Tunable as a boolean. Truthy values: 1/true/yes/on (case-insensitive).
/// Absent keys return `def`.
inline bool param_bool(const Args& args, const std::string& key, bool def) {
  auto it = args.params.find(key);
  if (it == args.params.end()) return def;
  std::string v = it->second;
  std::transform(v.begin(), v.end(), v.begin(),
                 [](unsigned char c) { return std::tolower(c); });
  return v == "1" || v == "true" || v == "yes" || v == "on";
}

class BenchmarkImplementation {
 public:
  virtual ~BenchmarkImplementation() = default;
  virtual std::string name() const = 0;
  virtual void prepare(const Args& args) = 0;
  virtual std::vector<uint8_t> run(const Args& args) = 0;
};

inline uint32_t crc32_hash(const std::vector<uint8_t>& data) {
  // Use zlib's hardware-accelerated CRC32 implementation
  return ::crc32(0L, data.data(), data.size());
}

/// Encodes RGB pixel data as PPM P6 format (8-bit per channel).
///
/// @param width Image width in pixels
/// @param height Image height in pixels
/// @param rgb_data RGB pixel data (3 bytes per pixel, row-major order)
/// @return A vector containing the complete PPM file (header + pixel data)
inline std::vector<uint8_t> encode_ppm_rgb8(
    uint32_t width, uint32_t height, const std::vector<uint8_t>& rgb_data) {
  size_t expected_size = static_cast<size_t>(width) * height * 3;
  if (rgb_data.size() != expected_size) {
    throw std::runtime_error("RGB data size mismatch: expected " +
                             std::to_string(expected_size) + " bytes, got " +
                             std::to_string(rgb_data.size()));
  }

  std::string header =
      "P6\n" + std::to_string(width) + " " + std::to_string(height) + "\n255\n";
  std::vector<uint8_t> output;
  output.reserve(header.size() + rgb_data.size());
  output.insert(output.end(), header.begin(), header.end());
  output.insert(output.end(), rgb_data.begin(), rgb_data.end());
  return output;
}

/// Encodes RGB pixel data as PPM P6 format (16-bit per channel).
///
/// @param width Image width in pixels
/// @param height Image height in pixels
/// @param rgb_data RGB pixel data (u16 values, 3 values per pixel, row-major
/// order)
/// @return A vector containing the complete PPM file (header + pixel data in
/// big-endian)
inline std::vector<uint8_t> encode_ppm_rgb16(
    uint32_t width, uint32_t height, const std::vector<uint16_t>& rgb_data) {
  size_t expected_size = static_cast<size_t>(width) * height * 3;
  if (rgb_data.size() != expected_size) {
    throw std::runtime_error(
        "RGB data size mismatch: expected " + std::to_string(expected_size) +
        " u16 values, got " + std::to_string(rgb_data.size()));
  }

  std::string header = "P6\n" + std::to_string(width) + " " +
                       std::to_string(height) + "\n65535\n";
  std::vector<uint8_t> output;
  output.reserve(header.size() + rgb_data.size() * 2);
  output.insert(output.end(), header.begin(), header.end());

  // Convert to big-endian
  for (uint16_t val : rgb_data) {
    output.push_back(static_cast<uint8_t>(val >> 8));
    output.push_back(static_cast<uint8_t>(val & 0xFF));
  }
  return output;
}

struct RGBImage {
  int width;
  int height;
  std::vector<uint8_t> data;
};

/// Reads an entire binary file into a vector.
inline std::vector<uint8_t> read_binary_file(const std::string& path) {
  std::ifstream file(path, std::ios::binary | std::ios::ate);
  if (!file) throw std::runtime_error("Failed to open input file: " + path);
  std::streamsize size = file.tellg();
  file.seekg(0, std::ios::beg);
  std::vector<uint8_t> data(size);
  if (!file.read(reinterpret_cast<char*>(data.data()), size))
    throw std::runtime_error("Failed to read input file");
  return data;
}

inline RGBImage decode_ppm_rgb8(const std::string& input_path) {
  std::ifstream file(input_path, std::ios::binary | std::ios::ate);
  if (!file)
    throw std::runtime_error("Failed to open input file: " + input_path);
  std::streamsize size = file.tellg();
  file.seekg(0, std::ios::beg);
  std::vector<char> buffer(size);
  if (!file.read(buffer.data(), size))
    throw std::runtime_error("Failed to read input file");

  const char* data = buffer.data();
  if (buffer.size() < 3 || data[0] != 'P' || data[1] != '6') {
    throw std::runtime_error("Input must be PPM P6 format");
  }

  size_t pos = 3;
  // Skip whitespace
  while (pos < buffer.size() &&
         (data[pos] == ' ' || data[pos] == '\n' || data[pos] == '\r'))
    pos++;

  // Skip comments
  while (pos < buffer.size() && data[pos] == '#') {
    while (pos < buffer.size() && data[pos] != '\n') pos++;
    pos++;
  }

  int width = 0;
  while (pos < buffer.size() && data[pos] >= '0' && data[pos] <= '9') {
    width = width * 10 + (data[pos] - '0');
    pos++;
  }
  while (pos < buffer.size() &&
         (data[pos] == ' ' || data[pos] == '\n' || data[pos] == '\r'))
    pos++;

  int height = 0;
  while (pos < buffer.size() && data[pos] >= '0' && data[pos] <= '9') {
    height = height * 10 + (data[pos] - '0');
    pos++;
  }

  // Skip max value
  while (pos < buffer.size() &&
         (data[pos] == ' ' || data[pos] == '\n' || data[pos] == '\r'))
    pos++;
  while (pos < buffer.size() && data[pos] >= '0' && data[pos] <= '9') pos++;
  // Exactly one whitespace character separates the maxval from the raster.
  // Do NOT skip further: the first pixel byte may itself be a whitespace
  // value (e.g. 0x0a/0x0d/0x20), which a greedy skip would wrongly consume.
  if (pos < buffer.size() &&
      (data[pos] == ' ' || data[pos] == '\n' || data[pos] == '\r' ||
       data[pos] == '\t'))
    pos++;

  std::vector<uint8_t> rgb_data(
      reinterpret_cast<const uint8_t*>(data + pos),
      reinterpret_cast<const uint8_t*>(data + buffer.size()));

  if (rgb_data.size() < static_cast<size_t>(width) * height * 3) {
    throw std::runtime_error("Insufficient pixel data in PPM file");
  }

  return {width, height, rgb_data};
}

inline Args parse_args(int argc, char** argv) {
  Args args;
  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if (arg == "--input" && i + 1 < argc)
      args.input = argv[++i];
    else if (arg == "--output" && i + 1 < argc)
      args.output = argv[++i];
    else if (arg == "--param" && i + 1 < argc) {
      // Split on the first '=' only, so values may contain '='.
      std::string kv = argv[++i];
      size_t eq = kv.find('=');
      if (eq != std::string::npos && eq > 0) {
        args.params[kv.substr(0, eq)] = kv.substr(eq + 1);
      }
    } else if (arg == "--iterations" && i + 1 < argc)
      args.iterations = std::stoi(argv[++i]);
    else if (arg == "--warmup" && i + 1 < argc)
      args.warmup = std::stoi(argv[++i]);
    else if (arg == "--threads" && i + 1 < argc)
      args.threads = std::stoi(argv[++i]);
    else if (arg == "--discard")
      args.discard = true;
  }
  return args;
}

inline int run_benchmark(int argc, char** argv, BenchmarkImplementation& impl) {
  Args args = parse_args(argc, argv);

  if (args.threads > 0) {
    // Set environment variable for OMP
    std::string threads_str = std::to_string(args.threads);
#ifdef _WIN32
    _putenv_s("OMP_NUM_THREADS", threads_str.c_str());
#else
    setenv("OMP_NUM_THREADS", threads_str.c_str(), 1);
#endif
  }

  try {
    impl.prepare(args);

    // Warmup
    for (int i = 0; i < args.warmup; ++i) {
      impl.run(args);
    }

    // Measurement
    for (int i = 0; i < args.iterations; ++i) {
      auto output = impl.run(args);

      if (args.discard) {
        uint32_t c = crc32_hash(output);
        // Prevent optimization
        volatile uint32_t dummy = c;
        (void)dummy;
      } else {
        if (!output.empty()) {
          std::ofstream outfile(args.output, std::ios::binary);
          outfile.write(reinterpret_cast<const char*>(output.data()),
                        output.size());
        }
      }
    }
  } catch (const std::exception& e) {
    std::cerr << "Error: " << e.what() << std::endl;
    return 1;
  }

  return 0;
}
