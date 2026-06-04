"""Self-contained HTML report for a results bundle.

Bundles a run's summarized graphs (and the BD-rate table) into a single
``report.html`` with every image embedded as a base64 data URI, so the whole
result set is one openable, shareable file with no external assets.
"""

import base64
import glob
import html
import json
import os
from typing import Optional

from bench_lib.plotting import compute_bd_rate_table


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


def _load_json(path: str) -> Optional[dict]:
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


def _bd_rate_table_html(bundle_dir: str) -> str:
    """BD-rate table (vs each format's reference encoder) from the quality
    suite's metrics.json, if present."""
    metrics = _load_json(os.path.join(bundle_dir, "quality", "metrics.json"))
    if not metrics:
        return ""
    table = compute_bd_rate_table(metrics)
    if not table:
        return ""
    rows = ["<tr><th>Format</th><th>Implementation</th><th>BD-rate vs ref</th></tr>"]
    for fmt in sorted(table):
        for impl in sorted(table[fmt]):
            bd = table[fmt][impl]
            bd_str = f"{bd:+.1f}%" if bd is not None else "N/A"
            rows.append(
                f"<tr><td>{html.escape(fmt.upper())}</td>"
                f"<td>{html.escape(impl)}</td><td>{bd_str}</td></tr>"
            )
    return (
        "<h3>BD-rate (SSIMULACRA2, vs reference encoder)</h3>"
        "<p>Negative = fewer bits for equal quality (better). "
        "<code>N/A</code> = non-overlapping quality ranges.</p>"
        "<table>" + "".join(rows) + "</table>"
    )


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
    """Write ``<bundle_dir>/report.html`` bundling the performance and quality
    charts (whichever subfolders exist) plus the BD-rate table. Returns the path.
    """
    perf_dir = os.path.join(bundle_dir, "performance")
    qual_dir = os.path.join(bundle_dir, "quality")

    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Benchmark Report</title>",
        f"<style>{_CSS}</style></head><body>",
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
        parts.append("<h2>Quality (rate-distortion)</h2>")
        bd = _bd_rate_table_html(bundle_dir)
        if bd:
            parts.append(bd)
        parts.append(_embed_pngs(qual_dir))

    parts.append(
        "<p class='muted'>Raw data lives alongside this file: "
        "<code>performance/raw.json</code>, <code>quality/metrics.json</code>, "
        "and per-suite <code>summary.md</code>.</p>"
    )
    parts.append("</body></html>")

    out_path = os.path.join(bundle_dir, "report.html")
    with open(out_path, "w") as f:
        f.write("\n".join(parts))
    return out_path
