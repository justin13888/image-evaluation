"""Enums, Pydantic models, constants, type aliases, and helpers."""

import os
import re
import secrets
import shlex
import threading
from enum import Enum
from itertools import chain
from typing import (
    Annotated,
    Callable,
    Dict,
    Literal,
    NotRequired,
    Optional,
    Tuple,
    TypedDict,
    Union,
)

import tyro
from pathlib import Path
from pydantic import BaseModel, Field, model_validator


# Use this lock to ensure only one thread writes to the console at a time
print_lock = threading.Lock()


def safe_print(message):
    """Prints a message safely across multiple threads."""
    with print_lock:
        print(message)


def generate_base32_string(length: int) -> str:
    # Base32 alphabet: A-Z and 2-7
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    return "".join(secrets.choice(alphabet) for _ in range(length))


class ImageFormat(str, Enum):
    JPEG = "jpeg"
    PNG = "png"
    WEBP = "webp"
    AVIF = "avif"
    JXL = "jxl"


class PPMImageFormat(str, Enum):
    PPM = "ppm"


ImageFormats = Union[ImageFormat, PPMImageFormat]

FORMAT_EXT_MAP: Dict[ImageFormats, str] = {
    ImageFormat.JPEG: "jpg",
    ImageFormat.PNG: "png",
    ImageFormat.WEBP: "webp",
    ImageFormat.AVIF: "avif",
    ImageFormat.JXL: "jxl",
    PPMImageFormat.PPM: "ppm",
}


def is_format_lossless(format: ImageFormats) -> bool:
    """Determine if a given image format is lossless."""
    # Note: JXL can be lossless but all implementations in this repo assume it can very well be lossy
    return format in {ImageFormat.PNG, PPMImageFormat.PPM}


class BenchmarkMode(str, Enum):
    ENCODE = "encode"
    DECODE = "decode"
    BOTH = "both"


class DatasetId(str, Enum):
    TEST = "test"
    KODAK = "kodak"
    DIV2K = "div2k"
    PATHOLOGICAL = "pathological"
    CLIC2025 = "clic2025"
    CID22 = "cid22"
    SCREEN = "screen"
    TECNICK = "tecnick"


class Dataset:
    def __init__(
        self,
        description: str,
        files: Union[list[str], Callable[[], list[str]]],
        homepage: Optional[str] = None,
    ):
        self.description = description
        self._files = files
        # Canonical public page for the dataset (provenance / "where it came
        # from"); surfaced in the report. None for locally generated datasets.
        self.homepage = homepage

    @property
    def files(self) -> list[str]:
        if callable(self._files):
            return self._files()
        return self._files


def _get_div2k_files() -> list[str]:
    p = Path("data/div2k/selected.txt")
    if p.exists():
        lines = p.read_text().splitlines()
        return [f"data/div2k/DIV2K_train_HR/{name}" for name in lines if name.strip()]
    return []


def _corpus_sources(directory: str, ext: str = "png") -> list[str]:
    """Sorted original images in a corpus directory, excluding generated files.

    The benchmark caches reference-encoded decode inputs (``<stem>.<label>.<ext>``)
    and PPM/format intermediates next to the originals for reuse across runs.
    Corpus originals have a single-component stem (a content hash or numeric id),
    so any file whose stem already contains a '.' is a generated intermediate and
    must be excluded from the source list. Otherwise it is re-discovered as a
    source and re-encoded, chaining suffixes combinatorially
    (e.g. ``X.compression-0.compression-1.png``) until the disk fills.
    """
    from glob import glob

    sources = []
    for path in glob(os.path.join(directory, f"*.{ext}")):
        stem = os.path.splitext(os.path.basename(path))[0]
        if "." in stem:
            continue  # generated intermediate (e.g. X.compression-0.png), not an original
        sources.append(path)
    return sorted(sources)


def _get_tecnick_files() -> list[str]:
    # selected.txt holds repo-relative-to-data/tecnick paths (one per line).
    p = Path("data/tecnick/selected.txt")
    if p.exists():
        lines = p.read_text().splitlines()
        return [f"data/tecnick/{name}" for name in lines if name.strip()]
    return []


DATASETS: Dict[str, Dataset] = {
    "test": Dataset(
        description="Single test file (legacy)",
        files=["data/test.ppm"],
    ),
    "kodak": Dataset(
        description="KODAK PhotoCD dataset (24 images, ~0.4MP)",
        files=lambda: [f"data/kodak/kodim{i:02d}.png" for i in range(1, 25)],
        homepage="https://r0k.us/graphics/kodak/",
    ),
    "div2k": Dataset(
        description="DIV2K selected subset (20 diverse high-res images)",
        files=_get_div2k_files,
        homepage="https://data.vision.ee.ethz.ch/cvl/DIV2K/",
    ),
    "pathological": Dataset(
        description="Pathological test cases (4 synthetic images)",
        files=[
            "data/pathological/solid_4k.png",
            "data/pathological/noise_4k.png",
            "data/pathological/screenshot_4k.png",
            "data/pathological/alpha_gradient_4k.png",
        ],
    ),
    "clic2025": Dataset(
        description="CLIC 2025 final-test (30 modern high-res photos, ~2048px)",
        files=lambda: _corpus_sources("vendor/codec-corpus/clic2025/final-test"),
        homepage="https://clic2025.compression.cc/",
    ),
    "cid22": Dataset(
        description="CID22 validation references (41 images, 512px; SSIMULACRA2 set)",
        files=lambda: _corpus_sources("vendor/codec-corpus/CID22/CID22-512/validation"),
        homepage="https://cloudinary.com/labs/cid22",
    ),
    "screen": Dataset(
        description="GB82-SC real screen content (10 UI/text/graphics images)",
        files=lambda: _corpus_sources("vendor/codec-corpus/gb82-sc"),
        homepage="https://github.com/imazen/codec-corpus",
    ),
    "tecnick": Dataset(
        description="Tecnick TESTIMAGES SAMPLING (24 diverse 1200x1200 images)",
        files=_get_tecnick_files,
        homepage="https://testimages.org/",
    ),
}


class BenchmarkType(str, Enum):
    ENCODE = "encode"
    DECODE = "decode"


class Implementation(BaseModel):
    name: str
    build: Literal["cpp", "rust"]
    lang: str
    # Binary path
    bin: str
    type: BenchmarkType
    # Image format supported. None implies any format (e.g., null implementation)
    format: Optional[ImageFormat]
    # Provenance for derived secondary-knob series (see `Variant` / `_expand_variants`):
    # None  -> a hand-written base implementation;
    # "curated" -> a default-on variant (--params variants);
    # "oat"  -> a one-at-a-time variant only run under --params all.
    # Variants reuse their base's `bin`, so they add no build target.
    variant_kind: Optional[Literal["curated", "oat"]] = None

    @property
    def is_variant(self) -> bool:
        return self.variant_kind is not None


class Tunable(BaseModel):
    """One knob an implementation exposes, sent to its binary via --param.

    Values travel on the wire as strings (e.g. "80", "1.0", "true", "444"); the
    binary's typed getter (param_u32/param_f32/param_bool/param_str) interprets
    them. `kind`/`min`/`max`/`choices` are advisory metadata for the orchestrator
    and reports, not enforced by the binary.
    """

    name: str
    kind: Literal["int", "float", "bool", "enum", "str"]
    default: str
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Optional[list[str]] = None
    description: str = ""
    # When set, this knob IS read by the binary but is deliberately NOT varied by
    # the sweep (it stays pinned at its perf_preset/default). The reason is
    # surfaced verbatim in the generated tunables overview so every intentional
    # "we don't test this" decision lives in code (issue #4). None = either swept
    # (the quality axis) or exercised via a `variant`.
    skip_reason: Optional[str] = None


class Variant(BaseModel):
    """A named secondary operating point of a base encoder: a fixed override of
    one or more non-quality knobs, layered on top of the schema's perf_preset.

    Each variant derives a distinct Implementation that REUSES the base binary
    (exactly like the hand-written ``libjxl-lossless-encode``) plus a derived
    schema, so it becomes its own series end-to-end — its own rate-distortion
    curve, BD-rate row, and Pareto candidacy — without polluting the base
    encoder's curve (every report/plot path groups by ``impl`` name). The quality
    axis is still swept *within* each variant, tracing a full curve at the fixed
    secondary setting."""

    # Short, grammar-safe tag (no ", ", " (", or "="); see `variant_impl_name`).
    tag: str
    # Knob -> fixed value, applied on top of perf_preset for this variant.
    overrides: Dict[str, str]
    description: str = ""


class TunableSchema(BaseModel):
    """Declares the tunables an implementation honours, the single fixed
    operating point used by the *performance* suite, and (for lossy encoders) the
    knob + values swept by the *quality* suite to trace a rate-distortion curve.

    Keeping this in the orchestrator lets the per-binary harness stay dumb: it
    reads whatever --param keys it's handed and ignores the rest, while Python
    decides which keys to send for each suite.
    """

    # Every knob this implementation reads. May be empty (decoders, spng).
    params: list[Tunable] = []
    # The knob the quality suite sweeps. For a *lossy* encoder this traces a
    # size-vs-quality (rate-distortion) curve; for a *lossless* encoder (see
    # `lossless`) it is instead a compression-effort axis tracing size-vs-effort.
    # None for decoders and for knob-less encoders (spng, image-webp), which the
    # quality suite then runs at a single operating point.
    quality_axis: Optional[str] = None
    # Concrete quality-axis values the quality suite sweeps (issue #8). Strings on
    # the wire, ordered low-quality -> high-quality (lossy) or low-effort ->
    # high-effort (lossless). Empty when quality_axis is None.
    quality_sweep: list[str] = []
    # The single fixed operating point the performance suite uses. param -> value.
    perf_preset: dict[str, str] = {}
    # True when this encoder's output is lossless (pixel-identical round-trip).
    # Such encoders have no rate-distortion tradeoff, so their quality-suite rows
    # are flagged (issue #26) and routed to the dedicated lossless
    # compression-efficiency view instead of the rate-distortion charts / BD-rate
    # / Pareto front. `quality_axis` (if set) is then an effort axis, not quality.
    lossless: bool = False
    # Curated secondary operating points run by default (--params variants): each
    # derives a distinct base@tag series (issue #4). Empty for decoders, knob-less
    # encoders, and the derived variant schemas themselves.
    variants: list[Variant] = []
    # Library features this implementation deliberately does NOT wire as a knob at
    # all (so there is no `Tunable` to carry a `skip_reason`). (name, reason) pairs,
    # surfaced in the generated overview so the "intentionally skipped as
    # irrelevant" decisions live in code (issue #4).
    skipped: list[Tuple[str, str]] = []

    def perf_params(self) -> Dict[str, str]:
        """Concrete params for the performance suite (the fixed preset)."""
        return dict(self.perf_preset)

    def quality_params(self, axis_value: str) -> Dict[str, str]:
        """Concrete params for one quality-suite operating point: the preset with
        the quality axis overridden to `axis_value`."""
        params = dict(self.perf_preset)
        if self.quality_axis is not None:
            params[self.quality_axis] = axis_value
        return params


NULL_IMPLEMENTATIONS: list[Implementation] = [
    Implementation(
        name="null-cpp-decode",
        build="cpp",
        lang="c++",
        bin="implementations/cpp/null/build/bench-null-decode",
        type=BenchmarkType.DECODE,
        format=None,
    ),
    Implementation(
        name="null-cpp-encode",
        build="cpp",
        lang="c++",
        bin="implementations/cpp/null/build/bench-null-encode",
        type=BenchmarkType.ENCODE,
        format=None,
    ),
    Implementation(
        name="null-rust-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-null-decode",
        type=BenchmarkType.DECODE,
        format=None,
    ),
    Implementation(
        name="null-rust-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-null-encode",
        type=BenchmarkType.ENCODE,
        format=None,
    ),
]

IMPLEMENTATIONS: list[Implementation] = [
    # JPEG
    Implementation(
        name="jpeg-decoder-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-jpeg-decoder-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.JPEG,
    ),
    Implementation(
        name="zune-jpeg-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zune-jpeg-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.JPEG,
    ),
    Implementation(
        name="jpeg-encoder-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-jpeg-encoder-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.JPEG,
    ),
    Implementation(
        name="image-jpeg-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-image-jpeg-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.JPEG,
    ),
    # zenjpeg: pure-Rust jpegli port (AGPL-3.0, imazen/zenjpeg).
    Implementation(
        name="zenjpeg-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zenjpeg-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.JPEG,
    ),
    Implementation(
        name="zenjpeg-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zenjpeg-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.JPEG,
    ),
    Implementation(
        name="libjpeg-turbo-decode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/libjpeg-turbo/build/bench-libjpeg-turbo-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.JPEG,
    ),
    Implementation(
        name="libjpeg-turbo-encode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/libjpeg-turbo/build/bench-libjpeg-turbo-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.JPEG,
    ),
    Implementation(
        name="mozjpeg-decode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/mozjpeg/build/bench-mozjpeg-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.JPEG,
    ),
    Implementation(
        name="mozjpeg-encode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/mozjpeg/build/bench-mozjpeg-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.JPEG,
    ),
    Implementation(
        name="jpegli-encode",
        build="cpp",
        lang="c++",
        bin="implementations/cpp/jpegli/build/bench-jpegli-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.JPEG,
    ),
    # PNG
    Implementation(
        name="image-png-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-image-png-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.PNG,
    ),
    Implementation(
        name="image-png-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-image-png-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.PNG,
    ),
    Implementation(
        name="zune-png-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zune-png-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.PNG,
    ),
    Implementation(
        name="zune-png-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zune-png-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.PNG,
    ),
    # zenpng: pure-Rust lossless PNG codec (AGPL-3.0, imazen/zenpng).
    Implementation(
        name="zenpng-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zenpng-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.PNG,
    ),
    Implementation(
        name="zenpng-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zenpng-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.PNG,
    ),
    Implementation(
        name="libpng-decode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/libpng/build/bench-libpng-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.PNG,
    ),
    Implementation(
        name="libpng-encode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/libpng/build/bench-libpng-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.PNG,
    ),
    Implementation(
        name="spng-decode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/spng/build/bench-spng-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.PNG,
    ),
    Implementation(
        name="spng-encode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/spng/build/bench-spng-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.PNG,
    ),
    # WEBP
    Implementation(
        name="image-webp-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-image-webp-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.WEBP,
    ),
    Implementation(
        name="image-webp-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-image-webp-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.WEBP,
    ),
    # zenwebp: pure-Rust WebP codec (AGPL-3.0, imazen/zenwebp). Lossy VP8 +
    # lossless VP8L as separate encode series, plus a decoder.
    Implementation(
        name="zenwebp-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zenwebp-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.WEBP,
    ),
    Implementation(
        name="zenwebp-lossless-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zenwebp-lossless-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.WEBP,
    ),
    Implementation(
        name="zenwebp-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zenwebp-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.WEBP,
    ),
    Implementation(
        name="libwebp-decode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/libwebp/build/bench-libwebp-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.WEBP,
    ),
    Implementation(
        name="libwebp-encode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/libwebp/build/bench-libwebp-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.WEBP,
    ),
    # libwebp in lossless mode (VP8L). Reuses the same binary as libwebp-encode;
    # the distinct name makes it a separate series so the lossless operating point
    # is not conflated with the lossy VP8 quality sweep (mirrors
    # libjxl-lossless-encode). Used as the WebP lossless reference encoder so the
    # decoder sweep also exercises the VP8L decode path (issue #21).
    Implementation(
        name="libwebp-lossless-encode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/libwebp/build/bench-libwebp-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.WEBP,
    ),
    # AVIF
    Implementation(
        name="rav1e-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-rav1e-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.AVIF,
    ),
    Implementation(
        name="libavif-decode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/libavif/build/bench-libavif-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.AVIF,
    ),
    Implementation(
        name="libavif-encode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/libavif/build/bench-libavif-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.AVIF,
    ),
    Implementation(
        name="dav1d-decode",
        build="cpp",
        lang="c/asm",
        bin="implementations/cpp/dav1d/build/bench-dav1d-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.AVIF,
    ),
    Implementation(
        name="svt-av1-encode",
        build="cpp",
        lang="c",
        bin="implementations/cpp/svt-av1/build/bench-svt-av1-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.AVIF,
    ),
    Implementation(
        name="libgav1-decode",
        build="cpp",
        lang="c++",
        bin="implementations/cpp/libgav1/build/bench-libgav1-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.AVIF,
    ),
    Implementation(
        name="rav1d-decode",
        build="cpp",
        lang="rust",
        bin="implementations/cpp/rav1d/build/bench-rav1d-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.AVIF,
    ),
    # JXL
    Implementation(
        name="jxl-oxide-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-jxl-oxide-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.JXL,
    ),
    Implementation(
        name="jxl-rs-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-jxl-rs-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.JXL,
    ),
    Implementation(
        name="zune-jpegxl-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zune-jpegxl-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.JXL,
    ),
    Implementation(
        name="libjxl-decode",
        build="cpp",
        lang="c++",
        bin="implementations/cpp/libjxl/build/bench-libjxl-decode",
        type=BenchmarkType.DECODE,
        format=ImageFormat.JXL,
    ),
    Implementation(
        name="libjxl-encode",
        build="cpp",
        lang="c++",
        bin="implementations/cpp/libjxl/build/bench-libjxl-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.JXL,
    ),
    # libjxl in lossless mode (distance 0). Reuses the same binary as
    # libjxl-encode; the distinct name makes it a separate series so the lossless
    # operating point is not conflated with the lossy distance sweep.
    Implementation(
        name="libjxl-lossless-encode",
        build="cpp",
        lang="c++",
        bin="implementations/cpp/libjxl/build/bench-libjxl-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.JXL,
    ),
]

assert not (set(ImageFormat) - {i.format for i in IMPLEMENTATIONS}), (
    "IMPLEMENTATIONS missing some ImageFormats"
)


def find_implementation_by_name(name: str) -> Optional[Implementation]:
    """Find implementation by name."""
    for impl in chain(IMPLEMENTATIONS, NULL_IMPLEMENTATIONS):
        if impl.name == name:
            return impl

    return None


REFERENCE_ENCODERS: Dict[ImageFormats, str] = {
    ImageFormat.JPEG: "libjpeg-turbo-encode",
    ImageFormat.PNG: "libpng-encode",
    ImageFormat.WEBP: "libwebp-encode",
    ImageFormat.AVIF: "libavif-encode",
    ImageFormat.JXL: "libjxl-encode",
    PPMImageFormat.PPM: "null-cpp-encode",
}

# Lossless reference encoders, used only to generate the *lossless* decode-path
# inputs for the decoder sweep (issue #21): formats with both a lossy and a
# lossless mode (WebP VP8L, JXL distance-0) must exercise both decode paths, not
# just the lossy one from REFERENCE_ENCODERS. A format absent here has no separate
# lossless decode path — JPEG is lossy-only, PNG is already lossless-only (its sole
# REFERENCE_ENCODERS entry covers it), and AVIF has no lossless encoder yet.
LOSSLESS_REFERENCE_ENCODERS: Dict[ImageFormats, str] = {
    ImageFormat.WEBP: "libwebp-lossless-encode",
    ImageFormat.JXL: "libjxl-lossless-encode",
}

# Reference decoders, used to turn an encoded output back into a PPM so iqa-cli
# (which does not decode codec formats) can compare raw pixels against the
# source. One trusted decoder per format.
REFERENCE_DECODERS: Dict[ImageFormats, str] = {
    ImageFormat.JPEG: "libjpeg-turbo-decode",
    ImageFormat.PNG: "libpng-decode",
    ImageFormat.WEBP: "libwebp-decode",
    ImageFormat.AVIF: "libavif-decode",
    ImageFormat.JXL: "libjxl-decode",
}


# Lossy formats whose decode inverse transform is NON-normative: the spec does
# not fix an exact integer inverse transform, so two correct, independent
# decoders legitimately produce sub-LSB-different pixels. A decode that is *not*
# bit-exact versus the format's golden reference decoder is therefore EXPECTED
# here, not a defect:
#   JPEG - the IDCT is only accuracy-bounded (ITU-T T.81 Annex A / IEEE 1180),
#          never specified bit-for-bit, and chroma upsampling differs by decoder.
#   JXL  - the VarDCT inverse transform is floating-point, not bit-reproducible
#          across implementations.
# AV1/AVIF and VP8/WebP are deliberately ABSENT: their integer inverse transforms
# are normative, so every conformant decoder must match the reference bit-for-bit
# and a mismatch there IS a real defect. PNG is lossless (scored vs the source,
# not a golden decoder), so it never reaches this gate.
NON_NORMATIVE_LOSSY_DECODE_FORMATS: frozenset = frozenset(
    {ImageFormat.JPEG, ImageFormat.JXL}
)


def decode_approx_expected(format: str, basis: str) -> bool:
    """True when a non-bit-exact decode is EXPECTED (faithful, not a defect).

    Holds only for a *lossy* decode (scored vs the format's ``golden`` reference
    decoder, ``basis == "golden"``) of a format whose inverse transform is
    non-normative (JPEG, JXL; see ``NON_NORMATIVE_LOSSY_DECODE_FORMATS``). A
    ``source``-basis decode (the lossless path: PNG always, the WebP/JXL lossless
    path) must always be bit-exact, as must a ``golden``-basis decode of a
    normative format (AV1/AVIF, VP8/WebP); for those a finite PSNR is a genuine
    failure, so this returns ``False``."""
    if basis != "golden":
        return False
    try:
        return ImageFormat(format) in NON_NORMATIVE_LOSSY_DECODE_FORMATS
    except ValueError:
        return False


# Threading configurations: single-threaded (per-core efficiency) then all-cores
# (real-world throughput). Output bytes are identical across these, so metrics
# are collected for only one of them; only timing/memory vary by thread count.
THREAD_MODES: list[int] = [1, 0]


# ---------------------------------------------------------------------------
# Per-implementation tunable schemas.
#
# Each encoder declares the knobs it honours (sent to the binary via --param),
# the single fixed operating point used by the performance suite (`perf_preset`),
# and the `quality_axis` knob plus discrete `quality_sweep` values the quality
# suite steps through — a rate-distortion curve for lossy encoders (issue #8), or
# a size-vs-effort curve for lossless ones (`lossless=True`, issue #26). Lossless
# encoders with no knob (spng, image-webp) still declare `lossless=True` so they
# contribute a single operating point. Decoders and null binaries fall back to an
# empty schema via `schema_for`.
# ---------------------------------------------------------------------------

# Shared quality-axis sweeps (ordered low-quality -> high-quality).
_JPEG_QUALITY_SWEEP = ["10", "20", "30", "40", "50", "60", "70", "80", "85", "90", "95"]
_WEBP_QUALITY_SWEEP = ["10", "20", "30", "40", "50", "60", "70", "80", "90", "95"]
# WebP encoder effort/speed method (0=fast/larger .. 6=slow/smaller). Used as the
# effort axis for lossless VP8L.
_WEBP_METHOD_SWEEP = ["0", "1", "2", "3", "4", "5", "6"]
_AVIF_QUALITY_SWEEP = ["20", "30", "40", "50", "60", "70", "80", "90"]
# JXL distance: higher distance = lower quality. Full *lossy* range from very low
# quality (15.0) down to near-lossless (0.1). Dense near the high-quality end
# (small distances), which is where the rate-distortion curve is most sensitive.
# distance 0 (true lossless) is a different encode mode (original profile, not
# XYB) and is exposed as the separate `libjxl-lossless-encode` variant instead of
# the tail of this lossy curve.
_JXL_DISTANCE_SWEEP = [
    "15.0",
    "12.0",
    "10.0",
    "8.0",
    "6.0",
    "5.0",
    "4.0",
    "3.0",
    "2.0",
    "1.5",
    "1.0",
    "0.75",
    "0.5",
    "0.25",
    "0.1",
]

# Lossless effort/compression sweeps (issue #26), ordered low-effort ->
# high-effort. For lossless encoders these trace size-vs-effort, not quality.
_PNG_ZLIB_SWEEP = [str(i) for i in range(10)]  # zlib level / effort 0-9
_IMAGE_PNG_COMPRESSION_SWEEP = ["fast", "default", "best"]  # image crate preset
_JXL_EFFORT_SWEEP = [str(i) for i in range(1, 10)]  # libjxl effort 1-9
# zenpng effort 0-200; sweep the named-preset points of the standard pipeline
# (None..Intense). 31+ needs the zopfli feature / runs minutes per MP, so it is
# left out of the swept range (still reachable via --param effort=N).
_ZENPNG_EFFORT_SWEEP = ["0", "1", "2", "7", "13", "17", "19", "22", "24"]


# Curated JPEG secondary operating points (issue #4). 4:4:4 vs the default 4:2:0
# is the single highest-value chroma axis; sequential vs progressive is added only
# for the reference-class encoders to bound the default series count.
_JPEG_444_VARIANT = Variant(
    tag="subsampling-444",
    overrides={"subsampling": "444"},
    description="4:4:4 (no chroma subsampling) vs the default 4:2:0",
)
_JPEG_SEQUENTIAL_VARIANT = Variant(
    tag="progressive-off",
    overrides={"progressive": "false"},
    description="Sequential (baseline) vs the default progressive scan",
)


def _jpeg_full_schema(variants: Optional[list["Variant"]] = None) -> "TunableSchema":
    """JPEG encoders exposing quality + progressive + chroma subsampling
    (libjpeg-turbo, mozjpeg, jpegli, jpeg-encoder, zenjpeg). `variants` selects the
    curated secondary series; defaults to the 4:4:4 chroma variant."""
    return TunableSchema(
        params=[
            Tunable(
                name="quality",
                kind="int",
                default="80",
                min=1,
                max=100,
                description="JPEG quality (1-100)",
            ),
            Tunable(
                name="progressive",
                kind="bool",
                default="true",
                description="Progressive (multi-scan) encoding",
            ),
            Tunable(
                name="subsampling",
                kind="enum",
                default="420",
                choices=["420", "444"],
                description="Chroma subsampling",
            ),
        ],
        quality_axis="quality",
        quality_sweep=_JPEG_QUALITY_SWEEP,
        perf_preset={"quality": "80", "progressive": "true", "subsampling": "420"},
        variants=[_JPEG_444_VARIANT] if variants is None else variants,
    )


def _mozjpeg_schema() -> "TunableSchema":
    """mozjpeg = the full JPEG schema plus mozjpeg's headline extension, trellis
    quantization (on by default; the meaningful test is turning it OFF)."""
    schema = _jpeg_full_schema(
        variants=[
            _JPEG_444_VARIANT,
            _JPEG_SEQUENTIAL_VARIANT,
            Variant(
                tag="trellis-off",
                overrides={"trellis": "false"},
                description="Disable mozjpeg trellis quantization (on by default)",
            ),
        ]
    )
    schema.params.append(
        Tunable(
            name="trellis",
            kind="bool",
            default="true",
            description="Trellis quantization (mozjpeg-specific; default on)",
        )
    )
    schema.perf_preset["trellis"] = "true"
    # mozjpeg-specific knobs we read about but deliberately do not sweep.
    schema.skipped = [
        ("optimize_scans", "irrelevant: scan-order micro-opt, not RD-relevant here"),
        ("dc_scan_opt_mode", "irrelevant: DC scan tuning, marginal vs quality"),
        ("base_quant_tbl_idx", "irrelevant: alternate quant tables, niche"),
    ]
    return schema


def _avif_schema(support_444: bool = True) -> "TunableSchema":
    """AVIF encoders via libavif (libavif, svt-av1) share a 0-100 quality knob plus
    a speed preset and chroma format. `speed` is a quality/throughput trade left to
    the performance overlay; 4:4:4 chroma is the curated secondary series.

    `support_444=False` restricts the encoder to 4:2:0 only and drops the 4:4:4
    series — SVT-AV1 (`Svt[error]: Only support 420 now`) rejects any other chroma
    format, so exposing the variant would just produce guaranteed-failing runs."""
    return TunableSchema(
        params=[
            Tunable(
                name="quality",
                kind="int",
                default="65",
                min=0,
                max=100,
                description="AVIF quality (0-100)",
            ),
            Tunable(
                name="speed",
                kind="int",
                default="6",
                min=0,
                max=10,
                skip_reason="speed is a quality/throughput trade — covered by the "
                "performance overlay, not a default RD series",
            ),
            Tunable(
                name="yuv",
                kind="enum",
                default="420",
                choices=["420", "444"] if support_444 else ["420"],
                description="Chroma subsampling (YUV format)",
            ),
        ],
        quality_axis="quality",
        quality_sweep=_AVIF_QUALITY_SWEEP,
        perf_preset={"quality": "65", "speed": "6", "yuv": "420"},
        variants=[
            Variant(
                tag="yuv-444",
                overrides={"yuv": "444"},
                description="4:4:4 (no chroma subsampling) vs the default 4:2:0",
            )
        ]
        if support_444
        else [],
        skipped=[
            (
                "codec-specific-options",
                "deferred: aom/svt keys (tune/aq-mode/sharpness/denoise) via "
                "avifEncoderSetCodecSpecificOption are backend- and "
                "version-specific; tracked for follow-up",
            ),
            (
                "tiling",
                "irrelevant: tileRowsLog2/tileColsLog2 affect parallelism, not RD",
            ),
            ("min/maxQuantizer", "deprecated by libavif in favour of `quality`"),
        ],
    )


TUNABLE_SCHEMAS: Dict[str, "TunableSchema"] = {
    # --- JPEG ---
    # Reference-class encoders also get the sequential (progressive-off) series.
    "libjpeg-turbo-encode": _jpeg_full_schema(
        variants=[_JPEG_444_VARIANT, _JPEG_SEQUENTIAL_VARIANT]
    ),
    "mozjpeg-encode": _mozjpeg_schema(),
    "jpegli-encode": _jpeg_full_schema(),
    "jpeg-encoder-encode": _jpeg_full_schema(),
    "image-jpeg-encode": TunableSchema(
        params=[
            Tunable(name="quality", kind="int", default="80", min=1, max=100),
        ],
        quality_axis="quality",
        quality_sweep=_JPEG_QUALITY_SWEEP,
        perf_preset={"quality": "80"},
    ),
    # zenjpeg exposes quality + progressive + chroma subsampling, like the other
    # full-featured JPEG encoders.
    "zenjpeg-encode": _jpeg_full_schema(),
    # --- WEBP ---
    "libwebp-encode": TunableSchema(
        params=[
            Tunable(name="quality", kind="float", default="75", min=0, max=100),
            Tunable(
                name="method",
                kind="int",
                default="4",
                min=0,
                max=6,
                skip_reason="effort/speed knob — covered by the performance overlay, "
                "not a default RD series",
            ),
            Tunable(
                name="lossless",
                kind="bool",
                default="false",
                skip_reason="mode toggle: lossless WebP is a distinct pipeline "
                "(see image-webp-encode), not a knob on the lossy RD curve",
            ),
        ],
        quality_axis="quality",
        quality_sweep=_WEBP_QUALITY_SWEEP,
        perf_preset={"quality": "75", "method": "4", "lossless": "false"},
        skipped=[
            (
                "filter/sns/segments/near_lossless/use_sharp_yuv/preprocessing",
                "irrelevant: large WebPConfig tail with low RD value vs quality; "
                "alpha/target-size knobs N/A for opaque RGB / deterministic runs",
            )
        ],
    ),
    # image-webp encodes lossless WebP only (crate limitation) and exposes no
    # knob, so it has no rate-distortion curve. Flagged lossless (issue #26) so it
    # contributes one operating point to the lossless compression-efficiency view.
    "image-webp-encode": TunableSchema(lossless=True),
    # zenwebp lossy VP8: quality (0-100) + speed/quality method (0-6).
    "zenwebp-encode": TunableSchema(
        params=[
            Tunable(name="quality", kind="float", default="75", min=0, max=100),
            Tunable(name="method", kind="int", default="4", min=0, max=6),
        ],
        quality_axis="quality",
        quality_sweep=_WEBP_QUALITY_SWEEP,
        perf_preset={"quality": "75", "method": "4"},
    ),
    # zenwebp lossless VP8L: pixel-identical; quality is pinned high (max entropy
    # reduction) and the swept axis is the encoder `method` (effort).
    "zenwebp-lossless-encode": TunableSchema(
        params=[
            Tunable(name="quality", kind="float", default="100", min=0, max=100),
            Tunable(name="method", kind="int", default="4", min=0, max=6),
        ],
        quality_axis="method",
        quality_sweep=_WEBP_METHOD_SWEEP,
        perf_preset={"quality": "100", "method": "4"},
        lossless=True,
    ),
    # libwebp lossless VP8L: same binary as libwebp-encode with the `lossless` flag
    # pinned on. Quality is pinned high and the swept axis is the encoder `method`
    # (effort), tracing size-vs-effort; it also feeds the VP8L decode path of the
    # decoder sweep (issue #21). `lossless` is pinned in perf_preset so the binary
    # actually receives `--param lossless=true` (only preset keys are emitted).
    "libwebp-lossless-encode": TunableSchema(
        params=[
            Tunable(name="quality", kind="float", default="100", min=0, max=100),
            Tunable(name="method", kind="int", default="4", min=0, max=6),
            Tunable(
                name="lossless",
                kind="bool",
                default="true",
                skip_reason="pinned on: this series IS the lossless VP8L pipeline "
                "(lossless=false would just duplicate libwebp-encode's lossy VP8)",
            ),
        ],
        quality_axis="method",
        quality_sweep=_WEBP_METHOD_SWEEP,
        perf_preset={"quality": "100", "method": "4", "lossless": "true"},
        lossless=True,
    ),
    # --- AVIF ---
    "rav1e-encode": TunableSchema(
        params=[
            Tunable(
                name="quality",
                kind="int",
                default="65",
                min=0,
                max=100,
                description="AVIF quality 0-100 (mapped to rav1e quantizer)",
            ),
            Tunable(
                name="speed",
                kind="int",
                default="6",
                min=0,
                max=10,
                skip_reason="speed is a quality/throughput trade — covered by the "
                "performance overlay, not a default RD series",
            ),
            Tunable(name="chroma", kind="enum", default="420", choices=["420", "444"]),
        ],
        quality_axis="quality",
        quality_sweep=_AVIF_QUALITY_SWEEP,
        perf_preset={"quality": "65", "speed": "6", "chroma": "420"},
        variants=[
            Variant(
                tag="chroma-444",
                overrides={"chroma": "444"},
                description="4:4:4 (no chroma subsampling) vs the default 4:2:0",
            )
        ],
        skipped=[
            (
                "tune",
                "deferred: Psnr-vs-Psychovisual tuning entangles the IQA metric choice",
            ),
            ("tiling", "irrelevant: tile_rows/tile_cols affect parallelism, not RD"),
            (
                "film_grain",
                "deferred: needs calibrated noise params (TODO in encode.rs)",
            ),
        ],
    ),
    "libavif-encode": _avif_schema(),
    # SVT-AV1 only encodes YUV 4:2:0 ("Svt[error]: Only support 420 now"), so it
    # gets the AVIF schema without the 4:4:4 chroma series.
    "svt-av1-encode": _avif_schema(support_444=False),
    # --- JXL ---
    # Issue #4 explicitly calls out JXL "progressive decoding and various quality
    # constraint settings". libjxl-encode wires the encode-side levers
    # (progressive AC, modular mode, plus responsive/progressive-DC/decoding-speed)
    # via JxlEncoderFrameSettingsSetOption; `progressive` and `modular` get curated
    # variant series, the rest are wired-but-documented-skipped to bound the PR.
    "libjxl-encode": TunableSchema(
        params=[
            Tunable(
                name="distance",
                kind="float",
                default="1.0",
                min=0.0,
                max=25.0,
                description="Butteraugli distance; 0 = lossless",
            ),
            Tunable(name="effort", kind="int", default="7", min=1, max=9),
            Tunable(
                name="progressive",
                kind="enum",
                default="-1",
                choices=["-1", "0", "1"],
                description="Spectral progressive AC (-1 auto / 0 off / 1 on)",
            ),
            Tunable(
                name="modular",
                kind="enum",
                default="-1",
                choices=["-1", "0", "1"],
                description="Force modular(1)/VarDCT(0) encoding; -1 = encoder chooses",
            ),
            Tunable(
                name="responsive",
                kind="enum",
                default="-1",
                choices=["-1", "0", "1"],
                skip_reason="irrelevant: modular-mode progressive; not RD-relevant "
                "for VarDCT photos",
            ),
            Tunable(
                name="progressive_dc",
                kind="enum",
                default="-1",
                choices=["-1", "0", "1", "2"],
                skip_reason="irrelevant: low-res DC passes barely move final size",
            ),
            Tunable(
                name="decoding_speed",
                kind="int",
                default="0",
                min=0,
                max=4,
                skip_reason="irrelevant: trades density for decode speed, scored on "
                "the encode side here",
            ),
        ],
        quality_axis="distance",
        quality_sweep=_JXL_DISTANCE_SWEEP,
        perf_preset={"distance": "1.0", "effort": "7"},
        variants=[
            Variant(
                tag="progressive-on",
                overrides={"progressive": "1"},
                description="Spectral progressive AC enabled",
            ),
            Variant(
                tag="modular-on",
                overrides={"modular": "1"},
                description="Force modular-mode encoding",
            ),
        ],
        skipped=[
            (
                "epf/gaborish/photon_noise/dots/patches",
                "irrelevant: long-tail VarDCT artefact controls; the named "
                "progressive/modular levers cover the issue's intent",
            ),
            (
                "color_transform",
                "irrelevant: XYB is the correct high-quality lossy path",
            ),
        ],
    ),
    # Lossless libjxl: distance pinned to 0 (true-lossless path, original profile
    # not XYB). Its quality-suite axis is *effort* (issue #26): a size-vs-effort
    # sweep feeding the lossless compression-efficiency view, not the JXL
    # rate-distortion curve.
    "libjxl-lossless-encode": TunableSchema(
        params=[
            Tunable(
                name="distance",
                kind="float",
                default="0.0",
                min=0.0,
                max=0.0,
                description="Butteraugli distance pinned to 0 (lossless)",
            ),
            Tunable(name="effort", kind="int", default="7", min=1, max=9),
        ],
        quality_axis="effort",
        quality_sweep=_JXL_EFFORT_SWEEP,
        perf_preset={"distance": "0.0", "effort": "7"},
        lossless=True,
        skipped=[
            (
                "progressive/modular/responsive/decoding_speed",
                "wired in the shared libjxl binary but exercised only by the lossy "
                "libjxl-encode series; the lossless path uses encoder defaults",
            )
        ],
    ),
    "zune-jpegxl-encode": TunableSchema(
        params=[
            Tunable(name="quality", kind="int", default="90", min=0, max=100),
            Tunable(name="effort", kind="int", default="7", min=1, max=9),
        ],
        quality_axis="quality",
        quality_sweep=["40", "50", "60", "70", "80", "90", "95", "100"],
        perf_preset={"quality": "90", "effort": "7"},
    ),
    # --- PNG (lossless: the swept axis is compression *effort*, not quality;
    # rows feed the lossless compression-efficiency view, issue #26) ---
    "image-png-encode": TunableSchema(
        params=[
            Tunable(
                name="compression",
                kind="enum",
                default="default",
                choices=["fast", "default", "best"],
            ),
            Tunable(
                name="filter",
                kind="enum",
                default="adaptive",
                choices=["none", "sub", "up", "avg", "paeth", "adaptive"],
            ),
        ],
        quality_axis="compression",
        quality_sweep=_IMAGE_PNG_COMPRESSION_SWEEP,
        perf_preset={"compression": "default", "filter": "adaptive"},
        lossless=True,
        variants=[
            Variant(
                tag="filter-none",
                overrides={"filter": "none"},
                description="No row filtering vs the default adaptive filter",
            )
        ],
    ),
    # zune-png 0.5.x encoder emits STORED (uncompressed) DEFLATE blocks via zune-inflate
    # (no compression impl yet) and ignores effort, so there is no compression-efficiency
    # sweep -- it contributes one honest lossless operating point (~24 bpp). See issue #26.
    "zune-png-encode": TunableSchema(lossless=True),
    # zenpng: lossless; the swept axis is its 0-200 compression effort. Default 13
    # is the `Balanced` preset. Filter selection is automatic (no knob).
    "zenpng-encode": TunableSchema(
        params=[
            Tunable(
                name="effort",
                kind="int",
                default="13",
                min=0,
                max=200,
                description="zenpng compression effort (0-200)",
            )
        ],
        quality_axis="effort",
        quality_sweep=_ZENPNG_EFFORT_SWEEP,
        perf_preset={"effort": "13"},
        lossless=True,
    ),
    "libpng-encode": TunableSchema(
        params=[Tunable(name="compression", kind="int", default="6", min=0, max=9)],
        quality_axis="compression",
        quality_sweep=_PNG_ZLIB_SWEEP,
        perf_preset={"compression": "6"},
        lossless=True,
    ),
    # spng exposes no compression control, so it has no effort axis; flagged
    # lossless so it still contributes a single operating point (issue #26).
    "spng-encode": TunableSchema(lossless=True),
}


def schema_for(impl_name: str) -> "TunableSchema":
    """Tunable schema for an implementation; an empty schema (no knobs, no
    quality axis, not lossless) for decoders and null binaries. Knob-less
    lossless encoders (spng, image-webp) carry an explicit `lossless=True`
    schema so they are still picked up by the quality suite."""
    return TUNABLE_SCHEMAS.get(impl_name, TunableSchema())


# Decoders take no params in this harness (they consume the bitstream as-is and
# are scored vs the format's golden decoder), so library decode knobs that exist
# but are intentionally not exercised live here, surfaced in the overview. Keyed
# by decoder impl name; only entries worth calling out are listed.
DECODER_SKIP_NOTES: Dict[str, list[Tuple[str, str]]] = {
    "libjxl-decode": [
        (
            "progressive decode",
            "out of scope: the harness scores full-decode fidelity vs golden; "
            "partial/progressive-decode quality needs a different rig",
        ),
    ],
    "dav1d-decode": [
        (
            "apply_grain / inloop_filters",
            "conformance/post-processing, not a fidelity knob here",
        ),
    ],
    "rav1d-decode": [
        (
            "apply_grain / inloop_filters",
            "conformance/post-processing, not a fidelity knob here",
        ),
    ],
    "libgav1-decode": [
        ("post_filter_mask", "post-processing, not a fidelity knob here"),
    ],
    "libwebp-decode": [
        (
            "no_fancy_upsampling / dithering",
            "post-processing, not core decode fidelity",
        ),
    ],
    "libjpeg-turbo-decode": [
        (
            "dct_method / fancy_upsampling",
            "output forced to 8-bit RGB; scored vs golden",
        ),
    ],
}


def variant_impl_name(base: str, tag: str) -> str:
    """Series name for a derived secondary-knob variant: ``base@tag``.

    The ``@`` / ``-`` are safe in the impl-name slot of ``BenchmarkTask.name()``,
    which ``summary._parse_command_name`` recovers by splitting on the first
    ``" ("`` then ``", "``. Assert the grammar so a bad tag fails loudly at import
    rather than silently corrupting a parsed metric row."""
    name = f"{base}@{tag}"
    assert ", " not in name and " (" not in name and "=" not in name, (
        f"variant name {name!r} would break command-name parsing"
    )
    return name


def _derive_variant(
    base: "Implementation",
    schema: "TunableSchema",
    tag: str,
    overrides: Dict[str, str],
    kind: Literal["curated", "oat"],
) -> None:
    """Register one derived variant: an Implementation reusing the base binary plus
    a schema with `overrides` folded into perf_preset. Idempotent per derived name
    (so a curated variant and an identical OAT one don't double-register)."""
    vname = variant_impl_name(base.name, tag)
    if vname in TUNABLE_SCHEMAS:
        return
    IMPLEMENTATIONS.append(
        base.model_copy(update={"name": vname, "variant_kind": kind})
    )
    TUNABLE_SCHEMAS[vname] = schema.model_copy(
        update={
            "perf_preset": {**schema.perf_preset, **overrides},
            "variants": [],
            "skipped": [],
        }
    )


def _expand_variants() -> None:
    """Append derived secondary-knob series to IMPLEMENTATIONS + TUNABLE_SCHEMAS.

    For every base encoder schema: (1) its curated ``variants`` (run by default,
    ``--params variants``); then (2) a one-at-a-time (OAT) expansion of every
    enum/bool non-axis knob that is NOT documented-skipped, to each of its other
    values (``--params all``), skipping any override already covered by a curated
    variant. Each derived entry reuses the base binary (like the hand-written
    ``libjxl-lossless-encode``) and carries a distinct name, so it becomes its own
    series end-to-end without touching report/plot/summary code."""
    base_impls = {i.name: i for i in IMPLEMENTATIONS if not i.is_variant}
    for base_name, schema in list(TUNABLE_SCHEMAS.items()):
        base = base_impls.get(base_name)
        if base is None:
            continue
        seen: set = set()
        for v in schema.variants:
            seen.add(frozenset(v.overrides.items()))
            _derive_variant(base, schema, v.tag, v.overrides, "curated")
        for p in schema.params:
            if (
                p.name == schema.quality_axis
                or p.skip_reason is not None
                or p.kind not in ("enum", "bool")
            ):
                continue  # axis / documented-skip / non-categorical → not OAT-expanded
            base_val = schema.perf_preset.get(p.name, p.default)
            values = ["true", "false"] if p.kind == "bool" else (p.choices or [])
            for val in values:
                ov = {p.name: val}
                if val == base_val or frozenset(ov.items()) in seen:
                    continue
                seen.add(frozenset(ov.items()))
                _derive_variant(base, schema, f"{p.name}-{val}", ov, "oat")


_expand_variants()


# Self-consistency: every declared schema must name a real implementation, and a
# declared quality axis must appear in that schema's params + have sweep values.
assert set(TUNABLE_SCHEMAS) <= {i.name for i in IMPLEMENTATIONS}, (
    "TUNABLE_SCHEMAS names an unknown implementation"
)
for _name, _schema in TUNABLE_SCHEMAS.items():
    if _schema.quality_axis is not None:
        assert _schema.quality_axis in {p.name for p in _schema.params}, (
            f"{_name}: quality_axis '{_schema.quality_axis}' not in params"
        )
        assert _schema.quality_sweep, f"{_name}: quality_axis set but empty sweep"
    # A knob-less encoder with no quality axis only enters the quality suite when
    # it is flagged lossless (single operating point); otherwise it is skipped.
    if _schema.quality_axis is None and _schema.quality_sweep:
        raise AssertionError(f"{_name}: quality_sweep set but no quality_axis")


# Operating-point label for a lossless encoder's single point (no swept axis),
# e.g. spng / image-webp. Grammar-safe like `quality_label` (no ", " or "=").
LOSSLESS_LABEL = "lossless"


def quality_label(axis: str, value: str) -> str:
    """Grammar-safe operating-point label for one quality-sweep step, e.g.
    ``quality-80`` or ``distance-1.0``. Never contains ``", "`` or ``"="``."""
    return f"{axis}-{value}"


def slug(text: str) -> str:
    """Normalize a token into a deterministic, filesystem- and URL-safe slug:
    lowercase, every run of characters outside ``[a-z0-9._-]`` collapsed to a
    single ``-``, leading/trailing ``-`` trimmed. Stable for a given input, so
    the same (impl, label, image) always maps to the same report-asset path.

    Operating-point labels (``quality-80``, ``distance-1.0``, ``lossless``) and
    most impl names are already nearly slug-safe; this mainly maps a variant's
    ``@`` separator (e.g. ``libjxl-encode@progressive``) to ``-``."""
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-") or "x"


def image_slug(source_path: str) -> str:
    """Deterministic per-image slug from a source path's stem. Corpus sources
    are content-hash-named, so this is stable regardless of dataset ordering
    (unlike a running index, which shifts when the sample set changes)."""
    return slug(os.path.splitext(os.path.basename(source_path))[0])


def select_sweep(sweep: list[str], steps: Optional[int]) -> list[str]:
    """Pick `steps` evenly-spaced values from `sweep` (always including the
    first and last). `None` keeps the full sweep; values >= len keep all."""
    if steps is None or steps >= len(sweep) or steps <= 0:
        return list(sweep)
    if steps == 1:
        return [sweep[0]]
    n = len(sweep)
    idx = sorted({round(i * (n - 1) / (steps - 1)) for i in range(steps)})
    return [sweep[i] for i in idx]


# Rigorous-timing breadth for the optional performance overlay. Every metric row
# already carries a one-pass *relative* time; this controls how much extra,
# isolated hyperfine timing is layered on top:
#   off    — none (relative times only);
#   anchor — one point per impl (its perf preset), the default;
#   all    — every operating point, across both thread modes.
PerfMode = Literal["off", "anchor", "all"]

_PERF_ARG = Annotated[
    PerfMode,
    tyro.conf.arg(aliases=["-p"]),
    Field(
        description="Rigorous (hyperfine) timing breadth layered on the sweep: "
        "'off' relative one-pass times only; 'anchor' rigorous timing at each "
        "impl's preset point; 'all' rigorous timing at every operating point."
    ),
]


# Secondary-knob coverage for the quality sweep (issue #4). Selects which derived
# variant series (see `Variant` / `_expand_variants`) join the base encoders:
#   axis     — quality axis only (legacy single-axis behaviour / escape hatch);
#   variants — quality axis + each impl's curated secondary variants (default);
#   all      — additionally the one-at-a-time expansion of every enum/bool knob.
ParamMode = Literal["axis", "variants", "all"]

_PARAMS_ARG = Annotated[
    ParamMode,
    tyro.conf.arg(aliases=["-P"]),
    Field(
        description="Secondary-knob coverage: 'axis' = quality axis only; "
        "'variants' = curated per-impl variants (default); 'all' = also a "
        "one-at-a-time expansion of every enum/bool knob."
    ),
]


class BaseArgs(BaseModel):
    """Options shared by every sweep run."""

    formats: Annotated[
        list[ImageFormat],
        tyro.conf.EnumChoicesFromValues,
        tyro.conf.arg(aliases=["-f"]),
        Field(description="List of formats to test."),
    ] = list(ImageFormat)
    dataset: Annotated[
        DatasetId,
        tyro.conf.EnumChoicesFromValues,
        tyro.conf.arg(aliases=["-d"]),
        Field(description="Dataset to benchmark"),
    ] = DatasetId.TEST
    sample: Annotated[
        Optional[int],
        Field(
            description="Limit the maximum number of files from dataset to sample randomly"
        ),
    ] = None
    quick: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(
            description="Quick mode (2 quality points per impl; all-cores-only anchor timing)"
        ),
    ] = False
    skip_build: Annotated[
        bool, tyro.conf.FlagCreatePairsOff, Field(description="Skip compilation step")
    ] = False
    debug: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(description="Enable debug mode (more verbose output)"),
    ] = False


class RunArgs(BaseArgs):
    """Unified benchmark: sweep every encoder's quality/effort axis (and every
    decoder across the same axis of reference-encoded inputs), scoring quality at
    each operating point — encoders by IQA vs the source, decoders by PSNR vs the
    golden/reference decoder — and recording each point's one-pass relative time.
    Rigorous performance timing is an optional overlay selected by ``--perf``."""

    mode: Annotated[
        BenchmarkMode,
        tyro.conf.EnumChoicesFromValues,
        tyro.conf.arg(aliases=["-m"]),
        Field(description="Implementation-type filter (encode/decode; default both)"),
    ] = BenchmarkMode.BOTH
    perf: _PERF_ARG = "anchor"
    perf_images: Annotated[
        int,
        Field(
            description="Source images the rigorous timing overlay covers (spread "
            "across the size range). The quality sweep always covers the full "
            "--sample; timing is content-light at fixed resolution, so a handful of "
            "images stays statistically significant (each re-run ~10x) while bounding "
            "cost — the lever that keeps a full clic2025 run tractable. 0 = all "
            "sampled images."
        ),
    ] = 5
    params: _PARAMS_ARG = "variants"
    demo: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(
            description="Demo preset: a fast, deliberately NON-rigorous sweep that "
            "still fills every report section. Implies --quick plus --scaling and "
            "--effort, defaults --sample to 2 and --jobs to all logical cores, and "
            "keeps the default --perf anchor — a single run per point at maximum "
            "parallelism. For showing off the whole report, not for accurate "
            "measurements."
        ),
    ] = False
    quality_steps: Annotated[
        Optional[int],
        tyro.conf.arg(aliases=["-q"]),
        Field(
            description="Number of quality-axis points per impl (evenly sampled from "
            "the full sweep). Default: every declared point."
        ),
    ] = None
    decode_steps: Annotated[
        Optional[int],
        tyro.conf.arg(aliases=["-D"]),
        Field(
            description="Number of decoder operating points (input bitrates) per "
            "format, sampled from the reference encoder's sweep — decoupled from "
            "--quality-steps. Decode cost/fidelity is ~flat across bitrate, so a few "
            "points suffice; default 3. Use 0 for the full encoder axis (legacy)."
        ),
    ] = 3
    iterations: Annotated[
        int,
        tyro.conf.arg(aliases=["-i"]),
        Field(description="Iterations per rigorous (hyperfine) timing benchmark"),
    ] = 10
    warmup: Annotated[
        int,
        tyro.conf.arg(aliases=["-w"]),
        Field(description="Warmup iterations for rigorous timing"),
    ] = 2
    jobs: Annotated[
        Optional[int],
        tyro.conf.arg(aliases=["-j"]),
        Field(
            description="Parallel scoring workers (default: physical core count). Each "
            "encode/decode runs single-threaded; peak memory scales with this."
        ),
    ] = None
    keep_temp: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(
            description="Keep each task's encoded/decoded temp files (and the staging "
            "dir) for inspection. Default off: temp files are deleted as each task is "
            "scored, bounding peak disk use on large sweeps."
        ),
    ] = False
    report_images: Annotated[
        bool,
        Field(
            description="Persist every quality-pass result's exact encoded artifact "
            "(plus each source) into the bundle's assets/ tree, so the report's data "
            "points are clickable into an image gallery. Default on. --no-report-images "
            "restores the lean behavior (no images kept) for very large sweeps or to "
            "stay well under a static host's file-count limit."
        ),
    ] = True
    pin_cores: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(description="Pin rigorous timing benchmarks to specific CPU cores"),
    ] = False
    measure_memory: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(description="Measure peak memory usage (rigorous timing overlay)"),
    ] = False
    scaling: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(
            description="Add a scaling suite: time encode/decode vs pixel count on a "
            "downscaled resolution ladder and fit a per-codec exponent (time ∝ "
            "pixels^k), exposing super-linear codecs (e.g. AVIF) vs linear (JPEG)."
        ),
    ] = False
    scaling_images: Annotated[
        int,
        Field(
            description="Number of (largest) source images downscaled for the scaling "
            "ladder."
        ),
    ] = 3
    scaling_ladder: Annotated[
        Optional[list[float]],
        Field(
            description="Megapixel rungs for the scaling ladder (downscale-only). "
            "Default: 0.25 0.5 1 2."
        ),
    ] = None
    effort: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(
            description="Add an effort/speed suite: sweep each lossy codec's pinned "
            "effort knob (AVIF speed, JXL effort, WebP method) at fixed quality and "
            "record the time/size/quality tradeoff. Off by default."
        ),
    ] = False
    effort_images: Annotated[
        int,
        Field(
            description="Number of (largest) source images used for the effort suite "
            "(downscaled to ~1 MP)."
        ),
    ] = 4

    @model_validator(mode="after")
    def _apply_demo(self) -> "RunArgs":
        """Expand the ``--demo`` preset into the concrete flags the runner reads.

        Demo is a fast, complete-coverage sweep (see the field help): the quick
        metric pass (2 quality points, 1 decode point, all-cores-only timing,
        single-run hyperfine) *plus* both opt-in suites so the scaling and effort
        report sections have data, a small default sample, and all logical cores
        for the always-parallel metric pass. ``--perf`` stays at its default
        ``anchor`` so the performance section is populated cheaply. Explicit
        ``--sample`` / ``--jobs`` still win; ``--scaling`` / ``--effort`` /
        ``--quick`` only turn on, so forcing them on is consistent."""
        if self.demo:
            self.quick = True
            self.scaling = True
            self.effort = True
            if self.sample is None:
                self.sample = 2
            if self.jobs is None:
                self.jobs = os.cpu_count()
        return self


class QualityArgs(RunArgs):
    """Backward-compatible alias for ``run --perf off``: the quality sweep with
    relative one-pass timing only (no rigorous hyperfine pass)."""

    perf: _PERF_ARG = "off"


class PerfArgs(RunArgs):
    """Backward-compatible alias for ``run --perf all``: the full sweep with
    rigorous hyperfine timing at every operating point. Quality is still scored
    (now always collected), so this is a superset of the old performance suite."""

    perf: _PERF_ARG = "all"


class AllArgs(RunArgs):
    """Backward-compatible alias for ``run`` (``--perf anchor``): the quality sweep
    plus rigorous timing at each impl's preset point."""

    pass


class CleanArgs(BaseModel):
    """Clean build artifacts."""

    yes: Annotated[
        bool,
        tyro.conf.arg(
            aliases=["-y"],
        ),
        tyro.conf.FlagCreatePairsOff,
        Field(description="Skip confirmation prompt"),
    ] = False


class CompileArgs(BaseModel):
    """Compile the project."""

    implementations: Annotated[
        Optional[list[str]],
        Field(description="List of implementations to compile."),
    ] = None


class SetupArgs(BaseModel):
    """Download and verify benchmark datasets."""

    dataset: Annotated[
        Optional[DatasetId],
        tyro.conf.EnumChoicesFromValues,
        tyro.conf.arg(aliases=["-d"]),
        Field(description="Dataset to set up (default: all)"),
    ] = None
    force: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(description="Force re-download/regenerate even if already present"),
    ] = False
    verify_only: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(description="Only verify integrity, do not download or regenerate"),
    ] = False


class DocsArgs(BaseModel):
    """Generate (or verify) the per-implementation tunables overview, the
    high-level view of every impl's knobs/variants/skips synthesized from
    TUNABLE_SCHEMAS (issue #4). Writes docs/tunables.md."""

    check: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(
            description="Verify docs/tunables.md matches the schemas instead of "
            "writing it; exit non-zero on drift (for CI)."
        ),
    ] = False


class ReportArgs(BaseModel):
    """Rebuild ``report.html`` for an EXISTING results bundle from its raw metrics,
    without re-running the (expensive) benchmark. Only the presentation is rebuilt;
    the embedded raw measurements are reused exactly as they are on disk.

    DANGER: this assumes the bundle's raw data was produced by a codebase
    compatible with the *current* report code. If the metrics schema or a codec's
    behaviour changed since the bundle was made, the regenerated graphs can be
    silently wrong. Re-run a full sweep if in doubt. Requires the
    ``--assume-results-current`` opt-in (or an interactive confirmation)."""

    directory: Annotated[
        str,
        tyro.conf.Positional,
        Field(description="Path to an existing results bundle directory."),
    ]
    assume_results_current: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(
            description="Confirm you understand this ONLY rebuilds the HTML from "
            "reused raw metrics and assumes the codebase/results still match. "
            "Without it you are prompted interactively."
        ),
    ] = False


CliEntry = Union[
    Annotated[RunArgs, tyro.conf.subcommand(name="run")],
    Annotated[QualityArgs, tyro.conf.subcommand(name="quality")],
    Annotated[PerfArgs, tyro.conf.subcommand(name="perf")],
    Annotated[AllArgs, tyro.conf.subcommand(name="all")],
    Annotated[ReportArgs, tyro.conf.subcommand(name="report")],
    Annotated[CleanArgs, tyro.conf.subcommand(name="clean")],
    Annotated[CompileArgs, tyro.conf.subcommand(name="compile")],
    Annotated[SetupArgs, tyro.conf.subcommand(name="setup")],
    Annotated[DocsArgs, tyro.conf.subcommand(name="docs")],
]


class BenchmarkTask(BaseModel):
    """
    A single benchmark task.
    """

    impl: Implementation
    # Concrete tunables passed to the binary as --param key=value.
    params: Dict[str, str]
    # Short, grammar-safe token for this operating point (no ',' or '='), e.g.
    # "perf" for the performance preset or "q80"/"d1.0" for a quality-sweep step.
    # Used in the hyperfine command name, identifiers, and plot keys.
    label: str
    input_path: str
    source_path: str
    iterations: int
    warmup: int
    threads: int
    discard_output: bool
    measure_memory: bool
    pin_cores: bool
    # Decode-only: True when the encoded input was produced by the format's
    # dedicated lossless reference encoder (LOSSLESS_REFERENCE_ENCODERS, issue #21),
    # so the added lossless decode path is distinguishable from the lossy one in the
    # metrics. Always False for encoder/null tasks and for mono-mode formats.
    input_lossless: bool = False

    def format_as_str(self) -> str:
        """
        Return string representation of implementation format (if any).
        """
        return self.impl.format.value if self.impl.format else "null"

    def name(self) -> str:
        # The operating-point label and thread mode are part of the name so a
        # single run can sweep both: they keep hyperfine command names unique and
        # let the summary parser recover each dimension. The basename is kept last
        # so a comma in a filename can never shift the earlier fields. The label
        # never contains ", " (enforced where labels are minted).
        return (
            f"{self.impl.name} ({self.format_as_str()}, {self.impl.type.value}, "
            f"{self.label}, t{self.threads}, {os.path.basename(self.input_path)})"
        )

    def identifier(self) -> str:
        """
        Unique identifier for this task.
        """
        return f"{self.impl.name}_{self.format_as_str()}_{self.label}_{os.path.basename(self.input_path)}_{generate_base32_string(8)}"

    def output_ext(self) -> Optional[ImageFormats]:
        """
        Output extension (e.g. .jpg, .jxl, .ppm) for this task.
        """
        if self.impl.type == BenchmarkType.DECODE:
            return PPMImageFormat.PPM
        else:
            return self.impl.format

    def asset_relpath(self) -> Optional[str]:
        """Bundle-root-relative path where this result's *exact* encoded artifact
        is persisted for the report's image gallery (``None`` for a null /
        unformatted task). The directory groups exactly by a chart data point's
        ``(format, impl, label)``::

            encode: assets/<fmt>/<impl>/<label>/<image>.<ext>   # the produced output
            decode: assets/<fmt>/_inputs/<label>/<image>.<ext>  # the consumed input

        A decode point's image is the bitstream it decoded, which is identical
        across every decoder of that input, so decode rows share one ``_inputs``
        file (deduped) rather than one copy per decoder.

        Deterministic, unlike :func:`identifier` — whose random suffix exists
        only to keep per-task temp deletion race-free."""
        if self.impl.format is None:
            return None
        fmt = self.impl.format.value
        img = image_slug(self.source_path)
        label = slug(self.label)
        if self.impl.type == BenchmarkType.DECODE:
            ext = os.path.splitext(self.input_path)[1].lstrip(".") or "bin"
            return f"assets/{fmt}/_inputs/{label}/{img}.{ext}"
        ext = FORMAT_EXT_MAP[self.impl.format]
        return f"assets/{fmt}/{slug(self.impl.name)}/{label}/{img}.{ext}"

    def source_asset_relpath(self) -> str:
        """Bundle-root-relative path for this task's original source image,
        stored once per image so the gallery can toggle original vs.
        reconstruction. The codecs' actual input is the canonical PPM; it is
        published here losslessly as PNG so a browser can render it."""
        return f"assets/_sources/{image_slug(self.source_path)}.png"

    def cmd(
        self,
        output_path: str,
        iterations: Optional[int] = None,
        warmup: Optional[int] = None,
        discard: Optional[bool] = None,
    ) -> str:
        """
        Generate command based on output path.

        Optional `iterations` and `warmup` override the task's stored values,
        useful for one-shot metric collection runs. `discard` overrides the
        task's discard policy — metric collection passes `discard=False` so the
        binary actually writes an output file to measure/score.
        """

        binary = self.impl.bin
        use_discard = self.discard_output if discard is None else discard

        # Build command
        cmd_parts = [
            binary,
            "--input",
            self.input_path,
            "--output",
            output_path,
            "--iterations",
            str(iterations if iterations is not None else self.iterations),
            "--warmup",
            str(warmup if warmup is not None else self.warmup),
            "--threads",
            str(self.threads),
        ]

        # Emit tunables as repeated --param key=value (sorted for deterministic
        # command strings). The binary reads only the keys it understands.
        for key in sorted(self.params):
            cmd_parts += ["--param", f"{key}={self.params[key]}"]

        if use_discard:
            cmd_parts.append("--discard")

        # Wrap with taskset for core pinning (pin to cores 0-3 for consistency)
        if self.pin_cores:
            cmd_parts = ["taskset", "-c", "0-3"] + cmd_parts

        command = shlex.join(cmd_parts)

        return command


# Build list is list[BenchmarkTask]
BenchList = list[BenchmarkTask]

# A timing chart is produced per (format, type, operating-point label); the two
# thread modes are grouped *within* a chart, so threads are not part of the key.
BenchmarkKey = Tuple[ImageFormat, BenchmarkType, str]


def filename_from_key(key: BenchmarkKey) -> str:
    """
    Generate a filename-safe string from a benchmark key.

    Does not include file extension.
    """
    format, bench_type, label = key
    return f"{format.value}_{bench_type.value}_{label}_results"


class BenchmarkMetrics(TypedDict):
    name: str
    impl: str
    # Implementation language (e.g. "c", "c++", "rust", "c/asm") and build
    # ecosystem ("cpp"/"rust"). These differ for e.g. rav1d: a Rust library
    # benchmarked through the C++ harness (lang="rust", build="cpp").
    lang: str
    build: str
    # Operating-point label (e.g. "perf", "q80", "d1.0").
    label: str
    # Serialized tunables for this point (e.g. "effort=7;quality=80").
    params: str
    # The swept knob name and its value for this point (empty strings for
    # single-point/decode rows). Used to order/annotate rate-distortion curves
    # (lossy) or the effort axis of the lossless efficiency view (issue #26).
    quality_axis: str
    quality_value: str
    input_path: str
    source_path: str
    # Encoded artifact size in bytes: the encoder's output, or — for a decoder —
    # the encoded input it consumed (the decoded PPM is raw/format-invariant). bpp
    # is derived from this, so it is input bitrate for decode rows. 0 on error.
    filesize: int
    # IQA scores from the iqa crate (via the iqa-cli binary). ssimulacra2:
    # 100 = identical, -1.0 on error. psnr in dB; None when non-finite
    # (pixel-identical -> +inf) or unavailable. ssim: 0..1, 1.0 = identical
    # (higher is better). butteraugli: >=0, 0.0 = identical (lower is better).
    # ssim/butteraugli are None when unavailable.
    ssimulacra2: float
    psnr: Optional[float]
    ssim: Optional[float]
    butteraugli: Optional[float]
    # What the IQA scores above are measured against: "source" for encoders (vs the
    # original image) or "golden" for decoders (vs the format's reference decoder on
    # the same input, isolating decoder fidelity from the encoder loss both share).
    # A bit-exact decoder therefore scores ∞/identical; only approximate decode
    # paths show a finite PSNR. "" for rows with no score.
    metric_basis: str
    error: Optional[str]
    type: str
    format: str
    # True for a lossless encoder's rows (issue #26): pixel-identical round-trip,
    # so excluded from rate-distortion charts / BD-rate / Pareto and shown in the
    # lossless compression-efficiency view instead. For a *decode* row this instead
    # marks that the input came from the format's dedicated lossless reference encoder
    # (issue #21), separating the added lossless decode path from the lossy one;
    # encoder-side views ignore decode rows (they gate on type == "encode"), so the
    # reuse is unambiguous.
    lossless: bool
    # Image dimension metrics
    width: int
    height: int
    megapixels: float
    bpp: float  # bits per pixel = (filesize * 8) / (width * height)
    # Wall-clock seconds for the single pass (encode or decode) that produced this
    # row (issue #29). One pass, no warmup/repeats, under the parallel worker pool,
    # so it includes contention from sibling tasks — a *relative* indicator of how
    # an operating point's cost scales, not the rigorous-timing overlay's isolated
    # measurement. 0.0 on error.
    time_s: float
    # Wall-clock seconds for the single reference-decode of THIS encoded output —
    # measured (never joined) by timing the decode the encoder path already does to
    # score IQA, so "quality vs decode time" is a real, drift-free axis. Same
    # single-pass/relative caveat as time_s. Only set on encode rows; None on decode
    # rows (their time_s already *is* a decode time) and on error.
    decode_time_s: Optional[float]
    # Rigorous-timing overlay results, merged back onto this row by matching its
    # `name` against the overlay's single-threaded (t1) hyperfine command name (see
    # runner._merge_rigorous_timing). Present ONLY on the rows the overlay actually
    # re-timed in isolation (each impl's anchor point + — for lossless encoders —
    # the min/max effort endpoints), absent otherwise. Unlike time_s these are
    # isolated, repeated-trial measurements: time_rigorous_s is the mean over
    # time_runs runs (time_runs > 1 ⇒ statistically significant; --quick's single
    # run is not), with time_rigorous_stddev_s the run-to-run spread. The report
    # uses these to anchor the lossless size-vs-effort endpoints and to decide when
    # a timing axis is rigorous rather than single-pass.
    time_rigorous_s: NotRequired[Optional[float]]
    time_rigorous_stddev_s: NotRequired[Optional[float]]
    time_runs: NotRequired[Optional[int]]
    # Definitive bit-exactness from a direct byte compare of the produced raster
    # against the reference it is scored against (decode rows: vs source for a
    # losslessly-encoded input, else vs the golden decoder; encode rows: vs source,
    # lossless encoders only). True/False where a ground-truth compare applies;
    # None where it does not (lossy encode rows) or the raster could not be read.
    # Independent of iqa-cli's PSNR (no rounding / non-finite inference).
    bit_exact: Optional[bool]
    # Diagnostics for a non-bit-exact row (both None when bit-exact or N/A): the
    # byte offset of the first differing sample, and the count of differing bytes.
    bit_exact_first_diff: Optional[int]
    bit_exact_diff_count: Optional[int]
    # Bundle-root-relative paths to the images backing this data point in the
    # report gallery (see BenchmarkTask.asset_relpath / source_asset_relpath):
    # the exact encoded artifact for this result (encoder output, or — for a
    # decoder — the bitstream it decoded) and the original source image. Both
    # None when image capture is off (--no-report-images) or the row errored.
    asset_path: Optional[str]
    source_asset: Optional[str]
