"""Matplotlib figure creation for the performance suite, plus rate-distortion
analysis helpers (BD-rate, Pareto front) for the quality suite.

The quality suite no longer renders charts to PNG: report.html embeds the raw
metrics and draws interactive SVG rate-distortion curves client-side (see
``bench_lib/assets/report.js``). The numeric summaries that are awkward to
recompute in the browser — BD-rate (numpy polyfit/polyint) and the Pareto front —
are computed here and embedded as small JSON blobs.
"""

import os

import matplotlib.pyplot as plt
import numpy as np
from typing import Callable, Dict, Any, List, Optional, Tuple

from bench_lib.models import (
    REFERENCE_ENCODERS,
    BenchmarkKey,
    BenchmarkMetrics,
    BenchmarkType,
    ImageFormat,
    decode_approx_expected,
    quality_label,
    schema_for,
)

# Threading modes -> (legend label, bar colour). 1 = single-threaded, 0 = all cores.
_THREAD_STYLES = {
    1: ("single-threaded", "#4C72B0"),
    0: ("all-cores", "#DD8452"),
}


def _filter_valid_encode_metrics(
    metrics: list[BenchmarkMetrics],
) -> list[BenchmarkMetrics]:
    """Filter to lossy encode-only metrics with valid bpp, score, and no errors.

    Lossless rows (issue #26) are excluded: they have no rate-distortion tradeoff,
    so they do not belong in BD-rate or the rate-distortion Pareto front — they go
    to the lossless compression-efficiency view instead."""
    return [
        m
        for m in metrics
        if m["type"] == "encode"
        and not m.get("lossless")
        and m["bpp"] > 0
        and m["ssimulacra2"] > 0
        and not m.get("error")
    ]


def _finite_encode_metrics(
    metrics: list[BenchmarkMetrics],
) -> list[BenchmarkMetrics]:
    """Encode metrics with positive bpp and a *finite* SSIMULACRA2, with no error.

    Unlike :func:`_filter_valid_encode_metrics` this keeps negative scores: a
    SSIMULACRA2 below zero is the legitimate low-quality tail of a rate-distortion
    curve, not an error, and dropping it truncates the curve. Used for the
    rate-distortion views (the Pareto front here, and the client-side charts,
    which apply the same rule). Lossless rows (issue #26) are excluded — they have
    no distortion axis and are shown in the lossless efficiency view instead."""
    out: list[BenchmarkMetrics] = []
    for m in metrics:
        if m["type"] != "encode" or m.get("error") or m.get("lossless"):
            continue
        s = m["ssimulacra2"]
        if m["bpp"] > 0 and s is not None and np.isfinite(s):
            out.append(m)
    return out


def _group_by(items: list, key_fn: Callable) -> Dict:
    """Group a list of items by a key function into a dict of lists."""
    groups: Dict = {}
    for item in items:
        k = key_fn(item)
        if k not in groups:
            groups[k] = []
        groups[k].append(item)
    return groups


def pareto_front_encoders(
    metrics: list[BenchmarkMetrics],
) -> Dict[str, list[str]]:
    """Per format, the encoders on the Pareto front of (bpp down, SSIMULACRA2 up).

    Each encoder's curve is first aggregated to one mean point per quality-sweep
    step (bpp and score averaged across images). An encoder is on the front if it
    owns at least one point that no *other* encoder dominates (another point with
    bpp <= and score >=, at least one strict). Returns ``{format: [impl, ...]}``
    sorted by name. This mirrors the report's combined cross-format chart so
    report.html and summary.md agree on the "best encoders of each format"."""
    result: Dict[str, list[str]] = {}
    by_fmt = _group_by(_finite_encode_metrics(metrics), lambda m: m["format"])
    for fmt, fmt_metrics in by_fmt.items():
        # (impl, label) -> mean (bpp, score) across images: one point per sweep step.
        agg: Dict[Tuple[str, str], list] = {}
        for m in fmt_metrics:
            agg.setdefault((m["impl"], m["label"]), []).append(
                (m["bpp"], m["ssimulacra2"])
            )
        points = []  # (impl, mean_bpp, mean_score)
        for (impl, _label), pts in agg.items():
            n = len(pts)
            points.append(
                (impl, sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)
            )
        front: set = set()
        for impl, bpp, score in points:
            dominated = any(
                o_impl != impl
                and o_bpp <= bpp
                and o_score >= score
                and (o_bpp < bpp or o_score > score)
                for o_impl, o_bpp, o_score in points
            )
            if not dominated:
                front.add(impl)
        if front:
            result[fmt] = sorted(front)
    return result


# Uncompressed PPM source is 8-bit RGB, so 24 bits per pixel. Used to express a
# lossless encoder's bpp as a compression ratio (24 / bpp, higher is better).
_SOURCE_BPP = 24.0


def lossless_efficiency(
    metrics: list[BenchmarkMetrics],
) -> Dict[str, Dict[str, Any]]:
    """Per lossless encoder, its compression efficiency across the dataset (issue
    #26). Each encoder's lossless rows are aggregated to one mean-bpp point per
    effort step (averaged across images); the best (smallest) such bpp is the
    encoder's headline number, with its compression ratio against the 24 bpp RGB8
    source. ``points`` is ordered low-effort -> high-effort using the schema's
    declared sweep, so the size-vs-effort curve reads left-to-right.

    Returns ``{impl: {format, axis, best_bpp, best_label, ratio, points}}`` where
    each point is ``{label, value, bpp, time_s, time_rigorous_s, time_stddev_s,
    runs}`` — the rigorous fields set only on the endpoints the timing overlay
    re-measured. Encoders with no valid lossless row are omitted."""
    rows = [
        m
        for m in metrics
        if m.get("lossless")
        and m["type"] == "encode"
        and not m.get("error")
        and m["bpp"] > 0
    ]
    result: Dict[str, Dict[str, Any]] = {}
    for impl, impl_rows in _group_by(rows, lambda m: m["impl"]).items():
        # label -> mean bpp across images (one point per effort step).
        agg: Dict[str, Dict[str, Any]] = {}
        for m in impl_rows:
            a = agg.setdefault(
                m["label"],
                {
                    "value": m["quality_value"],
                    "sum": 0.0,
                    "n": 0,
                    "t": 0.0,
                    "rt_sum": 0.0,
                    "rt_sq": 0.0,
                    "rt_var": 0.0,
                    "rt_n": 0,
                    "runs": 0,
                },
            )
            a["sum"] += m["bpp"]
            a["t"] += m.get("time_s") or 0.0
            a["n"] += 1
            # Fold the rigorous-timing overlay's isolated endpoint measurements
            # (issue #26): only rows the overlay actually re-timed carry these, and
            # only repeated runs (>1) count as statistically significant.
            rt = m.get("time_rigorous_s")
            runs = m.get("time_runs") or 0
            if rt is not None and runs > 1:
                sd = m.get("time_rigorous_stddev_s") or 0.0
                a["rt_sum"] += rt
                a["rt_sq"] += rt * rt
                a["rt_var"] += sd * sd
                a["rt_n"] += 1
                a["runs"] = runs if a["runs"] == 0 else min(a["runs"], runs)
        # Canonical low->high effort order from the schema's sweep; any labels not
        # in the sweep (e.g. the single "lossless" point) keep insertion order.
        schema = schema_for(impl)
        ordered = (
            [quality_label(schema.quality_axis, v) for v in schema.quality_sweep]
            if schema.quality_axis
            else []
        )
        labels = [lbl for lbl in ordered if lbl in agg] + [
            lbl for lbl in agg if lbl not in ordered
        ]

        def _rig(a):
            """Mean / pooled-σ / run-count of the rigorous endpoint timing across
            images for one effort step (None when no image was rigorously timed)."""
            k = a["rt_n"]
            if k == 0:
                return None, None, 0
            mean = a["rt_sum"] / k
            within = a["rt_var"] / k
            between = max(a["rt_sq"] / k - mean * mean, 0.0)
            return mean, (within + between) ** 0.5, a["runs"]

        points = []
        for lbl in labels:
            a = agg[lbl]
            rmean, rstd, rruns = _rig(a)
            points.append(
                {
                    "label": lbl,
                    "value": a["value"],
                    "bpp": a["sum"] / a["n"],
                    "time_s": a["t"] / a["n"],
                    # Isolated, repeated-trial endpoint timing (issue #26): None on
                    # interior/single-pass points. The report plots x by these where
                    # present, falling back to time_s, and uses runs > 1 to mark a
                    # point as statistically anchored.
                    "time_rigorous_s": rmean,
                    "time_stddev_s": rstd,
                    "runs": rruns,
                }
            )
        best = min(points, key=lambda p: p["bpp"])
        result[impl] = {
            "format": impl_rows[0]["format"],
            # The swept effort knob (e.g. "method"/"effort"/"compression"); empty for
            # a single-knob encoder with no effort axis. The report uses this — not a
            # point-count guess — to tell a swept curve from a lone operating point.
            "axis": schema.quality_axis or "",
            "best_bpp": best["bpp"],
            "best_label": best["label"],
            "ratio": _SOURCE_BPP / best["bpp"] if best["bpp"] > 0 else None,
            "points": points,
        }
    return result


def decoder_fidelity(
    metrics: list[BenchmarkMetrics],
) -> Dict[str, Dict[str, Any]]:
    """Per decoder, its speed and fidelity versus the reference it is scored
    against across the sweep of reference-encoded inputs.

    Decoders have no rate-distortion tradeoff of their own. A correct decoder
    reproduces its reference exactly, so its PSNR is ∞ (recorded as ``None``). The
    reference differs by input: a **losslessly-encoded input** (PNG always; the
    WebP/JXL lossless path) has the *source* as ground truth (``metric_basis ==
    "source"``), so fidelity is measured directly against it; a **lossy input**
    (JPEG, AVIF, WebP/JXL lossy path) has no source truth, so it is measured
    against the format's *golden* decoder (``metric_basis == "golden"``). This view
    answers "how fast, and is it faithful?" — each decoder's rows are aggregated to
    its mean one-pass decode time, mean input bpp, the worst (minimum *finite*)
    PSNR, whether every point was bit-exact, and a ``points`` list (decode time +
    PSNR + bit_exact vs input bpp) for the speed-vs-bitrate chart.

    Bit-exactness uses the row's definitive ``bit_exact`` byte-compare field when
    present, falling back to "no finite PSNR" for older bundles.

    Formats with both a lossy and a lossless mode (WebP, JXL) decode inputs from
    both paths (issue #21); the two are reported separately — the lossless path is
    keyed ``"<impl> (lossless)"`` — so a bit-exact lossless path can't mask an
    approximate lossy one in the same decoder's aggregate.

    Returns ``{key: {format, mean_time_s, mean_bpp, count, bit_exact,
    worst_psnr, basis, approx_expected, points:[{bpp, time_s, psnr, bit_exact,
    label}]}}`` where ``key`` is the decoder name (lossy path) or ``"<impl>
    (lossless)"`` (lossless path), and ``approx_expected`` marks a non-bit-exact
    result that is expected/faithful (lossy JPEG or JXL vs golden) rather than a
    failure. Decoders with no valid scored row are omitted."""
    rows = [
        m
        for m in metrics
        if m["type"] == "decode"
        and m.get("metric_basis") in ("golden", "source")
        and not m.get("error")
    ]
    result: Dict[str, Dict[str, Any]] = {}
    grouped = _group_by(rows, lambda m: (m["impl"], bool(m.get("lossless"))))
    for (impl, is_lossless), impl_rows in grouped.items():
        times = [
            m["time_s"] for m in impl_rows if isinstance(m.get("time_s"), (int, float))
        ]
        bpps = [m["bpp"] for m in impl_rows if m["bpp"] > 0]
        # PSNR is None for a pixel-identical (bit-exact) decode; a finite value
        # flags an approximate decode path (vs source for lossless, vs golden lossy).
        finite_psnrs = [
            m["psnr"] for m in impl_rows if isinstance(m.get("psnr"), (int, float))
        ]
        # Definitive bit-exactness from the per-row byte compare when available;
        # legacy bundles (no field) fall back to the PSNR-non-finite inference.
        explicit = [m["bit_exact"] for m in impl_rows if m.get("bit_exact") is not None]
        bit_exact = all(explicit) if explicit else (len(finite_psnrs) == 0)
        points = sorted(
            (
                {
                    "bpp": m["bpp"],
                    "time_s": m.get("time_s") or 0.0,
                    "psnr": m.get("psnr"),
                    "bit_exact": m.get("bit_exact"),
                    "label": m["label"],
                    # Rigorous (isolated, single-core, repeated-trial) decode time on
                    # the rows the overlay re-timed (anchor label, perf-images subset);
                    # only repeated runs > 1 count. None elsewhere. The report pools
                    # these per label to anchor and whisker the speed-vs-bitrate chart.
                    "time_rigorous_s": (
                        m["time_rigorous_s"]
                        if isinstance(m.get("time_rigorous_s"), (int, float))
                        and (m.get("time_runs") or 0) > 1
                        else None
                    ),
                    "time_stddev_s": m.get("time_rigorous_stddev_s"),
                    "runs": m.get("time_runs") or 0,
                }
                for m in impl_rows
            ),
            key=lambda p: p["bpp"],
        )
        basis = impl_rows[0].get("metric_basis", "golden")
        fmt = impl_rows[0]["format"]
        # Lossy path keeps the bare impl name; the lossless path (issue #21) is a
        # distinct, self-describing row so the two are not conflated.
        display = f"{impl} (lossless)" if is_lossless else impl
        result[display] = {
            "format": fmt,
            "mean_time_s": (sum(times) / len(times)) if times else 0.0,
            "mean_bpp": (sum(bpps) / len(bpps)) if bpps else 0.0,
            "count": len(impl_rows),
            "bit_exact": bit_exact,
            "worst_psnr": (min(finite_psnrs) if finite_psnrs else None),
            "basis": basis,
            # True when a finite PSNR here is EXPECTED (a faithful, sub-LSB lossy
            # decode of a non-normative format: JPEG, lossy JXL) so the report
            # shows it neutrally, not as a failure. False for the lossless path
            # (source basis) and normative lossy formats (AV1/AVIF, VP8/WebP),
            # where bit-exact is required.
            "approx_expected": decode_approx_expected(fmt, basis),
            "points": points,
        }
    return result


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


def compute_bd_rate(
    rate_ref: List[float],
    metric_ref: List[float],
    rate_test: List[float],
    metric_test: List[float],
) -> Optional[float]:
    """Bjøntegaard delta-rate (%) of `test` relative to `ref`, using `metric`
    (e.g. SSIMULACRA2) as the quality axis and log(rate=bpp). Negative means the
    test codec needs *less* rate for the same quality (better). Returns ``None``
    when it cannot be computed (too few points, no overlap, or a numerical
    failure)."""
    try:
        if len(rate_ref) < 2 or len(rate_test) < 2:
            return None
        lr_ref = np.log(np.asarray(rate_ref, dtype=float))
        lr_test = np.log(np.asarray(rate_test, dtype=float))
        m_ref = np.asarray(metric_ref, dtype=float)
        m_test = np.asarray(metric_test, dtype=float)

        lo = max(m_ref.min(), m_test.min())
        hi = min(m_ref.max(), m_test.max())
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            return None

        deg = min(3, len(m_ref) - 1, len(m_test) - 1)
        if deg < 1:
            return None
        p_ref = np.polyfit(m_ref, lr_ref, deg)
        p_test = np.polyfit(m_test, lr_test, deg)
        ip_ref = np.polyint(p_ref)
        ip_test = np.polyint(p_test)
        int_ref = np.polyval(ip_ref, hi) - np.polyval(ip_ref, lo)
        int_test = np.polyval(ip_test, hi) - np.polyval(ip_test, lo)
        avg = (int_test - int_ref) / (hi - lo)
        result = (np.exp(avg) - 1.0) * 100.0
        return float(result) if np.isfinite(result) else None
    except Exception:
        return None


def compute_bd_rate_table(
    metrics: list[BenchmarkMetrics],
) -> Dict[str, Dict[str, Optional[float]]]:
    """Per-format BD-rate of each lossy encoder versus that format's reference
    encoder, using SSIMULACRA2. BD-rate is computed per image (each image is its
    own rate-distortion curve) and averaged across the dataset.

    Returns ``{format: {impl: bd_rate_percent_or_None}}`` (excludes the anchor).
    """
    table: Dict[str, Dict[str, Optional[float]]] = {}
    encode_metrics = _filter_valid_encode_metrics(metrics)
    if not encode_metrics:
        return table

    by_fmt = _group_by(encode_metrics, lambda m: m["format"])
    for fmt, fmt_metrics in by_fmt.items():
        try:
            anchor = REFERENCE_ENCODERS.get(ImageFormat(fmt))
        except ValueError:
            anchor = None
        if anchor is None:
            continue

        # (impl, image) -> sorted [(bpp, ssimulacra2)]
        curves: Dict[Tuple[str, str], list] = {}
        for m in fmt_metrics:
            key = (m["impl"], os.path.basename(m["input_path"]))
            curves.setdefault(key, []).append((m["bpp"], m["ssimulacra2"]))

        impls = sorted({m["impl"] for m in fmt_metrics})
        images = sorted({os.path.basename(m["input_path"]) for m in fmt_metrics})
        if anchor not in impls:
            continue

        fmt_table: Dict[str, Optional[float]] = {}
        for impl in impls:
            if impl == anchor:
                continue
            rates: list = []
            for img in images:
                ref = curves.get((anchor, img))
                test = curves.get((impl, img))
                if not ref or not test:
                    continue
                ref_s = sorted(ref)
                test_s = sorted(test)
                bd = compute_bd_rate(
                    [p[0] for p in ref_s],
                    [p[1] for p in ref_s],
                    [p[0] for p in test_s],
                    [p[1] for p in test_s],
                )
                if bd is not None:
                    rates.append(bd)
            fmt_table[impl] = (sum(rates) / len(rates)) if rates else None
        if fmt_table:
            table[fmt] = fmt_table

    return table
