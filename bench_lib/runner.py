"""Dataset/input management, benchmark execution, and metrics collection."""

import csv
import datetime
import errno
import glob
import hashlib
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
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, Optional, Set

import humanize
from colorama import Fore, Style
from PIL import Image as PILImage

from bench_lib.build import build_project, build_projects
from bench_lib.imageprep import single_thread_env as _single_thread_env
from bench_lib.imageprep import to_canonical_ppm, to_viewable_png
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
    ReportArgs,
    RunArgs,
    SetupArgs,
    TunableSchema,
    find_implementation_by_name,
    quality_label,
    schema_for,
    select_sweep,
)
from bench_lib.effort import (
    build_effort_tasks,
    prepare_effort_images,
    write_effort_outputs,
)
from bench_lib.report import _git_info, generate_report_html
from bench_lib.scaling import (
    SCALING_HYPERFINE_FLAGS,
    SCALING_LADDER_MP,
    build_scaling_tasks,
    generate_ladder,
    select_representative_sources,
    select_scaling_sources,
    write_scaling_outputs,
)
from bench_lib.summary import generate_summary
from bench_lib.system_info import (
    _detect_mimalloc_version,
    get_compiler_versions,
    get_git_info,
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


def _generate_intermediates_parallel(
    fn: Callable[[str], tuple[str, str]], items: Sequence[str]
) -> list[tuple[str, str]]:
    """Map ``fn`` over ``items`` across a thread pool, preserving input order.

    Used to pre-generate reference inputs (the cached per-image PPM conversion +
    reference encode). Those are independent per image and **never timed**, so
    running them concurrently — each child subprocess pinned to a single thread
    (see ``_single_thread_env`` and the ``--threads 1`` the callers pass) —
    saturates the CPU without the jobs×all-cores oversubscription a parallel
    all-core encode would cause. Decode inputs are scored against the golden
    decoder of the *same* file, so the thread count cannot shift any reported
    metric. The reference binary must already be built (callers build it up
    front) so no worker triggers a cargo build that would race the target dir
    (cf. ``_ensure_reference_decoders``). Re-raises the first worker error,
    matching the serial path's fail-closed behaviour."""
    if not items:
        return []
    workers = _resolve_jobs(None, len(items))
    results: list[tuple[str, str]] = [("", "")] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {executor.submit(fn, item): i for i, item in enumerate(items)}
        for future in as_completed(future_to_idx):
            results[future_to_idx[future]] = future.result()
    return results


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

    target_ext = FORMAT_EXT_MAP[format]

    # Resolve + build the reference encoder once up front (only needed when the
    # dataset must be transcoded into a non-PPM target). Building it inside the
    # worker pool below would race cargo's target dir — no cross-thread lock —
    # so we mirror _ensure_reference_decoders and prepare it serially here.
    ref_impl: Optional[Implementation] = None
    if format != PPMImageFormat.PPM:
        ref_impl_name = REFERENCE_ENCODERS.get(format)
        if not ref_impl_name:
            raise RuntimeError(f"No reference encoder defined for {format}")
        ref_impl = find_implementation_by_name(ref_impl_name)
        if not ref_impl:
            raise RuntimeError(f"Reference encoder {ref_impl_name} not found")
        if not os.path.exists(ref_impl.bin):
            build_project(ref_impl)

    def _prepare(f: str) -> tuple[str, str]:
        """Resolve one source into its (input_path, source_path), generating the
        cached intermediate if missing. Independent per image → safe to run in
        the pool (only reads/writes this image's own paths)."""
        if f.lower().endswith(f".{target_ext}"):
            # Dataset file already in required format.
            return (f, f)
        # A single reference preset is used per format, so one encoded file per
        # source suffices (no quality suffix).
        base_path = os.path.splitext(f)[0]
        target_file = f"{base_path}.{target_ext}"
        if os.path.exists(target_file):
            return (target_file, f)

        print(f"Generating {target_file} from {f}...")
        # 1. Convert to 8-bit P6 PPM (forced 8-bit: not all impls handle 16-bit).
        intermediate_ppm = _source_to_ppm(f)
        # If the requested format is PPM itself, the converted P6 intermediate IS
        # the target. Running the reference "encoder" (null-cpp-encode) over it
        # would overwrite the valid P6 file with headerless raw RGB (it writes
        # img.data, not a PPM), so skip the encode step entirely.
        if format == PPMImageFormat.PPM:
            return (intermediate_ppm, f)

        # 2. Encode at the reference encoder's fixed preset (--param k=v).
        # Single-threaded (see _generate_intermediates_parallel): this output is
        # never timed and decode inputs are scored vs the golden decoder of the
        # same file, so the thread count cannot change any reported metric.
        assert ref_impl is not None  # guaranteed: format != PPM resolved one above
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
            "1",
        ]
        for key, value in sorted(schema_for(ref_impl.name).perf_params().items()):
            ref_cmd += ["--param", f"{key}={value}"]
        subprocess.run(
            ref_cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,  # Capture stderr to avoid spam, unless error
            env=_single_thread_env(),
        )
        return (target_file, f)

    input_files = _generate_intermediates_parallel(_prepare, dataset_files)

    # Verify all input files exist
    missing = [f_path for f_path, _ in input_files if not os.path.exists(f_path)]
    if missing:
        raise RuntimeError(
            f"Some input files not found {len(missing)} for '{dataset_name}': {','.join(missing[:5])}"
        )

    INPUT_FILES_CACHE[(dataset_name, format, limit)] = input_files
    return input_files


def _source_to_ppm(f: str) -> str:
    """Return an 8-bit P6 PPM path for source `f` (no resize), generating it via
    the shared canonicalizer if `f` is not already a PPM. The full-resolution
    counterpart of the scaling/effort downscale, routed through the same
    :func:`to_canonical_ppm` so every sweep prepares inputs identically."""
    if f.lower().endswith(".ppm"):
        return f
    ppm = f"{os.path.splitext(f)[0]}.ppm"
    to_canonical_ppm(f, ppm)
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
    # Build the reference encoder once up front (the worker pool below must not
    # trigger a cargo build that would race the target dir).
    if not os.path.exists(ref_impl.bin):
        build_project(ref_impl)

    ext = FORMAT_EXT_MAP[format]

    def _prepare(f: str) -> tuple[str, str]:
        """Reference-encode one source at this operating point, returning
        (encoded_path, source_ppm_path). Independent per image → pool-safe.
        Single-threaded (--threads 1 + 1-thread env): never timed, and decoders
        are scored vs the golden decode of the same file, so the thread count
        cannot move any reported metric (see _generate_intermediates_parallel)."""
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
                "1",
            ]
            for k, v in sorted(params.items()):
                cmd += ["--param", f"{k}={v}"]
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=_single_thread_env(),
            )
        return (target, ppm)

    inputs = _generate_intermediates_parallel(_prepare, dataset_files)

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

    Decoders take no params, so the *input* is what varies: for a few operating
    points of the format's reference encoder, reference-encode the sources at that
    point and decode them. Decode cost and fidelity (PSNR vs the golden decoder,
    scored later) thus trace input bitrate. Because decode cost/fidelity is ~flat
    across bitrate, the point count is decoupled from the encoder's --quality-steps
    and set by --decode-steps (default a few; --quick → one; 0 → the full encoder
    axis). Knob-less reference encoders contribute a single point.

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

    # Decoders don't sweep quality; their cost/fidelity is ~flat across input
    # bitrate, so only a few representative input encodings are needed — decoupled
    # from the encoder's --quality-steps. --quick collapses to one point;
    # --decode-steps 0 (or None) falls back to the encoder step count (full axis).
    dsteps = 1 if args.quick else (args.decode_steps or steps)

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
        points = _encoder_points(schema, dsteps)
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


# A per-run cache of golden-reference PPMs shared across decoder tasks:
# {"dir": staging subdir, "map": {key: published_path}, "lock": Lock}.
GoldenCache = Dict[str, object]


def _new_golden_cache(temp_dir: str) -> GoldenCache:
    """Create the golden-PPM cache rooted in a subdir of the metric staging dir
    (so it is freed with the staging dir, or kept under --keep-temp)."""
    golden_dir = os.path.join(temp_dir, "golden")
    os.makedirs(golden_dir, exist_ok=True)
    return {"dir": golden_dir, "map": {}, "lock": threading.Lock()}


def _golden_reference_ppm(
    task: BenchmarkTask,
    golden_cache: GoldenCache,
    env: Dict[str, str],
    identifier: str,
) -> str:
    """Return the golden-reference PPM for a decoder task's input, decoding it at
    most once across all decoders that share that input.

    Every decoder of a format scores against the *same* golden decode of the
    *same* bitstream, so the result is identical regardless of which decoder
    asked for it — memoizing it turns N redundant reference decodes per
    (format, input) into one. The first task to see an input publishes the PPM
    atomically (decode into a private ``.part`` outside the lock, then
    ``os.replace`` onto the canonical path); later tasks reuse it. A rare race
    (two tasks for the same input in flight at once) just decodes twice and
    republishes identical bytes — correct, only mildly wasteful. The returned
    path is owned by the cache dir and freed with the staging dir, so it is
    NEVER added to a task's ``temp_files`` (that would let one decoder delete the
    PPM another still needs)."""
    lock = golden_cache["lock"]
    cache_map = golden_cache["map"]
    ref_name = REFERENCE_DECODERS.get(task.impl.format)
    key = f"{ref_name}\0{os.path.abspath(task.input_path)}"
    with lock:  # type: ignore[union-attr]
        canonical = cache_map.get(key)  # type: ignore[union-attr]
        published = canonical is not None and os.path.exists(canonical)
        if canonical is None:
            digest = hashlib.sha1(key.encode()).hexdigest()[:16]
            canonical = os.path.join(str(golden_cache["dir"]), f"{digest}.ppm")
            cache_map[key] = canonical  # type: ignore[index]
    if published:
        return canonical
    part = f"{canonical}.{identifier}.part"
    _decode_to_ppm(task, task.input_path, part, env=env)
    os.replace(part, canonical)
    return canonical


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


def _decode_input_is_lossless(task: BenchmarkTask) -> bool:
    """Whether a decode task's input was produced by a *lossless* reference encoder
    — i.e. the source survives the encode intact, so the decoded pixels have the
    source as a true ground truth (and bit-exactness can be checked against it).

    The producing reference is the format's lossless reference when the task is on
    the dedicated lossless decode path (issue #21, ``input_lossless``), else the
    format's main reference encoder — which is itself lossless for a lossless-only
    format like PNG. Decoupled from the row's ``lossless`` flag so PNG (lossless,
    yet not split into a separate decode path) is still recognised."""
    if task.impl.type != BenchmarkType.DECODE or task.impl.format is None:
        return False
    ref_map = LOSSLESS_REFERENCE_ENCODERS if task.input_lossless else REFERENCE_ENCODERS
    ref_name = ref_map.get(task.impl.format)
    return bool(ref_name and schema_for(ref_name).lossless)


def _read_ppm_raster(path: str) -> Optional[tuple[int, int, bytes]]:
    """Read an image as ``(width, height, raw RGB bytes)`` for an exact pixel
    compare. Goes through PIL so PPM header quirks (comments, whitespace) are
    handled identically on both sides of the comparison; returns None if the file
    can't be read. P6 PPM carries no colour metadata, so the bytes are the stored
    samples verbatim."""
    try:
        with PILImage.open(path) as im:
            rgb = im.convert("RGB")
            return (rgb.width, rgb.height, rgb.tobytes())
    except Exception:
        return None


def _raster_bit_exact(
    reference_path: str, distorted_path: str
) -> tuple[Optional[bool], Optional[int], Optional[int]]:
    """Definitive byte-level bit-exactness of ``distorted`` vs ``reference`` (both
    8-bit RGB rasters), independent of iqa-cli/PSNR.

    Returns ``(bit_exact, first_diff_byte, diff_byte_count)``. ``bit_exact`` is
    None (with None diagnostics) when either raster can't be read; differing
    dimensions count as not-exact. The exact-match path is a single C-level
    ``bytes`` compare (cheap); the per-byte diagnostic only runs on a mismatch."""
    ref = _read_ppm_raster(reference_path)
    dist = _read_ppm_raster(distorted_path)
    if ref is None or dist is None:
        return (None, None, None)
    (rw, rh, rb), (dw, dh, db) = ref, dist
    if (rw, rh) != (dw, dh) or len(rb) != len(db):
        return (False, 0, max(len(rb), len(db)))
    if rb == db:  # fast path: most rows are bit-exact
        return (True, None, 0)
    # Mismatch is rare → vectorise the diagnostic instead of a Python byte loop.
    import numpy as np

    a = np.frombuffer(rb, dtype=np.uint8)
    b = np.frombuffer(db, dtype=np.uint8)
    diff = a != b
    return (False, int(np.argmax(diff)), int(diff.sum()))


def _publish_file_once(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst`` once, atomically. Shared across the parallel pool
    — every decoder of an input publishes the same ``_inputs/`` artifact — so it
    is a no-op when ``dst`` exists and copies via a temp + ``os.replace``,
    leaving no half-written file for a concurrent reader."""
    if os.path.exists(dst):
        return
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst) or ".", suffix=".tmp")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _measure_one(
    task: BenchmarkTask,
    temp_dir: str,
    env: Dict[str, str],
    golden_cache: GoldenCache,
    keep_temp: bool = False,
    bundle_dir: Optional[str] = None,
    capture: bool = False,
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
    # Gallery assets for this data point (None when capture is off / no bundle /
    # null task): the exact encoded artifact and the browser-viewable source.
    asset_rel = task.asset_relpath() if (capture and bundle_dir) else None
    source_rel = task.source_asset_relpath() if (capture and bundle_dir) else None
    try:
        identifier = task.identifier()
        format_ext_str = FORMAT_EXT_MAP[task.output_ext()]
        # An *encoder's* artifact is its output, so encode straight into the
        # bundle's assets/ tree and keep it — the path is unique per
        # (impl, label, image), so no worker races another for it. A *decoder's*
        # artifact is the bitstream it consumed (its raw-PPM output is
        # format-invariant), so the output stays a deleted-as-scored temp file
        # and the input is published after scoring instead.
        if asset_rel and task.impl.type == BenchmarkType.ENCODE:
            output_path = os.path.join(bundle_dir, asset_rel)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
        else:
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
        # the iqa crate, which consumes raw pixels). The reference each row is
        # scored against:
        #   - Encoders: decode the encoded output with the format's reference
        #     decoder and score against the original source ("source" basis).
        #   - Lossless-input decoders (PNG always; WebP/JXL lossless path): the
        #     decoded pixels MUST equal the source, so score against the *source*
        #     ground truth ("source" basis) — the same path encoders use. This
        #     measures true correctness, not agreement with another decoder.
        #   - Lossy-input decoders (JPEG, AVIF, WebP/JXL lossy path): no source
        #     ground truth exists, so score against the *golden* (reference)
        #     decoder's PPM of the same input ("golden" basis), isolating decoder
        #     fidelity from the encoder loss both share.
        decode_time_s: Optional[float] = None
        if task.impl.type == BenchmarkType.ENCODE:
            distorted_ppm = os.path.join(temp_dir, f"{identifier}_decoded.ppm")
            temp_files.append(distorted_ppm)
            # Time this single reference-decode: a real, drift-free decode cost for
            # this exact encoded output (relative/single-pass, like time_s).
            decode_start = time.time()
            _decode_to_ppm(task, output_path, distorted_ppm, env=env)
            decode_time_s = time.time() - decode_start
            reference_ppm = task.source_path
            metric_basis = "source"
        else:
            distorted_ppm = output_path
            if _decode_input_is_lossless(task):
                # Lossless round-trip: the source is the ground truth.
                reference_ppm = task.source_path
                metric_basis = "source"
            else:
                # Lossy: the golden PPM is shared across every decoder of this input
                # (memoized + atomically published), so it is owned by the golden
                # cache dir, not this task — do NOT add it to temp_files.
                reference_ppm = _golden_reference_ppm(
                    task, golden_cache, env, identifier
                )
                metric_basis = "golden"
        score, psnr, ssim, butteraugli = _run_iqa(reference_ppm, distorted_ppm, env=env)

        # Definitive bit-exactness (independent of PSNR): a byte compare of the
        # produced raster against the reference it was scored against. Meaningful
        # for any decode row (does it reproduce its reference exactly?) and for a
        # lossless encoder's round-trip; not for a lossy encode (intentionally not
        # identical), where it stays None.
        bit_exact: Optional[bool] = None
        bit_exact_first_diff: Optional[int] = None
        bit_exact_diff_count: Optional[int] = None
        if task.impl.type == BenchmarkType.DECODE or lossless:
            bit_exact, bit_exact_first_diff, bit_exact_diff_count = _raster_bit_exact(
                reference_ppm, distorted_ppm
            )

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
        # Surface a genuinely non-bit-exact lossless path right in the sweep log so
        # it's caught while running (drives the per-codec deep-fix), against the
        # basis it was scored on (source for lossless, golden for lossy decode).
        be_str = ""
        if bit_exact is False:
            be_str = (
                f" {Fore.YELLOW}[NOT bit-exact vs {metric_basis}: "
                f"{bit_exact_diff_count} bytes differ]{Style.RESET_ALL}"
            )
        status = (
            f"{Fore.GREEN}✓{Style.RESET_ALL} {task.name()} — "
            f"Size: {humanize.naturalsize(filesize, binary=True)}, "
            f"SSIMULACRA2: {score:.2f}, PSNR: {psnr_str}, SSIM: {ssim_str}, "
            f"Butteraugli: {ba_str}, bpp: {bpp:.3f} "
            f"(took {elapsed_time:.1f} s){be_str}{dim_warning}"
        )

        # Publish this row's gallery assets (best-effort: a copy/convert failure
        # must never fail a measurement). Encode artifacts were already written
        # in place above; a decode artifact is the shared input bitstream.
        asset_path_out: Optional[str] = None
        source_asset_out: Optional[str] = None
        if asset_rel and bundle_dir:
            try:
                if task.impl.type == BenchmarkType.DECODE:
                    _publish_file_once(
                        task.input_path, os.path.join(bundle_dir, asset_rel)
                    )
                asset_path_out = asset_rel
                if source_rel and to_viewable_png(
                    task.source_path, os.path.join(bundle_dir, source_rel)
                ):
                    source_asset_out = source_rel
            except Exception:
                pass  # leave the row without gallery links rather than failing it

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
            decode_time_s=decode_time_s,
            bit_exact=bit_exact,
            bit_exact_first_diff=bit_exact_first_diff,
            bit_exact_diff_count=bit_exact_diff_count,
            asset_path=asset_path_out,
            source_asset=source_asset_out,
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
            decode_time_s=None,
            bit_exact=None,
            bit_exact_first_diff=None,
            bit_exact_diff_count=None,
            asset_path=None,
            source_asset=None,
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
    bundle_dir: Optional[str] = None,
    capture: bool = False,
) -> list[BenchmarkMetrics]:
    """Generate file size and visual quality metrics, encoding tasks in parallel.

    Up to `max_workers` encode→decode→score pipelines run concurrently; each
    child is pinned to one thread, so a pool sized to the physical core count
    saturates the CPU without oversubscribing (issue #23). Tasks are dispatched
    in binary-sorted order so an encoder's runs cluster in time, keeping its
    binary + shared libs hot in cache.

    By default each task's temp files are deleted as soon as it's scored, so peak
    disk use stays bounded on large sweeps; `keep_temp` keeps every intermediate
    (and the staging dir) for inspection instead.

    When `capture` is set (and `bundle_dir` given), each result's exact encoded
    artifact and its source are persisted under `<bundle_dir>/assets` for the
    report gallery (see `BenchmarkTask.asset_relpath`); these are deliverables,
    not temp files, so they survive the per-task cleanup."""
    print(f"{Fore.BLUE}{'=' * 70}\nCOLLECTING METRICS\n{'=' * 70}\n")
    if capture and bundle_dir:
        print(f"Persisting per-result images under: {bundle_dir}/assets\n")

    temp_dir = tempfile.mkdtemp()
    if keep_temp:
        print(f"Temporary outputs stored in: {temp_dir}")
    else:
        print(f"Staging temp outputs in: {temp_dir} (freed per task as scored)")
    print(f"Encoding with {max_workers} parallel worker(s), 1 thread each\n")

    # Pin every child process to a single thread: covers rayon-/OMP-based codecs
    # and iqa-cli. The encode/decode also pass --threads 1 for codecs (e.g.
    # libavif) whose internal pool keys off the flag rather than these env vars.
    env = _single_thread_env()

    # Shared golden-PPM cache: every decoder of a given input scores against the
    # same golden decode of it, so memoize that decode across decoders instead of
    # repeating it per decoder (see _golden_reference_ppm). Lives under temp_dir,
    # freed with it (or kept under --keep-temp).
    golden_cache = _new_golden_cache(temp_dir)

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
                executor.submit(
                    _measure_one,
                    task,
                    temp_dir,
                    env,
                    golden_cache,
                    keep_temp,
                    bundle_dir,
                    capture,
                )
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
        "git": get_git_info(),
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


def _lossless_endpoint_labels(impl_name: str, impl_tasks: BenchList) -> set[str]:
    """The min- and max-effort labels actually emitted for a swept lossless encoder.

    The lossless size-vs-effort report curve is anchored at these two extremes
    (issue #26): timing them rigorously lets the report trust the curve's endpoints
    even when the interior points are single-pass. Ordered by the schema's declared
    low→high sweep and restricted to the labels still present after any
    ``--quality-steps`` subsampling. Empty for lossy or single-knob encoders (no
    effort axis)."""
    schema = schema_for(impl_name)
    if not (schema.lossless and schema.quality_axis):
        return set()
    emitted = {t.label for t in impl_tasks}
    ordered = [
        lbl
        for v in schema.quality_sweep
        if (lbl := quality_label(schema.quality_axis, v)) in emitted
    ]
    return {ordered[0], ordered[-1]} if ordered else set()


def _select_timing_tasks(tasks: BenchList, perf: PerfMode) -> BenchList:
    """Subset of sweep tasks to time rigorously under hyperfine.

    ``all`` → every operating point. ``anchor`` → per implementation, the point
    nearest its perf preset (all images at that point), reproducing the old
    performance suite's single-preset coverage, PLUS each lossless encoder's min/max
    effort endpoints so the report's size-vs-effort curve has statistically
    significant extremes (issue #26). Null baselines (a single point) are always
    included."""
    if perf == "all":
        return list(tasks)
    by_impl: Dict[str, BenchList] = {}
    for t in tasks:
        by_impl.setdefault(t.impl.name, []).append(t)
    chosen: BenchList = []
    for name, impl_tasks in by_impl.items():
        labels = {_anchor_label(name, impl_tasks)}
        labels |= _lossless_endpoint_labels(name, impl_tasks)
        chosen.extend(t for t in impl_tasks if t.label in labels)
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

    # Persist each result's image under <bundle>/assets (report.html lives at the
    # bundle root, so the rows' relative `assets/...` paths resolve from it).
    metrics = generate_metrics(
        metric_tasks,
        result_dir,
        max_workers,
        args.keep_temp,
        bundle_dir=os.path.dirname(result_dir),
        capture=args.report_images,
    )
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


def _run_hyperfine_tasks(
    timing: BenchList, result_dir: str, base_flags: list[str], debug: bool
) -> str:
    """Time fully-configured tasks under hyperfine; return the merged ``raw.json``
    path written in ``result_dir``.

    Each task must already carry its real threads/iterations/warmup + discard
    policy (so ``cmd()``/``name()`` reflect the timed run). hyperfine takes every
    benchmark as positional argv, so a large sweep over a dataset with long file
    paths (e.g. clic2025's 64-hex-hash names) can blow past the OS argv limit
    (ARG_MAX) in a single invocation (Errno 7, E2BIG). Split the benchmarks into
    chunks whose combined argv stays well under the limit, run hyperfine per
    chunk, and merge the per-chunk JSON. Each benchmark is timed independently and
    the summary recomputes relative speed from the absolute per-benchmark stats,
    so chunking does not affect the results. Shared by the performance overlay and
    the scaling suite."""
    json_output = f"{result_dir}/raw.json"
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
        results = _run_hyperfine_chunk(chunk, base_flags, part_path, debug)
        if not single:
            merged_results.extend(results)
            os.remove(part_path)

    # Stitch the per-chunk exports back into the single raw.json the summary reads.
    if len(chunks) > 1:
        with open(json_output, "w") as f:
            json.dump({"results": merged_results}, f, indent=2)
    return json_output


def _run_timing_overlay(args: RunArgs, tasks: BenchList, result_dir: str) -> None:
    """Rigorous (hyperfine) timing overlay over the selected subset of the sweep.

    Always compute-only (``--discard``): discarding output removes filesystem-write
    variance as a confound (issue #9). ``--perf anchor`` times each impl's preset
    point plus each lossless encoder's min/max effort endpoints (issue #26);
    ``--perf all`` times every operating point. Each selected task runs at both
    threading modes (--quick collapses to all-cores). Writes raw.json, a manifest,
    and the per-pass timing summary into `result_dir`."""
    thread_modes = [0] if args.quick else list(THREAD_MODES)
    selected = _select_timing_tasks(tasks, args.perf)

    # Bound the overlay to a representative image subset (keeps a full clic2025 run
    # tractable). Timing is content-light at fixed resolution and is a secondary
    # axis, so a few images stay statistically significant while cutting cost; the
    # quality sweep still covers every image. ``--perf-images 0`` → all images.
    if args.perf_images and args.perf_images > 0:
        distinct = list(dict.fromkeys(t.source_path for t in selected))
        keep = set(select_representative_sources(distinct, args.perf_images))
        if keep:
            selected = [t for t in selected if t.source_path in keep]
    perf_image_count = len({t.source_path for t in selected})

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
            "perf_images": args.perf_images,
            "perf_image_count": perf_image_count,
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

    base_flags = (
        ["--warmup", "3", "--min-runs", "10"]
        if not args.quick
        else ["--warmup", "0", "--min-runs", "1", "--max-runs", "1"]
    )
    json_output = _run_hyperfine_tasks(timing, result_dir, base_flags, args.debug)

    generate_summary(result_dir, json_output, None)

    if args.measure_memory:
        mem_commands = [
            task.cmd("/dev/null", iterations=1, warmup=0) for task in timing
        ]
        mem_names = [task.name() for task in timing]
        measure_memory(result_dir, mem_commands, mem_names)


def _merge_rigorous_timing(perf_dir: str, qual_dir: str) -> None:
    """Fold the overlay's isolated single-threaded timings back into the quality
    metrics (issue #26 anchoring).

    The overlay re-times a selected subset under hyperfine; each timed task's
    threads=1 command name equals the metric pass's row ``name`` (both are
    ``task.name()`` at t1 — the all-cores clone carries a different thread tag), so
    we join on ``name``. Matched rows gain ``time_rigorous_s`` /
    ``time_rigorous_stddev_s`` / ``time_runs`` (isolated, repeated-trial); the rest
    keep only single-pass ``time_s``. No-op when either file is missing."""
    raw_path = os.path.join(perf_dir, "raw.json")
    metrics_path = os.path.join(qual_dir, "metrics.json")
    if not (os.path.exists(raw_path) and os.path.exists(metrics_path)):
        return
    with open(raw_path) as f:
        results = json.load(f).get("results", [])
    by_name = {r["command"]: r for r in results if r.get("command") and r.get("times")}
    with open(metrics_path) as f:
        metrics = json.load(f)
    merged = 0
    for m in metrics:
        r = by_name.get(m.get("name"))
        if r is None:
            continue
        m["time_rigorous_s"] = r.get("mean")
        m["time_rigorous_stddev_s"] = r.get("stddev")
        m["time_runs"] = len(r.get("times") or [])
        merged += 1
    if merged:
        with open(metrics_path, "w") as f:
            json.dump(metrics, f, indent=2)
        print(f"\n✓ Anchored {merged} metric row(s) with rigorous timing")


def _run_scaling_suite(args: RunArgs, bundle_dir: str) -> bool:
    """Time encode/decode vs pixel count on a downscaled resolution ladder and
    write the ``scaling/`` suite (per-(format, op) log-log charts + a summary with
    a fitted exponent per codec). Returns True iff it produced output.

    Single-threaded by design (see ``scaling``): the question is how cost grows
    with pixels, isolated from parallel-scaling efficiency. Reuses the shared
    hyperfine driver with lighter run counts (a slope only needs a rough mean)."""
    print("=" * 70)
    print("SCALING SUITE (time vs pixel count)")
    print("=" * 70)

    sources = select_scaling_sources(
        get_dataset_files(args.dataset), args.scaling_images
    )
    if not sources:
        print("\nNo source images available for the scaling suite; skipping.")
        return False
    ladder = args.scaling_ladder or SCALING_LADDER_MP
    rungs = generate_ladder(args.dataset, sources, ladder)
    if not rungs:
        print("\nNo downscaled rungs generated (sources too small?); skipping.")
        return False
    tasks, pixels_by_basename = build_scaling_tasks(args.formats, rungs)
    if not tasks:
        print("\nNo scaling tasks to run (binaries missing?); skipping.")
        return False

    scal_dir = os.path.join(bundle_dir, "scaling")
    os.makedirs(scal_dir, exist_ok=True)
    manifest = {
        **_base_manifest(),
        "benchmark_config": {
            "suite": "scaling",
            **_dataset_manifest(args),
            "formats": args.formats,
            "scaling_images": len(sources),
            "ladder_mp": ladder,
            "rungs": len(rungs),
            "threads": 1,
        },
    }
    with open(f"{scal_dir}/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n✓ {len(tasks)} scaling benchmark(s) across {len(rungs)} ladder rung(s)\n")

    # Quick/demo: a single timed run per rung (skip statistical significance) to
    # match the rest of the quick path — the log-log slope is only indicative here.
    scaling_flags = (
        ["--warmup", "0", "--min-runs", "1", "--max-runs", "1"]
        if args.quick
        else list(SCALING_HYPERFINE_FLAGS)
    )
    raw = _run_hyperfine_tasks(tasks, scal_dir, scaling_flags, args.debug)
    write_scaling_outputs(scal_dir, raw, pixels_by_basename)
    return True


def _run_effort_suite(args: RunArgs, bundle_dir: str) -> bool:
    """Sweep each lossy codec's pinned effort/speed knob at fixed quality and write
    the ``effort/`` suite (time / bpp / SSIMULACRA2 vs effort charts + a summary).
    Reuses ``generate_metrics`` for the encode→decode→score pipeline, on a ~1 MP
    downscale of a few sources so the high-effort end stays affordable. Returns
    True iff it produced output."""
    print("=" * 70)
    print("EFFORT / SPEED SUITE (time vs quality vs size)")
    print("=" * 70)

    sources = select_scaling_sources(
        get_dataset_files(args.dataset), args.effort_images
    )
    image_ppms = prepare_effort_images(args.dataset, sources)
    if not image_ppms:
        print("\nNo images available for the effort suite; skipping.")
        return False
    tasks = build_effort_tasks(args.formats, image_ppms)
    if not tasks:
        print("\nNo effort-swept codecs built for these formats; skipping.")
        return False

    eff_dir = os.path.join(bundle_dir, "effort")
    os.makedirs(eff_dir, exist_ok=True)
    manifest = {
        **_base_manifest(),
        "benchmark_config": {
            "suite": "effort",
            **_dataset_manifest(args),
            "formats": args.formats,
            "effort_images": len(image_ppms),
            "effort_mp": 1.0,
        },
    }
    with open(f"{eff_dir}/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n✓ {len(tasks)} effort benchmark(s)\n")

    max_workers = _resolve_jobs(args.jobs, len(tasks))
    metrics = generate_metrics(tasks, eff_dir, max_workers, args.keep_temp)
    with open(f"{eff_dir}/metrics.json", "w") as f:
        json.dump(metrics, f, indent=2)
    write_effort_outputs(eff_dir, metrics)
    return True


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
        _merge_rigorous_timing(perf_dir, qual_dir)
        suites.append("performance")

    # 3. Scaling suite (optional): encode/decode time vs pixel count on a
    # downscaled ladder, characterizing each codec's scaling exponent.
    if args.scaling and _run_scaling_suite(args, bundle):
        suites.append("scaling")

    # 4. Effort/speed suite (optional): the time/size/quality tradeoff across each
    # lossy codec's pinned effort knob (AVIF speed, JXL effort, WebP method).
    if args.effort and _run_effort_suite(args, bundle):
        suites.append("effort")

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
    if "scaling" in suites:
        lines.append(
            "- Scaling (time vs pixels): [`scaling/summary.md`](scaling/summary.md)"
        )
    if "effort" in suites:
        lines.append(
            "- Effort/speed (time vs quality vs size): "
            "[`effort/summary.md`](effort/summary.md)"
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
    if "scaling" in suites:
        print("  - scaling/       : raw.json, summary.md, time-vs-pixels charts")
    if "effort" in suites:
        print("  - effort/        : metrics.json, summary.md, effort-tradeoff charts")
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


def _report_reuse_banner(bundle_dir: str) -> None:
    """Print a loud warning that ``bench report`` only re-skins existing raw data,
    and surface the bundle's recorded commit against the current HEAD so any
    codebase drift is visible before the (reused) graphs are trusted."""
    print(f"\n{Fore.YELLOW}{'=' * 70}")
    print("REGENERATING HTML ONLY — NO BENCHMARK IS RE-RUN")
    print(f"{'=' * 70}{Style.RESET_ALL}")
    print(
        "This rebuilds report.html from the raw metrics already in the bundle. "
        "Those measurements were produced by whatever codebase made the bundle; "
        "the regenerated graphs ASSUME that data still matches the current report "
        "code and codec behaviour. If anything relevant changed, the charts can be "
        f"{Fore.YELLOW}silently wrong{Style.RESET_ALL} — re-run a full sweep if in doubt."
    )
    bundle_git = _git_info(bundle_dir) or {}
    bundle_commit = str(bundle_git.get("commit") or "")
    current = get_git_info() or {}
    current_commit = str(current.get("commit") or "")
    if bundle_commit:
        same = bundle_commit == current_commit
        marker = (
            f"{Fore.GREEN}(matches current HEAD){Style.RESET_ALL}"
            if same
            else f"{Fore.RED}(differs from current HEAD!){Style.RESET_ALL}"
        )
        print(
            f"\n  bundle commit : {bundle_commit[:12]}"
            f"{' · dirty' if bundle_git.get('dirty') else ''} {marker}"
        )
        print(
            f"  current HEAD  : {current_commit[:12] or 'unknown'}"
            f"{' · dirty' if current.get('dirty') else ''}"
        )
    else:
        print(
            f"\n  {Fore.YELLOW}This bundle records no git commit — its provenance "
            f"is unknown.{Style.RESET_ALL}"
        )
    print()


def run_report(args: ReportArgs) -> None:
    """Rebuild ``report.html`` for an existing bundle from its raw metrics, without
    re-running anything (issue: cheap report iteration on expensive results).

    Reuses the single HTML entrypoint ``generate_report_html`` (the same call
    ``_finalize_bundle`` makes), so there is no duplicate report logic — this only
    adds the safety rail around it."""
    bundle = os.path.abspath(args.directory)
    if not os.path.isdir(bundle):
        print(f"{Fore.RED}Error: not a directory: {bundle}{Style.RESET_ALL}")
        sys.exit(1)
    # A real bundle always has the quality suite (the metric pass always runs) or
    # at least a top-level manifest; reject anything else early.
    looks_like_bundle = os.path.exists(
        os.path.join(bundle, "quality", "metrics.json")
    ) or os.path.exists(os.path.join(bundle, "manifest.json"))
    if not looks_like_bundle:
        print(
            f"{Fore.RED}Error: {bundle} does not look like a results bundle "
            f"(no quality/metrics.json or manifest.json).{Style.RESET_ALL}"
        )
        sys.exit(1)

    _report_reuse_banner(bundle)
    if not args.assume_results_current:
        try:
            answer = (
                input("Regenerate report.html from this reused data? [y/N] ")
                .strip()
                .lower()
            )
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted (pass --assume-results-current to skip this prompt).")
            sys.exit(1)

    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report_path = generate_report_html(bundle, generated_at=generated_at)
    print(f"\n✓ Report regenerated: {report_path}")


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
