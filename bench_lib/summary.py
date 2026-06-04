"""Summary report generation from benchmark results."""

import datetime
import io
import json
import os
from typing import Optional, Tuple

import humanize
import matplotlib.pyplot as plt

from bench_lib.models import (
    BenchmarkMetrics,
    filename_from_key,
    find_implementation_by_name,
)
from bench_lib.plotting import (
    create_format_comparison_plot,
    create_implementation_comparison_plots,
    create_plots_from_parsed_results,
    create_quality_vs_bpp_plots,
)


def _parse_command_name(name: str) -> Optional[dict]:
    """Parse a hyperfine command name produced by ``BenchmarkTask.name()``.

    Expected form: ``"impl-name (fmt, type, label, tN, basename)"`` where
    ``label`` is the operating-point token. The basename is kept whole even if
    it contains ", " (it is the last field). Returns ``None`` for names not in
    this decorated 5-field form.
    """
    if " (" not in name or not name.endswith(")"):
        return None
    base_name, _, rest = name.partition(" (")
    rest = rest[:-1]  # strip trailing ")"
    meta = rest.split(", ", 4)  # maxsplit keeps a comma-containing basename intact
    if len(meta) < 5:
        return None
    fmt, bench_type, label, threads_tok, basename = meta
    try:
        threads = int(threads_tok.lstrip("t"))
    except ValueError:
        return None
    return {
        "impl": base_name,
        "format": fmt,
        "type": bench_type,
        "label": label,
        "threads": threads,
        "basename": basename,
    }


def generate_summary(
    result_dir: str,
    raw_json_path: Optional[str],
    metrics: Optional[list[BenchmarkMetrics]],
):
    """Generate summary.md from hyperfine results."""
    summary_path = f"{result_dir}/summary.md"
    try:
        # Create an in-memory buffer
        buffer = io.StringIO()

        # Perform all your write operations to the buffer
        buffer.write("# Benchmark Results\n\n")
        buffer.write(
            f"Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n\n"
        )

        # Process raw JSON data if available
        if raw_json_path is not None:
            try:
                with open(raw_json_path) as f:
                    data = json.load(f)
            except Exception as e:
                print(f"Warning: Could not generate summary: {e}")
                return

            # Parse and aggregate results across images, keyed by every swept
            # dimension. The key includes both the operating-point label and the
            # thread mode — otherwise distinct operating points and single/parallel
            # timings would be averaged into one meaningless bar.
            # aggregated[bench_type][fmt][label][impl][threads] -> running stats
            aggregated_results: dict = {"encode": {}, "decode": {}}

            for result in data.get("results", []):
                parsed = _parse_command_name(result.get("command", ""))
                if parsed is None:
                    continue
                bench_type = parsed["type"]
                fmt = parsed["format"]
                if bench_type not in aggregated_results or fmt == "null":
                    continue  # null implementations have no format, skip

                cell = (
                    aggregated_results[bench_type]
                    .setdefault(fmt, {})
                    .setdefault(parsed["label"], {})
                    .setdefault(parsed["impl"], {})
                    .setdefault(
                        parsed["threads"],
                        {
                            "mean_sum": 0.0,
                            "mean_sq_sum": 0.0,
                            "var_sum": 0.0,
                            "count": 0,
                        },
                    )
                )
                mean_ms = (result.get("mean") or 0) * 1000
                stddev_ms = (result.get("stddev") or 0) * 1000
                cell["mean_sum"] += mean_ms
                cell["mean_sq_sum"] += mean_ms**2
                cell["var_sum"] += stddev_ms**2
                cell["count"] += 1

            def _finalize(cell: dict) -> Tuple[float, float]:
                n = cell["count"]
                grand_mean = cell["mean_sum"] / n
                # Pooled within-group variance (average of per-image variances)
                within_var = cell["var_sum"] / n
                # Between-group variance (variance of per-image means)
                between_var = cell["mean_sq_sum"] / n - grand_mean**2
                return grand_mean, (within_var + max(between_var, 0.0)) ** 0.5

            # Convert to plotting format:
            # parsed[bench_type][fmt][quality] -> [{name, threads, mean, stddev}]
            parsed_results: dict = {}
            for b_type, fmts in aggregated_results.items():
                parsed_results[b_type] = {}
                for fmt, qualities in fmts.items():
                    parsed_results[b_type][fmt] = {}
                    for quality, impls in qualities.items():
                        entries = []
                        for impl_name, by_threads in impls.items():
                            for threads, cell in by_threads.items():
                                mean, stddev = _finalize(cell)
                                entries.append(
                                    {
                                        "name": impl_name,
                                        "threads": threads,
                                        "mean": mean,
                                        "stddev": stddev,
                                    }
                                )
                        parsed_results[b_type][fmt][quality] = entries

            # Generate plots and export them individually
            plot_files: list[
                Tuple[str, str, str, str]
            ] = []  # (bench_type, bench_format, label, filename)
            plots = create_plots_from_parsed_results(parsed_results)

            for key, fig in plots:
                filename = filename_from_key(key) + ".png"
                filepath = os.path.join(result_dir, filename)
                fig.savefig(filepath)
                plt.close(fig)
                # key = (ImageFormat, BenchmarkType, label-string)
                plot_files.append((key[1].value, key[0].value, key[2], filename))

            buffer.write("## Summary\n\n")
            buffer.write(
                "Each row is one implementation aggregated over the dataset, for a "
                "given operating point and threading mode (single = `--threads 1`, "
                "all = `--threads 0`).\n\n"
            )
            buffer.write(
                "| Implementation | Op | Threads | Lang | Mean (ms) | Std Dev (ms) | 95% CI (ms) | Min (ms) | Max (ms) |\n"
            )
            buffer.write(
                "|----------------|----|---------|------|-----------|--------------|-------------|----------|----------|\n"
            )

            for result in data.get("results", []):
                name = result.get("command", "unknown")
                parsed = _parse_command_name(name)
                impl_name = parsed["impl"] if parsed else name.split(" (")[0]
                quality = parsed["label"] if parsed else "?"
                threads_label = (
                    ("single" if parsed["threads"] == 1 else "all") if parsed else "?"
                )
                impl = find_implementation_by_name(impl_name)
                lang = impl.lang if impl else "?"
                mean = (result.get("mean") or 0) * 1000  # Convert to ms
                stddev = (result.get("stddev") or 0) * 1000
                min_time = (result.get("min") or 0) * 1000
                max_time = (result.get("max") or 0) * 1000

                # Calculate 95% confidence interval
                times = result.get("times", [])
                n = len(times) if times else 10
                stderr = stddev / (n**0.5)
                ci_margin = 1.96 * stderr
                ci_lower = mean - ci_margin
                ci_upper = mean + ci_margin
                ci_str = f"{ci_lower:.2f}–{ci_upper:.2f}"

                buffer.write(
                    f"| {impl_name} | {quality} | {threads_label} | {lang} | {mean:.2f} | {stddev:.2f} | {ci_str} | {min_time:.2f} | {max_time:.2f} |\n"
                )

            buffer.write("\n## Detailed Results\n")
            buffer.write(
                "\nOne chart per (format, operation, operating point); bars are "
                "grouped per implementation showing single-threaded vs all-cores.\n"
            )

            if plot_files:
                for bench_type, bench_format, quality, filename in sorted(
                    plot_files,
                    key=lambda p: (p[0], p[1], p[2], p[3]),
                ):
                    buffer.write(
                        f"\n### {bench_type.capitalize()} {bench_format.upper()} — {quality}\n\n"
                        f"![{bench_type} {bench_format} {quality} results]({filename})\n"
                    )
            else:
                buffer.write("No plots generated.\n")

        # Process metrics if available
        if metrics is not None:
            # Sort metrics: encode vs decode, format, operating point, input file.
            # We want ENCODE to come before DECODE.
            def type_priority(t: str) -> int:
                return 0 if t == "encode" else 1

            metrics.sort(
                key=lambda m: (
                    type_priority(m["type"]),
                    m["format"],
                    m["label"],
                    os.path.basename(m["input_path"]),
                )
            )

            # Generate new visualization plots
            buffer.write("\n## Compression Analysis\n")

            # 1. Quality vs BPP plots
            quality_bpp_plots = create_quality_vs_bpp_plots(metrics)
            if quality_bpp_plots:
                buffer.write("\n### Quality vs Compression Efficiency\n\n")
                buffer.write(
                    "Each point is one image encoded at one quality tier; the per-implementation "
                    "spread traces its quality-vs-bpp curve. Higher SSIMULACRA2 score and lower "
                    "bpp (top-left) indicates better compression efficiency.\n\n"
                )
                for filename, fig in quality_bpp_plots:
                    filepath = os.path.join(result_dir, filename)
                    fig.savefig(filepath, dpi=150)
                    plt.close(fig)
                    fmt_name = (
                        filename.replace("quality_vs_bpp_", "")
                        .replace(".png", "")
                        .upper()
                    )
                    buffer.write(
                        f"#### {fmt_name}\n\n![Quality vs BPP for {fmt_name}]({filename})\n\n"
                    )

            # 2. Format comparison plot
            format_comparison = create_format_comparison_plot(metrics)
            if format_comparison:
                filename, fig = format_comparison
                filepath = os.path.join(result_dir, filename)
                fig.savefig(filepath, dpi=150)
                plt.close(fig)
                buffer.write("\n### Format Comparison\n\n")
                buffer.write(
                    "Aggregate comparison of formats across all implementations, images, "
                    "and quality tiers.\n\n"
                )
                buffer.write(f"![Format Comparison]({filename})\n\n")

            # 3. Implementation comparison plots
            impl_comparison_plots = create_implementation_comparison_plots(metrics)
            if impl_comparison_plots:
                buffer.write("\n### Implementation Comparison\n\n")
                buffer.write(
                    "Box plots showing distribution of quality and compression across images "
                    "and quality tiers per implementation.\n\n"
                )
                for filename, fig in impl_comparison_plots:
                    filepath = os.path.join(result_dir, filename)
                    fig.savefig(filepath, dpi=150)
                    plt.close(fig)
                    fmt_name = (
                        filename.replace("impl_comparison_", "")
                        .replace(".png", "")
                        .upper()
                    )
                    buffer.write(
                        f"#### {fmt_name}\n\n![Implementation comparison for {fmt_name}]({filename})\n\n"
                    )

            # Metrics table
            buffer.write("\n## Metrics\n\n")
            buffer.write(
                "| Implementation | Lang | Build | Op | Params | Input File | File Size | bpp | SSIMULACRA 2 | Status |\n"
            )
            buffer.write(
                "|----------------|------|-------|----|--------|------------|-----------|-----|--------------|--------|\n"
            )
            for m in metrics:
                impl_name = m["impl"]
                impl_lang = m.get("lang", "?")
                impl_build = m.get("build", "?")
                quality = m["label"]
                params = m.get("params", "") or "—"
                input_file = os.path.basename(m["input_path"])
                filesize = (
                    humanize.naturalsize(m["filesize"], binary=True)
                    if m["filesize"] > 0
                    else "N/A"
                )
                bpp = f"{m['bpp']:.3f}" if m["bpp"] > 0 else "N/A"
                ssim_score = m["ssimulacra2"]
                status = "✗ " + m["error"][:30] + "..." if m.get("error") else "✓"
                buffer.write(
                    f"| {impl_name} | {impl_lang} | {impl_build} | {quality} | {params} | {input_file} | {filesize} | {bpp} | {ssim_score} | {status} |\n"
                )

        # Write buffer to file
        with open(summary_path, "w") as f:
            f.write(buffer.getvalue())
        print(f"\n✓ Summary written to {summary_path}")
    except Exception as e:
        print(f"An error occurred; file ({summary_path}) was not written: {e}")
    finally:
        buffer.close()
