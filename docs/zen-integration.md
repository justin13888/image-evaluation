# imazen "zen" codec integration (issue #34)

Tracking branch for integrating imazen's five AGPL-3.0 pure-Rust image codecs so they
can be benchmarked against the golden (reference C) implementations. Each library lands
as its own PR targeting this branch; this branch is the accumulator and the source of
the draft PR to `master`.

## Libraries & sub-PRs

| Library | Format | Implementations added | crates.io | PR | Status |
| --- | --- | --- | --- | --- | --- |
| [zenjpeg](https://github.com/imazen/zenjpeg) | JPEG | `zenjpeg-encode` (quality/progressive/subsampling), `zenjpeg-decode` | ✅ 0.8 | #36 | ✅ built + round-trip verified |
| [zenpng](https://github.com/imazen/zenpng) | PNG | `zenpng-encode` (effort, lossless), `zenpng-decode` | ✅ 0.1 | #37 | ✅ built + lossless-exact verified |
| [zenwebp](https://github.com/imazen/zenwebp) | WebP | `zenwebp-encode` (lossy), `zenwebp-lossless-encode`, `zenwebp-decode` | ✅ 0.4 | #38 | ✅ built + round-trip verified |
| [zenavif](https://github.com/imazen/zenavif) | AVIF | `zenavif-encode` (quality/speed), `zenavif-decode` | ✅ 0.1 | #39 | ✅ built + round-trip verified |
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
3. **zenavif chroma subsampling not exposed (PR #39).** *(2026-06-11)* The latest published
   `zenavif` (0.1.6) exposes only `quality` + `speed`; the 4:2:0/4:4:4 `chroma_subsampling`
   knob was added upstream after 0.1.6, so the `zenavif-encode` series omits it until a release
   exposes it. (The benchmark still sweeps quality; the encoder uses its default chroma format.)

## Notes

- **Merge order:** the four buildable PRs touch only their own format's section of
  `models.py`/README plus `Cargo.lock`. Merge them sequentially, rebasing between merges
  to resolve the `Cargo.lock` overlap.
