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

## Notes

- **Licensing:** all five are **AGPL-3.0**; the repo itself stays MIT, but benchmark
  binaries that link them are AGPL-derived. Disclosed inline in each README row.
- **zenjxl blocker (#40):** `zenjxl 0.2.1` requires `jxl-encoder ^0.3.2`, which is not on
  crates.io yet (max published 0.3.1), and zenjxl's own `[patch.crates-io]` is ignored
  when it is consumed as a dependency. The PR is a draft until `jxl-encoder 0.3.2`
  publishes or a root `[patch.crates-io]` is added.
- **Merge order:** the four buildable PRs touch only their own format's section of
  `models.py`/README plus `Cargo.lock`. Merge them sequentially, rebasing between merges
  to resolve the `Cargo.lock` overlap.
