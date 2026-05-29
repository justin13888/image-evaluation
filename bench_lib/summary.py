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

            # Parse and aggregate results
            aggregated_results = {"encode": {}, "decode": {}}

            for result in data.get("results", []):
                name = result.get("command", "unknown")
                # Format: "impl_name (fmt, type, input)"
                try:
                    # Extract info from name
                    if "(" in name and ")" in name:
                        base_name = name.split(" (")[0]
                        meta = name.split(" (")[1].rstrip(")").split(", ")
                        if len(meta) >= 2:
                            fmt = meta[0]
                            bench_type = meta[1]

                            if bench_type in aggregated_results:
                                if fmt not in aggregated_results[bench_type]:
                                    aggregated_results[bench_type][fmt] = {}

                                if base_name not in aggregated_results[bench_type][fmt]:
                                    aggregated_results[bench_type][fmt][base_name] = {
                                        "mean_sum": 0.0,
                                        "mean_sq_sum": 0.0,
                                        "var_sum": 0.0,
                                        "count": 0,
                                    }

                                mean_ms = (result.get("mean") or 0) * 1000
                                stddev_ms = (result.get("stddev") or 0) * 1000

                                agg = aggregated_results[bench_type][fmt][base_name]
                                agg["mean_sum"] += mean_ms
                                agg["mean_sq_sum"] += mean_ms**2
                                agg["var_sum"] += stddev_ms**2
                                agg["count"] += 1
                except Exception:
                    continue

            # Convert to expected format for plotting
            parsed_results = {}
            for b_type, formats in aggregated_results.items():
                parsed_results[b_type] = {}
                for fmt, impls in formats.items():
                    if fmt == "null":
                        continue  # null implementations have no format, skip
                    parsed_results[b_type][fmt] = []
                    for impl_name, agg in impls.items():
                        n = agg["count"]
                        grand_mean = agg["mean_sum"] / n
                        # Pooled within-group variance (average of per-image variances)
                        within_var = agg["var_sum"] / n
                        # Between-group variance (variance of per-image means)
                        between_var = agg["mean_sq_sum"] / n - grand_mean**2
                        combined_stddev = (within_var + max(between_var, 0.0)) ** 0.5
                        parsed_results[b_type][fmt].append(
                            {
                                "name": impl_name,
                                "mean": grand_mean,
                                "stddev": combined_stddev,
                            }
                        )

            # Generate plots and export them individually
            plot_files: list[
                Tuple[str, str, str]
            ] = []  # (bench_type, bench_format, filename)
            plots = create_plots_from_parsed_results(parsed_results)

            for key, fig in plots:
                filename = filename_from_key(key) + ".png"
                filepath = os.path.join(result_dir, filename)
                fig.savefig(filepath)
                plt.close(fig)
                bench_type_str = key[1].value
                bench_format_str = key[0].value
                plot_files.append((bench_type_str, bench_format_str, filename))

            buffer.write("## Summary\n\n")
            buffer.write(
                "| Implementation | Lang | Mean (ms) | Std Dev (ms) | 95% CI (ms) | Min (ms) | Max (ms) |\n"
            )
            buffer.write(
                "|---------------|------|-----------|--------------|-------------|----------|----------|\n"
            )

            for result in data.get("results", []):
                name = result.get("command", "unknown")
                impl = find_implementation_by_name(name.split(" (")[0])
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
                    f"| {name} | {lang} | {mean:.2f} | {stddev:.2f} | {ci_str} | {min_time:.2f} | {max_time:.2f} |\n"
                )

            buffer.write("\n## Detailed Results\n")

            if plot_files:
                for bench_type, bench_format, filename in plot_files:
                    buffer.write(
                        f"\n### {bench_type.capitalize()} {bench_format.upper()}\n\n![{bench_type.capitalize()} {bench_format.lower()} results]({filename})\n"
                    )
            else:
                buffer.write("No plots generated.\n")

        # Process metrics if available
        if metrics is not None:
            # Sort metrics: encode vs decode, format, quality, input file
            # We want ENCODE to come before DECODE.
            def type_priority(t: str) -> int:
                return 0 if t == "encode" else 1

            metrics.sort(
                key=lambda m: (
                    type_priority(m["type"]),
                    m["format"],
                    m["quality"],
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
                    "Higher SSIMULACRA2 score and lower bpp (top-left) indicates better compression efficiency.\n\n"
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
                    "Aggregate comparison of formats across all implementations and images.\n\n"
                )
                buffer.write(f"![Format Comparison]({filename})\n\n")

            # 3. Implementation comparison plots
            impl_comparison_plots = create_implementation_comparison_plots(metrics)
            if impl_comparison_plots:
                buffer.write("\n### Implementation Comparison\n\n")
                buffer.write(
                    "Box plots showing distribution of quality and compression across images per implementation.\n\n"
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
                "| Implementation | Lang | Build | Quality | Input File | File Size | bpp | SSIMULACRA 2 | Status |\n"
            )
            buffer.write(
                "|----------------|------|-------|---------|------------|-----------|-----|--------------|--------|\n"
            )
            for m in metrics:
                impl_name = m["impl"]
                impl_lang = m.get("lang", "?")
                impl_build = m.get("build", "?")
                quality = m["quality"]
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
                    f"| {impl_name} | {impl_lang} | {impl_build} | {quality} | {input_file} | {filesize} | {bpp} | {ssim_score} | {status} |\n"
                )

        # Write buffer to file
        with open(summary_path, "w") as f:
            f.write(buffer.getvalue())
        print(f"\n✓ Summary written to {summary_path}")
    except Exception as e:
        print(f"An error occurred; file ({summary_path}) was not written: {e}")
    finally:
        buffer.close()
