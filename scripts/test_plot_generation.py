#!/usr/bin/env python3
"""
Sanity test for plot generation and report bundling.

Constructs small synthetic inputs and exercises:
  - bench_lib.summary.generate_summary on hyperfine-like timing JSON (perf),
  - generate_summary on quality metrics (tables-only: BD-rate + Pareto front),
  - bench_lib.report.generate_report_html on a fake bundle: perf charts stay
    base64 PNGs, the quality suite is interactive with the raw metrics embedded.
"""

import os
import re
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


def _metric(
    impl, fmt, label, bpp, ss, psnr, ssim=None, butteraugli=None, img="file.png"
):
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
        "ssim": ssim,
        "butteraugli": butteraugli,
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
    """Quality summary is tables-only now (interactive curves live in
    report.html): BD-rate + Pareto front + a link to report.html, and no chart
    PNGs are emitted for the quality suite."""
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
    assert not any(
        f.startswith(("rd_curve_", "impl_comparison_", "format_comparison"))
        for f in files
    ), "quality suite must not pre-render chart PNGs anymore"
    assert "BD-rate" in txt, "expected BD-rate table"
    assert "Pareto front" in txt, "expected Pareto best-of-format table"
    assert "report.html" in txt, "summary should point to the interactive report"
    print("✓ quality summary (tables-only):", sorted(files))


def test_report_html():
    """Self-contained report.html: perf charts stay base64 PNGs; the quality
    suite is interactive with the raw metrics embedded inline (recomputable)."""
    bundle = os.path.join(repo_root, "results", "tmp_bundle")
    perf = os.path.join(bundle, "performance")
    qual = os.path.join(bundle, "quality")
    os.makedirs(perf, exist_ok=True)
    os.makedirs(qual, exist_ok=True)
    # Minimal placeholder perf PNG (content irrelevant; report just base64s it).
    with open(os.path.join(perf, "jpeg_encode_perf_results.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n placeholder")
    # Real quality metrics (incl. SSIM higher-better and Butteraugli lower-better)
    # so all four metric series are exercised in the interactive path. Two
    # distinct images per operating point so the aggregation path (mean across
    # >1 image) and the distinct-image count are exercised.
    metrics = []
    for img in ("img_a.png", "img_b.png"):
        metrics += [
            _metric(
                "libjpeg-turbo-encode",
                "jpeg",
                "quality-60",
                0.30,
                60.0,
                30.0,
                0.95,
                2.1,
                img=img,
            ),
            _metric(
                "libjpeg-turbo-encode",
                "jpeg",
                "quality-90",
                1.00,
                90.0,
                42.0,
                0.99,
                0.6,
                img=img,
            ),
            _metric(
                "mozjpeg-encode",
                "jpeg",
                "quality-60",
                0.25,
                60.0,
                30.0,
                0.95,
                2.0,
                img=img,
            ),
            _metric(
                "mozjpeg-encode",
                "jpeg",
                "quality-90",
                0.85,
                90.0,
                42.0,
                0.99,
                0.5,
                img=img,
            ),
        ]
    # A legitimate low-quality point with negative SSIMULACRA2 — must survive.
    metrics.append(
        _metric(
            "jpeg-encoder-encode",
            "jpeg",
            "quality-10",
            1.10,
            -40.0,
            11.0,
            0.40,
            12.0,
            img="img_a.png",
        )
    )
    with open(os.path.join(qual, "metrics.json"), "w") as f:
        json.dump(metrics, f)
    # A manifest carrying the run's benchmark_config so the report can describe
    # the dataset (name + source link) and run configuration.
    with open(os.path.join(qual, "manifest.json"), "w") as f:
        json.dump(
            {
                "benchmark_config": {
                    "suite": "quality",
                    "dataset": "div2k",
                    "dataset_description": "DIV2K selected subset",
                    "dataset_homepage": "https://data.vision.ee.ethz.ch/cvl/DIV2K/",
                    "sample": None,
                    "formats": ["jpeg"],
                    "mode": "both",
                    "quality_steps": None,
                    "quick": False,
                }
            },
            f,
        )

    out = generate_report_html(bundle, generated_at="2026-01-01T00:00:00Z")
    html = open(out).read()
    # Perf charts remain embedded images; nothing is loaded externally.
    assert "data:image/png;base64," in html, "perf images must be embedded"
    assert 'src="http' not in html and "src='http" not in html, "no external images"
    assert "Performance" in html and "Quality" in html
    # Quality is interactive: raw data embedded + the chart engine inlined.
    assert 'id="quality-metrics"' in html, "raw metrics must be embedded inline"
    assert "quality-app" in html and "renderRDChart" in html, "chart engine inlined"
    embedded = json.loads(
        re.search(
            r'<script id="quality-metrics" type="application/json">(.*?)</script>',
            html,
            re.S,
        ).group(1)
    )
    assert len(embedded) == len(metrics), "all raw rows must round-trip into the report"
    assert any(r["ssimulacra2"] < 0 for r in embedded), (
        "negative-score tail must survive"
    )
    assert all("ssim" in r and "butteraugli" in r for r in embedded), (
        "ssim + butteraugli must round-trip into the report"
    )
    assert "Butteraugli" in html, "Butteraugli metric must be wired into the report"
    assert "q-metric-grid" in html, (
        "per-format section must render the all-metrics small-multiples grid"
    )
    # Dataset & run configuration: the report must describe what was benchmarked
    # and link to the dataset's source.
    assert "Dataset &amp; run configuration" in html, (
        "report must surface the dataset / run configuration"
    )
    assert "div2k" in html, "dataset name must be shown"
    assert "https://data.vision.ee.ethz.ch/cvl/DIV2K/" in html, (
        "dataset source link must be shown"
    )
    assert "jpeg" in html and "Formats" in html, "run formats must be shown"
    # Aggregation disclosure: the mount point exists and the engine knows how to
    # state mean-across-N-images vs single-image, with known-range axes.
    assert "id='q-aggregation'" in html or 'id="q-aggregation"' in html, (
        "aggregation note mount point must be present"
    )
    assert "arithmetic average" in html, "aggregation note must explain the mean"
    assert "hardHi" in html, "known-range axis scaling must be wired into the engine"
    print("✓ report.html (interactive quality):", os.path.basename(out))


def test_variant_series_roundtrip():
    """Secondary-knob variants (issue #4) are distinct series: their ``base@tag``
    impl name must survive the ``BenchmarkTask.name()`` -> ``_parse_command_name``
    round-trip (so report/summary recover the right series, not a corrupted base
    curve), and ``schema_for`` must resolve them with the override folded in."""
    from bench_lib.models import IMPLEMENTATIONS, BenchmarkTask, schema_for
    from bench_lib.summary import _parse_command_name

    assert [i for i in IMPLEMENTATIONS if i.variant_kind == "curated"], (
        "expected curated variants from _expand_variants()"
    )
    assert [i for i in IMPLEMENTATIONS if i.variant_kind == "oat"], (
        "expected one-at-a-time (oat) variants from _expand_variants()"
    )
    impl = next(i for i in IMPLEMENTATIONS if i.name == "libjxl-encode@progressive-on")
    schema = schema_for(impl.name)
    assert schema.quality_axis == "distance", "variant keeps the base quality axis"
    assert schema.perf_preset.get("progressive") == "1", "override folded into preset"
    task = BenchmarkTask(
        impl=impl,
        params=schema.quality_params("1.0"),
        label="distance-1.0",
        input_path="data/img.png",
        source_path="data/img.png",
        iterations=1,
        warmup=0,
        threads=1,
        discard_output=False,
        measure_memory=False,
        pin_cores=False,
    )
    parsed = _parse_command_name(task.name())
    assert parsed is not None, f"variant task name did not parse: {task.name()!r}"
    assert parsed["impl"] == "libjxl-encode@progressive-on", parsed
    assert parsed["format"] == "jxl" and parsed["type"] == "encode", parsed
    assert parsed["label"] == "distance-1.0" and parsed["threads"] == 1, parsed
    print("✓ variant series round-trip:", parsed["impl"])


def test_tunables_doc_in_sync():
    """docs/tunables.md must match what the schemas generate — the overview is the
    single in-code source of truth, so drift fails CI (run ./bench docs to fix)."""
    from bench_lib.tunables_doc import render_tunables_markdown

    path = os.path.join(repo_root, "docs", "tunables.md")
    assert os.path.exists(path), "docs/tunables.md missing — run ./bench docs"
    committed = open(path).read()
    assert committed == render_tunables_markdown(), (
        "docs/tunables.md is out of sync with TUNABLE_SCHEMAS; run ./bench docs"
    )
    print("✓ tunables overview in sync with TUNABLE_SCHEMAS")


def main():
    test_timing_summary()
    test_quality_summary()
    test_report_html()
    test_variant_series_roundtrip()
    test_tunables_doc_in_sync()
    print("\nAll plot/report generation checks passed.")


if __name__ == "__main__":
    main()
