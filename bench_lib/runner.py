"""Dataset/input management, benchmark execution, and metrics collection."""

import csv
import datetime
import errno
import glob
import json
import os
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, Optional, Set

import humanize
from colorama import Fore, Style
from PIL import Image as PILImage

from bench_lib.build import build_project, build_projects
from bench_lib.models import (
    DATASETS,
    FORMAT_EXT_MAP,
    IMPLEMENTATIONS,
    NULL_IMPLEMENTATIONS,
    REFERENCE_DECODERS,
    REFERENCE_ENCODERS,
    THREAD_MODES,
    BenchList,
    BenchmarkMetrics,
    BenchmarkMode,
    BenchmarkTask,
    BenchmarkType,
    CleanArgs,
    CompileArgs,
    DatasetId,
    DocsArgs,
    ImageFormat,
    ImageFormats,
    Implementation,
    LOSSLESS_LABEL,
    LOSSLESS_REFERENCE_ENCODERS,
    PerfMode,
    PPMImageFormat,
    RunArgs,
    SetupArgs,
    TunableSchema,
    find_implementation_by_name,
    quality_label,
    schema_for,
    select_sweep,
)
from bench_lib.report import generate_report_html
from bench_lib.summary import generate_summary
from bench_lib.system_info import (
    _detect_mimalloc_version,
    get_compiler_versions,
    get_library_versions,
    get_physical_cores,
    get_system_info,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# iqa-cli: published Rust binary computing IQA metrics (SSIMULACRA2 + PSNR + SSIM
# + Butteraugli) via the iqa crate. Installed from crates.io into target/bin by
# build.install_iqa_cli (see `./bench compile`), no longer built from in-repo
# source.
IQA_CLI_BIN = os.path.join(PROJECT_ROOT, "target", "bin", "iqa-cli")

DATASET_FILES_CHECKED: Set[str] = set()

# Cache input file list for re-use
INPUT_FILES_CACHE: Dict[
    tuple[DatasetId, ImageFormats, Optional[int]],
    Sequence[tuple[str, str]],
] = {}


def get_dataset_files(dataset_name: DatasetId) -> list[str]:
    """
    Get list of files for a given dataset.

    Automatically ensures the dataset is ready (downloads/generates if needed).
    """
    if dataset_name not in DATASETS:
        raise ValueError(f"Unknown dataset: {dataset_name}")

    if dataset_name not in DATASET_FILES_CHECKED:
        from bench_lib.data_setup import ensure_dataset

        ensure_dataset(dataset_name)
        DATASET_FILES_CHECKED.add(dataset_name)

    return DATASETS[dataset_name].files


def get_input_files(
    dataset_name: DatasetId,
    format: ImageFormats,
    limit: Optional[int] = None,
) -> Sequence[tuple[str, str]]:
    """
    Get list of input files for a given dataset, benchmark type.

    Returns a sequence of (input_path, source_path) tuples, where:
    - input_path: Path to the input file for the benchmark.
    - source_path: Path to the original source file (for quality comparison).

    Pre-generates input files for benchmark type if necessary. Decode inputs are
    produced by encoding each source with the format's reference encoder at that
    encoder's fixed performance preset (one file per source per format).
    """

    if (dataset_name, format, limit) in INPUT_FILES_CACHE:
        return INPUT_FILES_CACHE[(dataset_name, format, limit)]

    # Get all files for the dataset
    dataset_files = get_dataset_files(dataset_name)

    # Sample files if limit is requested
    if limit is not None and limit < len(dataset_files):
        # We use a fixed seed based on the dataset name to ensure that
        # the same subset is selected for both encode/decode passes within runs
        # if the file list is stable.
        rnd = random.Random(f"{dataset_name}_{limit}")
        dataset_files = rnd.sample(dataset_files, limit)

    # input_files: (input_file, output_file)
    input_files: list[tuple[str, str]] = []
    target_ext = FORMAT_EXT_MAP[format]

    for f in dataset_files:
        if f.lower().endswith(f".{target_ext}"):
            # Dataset file already in required format
            input_files.append((f, f))
        else:
            # Determine target file name. A single reference preset is used per
            # format, so one encoded file per source suffices (no quality suffix).
            base_path = os.path.splitext(f)[0]
            target_file = f"{base_path}.{target_ext}"

            # If target file exists, we can use it
            if os.path.exists(target_file):
                input_files.append((target_file, f))
            else:
                # Need to convert dataset file into required format
                print(f"Generating {target_file} from {f}...")

                # 1. Convert to PPM
                intermediate_ppm = f"{base_path}.ppm"
                if not os.path.exists(intermediate_ppm):
                    # Use ImageMagick to convert to 8-bit P6 PPM
                    # We force 8-bit depth as not all implementations handle 16-bit PPM
                    subprocess.run(
                        ["convert", f, "-depth", "8", intermediate_ppm],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

                # If the requested format is PPM itself, the converted P6
                # intermediate IS the target. Running the reference "encoder"
                # (null-cpp-encode) over it would overwrite the valid P6 file
                # with headerless raw RGB (it writes img.data, not a PPM), so
                # skip the encode step entirely.
                if format == PPMImageFormat.PPM:
                    input_files.append((intermediate_ppm, f))
                    continue

                # 2. Encode using reference encoder
                ref_impl_name = REFERENCE_ENCODERS.get(format)
                if not ref_impl_name:
                    raise RuntimeError(f"No reference encoder defined for {format}")

                ref_impl = find_implementation_by_name(ref_impl_name)
                if not ref_impl:
                    raise RuntimeError(f"Reference encoder {ref_impl_name} not found")

                # Ensure reference implementation is built
                # Note: We rely on the user having built everything or `run` building it.
                # If we are in `run`, builds happen before this.
                if not os.path.exists(ref_impl.bin):
                    # Try to build it on demand?
                    build_project(ref_impl)

                # Run encoder at its fixed performance preset (--param k=v).
                ref_cmd = [
                    ref_impl.bin,
                    "--input",
                    intermediate_ppm,
                    "--output",
                    target_file,
                    "--iterations",
                    "1",
                    "--warmup",
                    "0",
                    "--threads",
                    "0",
                ]
                for key, value in sorted(
                    schema_for(ref_impl.name).perf_params().items()
                ):
                    ref_cmd += ["--param", f"{key}={value}"]
                subprocess.run(
                    ref_cmd,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,  # Capture stderr to avoid spam, unless error
                )

                input_files.append((target_file, f))
    # Verify all input files exist
    missing = [f_path for f_path, _ in input_files if not os.path.exists(f_path)]
    if missing:
        raise RuntimeError(
            f"Some input files not found {len(missing)} for '{dataset_name}': {','.join(missing[:5])}"
        )

    INPUT_FILES_CACHE[(dataset_name, format, limit)] = input_files
    return input_files


def _source_to_ppm(f: str) -> str:
    """Return an 8-bit P6 PPM path for source `f`, generating it via ImageMagick
    if `f` is not already a PPM. Mirrors the conversion in `get_input_files`
    (forced 8-bit, since not every implementation handles 16-bit PPM)."""
    if f.lower().endswith(".ppm"):
        return f
    base = os.path.splitext(f)[0]
    ppm = f"{base}.ppm"
    if not os.path.exists(ppm):
        subprocess.run(
            ["convert", f, "-depth", "8", ppm],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return ppm


# Decode inputs are reference-encoded once per (dataset, format, reference encoder,
# operating point).
DECODE_INPUTS_CACHE: Dict[
    tuple[DatasetId, ImageFormats, str, str, Optional[int]],
    Sequence[tuple[str, str]],
] = {}


def get_decode_inputs(
    dataset_name: DatasetId,
    format: ImageFormat,
    ref_name: str,
    label: str,
    params: Dict[str, str],
    limit: Optional[int] = None,
) -> Sequence[tuple[str, str]]:
    """Reference-encoded decode inputs for one operating point of the sweep.

    For each (sampled) source image, encode it with the `ref_name` reference encoder
    at `params` — the same quality/effort axis the encoder sweep uses — returning
    (encoded_path, source_ppm_path) pairs. `ref_name` is the format's lossy reference
    encoder, or its lossless one when generating the lossless decode path (issue #21).
    The encoded file is named with the operating-point `label` so different quality
    levels never collide (the lossy and lossless references use distinct label axes,
    e.g. `quality-*` vs `method-*`), and the list is cached per (dataset, format,
    ref_name, label, limit).

    The decoder under test reads `encoded_path`; fidelity is scored against the
    format's golden decoder of the same input (not against the source), so
    `source_ppm_path` is returned only for reference/round-trip use."""
    key = (dataset_name, format, ref_name, label, limit)
    if key in DECODE_INPUTS_CACHE:
        return DECODE_INPUTS_CACHE[key]

    dataset_files = get_dataset_files(dataset_name)
    if limit is not None and limit < len(dataset_files):
        # Same seed as get_input_files so decode sources track encode sources.
        rnd = random.Random(f"{dataset_name}_{limit}")
        dataset_files = rnd.sample(dataset_files, limit)

    ref_impl = find_implementation_by_name(ref_name)
    if not ref_impl:
        raise RuntimeError(f"Reference encoder {ref_name} not found")
    if not os.path.exists(ref_impl.bin):
        build_project(ref_impl)

    ext = FORMAT_EXT_MAP[format]
    inputs: list[tuple[str, str]] = []
    for f in dataset_files:
        ppm = _source_to_ppm(f)
        base = os.path.splitext(f)[0]
        target = f"{base}.{label}.{ext}"
        if not os.path.exists(target):
            print(f"Generating decode input {target} from {f}...")
            cmd = [
                ref_impl.bin,
                "--input",
                ppm,
                "--output",
                target,
                "--iterations",
                "1",
                "--warmup",
                "0",
                "--threads",
                "0",
            ]
            for k, v in sorted(params.items()):
                cmd += ["--param", f"{k}={v}"]
            subprocess.run(
                cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
        inputs.append((target, ppm))

    DECODE_INPUTS_CACHE[key] = inputs
    return inputs


def _types_for_mode(mode: BenchmarkMode) -> list[BenchmarkType]:
    """Implementation types selected by the ``--mode`` filter."""
    if mode == BenchmarkMode.ENCODE:
        return [BenchmarkType.ENCODE]
    if mode == BenchmarkMode.DECODE:
        return [BenchmarkType.DECODE]
    return [BenchmarkType.ENCODE, BenchmarkType.DECODE]


def _require_bin(impl: Implementation) -> None:
    if not os.path.exists(impl.bin):
        raise RuntimeError(f"Error: Binary not found: {impl.bin}")


def _encoder_points(
    schema: TunableSchema, steps: Optional[int]
) -> list[tuple[Dict[str, str], str]]:
    """Operating points ``(params, label)`` for one encoder.

    Lossy → one point per swept quality value (rate-distortion). Lossless with an
    effort knob → one point per swept effort value (size-vs-effort, issue #26).
    Knob-less lossless → a single point. Knob-less lossy → no curve (skipped)."""
    if schema.quality_axis is None:
        if schema.lossless:
            return [(schema.perf_params(), LOSSLESS_LABEL)]
        return []
    return [
        (schema.quality_params(value), quality_label(schema.quality_axis, value))
        for value in select_sweep(schema.quality_sweep, steps)
    ]


def _encoders_for(format: ImageFormat, params_mode: str) -> list[Implementation]:
    """Encoders to sweep for `format`, filtered by the ``--params`` coverage mode.

    Secondary-knob variants are pre-expanded into ``IMPLEMENTATIONS`` by
    ``models._expand_variants`` and tagged via ``variant_kind`` (None = base,
    "curated", "oat"); here we just pick which kinds to include:
    ``axis`` → base only (legacy single-axis sweep); ``variants`` → base + curated
    (default); ``all`` → base + curated + the one-at-a-time expansion."""
    allowed: set = {None}
    if params_mode in ("variants", "all"):
        allowed.add("curated")
    if params_mode == "all":
        allowed.add("oat")
    return [
        impl
        for impl in IMPLEMENTATIONS
        if impl.format == format
        and impl.type == BenchmarkType.ENCODE
        and impl.variant_kind in allowed
    ]


def build_sweep(format: ImageFormat, dataset: DatasetId, args: RunArgs) -> BenchList:
    """Construct the unified operating-point sweep for one format.

    Every task is built once with ``threads=1`` and ``discard_output=False`` so the
    metric pass can run it a single time and score the written output; its
    wall-clock is recorded as a relative time (issue #29). The optional
    rigorous-timing overlay re-runs a selected subset under hyperfine across thread
    modes (see ``_run_timing_overlay``). Per implementation:

    - *lossy encoder* (a quality axis): one task per swept quality value — a
      rate-distortion curve (issue #8);
    - *lossless encoder* with an effort knob (PNG, lossless JXL): one task per
      swept effort value — a size-vs-effort curve (issue #26);
    - *lossless encoder* with no knob (spng, image-webp): a single point;
    - *decoder*: one task per swept input encoding (the format's reference encoder
      run at each quality level), so decode cost/fidelity trace input bitrate;
    - *null*: a single timing-only point (no format to score), used only by the
      rigorous-timing overlay.

    The ``--mode`` filter restricts to encoders and/or decoders. Null baselines
    are emitted for whichever direction(s) are selected.
    """
    types = _types_for_mode(args.mode)
    steps = 2 if args.quick else args.quality_steps
    benches: BenchList = []

    def emit(
        impl: Implementation,
        params: Dict[str, str],
        label: str,
        input_file: str,
        source_file: str,
        input_lossless: bool = False,
    ) -> None:
        benches.append(
            BenchmarkTask(
                impl=impl,
                params=params,
                label=label,
                input_path=input_file,
                source_path=source_file,
                input_lossless=input_lossless,
                # One pass writes the output so size + IQA can be scored; its
                # wall-clock is the relative time (issue #29). The timing overlay
                # clones these with discard + real iteration/thread counts.
                iterations=1,
                warmup=0,
                # Single-threaded: the metric pass runs many tasks concurrently
                # (one per physical core), so internal codec threads would only
                # oversubscribe. Output is thread-invariant.
                threads=1,
                discard_output=False,
                measure_memory=False,
                pin_cores=False,
            )
        )

    # --- Encoders: sweep the quality/effort axis (PPM in; scored vs source). ---
    # The encoder set includes secondary-knob variant series per the --params mode
    # (each a distinct base@tag impl reusing the base binary); decoders below are
    # deliberately left encoder-agnostic (reference-encoded at the preset point),
    # since a decoder cannot see which encoder knob produced its input.
    if BenchmarkType.ENCODE in types:
        encoders = _encoders_for(format, args.params)
        if encoders:
            enc_inputs = get_input_files(dataset, PPMImageFormat.PPM, args.sample)
            for impl in encoders:
                _require_bin(impl)
                for params, label in _encoder_points(schema_for(impl.name), steps):
                    for input_file, source_file in enc_inputs:
                        emit(impl, params, label, input_file, source_file)

    # --- Decoders: sweep the input-encoding axis (scored vs the golden decoder). ---
    if BenchmarkType.DECODE in types:
        _build_decoder_sweep(format, dataset, args, steps, emit)

    # --- Null: a single timing-only baseline per selected direction. ---
    for impl in NULL_IMPLEMENTATIONS:
        if impl.type not in types:
            continue
        _require_bin(impl)
        input_format = (
            PPMImageFormat.PPM if impl.type == BenchmarkType.ENCODE else format
        )
        for input_file, source_file in get_input_files(
            dataset, input_format, args.sample
        ):
            emit(impl, {}, "perf", input_file, source_file)

    return benches


def _build_decoder_sweep(
    format: ImageFormat,
    dataset: DatasetId,
    args: RunArgs,
    steps: Optional[int],
    emit: Callable[..., None],
) -> None:
    """Emit decoder tasks across the same operating-point axis as the encoders.

    Decoders take no params, so the *input* is what varies: for each operating
    point of the format's reference encoder (the same quality/effort sweep
    encoders trace), reference-encode the sources at that point and decode them.
    Decode cost and fidelity (PSNR vs the golden decoder, scored later) thus trace
    input bitrate. Knob-less reference encoders contribute a single point.

    Formats with both a lossy and a lossless mode (WebP, JXL) are swept against
    *both* their lossy (REFERENCE_ENCODERS) and lossless (LOSSLESS_REFERENCE_ENCODERS)
    reference encoders, so every decode path is exercised, not just the lossy one
    (issue #21). The lossless path is additive: if its reference binary cannot be
    built it is skipped so the lossy sweep still runs."""
    decoders = [
        impl
        for impl in IMPLEMENTATIONS
        if impl.format == format and impl.type == BenchmarkType.DECODE
    ]
    if not decoders:
        return

    ref_names: list[str] = []
    lossy_ref = REFERENCE_ENCODERS.get(format)
    if lossy_ref:
        ref_names.append(lossy_ref)
    lossless_ref = LOSSLESS_REFERENCE_ENCODERS.get(format)
    if lossless_ref and lossless_ref not in ref_names:
        ref_names.append(lossless_ref)
    if not ref_names:
        raise RuntimeError(f"No reference encoder defined for {format}")

    for ref_name in ref_names:
        ref_impl = find_implementation_by_name(ref_name)
        if not ref_impl:
            raise RuntimeError(f"Reference encoder {ref_name} not found")
        if not os.path.exists(ref_impl.bin):
            build_project(ref_impl)
        if not os.path.exists(ref_impl.bin):
            # Additive lossless path: skip so the lossy decode sweep still runs.
            print(f"Skipping decode inputs from {ref_name}: binary unavailable")
            continue

        # Mark the dedicated lossless reference's inputs as the lossless decode path
        # (issue #21), so the report separates it from the lossy path. Keyed on the
        # reference's identity, not its schema.lossless, so a lossless-only format
        # like PNG (whose sole reference is lossless) is not spuriously split.
        is_lossless_path = ref_name == lossless_ref
        schema = schema_for(ref_impl.name)
        points = _encoder_points(schema, steps)
        if not points:
            points = [(schema.perf_params(), "perf")]

        for params, label in points:
            inputs = get_decode_inputs(
                dataset, format, ref_name, label, params, args.sample
            )
            for impl in decoders:
                _require_bin(impl)
                for input_file, source_file in inputs:
                    emit(
                        impl,
                        {},
                        label,
                        input_file,
                        source_file,
                        input_lossless=is_lossless_path,
                    )


def _task_metric_fields(task: BenchmarkTask) -> Dict[str, str]:
    """Derive the operating-point metadata stored on a metric row: the label,
    the serialized params, and the swept quality knob name + value (empty for
    lossless/decode points)."""
    schema = schema_for(task.impl.name)
    axis = schema.quality_axis or ""
    return {
        "label": task.label,
        "params": ";".join(f"{k}={v}" for k, v in sorted(task.params.items())),
        "quality_axis": axis,
        "quality_value": task.params.get(axis, "") if axis else "",
    }


def _decode_to_ppm(
    task: BenchmarkTask,
    encoded_path: str,
    ppm_path: str,
    env: Optional[Dict[str, str]] = None,
) -> None:
    """Decode an encoded output back to PPM using the format's reference decoder,
    so iqa-cli can compare raw pixels (iqa does not decode codec formats).

    `env` (when set) is passed to the decoder process; the suite uses it to pin
    the decode to a single thread. `--threads 1` is also forced so codecs whose
    thread pool keys off the flag rather than env (e.g. libavif) stay capped —
    both levers are needed to avoid oversubscription under the parallel pool."""
    ref_name = REFERENCE_DECODERS.get(task.impl.format)
    if not ref_name:
        raise RuntimeError(f"No reference decoder defined for {task.impl.format}")
    ref_dec = find_implementation_by_name(ref_name)
    if not ref_dec:
        raise RuntimeError(f"Reference decoder {ref_name} not found")
    if not os.path.exists(ref_dec.bin):
        build_project(ref_dec)
    subprocess.run(
        [
            ref_dec.bin,
            "--input",
            encoded_path,
            "--output",
            ppm_path,
            "--iterations",
            "1",
            "--warmup",
            "0",
            "--threads",
            "1",
        ],
        check=True,
        stderr=subprocess.PIPE,
        env=env,
    )


def _run_iqa(
    reference_path: str,
    distorted_path: str,
    env: Optional[Dict[str, str]] = None,
) -> tuple[float, Optional[float], Optional[float], Optional[float]]:
    """Run iqa-cli on two images, returning (ssimulacra2, psnr, ssim, butteraugli).
    SSIMULACRA2 is -1.0 if missing; the rest are None when non-finite/unavailable.
    SSIM and Butteraugli are higher/lower-is-better respectively (1.0 / 0.0 =
    identical). `env` (when set) is passed through to cap iqa-cli's rayon threads
    under the parallel pool."""
    res = subprocess.run(
        [
            IQA_CLI_BIN,
            "--reference",
            reference_path,
            "--distorted",
            distorted_path,
            "--metric",
            "ssimulacra2,psnr,ssim,butteraugli",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    data = json.loads(res.stdout.strip())
    ss = data.get("ssimulacra2")
    psnr = data.get("psnr")
    ssim = data.get("ssim")
    butteraugli = data.get("butteraugli")
    return (
        float(ss) if ss is not None else -1.0,
        float(psnr) if psnr is not None else None,
        float(ssim) if ssim is not None else None,
        float(butteraugli) if butteraugli is not None else None,
    )


def _measure_one(
    task: BenchmarkTask, temp_dir: str, env: Dict[str, str], keep_temp: bool = False
) -> tuple[Optional[BenchmarkMetrics], str]:
    """Encode one task, decode + score it, and return its metric row alongside a
    preformatted status line.

    Runs inside a worker thread: it never raises and never prints, so the caller
    can print the returned line from the main thread and keep parallel output
    interleave-free. Every child process inherits `env`, which (with the tasks'
    `--threads 1`) pins it to a single thread. Returns (None, status) when the
    task has no format to measure (skipped).

    Unless `keep_temp` is set, the encoded output and decoded PPM this task writes
    are deleted as soon as it's scored — they're dead once the size + IQA numbers
    are recorded, and freeing them per task (rather than at end of sweep) bounds
    peak disk use to ~`max_workers` tasks' worth instead of the whole sweep's."""
    if task.impl.format is None or task.output_ext() is None:
        return (
            None,
            f"{Fore.BLUE}Skipped {task.name()} (null format){Style.RESET_ALL}",
        )

    # Lossless encoders (issue #26) are flagged on every row so the report/summary
    # can route them to the compression-efficiency view and out of the RD analytics.
    # For a decode row the encoder schema is empty, so the flag instead marks that the
    # input came from a lossless reference encoder (issue #21), separating the lossless
    # decode path from the lossy one.
    lossless = schema_for(task.impl.name).lossless or task.input_lossless

    # Temp files this task writes into the shared staging dir; removed in `finally`
    # (unless --keep-temp). Filenames carry a random suffix via identifier(), so
    # per-task deletion never races another worker.
    temp_files: list[str] = []
    try:
        identifier = task.identifier()
        format_ext_str = FORMAT_EXT_MAP[task.output_ext()]
        output_path = os.path.join(temp_dir, f"{identifier}.{format_ext_str}")
        temp_files.append(output_path)

        # Run implementation once (no warmup) to get the output file. Force
        # discard=False so the binary writes a real file even though timing runs
        # are always compute-only (--discard). Output is captured (not inherited)
        # so concurrent encoders don't interleave on the console.
        start_time = time.time()
        subprocess.run(
            shlex.split(task.cmd(output_path, iterations=1, warmup=0, discard=False)),
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        elapsed_time = time.time() - start_time

        # Verify implementation generated output
        if not os.path.exists(output_path):
            raise RuntimeError(
                f"Implementation {task.name()} was ran to collect metrics but "
                f"output file not found at: {output_path}"
            )

        # 1. File size. For an *encoder* the meaningful size is its encoded
        # output; for a *decoder* it is the encoded *input* it consumed (the
        # decoded PPM is raw and format-invariant), so bpp tracks input bitrate
        # either way and decode curves plot against the same axis as encode curves.
        size_path = (
            output_path if task.impl.type == BenchmarkType.ENCODE else task.input_path
        )
        try:
            filesize = os.path.getsize(size_path)
        except Exception:
            filesize = 0

        # 2. Compute IQA via iqa-cli (SSIMULACRA2 + PSNR + SSIM + Butteraugli, from
        # the iqa crate, which consumes raw pixels):
        #   - Encoders: decode the encoded output with the format's reference
        #     decoder and score against the original source ("source" basis).
        #   - Decoders: the output is already a PPM; score it against the *golden*
        #     (reference) decoder's PPM for the same input ("golden" basis). This
        #     isolates decoder fidelity from the encoder loss both share, so a
        #     bit-exact decoder scores ∞ and only approximate paths show a finite
        #     PSNR.
        if task.impl.type == BenchmarkType.ENCODE:
            distorted_ppm = os.path.join(temp_dir, f"{identifier}_decoded.ppm")
            temp_files.append(distorted_ppm)
            _decode_to_ppm(task, output_path, distorted_ppm, env=env)
            reference_ppm = task.source_path
            metric_basis = "source"
        else:
            distorted_ppm = output_path
            reference_ppm = os.path.join(temp_dir, f"{identifier}_golden.ppm")
            temp_files.append(reference_ppm)
            _decode_to_ppm(task, task.input_path, reference_ppm, env=env)
            metric_basis = "golden"
        score, psnr, ssim, butteraugli = _run_iqa(reference_ppm, distorted_ppm, env=env)

        # 3. Get image dimensions from source file
        width, height, megapixels, bpp = 0, 0, 0.0, 0.0
        dim_warning = ""
        try:
            with PILImage.open(task.source_path) as img:
                width, height = img.size
                megapixels = (width * height) / 1_000_000
                if width > 0 and height > 0 and filesize > 0:
                    bpp = (filesize * 8) / (width * height)
        except Exception as img_err:
            dim_warning = (
                f"\n  {Fore.YELLOW}Warning: Could not get dimensions: "
                f"{img_err}{Style.RESET_ALL}"
            )

        psnr_str = f"{psnr:.2f}" if psnr is not None else "∞/NA"
        ssim_str = f"{ssim:.4f}" if ssim is not None else "NA"
        ba_str = f"{butteraugli:.3f}" if butteraugli is not None else "NA"
        status = (
            f"{Fore.GREEN}✓{Style.RESET_ALL} {task.name()} — "
            f"Size: {humanize.naturalsize(filesize, binary=True)}, "
            f"SSIMULACRA2: {score:.2f}, PSNR: {psnr_str}, SSIM: {ssim_str}, "
            f"Butteraugli: {ba_str}, bpp: {bpp:.3f} "
            f"(took {elapsed_time:.1f} s){dim_warning}"
        )

        metric = BenchmarkMetrics(
            name=task.name(),
            impl=task.impl.name,
            lang=task.impl.lang,
            build=task.impl.build,
            **_task_metric_fields(task),
            metric_basis=metric_basis,
            input_path=task.input_path,
            source_path=task.source_path,
            filesize=filesize,
            ssimulacra2=score,
            psnr=psnr,
            ssim=ssim,
            butteraugli=butteraugli,
            error=None,
            type=task.impl.type.value,
            format=task.impl.format.value,
            lossless=lossless,
            width=width,
            height=height,
            megapixels=megapixels,
            bpp=bpp,
            time_s=elapsed_time,
        )
        return (metric, status)
    except Exception as e:
        detail = ""
        if isinstance(e, subprocess.CalledProcessError):
            if e.stderr:
                detail += (
                    f"\n  {Fore.YELLOW}Standard Error Output:"
                    f"\n  {Fore.WHITE}{e.stderr.strip()}{Style.RESET_ALL}"
                )
            if e.stdout:
                detail += (
                    f"\n  {Fore.YELLOW}Standard Output (at time of failure):"
                    f"\n  {Fore.WHITE}{e.stdout.strip()}{Style.RESET_ALL}"
                )
        status = (
            f"{Fore.RED}✗ Error running {task.name()}: {e}{Style.RESET_ALL}{detail}"
        )
        metric = BenchmarkMetrics(
            name=task.name(),
            impl=task.impl.name,
            lang=task.impl.lang,
            build=task.impl.build,
            **_task_metric_fields(task),
            metric_basis=(
                "source" if task.impl.type == BenchmarkType.ENCODE else "golden"
            ),
            input_path=task.input_path,
            source_path=task.source_path,
            filesize=0,
            ssimulacra2=-1.0,
            psnr=None,
            ssim=None,
            butteraugli=None,
            error=str(e),
            type=task.impl.type.value,
            format=task.impl.format.value,
            lossless=lossless,
            width=0,
            height=0,
            megapixels=0.0,
            bpp=0.0,
            # The run may have failed before/within the timed pass; the time is
            # meaningless for a failed row, so record 0.0 (also: elapsed_time may
            # be unbound if subprocess.run raised).
            time_s=0.0,
        )
        return (metric, status)
    finally:
        # Free this task's temp files the moment it's scored (size + IQA already
        # captured), so a huge sweep doesn't accumulate every encode+decode on
        # disk at once. Runs on both the success and error returns above. The
        # error path may not have written some files — tolerate that.
        if not keep_temp:
            for path in temp_files:
                try:
                    os.remove(path)
                except OSError:
                    pass


def _ensure_reference_decoders(benches: BenchList) -> None:
    """Build any missing reference decoders serially before the parallel pool.

    `_decode_to_ppm` lazily builds a missing decoder, but `build_rust_project`
    takes no cross-thread lock and would race on cargo's target dir if several
    workers triggered it at once. Resolve + build them up front instead. Normally
    a no-op (the suite builds everything before running) — this guards the
    `--skip-build` path."""
    seen: Set[str] = set()
    for task in benches:
        if task.impl.format is None:
            continue
        ref_name = REFERENCE_DECODERS.get(task.impl.format)
        if not ref_name or ref_name in seen:
            continue
        seen.add(ref_name)
        ref_dec = find_implementation_by_name(ref_name)
        if ref_dec and not os.path.exists(ref_dec.bin):
            build_project(ref_dec)


def generate_metrics(
    benches: BenchList,
    result_dir: str,
    max_workers: int,
    keep_temp: bool = False,
) -> list[BenchmarkMetrics]:
    """Generate file size and visual quality metrics, encoding tasks in parallel.

    Up to `max_workers` encode→decode→score pipelines run concurrently; each
    child is pinned to one thread, so a pool sized to the physical core count
    saturates the CPU without oversubscribing (issue #23). Tasks are dispatched
    in binary-sorted order so an encoder's runs cluster in time, keeping its
    binary + shared libs hot in cache.

    By default each task's temp files are deleted as soon as it's scored, so peak
    disk use stays bounded on large sweeps; `keep_temp` keeps every intermediate
    (and the staging dir) for inspection instead."""
    print(f"{Fore.BLUE}{'=' * 70}\nCOLLECTING METRICS\n{'=' * 70}\n")

    temp_dir = tempfile.mkdtemp()
    if keep_temp:
        print(f"Temporary outputs stored in: {temp_dir}")
    else:
        print(f"Staging temp outputs in: {temp_dir} (freed per task as scored)")
    print(f"Encoding with {max_workers} parallel worker(s), 1 thread each\n")

    # Pin every child process to a single thread: covers rayon-/OMP-based codecs
    # and iqa-cli. The encode/decode also pass --threads 1 for codecs (e.g.
    # libavif) whose internal pool keys off the flag rather than these env vars.
    env = {**os.environ, "RAYON_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}

    # Build missing reference decoders up front (serial) to avoid a cargo race
    # inside the worker threads.
    _ensure_reference_decoders(benches)

    # Dispatch same-binary tasks together so their executable stays cache-hot;
    # ThreadPoolExecutor pulls from the queue in submission order.
    ordered = sorted(benches, key=lambda t: (t.impl.bin, t.label, t.input_path))

    metrics: list[BenchmarkMetrics] = []
    total = len(ordered)
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(_measure_one, task, temp_dir, env, keep_temp)
                for task in ordered
            ]
            for done, future in enumerate(as_completed(futures), start=1):
                metric, status = future.result()
                print(f"[{done}/{total}] {status}")
                if metric is not None:
                    metrics.append(metric)
    finally:
        # Default: drop the now-empty staging dir (plus any error-path stragglers
        # the per-task cleanup couldn't write). With --keep-temp, leave everything
        # in place and point the user at it.
        if keep_temp:
            print(f"\nTemporary outputs preserved at: {temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)

    return metrics


def measure_memory(result_dir: str, commands: list[str], command_names: list[str]):
    """Measure peak memory usage using /usr/bin/time."""

    print("\n" + "=" * 70)
    print("MEASURING MEMORY USAGE")
    print("=" * 70)
    print()

    memory_data = []

    for cmd, name in zip(commands, command_names):
        print(f"Measuring: {name}")

        # Remove any core pinning wrapper for clean memory measurement
        cmd_parts = cmd.split()
        system = platform.system()

        # Remove Linux 'taskset' wrapper
        if system == "Linux" and "taskset" in cmd_parts:
            try:
                idx = cmd_parts.index("taskset")
                # Find the next argument after core list (e.g., 'taskset -c 0-3 ...')
                # Accept both '-c 0-3' and '-c', '0-3'
                if cmd_parts[idx + 1] == "-c":
                    # taskset -c 0-3 ...
                    cmd_parts = cmd_parts[idx + 3 :]
                else:
                    # taskset 0-3 ...
                    cmd_parts = cmd_parts[idx + 2 :]

                cmd = " ".join(cmd_parts)
            except Exception:
                pass

        # Remove macOS 'cpuset' wrapper (if ever used)
        elif system == "Darwin" and "cpuset" in cmd_parts:
            try:
                idx = cmd_parts.index("cpuset")
                # cpuset -l 0-3 -- ...
                if cmd_parts[idx + 1] == "-l":
                    # Find '--' separator
                    if "--" in cmd_parts:
                        sep = cmd_parts.index("--", idx)
                        cmd_parts = cmd_parts[sep + 1 :]
                    else:
                        cmd_parts = cmd_parts[idx + 3 :]
                else:
                    cmd_parts = cmd_parts[idx + 1 :]
                cmd = " ".join(cmd_parts)
            except Exception:
                pass

        # On Windows, skip pinning (no-op)
        # (No wrapper expected)

        # Run with /usr/bin/time -v (Linux), /usr/bin/time -l (macOS), warn on Windows
        if system == "Linux":
            time_cmd = ["/usr/bin/time", "-v"] + cmd.split()
        elif system == "Darwin":
            time_cmd = ["/usr/bin/time", "-l"] + cmd.split()
        else:
            print("  Warning: Memory measurement not supported on this platform.")
            memory_data.append({"name": name, "peak_rss_mb": 0, "peak_rss_kb": 0})
            continue

        try:
            result = subprocess.run(
                time_cmd, capture_output=True, text=True, timeout=120
            )
            stderr = result.stderr
            # Linux: Parse 'Maximum resident set size (kbytes): NNNN'
            if system == "Linux":
                rss_match = re.search(
                    r"Maximum resident set size \(kbytes\): (\d+)", stderr
                )
                peak_rss_kb = int(rss_match.group(1)) if rss_match else 0
                peak_rss_mb = peak_rss_kb / 1024.0
            # macOS: Parse 'maximum resident set size' (bytes)
            elif system == "Darwin":
                rss_match = re.search(r"maximum resident set size\s+(\d+)", stderr)
                peak_rss_kb = int(rss_match.group(1)) / 1024.0 if rss_match else 0
                peak_rss_mb = peak_rss_kb / 1024.0
            else:
                peak_rss_kb = 0
                peak_rss_mb = 0

            memory_data.append(
                {"name": name, "peak_rss_mb": peak_rss_mb, "peak_rss_kb": peak_rss_kb}
            )

        except subprocess.TimeoutExpired:
            print(f"  Warning: Timeout measuring {name}")
            memory_data.append({"name": name, "peak_rss_mb": 0, "peak_rss_kb": 0})
        except Exception as e:
            print(f"  Warning: Error measuring {name}: {e}")
            memory_data.append({"name": name, "peak_rss_mb": 0, "peak_rss_kb": 0})

    # Write CSV
    csv_path = f"{result_dir}/memory.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "peak_rss_mb", "peak_rss_kb"])
        writer.writeheader()
        writer.writerows(memory_data)

    print(f"\n✓ Memory data written to {csv_path}")


def _new_result_dir() -> str:
    """Create and return a fresh timestamped results directory."""
    os.makedirs("results", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    result_dir = f"results/{timestamp}"
    os.makedirs(result_dir, exist_ok=True)
    return result_dir


def _base_manifest() -> dict:
    """System/compiler/library/allocator metadata shared by both suites."""
    return {
        **get_system_info(),
        "compiler": get_compiler_versions(),
        "libraries": get_library_versions(),
        "allocator": f"mimalloc {_detect_mimalloc_version()}",
    }


def _dataset_manifest(args: RunArgs) -> dict:
    """Dataset provenance for the run's benchmark_config: the id, its human
    description, the canonical homepage (None for generated datasets), and the
    sample cap. Lets the report describe *what* was benchmarked and link to it
    without re-importing the dataset registry."""
    # DATASETS is keyed by the dataset's string value; the str-Enum member hashes
    # and compares equal to it, so look it up directly (str() would yield the
    # "DatasetId.X" repr and miss).
    ds = DATASETS.get(args.dataset)
    return {
        "dataset": args.dataset,
        "dataset_description": ds.description if ds else None,
        "dataset_homepage": ds.homepage if ds else None,
        "sample": args.sample,
    }


def _resolve_jobs(jobs: Optional[int], task_count: int) -> int:
    """Parallel scoring workers: physical cores by default (one single-threaded
    task per core saturates the CPU without oversubscribing), capped at the task
    count and overridable via --jobs. A non-positive --jobs falls back to the
    physical-core default."""
    requested = jobs if (jobs and jobs > 0) else get_physical_cores()
    return max(1, min(requested, max(1, task_count)))


def _anchor_label(impl_name: str, tasks: BenchList) -> str:
    """The emitted operating-point label closest to this impl's perf preset.

    Chosen from the labels actually emitted for the impl, so it stays valid when
    ``--quality-steps`` / ``--quick`` subsample the sweep. Prefers an exact match
    on the preset's quality-axis value, else the numerically nearest swept value;
    falls back to the first label for single-point / null impls."""
    labels = list(dict.fromkeys(t.label for t in tasks))
    if not labels:
        return ""
    schema = schema_for(impl_name)
    axis = schema.quality_axis
    if axis is None or axis not in schema.perf_preset:
        return labels[0]
    target = schema.perf_preset[axis]
    exact = quality_label(axis, target)
    if exact in labels:
        return exact
    try:
        target_f = float(target)
    except ValueError:
        return labels[0]
    numeric: list[tuple[str, float]] = []
    for t in tasks:
        raw = t.params.get(axis)
        try:
            numeric.append((t.label, float(raw)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    if not numeric:
        return labels[0]
    return min(numeric, key=lambda lv: abs(lv[1] - target_f))[0]


def _select_timing_tasks(tasks: BenchList, perf: PerfMode) -> BenchList:
    """Subset of sweep tasks to time rigorously under hyperfine.

    ``all`` → every operating point; ``anchor`` → per implementation, only the
    point nearest its perf preset (all images at that point), reproducing the old
    performance suite's single-preset coverage. Null baselines (a single point)
    are always included."""
    if perf == "all":
        return list(tasks)
    by_impl: Dict[str, BenchList] = {}
    for t in tasks:
        by_impl.setdefault(t.impl.name, []).append(t)
    chosen: BenchList = []
    for name, impl_tasks in by_impl.items():
        anchor = _anchor_label(name, impl_tasks)
        chosen.extend(t for t in impl_tasks if t.label == anchor)
    return chosen


def _run_metric_pass(args: RunArgs, tasks: BenchList, result_dir: str) -> list[str]:
    """Quality + relative-timing pass over every scorable task (null skipped).

    Runs each task once (encoders: IQA vs source; decoders: PSNR vs the golden
    decoder) and records its single-pass wall-clock as a relative time (issue
    #29). Writes metrics.json, a manifest, and the per-pass summary.

    Returns the names of any runs that failed (empty if all succeeded). On failure
    the summary is skipped — the sweep has holes — and the caller aborts without
    finalizing the bundle."""
    print("=" * 70)
    print("QUALITY SWEEP (metric pass)")
    print("=" * 70)

    metric_tasks = [t for t in tasks if t.impl.format is not None]
    if not metric_tasks:
        print("\nError: No scorable benchmarks to run!")
        sys.exit(1)

    # Record which quality-axis values each impl actually swept (for the
    # reproducibility manifest and downstream rate-distortion plots).
    sweeps: Dict[str, list[str]] = {}
    for task in metric_tasks:
        sweeps.setdefault(task.impl.name, [])
        axis = schema_for(task.impl.name).quality_axis
        value = task.params.get(axis, "") if axis else ""
        if value and value not in sweeps[task.impl.name]:
            sweeps[task.impl.name].append(value)

    max_workers = _resolve_jobs(args.jobs, len(metric_tasks))

    manifest = {
        **_base_manifest(),
        "benchmark_config": {
            "suite": "quality",
            **_dataset_manifest(args),
            "formats": args.formats,
            "mode": args.mode,
            "quality_steps": args.quality_steps,
            "quality_sweeps": sweeps,
            "quick": args.quick,
            "jobs": max_workers,
        },
    }
    with open(f"{result_dir}/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n✓ Manifest written to {result_dir}/manifest.json")
    print(f"\n✓ {len(metric_tasks)} quality measurement(s) across the sweep\n")

    metrics = generate_metrics(metric_tasks, result_dir, max_workers, args.keep_temp)
    metrics_path = f"{result_dir}/metrics.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"\n✓ Metrics saved to {metrics_path}")

    failures = [m["name"] for m in metrics if m["error"] is not None]
    if failures:
        return failures

    generate_summary(result_dir, None, metrics)
    return []


def _run_hyperfine_chunk(
    chunk: list, base_flags: list[str], export_path: str, debug: bool
) -> list:
    """Time one chunk of benchmarks under hyperfine and return its ``results`` list.

    The combined argv is sized to stay under ARG_MAX by the caller, but the byte
    estimate is conservative rather than exact. If ``execve`` still rejects it with
    E2BIG, the estimate undershot: bisect the chunk, time each half independently,
    and merge — so the safeguard degrades gracefully instead of crashing. A chunk
    that's already a single benchmark cannot be split further, so re-raise with a
    clear message in that case."""
    hyperfine_cmd = ["hyperfine", *base_flags, "--export-json", export_path]
    for task in chunk:
        hyperfine_cmd.extend(["--command-name", task.name()])
    hyperfine_cmd.extend([task.cmd("/dev/null") for task in chunk])
    if debug:
        hyperfine_cmd.append("--show-output")

    try:
        subprocess.run(hyperfine_cmd, check=True)
    except FileNotFoundError:
        print("\nError: 'hyperfine' not found")
        sys.exit(1)
    except OSError as e:
        if e.errno != errno.E2BIG:
            raise
        if len(chunk) <= 1:
            print(
                "\nError: a single benchmark's command exceeds the OS argv limit "
                "(ARG_MAX); cannot split further"
            )
            sys.exit(1)
        # Estimate undershot ARG_MAX — bisect and time each half on its own.
        mid = len(chunk) // 2
        results: list = []
        for half_idx, half in enumerate((chunk[:mid], chunk[mid:])):
            half_path = f"{export_path}.h{half_idx}"
            results.extend(_run_hyperfine_chunk(half, base_flags, half_path, debug))
            os.remove(half_path)
        with open(export_path, "w") as f:
            json.dump({"results": results}, f)
        return results
    except subprocess.CalledProcessError:
        print("\nError: Benchmark execution failed")
        print(f"Test command: {' '.join(hyperfine_cmd)}")
        sys.exit(1)

    with open(export_path) as f:
        return json.load(f).get("results", [])


def _run_timing_overlay(args: RunArgs, tasks: BenchList, result_dir: str) -> None:
    """Rigorous (hyperfine) timing overlay over the selected subset of the sweep.

    Always compute-only (``--discard``): discarding output removes filesystem-write
    variance as a confound (issue #9). ``--perf anchor`` times each impl's preset
    point; ``--perf all`` times every operating point. Each selected task runs at
    both threading modes (--quick collapses to all-cores). Writes raw.json, a
    manifest, and the per-pass timing summary into `result_dir`."""
    thread_modes = [0] if args.quick else list(THREAD_MODES)
    selected = _select_timing_tasks(tasks, args.perf)

    # Clone each selected task per thread mode, baking in the real iteration/warmup
    # counts and the discard policy so name() and cmd() reflect the timed run.
    timing: BenchList = []
    for task in selected:
        for threads in thread_modes:
            timing.append(
                task.model_copy(
                    update={
                        "threads": threads,
                        "iterations": args.iterations,
                        "warmup": args.warmup,
                        "discard_output": True,
                        "measure_memory": args.measure_memory,
                        "pin_cores": args.pin_cores,
                    }
                )
            )

    print("=" * 70)
    print("PERFORMANCE OVERLAY (rigorous timing)")
    print("=" * 70)

    manifest = {
        **_base_manifest(),
        "benchmark_config": {
            "suite": "performance",
            **_dataset_manifest(args),
            "formats": args.formats,
            "mode": args.mode,
            "perf": args.perf,
            "thread_modes": thread_modes,
            "discard_output": True,
            "iterations": args.iterations,
            "warmup": args.warmup,
            "pin_cores": args.pin_cores,
            "quick": args.quick,
        },
    }
    with open(f"{result_dir}/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n✓ Manifest written to {result_dir}/manifest.json")

    if not timing:
        print("\nNo timing tasks selected; skipping rigorous timing.")
        return

    print(f"\n✓ {len(timing)} timing benchmark(s) ready to run\n")

    json_output = f"{result_dir}/raw.json"
    base_flags = (
        ["--warmup", "3", "--min-runs", "10"]
        if not args.quick
        else ["--warmup", "0", "--min-runs", "1", "--max-runs", "1"]
    )

    # hyperfine takes every benchmark as positional argv, so a large sweep over a
    # dataset with long file paths (e.g. clic2025's 64-hex-hash names) can blow
    # past the OS argv limit (ARG_MAX) in a single invocation (Errno 7, E2BIG).
    # Split the benchmarks into chunks whose combined argv stays well under the
    # limit, run hyperfine per chunk, and merge the per-chunk JSON. Each benchmark
    # is timed independently, and the summary recomputes relative speed from the
    # absolute per-benchmark stats, so chunking does not affect the results.
    ARGV_BUDGET = 128_000  # bytes; conservative vs ARG_MAX (>=256 KiB everywhere)
    chunks: list[list] = []
    current: list = []
    current_len = 0
    for task in timing:
        # Each task contributes three argv entries: --command-name, the name, and
        # the command string (+ a little slack for separators/quoting).
        cost = len(task.name()) + len(task.cmd("/dev/null")) + 24
        if current and current_len + cost > ARGV_BUDGET:
            chunks.append(current)
            current, current_len = [], 0
        current.append(task)
        current_len += cost
    if current:
        chunks.append(current)

    if len(chunks) > 1:
        print(f"Splitting into {len(chunks)} hyperfine runs to stay under ARG_MAX\n")

    merged_results: list = []
    for idx, chunk in enumerate(chunks):
        single = len(chunks) == 1
        # Single chunk writes straight to raw.json (hyperfine's native export);
        # multi-chunk runs go to part files that are merged and then removed.
        part_path = json_output if single else f"{result_dir}/raw.part{idx}.json"
        results = _run_hyperfine_chunk(chunk, base_flags, part_path, args.debug)
        if not single:
            merged_results.extend(results)
            os.remove(part_path)

    # Stitch the per-chunk exports back into the single raw.json the summary reads.
    if len(chunks) > 1:
        with open(json_output, "w") as f:
            json.dump({"results": merged_results}, f, indent=2)

    generate_summary(result_dir, json_output, None)

    if args.measure_memory:
        mem_commands = [
            task.cmd("/dev/null", iterations=1, warmup=0) for task in timing
        ]
        mem_names = [task.name() for task in timing]
        measure_memory(result_dir, mem_commands, mem_names)


def run_sweep(args: RunArgs) -> None:
    """Run the unified sweep into a fresh bundle.

    Always runs the metric pass (quality + a one-pass relative time for every
    operating point). When ``--perf`` is not ``off``, layers a rigorous hyperfine
    timing overlay on the selected subset (each impl's preset point for ``anchor``,
    every point for ``all``) across both thread modes. Both halves cover the *same*
    operating-point sweep, so quality and performance are reported together."""
    if not args.skip_build:
        build_projects(args.formats)
    else:
        print("Skipping build step (--skip-build)...")

    bundle = _new_result_dir()

    tasks: BenchList = []
    for fmt in args.formats:
        tasks += build_sweep(fmt, args.dataset, args)
    if not tasks:
        print("\nError: No benchmarks to run!")
        sys.exit(1)

    # 1. Metric pass (always): quality + relative timing for every scorable task.
    qual_dir = os.path.join(bundle, "quality")
    os.makedirs(qual_dir, exist_ok=True)
    failures = _run_metric_pass(args, tasks, qual_dir)
    if failures:
        _abort_on_quality_failures(failures, bundle)
    suites = ["quality"]

    # 2. Rigorous timing overlay (optional, secondary).
    if args.perf != "off":
        perf_dir = os.path.join(bundle, "performance")
        os.makedirs(perf_dir, exist_ok=True)
        _run_timing_overlay(args, tasks, perf_dir)
        suites.append("performance")

    _finalize_bundle(bundle, suites)
    _print_bundle(bundle, suites)


def _finalize_bundle(bundle_dir: str, suites: list[str]):
    """Write the bundle's top-level manifest, an index summary.md linking the
    per-suite reports, and a self-contained report.html embedding every chart."""
    manifest = {
        **_base_manifest(),
        "bundle": True,
        "suites": suites,
    }
    with open(os.path.join(bundle_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    lines = ["# Benchmark Bundle\n"]
    if "performance" in suites:
        lines.append(
            "- Performance (timing): [`performance/summary.md`](performance/summary.md)"
        )
    if "quality" in suites:
        lines.append(
            "- Quality (rate-distortion): [`quality/summary.md`](quality/summary.md)"
        )
    lines.append(
        "\nOpen [`report.html`](report.html) for a single self-contained view.\n"
    )
    with open(os.path.join(bundle_dir, "summary.md"), "w") as f:
        f.write("\n".join(lines))

    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report_path = generate_report_html(bundle_dir, generated_at=generated_at)
    print(f"\n✓ Bundle report written to {report_path}")


def _print_bundle(bundle_dir: str, suites: list[str]):
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"\nBundle saved to: {bundle_dir}/")
    if "performance" in suites:
        print("  - performance/   : raw.json, summary.md, timing charts, memory.csv")
    if "quality" in suites:
        print("  - quality/       : metrics.json, summary.md, rate-distortion charts")
    print("  - manifest.json  : bundle metadata")
    print("  - summary.md     : index")
    print("  - report.html    : self-contained report (all charts embedded)")
    print()


def _abort_on_quality_failures(failures: list[str], bundle: str):
    """Print which quality runs failed and exit non-zero. Called instead of
    finalizing the bundle when the rate-distortion sweep has holes."""
    print(f"\n{Fore.RED}{'=' * 70}")
    print(f"QUALITY SUITE FAILED — {len(failures)} run(s) errored")
    print(f"{'=' * 70}{Style.RESET_ALL}")
    print(
        "\nThe rate-distortion sweep is incomplete, so no summary or report was "
        "generated. Raw measurements (including error rows) were kept at:"
    )
    print(f"  {bundle}/quality/metrics.json\n")
    print("Failed run(s):")
    for name in failures:
        print(f"  {Fore.RED}✗{Style.RESET_ALL} {name}")
    print()
    sys.exit(1)


def run_compile(args: CompileArgs):
    """Compile the project."""
    print("🔨 Compiling project...")
    if not args.implementations:
        # Build all implementations
        formats = list(ImageFormat)
        build_projects(formats)
    else:
        for name in args.implementations:
            # Find implementation by name
            impl = find_implementation_by_name(name)
            if not impl:
                raise RuntimeError(f"\nError: Implementation '{name}' not found")
            build_project(impl)

    pass


def run_setup(args: SetupArgs) -> None:
    """Run dataset setup or verification."""
    from bench_lib.data_setup import ensure_all_datasets, ensure_dataset, verify_dataset

    if args.verify_only:
        if args.dataset is not None:
            ok = verify_dataset(args.dataset)
            if not ok:
                sys.exit(1)
        else:
            all_ok = all(verify_dataset(d) for d in DatasetId)
            if not all_ok:
                sys.exit(1)
    else:
        if args.dataset is not None:
            ensure_dataset(args.dataset, force=args.force)
        else:
            ensure_all_datasets(force=args.force)


def run_docs(args: DocsArgs) -> None:
    """Generate (or --check) docs/tunables.md from the tunable schemas.

    The overview is synthesized from `TUNABLE_SCHEMAS` (the single in-code source
    of truth), so `--check` (used in CI / tests) fails if the committed file has
    drifted from the schemas — keeping the high-level overview honest (issue #4)."""
    from bench_lib.tunables_doc import render_tunables_markdown

    path = os.path.join(PROJECT_ROOT, "docs", "tunables.md")
    content = render_tunables_markdown()
    if args.check:
        current = None
        if os.path.exists(path):
            with open(path) as f:
                current = f.read()
        if current != content:
            print(
                f"{Fore.RED}✗ {path} is out of date.{Style.RESET_ALL} "
                "Run './bench docs' to regenerate it."
            )
            sys.exit(1)
        print(f"{Fore.GREEN}✓ {path} is up to date.{Style.RESET_ALL}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    print(f"{Fore.GREEN}✓ Wrote {path}{Style.RESET_ALL}")


def run_clean(args: CleanArgs):
    """Clean build artifacts."""
    print("🧹 Cleaning project...\n")

    # cargo clean
    print("Run cargo clean...")
    subprocess.run(["cargo", "clean"], check=True)

    # Delete implementations/cpp/**/build
    print("Delete implementations/cpp/**/build...")
    build_dirs = glob.glob("implementations/cpp/*/build")
    for build_dir in build_dirs:
        print(f"  Deleting {build_dir}...")
        shutil.rmtree(build_dir, ignore_errors=True)

    # Delete vendor build artifacts
    for d in ["vendor/build", "vendor/install"]:
        if os.path.exists(d):
            print(f"  Deleting {d}...")
            shutil.rmtree(d, ignore_errors=True)

    # Delete result directory
    if os.path.exists("results"):
        # Ask for confirmation
        if not args.yes:
            confirm = input("Delete results directory? (y/N): ")
            if confirm.lower() != "y":
                return

        print("Delete results directory...")
        shutil.rmtree("results")
    else:
        print("Skipping: Results directory does not exist.")
