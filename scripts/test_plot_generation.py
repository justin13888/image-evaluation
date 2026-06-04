#!/usr/bin/env python3
"""
Sanity test for plot generation in bench.generate_summary.

This script constructs a small hyperfine-like JSON file and calls
bench_lib.summary.generate_summary to verify the creation of separate plot files.
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
        # Ensure the new process inherits a VIRTUAL_ENV env var and updated PATH
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

from bench_lib.summary import generate_summary  # noqa: E402


def main():
    # Minimal hyperfine JSON structure. Command names use the 5-field form
    # produced by BenchmarkTask.name(): "impl (fmt, type, label, tN, basename)"
    # where `label` is the operating-point token. Two operating points and both
    # thread modes exercise the swept aggregation key (grouped single-vs-parallel
    # bars + a per-operating-point chart).
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
            result("implA (jpeg, encode, q90, t0, file.jpg)", 0.060, 0.005),
            result("implB (jpeg, encode, q90, t0, file.jpg)", 0.095, 0.009),
        ]
    }

    tmpdir = os.path.join(os.path.dirname(__file__), "../results/tmp")
    os.makedirs(tmpdir, exist_ok=True)
    raw_json_path = os.path.join(tmpdir, "raw.json")
    with open(raw_json_path, "w") as f:
        json.dump(data, f)

    # Call generate_summary which should create PNGs and summary.md
    generate_summary(tmpdir, raw_json_path, None)

    # Check result files
    print("Directory contents:")
    files = sorted(os.listdir(tmpdir))
    for fname in files:
        print(" -", fname)

    # Ensure at least one PNG (plot) and the summary exist
    pngs = [f for f in files if f.endswith(".png")]
    assert len(pngs) >= 1, "Expected at least one plot PNG to be generated"

    assert "summary.md" in files, "Expected summary.md to be generated"


if __name__ == "__main__":
    main()
