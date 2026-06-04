"""Dataset/input management, benchmark execution, and metrics collection."""

import csv
import datetime
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
from typing import Dict, Optional, Set

import humanize
from colorama import Fore, Style
from PIL import Image as PILImage

from bench_lib.build import build_project, build_projects
from bench_lib.models import (
    AllArgs,
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
    ImageFormat,
    ImageFormats,
    PerfArgs,
    PPMImageFormat,
    QualityArgs,
    SetupArgs,
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
    get_system_info,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# iqa-cli: in-repo Rust binary computing IQA metrics (SSIMULACRA2 + PSNR) via the
# iqa-rs crate. Built as part of the Rust workspace (cargo build --release).
IQA_CLI_BIN = os.path.join(PROJECT_ROOT, "target", "release", "iqa-cli")

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


def build_bench_list(
    type: BenchmarkType,
    format: ImageFormat,
    dataset: DatasetId,
    threads: int,
    args: PerfArgs,
) -> BenchList:
    """Construct performance-suite tasks for one (type, format, threads) cell.
    Each implementation runs at its fixed performance preset
    (``schema.perf_params()``), labelled ``perf``."""
    from itertools import chain

    # Construct list of implementations to run
    # For each format, we get the null implementation + implementations that support that format, together filtered by type
    implementations = chain(
        (impl for impl in NULL_IMPLEMENTATIONS if impl.type == type),
        (
            impl
            for impl in IMPLEMENTATIONS
            if impl.format == format and impl.type == type
        ),
    )

    # Construct bench list
    benches: BenchList = []
    for impl in implementations:
        # Verify binary exists
        if not os.path.exists(impl.bin):
            raise RuntimeError(f"Error: Binary not found: {impl.bin}")

        # Determine correct input format
        match impl.type:
            case BenchmarkType.DECODE:
                input_format = format
            case BenchmarkType.ENCODE:
                input_format = PPMImageFormat.PPM
            case _:
                raise ValueError(f"Unknown implementation type: {impl.type}")

        # Fixed performance preset for this implementation (empty for decoders /
        # null / knob-less encoders).
        params = schema_for(impl.name).perf_params()

        for input_file, source_file in get_input_files(
            dataset, input_format, args.sample
        ):
            input_path = input_file

            bench = BenchmarkTask(
                impl=impl,
                params=params,
                label="perf",
                input_path=input_path,
                source_path=source_file,
                iterations=args.iterations,
                warmup=args.warmup,
                threads=threads,
                # Timing runs are always compute-only (issue #9): discarding the
                # output removes filesystem-write variance as a confound. The
                # metric-collection path overrides this to write a real file.
                discard_output=True,
                measure_memory=args.measure_memory,
                pin_cores=args.pin_cores,
            )
            benches.append(bench)

    return benches


def build_quality_bench_list(
    format: ImageFormat,
    dataset: DatasetId,
    args: QualityArgs,
) -> BenchList:
    """Construct quality-suite tasks for one format: for each lossy encoder with
    a quality axis, one task per swept axis value. No null/decoder tasks, no
    thread sweep (output is thread-invariant), and outputs are written (not
    discarded) so size + IQA can be measured."""
    benches: BenchList = []
    encoders = [
        impl
        for impl in IMPLEMENTATIONS
        if impl.format == format and impl.type == BenchmarkType.ENCODE
    ]
    input_files = get_input_files(dataset, PPMImageFormat.PPM, args.sample)

    for impl in encoders:
        if not os.path.exists(impl.bin):
            raise RuntimeError(f"Error: Binary not found: {impl.bin}")

        schema = schema_for(impl.name)
        if schema.quality_axis is None:
            # Lossless / knob-less encoder: no rate-distortion curve to trace.
            continue

        steps = 2 if args.quick else args.quality_steps
        for value in select_sweep(schema.quality_sweep, steps):
            params = schema.quality_params(value)
            label = quality_label(schema.quality_axis, value)
            for input_file, source_file in input_files:
                benches.append(
                    BenchmarkTask(
                        impl=impl,
                        params=params,
                        label=label,
                        input_path=input_file,
                        source_path=source_file,
                        # Quality runs are not timed; one pass writes the output.
                        iterations=1,
                        warmup=0,
                        threads=0,
                        discard_output=False,
                        measure_memory=False,
                        pin_cores=False,
                    )
                )

    return benches


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


def _decode_to_ppm(task: BenchmarkTask, encoded_path: str, ppm_path: str) -> None:
    """Decode an encoded output back to PPM using the format's reference decoder,
    so iqa-cli can compare raw pixels (iqa-rs does not decode codec formats)."""
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
            "0",
        ],
        check=True,
        stderr=subprocess.PIPE,
    )


def _run_iqa(reference_path: str, distorted_path: str) -> tuple[float, Optional[float]]:
    """Run iqa-cli on two images, returning (ssimulacra2, psnr). SSIMULACRA2 is
    -1.0 if missing; PSNR is None when non-finite/unavailable."""
    res = subprocess.run(
        [
            IQA_CLI_BIN,
            "--reference",
            reference_path,
            "--distorted",
            distorted_path,
            "--metric",
            "ssimulacra2,psnr",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(res.stdout.strip())
    ss = data.get("ssimulacra2")
    psnr = data.get("psnr")
    return (
        float(ss) if ss is not None else -1.0,
        float(psnr) if psnr is not None else None,
    )


def generate_metrics(benches: BenchList, result_dir: str) -> list[BenchmarkMetrics]:
    """Generate file size and visual quality metrics."""
    print(f"{Fore.BLUE}{'=' * 70}\nCOLLECTING METRICS\n{'=' * 70}\n")

    temp_dir = tempfile.mkdtemp()
    print(f"Temporary outputs stored in: {temp_dir}")

    metrics: list[BenchmarkMetrics] = []

    try:
        for i, task in enumerate(benches):
            print(
                f"[{i + 1}/{len(benches)}] Processing ({task.name()} >>>> ",
                end=" ",
                flush=True,
            )

            identifier = task.identifier()
            format_ext = task.output_ext()
            if task.impl.format is None or format_ext is None:
                print(
                    f"{Fore.BLUE}Skipping collecting metrics for {task.name()} due to null format...{Style.RESET_ALL}"
                )
                continue

            format_ext_str = FORMAT_EXT_MAP[format_ext]
            output_path = os.path.join(temp_dir, f"{identifier}.{format_ext_str}")

            # Obtain metric
            try:
                # Run implementation once (no warmup) to get the output file.
                # Force discard=False so the binary writes a real file even
                # though timing runs are always compute-only (--discard).
                start_time = time.time()
                subprocess.run(
                    shlex.split(
                        task.cmd(output_path, iterations=1, warmup=0, discard=False)
                    ),
                    check=True,
                )
                end_time = time.time()
                elapsed_time = end_time - start_time

                # Verify implementation generated output
                if not os.path.exists(output_path):
                    raise RuntimeError(
                        f"Implementation {task.name()} was ran to collect metrics but output file not found at: {output_path}{Style.RESET_ALL}"
                    )

                # 1. Get file size
                try:
                    filesize = os.path.getsize(output_path)
                except Exception:
                    filesize = 0

                # 2. Compute IQA via iqa-cli (SSIMULACRA2 + PSNR, from iqa-rs).
                # iqa-rs consumes raw pixels, so an encoded output is first
                # decoded to PPM with the format's reference decoder; decode tasks
                # already produce a PPM. The reference is the original source.
                if task.impl.type == BenchmarkType.ENCODE:
                    distorted_ppm = os.path.join(temp_dir, f"{identifier}_decoded.ppm")
                    _decode_to_ppm(task, output_path, distorted_ppm)
                else:
                    distorted_ppm = output_path
                score, psnr = _run_iqa(task.source_path, distorted_ppm)

                # 3. Get image dimensions from source file
                width, height, megapixels, bpp = 0, 0, 0.0, 0.0
                try:
                    with PILImage.open(task.source_path) as img:
                        width, height = img.size
                        megapixels = (width * height) / 1_000_000
                        if width > 0 and height > 0 and filesize > 0:
                            bpp = (filesize * 8) / (width * height)
                except Exception as img_err:
                    print(
                        f"{Fore.YELLOW}Warning: Could not get dimensions: {img_err}{Style.RESET_ALL}"
                    )

                psnr_str = f"{psnr:.2f}" if psnr is not None else "∞/NA"
                print(
                    f"{Fore.GREEN}✓ Size: {humanize.naturalsize(filesize, binary=True)}, "
                    f"SSIMULACRA2: {score:.2f}, PSNR: {psnr_str}, bpp: {bpp:.3f} "
                    f"{Style.RESET_ALL}(took {elapsed_time:.1f} s)"
                )

                metrics.append(
                    BenchmarkMetrics(
                        name=task.name(),
                        impl=task.impl.name,
                        lang=task.impl.lang,
                        build=task.impl.build,
                        **_task_metric_fields(task),
                        input_path=task.input_path,
                        source_path=task.source_path,
                        filesize=filesize,
                        ssimulacra2=score,
                        psnr=psnr,
                        error=None,
                        type=task.impl.type.value,
                        format=task.impl.format.value,
                        width=width,
                        height=height,
                        megapixels=megapixels,
                        bpp=bpp,
                    )
                )
            except Exception as e:
                print(f"{Fore.RED}✗ Error running {task.name()}: {e}")
                if isinstance(e, subprocess.CalledProcessError):
                    if e.stderr:
                        print(f"{Fore.YELLOW}Standard Error Output:")
                        print(f"{Fore.WHITE}{e.stderr.strip()}")
                    if e.stdout:
                        print(f"{Fore.YELLOW}Standard Output (at time of failure):")
                        print(f"{Fore.WHITE}{e.stdout.strip()}")

                metrics.append(
                    BenchmarkMetrics(
                        name=task.name(),
                        impl=task.impl.name,
                        lang=task.impl.lang,
                        build=task.impl.build,
                        **_task_metric_fields(task),
                        input_path=task.input_path,
                        source_path=task.source_path,
                        filesize=0,
                        ssimulacra2=-1.0,
                        psnr=None,
                        error=str(e),
                        type=task.impl.type.value,
                        format=task.impl.format.value,
                        width=0,
                        height=0,
                        megapixels=0.0,
                        bpp=0.0,
                    )
                )
    finally:
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


def _run_perf_suite(args: PerfArgs, result_dir: str):
    """Performance suite body: hyperfine timing of encode + decode at each
    implementation's fixed preset, swept across both threading modes. Timing is
    always compute-only (--discard); no size/quality metrics are collected here
    (that is the quality suite's job). Writes its artifacts into `result_dir`;
    the caller handles building and bundle finalization."""
    formats = args.formats
    # Both threading modes; --quick collapses to all-cores only.
    thread_modes = [0] if args.quick else list(THREAD_MODES)

    print("=" * 70)
    print("PERFORMANCE SUITE")
    print("=" * 70)

    manifest = {
        **_base_manifest(),
        "benchmark_config": {
            "suite": "performance",
            "dataset": args.dataset,
            "formats": formats,
            "mode": args.mode,
            "operating_point": "perf-preset",
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

    types_to_run = []
    if args.mode in (BenchmarkMode.ENCODE, BenchmarkMode.BOTH):
        types_to_run.append(BenchmarkType.ENCODE)
    if args.mode in (BenchmarkMode.DECODE, BenchmarkMode.BOTH):
        types_to_run.append(BenchmarkType.DECODE)

    benches: BenchList = []
    for bench_type in types_to_run:
        for format in formats:
            for threads in thread_modes:
                benches += build_bench_list(
                    bench_type, format, args.dataset, threads, args
                )

    if not benches:
        print("\nError: No benchmarks to run!")
        sys.exit(1)

    print(f"\n✓ {len(benches)} timing benchmark(s) ready to run\n")
    print("=" * 70)
    print("RUNNING TIMING BENCHMARKS")
    print("=" * 70)
    print()

    json_output = f"{result_dir}/raw.json"
    if not args.quick:
        hyperfine_cmd = [
            "hyperfine",
            "--warmup",
            "3",
            "--min-runs",
            "10",
            "--export-json",
            json_output,
        ]
    else:
        hyperfine_cmd = [
            "hyperfine",
            "--warmup",
            "0",
            "--min-runs",
            "1",
            "--max-runs",
            "1",
            "--export-json",
            json_output,
        ]
    for task in benches:
        hyperfine_cmd.extend(["--command-name", task.name()])
    hyperfine_cmd.extend([task.cmd("/dev/null") for task in benches])
    if args.debug:
        hyperfine_cmd.append("--show-output")

    try:
        subprocess.run(hyperfine_cmd, check=True)
    except FileNotFoundError:
        print("\nError: 'hyperfine' not found")
        sys.exit(1)
    except subprocess.CalledProcessError:
        print("\nError: Benchmark execution failed")
        print(f"Test command: {' '.join(hyperfine_cmd)}")
        sys.exit(1)

    # Timing-only summary (no IQA/size metrics in the performance suite).
    generate_summary(result_dir, json_output, None)

    if args.measure_memory:
        mem_commands = [
            task.cmd("/dev/null", iterations=1, warmup=0) for task in benches
        ]
        mem_names = [task.name() for task in benches]
        measure_memory(result_dir, mem_commands, mem_names)


def _run_quality_suite(args: QualityArgs, result_dir: str) -> list[str]:
    """Quality suite body: sweep each lossy encoder's quality axis over many
    steps and measure file size + IQA (SSIMULACRA2, PSNR) per step, tracing a
    rate-distortion curve (issue #8). Encoders only; no timing, no thread sweep.
    Writes its artifacts into `result_dir`; the caller handles building and
    bundle finalization.

    Returns the names of any runs that failed (empty if all succeeded). On
    failure the per-suite summary is skipped — the sweep has holes, so the
    caller must abort without finalizing the bundle."""
    formats = args.formats

    print("=" * 70)
    print("QUALITY SUITE")
    print("=" * 70)

    benches: BenchList = []
    for format in formats:
        benches += build_quality_bench_list(format, args.dataset, args)

    if not benches:
        print("\nError: No quality benchmarks to run (no lossy encoders selected)!")
        sys.exit(1)

    # Record which quality-axis values each encoder actually swept (for the
    # reproducibility manifest and downstream rate-distortion plots).
    sweeps: Dict[str, list[str]] = {}
    for task in benches:
        sweeps.setdefault(task.impl.name, [])
        axis = schema_for(task.impl.name).quality_axis
        value = task.params.get(axis, "") if axis else ""
        if value and value not in sweeps[task.impl.name]:
            sweeps[task.impl.name].append(value)

    manifest = {
        **_base_manifest(),
        "benchmark_config": {
            "suite": "quality",
            "dataset": args.dataset,
            "formats": formats,
            "quality_steps": args.quality_steps,
            "quality_sweeps": sweeps,
            "quick": args.quick,
        },
    }
    with open(f"{result_dir}/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n✓ Manifest written to {result_dir}/manifest.json")
    print(f"\n✓ {len(benches)} quality measurement(s) across the sweep\n")

    metrics = generate_metrics(benches, result_dir)
    metrics_path = f"{result_dir}/metrics.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"\n✓ Metrics saved to {metrics_path}")

    # A failed run leaves a hole in the rate-distortion sweep, so the summary and
    # report would be incomplete/misleading. Keep the raw metrics.json (error
    # rows included for debugging) but skip report generation and signal failure
    # to the caller, which aborts before finalizing the bundle.
    failures = [m["name"] for m in metrics if m["error"] is not None]
    if failures:
        return failures

    # IQA/size-only summary (rate-distortion plots, no timing in this suite).
    generate_summary(result_dir, None, metrics)
    return []


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


def run_perf(args: PerfArgs):
    """Run the performance suite into a fresh bundle (performance/ subfolder)."""
    if not args.skip_build:
        build_projects(args.formats)
    else:
        print("Skipping build step (--skip-build)...")
    bundle = _new_result_dir()
    perf_dir = os.path.join(bundle, "performance")
    os.makedirs(perf_dir, exist_ok=True)
    _run_perf_suite(args, perf_dir)
    _finalize_bundle(bundle, ["performance"])
    _print_bundle(bundle, ["performance"])


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


def run_quality(args: QualityArgs):
    """Run the quality suite into a fresh bundle (quality/ subfolder)."""
    if not args.skip_build:
        build_projects(args.formats)
    else:
        print("Skipping build step (--skip-build)...")
    bundle = _new_result_dir()
    qual_dir = os.path.join(bundle, "quality")
    os.makedirs(qual_dir, exist_ok=True)
    failures = _run_quality_suite(args, qual_dir)
    if failures:
        _abort_on_quality_failures(failures, bundle)
    _finalize_bundle(bundle, ["quality"])
    _print_bundle(bundle, ["quality"])


def run_all(args: AllArgs):
    """Run both suites into one bundle (performance/ + quality/ subfolders)."""
    if not args.skip_build:
        build_projects(args.formats)
    else:
        print("Skipping build step (--skip-build)...")
    bundle = _new_result_dir()
    perf_dir = os.path.join(bundle, "performance")
    qual_dir = os.path.join(bundle, "quality")
    os.makedirs(perf_dir, exist_ok=True)
    os.makedirs(qual_dir, exist_ok=True)

    # Suites skip their own build (done once above) via skip_build=True.
    perf_args = PerfArgs(
        formats=args.formats,
        dataset=args.dataset,
        mode=args.mode,
        iterations=args.iterations,
        warmup=args.warmup,
        sample=args.sample,
        pin_cores=args.pin_cores,
        quick=args.quick,
        measure_memory=args.measure_memory,
        skip_build=True,
        debug=args.debug,
    )
    quality_args = QualityArgs(
        formats=args.formats,
        dataset=args.dataset,
        sample=args.sample,
        quality_steps=args.quality_steps,
        quick=args.quick,
        skip_build=True,
        debug=args.debug,
    )
    _run_perf_suite(perf_args, perf_dir)
    failures = _run_quality_suite(quality_args, qual_dir)
    if failures:
        _abort_on_quality_failures(failures, bundle)
    _finalize_bundle(bundle, ["performance", "quality"])
    _print_bundle(bundle, ["performance", "quality"])


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
