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
    ALL_QUALITY_TIERS,
    DATASETS,
    FORMAT_EXT_MAP,
    IMPLEMENTATIONS,
    NULL_IMPLEMENTATIONS,
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
    PPMImageFormat,
    QualityTier,
    RunArgs,
    SetupArgs,
    find_implementation_by_name,
    is_format_lossless,
)
from bench_lib.summary import generate_summary
from bench_lib.system_info import (
    _detect_mimalloc_version,
    get_compiler_versions,
    get_library_versions,
    get_system_info,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATASET_FILES_CHECKED: Set[str] = set()

# Cache input file list for re-use
INPUT_FILES_CACHE: Dict[
    tuple[DatasetId, ImageFormats, QualityTier, Optional[int]],
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
    quality: QualityTier,
    limit: Optional[int] = None,
) -> Sequence[tuple[str, str]]:
    """
    Get list of input files for a given dataset, benchmark type.

    Returns a sequence of (input_path, source_path) tuples, where:
    - input_path: Path to the input file for the benchmark.
    - source_path: Path to the original source file (for quality comparison).

    Pre-generates input files for benchmark type if necessary.
    """

    if (dataset_name, format, quality, limit) in INPUT_FILES_CACHE:
        return INPUT_FILES_CACHE[(dataset_name, format, quality, limit)]

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
            # Determine target file name
            base_path = os.path.splitext(f)[0]
            if is_format_lossless(format):
                target_file = f"{base_path}.{target_ext}"
            else:
                target_file = f"{base_path}_{quality.value}.{target_ext}"

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

                # Run encoder
                subprocess.run(
                    [
                        ref_impl.bin,
                        "--input",
                        intermediate_ppm,
                        "--output",
                        target_file,
                        "--quality",
                        quality.value,
                        "--iterations",
                        "1",
                        "--warmup",
                        "0",
                        "--threads",
                        "0",
                    ],
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

    INPUT_FILES_CACHE[(dataset_name, format, quality, limit)] = input_files
    return input_files


def build_bench_list(
    type: BenchmarkType,
    format: ImageFormat,
    dataset: DatasetId,
    quality: QualityTier,
    threads: int,
    args: RunArgs,
) -> BenchList:
    """Construct list of benchmark commands to run for one (type, format,
    quality, threads) cell of the swept experiment matrix."""
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

        for input_file, source_file in get_input_files(
            dataset, input_format, quality, args.sample
        ):
            input_path = input_file

            bench = BenchmarkTask(
                impl=impl,
                quality=quality,
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

                # 2. Run (modified) ssimulacra2_rs binary.
                score = -1.0
                ssimulacra2_bin = os.path.join(
                    PROJECT_ROOT,
                    "vendor",
                    "build",
                    "ssimulacra2",
                    "release",
                    "ssimulacra2_rs",
                )
                res = subprocess.run(
                    [ssimulacra2_bin, "image", task.source_path, output_path],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                out_str = res.stdout.strip()  # Output is like "Score: 87.432321"

                if re.fullmatch(r"^Score: -?\d+\.\d+$", out_str):
                    score = float(out_str.split(": ")[1])
                else:
                    raise ValueError(
                        f"Unable to parse SSIMULACRA 2 output: `{out_str}`"
                    )

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

                print(
                    f"{Fore.GREEN}✓ Size: {humanize.naturalsize(filesize, binary=True)}, Score: {score:.2f}, bpp: {bpp:.3f} {Style.RESET_ALL}(took {elapsed_time:.1f} s)"
                )

                metrics.append(
                    BenchmarkMetrics(
                        name=task.name(),
                        impl=task.impl.name,
                        lang=task.impl.lang,
                        build=task.impl.build,
                        quality=task.quality.value,
                        input_path=task.input_path,
                        source_path=task.source_path,
                        filesize=filesize,
                        ssimulacra2=score,
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
                        quality=task.quality.value,
                        input_path=task.input_path,
                        source_path=task.source_path,
                        filesize=0,
                        ssimulacra2=-1.0,
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


def run(args: RunArgs):
    """Execute benchmarks using hyperfine."""
    formats = args.formats

    # Determine the swept experiment matrix. A normal run sweeps every quality
    # tier and both threading modes so a single `./bench run -d <dataset>`
    # yields a complete, self-comparable dataset (issue #9). --quick collapses
    # the sweep to one fast cell for smoke tests.
    if args.quick:
        quality_tiers = [QualityTier.WEB_LOW]
        thread_modes = [0]
    else:
        quality_tiers = list(ALL_QUALITY_TIERS)
        thread_modes = list(THREAD_MODES)

    # Build projects
    if not args.skip_build:
        build_projects(formats)
    else:
        print("Skipping build step (--skip-build)...")

    # Create results directory
    os.makedirs("results", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    result_dir = f"results/{timestamp}"
    os.makedirs(result_dir, exist_ok=True)

    print("=" * 70)
    print("COLLECTING BENCHMARK COMMANDS")
    print("=" * 70)

    # Generate manifest
    manifest = {
        **get_system_info(),
        "compiler": get_compiler_versions(),
        "libraries": get_library_versions(),
        "allocator": f"mimalloc {_detect_mimalloc_version()}",
        "benchmark_config": {
            # The only true experiment variable.
            "dataset": args.dataset,
            # Subset filters (default = everything).
            "formats": formats,
            "mode": args.mode,
            # Swept every run; timing/memory vary across these, metrics do not.
            "quality_tiers": quality_tiers,
            "thread_modes": thread_modes,
            # Fixed: timing is always compute-only.
            "discard_output": True,
            # Measurement tuning (do not change the experimental condition).
            "iterations": args.iterations,
            "warmup": args.warmup,
            "pin_cores": args.pin_cores,
            "quick": args.quick,
        },
    }

    with open(f"{result_dir}/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n✓ Manifest written to {result_dir}/manifest.json")

    benches: BenchList = []

    types_to_run = []
    if args.mode in (BenchmarkMode.ENCODE, BenchmarkMode.BOTH):
        types_to_run.append(BenchmarkType.ENCODE)
    if args.mode in (BenchmarkMode.DECODE, BenchmarkMode.BOTH):
        types_to_run.append(BenchmarkType.DECODE)

    # Sweep the full matrix: (type, format) x quality x threads.
    for bench_type in types_to_run:
        for format in formats:
            for quality in quality_tiers:
                for threads in thread_modes:
                    benches += build_bench_list(
                        bench_type, format, args.dataset, quality, threads, args
                    )

    if not benches:
        print("\nError: No benchmarks to run!")
        sys.exit(1)

    json_output = None
    if not args.no_benchmarks:
        print(f"\n✓ {len(benches)} benchmark(s) ready to run\n")

        # Run hyperfine
        print("=" * 70)
        print("RUNNING BENCHMARKS")
        print("=" * 70)
        print()

        json_output = f"{result_dir}/raw.json"
        hyperfine_cmd: list[str]
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

        # Add command names for better output
        for task in benches:
            hyperfine_cmd.extend(["--command-name", task.name()])

        hyperfine_cmd.extend([task.cmd("/dev/null") for task in benches])

        # --show-output if debug
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

    # Collect metrics
    metrics = None
    if not args.no_metrics:
        # File size and SSIMULACRA2 depend only on (impl, format, type, quality,
        # image) — the encoded/decoded bytes are identical across thread modes.
        # Deduplicate so each tuple is measured (and ssimulacra2'd) exactly once.
        seen_metric_keys: Set[tuple] = set()
        metrics_benches: BenchList = []
        for task in benches:
            key = (
                task.impl.name,
                task.format_as_str(),
                task.impl.type.value,
                task.quality.value,
                os.path.basename(task.input_path),
            )
            if key in seen_metric_keys:
                continue
            seen_metric_keys.add(key)
            metrics_benches.append(task)

        metrics = generate_metrics(metrics_benches, result_dir)
        # Save metrics
        metrics_path = f"{result_dir}/metrics.json"
        with open(metrics_path, "w") as f:
            json.dump(metrics, f, indent=2)

        print(f"\n✓ Metrics saved to {metrics_path}")

    # Generate summary
    generate_summary(result_dir, json_output, metrics)

    # Measure memory if requested
    if args.measure_memory:
        mem_commands = [
            task.cmd("/dev/null", iterations=1, warmup=0) for task in benches
        ]
        mem_names = [task.name() for task in benches]
        measure_memory(result_dir, mem_commands, mem_names)
        print("  - memory.csv     : Peak RSS measurements")

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"\nResults saved to: {result_dir}/")
    print("  - manifest.json  : Reproducibility metadata")
    print("  - raw.json       : Full hyperfine output")
    if not args.no_metrics:
        print("  - metrics.json   : File sizes and SSIMULACRA 2 scores")
    if not args.no_benchmarks:
        print("  - summary.md     : Human-readable tables")
    print()


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
