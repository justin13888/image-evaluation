"""Scaling suite: how encode/decode time scales with pixel count.

The main rate-distortion sweep runs every image at its native resolution, so
pixel count is confounded with content and the *scaling behaviour* of a codec is
invisible — yet codecs differ sharply here (AVIF/AV1 encode time grows
super-linearly with pixels while JPEG is ~linear). This suite isolates that: it
takes a few source images, downscales each to a controlled ladder of pixel
counts (same content, only resolution varies), times every codec at its
performance preset on each rung, and fits ``time ∝ pixels^k`` per codec — so the
exponent ``k`` (≈1 linear, >1 super-linear) is reported directly.

Timing is single-threaded on purpose: it isolates the pixel-count exponent from
parallel-scaling efficiency (tiling/thread-pool effects), which is a separate
question. The ladder is downscale-only (never upscales a source), so every rung
is real detail, not interpolation.

This module is pure (depends only on ``models`` + ``summary._parse_command_name`` +
matplotlib/numpy); ``runner._run_scaling_suite`` orchestrates it (gathers the
dataset files, pre-builds binaries, runs hyperfine via the shared driver).
"""

import json
import os
import subprocess
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np

from bench_lib.imageprep import image_pixels, single_thread_env, to_canonical_ppm
from bench_lib.models import (
    FORMAT_EXT_MAP,
    IMPLEMENTATIONS,
    REFERENCE_ENCODERS,
    BenchList,
    BenchmarkTask,
    BenchmarkType,
    DatasetId,
    ImageFormat,
    find_implementation_by_name,
    schema_for,
)

# Default pixel-count ladder (megapixels), downscale-only. Capped at 2 MP so it
# stays a genuine downscale of typical ~2.8 MP photographic sources (CLIC2025);
# the 0.25→2 MP span (8×) is wide enough to fit a stable log-log slope. Override
# via --scaling-ladder.
SCALING_LADDER_MP: List[float] = [0.25, 0.5, 1.0, 2.0]

# Lighter hyperfine flags than the performance overlay: a log-log slope only needs
# a rough mean per rung, so a handful of runs suffices (and keeps the suite cheap
# even where a 2 MP AVIF encode takes seconds).
SCALING_HYPERFINE_FLAGS: List[str] = ["--warmup", "1", "--min-runs", "3"]

_CACHE_ROOT = os.path.join("data", ".scaling_cache")


def scaling_label(target_mp: float) -> str:
    """Grammar-safe operating-point label for a ladder rung, e.g. ``mp-0.5``.
    Must never contain ``", "``, ``"="`` or ``" ("`` (see ``BenchmarkTask.name``
    / ``summary._parse_command_name``)."""
    label = f"mp-{target_mp:g}"
    assert ", " not in label and "=" not in label and " (" not in label, (
        f"scaling label {label!r} would break command-name parsing"
    )
    return label


def select_scaling_sources(files: List[str], n: int) -> List[str]:
    """The ``n`` largest source images (by pixel count). Largest first maximises
    the downscale range available for the ladder; ties break on path for
    determinism."""
    sized = [(f, image_pixels(f)) for f in files]
    sized = [(f, px) for f, px in sized if px > 0]
    sized.sort(key=lambda fp: (-fp[1], fp[0]))
    return [f for f, _ in sized[: max(0, n)]]


# One ladder rung: the downscaled PPM plus its actual pixel count (the x-axis).
Rung = Dict[str, object]  # {"ppm": str, "pixels": int, "target_mp": float}


def generate_ladder(
    dataset: DatasetId, sources: List[str], ladder_mp: List[float]
) -> List[Rung]:
    """Materialise the downscale-only ladder for ``sources``, caching PPMs under
    ``data/.scaling_cache/<dataset>/``. A rung is emitted only when its target is
    strictly smaller than the source (no upscaling); each carries the *actual*
    pixel count of the generated PPM."""
    cache_dir = os.path.join(_CACHE_ROOT, dataset.value)
    rungs: List[Rung] = []
    for src in sources:
        src_px = image_pixels(src)
        if src_px <= 0:
            continue
        stem = os.path.splitext(os.path.basename(src))[0]
        for mp in sorted(ladder_mp):
            target_px = int(mp * 1_000_000)
            if target_px >= src_px:
                continue  # downscale-only
            out_ppm = os.path.join(cache_dir, f"{stem}.{scaling_label(mp)}.ppm")
            if not to_canonical_ppm(src, out_ppm, target_px):
                continue
            rungs.append(
                {"ppm": out_ppm, "pixels": image_pixels(out_ppm), "target_mp": mp}
            )
    return rungs


def _timing_task(
    impl, params: Dict[str, str], input_path: str, target_mp: float
) -> BenchmarkTask:
    """A single-threaded, compute-only (discard) timing task for one rung."""
    return BenchmarkTask(
        impl=impl,
        params=params,
        label=scaling_label(target_mp),
        input_path=input_path,
        source_path=input_path,
        iterations=1,
        warmup=0,
        threads=1,
        discard_output=True,
        measure_memory=False,
        pin_cores=False,
    )


def _reference_encode_rung(ref_impl, fmt: ImageFormat, rung: Rung) -> Optional[str]:
    """Encode a rung's PPM with the format's reference encoder at its preset, for
    use as a decoder input. Cached next to the PPM as ``<ppm-stem>.<ext>``; None
    if the reference binary is unavailable or the encode fails."""
    if not os.path.exists(ref_impl.bin):
        return None
    ppm = str(rung["ppm"])
    ext = FORMAT_EXT_MAP[fmt]
    target = f"{os.path.splitext(ppm)[0]}.{ext}"
    if not os.path.exists(target):
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
        for k, v in sorted(schema_for(ref_impl.name).perf_params().items()):
            cmd += ["--param", f"{k}={v}"]
        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=single_thread_env(),
            )
        except subprocess.CalledProcessError:
            return None
    return target if os.path.exists(target) else None


def build_scaling_tasks(
    formats: List[ImageFormat], rungs: List[Rung]
) -> Tuple[BenchList, Dict[str, int]]:
    """Build the per-format timing tasks across the ladder and a basename→pixels
    map (the x-axis lookup the summary uses, keyed by each task input's basename).

    Encoders: base implementations only (no secondary-knob variants), each at its
    performance preset. Decoders: the format's reference encoder produces one
    encoded input per rung, decoded by every decoder. Implementations whose binary
    is missing are skipped (the suite runs after the build step, so this only trips
    on a partial build)."""
    tasks: BenchList = []
    pixels_by_basename: Dict[str, int] = {}

    def add(impl, params, input_path, rung) -> None:
        tasks.append(_timing_task(impl, params, input_path, float(rung["target_mp"])))
        pixels_by_basename[os.path.basename(input_path)] = int(rung["pixels"])

    for fmt in formats:
        encoders = [
            i
            for i in IMPLEMENTATIONS
            if i.format == fmt
            and i.type == BenchmarkType.ENCODE
            and i.variant_kind is None
            and os.path.exists(i.bin)
        ]
        for impl in encoders:
            params = schema_for(impl.name).perf_params()
            for rung in rungs:
                add(impl, params, str(rung["ppm"]), rung)

        decoders = [
            i
            for i in IMPLEMENTATIONS
            if i.format == fmt
            and i.type == BenchmarkType.DECODE
            and os.path.exists(i.bin)
        ]
        if not decoders:
            continue
        ref_name = REFERENCE_ENCODERS.get(fmt)
        ref_impl = find_implementation_by_name(ref_name) if ref_name else None
        if not ref_impl:
            continue
        for rung in rungs:
            enc = _reference_encode_rung(ref_impl, fmt, rung)
            if enc is None:
                continue
            for dimpl in decoders:
                add(dimpl, {}, enc, rung)

    return tasks, pixels_by_basename


def _fit_exponent(
    pixels: List[int], times_s: List[float]
) -> Optional[Tuple[float, float]]:
    """Fit ``log(time) = k·log(pixels) + b`` and return ``(k, r2)``; None if there
    are fewer than two distinct positive points."""
    xs = np.array(pixels, dtype=float)
    ys = np.array(times_s, dtype=float)
    mask = (xs > 0) & (ys > 0)
    xs, ys = xs[mask], ys[mask]
    if len(xs) < 2 or len(set(xs.tolist())) < 2:
        return None
    lx, ly = np.log(xs), np.log(ys)
    k, b = np.polyfit(lx, ly, 1)
    pred = k * lx + b
    ss_res = float(((ly - pred) ** 2).sum())
    ss_tot = float(((ly - ly.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return float(k), r2


def write_scaling_outputs(
    result_dir: str, raw_json_path: str, pixels_by_basename: Dict[str, int]
) -> None:
    """Parse the hyperfine export, fit a scaling exponent per (format, op, impl),
    write a log-log chart per (format, op), and a ``summary.md`` leaderboard."""
    # Lazy import avoids a models→summary→… cycle at module load.
    from bench_lib.summary import _parse_command_name

    try:
        with open(raw_json_path) as f:
            results = json.load(f).get("results", [])
    except Exception as e:
        print(f"Warning: could not read scaling results: {e}")
        return

    # series[(fmt, type)][impl] -> list[(pixels, mean_seconds)]
    series: Dict[Tuple[str, str], Dict[str, List[Tuple[int, float]]]] = {}
    for r in results:
        parsed = _parse_command_name(r.get("command", ""))
        if parsed is None or parsed["format"] == "null":
            continue
        px = pixels_by_basename.get(parsed["basename"])
        mean = r.get("mean")
        if not px or mean is None:
            continue
        key = (parsed["format"], parsed["type"])
        series.setdefault(key, {}).setdefault(parsed["impl"], []).append(
            (px, float(mean))
        )

    rows: List[Tuple[str, str, str, Optional[float], Optional[float], float]] = []
    for (fmt, btype), impls in sorted(series.items()):
        fig, ax = plt.subplots(figsize=(7, 5))
        for impl in sorted(impls):
            pts = sorted(impls[impl])
            xs_mp = [p / 1_000_000 for p, _ in pts]
            ys_ms = [t * 1000 for _, t in pts]
            fit = _fit_exponent([p for p, _ in pts], [t for _, t in pts])
            if fit is not None:
                k, r2 = fit
                label = f"{impl} (k={k:.2f}, R²={r2:.2f})"
            else:
                k, r2, label = None, None, impl
            (line,) = ax.plot(xs_mp, ys_ms, "o", label=label)
            if fit is not None and len(xs_mp) >= 2:
                xmin, xmax = min(xs_mp), max(xs_mp)
                xline = np.array([xmin, xmax])
                # y = exp(b) * (px)^k; refit in MP space for the drawn line.
                lk, lb = np.polyfit(np.log([x for x in xs_mp]), np.log(ys_ms), 1)
                yline = np.exp(lb) * xline**lk
                ax.plot(xline, yline, "--", color=line.get_color(), alpha=0.5)
            largest_ms = ys_ms[-1] if ys_ms else float("nan")
            rows.append((fmt, btype, impl, k, r2, largest_ms))

        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("Megapixels (log)")
        ax.set_ylabel("Mean time, ms (log)")
        ax.set_title(f"{btype.capitalize()} {fmt.upper()} — time vs pixels (1 thread)")
        ax.grid(True, which="both", ls=":", alpha=0.4)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(os.path.join(result_dir, f"scaling_{fmt}_{btype}.svg"))
        plt.close(fig)

    _write_summary_md(result_dir, rows)


def _write_summary_md(
    result_dir: str,
    rows: List[Tuple[str, str, str, Optional[float], Optional[float], float]],
) -> None:
    lines = [
        "# Scaling — time vs pixel count\n",
        "Each codec is timed single-threaded at its performance preset on a "
        "downscale-only resolution ladder (same content, only pixels vary), and "
        "`time ∝ pixels^k` is fit in log-log space. **k ≈ 1 = linear; k > 1 = "
        "super-linear** (cost grows faster than pixel count). Single-threaded to "
        "isolate the pixel-count exponent from parallel-scaling effects.\n",
        "| Format | Op | Implementation | Scaling k | R² | Mean ms @ largest rung |",
        "|--------|----|----------------|-----------|----|------------------------|",
    ]
    for fmt, btype, impl, k, r2, largest_ms in sorted(rows):
        k_str = f"{k:.2f}" if k is not None else "N/A"
        r2_str = f"{r2:.2f}" if r2 is not None else "N/A"
        lines.append(
            f"| {fmt.upper()} | {btype} | {impl} | {k_str} | {r2_str} | "
            f"{largest_ms:.2f} |"
        )
    lines.append("")
    with open(os.path.join(result_dir, "summary.md"), "w") as f:
        f.write("\n".join(lines))
    print(f"\n✓ Scaling summary written to {result_dir}/summary.md")
