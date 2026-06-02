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


class QualityTier(str, Enum):
    WEB_LOW = "web-low"
    WEB_HIGH = "web-high"
    ARCHIVAL = "archival"


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


# Fixed experiment protocol (issue #9): every `./bench run` sweeps these
# internally instead of exposing them as CLI knobs, so a single run produces a
# complete, self-comparable dataset. Only `--dataset` remains a true variable.
#
# Quality tiers are swept low -> high (the natural quality-vs-bpp curve order).
ALL_QUALITY_TIERS: list[QualityTier] = list(QualityTier)
# Threading configurations: single-threaded (per-core efficiency) then all-cores
# (real-world throughput). Output bytes are identical across these, so metrics
# are collected for only one of them; only timing/memory vary by thread count.
THREAD_MODES: list[int] = [1, 0]


class RunArgs(BaseModel):
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
    mode: Annotated[
        BenchmarkMode,
        tyro.conf.EnumChoicesFromValues,
        tyro.conf.arg(aliases=["-m"]),
        Field(description="Benchmark mode (subset filter; default runs both)"),
    ] = BenchmarkMode.BOTH
    iterations: Annotated[
        int,
        tyro.conf.arg(aliases=["-i"]),
        Field(description="Iterations per benchmark"),
    ] = 10
    warmup: Annotated[
        int,
        tyro.conf.arg(aliases=["-w"]),
        Field(description="Warmup iterations"),
    ] = 2
    sample: Annotated[
        Optional[int],
        Field(
            description="Limit the maximum number of files from dataset to sample randomly"
        ),
    ] = None

    # Booleans automatically become flags.
    pin_cores: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(description="Pin benchmarks to specific CPU cores"),
    ] = False
    quick: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(description="Quick mode (single iteration) (only for testing)"),
    ] = False
    measure_memory: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(description="Measure peak memory usage"),
    ] = False
    skip_build: Annotated[
        bool, tyro.conf.FlagCreatePairsOff, Field(description="Skip compilation step")
    ] = False
    no_benchmarks: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(description="Do not run benchmarks"),
    ] = False
    no_metrics: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(description="Do not collect metrics"),
    ] = False
    debug: Annotated[
        bool,
        tyro.conf.FlagCreatePairsOff,
        Field(description="Enable debug mode (more verbose output)"),
    ] = False


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
    Annotated[CleanArgs, tyro.conf.subcommand(name="clean")],
    Annotated[CompileArgs, tyro.conf.subcommand(name="compile")],
    Annotated[SetupArgs, tyro.conf.subcommand(name="setup")],
]


class BenchmarkTask(BaseModel):
    """
    A single benchmark task.
    """

    impl: Implementation
    quality: QualityTier
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
        # Quality tier and thread mode are part of the label because a single
        # run now sweeps both — they keep hyperfine command names unique and let
        # the summary parser recover each dimension. The basename is kept last so
        # a comma in a filename can never shift the earlier fields.
        return (
            f"{self.impl.name} ({self.format_as_str()}, {self.impl.type.value}, "
            f"{self.quality.value}, t{self.threads}, {os.path.basename(self.input_path)})"
        )

    def identifier(self) -> str:
        """
        Unique identifier for this task.
        """
        return f"{self.impl.name}_{self.format_as_str()}_{self.quality.value}_{os.path.basename(self.input_path)}_{generate_base32_string(8)}"

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
            "--quality",
            self.quality.value,
            "--iterations",
            str(iterations if iterations is not None else self.iterations),
            "--warmup",
            str(warmup if warmup is not None else self.warmup),
            "--threads",
            str(self.threads),
        ]

        if use_discard:
            cmd_parts.append("--discard")

        # Wrap with taskset for core pinning (pin to cores 0-3 for consistency)
        if self.pin_cores:
            cmd_parts = ["taskset", "-c", "0-3"] + cmd_parts

        command = shlex.join(cmd_parts)

        return command


# Build list is list[BenchmarkTask]
BenchList = list[BenchmarkTask]

# A timing chart is produced per (format, type, quality); the two thread modes
# are grouped *within* a chart, so threads are not part of the key.
BenchmarkKey = Tuple[ImageFormat, BenchmarkType, QualityTier]


def filename_from_key(key: BenchmarkKey) -> str:
    """
    Generate a filename-safe string from a benchmark key.

    Does not include file extension.
    """
    format, bench_type, quality = key
    return f"{format.value}_{bench_type.value}_{quality.value}_results"


class BenchmarkMetrics(TypedDict):
    name: str
    impl: str
    # Implementation language (e.g. "c", "c++", "rust", "c/asm") and build
    # ecosystem ("cpp"/"rust"). These differ for e.g. rav1d: a Rust library
    # benchmarked through the C++ harness (lang="rust", build="cpp").
    lang: str
    build: str
    quality: str
    input_path: str
    source_path: str
    filesize: int
    ssimulacra2: float
    error: Optional[str]
    type: str
    format: str
    # Image dimension metrics
    width: int
    height: int
    megapixels: float
    bpp: float  # bits per pixel = (filesize * 8) / (width * height)
