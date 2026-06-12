"""Enums, Pydantic models, constants, type aliases, and helpers."""

import os
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
    Optional,
    Tuple,
    TypedDict,
    Union,
)

import tyro
from pathlib import Path
from pydantic import BaseModel, Field


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


class Dataset:
    def __init__(
        self, description: str, files: Union[list[str], Callable[[], list[str]]]
    ):
        self.description = description
        self._files = files

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


DATASETS: Dict[str, Dataset] = {
    "test": Dataset(
        description="Single test file (legacy)",
        files=["data/test.ppm"],
    ),
    "kodak": Dataset(
        description="KODAK PhotoCD dataset (24 images, ~0.4MP)",
        files=lambda: [f"data/kodak/kodim{i:02d}.png" for i in range(1, 25)],
    ),
    "div2k": Dataset(
        description="DIV2K selected subset (20 diverse high-res images)",
        files=_get_div2k_files,
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
    # AVIF
    Implementation(
        name="rav1e-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-rav1e-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.AVIF,
    ),
    # zenavif: pure-Rust AVIF codec (AGPL-3.0, imazen/zenavif). Decode via
    # rav1d-safe, encode via zenravif.
    Implementation(
        name="zenavif-encode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zenavif-encode",
        type=BenchmarkType.ENCODE,
        format=ImageFormat.AVIF,
    ),
    Implementation(
        name="zenavif-decode",
        build="rust",
        lang="rust",
        bin="target/release/bench-zenavif-decode",
        type=BenchmarkType.DECODE,
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


def _jpeg_full_schema() -> "TunableSchema":
    """JPEG encoders exposing quality + progressive + chroma subsampling
    (libjpeg-turbo, mozjpeg, jpegli, jpeg-encoder)."""
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
    )


def _avif_schema() -> "TunableSchema":
    """AVIF encoders via libavif (libavif, svt-av1) and rav1e share a 0-100
    quality knob plus a speed preset and chroma format."""
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
            Tunable(name="speed", kind="int", default="6", min=0, max=10),
            Tunable(
                name="yuv",
                kind="enum",
                default="420",
                choices=["420", "444"],
                description="Chroma subsampling (YUV format)",
            ),
        ],
        quality_axis="quality",
        quality_sweep=_AVIF_QUALITY_SWEEP,
        perf_preset={"quality": "65", "speed": "6", "yuv": "420"},
    )


TUNABLE_SCHEMAS: Dict[str, "TunableSchema"] = {
    # --- JPEG ---
    "libjpeg-turbo-encode": _jpeg_full_schema(),
    "mozjpeg-encode": _jpeg_full_schema(),
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
    # --- WEBP ---
    "libwebp-encode": TunableSchema(
        params=[
            Tunable(name="quality", kind="float", default="75", min=0, max=100),
            Tunable(name="method", kind="int", default="4", min=0, max=6),
            Tunable(name="lossless", kind="bool", default="false"),
        ],
        quality_axis="quality",
        quality_sweep=_WEBP_QUALITY_SWEEP,
        perf_preset={"quality": "75", "method": "4", "lossless": "false"},
    ),
    # image-webp encodes lossless WebP only (crate limitation) and exposes no
    # knob, so it has no rate-distortion curve. Flagged lossless (issue #26) so it
    # contributes one operating point to the lossless compression-efficiency view.
    "image-webp-encode": TunableSchema(lossless=True),
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
            Tunable(name="speed", kind="int", default="6", min=0, max=10),
            Tunable(name="chroma", kind="enum", default="420", choices=["420", "444"]),
        ],
        quality_axis="quality",
        quality_sweep=_AVIF_QUALITY_SWEEP,
        perf_preset={"quality": "65", "speed": "6", "chroma": "420"},
    ),
    "libavif-encode": _avif_schema(),
    "svt-av1-encode": _avif_schema(),
    # zenavif 0.1.x exposes quality + speed (no public chroma-subsampling knob).
    "zenavif-encode": TunableSchema(
        params=[
            Tunable(
                name="quality",
                kind="int",
                default="65",
                min=1,
                max=100,
                description="AVIF quality 1-100 (mapped to AV1 quantizer)",
            ),
            Tunable(name="speed", kind="int", default="6", min=1, max=10),
        ],
        quality_axis="quality",
        quality_sweep=_AVIF_QUALITY_SWEEP,
        perf_preset={"quality": "65", "speed": "6"},
    ),
    # --- JXL ---
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
        ],
        quality_axis="distance",
        quality_sweep=_JXL_DISTANCE_SWEEP,
        perf_preset={"distance": "1.0", "effort": "7"},
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
    ),
    "zune-png-encode": TunableSchema(
        params=[Tunable(name="effort", kind="int", default="4", min=0, max=9)],
        quality_axis="effort",
        quality_sweep=_PNG_ZLIB_SWEEP,
        perf_preset={"effort": "4"},
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
    quality_steps: Annotated[
        Optional[int],
        tyro.conf.arg(aliases=["-q"]),
        Field(
            description="Number of quality-axis points per impl (evenly sampled from "
            "the full sweep). Default: every declared point."
        ),
    ] = None
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


CliEntry = Union[
    Annotated[RunArgs, tyro.conf.subcommand(name="run")],
    Annotated[QualityArgs, tyro.conf.subcommand(name="quality")],
    Annotated[PerfArgs, tyro.conf.subcommand(name="perf")],
    Annotated[AllArgs, tyro.conf.subcommand(name="all")],
    Annotated[CleanArgs, tyro.conf.subcommand(name="clean")],
    Annotated[CompileArgs, tyro.conf.subcommand(name="compile")],
    Annotated[SetupArgs, tyro.conf.subcommand(name="setup")],
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
    # lossless compression-efficiency view instead.
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
