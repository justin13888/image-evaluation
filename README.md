# image-evaluation

This repository contains benchmarks for various image format implementations, comparing performance across C, C++, and Rust libraries.

> Notes: We compare all *software* implementations of all standard (and still relevant) image formats and encourage new contributions to be submitted via GitHub issues/PR. Additionally, this implies that all *hardware* implementations are out-of-scope; in general, software encoders always result in better quality at the cost of latency and a correct hardware decoder typically can achieve lower latencies than an equivalent software decoder since formats typically require byte-for-byte exactness except for odd ones like JPEG.

## Getting Started

### Prerequisites

* [mise](https://mise.jdx.dev/) — task runner and tool manager (install: `curl https://mise.run | sh`). It provisions the project's `uv` (Python package manager) and [`hk`](https://hk.jdx.dev/) (git hooks); run `mise install` to fetch them. Rust (via rustup) and the system C/C++ build tools below are not managed by mise.
* Rust toolchain ([rustup](https://rustup.rs/))
* CMake, Clang, ccache
* Autoconf + Automake (to build the vendored NASM assembler)
* Meson + Ninja (for dav1d)
* ImageMagick, hyperfine, wget, unzip

  On Ubuntu/Debian:

  ```bash
  sudo apt install build-essential clang clang-format cmake ccache autoconf automake \
    meson ninja-build pkg-config imagemagick hyperfine wget unzip
  ```

  On macOS:

  ```bash
  brew install clang-format cmake ccache autoconf automake meson ninja pkg-config imagemagick hyperfine wget unzip
  ```

The NASM assembler (used to assemble the x86-64 SIMD kernels in aom, dav1d, rav1d, libjpeg-turbo, mozjpeg, SVT-AV1, and libwebp) is also vendored as a git submodule and built from source automatically — you no longer need a system-wide `nasm`. All C/C++ image libraries (zlib, mimalloc, libjpeg-turbo, mozjpeg, libpng, spng, libwebp, dav1d, aom, SVT-AV1, libgav1, libavif, libjxl) and Rust libraries (rav1d, jxl-rs) are vendored as git submodules and built automatically; image-quality metrics come from the published [`iqa-cli`](https://crates.io/crates/iqa-cli) binary (installed from crates.io via `cargo install`, building the [`iqa`](https://crates.io/crates/iqa) crate with lcms2 compiled from source). No system dev packages for these libraries are required.

> **CMake version:** CMake ≥ 3.5 is required. CMake 4.x is supported — `vendor/build_vendor.py` passes `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` automatically for older vendored projects (e.g. mozjpeg) that declare a lower minimum.

### Development Setup

1. **Install project tooling and git hooks**:

   ```bash
   mise install   # fetches uv + hk and wires the git hooks (runs `hk install`)
   ```

2. **Available tasks**:
   - `mise run fix` — format + lint fix (run before committing)
   - `mise run check` — CI-style read-only checks
   - `mise run test` — run all tests
   - `mise tasks` — list every available task

   The pre-commit hook runs `mise run pre-commit` automatically on `git commit`. The pre-push hook runs `mise run test` on `git push`.

### Setup

1. **Fetch vendored sources**:

   ```bash
   git submodule update --init --recursive
   ```

2. **Install Python dependencies**:

   ```bash
   uv sync  # Creates .venv with pillow, imagehash, numpy
   ```

3. **Download benchmark datasets**:

   ```bash
   ./bench setup              # All datasets

   # Or set up specific datasets:
   ./bench setup -d kodak         # KODAK (24 natural photos)
   ./bench setup -d div2k         # DIV2K (20 diverse 2K/4K, ~3.5GB download)
   ./bench setup -d pathological  # Synthetic stress tests
   ./bench setup -d test          # Single legacy test image
   ./bench setup -d clic2025      # CLIC 2025 (codec-corpus submodule)
   ./bench setup -d cid22         # CID22 validation refs (codec-corpus submodule)
   ./bench setup -d screen        # GB82-SC real screen content (codec-corpus submodule)
   ./bench setup -d tecnick       # Tecnick SAMPLING (~614MB download)

   # Other options:
   ./bench setup --force          # Force re-download/regenerate
   ./bench setup --verify-only    # Check integrity only
   ```

   > **Note:** `./bench run` automatically sets up required datasets on first use, so an explicit `./bench setup` step is optional. The `clic2025`/`cid22`/`screen` sets live in the [`imazen/codec-corpus`](https://github.com/imazen/codec-corpus) submodule and are fetched on demand via `git submodule update --init --depth 1 vendor/codec-corpus` (run automatically by setup). To fetch only those three subdirs instead of the full ~600MB corpus: `git -C vendor/codec-corpus sparse-checkout set clic2025/final-test CID22/CID22-512/validation gb82-sc`.

4. **Build implementations** (vendored libraries + all implementations built automatically via `./bench compile`)

### Running Benchmarks

The benchmark is **quality-first**: a single **`./bench run`** sweeps the *same* operating points for every implementation and scores quality at each, then *optionally* layers rigorous performance timing on top. Raw speed means little without the quality it trades for, so quality is primary and performance is an overlay.

- **Encoders** are swept across their quality/effort axis (e.g. JPEG quality, JXL distance) and scored with **file size + bits-per-pixel + SSIMULACRA2 + PSNR + SSIM + Butteraugli** against the source — a rate-distortion curve ([issue #8](https://github.com/justin13888/image-evaluation/issues/8)). **Lossless encoders** (PNG, lossless JXL/WebP) have no such tradeoff (output is pixel-identical), so they are swept over their *compression-effort* axis and ranked by file size in a dedicated lossless efficiency view ([issue #26](https://github.com/justin13888/image-evaluation/issues/26)).
- **Decoders** are swept across the same axis of reference-encoded inputs and scored on **decode speed** and **PSNR vs the format's golden (reference) decoder** of the same input — isolating decoder fidelity from the encoder loss both share. A bit-exact decoder scores ∞; an approximate decode path shows a finite PSNR.
- Every operating point also records a one-pass *relative* time ([issue #29](https://github.com/justin13888/image-evaluation/issues/29)).

`--perf {off,anchor,all}` selects the optional rigorous (hyperfine, compute-only) timing overlay:

- **`off`** — quality only, relative one-pass times (fastest);
- **`anchor`** *(default)* — rigorous timing at each implementation's preset point, plus the relative-time curve across the whole sweep;
- **`all`** — rigorous timing at **every** operating point, across both threading modes (most thorough; runtime grows with the number of points).

IQA metrics come from the [`iqa`](https://crates.io/crates/iqa) crate via the published [`iqa-cli`](https://crates.io/crates/iqa-cli) binary. `--formats` filters formats; `--mode {encode,decode,both}` narrows to encoders and/or decoders. Backward-compatible aliases map onto `run`: **`./bench quality`** = `run --perf off`, **`./bench perf`** = `run --perf all`, **`./bench all`** = `run` (anchor).

`--decode-steps` sets how many input bitrates the decoder sweep uses (default **3**, sampled from the reference encoder's range): decode cost/fidelity is ~flat across bitrate, so a few points characterize it without re-timing every quality level — pass `--decode-steps 0` to restore the full encoder axis. Two opt-in suites add axes the main sweep deliberately holds fixed, each as an extra subfolder in the bundle:

- **`--scaling`** times encode/decode on a controlled, downscale-only resolution ladder (same content, only pixels vary) and fits a per-codec `time ∝ pixels^k` exponent — exposing super-linear codecs (AVIF/AV1, `k > 1`) vs linear ones (JPEG, `k ≈ 1`), which a native-resolution sweep cannot show.
- **`--effort`** sweeps each lossy codec's *pinned* effort/speed knob (AVIF `speed`, JXL `effort`, WebP `method`) at fixed quality on a ~1 MP downscale, tracing the time↔size↔quality tradeoff that the rate-distortion sweep otherwise fixes at one preset.

```bash
# Quick smoke test (2 quality points per impl, anchor timing, all-cores only)
./bench run --dataset kodak --sample 3 --quick

# Demo: a fast, deliberately NON-rigorous sweep that still fills EVERY section of
# the HTML report (rate-distortion, lossless, decoder fidelity, BD-rate, plus the
# performance / scaling / effort overlays and the image gallery). It implies
# --quick and turns on --scaling + --effort, defaults --sample to 2 and --jobs to
# all logical cores, and keeps --perf anchor: a single run per point at maximum
# parallelism — meant for demonstrating the report, not for accurate numbers.
./bench run --demo                  # built-in 'test' dataset; finishes quickly
./bench run --demo --dataset kodak  # a few real images for richer curves

# Full quality-first sweep + rigorous anchor timing (the default)
./bench run --dataset kodak

# Quality only, no rigorous timing (fastest iteration)
./bench run --dataset kodak --perf off

# Rigorous timing at EVERY operating point (most thorough; much longer)
./bench run --dataset kodak --perf all

# Fewer, evenly-sampled points; subset of formats; encoders only
./bench run --dataset kodak --quality-steps 5 --formats jpeg jxl --mode encode

# Secondary-knob coverage: 'variants' (lean default — only codec-signature
# variants) → 'all' (every enum/bool knob one-at-a-time; --full is shorthand);
# '--params axis' restores the legacy quality-axis-only sweep
./bench run --dataset kodak --full

# Peak memory during the timing overlay; override inner-loop iters/warmup (default 10/2)
./bench run --dataset div2k --measure-memory --iterations 20 --warmup 3

# Comprehensive clic2025 run sized for ~8h on ~8 cores: fewer quality points and a
# trimmed decoder sweep keep the matrix feasible, while --scaling and --effort add
# the resolution-scaling and effort/speed characterizations in the same bundle
# TODO: refresh this estimate — it predates the --perf-images bound. A default
# `./bench run -d clic2025` is now ~1h (quality matrix ~20-30 min + a bounded
# rigorous-timing overlay ~25-45 min); the overlay, not the matrix, is the lever.
./bench run --dataset clic2025 --quality-steps 6 --decode-steps 3 --scaling --effort

# Resolution-scaling characterization on its own (time vs pixels, fitted exponent)
./bench run --dataset clic2025 --perf off --scaling --scaling-images 3

# Backward-compatible aliases
./bench quality --dataset kodak     # = run --perf off
./bench perf --dataset kodak        # = run --perf all
./bench all --dataset kodak         # = run (anchor)

# --- Shared ---
./bench compile          # build vendored libs + all implementations + install iqa-cli
./bench docs             # regenerate docs/tunables.md (--check verifies it in CI)
./bench clean            # remove build artifacts and results
```

> **Runtime note:** the always-on metric pass scales with operating points × images × implementations; `--quality-steps`, `--decode-steps`, `--sample`, `--formats`/`--mode`, `--params`, and `--quick` all cap it. `--params variants` (default) adds only the few **codec-signature** variants that expose a codec's unique behavior (mozjpeg `trellis-off`, jpegli `xyb`, libjxl `modular-on`); `--params all` (or the `--full` shorthand) restores the full one-at-a-time expansion — every chroma/progressive/filter/integer-quality series (heavier), while `--params axis` is the leanest (quality axis only). The optional `--perf all` overlay re-times every point across both thread modes — the largest cost multiplier — while the default `--perf anchor` times just one point per implementation. Use `--perf off` for the fastest quality-only iteration. To populate **every** report section in one fast pass (for a demo, not for accurate numbers), `--demo` bundles the smallest complete-coverage settings: it implies `--quick --scaling --effort`, defaults `--sample 2` and `--jobs` to all logical cores, and keeps `--perf anchor`.

### Cleanup

```bash
./bench clean
```

### Results

Every run writes a **bundle** to `./results/<timestamp>/` containing a `quality/` subfolder (always), a `performance/` subfolder (whenever `--perf` is not `off`), and `scaling/`/`effort/` subfolders (when `--scaling`/`--effort` are given), plus a top-level index and a self-contained report:

```
results/<timestamp>/
├── report.html        # self-contained, opens offline (interactive quality + overlays)
├── summary.md         # index linking the per-pass summaries
├── manifest.json      # bundle metadata (which passes ran)
├── quality/           # always: metrics.json, summary.md (tables), manifest.json
├── performance/       # when --perf != off: raw.json, summary.md, timing charts, memory.csv
├── scaling/           # when --scaling: raw.json, summary.md, time-vs-pixels charts (fitted k)
├── effort/            # when --effort: metrics.json, summary.md, effort-tradeoff charts
└── assets/            # when --report-images (default): the exact image behind every data point
```

**`assets/`** — with `--report-images` (on by default) the quality pass keeps the **exact encoded artifact** of every result instead of discarding it, so each data point in the report is clickable into the images it aggregates. The tree mirrors a data point's identity, `assets/<format>/<impl>/<label>/<image>.<ext>` for an encoder's output (e.g. `assets/jpeg/mozjpeg-encode/quality-80/<hash>.jpg`); a decoder's input bitstream — identical across every decoder of it — is shared under `assets/<format>/_inputs/<label>/`, and each original is stored once (lossless PNG) under `assets/_sources/`. Bytes are kept verbatim with no transcoding, so a `.jxl` stays `.jxl` (it renders only in a JXL-capable browser). Pass `--no-report-images` to skip this (smaller bundle, the pre-feature behavior) for very large sweeps.

**`quality/`** — `metrics.json` (per impl/format/operating-point/image: `filesize`, `bpp`, `ssimulacra2`, `psnr`, `ssim`, `butteraugli`, `metric_basis` (`"source"` for encoders, `"golden"` for decoders), `time_s` (single relative one-pass time, encode or decode), dimensions, the swept `quality_axis`/`quality_value`, and a `lossless` flag) is the raw data everything else is recomputed from; `summary.md` (BD-rate + Pareto best-of-format + lossless efficiency + **decoder fidelity & speed** + per-step metrics tables, linking to `report.html` for the curves); and `manifest.json` (`suite: quality` with the exact per-impl `quality_sweeps`). The metric pass renders **no chart PNGs** — its rate-distortion curves are interactive in `report.html`.

**`performance/`** — the optional rigorous-timing overlay. `raw.json` (full Hyperfine output: `mean/median/stddev/min/max`, `times[]`, `exit_codes[]`), `summary.md` (timing table + grouped single-vs-all-cores charts, one per format/operation), timing `*.png`, `manifest.json` (`suite: performance`, with the `perf` mode), and `memory.csv` (with `--measure-memory`).

**`report.html`** is a single offline-friendly file, **quality-first**. The quality view is primary and interactive: the full `metrics.json` is embedded inline and the rate-distortion curves are drawn client-side as SVG (no third-party JS) — per-format charts plus a combined cross-format Pareto chart of the best encoders, with metric (SSIMULACRA2/PSNR/SSIM/Butteraugli) and linear/log-x toggles, hover tooltips, a sortable BD-rate table, a **lossless compression-efficiency** section (bpp leaderboard + size-vs-effort chart), and a **decoder fidelity & speed** section (decode time + PSNR vs the golden decoder). Every operating point's encode/decode **time** is also a visible dimension ([issue #46](https://github.com/justin13888/image-evaluation/issues/46)): on the rate-distortion, Pareto and lossless-effort charts time is the point's **bubble size** (bigger = slower), and the decoder section adds a **speed-vs-bitrate scatter** (decode time vs input bpp, approximate-decode points ringed). A per-section **Show time** toggle (default on) hides the time dimension when you want the rate-distortion shape on its own. **Clicking a data point** (or focusing a chart and pressing Enter, with arrow keys to step between points) opens an in-report **lightbox** of the exact images aggregated into that point — loaded lazily from `assets/` by relative URL (never embedded, so `report.html` stays small) with an original-vs-reconstruction toggle and per-image metrics. The rigorous-timing overlay's charts (embedded as base64 PNGs) follow below it as the secondary view. Because the raw data is embedded, anything in the quality view can be recomputed from the report alone.

### Publishing the report

The `report.html` is self-contained and static, so a bundle can be hosted on **[Cloudflare Pages](https://pages.cloudflare.com/)**. The whole bundle directory is uploaded — `report.html` **plus the `assets/` image tree** it links to — so the gallery works on the live site. The `deploy-report` task picks a bundle, runs pre-flight checks, stages an `index.html` (so the site root serves the report), and uploads it with the [wrangler](https://developers.cloudflare.com/workers/wrangler/) CLI (provisioned by `mise install`).

Cloudflare Pages' free tier allows **20,000 files per deployment** and **25 MiB per file** (no total-size or bandwidth cap); the deploy task fails fast if a bundle would exceed either. A default `clic2025` run lands within both — ≈10k image files (about half the file budget) and a largest file of ~10 MiB (a near-raw lossless PNG of a ~3 MP photo) — though, because the images are kept verbatim, the total is sizeable (≈12 GB, dominated by the lossless points). It uploads free, but if a larger dataset/sweep pushes past the 20k-file limit (or you want a lighter deploy), re-run with `--no-report-images` or deploy a `--sample`d subset.

**One-time Cloudflare setup:**

1. Create a [Cloudflare account](https://dash.cloudflare.com/sign-up).
2. Create a Pages project named `image-evaluation` (the default name). Either:
   - **Dashboard:** Workers & Pages → Create → Pages → **Upload assets** (Direct Upload), and name it `image-evaluation`; or
   - **CLI:** `wrangler pages project create image-evaluation --production-branch master`.
3. Authenticate wrangler, either:
   - **Interactive:** `wrangler login` (opens a browser), or
   - **Headless/CI:** export `CLOUDFLARE_API_TOKEN` (a token with the *Cloudflare Pages → Edit* permission) and `CLOUDFLARE_ACCOUNT_ID`.

**Configure (optional)** — all have defaults; override via the environment or a gitignored `mise.local.toml` `[env]` block:

```bash
export CF_PAGES_PROJECT=my-project   # Pages project name (default: image-evaluation)
export CF_PAGES_BRANCH=master        # production branch    (default: master)
```

**Deploy:**

```bash
mise run deploy-report                            # newest bundle under results/
mise run deploy-report results/20260614_164319    # a specific bundle
```

The task fails fast with an actionable message if the bundle or its `report.html` is missing, if `wrangler` isn't installed, or if you are not authenticated. On success wrangler prints the live `*.pages.dev` URL, whose root loads the report.

## Methodology

### Input Generation

The benchmarks use a tiered collection of images to test different performance characteristics. You select which dataset to use via the `--dataset` flag when running benchmarks.

#### Available Datasets

1. **KODAK (`--dataset kodak`)** — [KODAK PhotoCD dataset](http://r0k.us/graphics/kodak/) (24 images, ~0.4MP each)
   * L2/L3 cache resident images
   * Tests raw instruction throughput and vectorization efficiency
   * Natural photography with varied content

2. **DIV2K (`--dataset div2k`)** — [DIV2K dataset](https://data.vision.ee.ethz.ch/cvl/DIV2K/) (20 selected images, 2K/4K resolution)
   * Selected via perceptual-hash diversity sampling (`_select_diverse_images`)
   * Tests memory bandwidth, allocator pressure, and large buffer performance
   * High-resolution, diverse content

3. **CLIC 2025 (`--dataset clic2025`)** — [CLIC 2025](https://clic2025.compression.cc/) final-test set (30 images, ~2048px) — _Unsplash License_
   * Modern high-resolution photographic content; the current de-facto codec-evaluation corpus
   * Vendored via the `imazen/codec-corpus` submodule (auto-initialized by setup)

4. **CID22 (`--dataset cid22`)** — [CID22](https://cloudinary.com/labs/cid22) validation references (41 images, 512px) — _CC BY-SA 4.0_
   * The dataset SSIMULACRA2 (this benchmark's primary quality metric) was validated against — perceptually authoritative
   * Vendored via the `imazen/codec-corpus` submodule

5. **Tecnick (`--dataset tecnick`)** — [TESTIMAGES SAMPLING](https://testimages.org/) (24 diverse 1200×1200 images) — _CC BY-NC-SA 4.0_
   * Classic higher-resolution supplement to KODAK; diversity-selected from 100 sources
   * Downloaded on demand (~614MB archive); **not** redistributed — its non-commercial license keeps it out of the repo

6. **Screen content (`--dataset screen`)** — [GB82-SC](https://github.com/imazen/codec-corpus) (10 real screenshots/UI/text images) — _CC0_
   * Screen content compresses very differently from photos; complements the synthetic `pathological/screenshot_4k.png`
   * Vendored via the `imazen/codec-corpus` submodule

7. **Pathological (`--dataset pathological`)** — Synthetic stress tests (4 images)
   * `solid_4k.png` — Solid color (tests RLE/skip optimizations)
   * `noise_4k.png` — Gaussian noise (worst-case for all compressors)
   * `screenshot_4k.png` — UI screenshot with text and flat regions
   * `alpha_gradient_4k.png` — Transparency gradient (for formats supporting alpha)

8. **Test (`--dataset test`)** — Single test file (legacy, minimal coverage)
   * For quick smoke tests only
   * Not recommended for comprehensive benchmarking

**Preparation Phase:**

* **For Encoding:** Images are taken as-is and converted to raw PPM (RGB24) or PAM (RGBA32) format.
* **For Decoding:** Images are pre-encoded using the **reference implementation** of the corresponding format at that encoder's fixed performance preset.

#### Dataset Selection Strategy

Choose your dataset based on your benchmarking goals:

* **Performance Optimization (`kodak`)** — Best for micro-optimizations and instruction-level tuning. Images fit in cache, minimizing memory system variance.
* **Real-World Throughput (`div2k`)** — Best for measuring production performance. Tests memory bandwidth, allocator efficiency, and scaling behavior.
* **Perceptual Quality / Rate-Distortion (`cid22`)** — Best for trustworthy quality comparisons: it is the SSIMULACRA2 validation corpus, so RD curves on it are perceptually well-calibrated.
* **Modern Photographic Content (`clic2025`)** — Best for representative results on contemporary high-resolution photos; the current community-standard codec-evaluation set.
* **Screen Content (`screen`)** — Best when codecs target UI/text/graphics, which compress very differently from photos.
* **Higher-Resolution Photography (`tecnick`)** — A classic 1200×1200 supplement to KODAK for resolution sensitivity.
* **Edge Case Validation (`pathological`)** — Best for finding corner cases, testing worst-case performance, and validating optimizations don't break on synthetic inputs.
* **Quick Validation (`test`)** — Single-image smoke tests only. Not suitable for performance comparison.

**Recommendation:** Run `kodak` for initial development and optimization work, then validate quality on `cid22`/`clic2025` (and `screen` for screen-content codecs) and robustness on `div2k`/`pathological` before publishing results.

**Licensing:** dataset images carry their own licenses (see each entry above). `cid22` is CC BY-SA 4.0 — attribute Cloudinary's CID22 when redistributing results derived from it. `tecnick` is CC BY-NC-SA 4.0 (non-commercial); it is downloaded on demand and never committed to this repository.

### Tunables & Operating Points

Each implementation declares its tunable knobs in a per-implementation schema in `bench_lib/models.py` (`TUNABLE_SCHEMAS`). The orchestrator passes the chosen values to the binary as generic `--param key=value` flags; the binary reads only the keys it understands. The schema defines two things per encoder:

- **`perf_preset`** — the single fixed operating point the **performance** suite uses (one set of params per codec). Presets are not quality-matched across implementations.
- **`quality_axis` + `quality_sweep`** — the knob the **quality** suite sweeps and the discrete values it steps through. For a **lossy** encoder this traces a rate-distortion curve (e.g. JPEG `quality`, JXL `distance`); for a **lossless** encoder (`lossless: true` — PNG, lossless JXL) it is instead a *compression-effort* axis tracing size-vs-effort. Knob-less lossless encoders (spng, image-webp) declare `lossless` with no axis and contribute a single operating point. Decoders have no axis.
- **`variants`** — *secondary* operating points that exercise implementation-specific knobs beyond the quality axis. Each variant is its own series — a distinct `impl@knob-value` reusing the same binary, so it gets its own rate-distortion curve without polluting the base. The lean **default** (`--params variants`) keeps only the few **codec-signature** variants that reveal a codec's unique behavior — jpegli `xyb`, libjxl `modular-on`, mozjpeg `trellis-off` (and jpegli's base runs in its faster native **distance** mode). Universal knob sweeps that behave the same across every codec — chroma `subsampling=444`/`422`/`440`, sequential/progressive scan, AVIF 4:4:4, JXL progressive, PNG filter modes, and jpegli's redundant integer-quality path — are demoted to `--params all` (shorthand: `--full`). `--params axis` sweeps the quality axis only. Knobs that are deliberately *not* swept carry a `skip_reason` in the schema so the decision is recorded.

The authoritative per-implementation overview — every knob, swept axis, variant series, and intentionally-skipped knob with its reason — is **generated** from `TUNABLE_SCHEMAS`:

> 📋 **[`docs/tunables.md`](docs/tunables.md)** — run `./bench docs` to regenerate it, or `./bench docs --check` to verify it is in sync (also enforced in CI).

> **Known limitations:**
> - **AVIF film grain synthesis** is not yet implemented in `libavif` or `rav1e` (a TODO is tracked in each).
> - **image-webp** (Rust) only supports lossless WebP encoding (crate limitation), so it has no effort knob — it contributes a single lossless operating point to the compression-efficiency view.
> - **spng** (C++) does not expose a compression-level control, so it too contributes a single lossless operating point.

### Benchmarking Architecture

To ensure statistically significant results and eliminate "Cold Start" bias (OS process spawning, dynamic linker loading), we use a hybrid approach:

1. **The Harness (Hyperfine):** Manages the statistical runs, warmup, and outlier detection.
2. **The Binary (Internal Loop):** Performs the actual decode/encode operation N times within a single process.

#### Binary Interface

Every encoder/decoder implementation is compiled into a standalone binary implementing this uniform CLI. The orchestrator passes codec tunables as repeated `--param key=value` flags (the binary ignores keys it does not understand), sweeps `--threads` for the rigorous-timing overlay, and sets `--discard` for timing runs:

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

The metric pass measures the fidelity of each output using the [`iqa`](https://crates.io/crates/iqa) crate (via the published [`iqa-cli`](https://crates.io/crates/iqa-cli) binary), reporting **SSIMULACRA2** (perceptual; 100 = identical), **PSNR** (dB), **SSIM** (structural similarity; 1.0 = identical, higher is better) and **Butteraugli** (perceptual difference; 0 = identical, **lower** is better). Because `iqa` consumes raw pixels, an **encoder's** output is first decoded back to PPM with the format's reference decoder and then compared to the **source** (`metric_basis: "source"`). A **decoder** is instead scored against the format's **golden (reference) decoder** of the same input (`metric_basis: "golden"`), isolating decoder fidelity from the encoder loss both share — a bit-exact decoder scores ∞, an approximate decode path a finite PSNR.

> [!IMPORTANT]
> **IQA metrics are approximations, not ground truth.** Every image-quality metric encodes its own model of the human visual system, and each comes with assumptions and blind spots:
>
> - **SSIMULACRA2** is a perceptual estimator calibrated against subjective datasets at *specific* viewing conditions (display resolution, brightness, viewing distance). It can mis-rank distortions it was not tuned for and is not guaranteed to be monotonic with perceived quality near the high-fidelity (near-lossless) end of the scale.
> - **PSNR** measures pixel-wise error only and is well known to correlate poorly with human perception — it cannot see structure, texture masking, or color sensitivity.
> - **SSIM** compares local structure, luminance, and contrast (higher is better, 1.0 = identical). It is more perceptual than PSNR but still a relatively simple model that can miss color and high-frequency artifacts.
> - **Butteraugli** (derived from libjxl) estimates the perceptual difference between two images, with **lower** meaning closer to identical (0 = identical). Like the others it encodes specific assumptions and can disagree with human judgement on content it was not tuned for.
>
> These are all *automated, full-reference* metrics: they compare against the source pixels and say nothing about aesthetic quality, artifact *annoyance*, or content the metric was never trained on (e.g. text, screenshots, medical or satellite imagery). **Aggregate scores (BD-rate, Pareto fronts) can be sensitive to the metric, dataset, and operating points chosen, and a few points of SSIMULACRA2 may not be perceptible.** Treat these results as a reproducible *guide* for narrowing options, **not** as a substitute for a properly controlled human subjective study (e.g. MOS/2AFC) when determining the genuinely best-looking option for a given use case.

#### Discard Checksum

**Rigorous timing is always compute-only:** the `--perf` overlay invokes every binary with `--discard`, removing filesystem-write variance as a confound. (The always-on metric pass runs each binary *without* `--discard` so it writes a real file to size and score.)

When `--discard` is set, output bytes are fed through a CRC32 checksum to prevent compiler elimination of the encode/decode work. The C/C++ harness uses zlib's `crc32()` function; the Rust harness uses `crc32fast::Hasher`. Both libraries select hardware-accelerated implementations (e.g. SSE4.2, ARM CRC32) where available at compile time.

#### Baseline Measurement

The benchmark suite includes a `null` operation binary that performs only:

1. Read input file into memory
2. Compute CRC32 checksum
3. (If not `--discard`) Write buffer to output

This establishes the I/O and measurement floor, allowing you to isolate codec overhead from system overhead.

### Threading Model

The **rigorous-timing overlay** (`--perf`) measures the two extremes — the most accurate and the fastest — and they appear side-by-side in the timing charts:

1. **Single-threaded (`--threads 1`) — most accurate:** measures per-core efficiency, and is **fixed to one dedicated CPU core** (`taskset`) so the repeated-trial number is reproducible. This is the timing merged back onto the rate-distortion / decoder charts (whiskered, "N runs ±σ" on hover).
2. **Parallel (`--threads 0`) — fastest:** uses **every logical core**, unpinned, to measure true peak throughput for batch processing.

**CPU pinning (default on, Linux).** All performance-critical measurements are fixed to one CPU core at a time. The always-on **metric pass** does not sweep threads — output bytes are thread-invariant — and runs one single-threaded task per core across a pool of **physical cores − 1** (core 0 is reserved for the OS/IO so it can't perturb a measurement), with **each task pinned to its own dedicated core** (`sched_setaffinity`; child harness, reference decode, and `iqa-cli` all inherit it). The single-threaded timing overlay is pinned to one fixed core as above; the all-cores mode is deliberately left unpinned. `--no-pin-cores` disables pinning (and falls back automatically where `taskset`/affinity is unavailable, e.g. containers/macOS). `--demo` turns pinning **off** and uses all logical cores to maximize throughput for a fast functional check — a demo relaxation that never changes the non-demo defaults. The manifest records `assigned_cores` / `reserved_core` / `timing_core` for reproducibility.

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
    "quick": false,
    "pin_cores": true,
    "assigned_cores": [1, 2, 3, 4, 5, 6, 7],
    "reserved_core": 0,
    "timing_core": 7,
    "physical_cores": 8,
    "logical_cores": 16
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
| **zenjpeg**       | Rust     | Pure-Rust [jpegli](https://github.com/libjxl/libjxl/tree/main/lib/jpegli) port from [imazen/zenjpeg](https://github.com/imazen/zenjpeg). **AGPL-3.0.** Encoder (quality/progressive/subsampling) and decoder (decoder is prerelease). |

### PNG

| Implementation | Language | Notes                                     |
| :------------- | :------- | :---------------------------------------- |
| **libpng**     | C        | Reference implementation                  |
| **spng**       | C        | "Simple PNG", speed-optimized. *Encoder does not expose a compression-level control.* |
| **png**        | Rust     | Standard `image-rs` crate                 |
| **zune-png**   | Rust     | Highly optimized pure Rust implementation |
| **zenpng**     | Rust     | Pure-Rust lossless codec from [imazen/zenpng](https://github.com/imazen/zenpng). **AGPL-3.0.** Encoder + decoder; swept over its 0–200 compression-effort axis (`zopfli` feature off, so the multi-minute high-effort presets are excluded from the sweep). |
| **oxipng**     | Rust     | Lossless PNG optimizer from [oxipng/oxipng](https://github.com/oxipng/oxipng) (**MIT**). Encoder-only; built single-threaded (`parallel` feature off) and encodes optimized PNGs directly from raw pixels, swept over its optimization-level axis (`-o 0..6`, `max`). Zopfli backend and Adam7 interlacing are `--full`-only variants. |

### WEBP

| Implementation | Language | Notes                         |
| :------------- | :------- | :---------------------------- |
| **libwebp**    | C        | Reference implementation      |
| **image-webp** | Rust     | *Lossless-only (crate limitation) — no quality axis; contributes a single lossless operating point to the compression-efficiency view.* |
| **zenwebp**    | Rust     | Pure-Rust WebP from [imazen/zenwebp](https://github.com/imazen/zenwebp). **AGPL-3.0.** Lossy VP8 (quality + method) and lossless VP8L (separate `zenwebp-lossless` series) encoders, plus a decoder. |

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

6. **IQA metrics are approximations.** SSIMULACRA2, PSNR, SSIM and Butteraugli are automated estimators of perceived quality, each with its own assumptions and blind spots (see [Image Quality Assessment](#image-quality-assessment)). They are a reproducible guide for narrowing options, **not** a replacement for a controlled human subjective study (e.g. MOS) when determining the genuinely best-looking option.

7. **imazen "zen" implementations — AGPL + integration caveats** *(as of 2026-06-11)*. The `zen*` implementations ([zenjpeg](https://github.com/imazen/zenjpeg), [zenpng](https://github.com/imazen/zenpng), [zenwebp](https://github.com/imazen/zenwebp), [zenjxl](https://github.com/imazen/zenjxl)) are **AGPL-3.0** ([issue #34](https://github.com/justin13888/image-evaluation/issues/34)). The harness and repository remain MIT-licensed, but any benchmark binary that links a `zen*` library is an AGPL-derived work, so redistributing built binaries must honour the AGPL. Two integration caveats apply as of this date — see [`docs/zen-integration.md`](docs/zen-integration.md) for the live status:
   - **zenjxl is blocked** and not yet built: `zenjxl 0.2.1` requires `jxl-encoder ^0.3.2`, which is not published to crates.io (max published is 0.3.1), so the workspace cannot resolve it. Re-check once `jxl-encoder 0.3.2` is released.
   - **zenavif was dropped**: it is a thin wrapper over the pure-Rust `rav1d-safe` decoder, whose multithreaded CDEF SIMD path panics intermittently (`overlapping DisjointMut` in `cdef_arm.rs`) on AVIF decode. AVIF is already covered by libavif/rav1e/SVT-AV1 and the dav1d/rav1d/libgav1 decoders, so the wrapper added flakiness without coverage. Re-add once the upstream `rav1d-safe` race is fixed.

## Contributing

Contributions are welcome!

* **New Implementations:** Must implement the standard CLI defined in "Benchmarking Architecture".
* **Optimization:** If you find flags or methods that improve a specific implementation, open a PR with benchmark results and updated manifest.
* **Image Sets:** Proposals for additional pathological or domain-specific test images are welcome.
* Run `mise run fix` before committing, or let the pre-commit hook handle it automatically.
* CI runs `mise run check` and `mise run test` on all PRs.
