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
    compute_bd_rate_table,
    create_plots_from_parsed_results,
    lossless_efficiency,
    pareto_front_encoders,
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

            # Compression analysis. The rate-distortion curves are interactive in
            # report.html (drawn client-side from the embedded raw metrics); the
            # summary.md keeps the numeric tables that are awkward to read off a
            # chart.
            buffer.write("\n## Compression Analysis\n")
            buffer.write(
                "\nInteractive rate-distortion curves — per format and a combined "
                "cross-format Pareto view — are in "
                "[`../report.html`](../report.html).\n"
            )

            # BD-rate table (Bjøntegaard delta-rate vs each format's reference)
            bd_table = compute_bd_rate_table(metrics)
            if bd_table:
                buffer.write("\n### BD-rate (vs reference encoder)\n\n")
                buffer.write(
                    "Bjøntegaard delta-rate over SSIMULACRA2, per image then averaged. "
                    "Negative = fewer bits for equal quality than the format's reference "
                    "encoder (better); `N/A` = non-overlapping quality ranges.\n\n"
                )
                buffer.write("| Format | Implementation | BD-rate vs ref |\n")
                buffer.write("|--------|----------------|----------------|\n")
                for fmt in sorted(bd_table):
                    for impl in sorted(bd_table[fmt]):
                        bd = bd_table[fmt][impl]
                        bd_str = f"{bd:+.1f}%" if bd is not None else "N/A"
                        buffer.write(f"| {fmt.upper()} | {impl} | {bd_str} |\n")
                buffer.write("\n")

            # Pareto front: the best (non-dominated) encoder(s) of each format —
            # the curves overlaid on the combined chart in report.html.
            pareto = pareto_front_encoders(metrics)
            if pareto:
                buffer.write("\n### Best encoders per format (Pareto front)\n\n")
                buffer.write(
                    "Encoders that are not dominated on the bpp-vs-SSIMULACRA2 "
                    "tradeoff (mean curve across the dataset).\n\n"
                )
                buffer.write("| Format | Pareto-optimal encoders |\n")
                buffer.write("|--------|-------------------------|\n")
                for fmt in sorted(pareto):
                    buffer.write(f"| {fmt.upper()} | {', '.join(pareto[fmt])} |\n")
                buffer.write("\n")

            # Lossless compression efficiency: lossless encoders have no
            # rate-distortion tradeoff, so they are compared by file size alone
            # (issue #26). Ratio is against the 24 bpp RGB8 source; higher = better.
            lossless = lossless_efficiency(metrics)
            if lossless:
                buffer.write("\n### Lossless compression efficiency\n\n")
                buffer.write(
                    "Lossless encoders produce a pixel-identical image, so they "
                    "differ only in size. Best (smallest) bits-per-pixel across the "
                    "effort sweep, averaged over the dataset; ratio vs the 24 bpp "
                    "RGB8 source (higher is better).\n\n"
                )
                buffer.write(
                    "| Format | Implementation | Best bpp | Ratio | Best setting |\n"
                )
                buffer.write(
                    "|--------|----------------|----------|-------|--------------|\n"
                )
                for impl in sorted(
                    lossless,
                    key=lambda i: (lossless[i]["format"], lossless[i]["best_bpp"]),
                ):
                    d = lossless[impl]
                    ratio = f"{d['ratio']:.2f}×" if d["ratio"] is not None else "N/A"
                    buffer.write(
                        f"| {d['format'].upper()} | {impl} | {d['best_bpp']:.3f} | "
                        f"{ratio} | {d['best_label']} |\n"
                    )
                buffer.write("\n")

            # Metrics table
            buffer.write("\n## Metrics\n\n")
            buffer.write(
                "| Implementation | Lang | Build | Op | Params | Input File | File Size | bpp | SSIMULACRA 2 | PSNR (dB) | SSIM | Butteraugli | Encode (s) | Status |\n"
            )
            buffer.write(
                "|----------------|------|-------|----|--------|------------|-----------|-----|--------------|-----------|------|-------------|------------|--------|\n"
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
                psnr_val = m.get("psnr")
                psnr_str = f"{psnr_val:.2f}" if psnr_val is not None else "∞/NA"
                ssim_val = m.get("ssim")
                ssim_str = f"{ssim_val:.4f}" if ssim_val is not None else "NA"
                ba_val = m.get("butteraugli")
                ba_str = f"{ba_val:.3f}" if ba_val is not None else "NA"
                enc_val = m.get("encode_time_s")
                enc_str = f"{enc_val:.2f}" if enc_val else "N/A"
                status = "✗ " + m["error"][:30] + "..." if m.get("error") else "✓"
                buffer.write(
                    f"| {impl_name} | {impl_lang} | {impl_build} | {quality} | {params} | {input_file} | {filesize} | {bpp} | {ssim_score} | {psnr_str} | {ssim_str} | {ba_str} | {enc_str} | {status} |\n"
                )

        # Write buffer to file
        with open(summary_path, "w") as f:
            f.write(buffer.getvalue())
        print(f"\n✓ Summary written to {summary_path}")
    except Exception as e:
        print(f"An error occurred; file ({summary_path}) was not written: {e}")
    finally:
        buffer.close()
