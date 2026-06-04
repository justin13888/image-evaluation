#!/usr/bin/env python3
"""
Sanity test for plot generation and report bundling.

Constructs small synthetic inputs and exercises:
  - bench_lib.summary.generate_summary on hyperfine-like timing JSON (perf),
  - generate_summary on quality metrics (rate-distortion curves + BD-rate),
  - bench_lib.report.generate_report_html on a fake bundle (self-contained HTML).
"""

import os
import sys
import json

# If we're not running in a virtualenv, attempt to re-exec using `.venv/bin/python`
if not os.environ.get("VIRTUAL_ENV"):
    venv_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".venv"))
    candidate = os.path.join(venv_root, "bin", "python")
    if os.path.exists(candidate):
        print(f"Re-executing using venv python: {candidate}")
        os.environ["VIRTUAL_ENV"] = venv_root
        os.environ["PATH"] = (
            os.path.join(venv_root, "bin") + ":" + os.environ.get("PATH", "")
        )
        os.execv(candidate, [candidate] + sys.argv)
    else:
        print(
            "Warning: Not running in a virtualenv. If you want tests to use a venv, create one at .venv and re-run the script."
        )

# Add repo root to sys.path so bench_lib can be imported
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from bench_lib.report import generate_report_html  # noqa: E402
from bench_lib.summary import generate_summary  # noqa: E402


def _metric(impl, fmt, label, bpp, ss, psnr, img="file.png"):
    axis = "quality"
    return {
        "name": f"{impl} ({fmt}, encode, {label}, t0, {img})",
        "impl": impl,
        "lang": "rust",
        "build": "rust",
        "label": label,
        "params": f"{axis}={label.split('-')[-1]}",
        "quality_axis": axis,
        "quality_value": label.split("-")[-1],
        "input_path": f"data/{img}",
        "source_path": f"data/{img}",
        "filesize": int(bpp * 1000),
        "ssimulacra2": ss,
        "psnr": psnr,
        "error": None,
        "type": "encode",
        "format": fmt,
        "width": 100,
        "height": 100,
        "megapixels": 0.01,
        "bpp": bpp,
    }


def test_timing_summary():
    """Perf timing summary from hyperfine-like JSON."""

    def result(command, mean, stddev):
        return {
            "command": command,
            "mean": mean,
            "stddev": stddev,
            "min": mean - stddev,
            "max": mean + stddev,
            "times": [mean, mean + stddev, mean - stddev],
        }

    data = {
        "results": [
            result("implA (jpeg, decode, perf, t1, file.jpg)", 0.050, 0.005),
            result("implA (jpeg, decode, perf, t0, file.jpg)", 0.030, 0.004),
            result("implB (jpeg, decode, perf, t1, file.jpg)", 0.080, 0.008),
            result("implB (jpeg, decode, perf, t0, file.jpg)", 0.045, 0.006),
        ]
    }
    tmpdir = os.path.join(repo_root, "results", "tmp_perf")
    os.makedirs(tmpdir, exist_ok=True)
    raw = os.path.join(tmpdir, "raw.json")
    with open(raw, "w") as f:
        json.dump(data, f)
    generate_summary(tmpdir, raw, None)
    files = os.listdir(tmpdir)
    assert any(f.endswith(".png") for f in files), "expected a timing plot"
    assert "summary.md" in files
    print("✓ timing summary:", sorted(files))


def test_quality_summary():
    """Quality summary: rate-distortion curves + BD-rate table from metrics."""
    metrics = []
    # Two JPEG implementations, a multi-point sweep so BD-rate is computable.
    sweep = [(0.3, 60.0, 30.0), (0.6, 78.0, 36.0), (1.0, 88.0, 41.0), (1.6, 94.0, 46.0)]
    for bpp, ss, psnr in sweep:
        metrics.append(
            _metric("libjpeg-turbo-encode", "jpeg", f"quality-{int(ss)}", bpp, ss, psnr)
        )
        # A slightly better competitor (same quality at lower bpp).
        metrics.append(
            _metric(
                "mozjpeg-encode", "jpeg", f"quality-{int(ss)}", bpp * 0.85, ss, psnr
            )
        )
    tmpdir = os.path.join(repo_root, "results", "tmp_quality")
    os.makedirs(tmpdir, exist_ok=True)
    generate_summary(tmpdir, None, metrics)
    files = os.listdir(tmpdir)
    txt = open(os.path.join(tmpdir, "summary.md")).read()
    assert any(f.startswith("rd_curve_") for f in files), "expected R-D curve plot"
    assert "Rate-Distortion" in txt
    assert "BD-rate" in txt, "expected BD-rate table"
    print("✓ quality summary:", sorted(files))


def test_report_html():
    """Self-contained report.html bundling perf + quality subfolders."""
    bundle = os.path.join(repo_root, "results", "tmp_bundle")
    perf = os.path.join(bundle, "performance")
    qual = os.path.join(bundle, "quality")
    os.makedirs(perf, exist_ok=True)
    os.makedirs(qual, exist_ok=True)
    # Minimal placeholder PNGs (content is irrelevant; report just base64s them).
    for p in (
        os.path.join(perf, "jpeg_encode_perf_results.png"),
        os.path.join(qual, "rd_curve_jpeg.png"),
    ):
        with open(p, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n placeholder")
    with open(os.path.join(qual, "metrics.json"), "w") as f:
        json.dump([], f)

    out = generate_report_html(bundle, generated_at="2026-01-01T00:00:00Z")
    html = open(out).read()
    assert "data:image/png;base64," in html, "images must be embedded, not linked"
    assert 'src="http' not in html and "src='http" not in html, "no external images"
    assert "Performance" in html and "Quality" in html
    print("✓ report.html:", os.path.basename(out))


def main():
    test_timing_summary()
    test_quality_summary()
    test_report_html()
    print("\nAll plot/report generation checks passed.")


if __name__ == "__main__":
    main()
