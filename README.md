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
    meson ninja-build pkg-config imagemagick hyperfine wget unzip liblcms2-dev
  ```

  On macOS:

  ```bash
  brew install clang-format cmake ccache nasm meson ninja pkg-config imagemagick hyperfine wget unzip little-cms2
  ```

  > **lcms2** is required to build the image-quality tool (`tools/iqa-cli`), which
  > links the [`iqa-rs`](https://github.com/justin13888/iqa-rs) crate's SSIMULACRA2
  > FFI. On macOS you may need `PKG_CONFIG_PATH="$(brew --prefix little-cms2)/lib/pkgconfig"`.

All C/C++ image libraries (zlib, mimalloc, libjpeg-turbo, mozjpeg, libpng, spng, libwebp, dav1d, aom, SVT-AV1, libgav1, libavif, libjxl) and Rust libraries (rav1d, jxl-rs, [iqa-rs](https://github.com/justin13888/iqa-rs) for image-quality metrics) are vendored as git submodules and built automatically. No system dev packages for these libraries are required (except `lcms2`, see Prerequisites).

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

   > **Note:** `./bench perf` and `./bench quality` automatically set up required datasets on first use, so an explicit `./bench setup` step is optional.

4. **Build implementations** (vendored libraries + all implementations built automatically via `./bench compile`)

### Running Benchmarks

The suite is split into **two distinctly separate benchmarks**, each its own subcommand:

- **`./bench perf`** — *performance*. Hyperfine-timed encode **and** decode at each implementation's single fixed preset, swept across both threading modes (single-threaded and all-cores). Timing is always compute-only (output discarded, CRC32-checksummed). Presets are hardcoded per codec and may produce different-quality outputs across implementations — this is intentional for now and will be refined.
- **`./bench quality`** — *quality / rate-distortion*. Sweeps each lossy **encoder's** quality axis (e.g. JPEG quality, JXL distance) over many operating points and measures **file size + bits-per-pixel + SSIMULACRA2 + PSNR** at each, tracing a size-vs-quality curve (the many points needed for [issue #8](https://github.com/justin13888/image-implementation-benchmark/issues/8)). Encoders only; no timing and no thread sweep (encoded bytes are thread-invariant). IQA metrics come from the [`iqa-rs`](https://github.com/justin13888/iqa-rs) crate via the `iqa-cli` tool.

`--formats` is an optional subset filter on both; `--mode {encode,decode,both}` further narrows the performance suite.

```bash
# --- Performance ---
# Quick smoke test (all-cores only, single iteration)
./bench perf --dataset kodak --sample 3 --quick

# Full KODAK timing sweep (24 images, cache-resident; both thread modes)
./bench perf --dataset kodak

# Subset filters + memory
./bench perf --dataset kodak --formats jpeg avif --mode decode
./bench perf --dataset div2k --measure-memory
# Override inner-loop iterations / warmup (default 10 / 2)
./bench perf --dataset kodak --iterations 20 --warmup 3

# --- Quality (rate-distortion) ---
# Quick smoke test (2 quality points per encoder)
./bench quality --dataset kodak --sample 3 --quick

# Full quality sweep (every declared quality point per encoder)
./bench quality --dataset kodak

# Fewer, evenly-sampled points per encoder (e.g. 5)
./bench quality --dataset kodak --quality-steps 5 --formats jpeg jxl

# --- Both suites in one bundle ---
# Runs perf + quality into a single results bundle with a self-contained report.html
./bench all --dataset kodak

# --- Shared ---
./bench compile          # build vendored libs + all implementations + iqa-cli
./bench clean            # remove build artifacts and results
```

> **Runtime note:** the performance suite's two thread modes make a full run ~2× a single-mode run; use `--sample N`, `--formats`/`--mode`, or `--quick` for fast iteration. The quality suite's cost scales with quality points × images × encoders; `--quality-steps` and `--sample` cap it, and `--quick` collapses to 2 points.

### Cleanup

```bash
./bench clean
```

### Results

Every run writes a **bundle** to `./results/<timestamp>/` containing a `performance/` and/or `quality/` subfolder (both for `./bench all`), plus a top-level index and a self-contained report:

```
results/<timestamp>/
├── report.html        # self-contained, opens offline (perf charts + interactive quality)
├── summary.md         # index linking the per-suite summaries
├── manifest.json      # bundle metadata (which suites ran)
├── performance/       # (perf / all) raw.json, summary.md, timing charts, memory.csv
└── quality/           # (quality / all) metrics.json, summary.md (tables), manifest.json
```

**`performance/`** — `raw.json` (full Hyperfine output: `mean/median/stddev/min/max`, `times[]`, `exit_codes[]`), `summary.md` (timing table + grouped single-vs-all-cores charts, one per format/operation), timing `*.png`, `manifest.json` (`suite: performance`), and `memory.csv` (with `--measure-memory`).

**`quality/`** — `metrics.json` (per impl/format/operating-point/image: `filesize`, `bpp`, `ssimulacra2`, `psnr`, dimensions, the swept `quality_axis`/`quality_value`) is the raw data everything else is recomputed from; `summary.md` (BD-rate table + Pareto best-of-format table + per-step metrics table, linking to `report.html` for the curves); and `manifest.json` (`suite: quality` with the exact per-encoder `quality_sweeps`). The quality suite renders **no chart PNGs** — its rate-distortion curves are interactive in `report.html`.

**`report.html`** is a single offline-friendly file. Performance charts are embedded as base64 PNGs. The **quality** view is interactive: the full `metrics.json` is embedded inline and the rate-distortion curves are drawn client-side as SVG (no third-party JS) — per-format charts plus a combined cross-format Pareto chart of the best encoders, with metric (SSIMULACRA2/PSNR) and linear/log-x toggles, hover tooltips, and a sortable BD-rate table. Because the raw data is embedded, anything in the quality view can be recomputed from the report alone.

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
* **For Decoding:** Images are pre-encoded using the **reference implementation** of the corresponding format at that encoder's fixed performance preset.

#### Dataset Selection Strategy

Choose your dataset based on your benchmarking goals:

* **Performance Optimization (`kodak`)** — Best for micro-optimizations and instruction-level tuning. Images fit in cache, minimizing memory system variance.
* **Real-World Throughput (`div2k`)** — Best for measuring production performance. Tests memory bandwidth, allocator efficiency, and scaling behavior.
* **Edge Case Validation (`pathological`)** — Best for finding corner cases, testing worst-case performance, and validating optimizations don't break on synthetic inputs.
* **Quick Validation (`test`)** — Single-image smoke tests only. Not suitable for performance comparison.

**Recommendation:** Run `kodak` for initial development and optimization work, then validate with `div2k` and `pathological` before publishing results.

### Tunables & Operating Points

Each implementation declares its tunable knobs in a per-implementation schema in `bench_lib/models.py` (`TUNABLE_SCHEMAS`). The orchestrator passes the chosen values to the binary as generic `--param key=value` flags; the binary reads only the keys it understands. The schema defines two things per encoder:

- **`perf_preset`** — the single fixed operating point the **performance** suite uses (one set of params per codec). Presets are not quality-matched across implementations.
- **`quality_axis` + `quality_sweep`** — the knob the **quality** suite sweeps (e.g. JPEG `quality`, JXL `distance`) and the discrete values it steps through to trace a rate-distortion curve. Lossless encoders (PNG, lossless-only WebP) and decoders have no quality axis.

Representative tunables (see the schema for the authoritative list):

| Format | Quality axis | Other knobs | Perf preset |
| :----- | :----------- | :---------- | :---------- |
| **JPEG** (libjpeg-turbo, mozjpeg, jpegli, jpeg-encoder) | `quality` 1-100 | `progressive`, `subsampling` (420/444) | q80, progressive, 420 |
| **JPEG** (image-jpeg) | `quality` 1-100 | — | q80 |
| **WEBP** (libwebp) | `quality` 0-100 | `method` 0-6, `lossless` | q75, m4, lossy |
| **AVIF** (libavif, svt-av1, rav1e) | `quality` 0-100 | `speed`, chroma (420/444) | q65, speed 6, 420 |
| **JXL** (libjxl) | `distance` (lossy, 15.0→0.1) | `effort` 1-9 | d1.0, e7 |
| **JXL** (libjxl-lossless) | — *(distance pinned to 0)* | `effort` 1-9 | d0.0, e7 |
| **JXL** (zune-jpegxl) | `quality` 0-100 | `effort` 1-9 | q90, e7 |
| **PNG** (libpng, zune-png, image-png) | — *(lossless)* | compression/effort/filter | per impl |

> **Known limitations:**
> - **AVIF film grain synthesis** is not yet implemented in `libavif` or `rav1e` (a TODO is tracked in each).
> - **image-webp** (Rust) only supports lossless WebP encoding (crate limitation), so it has no quality axis and is excluded from the quality suite.
> - **spng** (C++) does not expose a compression-level control.

### Benchmarking Architecture

To ensure statistically significant results and eliminate "Cold Start" bias (OS process spawning, dynamic linker loading), we use a hybrid approach:

1. **The Harness (Hyperfine):** Manages the statistical runs, warmup, and outlier detection.
2. **The Binary (Internal Loop):** Performs the actual decode/encode operation N times within a single process.

#### Binary Interface

Every encoder/decoder implementation is compiled into a standalone binary implementing this uniform CLI. The orchestrator passes codec tunables as repeated `--param key=value` flags (the binary ignores keys it does not understand), sweeps `--threads` for the performance suite, and sets `--discard` for timing runs:

```bash
./<binary> \
  --input <path> \
  --output <path> \
  [--param <key>=<value>]... \
  --iterations <int> \
  --warmup <int> \
  --threads <int> \
  [--discard]
```

| Flag           | Description                                                                                                                       |
| :------------- | :-------------------------------------------------------------------------------------------------------------------------------- |
| `--param`      | Repeatable `key=value` tunable (e.g. `--param quality=80 --param progressive=true`). Last value wins; unknown keys are ignored.    |
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

#### Image Quality Assessment

The quality suite measures the fidelity of each encoded output relative to the source using the [`iqa-rs`](https://github.com/justin13888/iqa-rs) crate (via the in-repo `iqa-cli` tool), reporting **SSIMULACRA2** (perceptual; 100 = identical) and **PSNR** (dB). Because `iqa-rs` consumes raw pixels, each encoded output is first decoded back to PPM with the format's reference decoder, then compared to the source. (Butteraugli and SSIM are planned in `iqa-rs` and tracked as TODOs.)

> [!IMPORTANT]
> **IQA metrics are approximations, not ground truth.** Every image-quality metric encodes its own model of the human visual system, and each comes with assumptions and blind spots:
>
> - **SSIMULACRA2** is a perceptual estimator calibrated against subjective datasets at *specific* viewing conditions (display resolution, brightness, viewing distance). It can mis-rank distortions it was not tuned for and is not guaranteed to be monotonic with perceived quality near the high-fidelity (near-lossless) end of the scale.
> - **PSNR** measures pixel-wise error only and is well known to correlate poorly with human perception — it cannot see structure, texture masking, or color sensitivity.
>
> Both are *automated, full-reference* metrics: they compare against the source pixels and say nothing about aesthetic quality, artifact *annoyance*, or content the metric was never trained on (e.g. text, screenshots, medical or satellite imagery). **Aggregate scores (BD-rate, Pareto fronts) can be sensitive to the metric, dataset, and operating points chosen, and a few points of SSIMULACRA2 may not be perceptible.** Treat these results as a reproducible *guide* for narrowing options, **not** as a substitute for a properly controlled human subjective study (e.g. MOS/2AFC) when determining the genuinely best-looking option for a given use case.

#### Discard Checksum

**Timing runs are always compute-only:** `./bench perf` invokes every binary with `--discard`, removing filesystem-write variance as a confound. (The quality suite runs each binary *without* `--discard` so it writes a real file to size and score.)

When `--discard` is set, output bytes are fed through a CRC32 checksum to prevent compiler elimination of the encode/decode work. The C/C++ harness uses zlib's `crc32()` function; the Rust harness uses `crc32fast::Hasher`. Both libraries select hardware-accelerated implementations (e.g. SSE4.2, ARM CRC32) where available at compile time.

#### Baseline Measurement

The benchmark suite includes a `null` operation binary that performs only:

1. Read input file into memory
2. Compute CRC32 checksum
3. (If not `--discard`) Write buffer to output

This establishes the I/O and measurement floor, allowing you to isolate codec overhead from system overhead.

### Threading Model

The **performance** suite automatically sweeps **both** threading configurations (they appear side-by-side in the timing charts):

1. **Single-threaded (`--threads 1`):** Measures per-core efficiency and is useful for comparing instruction-level optimization.
2. **Parallel (`--threads 0`):** Uses all available cores. Measures real-world throughput for batch processing.

The **quality** suite does not sweep threads — encoded bytes are thread-invariant, so it runs all-cores only. With `--pin-cores` (performance suite), binaries are pinned to specific cores using `taskset` (Linux) or equivalent to reduce scheduling variance.

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
clang/clang++ -O3 -fstrict-aliasing -fomit-frame-pointer -march=native -DNDEBUG
```

Note that `-fno-exceptions` and `-fno-rtti` are intentionally **not** used because the implementations use C++ exceptions for error handling. LTO is not applied per-binary (the build is still fast due to ccache); whole-program vtable optimization (`-fwhole-program-vtables`) is therefore omitted as it requires LTO.

**Note:** `-march=native` makes binaries specific to the host machine. Results are not portable across architectures.

### Reproducibility Manifest

Every benchmark run generates a `manifest.json` containing:

```json
{
  "timestamp": "2025-01-15T10:30:00Z",
  "os": "macOS 15.2",
  "kernel": "Darwin 24.2.0",
  "cpu": "Apple M3 Max",
  "cores": 14,
  "compiler": {
    "rustc": "1.84.0",
    "clang": "17.0.6",
    "cmake": "3.31.2"
  },
  "libraries": {
    "libjpeg-turbo": "3.1.0",
    "mozjpeg": "4.1.5",
    "libavif": "1.2.0",
    "dav1d": "1.5.0",
    "libjxl": "0.11.1",
    "...": "...",
    "mimalloc": "2.1.2",
    "hyperfine": "1.18.0"
  },
  "allocator": "mimalloc 2.1.2",
  "benchmark_config": {
    "suite": "performance",
    "dataset": "kodak",
    "formats": ["jpeg", "png", "webp", "avif", "jxl"],
    "mode": "both",
    "operating_point": "perf-preset",
    "thread_modes": [1, 0],
    "discard_output": true,
    "iterations": 10,
    "warmup": 2,
    "pin_cores": false,
    "quick": false
  }
}
```

The `benchmark_config` block records the suite (`performance` or `quality`) and its protocol. The performance manifest lists the swept `thread_modes`; the quality manifest instead records `quality_steps` and the exact per-encoder `quality_sweeps`. This manifest is written alongside results for full reproducibility.

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
| **spng**       | C        | "Simple PNG", speed-optimized. *Encoder does not expose a compression-level control.* |
| **png**        | Rust     | Standard `image-rs` crate                 |
| **zune-png**   | Rust     | Highly optimized pure Rust implementation |

### WEBP

| Implementation | Language | Notes                         |
| :------------- | :------- | :---------------------------- |
| **libwebp**    | C        | Reference implementation      |
| **image-webp** | Rust     | *Lossless-only crate limitation — no quality axis, so excluded from the quality suite's rate-distortion sweep.* |

### AVIF

| Implementation | Language | Notes                                     |
| :------------- | :------- | :---------------------------------------- |
| **libavif**    | C        | Reference (AOM/dav1d backend)             |
| **dav1d**      | C/Asm    | Decoder via libavif (dav1d backend)       |
| **libgav1**    | C++      | Decoder via libavif (libgav1 backend)     |
| **SVT-AV1**    | C        | Encoder via libavif (SVT-AV1 backend)     |
| **rav1e**      | Rust     | Encoder. *Film grain synthesis not yet implemented (tracked as a TODO).* |
| **rav1d**      | Rust     | Decoder (Rust port of dav1d). *Drop-in dav1d replacement; linked at binary level.* |

### JPEG XL

| Implementation  | Language | Notes                    |
| :-------------- | :------- | :----------------------- |
| **libjxl**      | C++      | Reference implementation |
| **jxl-oxide**   | Rust     | Pure Rust decoder        |
| **jxl-rs**      | Rust     | libjxl's official Rust decoder (vendored submodule) |
| **zune-jpegxl** | Rust     | Optimized Rust encoder   |

## Limitations and Caveats

1. **Architecture-specific results.** Due to `-march=native`, results are only valid for the exact CPU used. Cross-machine comparisons require recompilation and re-running.

2. **Allocator as confounding variable.** While we standardize on mimalloc, real-world performance may differ with system allocators.

3. **Image set limitations.** KODAK is compositionally narrow (natural photography). While we supplement with pathological cases, results may not generalize to all image types (e.g., medical imaging, satellite imagery).

4. **mozjpeg design goals.** mozjpeg prioritizes compression ratio over speed. Its slower encode times are intentional, not a deficiency.

5. **8-bit only pipeline.** All intermediate PPM files are normalized to 8-bit depth (max value 255). 16-bit images are not tested as they increase complexity of pipeline and do not provide meaningful extra data points.

6. **IQA metrics are approximations.** SSIMULACRA2 and PSNR are automated estimators of perceived quality, each with its own assumptions and blind spots (see [Image Quality Assessment](#image-quality-assessment)). They are a reproducible guide for narrowing options, **not** a replacement for a controlled human subjective study (e.g. MOS) when determining the genuinely best-looking option.

## Contributing

Contributions are welcome!

* **New Implementations:** Must implement the standard CLI defined in "Benchmarking Architecture".
* **Optimization:** If you find flags or methods that improve a specific implementation, open a PR with benchmark results and updated manifest.
* **Image Sets:** Proposals for additional pathological or domain-specific test images are welcome.
* Run `just fix` before committing, or let the pre-commit hook handle it automatically.
* CI runs `just check` and `just test` on all PRs.
