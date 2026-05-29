# image-implementation-benchmark

This repository contains benchmarks for various image format implementations, comparing performance across C, C++, and Rust libraries.

## Getting Started

### Prerequisites

* [uv](https://docs.astral.sh/uv/) - Python package manager (install: `curl -LsSf https://astral.sh/uv/install.sh | sh`)
* Rust toolchain ([rustup](https://rustup.rs/))
* CMake, Clang, ccache, NASM
* Meson + Ninja (for dav1d)
* ImageMagick, hyperfine, wget, unzip
* [just](https://github.com/casey/just) — task runner
* [lefthook](https://github.com/evilmartians/lefthook) — git hooks manager

  On Ubuntu/Debian:

  ```bash
  sudo apt install build-essential clang clang-format cmake ccache nasm \
    meson ninja-build pkg-config imagemagick hyperfine wget unzip
  ```

  On macOS:

  ```bash
  brew install clang-format cmake ccache nasm meson ninja pkg-config imagemagick hyperfine wget unzip
  ```

All C/C++ image libraries (zlib, mimalloc, libjpeg-turbo, mozjpeg, libpng, spng, libwebp, dav1d, aom, SVT-AV1, libgav1, libavif, libjxl) and Rust libraries (rav1d) and ssimulacra2 are vendored as git submodules and built automatically. No system dev packages for these libraries are required.

> **CMake version:** CMake ≥ 3.5 is required. CMake 4.x is supported — `vendor/build_vendor.py` passes `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` automatically for older vendored projects (e.g. mozjpeg) that declare a lower minimum.

### Development Setup

1. **Install git hooks**:

   ```bash
   lefthook install
   ```

2. **Available recipes**:
   - `just fix` — format + lint fix (run before committing)
   - `just check` — CI-style read-only checks
   - `just test` — run all tests

   The pre-commit hook runs `just pre-commit` automatically on `git commit`. The pre-push hook runs `just test` on `git push`.

### Setup

1. **Fetch vendored sources**:

   ```bash
   git submodule update --init --recursive
   ```

2. **Install Python dependencies**:

   ```bash
   uv sync  # Creates .venv with pillow, imagehash, numpy
   ```

3. **Download benchmark datasets** (~3.5GB):

   ```bash
   ./bench setup              # All datasets (KODAK, DIV2K, pathological, test)

   # Or set up specific datasets:
   ./bench setup -d kodak         # Only KODAK
   ./bench setup -d div2k         # Only DIV2K
   ./bench setup -d pathological  # Only pathological tests
   ./bench setup -d test          # Only test image

   # Other options:
   ./bench setup --force          # Force re-download/regenerate
   ./bench setup --verify-only    # Check integrity only
   ```

   > **Note:** `./bench run` automatically sets up required datasets on first use, so an explicit `./bench setup` step is optional.

4. **Build implementations** (vendored libraries + all implementations built automatically via `./bench compile`)

### Running Benchmarks

Use `./bench run` with a dataset. Always specify `--dataset` (default `test` has minimal coverage):

```bash
# Quick test (minimal sample, single iteration)
./bench run --dataset kodak --sample 3 --quick

# =====

# Recommended if you have time: KODAK dataset (24 images, cache-resident)
./bench run --dataset kodak

# High-resolution testing (20 diverse 2K/4K images)
./bench run --dataset div2k

# Pathological/stress testing (4 synthetic images)
./bench run --dataset pathological

# Test on sample of 3 images from KODAK dataset
./bench run --dataset kodak --sample 3

# Run specific formats
./bench run --dataset kodak --formats jpeg,avif

# Decode-only benchmarks
./bench run --dataset kodak --mode decode

# Parallel benchmarks (all CPU cores)
./bench run --dataset kodak --threads 0

# Discard output I/O (pure compute)
./bench run --dataset kodak --discard-output

# Measure memory usage
./bench run --dataset div2k --measure-memory

# Compile all benchmarks
./bench compile

# Advanced: override inner-loop iterations / warmup (default: 10 / 2)
./bench run --dataset kodak --iterations 20 --warmup 3

# Advanced: skip the build step (if already built)
./bench run --dataset kodak --skip-build

# Advanced: metrics only, no hyperfine timing runs
./bench run --dataset kodak --no-benchmarks

# Advanced: timing runs only, no SSIMULACRA2 metrics
./bench run --dataset kodak --no-metrics
```

### Cleanup

```bash
./bench clean
```

### Results

Results are in `./results/<timestamp>/`:

* `summary.md` - Human-readable tables
* `raw.json` - Full Hyperfine output
* `manifest.json` - Reproducibility manifest
* `memory.csv` - Peak RSS (if `--measure-memory` used)

## Methodology

### Input Generation

The benchmarks use a tiered collection of images to test different performance characteristics. You select which dataset to use via the `--dataset` flag when running benchmarks.

#### Available Datasets

1. **KODAK (`--dataset kodak`)** — [KODAK PhotoCD dataset](http://r0k.us/graphics/kodak/) (24 images, ~0.4MP each)
   * L2/L3 cache resident images
   * Tests raw instruction throughput and vectorization efficiency
   * Natural photography with varied content

2. **DIV2K (`--dataset div2k`)** — [DIV2K dataset](https://data.vision.ee.ethz.ch/cvl/DIV2K/) (20 selected images, 2K/4K resolution)
   * Selected via `scripts/select_div2k.py` using perceptual hash diversity sampling
   * Tests memory bandwidth, allocator pressure, and large buffer performance
   * High-resolution, diverse content

3. **Pathological (`--dataset pathological`)** — Synthetic stress tests (4 images)
   * `solid_4k.png` — Solid color (tests RLE/skip optimizations)
   * `noise_4k.png` — Gaussian noise (worst-case for all compressors)
   * `screenshot_4k.png` — UI screenshot with text and flat regions
   * `alpha_gradient_4k.png` — Transparency gradient (for formats supporting alpha)

4. **Test (`--dataset test`)** — Single test file (legacy, minimal coverage)
   * For quick smoke tests only
   * Not recommended for comprehensive benchmarking

**Preparation Phase:**

* **For Encoding:** Images are taken as-is and converted to raw PPM (RGB24) or PAM (RGBA32) format.
* **For Decoding:** Images are pre-encoded using the **reference implementation** of the corresponding format at specific quality tiers.

#### Dataset Selection Strategy

Choose your dataset based on your benchmarking goals:

* **Performance Optimization (`kodak`)** — Best for micro-optimizations and instruction-level tuning. Images fit in cache, minimizing memory system variance.
* **Real-World Throughput (`div2k`)** — Best for measuring production performance. Tests memory bandwidth, allocator efficiency, and scaling behavior.
* **Edge Case Validation (`pathological`)** — Best for finding corner cases, testing worst-case performance, and validating optimizations don't break on synthetic inputs.
* **Quick Validation (`test`)** — Single-image smoke tests only. Not suitable for performance comparison.

**Recommendation:** Run `kodak` for initial development and optimization work, then validate with `div2k` and `pathological` before publishing results.

### Quality Tiers

We benchmark against three distinct use cases. The CLI accepts tier names directly (`--quality web-low`), and each binary maps these to format-specific arguments internally.

| Tier         | Intent            | JPEG                | AVIF                       | JXL               | WEBP           |
| :----------- | :---------------- | :------------------ | :------------------------- | :---------------- | :------------- |
| **web-low**  | Thumbnail/Preview | Q50, Baseline       | Q65, Speed 6               | d4.0, e7          | Q50, m4        |
| **web-high** | Standard Delivery | Q80, Progressive    | Q65 *(grain synth TBD)*    | d1.0, e7          | Q75, m4 †      |
| **archival** | High Fidelity     | Q95, No Subsampling | Q85, YUV444                | d0, e9 (Lossless) | Lossless, z6   |

> **† Known limitations:**
> - **AVIF web-high grain synthesis** is specified but not yet implemented in either `libavif` or `rav1e`. Both encoders currently use the same parameters as web-low for this tier. A TODO is tracked in each implementation.
> - **image-webp** (Rust) only supports lossless WebP encoding (crate limitation). All three quality tiers produce lossless output. Exclude `image-webp` from lossy-tier comparisons until a lossy API is available.
> - **spng** (C++) does not expose compression level control, so all three PNG quality tiers produce identical output for `spng-encode`.

### Benchmarking Architecture

To ensure statistically significant results and eliminate "Cold Start" bias (OS process spawning, dynamic linker loading), we use a hybrid approach:

1. **The Harness (Hyperfine):** Manages the statistical runs, warmup, and outlier detection.
2. **The Binary (Internal Loop):** Performs the actual decode/encode operation N times within a single process.

#### Binary Interface

Every encoder/decoder implementation is compiled into a standalone binary implementing this CLI:

```bash
./<binary> \
  --input <path> \
  --output <path> \
  --quality <web-low|web-high|archival> \
  --iterations <int> \
  --warmup <int> \
  --threads <int> \
  [--discard]
```

| Flag           | Description                                                                                                                       |
| :------------- | :-------------------------------------------------------------------------------------------------------------------------------- |
| `--iterations` | Number of timed operations in the measurement loop.                                                                               |
| `--warmup`     | Number of untimed iterations to run before measurement (default: 2). Warms branch predictors, allocators, and caches.             |
| `--threads`    | Number of threads to use. Use `1` for single-threaded benchmarks, `0` for "use all available cores".                              |
| `--discard`    | Discard output instead of writing to disk. Computes a CRC32 checksum to prevent dead code elimination. Isolates compute from I/O. |

#### Memory Allocation Strategy

Memory is allocated and freed inside each iteration to simulate realistic per-request behavior. However, this introduces allocator variance as a confounding variable.

**Allocator configuration by language:**

| Language | Allocator | Notes                                                                                      |
| :------- | :-------- | :----------------------------------------------------------------------------------------- |
| C/C++    | mimalloc  | Linked explicitly via `-lmimalloc`                                                         |
| Rust     | mimalloc  | Via `mimalloc = { version = "0.1", features = ["local_dynamic_tls"] }` as global allocator |

**Note:** We purposely include allocation time in the measurements to reflect real-world usage patterns. We do not support preallocation for the timebeing.

#### Verification Strategy

The [benchmark harness](./bench) measures the visual similarity of the decoded output to the source based on the SSIMULACRA2 metric. While any choice of similarity metric is subject to bias, this is the validate that each benchmark implementation are producing output consistent to each other.

#### Discard Checksum

When `--discard` is set, output bytes are fed through a CRC32 checksum to prevent compiler elimination of the encode/decode work. The C/C++ harness uses zlib's `crc32()` function; the Rust harness uses `crc32fast::Hasher`. Both libraries select hardware-accelerated implementations (e.g. SSE4.2, ARM CRC32) where available at compile time.

#### Baseline Measurement

The benchmark suite includes a `null` operation binary that performs only:

1. Read input file into memory
2. Compute CRC32 checksum
3. (If not `--discard`) Write buffer to output

This establishes the I/O and measurement floor, allowing you to isolate codec overhead from system overhead.

### Threading Model

All benchmarks are run in two configurations:

1. **Single-threaded (`--threads 1`):** Measures per-core efficiency and is useful for comparing instruction-level optimization.
2. **Parallel (`--threads 0`):** Uses all available cores. Measures real-world throughput for batch processing.

Binaries are pinned to specific cores using `taskset` (Linux) or equivalent to reduce scheduling variance.

### Statistical Reporting

Results are collected via Hyperfine and reported with:

* **Median** (primary metric, robust to outliers)
* **Mean**
* **Standard deviation**
* **Min/Max**
* **95% confidence interval**

Hyperfine is configured with `--warmup 3` (process-level warmup, separate from the binary's internal `--warmup`) and `--min-runs 10`.

### Compilation Guidelines

Binaries are compiled for release with aggressive optimization.

#### Rust

We use the following profile in `Cargo.toml`:

```toml
[profile.release]
opt-level = 3
lto = "fat"
codegen-units = 1
panic = "abort"
strip = true
```

Built with `RUSTFLAGS="-C target-cpu=native"`.

#### C/C++

We use Clang for consistency. Each implementation's `CMakeLists.txt` builds with:

```bash
clang/clang++ -O3 -fwhole-program-vtables -fstrict-aliasing -fomit-frame-pointer -march=native -DNDEBUG
```

Note that `-fno-exceptions` and `-fno-rtti` are intentionally **not** used because the implementations use C++ exceptions for error handling. LTO is not applied per-binary (the build is still fast due to ccache).

**Note:** `-march=native` makes binaries specific to the host machine. Results are not portable across architectures.

### Reproducibility Manifest

Every benchmark run generates a `manifest.json` containing:

```json
{
  "timestamp": "2025-01-15T10:30:00Z",
  "cpu": "Apple M3 Max",
  "cores": 14,
  "os": "macOS 15.2",
  "kernel": "Darwin 24.2.0",
  "compiler": {
    "clang": "17.0.6",
    "rustc": "1.84.0"
  },
  "libraries": {
    "libjpeg-turbo": "3.1.0",
    "mozjpeg": "4.1.5 (commit abc1234)",
    "libavif": "1.2.0",
    "dav1d": "1.5.0",
    "libjxl": "0.11.1",
    "...": "..."
  },
  "allocator": "mimalloc 2.1.2",
  "hyperfine": "1.18.0"
}
```

This manifest is committed alongside results for full reproducibility.

## Image Format Implementations

We include modern formats and their most competitive implementations.

> **Note:** HEIF is excluded due to licensing constraints and lack of competitive open implementations.

### JPEG

| Implementation    | Language | Notes                                                                                                                       |
| :---------------- | :------- | :-------------------------------------------------------------------------------------------------------------------------- |
| **libjpeg-turbo** | C        | Industry standard, SIMD-optimized                                                                                           |
| **mozjpeg**       | C        | *Optimized for compression ratio, not speed.* Included for completeness; expect slower encode times by design.              |
| **jpegli**        | C++      | Google's perceptually-tuned JPEG encoder from [libjxl](https://github.com/libjxl/libjxl). Built from the vendored `libjxl` submodule (`jpegli-static`). Encoder only.   |
| **jpeg-decoder**  | Rust     | Pure Rust JPEG decoder used in [image-rs](https://github.com/image-rs/image)                                                |
| **zune-jpeg**     | Rust     | Pure-Rust JPEG decoder used in [zune-image](https://github.com/etemesi254/zune-image)                                       |
| **jpeg-encoder**  | Rust     | Pure-Rust JPEG encoder used in [zune-image](https://github.com/etemesi254/zune-image). AVX2 (SIMD) feature flag is enabled. |
| **image-jpeg**    | Rust     | JPEG encoder from the `image` crate (`image::codecs::jpeg::JpegEncoder`). Encoder-only; no progressive or subsampling control. |

### PNG

| Implementation | Language | Notes                                     |
| :------------- | :------- | :---------------------------------------- |
| **libpng**     | C        | Reference implementation                  |
| **spng**       | C        | "Simple PNG", speed-optimized. *Encoder does not expose compression level control; all quality tiers produce identical output.* |
| **png**        | Rust     | Standard `image-rs` crate                 |
| **zune-png**   | Rust     | Highly optimized pure Rust implementation |

### WEBP

| Implementation | Language | Notes                         |
| :------------- | :------- | :---------------------------- |
| **libwebp**    | C        | Reference implementation      |
| **image-webp** | Rust     | *Lossless-only crate limitation — lossy tiers (web-low, web-high) produce lossless output. Exclude from lossy-tier comparisons.* |

### AVIF

| Implementation | Language | Notes                                     |
| :------------- | :------- | :---------------------------------------- |
| **libavif**    | C        | Reference (AOM/dav1d backend)             |
| **dav1d**      | C/Asm    | Decoder via libavif (dav1d backend)       |
| **libgav1**    | C++      | Decoder via libavif (libgav1 backend)     |
| **SVT-AV1**    | C        | Encoder via libavif (SVT-AV1 backend)     |
| **rav1e**      | Rust     | Encoder. *Film grain synthesis (web-high) not yet implemented; web-high uses same parameters as web-low.* |
| **rav1d**      | Rust     | Decoder (Rust port of dav1d). *Drop-in dav1d replacement; linked at binary level.* |

### JPEG XL

| Implementation  | Language | Notes                    |
| :-------------- | :------- | :----------------------- |
| **libjxl**      | C++      | Reference implementation |
| **jxl-oxide**   | Rust     | Pure Rust decoder        |
| **zune-jpegxl** | Rust     | Optimized Rust encoder   |

## Limitations and Caveats

1. **Architecture-specific results.** Due to `-march=native`, results are only valid for the exact CPU used. Cross-machine comparisons require recompilation and re-running.

2. **Allocator as confounding variable.** While we standardize on mimalloc, real-world performance may differ with system allocators.

3. **Image set limitations.** KODAK is compositionally narrow (natural photography). While we supplement with pathological cases, results may not generalize to all image types (e.g., medical imaging, satellite imagery).

4. **mozjpeg design goals.** mozjpeg prioritizes compression ratio over speed. Its slower encode times are intentional, not a deficiency.

5. **8-bit only pipeline.** All intermediate PPM files are normalized to 8-bit depth (max value 255). 16-bit images are not tested as they increase complexity of pipeline and do not provide meaningful extra data points.

## Contributing

Contributions are welcome!

* **New Implementations:** Must implement the standard CLI defined in "Benchmarking Architecture".
* **Optimization:** If you find flags or methods that improve a specific implementation, open a PR with benchmark results and updated manifest.
* **Image Sets:** Proposals for additional pathological or domain-specific test images are welcome.
* Run `just fix` before committing, or let the pre-commit hook handle it automatically.
* CI runs `just check` and `just test` on all PRs.
