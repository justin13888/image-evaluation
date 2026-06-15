"""Effort/speed suite: the time ↔ quality ↔ size tradeoff for lossy codecs.

The rate-distortion sweep pins each lossy codec's effort/speed knob at its
performance preset (AVIF ``speed=6``, libjxl ``effort=7``, libwebp ``method=4``)
and only the anchor point is timed — so the *effort lever*, one of the biggest
practical differentiators (AVIF ``speed 0`` can be >10× slower than ``speed 10``
for a few dB), is invisible. This opt-in suite holds quality fixed and sweeps the
effort knob instead, recording encode time, size (bpp) and SSIMULACRA2 at each
setting, so the tradeoff curve is explicit.

Encodes happen at a fixed ~1 MP downscale of a few sources (reusing the scaling
ladder's downscaler) to keep the slow (high-effort) end bounded and the times
comparable across codecs at one resolution.

Pure module (models + scaling + matplotlib); ``runner._run_effort_suite``
orchestrates it, reusing ``generate_metrics`` for the encode→decode→score pass.
"""

import os
from collections import defaultdict
from typing import Dict, List, Tuple

import matplotlib.pyplot as plt

from bench_lib import scaling
from bench_lib.models import (
    BenchList,
    BenchmarkMetrics,
    BenchmarkTask,
    BenchmarkType,
    DatasetId,
    ImageFormat,
    find_implementation_by_name,
    quality_label,
    schema_for,
)

# Fixed resolution for the effort sweep: one downscale per source so the
# high-effort end stays affordable and times are comparable at a common size.
EFFORT_MP = 1.0

# Lossy encoders whose effort/speed knob is pinned in the RD sweep -> (knob,
# values to sweep). Ranges are sampled (not every step) since the tradeoff curve
# is smooth and the slow end is costly; quality stays at each encoder's preset.
EFFORT_AXES: Dict[str, Tuple[str, List[str]]] = {
    # AVIF speed: 0 = slowest/best, 10 = fastest/worst.
    "libavif-encode": ("speed", ["0", "2", "4", "6", "8", "10"]),
    "svt-av1-encode": ("speed", ["0", "2", "4", "6", "8", "10"]),
    "rav1e-encode": ("speed", ["0", "2", "4", "6", "8", "10"]),
    # libjxl / zune-jpegxl effort: 1 = fastest, 9 = slowest/best.
    "libjxl-encode": ("effort", ["1", "3", "5", "7", "9"]),
    "zune-jpegxl-encode": ("effort", ["1", "3", "5", "7", "9"]),
    # libwebp / zenwebp method: 0 = fastest, 6 = slowest/best.
    "libwebp-encode": ("method", ["0", "2", "4", "6"]),
    "zenwebp-encode": ("method", ["0", "2", "4", "6"]),
}


def prepare_effort_images(dataset: DatasetId, sources: List[str]) -> List[str]:
    """Downscale each source to ~``EFFORT_MP`` (never upscaling) as a cached 8-bit
    PPM, reusing the scaling ladder's downscaler/cache. Returns the PPM paths."""
    cache_dir = os.path.join("data", ".scaling_cache", dataset.value)
    out: List[str] = []
    for src in sources:
        src_px = scaling._image_pixels(src)
        if src_px <= 0:
            continue
        target_px = min(int(EFFORT_MP * 1_000_000), src_px)  # never upscale
        stem = os.path.splitext(os.path.basename(src))[0]
        ppm = os.path.join(cache_dir, f"{stem}.effort.ppm")
        if scaling._downscale_to_ppm(src, target_px, ppm):
            out.append(ppm)
    return out


def build_effort_tasks(formats: List[ImageFormat], image_ppms: List[str]) -> BenchList:
    """Scored encode tasks sweeping each lossy codec's effort knob at fixed quality.

    For every effort-swept encoder of a selected format whose binary exists, emit
    one task per (effort value, image): params are the encoder's preset with only
    the effort knob overridden, so quality is held constant. Tasks are scored
    (``discard_output=False``) and single-threaded, matching the metric pass."""
    fmt_set = set(formats)
    tasks: BenchList = []
    for impl_name, (knob, values) in EFFORT_AXES.items():
        impl = find_implementation_by_name(impl_name)
        if (
            impl is None
            or impl.format not in fmt_set
            or impl.type != BenchmarkType.ENCODE
        ):
            continue
        if not os.path.exists(impl.bin):
            continue  # not built (e.g. rav1e without nasm) — skip gracefully
        preset = schema_for(impl_name).perf_params()
        for value in values:
            params = {**preset, knob: value}
            label = quality_label(knob, value)  # e.g. speed-6, effort-7, method-4
            for ppm in image_ppms:
                tasks.append(
                    BenchmarkTask(
                        impl=impl,
                        params=params,
                        label=label,
                        input_path=ppm,
                        source_path=ppm,
                        iterations=1,
                        warmup=0,
                        threads=1,
                        discard_output=False,
                        measure_memory=False,
                        pin_cores=False,
                    )
                )
    return tasks


# agg[(format, impl)][value] -> mean (time_s, bpp, ssimulacra2) over images
def _aggregate(
    metrics: List[BenchmarkMetrics],
) -> Dict[Tuple[str, str, str], Dict[float, Tuple[float, float, float]]]:
    sums: Dict[Tuple[str, str, str], Dict[float, List[float]]] = defaultdict(
        lambda: defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    )
    for m in metrics:
        if m.get("error"):
            continue
        label = m["label"]
        knob, _, valstr = label.partition("-")
        try:
            value = float(valstr)
        except ValueError:
            continue
        cell = sums[(m["format"], m["impl"], knob)][value]
        cell[0] += m.get("time_s") or 0.0
        cell[1] += m.get("bpp") or 0.0
        cell[2] += m.get("ssimulacra2") if m.get("ssimulacra2") is not None else 0.0
        cell[3] += 1
    out: Dict[Tuple[str, str, str], Dict[float, Tuple[float, float, float]]] = {}
    for key, by_val in sums.items():
        out[key] = {
            v: (c[0] / c[3], c[1] / c[3], c[2] / c[3])
            for v, c in by_val.items()
            if c[3] > 0
        }
    return out


def write_effort_outputs(result_dir: str, metrics: List[BenchmarkMetrics]) -> None:
    """Per format, plot encode time / bpp / SSIMULACRA2 vs the effort knob (one
    line per codec), and write a ``summary.md`` table of the swept points."""
    agg = _aggregate(metrics)

    # Group impls by format (each format shares one knob name).
    by_format: Dict[str, List[Tuple[str, str, Dict[float, Tuple[float, float, float]]]]]
    by_format = defaultdict(list)
    for (fmt, impl, knob), pts in agg.items():
        by_format[fmt].append((impl, knob, pts))

    charts = [
        ("time", "Encode time (ms)", lambda tbs: tbs[0] * 1000.0),
        ("bpp", "Bits per pixel", lambda tbs: tbs[1]),
        ("quality", "SSIMULACRA2", lambda tbs: tbs[2]),
    ]
    for fmt, impls in sorted(by_format.items()):
        knob = impls[0][1] if impls else "effort"
        for suffix, ylabel, pick in charts:
            fig, ax = plt.subplots(figsize=(7, 5))
            for impl, _knob, pts in sorted(impls):
                xs = sorted(pts)
                ys = [pick(pts[v]) for v in xs]
                ax.plot(xs, ys, "o-", label=impl)
            ax.set_xlabel(f"{knob} (effort knob)")
            ax.set_ylabel(ylabel)
            ax.set_title(f"{fmt.upper()} — {ylabel} vs {knob} (quality fixed, ~1 MP)")
            ax.grid(True, ls=":", alpha=0.4)
            ax.legend(fontsize=8)
            fig.tight_layout()
            fig.savefig(os.path.join(result_dir, f"effort_{fmt}_{suffix}.svg"))
            plt.close(fig)

    _write_summary_md(result_dir, agg)


def _write_summary_md(
    result_dir: str,
    agg: Dict[Tuple[str, str, str], Dict[float, Tuple[float, float, float]]],
) -> None:
    lines = [
        "# Effort / speed — time vs quality vs size\n",
        "Each lossy codec's effort/speed knob swept at a fixed quality preset, on a "
        "~1 MP downscale of a few sources (means across images). This is the lever "
        "the rate-distortion sweep pins: it trades encode time for size/quality. "
        "Encode time is a single-pass wall-clock (relative), not the performance "
        "suite's isolated timing.\n",
        "| Format | Implementation | Knob=value | Mean time (ms) | Mean bpp | Mean SSIMULACRA2 |",
        "|--------|----------------|------------|----------------|----------|------------------|",
    ]
    for (fmt, impl, knob), pts in sorted(agg.items()):
        for value in sorted(pts):
            time_s, bpp, ss2 = pts[value]
            lines.append(
                f"| {fmt.upper()} | {impl} | {knob}={value:g} | {time_s * 1000:.2f} | "
                f"{bpp:.3f} | {ss2:.2f} |"
            )
    lines.append("")
    with open(os.path.join(result_dir, "summary.md"), "w") as f:
        f.write("\n".join(lines))
    print(f"\n✓ Effort summary written to {result_dir}/summary.md")
