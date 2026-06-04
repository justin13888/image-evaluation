"""Matplotlib figure creation for benchmark results."""

import matplotlib.pyplot as plt
import numpy as np
from typing import Callable, Dict, Any, Optional, Tuple

from bench_lib.models import (
    BenchmarkKey,
    BenchmarkMetrics,
    BenchmarkType,
    ImageFormat,
)

# Threading modes -> (legend label, bar colour). 1 = single-threaded, 0 = all cores.
_THREAD_STYLES = {
    1: ("single-threaded", "#4C72B0"),
    0: ("all-cores", "#DD8452"),
}


def _filter_valid_encode_metrics(
    metrics: list[BenchmarkMetrics],
) -> list[BenchmarkMetrics]:
    """Filter to encode-only metrics with valid bpp, score, and no errors."""
    return [
        m
        for m in metrics
        if m["type"] == "encode"
        and m["bpp"] > 0
        and m["ssimulacra2"] > 0
        and not m.get("error")
    ]


def _group_by(items: list, key_fn: Callable) -> Dict:
    """Group a list of items by a key function into a dict of lists."""
    groups: Dict = {}
    for item in items:
        k = key_fn(item)
        if k not in groups:
            groups[k] = []
        groups[k].append(item)
    return groups


def create_plots_from_parsed_results(
    parsed: Dict[str, Dict[str, Dict[str, list[Dict[str, Any]]]]],
) -> list[Tuple[BenchmarkKey, Any]]:
    """Create timing figures, one per (format, operation, operating-point label).

    ``parsed`` is nested ``[bench_type][fmt][label] -> [{name, threads, mean,
    stddev}]``. Within each chart, implementations are drawn as horizontal bar
    groups with one bar per threading mode (single-threaded vs all-cores) so the
    two configurations are directly comparable. Returns ``(key, Figure)`` tuples
    keyed by ``(ImageFormat, BenchmarkType, label)``; the caller saves and
    closes the figures.
    """

    plots: list[Tuple[BenchmarkKey, Any]] = []

    for bench_type, codecs in parsed.items():
        if not codecs:
            continue

        for fmt in sorted(codecs.keys()):
            qualities = codecs[fmt]
            for quality in sorted(qualities.keys()):
                entries = qualities[quality]
                if not entries:
                    continue

                # Group entries by implementation, indexed by thread count.
                by_impl: Dict[str, Dict[int, Dict[str, Any]]] = {}
                for e in entries:
                    by_impl.setdefault(e["name"], {})[e["threads"]] = e

                def _rep_mean(impl_entries: Dict[int, Dict[str, Any]]) -> float:
                    # Prefer the all-cores number for ordering; else any present.
                    if 0 in impl_entries:
                        return impl_entries[0]["mean"]
                    return min(v["mean"] for v in impl_entries.values())

                impl_names = sorted(by_impl.keys(), key=lambda n: _rep_mean(by_impl[n]))
                # Thread modes actually present, in stable single->all order.
                present_threads = [
                    t for t in (1, 0) if any(t in by_impl[n] for n in impl_names)
                ]
                if not present_threads:
                    continue

                n_groups = len(impl_names)
                n_series = len(present_threads)
                bar_h = 0.8 / n_series
                y_base = np.arange(n_groups)

                fig, ax = plt.subplots(
                    figsize=(10, 0.6 * max(4, n_groups) + 1),
                    constrained_layout=True,
                )
                fig.suptitle(
                    f"{bench_type.capitalize()} — {fmt.upper()} — {quality}",
                    fontsize=14,
                )

                for s_idx, t in enumerate(present_threads):
                    means = [by_impl[n].get(t, {}).get("mean", 0.0) for n in impl_names]
                    stds = [
                        by_impl[n].get(t, {}).get("stddev", 0.0) for n in impl_names
                    ]
                    offsets = y_base + (s_idx - (n_series - 1) / 2) * bar_h
                    label, color = _THREAD_STYLES.get(t, (f"threads={t}", None))
                    ax.barh(
                        offsets,
                        means,
                        height=bar_h,
                        xerr=stds,
                        align="center",
                        alpha=0.85,
                        capsize=3,
                        label=label,
                        color=color,
                    )
                    for off, v in zip(offsets, means):
                        if v > 0:
                            ax.text(v, off, f" {v:.2f}", va="center", fontsize=7)

                ax.set_yticks(y_base)
                ax.set_yticklabels(impl_names)
                ax.invert_yaxis()  # labels read top-to-bottom
                ax.set_xlabel("Time (ms)")
                ax.set_xlim(left=0)
                if n_series > 1:
                    ax.legend(loc="lower right", fontsize="small")

                key = (
                    ImageFormat(fmt),
                    BenchmarkType(bench_type.lower()),
                    quality,
                )
                plots.append((key, fig))

    return plots


def create_quality_vs_bpp_plots(
    metrics: list[BenchmarkMetrics],
) -> list[Tuple[str, Any]]:
    """Create scatter plots of SSIMULACRA2 score vs bits-per-pixel per format.

    Groups by format, each point is one image encoded by one implementation.
    Shows which implementations achieve better quality at lower file sizes.

    Returns a list of tuples (filename, Figure).
    """

    plots: list[Tuple[str, Any]] = []

    encode_metrics = _filter_valid_encode_metrics(metrics)

    if not encode_metrics:
        return plots

    formats = _group_by(encode_metrics, lambda m: m["format"])

    # Create one plot per format
    for fmt, fmt_metrics in sorted(formats.items()):
        impls = _group_by(fmt_metrics, lambda m: m["impl"])

        fig, ax = plt.subplots(figsize=(10, 6), constrained_layout=True)

        # Use a colormap
        colors = plt.cm.tab10.colors
        for idx, (impl_name, impl_metrics) in enumerate(sorted(impls.items())):
            bpps = [m["bpp"] for m in impl_metrics]
            scores = [m["ssimulacra2"] for m in impl_metrics]
            color = colors[idx % len(colors)]
            ax.scatter(bpps, scores, label=impl_name, alpha=0.7, s=50, c=[color])

        ax.set_xlabel("Bits per Pixel (bpp)")
        ax.set_ylabel("SSIMULACRA2 Score")
        ax.set_title(f"{fmt.upper()} - Quality vs Compression Efficiency")
        ax.legend(loc="lower right", fontsize="small")
        ax.grid(True, alpha=0.3)
        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)

        # Higher score and lower bpp is better (top-left corner)
        ax.annotate(
            "← Better",
            xy=(0.02, 0.98),
            xycoords="axes fraction",
            fontsize=9,
            color="green",
            ha="left",
            va="top",
        )

        filename = f"quality_vs_bpp_{fmt}.png"
        plots.append((filename, fig))

    return plots


def create_format_comparison_plot(
    metrics: list[BenchmarkMetrics],
) -> Optional[Tuple[str, Any]]:
    """Create grouped bar chart comparing formats.

    Shows average SSIMULACRA2 score and average bpp per format,
    aggregated across all implementations and images.

    Returns a tuple (filename, Figure) or None if no valid data.
    """

    encode_metrics = _filter_valid_encode_metrics(metrics)

    if not encode_metrics:
        return None

    # Aggregate by format
    format_stats: Dict[str, Dict[str, list[float]]] = {}
    for m in encode_metrics:
        fmt = m["format"]
        if fmt not in format_stats:
            format_stats[fmt] = {"bpp": [], "score": []}
        format_stats[fmt]["bpp"].append(m["bpp"])
        format_stats[fmt]["score"].append(m["ssimulacra2"])

    # Calculate averages
    formats_sorted = sorted(format_stats.keys())
    avg_bpp = [
        sum(format_stats[f]["bpp"]) / len(format_stats[f]["bpp"])
        for f in formats_sorted
    ]
    avg_score = [
        sum(format_stats[f]["score"]) / len(format_stats[f]["score"])
        for f in formats_sorted
    ]

    # Create grouped bar chart
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), constrained_layout=True)

    x = np.arange(len(formats_sorted))
    width = 0.6

    # Plot average bpp
    axes[0].bar(x, avg_bpp, width, color="steelblue", alpha=0.8)
    axes[0].set_xlabel("Format")
    axes[0].set_ylabel("Average Bits per Pixel")
    axes[0].set_title("Compression Efficiency (lower is better)")
    axes[0].set_xticks(x)
    axes[0].set_xticklabels([f.upper() for f in formats_sorted])
    axes[0].set_ylim(bottom=0)
    for i, v in enumerate(avg_bpp):
        axes[0].text(i, v + 0.05, f"{v:.2f}", ha="center", fontsize=9)

    # Plot average score
    axes[1].bar(x, avg_score, width, color="forestgreen", alpha=0.8)
    axes[1].set_xlabel("Format")
    axes[1].set_ylabel("Average SSIMULACRA2 Score")
    axes[1].set_title("Visual Quality (higher is better)")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels([f.upper() for f in formats_sorted])
    axes[1].set_ylim(bottom=0)
    for i, v in enumerate(avg_score):
        axes[1].text(i, v + 0.5, f"{v:.1f}", ha="center", fontsize=9)

    fig.suptitle("Format Comparison", fontsize=14)

    return ("format_comparison.png", fig)


def create_implementation_comparison_plots(
    metrics: list[BenchmarkMetrics],
) -> list[Tuple[str, Any]]:
    """Box plots comparing implementations within each format.

    Shows distribution of quality scores and bpp per implementation,
    helping identify which implementations are most consistent.

    Returns a list of tuples (filename, Figure).
    """

    plots: list[Tuple[str, Any]] = []

    encode_metrics = _filter_valid_encode_metrics(metrics)

    if not encode_metrics:
        return plots

    formats = _group_by(encode_metrics, lambda m: m["format"])

    # Create one plot per format
    for fmt, fmt_metrics in sorted(formats.items()):
        impls = _group_by(fmt_metrics, lambda m: m["impl"])

        if len(impls) < 2:
            continue  # Skip if only one implementation

        impl_names = sorted(impls.keys())

        fig, axes = plt.subplots(1, 2, figsize=(12, 5), constrained_layout=True)

        # Prepare data for box plots
        bpp_data = [sorted([m["bpp"] for m in impls[impl]]) for impl in impl_names]
        score_data = [
            sorted([m["ssimulacra2"] for m in impls[impl]]) for impl in impl_names
        ]

        # Box plot for bpp
        bp1 = axes[0].boxplot(bpp_data, tick_labels=impl_names, patch_artist=True)
        axes[0].set_ylabel("Bits per Pixel")
        axes[0].set_title("Compression Efficiency Distribution")
        axes[0].tick_params(axis="x", rotation=45)
        axes[0].set_ylim(bottom=0)
        for patch in bp1["boxes"]:
            patch.set_facecolor("steelblue")
            patch.set_alpha(0.7)

        # Box plot for score
        bp2 = axes[1].boxplot(score_data, tick_labels=impl_names, patch_artist=True)
        axes[1].set_ylabel("SSIMULACRA2 Score")
        axes[1].set_title("Visual Quality Distribution")
        axes[1].tick_params(axis="x", rotation=45)
        axes[1].set_ylim(bottom=0)
        for patch in bp2["boxes"]:
            patch.set_facecolor("forestgreen")
            patch.set_alpha(0.7)

        fig.suptitle(f"{fmt.upper()} - Implementation Comparison", fontsize=14)

        filename = f"impl_comparison_{fmt}.png"
        plots.append((filename, fig))

    return plots
