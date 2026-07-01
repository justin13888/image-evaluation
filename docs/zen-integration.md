# imazen "zen" codec integration (issue #34)

Tracking branch for integrating imazen's five AGPL-3.0 pure-Rust image codecs so they
can be benchmarked against the golden (reference C) implementations. Each library was
first developed on its own branch/PR; the buildable ones are now consolidated here.

**Status (2026-06-11):** three buildable libraries — **zenjpeg, zenpng, zenwebp**
(7 implementations total) — are merged into this branch; the full Rust workspace builds
and every encode/decode round-trip passes (PNG and lossless-WebP verified byte-exact).
**zenavif was dropped (2026-06-12)** — it is a thin wrapper over `rav1d-safe`, whose
multithreaded CDEF SIMD path panics intermittently on AVIF decode (see below); AVIF is
already covered by the libavif/rav1e/SVT-AV1 + dav1d/rav1d/libgav1 implementations.
**zenjxl is not merged** — it is blocked (see below) and remains the draft PR #40 against
this branch, to be merged once it can resolve.

## Libraries & sub-PRs

| Library | Format | Implementations added | crates.io | PR | Status |
| --- | --- | --- | --- | --- | --- |
| [zenjpeg](https://github.com/imazen/zenjpeg) | JPEG | `zenjpeg-encode` (quality/progressive/subsampling), `zenjpeg-decode` | ✅ 0.8 | #36 | ✅ built + round-trip verified |
| [zenpng](https://github.com/imazen/zenpng) | PNG | `zenpng-encode` (effort, lossless), `zenpng-decode` | ✅ 0.1 | #37 | ✅ built + lossless-exact verified |
| [zenwebp](https://github.com/imazen/zenwebp) | WebP | `zenwebp-encode` (lossy), `zenwebp-lossless-encode`, `zenwebp-decode` | ✅ 0.4 | #38 | ✅ built + round-trip verified |
| [zenavif](https://github.com/imazen/zenavif) | AVIF | ~~`zenavif-encode` (quality/speed), `zenavif-decode`~~ | ✅ 0.1 | #39 | ⛔ dropped 2026-06-12 (`rav1d-safe` decode flake) |
| [zenjxl](https://github.com/imazen/zenjxl) | JPEG XL | `zenjxl-encode` (distance), `zenjxl-lossless-encode`, `zenjxl-decode` | ❌ git-only | #40 | ⛔ blocked (draft) |

## Integration shape

Each library is a workspace member under `implementations/rust/zen<fmt>/` with thin
`BenchmarkImplementation` wrappers, registered in `bench_lib/models.py`
(`IMPLEMENTATIONS` + `TUNABLE_SCHEMAS`) so it appears automatically in both the quality
and performance sweeps, and documented in the README format tables. The zen impls are
**candidates, not references** — the `REFERENCE_ENCODERS`/`REFERENCE_DECODERS` maps are
unchanged, so decoders are scored against the existing golden decoders and encoder
outputs are golden-decoded for scoring (exactly the "relative to golden" comparison the
issue asks for).

## Known limitations (as of 2026-06-11)

These are time-sensitive — re-check the dates against upstream releases before relying on them.

1. **AGPL-3.0 licensing (all five libraries).** *(2026-06-11)* The harness/repo stay MIT, but
   any benchmark binary that links a `zen*` library is an AGPL-derived work; redistributing
   built binaries must honour the AGPL. Disclosed inline in each README format-table row and in
   the repo's "Limitations and Caveats" section.
2. **zenjxl is blocked / not yet built (PR #40).** *(2026-06-11)* `zenjxl 0.2.1` requires
   `jxl-encoder ^0.3.2`, which is not on crates.io (max published 0.3.1), and zenjxl's own
   `[patch.crates-io]` is ignored when it is consumed as a dependency, so the workspace cannot
   resolve. The wrapper code follows zenjxl's documented convenience API but is **unverified**
   (never compiled). Unblock by waiting for `jxl-encoder 0.3.2` to publish (`cargo update -p
   zenjxl`) or adding a root `[patch.crates-io]` for `jxl-encoder` (and `zenjpeg`/
   `zenjxl-decoder` if needed). PR #40 stays a draft until then.
3. **zenavif dropped — `rav1d-safe` decode flake (PR #39).** *(2026-06-12)* `zenavif-decode`
   is a thin wrapper over the pure-Rust `rav1d-safe` AV1 decoder, whose multithreaded CDEF SIMD
   path panics intermittently (`overlapping DisjointMut` in `rav1d-safe-0.5.7/src/safe_simd/
   cdef_arm.rs`, ~10% of AVIF-decode tasks, exit 134). Since AVIF is already covered by the
   libavif/rav1e/SVT-AV1 encoders and the dav1d/rav1d/libgav1 decoders, the wrapper added
   flakiness without new coverage, so both `zenavif-*` implementations and the
   `implementations/rust/zenavif/` crate were removed. (Separately, `zenavif 0.1.6` also never
   exposed a chroma-subsampling knob.) Re-add once the upstream `rav1d-safe` race is fixed.
4. **zenjpeg XYB not benchmarked — no round-trip to sRGB (`zenjpeg 0.8.4`).** *(2026-07-01)*
   zenjpeg exposes `EncoderConfig::xyb()` (jpegli's headline perceptual mode), and the encode
   wrapper can drive it, but its XYB output does not round-trip to correct sRGB through any
   decoder wired into this harness: jpegli's decoder (`jxl::extras::DecodeJpeg`) *rejects*
   zenjpeg's XYB bitstream outright, and zenjpeg's own decoder (`Decoder::new().decode()` →
   `PixelFormat::Rgb`) does **not** invert XYB→sRGB (a smoke-test scored ≈ −160 SSIMULACRA2,
   i.e. garbage; a linear→sRGB gamma pass did not recover it). Because the scored PPM would be
   meaningless, zenjpeg is benchmarked in **YCbCr only** (4:4:4 / 4:2:2 / 4:4:0 / 4:2:0);
   **jpegli carries the XYB comparison** (`jpegli-encode@xyb`, scored via the XYB-aware
   `jpegli-decode`). Re-enable zenjpeg XYB once its encode/decode XYB path is jpegli-interoperable
   or its own decoder color-manages XYB→sRGB. Recorded as a skipped knob in `docs/tunables.md`.
   Note: zenjpeg's YCbCr quality is already mapped through jpegli's `quality_to_distance` formula
   (`Quality::ApproxJpegli`), so its baseline *is* the distance path — no separate `@distance`
   variant is added (that would duplicate the base curve). **jpegli now matches this**: its base
   curve runs in native butteraugli **distance** mode too (`quality_control=distance`), because
   distance traces the same quality-axis range and the same quality as the libjpeg integer-quality
   path but at **much lower encode+decode time** — so the integer path is redundant as a default
   series. It is preserved only under `--full` as `jpegli-encode@quality_control-quality`, where the
   distance-vs-integer timing win can be verified head-to-head.

## Notes

- **Merge order:** the four buildable PRs touch only their own format's section of
  `models.py`/README plus `Cargo.lock`. Merge them sequentially, rebasing between merges
  to resolve the `Cargo.lock` overlap.
