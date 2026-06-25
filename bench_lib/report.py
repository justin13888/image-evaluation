"""Self-contained HTML report for a results bundle.

Bundles a run into a single ``report.html`` with no external assets. The
performance suite's charts are embedded as base64 PNGs. The quality suite is
**interactive**: its raw ``metrics.json`` (plus small derived summaries — BD-rate
and the Pareto front) is embedded inline as JSON, and the rate-distortion curves
are drawn client-side by ``assets/report.js`` from that data. The embedded raw
data is the single source of truth, so anything in the quality view can be
recomputed from the report alone.
"""

import base64
import glob
import html
import json
import os
from typing import Any, Optional

from bench_lib.plotting import (
    compute_bd_rate_table,
    decoder_fidelity,
    lossless_efficiency,
    pareto_front_encoders,
)

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")

# Public repository, for linking a run back to the exact commit that produced it.
_REPO_URL = "https://github.com/justin13888/image-evaluation"


def _asset(name: str) -> str:
    """Read a bundled asset (CSS/JS) to inline into the report."""
    with open(os.path.join(_ASSET_DIR, name), encoding="utf-8") as f:
        return f.read()


def _json_script(elem_id: str, obj: Any) -> str:
    """Embed ``obj`` as a compact ``<script type=application/json>`` block. ``<``
    is escaped to ``\\u003c`` (valid inside a JSON string) so no string value can
    close the script element or open a comment."""
    payload = json.dumps(obj, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script id="{elem_id}" type="application/json">{payload}</script>'


def _img_tag(img_path: str, fmt: str = "") -> str:
    """Embed a chart as a base64 <img> data URI (self-contained, no external src).
    SVG charts use ``image/svg+xml``; anything else is treated as PNG. A data-URI
    <img> (rather than inlined markup) isolates each SVG so matplotlib's shared
    ids/clip-paths can't collide across charts. ``fmt`` (the chart's image format,
    lowercase) is stamped as ``data-format`` so the report's format filter can
    show/hide this chart."""
    mime = "image/svg+xml" if img_path.endswith(".svg") else "image/png"
    with open(img_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    alt = html.escape(os.path.basename(img_path))
    fmt_attr = f' data-format="{html.escape(fmt)}"' if fmt else ""
    return (
        f'<figure{fmt_attr}><img alt="{alt}" '
        f'src="data:{mime};base64,{data}">'
        f"<figcaption>{alt}</figcaption></figure>"
    )


# Formats charts are grouped under, so the many per-(format, op, point) figures
# tab by format rather than scrolling as one long column.
_GROUP_FORMATS = ("jpeg", "png", "webp", "avif", "jxl")


def _chart_group(path: str) -> str:
    """Group key for a chart filename — the image format it belongs to, else
    'Other'. Filenames are ``<fmt>_op_…`` (perf), ``scaling_<fmt>_op`` and
    ``effort_<fmt>_…``, so the format token is in the first two underscore parts."""
    parts = os.path.splitext(os.path.basename(path))[0].lower().split("_")
    for tok in parts[:2]:
        if tok in _GROUP_FORMATS:
            return tok.upper()
    return "Other"


def _gallery_html(charts: list[str], group_id: str) -> str:
    """Embed charts as a full-width, ARIA-tabbed gallery grouped by format, so the
    big per-format figure sets are one tab each rather than a long scroll. A single
    group needs no tabs (flat list). Powered by the shared gallery-tab JS in
    report.js (it wires any ``[data-img-tabs]`` container)."""
    groups: dict[str, list[str]] = {}
    for p in charts:
        groups.setdefault(_chart_group(p), []).append(p)
    if len(groups) <= 1:
        # Single group: no tabs, but still wrap in a [data-img-tabs] block so every
        # gallery is uniform (same full-bleed centring + format-filter hooks).
        figs = "\n".join(_img_tag(p, _chart_group(p).lower()) for p in charts)
        return f'<div class="img-tabs" data-img-tabs>{figs}</div>'
    tabs: list[str] = []
    panels: list[str] = []
    for i, key in enumerate(sorted(groups)):
        sel = i == 0
        fmt = key.lower()
        tabs.append(
            f'<button class="q-tab{" active" if sel else ""}" type="button" '
            f'role="tab" id="{group_id}-tab-{i}" aria-controls="{group_id}-panel-{i}" '
            f'aria-selected="{"true" if sel else "false"}" data-format="{fmt}" '
            f'tabindex="{"0" if sel else "-1"}">{html.escape(key)}</button>'
        )
        figs = "\n".join(_img_tag(p, fmt) for p in groups[key])
        panels.append(
            f'<div class="q-tabpanel" role="tabpanel" id="{group_id}-panel-{i}" '
            f'aria-labelledby="{group_id}-tab-{i}" data-format="{fmt}" tabindex="0"'
            f"{'' if sel else ' hidden'}>{figs}</div>"
        )
    return (
        '<div class="img-tabs" data-img-tabs>'
        '<div class="q-tablist" role="tablist" aria-label="Charts by format">'
        + "".join(tabs)
        + "</div>"
        + "".join(panels)
        + "</div>"
    )


def _embed_charts(section_dir: str, group_id: str) -> str:
    """Embed every chart in a suite subdirectory, sorted by name, as a tabbed-by-
    format gallery. Prefers SVG and falls back to PNG of the same basename, so
    older PNG-only bundles still render."""
    by_base: dict[str, str] = {}
    # PNGs first, then let SVGs of the same basename win.
    for path in sorted(glob.glob(os.path.join(section_dir, "*.png"))):
        by_base[os.path.splitext(os.path.basename(path))[0]] = path
    for path in sorted(glob.glob(os.path.join(section_dir, "*.svg"))):
        by_base[os.path.splitext(os.path.basename(path))[0]] = path
    charts = [by_base[k] for k in sorted(by_base)]
    if not charts:
        return "<p><em>No charts.</em></p>"
    return _gallery_html(charts, group_id)


def _load_json(path: str) -> Optional[Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _benchmark_config(bundle_dir: str) -> Optional[dict]:
    """The run's ``benchmark_config`` (dataset, formats, mode, …) from whichever
    manifest carries it. Quality is preferred since it always runs."""
    for rel in ("quality/manifest.json", "performance/manifest.json", "manifest.json"):
        m = _load_json(os.path.join(bundle_dir, rel))
        if isinstance(m, dict) and isinstance(m.get("benchmark_config"), dict):
            return m["benchmark_config"]
    return None


def _git_info(bundle_dir: str) -> Optional[dict]:
    """The run's git provenance (commit + dirty flag) from whichever manifest
    carries it, mirroring ``_benchmark_config``'s search order. ``None`` for older
    bundles whose manifests predate git capture."""
    for rel in ("quality/manifest.json", "performance/manifest.json", "manifest.json"):
        m = _load_json(os.path.join(bundle_dir, rel))
        if isinstance(m, dict):
            git = m.get("git")
            if isinstance(git, dict) and git.get("commit"):
                return git
    return None


def _distinct_image_count(bundle_dir: str) -> Optional[int]:
    """Number of distinct source images actually scored, from the embedded
    metrics — the ground truth of what the curves aggregate over. ``None`` if no
    metrics are present."""
    metrics = _load_json(os.path.join(bundle_dir, "quality", "metrics.json"))
    if not isinstance(metrics, list):
        return None
    images = {
        os.path.basename(m.get("source_path") or m.get("input_path") or "")
        for m in metrics
        if isinstance(m, dict) and not m.get("error")
    }
    images.discard("")
    return len(images) or None


def _config_section(bundle_dir: str) -> list[str]:
    """A 'Dataset & run configuration' table: what was benchmarked, how many
    images the curves aggregate over, and a link to the dataset's source. Empty
    when no benchmark_config manifest is available (e.g. a bare metrics bundle)."""
    cfg = _benchmark_config(bundle_dir)
    if not cfg:
        return []
    n_images = _distinct_image_count(bundle_dir)

    rows: list[str] = []

    def row(label: str, value: str) -> None:
        rows.append(f"<tr><th>{html.escape(label)}</th><td>{value}</td></tr>")

    dataset = cfg.get("dataset")
    if dataset:
        homepage = cfg.get("dataset_homepage")
        name = html.escape(str(dataset))
        if homepage:
            name = (
                f'<a href="{html.escape(str(homepage))}" '
                f'rel="noopener noreferrer">{name}</a>'
            )
        desc = cfg.get("dataset_description")
        if desc:
            name += f" &mdash; {html.escape(str(desc))}"
        row("Dataset", name)

    if n_images is not None:
        sample = cfg.get("sample")
        note = (
            " (single image &mdash; values are that image's measurement, not an "
            "average)"
            if n_images == 1
            else " (each plotted point is the mean across these images)"
        )
        sample_note = (
            f" &middot; sampled from a larger set (--sample {html.escape(str(sample))})"
            if sample
            else ""
        )
        row("Images", f"{n_images}{note}{sample_note}")

    formats = cfg.get("formats")
    if formats:
        row(
            "Formats",
            html.escape(
                ", ".join(str(f) for f in formats)
                if isinstance(formats, list)
                else str(formats)
            ),
        )
    if cfg.get("mode"):
        row("Mode", html.escape(str(cfg["mode"])))
    qsteps = cfg.get("quality_steps")
    row(
        "Quality points",
        "every declared point" if qsteps in (None, 0) else html.escape(str(qsteps)),
    )
    if cfg.get("quick"):
        row("Quick mode", "yes (2 quality points/impl)")

    git = _git_info(bundle_dir)
    if git and git.get("commit"):
        commit = str(git["commit"])
        link = (
            f'<a href="{html.escape(_REPO_URL)}/tree/{html.escape(commit)}" '
            f'rel="noopener noreferrer"><code>{html.escape(commit[:12])}</code></a>'
        )
        if git.get("dirty"):
            link += " (dirty)"
        row("Commit", link)

    if not rows:
        return []
    return [
        "<h2>Dataset &amp; Run Configuration</h2>",
        "<table>" + "".join(rows) + "</table>",
    ]


def _manifest_summary(bundle_dir: str) -> str:
    """A short system/config summary from whichever manifest is available."""
    for rel in ("performance/manifest.json", "quality/manifest.json", "manifest.json"):
        m = _load_json(os.path.join(bundle_dir, rel))
        if not m:
            continue
        rows = []
        for key in ("os", "kernel", "cpu", "cores", "allocator"):
            if key in m:
                rows.append(
                    f"<tr><th>{html.escape(key)}</th>"
                    f"<td>{html.escape(str(m[key]))}</td></tr>"
                )
        compiler = m.get("compiler", {})
        if compiler:
            rows.append(
                "<tr><th>compiler</th><td>"
                + html.escape(", ".join(f"{k} {v}" for k, v in compiler.items()))
                + "</td></tr>"
            )
        return "<table>" + "".join(rows) + "</table>" if rows else ""
    return ""


def _quality_section(qual_dir: str) -> list[str]:
    """Build the interactive quality section: embed the raw metrics + derived
    summaries as JSON, drop in the chart mount points, and inline the chart JS.
    Returns the HTML fragments (empty if there is nothing to show)."""
    metrics = _load_json(os.path.join(qual_dir, "metrics.json"))
    parts = ["<h2>Quality &mdash; rate-distortion &amp; decoder fidelity</h2>"]
    if not metrics:
        parts.append("<p><em>No quality metrics.</em></p>")
        return parts

    qmanifest = _load_json(os.path.join(qual_dir, "manifest.json"))
    parts.append(
        "<p class='muted'>Interactive — rendered in your browser from the raw "
        "measurements embedded below. The <em>View</em> picker sets the X axis "
        "shared by every rate-distortion chart: quality vs size (bpp), vs encode "
        "time, or vs decode time (← → switch the per-format tabs; Alt+[ / Alt+] "
        "cycle views). Each format's tab stacks one full-width chart per metric "
        "(SSIMULACRA2, PSNR, SSIM, Butteraugli), and the <em>Cross-format Pareto</em> "
        "tab overlays each format's best encoders. Use the <em>Filters</em> panel to "
        "choose which metrics and implementations are shown; hover a point for "
        "details, and <strong>click a point (or focus a chart and press Enter) to "
        "view the exact images aggregated into it</strong>. Lossless encoders "
        "(PNG, lossless JXL/WebP) have no rate-distortion "
        "tradeoff, so they appear in their own compression-efficiency section rather "
        "than on the curves. Decode time is a real per-output measurement (the "
        "reference decode of each encoded result); encode time is shown on the size "
        "views as the point's <em>bubble size</em> (bigger = slower). Both are "
        "single-pass wall-clocks measured under the parallel pool — a <em>relative</em> "
        "sense of how an operating point's cost scales, not the performance suite's "
        "isolated timing.</p>"
    )
    parts.append(
        "<div class='q-disclaimer'><strong>IQA metrics are approximations, not "
        "ground truth.</strong> SSIMULACRA2, PSNR, SSIM and Butteraugli are "
        "automated estimators of perceived quality, each with its own assumptions "
        "and blind spots. SSIMULACRA2 is calibrated against subjective data at "
        "specific viewing conditions and can mis-rank distortions it was not tuned "
        "for (especially near the near-lossless end); PSNR is pixel-wise error "
        "that correlates poorly with perception; SSIM captures structural "
        "similarity (higher is better); Butteraugli is a perceptual difference "
        "where <em>lower</em> is better (0 = identical). Aggregate scores (BD-rate, "
        "Pareto fronts) are "
        "sensitive to the metric, dataset, and operating points chosen, and a few "
        "points of SSIMULACRA2 may not be perceptible. Treat these results as a "
        "reproducible guide for narrowing options &mdash; <em>not</em> a "
        "substitute for a controlled human subjective study (e.g. MOS) when "
        "determining the genuinely best-looking option.</div>"
    )
    # Embedded data (source of truth). Charts recompute from #quality-metrics; the
    # BD-rate and Pareto summaries are precomputed here (numpy / dominance) and
    # embedded so the browser need not reimplement them.
    parts.append(_json_script("quality-metrics", metrics))
    if qmanifest:
        parts.append(_json_script("quality-manifest", qmanifest))
    parts.append(_json_script("quality-bdrate", compute_bd_rate_table(metrics)))
    parts.append(_json_script("quality-pareto", pareto_front_encoders(metrics)))
    parts.append(_json_script("quality-lossless", lossless_efficiency(metrics)))
    parts.append(_json_script("quality-decoders", decoder_fidelity(metrics)))

    parts.append(
        "<div id='quality-app'>"
        "<div id='q-controls' class='q-controls' role='group' "
        "aria-label='Chart view controls'></div>"
        "<div id='q-status' class='q-visually-hidden' role='status' "
        "aria-live='polite'></div>"
        "<div id='q-aggregation' class='q-agg'></div>"
        "<details id='q-filters' class='q-filters'>"
        "<summary>Filters — metrics &amp; implementations shown</summary>"
        "<div id='q-filters-body' class='q-filters-body'></div>"
        "</details>"
        "<h3>Rate-distortion — by format</h3>"
        "<p class='q-note'>Per-format tabs (plus a cross-format Pareto overview); "
        "each tab stacks one full-width chart per metric, all on the X axis chosen "
        "by the <em>View</em> picker. Every encoder's quality sweep is aggregated to "
        "a mean curve over the images (see the aggregation note above); each "
        "metric's y-axis is fixed to its known range, so formats are directly "
        "comparable. Up and to the left/up is better.</p>"
        "<div id='q-tabs'></div>"
        "<h3>Lossless compression efficiency"
        "<span class='q-section-toggle' id='q-toggle-lossless'></span></h3>"
        "<p class='q-note'>Lossless encoders produce a pixel-identical image, so "
        "they differ only in file size — lower bits-per-pixel (bpp) is better. The "
        "leaderboard ranks each encoder by its smallest achievable bpp; the "
        "size-vs-effort chart traces how bpp falls as compression effort rises "
        "(single-knob encoders show one point), with bubble size encoding mean "
        "encode time (the size-vs-speed tradeoff that is lossless's whole story).</p>"
        "<div class='q-fidelity-note'><b>What these mean.</b> Lossless encoders "
        "all reproduce the source <b>pixel-for-pixel</b>, so correctness is a "
        "given and they compete only on size: <b>bpp</b> is the encoded bits per "
        "pixel and the <b>ratio</b> is against the 24&nbsp;bpp RGB source &mdash; "
        "lower bpp / higher ratio is better.</div>"
        "<div id='q-lossless'></div>"
        "<h3>Decoder fidelity &amp; speed"
        "<span class='q-section-toggle' id='q-toggle-decoder'></span></h3>"
        "<p class='q-note'>Decoders carry no rate-distortion tradeoff, so they are "
        "judged on speed and on fidelity against the reference they are scored "
        "against: the <em>source</em> ground truth for a losslessly-encoded input "
        "(PNG always; the WebP/JXL lossless path) or the format's <em>golden</em> "
        "(reference) decoder for a lossy input. Fidelity is computed by a definitive "
        "byte-level compare: ∞ = bit-exact; a finite worst-case PSNR flags an "
        "approximate decode path. Decode time is the relative one-pass cost across "
        "the input-bitrate sweep; the <em>Show time</em> toggle adds a "
        "speed-vs-bitrate scatter above the table.</p>"
        "<div class='q-fidelity-note'><b>Reading this table.</b> "
        "<b>Bit-exact</b> (&infin;) = the decoder reproduces its <b>basis</b> "
        "byte-for-byte; the basis is the original <b>source</b> for a "
        "losslessly-encoded input or the format's <b>golden</b> reference decoder "
        "for a lossy one. JPEG and lossy-JXL inverse transforms are not "
        "bit-reproducible across independent decoders, so a high <b>PSNR vs "
        "golden</b> (&asymp;&nbsp;50+&nbsp;dB) means <b>faithful, not broken</b>; "
        "AV1/AVIF and VP8/WebP use normative integer transforms, so for those "
        "bit-exact is required.</div>"
        "<div id='q-decoders'></div>"
        "<h3>BD-rate (SSIMULACRA2, vs reference encoder)</h3>"
        "<p class='q-note'>Negative = fewer bits for equal quality (better). "
        "Computed per image then averaged; <code>N/A</code> = non-overlapping "
        "quality ranges. Click a header to sort.</p>"
        "<div class='q-fidelity-note'><b>What this means.</b> BD-rate is the "
        "average <b>size difference at equal quality</b> (SSIMULACRA2) versus the "
        "format's <b>reference encoder</b>: <b>negative = fewer bits for the same "
        "quality (better)</b>, positive = larger. A relative rate measure, not an "
        "absolute size.</div>"
        "<div id='q-bdrate'></div>"
        "</div>"
    )
    return parts


_CSS = """
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem auto;
       max-width: 1100px; padding: 0 1rem; color: #1a1a1a; }
h1 { border-bottom: 2px solid #333; padding-bottom: .3rem; }
h2 { margin-top: 2.5rem; border-bottom: 1px solid #ccc; }
figure { margin: 1rem 0; }
img { max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 4px; }
figcaption { font-size: .85rem; color: #666; }
table { border-collapse: collapse; margin: 1rem 0; }
th, td { border: 1px solid #ccc; padding: .3rem .6rem; text-align: left; }
th { background: #f3f3f3; }
.muted { color: #777; font-size: .9rem; }
"""


def generate_report_html(bundle_dir: str, generated_at: Optional[str] = None) -> str:
    """Write ``<bundle_dir>/report.html`` bundling the performance charts and the
    interactive quality view (whichever subfolders exist). Returns the path."""
    perf_dir = os.path.join(bundle_dir, "performance")
    qual_dir = os.path.join(bundle_dir, "quality")

    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Benchmark Report</title>",
        f"<style>{_CSS}\n{_asset('report.css')}</style></head><body>",
        "<h1>Image Evaluation — Report</h1>",
    ]
    if generated_at:
        parts.append(f"<p class='muted'>Generated: {html.escape(generated_at)}</p>")

    parts.extend(_config_section(bundle_dir))

    manifest_html = _manifest_summary(bundle_dir)
    if manifest_html:
        parts.append("<h2>Environment</h2>")
        parts.append(manifest_html)

    # Quality is primary (rate-distortion + decoder fidelity); the rigorous timing
    # overlay is the optional secondary view, shown below it.
    if os.path.isdir(qual_dir):
        parts.extend(_quality_section(qual_dir))

    if os.path.isdir(perf_dir):
        parts.append("<h2>Performance &mdash; rigorous timing overlay</h2>")
        parts.append(
            "<p class='muted'>Optional, secondary view. Isolated hyperfine timing "
            "(warmup + repeats, compute-only) at the selected operating points, "
            "across single-threaded and all-cores modes. Quality above is primary: "
            "raw speed is only meaningful alongside the quality it trades for.</p>"
        )
        parts.append(_embed_charts(perf_dir, "perf"))

    scal_dir = os.path.join(bundle_dir, "scaling")
    if os.path.isdir(scal_dir):
        parts.append("<h2>Scaling &mdash; time vs pixel count</h2>")
        parts.append(
            "<p class='muted'>Each codec timed single-threaded at its performance "
            "preset on a downscale-only resolution ladder (same content, only pixels "
            "vary). Axes are log-log; the dashed line is a fit of "
            "<code>time &prop; pixels<sup>k</sup></code>. <strong>k &asymp; 1 is "
            "linear; k &gt; 1 is super-linear</strong> (cost grows faster than pixel "
            "count) &mdash; the per-codec exponent and R² are in the legend and "
            "<code>scaling/summary.md</code>. Single-threaded to isolate the "
            "pixel-count exponent from parallel-scaling effects.</p>"
        )
        parts.append(_embed_charts(scal_dir, "scaling"))

    eff_dir = os.path.join(bundle_dir, "effort")
    if os.path.isdir(eff_dir):
        parts.append("<h2>Effort / speed &mdash; time vs quality vs size</h2>")
        parts.append(
            "<p class='muted'>The lever the rate-distortion sweep pins: each lossy "
            "codec's effort/speed knob (AVIF <code>speed</code>, JXL "
            "<code>effort</code>, WebP <code>method</code>) swept at a fixed quality "
            "preset on a ~1 MP downscale. Charts show how encode time, size (bpp) and "
            "SSIMULACRA2 move with the knob; the tradeoff (slower = smaller/better, "
            "to a point) is the whole story. Encode time is a single-pass wall-clock "
            "(relative), not the performance suite's isolated timing. Numbers are in "
            "<code>effort/summary.md</code>.</p>"
        )
        parts.append(_embed_charts(eff_dir, "effort"))

    parts.append(
        "<p class='muted'>Raw data is embedded above "
        "(<code>#quality-metrics</code>) and also on disk alongside this file: "
        "<code>performance/raw.json</code>, <code>quality/metrics.json</code>, "
        "and per-suite <code>summary.md</code>.</p>"
    )
    # One inlined chart engine for the whole document: it draws the interactive
    # quality view and wires every tabbed image gallery (perf/scaling/effort).
    parts.append(f"<script>{_asset('report.js')}</script>")
    parts.append("</body></html>")

    out_path = os.path.join(bundle_dir, "report.html")
    with open(out_path, "w") as f:
        f.write("\n".join(parts))
    return out_path
