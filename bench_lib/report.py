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

from bench_lib.plotting import compute_bd_rate_table, pareto_front_encoders

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")


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


def _img_tag(png_path: str) -> str:
    """Embed a PNG as a base64 <img> data URI (self-contained, no external src)."""
    with open(png_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    alt = html.escape(os.path.basename(png_path))
    return (
        f'<figure><img alt="{alt}" '
        f'src="data:image/png;base64,{data}">'
        f"<figcaption>{alt}</figcaption></figure>"
    )


def _embed_pngs(section_dir: str) -> str:
    """Embed every PNG in a suite subdirectory, sorted by name."""
    pngs = sorted(glob.glob(os.path.join(section_dir, "*.png")))
    if not pngs:
        return "<p><em>No charts.</em></p>"
    return "\n".join(_img_tag(p) for p in pngs)


def _load_json(path: str) -> Optional[Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


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
    parts = ["<h2>Quality (rate-distortion)</h2>"]
    if not metrics:
        parts.append("<p><em>No quality metrics.</em></p>")
        return parts

    qmanifest = _load_json(os.path.join(qual_dir, "manifest.json"))
    parts.append(
        "<p class='muted'>Interactive — rendered in your browser from the raw "
        "measurements embedded below. Hover a point for details; click a legend "
        "entry to toggle a series. The per-format charts show every metric at "
        "once; the Pareto-metric and x-axis-scale toggles up top drive the "
        "cross-format Pareto chart.</p>"
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

    parts.append(
        "<div id='quality-app'>"
        "<div id='q-controls'></div>"
        "<h3>Cross-format Pareto front — best encoders of each format</h3>"
        "<p class='q-note'>Each curve is a format's Pareto-optimal encoder(s) "
        "(non-dominated in bpp vs quality), coloured by format. Up and to the "
        "left is better.</p>"
        "<div id='q-combined'></div>"
        "<h3>Rate-distortion by format</h3>"
        "<p class='q-note'>A small-multiples grid per format: every encoder's "
        "quality sweep aggregated to a clean mean curve across the dataset, shown "
        "for every available metric (SSIMULACRA2, PSNR, SSIM, Butteraugli).</p>"
        "<div id='q-charts'></div>"
        "<h3>BD-rate (SSIMULACRA2, vs reference encoder)</h3>"
        "<p class='q-note'>Negative = fewer bits for equal quality (better). "
        "Computed per image then averaged; <code>N/A</code> = non-overlapping "
        "quality ranges. Click a header to sort.</p>"
        "<div id='q-bdrate'></div>"
        "</div>"
    )
    parts.append(f"<script>{_asset('report.js')}</script>")
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
        "<h1>Image Implementation Benchmark — Report</h1>",
    ]
    if generated_at:
        parts.append(f"<p class='muted'>Generated: {html.escape(generated_at)}</p>")

    manifest_html = _manifest_summary(bundle_dir)
    if manifest_html:
        parts.append("<h2>Environment</h2>")
        parts.append(manifest_html)

    if os.path.isdir(perf_dir):
        parts.append("<h2>Performance (timing)</h2>")
        parts.append(_embed_pngs(perf_dir))

    if os.path.isdir(qual_dir):
        parts.extend(_quality_section(qual_dir))

    parts.append(
        "<p class='muted'>Raw data is embedded above "
        "(<code>#quality-metrics</code>) and also on disk alongside this file: "
        "<code>performance/raw.json</code>, <code>quality/metrics.json</code>, "
        "and per-suite <code>summary.md</code>.</p>"
    )
    parts.append("</body></html>")

    out_path = os.path.join(bundle_dir, "report.html")
    with open(out_path, "w") as f:
        f.write("\n".join(parts))
    return out_path
