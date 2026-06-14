# Implementation tunables & operating points

> **Generated** by `./bench docs` from `TUNABLE_SCHEMAS` in `bench_lib/models.py`. Do not edit by hand — run `./bench docs` to regenerate, and `./bench docs --check` verifies it is in sync.

Every encoder is swept across one **quality/effort axis** (a rate-distortion curve for lossy encoders, a size-vs-effort curve for lossless ones). Secondary knobs are tested as distinct **variant series** (`impl@knob-value`, reusing the same binary): the **curated** set runs by default (`./bench run`, i.e. `--params variants`); `--params all` additionally sweeps every remaining enum/bool knob one-at-a-time; `--params axis` restores the legacy axis-only sweep. A knob marked † (and every row in *Intentionally not swept*) is deliberately held fixed — the reason is recorded so the decision lives in code (issue #4).

## Encoders

### JPEG

| Encoder | Lang | Swept axis | Curated variants (default) | `--params all` adds | Other knobs read |
|---|---|---|---|---|---|
| `jpeg-encoder-encode` | rust | `quality` — 10→95 (11 pts, quality) | `subsampling-444` | `progressive-false` | `progressive`=true, `subsampling`=420 |
| `image-jpeg-encode` | rust | `quality` — 10→95 (11 pts, quality) | — | — | — |
| `zenjpeg-encode` | rust | `quality` — 10→95 (11 pts, quality) | `subsampling-444` | `progressive-false` | `progressive`=true, `subsampling`=420 |
| `libjpeg-turbo-encode` | c | `quality` — 10→95 (11 pts, quality) | `subsampling-444`, `progressive-off` | — | `progressive`=true, `subsampling`=420 |
| `mozjpeg-encode` | c | `quality` — 10→95 (11 pts, quality) | `subsampling-444`, `progressive-off`, `trellis-off` | — | `progressive`=true, `subsampling`=420, `trellis`=true |
| `jpegli-encode` | c++ | `quality` — 10→95 (11 pts, quality) | `subsampling-444` | `progressive-false` | `progressive`=true, `subsampling`=420 |

### PNG

| Encoder | Lang | Swept axis | Curated variants (default) | `--params all` adds | Other knobs read |
|---|---|---|---|---|---|
| `image-png-encode` | rust | `compression` — fast→best (3 pts, effort, lossless) | `filter-none` | `filter-sub`, `filter-up`, `filter-avg`, `filter-paeth` | `filter`=adaptive |
| `zune-png-encode` | rust | `effort` — 0→9 (10 pts, effort, lossless) | — | — | — |
| `zenpng-encode` | rust | `effort` — 0→24 (9 pts, effort, lossless) | — | — | — |
| `libpng-encode` | c | `compression` — 0→9 (10 pts, effort, lossless) | — | — | — |
| `spng-encode` | c | — (single lossless point) | — | — | — |

### WEBP

| Encoder | Lang | Swept axis | Curated variants (default) | `--params all` adds | Other knobs read |
|---|---|---|---|---|---|
| `image-webp-encode` | rust | — (single lossless point) | — | — | — |
| `zenwebp-encode` | rust | `quality` — 10→95 (10 pts, quality) | — | — | `method`=4 |
| `zenwebp-lossless-encode` | rust | `method` — 0→6 (7 pts, effort, lossless) | — | — | `quality`=100 |
| `libwebp-encode` | c | `quality` — 10→95 (10 pts, quality) | — | — | `method`=4†, `lossless`=false† |
| `libwebp-lossless-encode` | c | `method` — 0→6 (7 pts, effort, lossless) | — | — | `quality`=100, `lossless`=true† |

### AVIF

| Encoder | Lang | Swept axis | Curated variants (default) | `--params all` adds | Other knobs read |
|---|---|---|---|---|---|
| `rav1e-encode` | rust | `quality` — 20→90 (8 pts, quality) | `chroma-444` | — | `speed`=6†, `chroma`=420 |
| `libavif-encode` | c | `quality` — 20→90 (8 pts, quality) | `yuv-444` | — | `speed`=6†, `yuv`=420 |
| `svt-av1-encode` | c | `quality` — 20→90 (8 pts, quality) | — | — | `speed`=6†, `yuv`=420 |

### JXL

| Encoder | Lang | Swept axis | Curated variants (default) | `--params all` adds | Other knobs read |
|---|---|---|---|---|---|
| `zune-jpegxl-encode` | rust | `quality` — 40→100 (8 pts, quality) | — | — | `effort`=7 |
| `libjxl-encode` | c++ | `distance` — 15.0→0.1 (15 pts, quality) | `progressive-on`, `modular-on` | `progressive-0`, `modular-0` | `effort`=7, `progressive`=-1, `modular`=-1, `responsive`=-1†, `progressive_dc`=-1†, `decoding_speed`=0† |
| `libjxl-lossless-encode` | c++ | `effort` — 1→9 (9 pts, effort, lossless) | — | — | `distance`=0.0 |

## Intentionally not swept (documented as irrelevant / deferred)

Knobs the implementation *reads* but is deliberately pinned (`pinned`), plus library features deliberately never wired (`not wired`).

| Implementation | Knob(s) | Status | Why |
|---|---|---|---|
| `mozjpeg-encode` | `optimize_scans` | not wired | irrelevant: scan-order micro-opt, not RD-relevant here |
| `mozjpeg-encode` | `dc_scan_opt_mode` | not wired | irrelevant: DC scan tuning, marginal vs quality |
| `mozjpeg-encode` | `base_quant_tbl_idx` | not wired | irrelevant: alternate quant tables, niche |
| `libwebp-encode` | `method` | pinned (wired) | effort/speed knob — covered by the performance overlay, not a default RD series |
| `libwebp-encode` | `lossless` | pinned (wired) | mode toggle: lossless WebP is a distinct pipeline (see image-webp-encode), not a knob on the lossy RD curve |
| `libwebp-encode` | `filter/sns/segments/near_lossless/use_sharp_yuv/preprocessing` | not wired | irrelevant: large WebPConfig tail with low RD value vs quality; alpha/target-size knobs N/A for opaque RGB / deterministic runs |
| `libwebp-lossless-encode` | `lossless` | pinned (wired) | pinned on: this series IS the lossless VP8L pipeline (lossless=false would just duplicate libwebp-encode's lossy VP8) |
| `rav1e-encode` | `speed` | pinned (wired) | speed is a quality/throughput trade — covered by the performance overlay, not a default RD series |
| `rav1e-encode` | `tune` | not wired | deferred: Psnr-vs-Psychovisual tuning entangles the IQA metric choice |
| `rav1e-encode` | `tiling` | not wired | irrelevant: tile_rows/tile_cols affect parallelism, not RD |
| `rav1e-encode` | `film_grain` | not wired | deferred: needs calibrated noise params (TODO in encode.rs) |
| `libavif-encode` | `speed` | pinned (wired) | speed is a quality/throughput trade — covered by the performance overlay, not a default RD series |
| `libavif-encode` | `codec-specific-options` | not wired | deferred: aom/svt keys (tune/aq-mode/sharpness/denoise) via avifEncoderSetCodecSpecificOption are backend- and version-specific; tracked for follow-up |
| `libavif-encode` | `tiling` | not wired | irrelevant: tileRowsLog2/tileColsLog2 affect parallelism, not RD |
| `libavif-encode` | `min/maxQuantizer` | not wired | deprecated by libavif in favour of `quality` |
| `svt-av1-encode` | `speed` | pinned (wired) | speed is a quality/throughput trade — covered by the performance overlay, not a default RD series |
| `svt-av1-encode` | `codec-specific-options` | not wired | deferred: aom/svt keys (tune/aq-mode/sharpness/denoise) via avifEncoderSetCodecSpecificOption are backend- and version-specific; tracked for follow-up |
| `svt-av1-encode` | `tiling` | not wired | irrelevant: tileRowsLog2/tileColsLog2 affect parallelism, not RD |
| `svt-av1-encode` | `min/maxQuantizer` | not wired | deprecated by libavif in favour of `quality` |
| `libjxl-encode` | `responsive` | pinned (wired) | irrelevant: modular-mode progressive; not RD-relevant for VarDCT photos |
| `libjxl-encode` | `progressive_dc` | pinned (wired) | irrelevant: low-res DC passes barely move final size |
| `libjxl-encode` | `decoding_speed` | pinned (wired) | irrelevant: trades density for decode speed, scored on the encode side here |
| `libjxl-encode` | `epf/gaborish/photon_noise/dots/patches` | not wired | irrelevant: long-tail VarDCT artefact controls; the named progressive/modular levers cover the issue's intent |
| `libjxl-encode` | `color_transform` | not wired | irrelevant: XYB is the correct high-quality lossy path |
| `libjxl-lossless-encode` | `progressive/modular/responsive/decoding_speed` | not wired | wired in the shared libjxl binary but exercised only by the lossy libjxl-encode series; the lossless path uses encoder defaults |

## Decoders (parameterless)

Decoders take no tunables here — they consume the bitstream as-is and are scored against the format's golden (reference) decoder, so a bit-exact decoder scores ∞ and only an approximate path shows a finite PSNR. Library decode knobs that exist but are intentionally not exercised:

| Decoder | Format | Lang | Library knobs not exercised |
|---|---|---|---|
| `jpeg-decoder-decode` | jpeg | rust | — |
| `zune-jpeg-decode` | jpeg | rust | — |
| `zenjpeg-decode` | jpeg | rust | — |
| `libjpeg-turbo-decode` | jpeg | c | dct_method / fancy_upsampling (output forced to 8-bit RGB; scored vs golden) |
| `mozjpeg-decode` | jpeg | c | — |
| `image-png-decode` | png | rust | — |
| `zune-png-decode` | png | rust | — |
| `zenpng-decode` | png | rust | — |
| `libpng-decode` | png | c | — |
| `spng-decode` | png | c | — |
| `image-webp-decode` | webp | rust | — |
| `zenwebp-decode` | webp | rust | — |
| `libwebp-decode` | webp | c | no_fancy_upsampling / dithering (post-processing, not core decode fidelity) |
| `libavif-decode` | avif | c | — |
| `dav1d-decode` | avif | c/asm | apply_grain / inloop_filters (conformance/post-processing, not a fidelity knob here) |
| `libgav1-decode` | avif | c++ | post_filter_mask (post-processing, not a fidelity knob here) |
| `rav1d-decode` | avif | rust | apply_grain / inloop_filters (conformance/post-processing, not a fidelity knob here) |
| `jxl-oxide-decode` | jxl | rust | — |
| `jxl-rs-decode` | jxl | rust | — |
| `libjxl-decode` | jxl | c++ | progressive decode (out of scope: the harness scores full-decode fidelity vs golden; partial/progressive-decode quality needs a different rig) |

