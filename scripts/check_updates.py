#!/usr/bin/env python3
"""
Report benchmarked implementations that have a newer upstream release.

The two dependency sources need different methods (issue #67):
  - Vendored git submodules are pinned to release tags, so the pinned commit is
    matched to its tag via `git ls-remote --tags` and compared against the newest
    stable tag sharing that tag's prefix (`v`, `nasm-`, or none).
  - The crates.io codecs each implementations/rust/* crate benchmarks
    (BENCHMARKED_CRATES) are resolved through Cargo.lock and compared against the
    newest release in the crates.io sparse index (the newest stable one, unless
    the locked version is itself a pre-release).

Stdlib only, so it runs on a bare CI runner without uv, Rust, or submodules.

Exit status: 0 = everything current, 1 = something is outdated,
2 = a lookup failed (the report is incomplete).
"""

import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import tomllib
import traceback
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Datasets, not implementations: no release cadence to track.
SKIPPED_SUBMODULES = {"vendor/codec-corpus"}

# libjpeg-turbo tags its betas x.y.90 / x.y.91 rather than with a suffix.
BETA_TAG_RULES: dict[str, Callable[[tuple[int, ...]], bool]] = {
    "vendor/libjpeg-turbo": lambda v: len(v) >= 3 and v[2] >= 90,
}

# The crates.io codecs each Rust implementation benchmarks. A bare name is a
# dependency of the implementation crate itself; `parent/name` is the version of
# `name` that `parent` resolves to (image's codec backends). Helper crates
# (anyhow, the pnm reader, pixel conversion) are deliberately absent.
BENCHMARKED_CRATES: dict[str, list[str]] = {
    "image-jpeg": ["image", "image/zune-jpeg"],
    "image-png": ["image", "image/png"],
    "image-webp": ["image", "image/image-webp"],
    "jpeg-decoder": ["jpeg-decoder"],
    "jpeg-encoder": ["jpeg-encoder"],
    "jxl-oxide": ["jxl-oxide"],
    "oxipng": ["oxipng"],
    "rav1e": ["rav1e"],
    "zenjpeg": ["zenjpeg"],
    "zenpng": ["zenpng"],
    "zenwebp": ["zenwebp"],
    "zune-jpeg": ["zune-jpeg"],
    "zune-jpegxl": ["zune-jpegxl"],
    "zune-png": ["zune-png"],
}
# Implementations with no crates.io codec: the null baseline, and jxl-rs, which
# is a path dependency on the vendor/jxl-rs submodule (checked as a submodule).
NO_CRATES_IO_CODEC = {"null", "jxl-rs"}

SPARSE_INDEX = "https://index.crates.io/{}"
USER_AGENT = (
    "image-evaluation dependency freshness check "
    "(https://github.com/justin13888/image-evaluation)"
)
NETWORK_TIMEOUT_S = 60

# A release tag is an optional non-numeric prefix followed by a dotted version
# and nothing else, so `v1.6.0-rc1` / `1.5.0beta` are excluded as pre-releases.
RELEASE_TAG = re.compile(r"^(?P<prefix>[^0-9]*)(?P<version>\d+(?:\.\d+)*)$")


@dataclass
class Row:
    source: str
    name: str
    pinned: str
    latest: str
    used_by: list[str] = field(default_factory=list)

    @property
    def outdated(self) -> bool:
        return self.pinned != self.latest


# --- Submodules ---


def parse_release_tag(tag: str) -> tuple[str, tuple[int, ...]] | None:
    """Split a release tag into (prefix, numeric version), or None if it is not one."""
    m = RELEASE_TAG.match(tag)
    if not m:
        return None
    return m["prefix"], tuple(int(p) for p in m["version"].split("."))


def parse_ls_remote(output: str) -> dict[str, str]:
    """Map tag name -> commit SHA from `git ls-remote --tags` output.

    Annotated tags list both the tag object and the peeled commit (`^{}`); the
    peeled commit wins, since that is what a submodule gitlink records.
    """
    tags: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in output.splitlines():
        sha, _, ref = line.partition("\t")
        if not ref.startswith("refs/tags/"):
            continue
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            peeled[name.removesuffix("^{}")] = sha
        else:
            tags[name] = sha
    tags.update(peeled)
    return tags


def pinned_release_tag(sha: str, tags: dict[str, str]) -> str | None:
    """The release tag pointing at `sha`, preferring the highest if several do."""
    candidates = [
        (parsed[1], tag)
        for tag, tag_sha in tags.items()
        if tag_sha == sha and (parsed := parse_release_tag(tag))
    ]
    return max(candidates)[1] if candidates else None


def latest_release_tag(
    prefix: str,
    tags: dict[str, str],
    is_beta: Callable[[tuple[int, ...]], bool] = lambda _: False,
) -> str:
    """The newest non-beta release tag sharing `prefix`."""
    family = [
        (parsed[1], tag)
        for tag in tags
        if (parsed := parse_release_tag(tag))
        and parsed[0] == prefix
        and not is_beta(parsed[1])
    ]
    return max(family)[1]


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        # Fail instead of hanging on a credential prompt for an unreachable remote.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        check=True,
        capture_output=True,
        text=True,
        timeout=NETWORK_TIMEOUT_S,
    ).stdout


def list_submodules() -> list[tuple[str, str]]:
    """(path, url) for every submodule in .gitmodules, in file order."""
    parser = configparser.ConfigParser()
    parser.read(os.path.join(REPO_ROOT, ".gitmodules"))
    submodules = [
        (parser[section]["path"], parser[section]["url"])
        for section in parser.sections()
        if section.startswith("submodule ")
    ]
    # ConfigParser ignores a missing file; finding nothing must not read as current.
    if not submodules:
        raise LookupError("no submodules found in .gitmodules")
    return submodules


def check_submodule(path: str, url: str) -> Row:
    # The gitlink in HEAD's tree, so no submodule checkout is needed.
    sha = git("ls-tree", "HEAD", path).split()[2]
    tags = parse_ls_remote(git("ls-remote", "--tags", url))
    pinned = pinned_release_tag(sha, tags)
    if pinned is None:
        raise LookupError(f"pinned commit {sha[:12]} is not a release tag")
    prefix, _ = parse_release_tag(pinned)  # type: ignore[misc]
    is_beta = BETA_TAG_RULES.get(path, lambda _: False)
    return Row("submodule", path, pinned, latest_release_tag(prefix, tags, is_beta))


# --- crates.io codecs ---


def semver_key(version: str) -> tuple:
    """Semver precedence key: a pre-release sorts below its release, and dotted
    pre-release identifiers compare numerically when numeric (rc.9 < rc.10)."""
    core, _, pre = version.partition("+")[0].partition("-")
    nums = tuple(int(p) for p in core.split("."))
    if not pre:
        return nums, 1, ()
    idents = tuple(
        (0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")
    )
    return nums, 0, idents


def is_prerelease(version: str) -> bool:
    return "-" in version.partition("+")[0]


def locked_version(lock: dict, parent: str, dep: str) -> str:
    """The version of `dep` that Cargo.lock resolved for package `parent`.

    A lock dependency entry is `name` when only one version of the crate is in the
    lock, and `name version` when several are.
    """
    (pkg,) = [p for p in lock["package"] if p["name"] == parent]
    for entry in pkg.get("dependencies", []):
        name, _, version = entry.partition(" ")
        if name != dep:
            continue
        if version:
            return version.split(" ")[0]
        (only,) = [p["version"] for p in lock["package"] if p["name"] == dep]
        return only
    raise LookupError(f"{parent}: {dep} is not in Cargo.lock")


def resolve_crate(lock: dict, package: str, spec: str) -> tuple[str, str]:
    """(crate, locked version) for a BENCHMARKED_CRATES entry of `package`."""
    parent, _, crate = spec.rpartition("/")
    return crate, locked_version(lock, parent or package, crate)


def collect_crate_pins() -> dict[tuple[str, str], list[str]]:
    """(crate, locked version) -> implementation dirs that benchmark it."""
    with open(os.path.join(REPO_ROOT, "Cargo.lock"), "rb") as f:
        lock = tomllib.load(f)
    impl_root = os.path.join(REPO_ROOT, "implementations", "rust")
    impls = sorted(
        d
        for d in os.listdir(impl_root)
        if os.path.exists(os.path.join(impl_root, d, "Cargo.toml"))
    )
    unlisted = set(impls) - BENCHMARKED_CRATES.keys() - NO_CRATES_IO_CODEC
    if unlisted:
        raise LookupError(
            f"implementations/rust/{{{','.join(sorted(unlisted))}}} missing from "
            "BENCHMARKED_CRATES in scripts/check_updates.py"
        )
    pins: dict[tuple[str, str], list[str]] = {}
    for impl in impls:
        with open(os.path.join(impl_root, impl, "Cargo.toml"), "rb") as f:
            package = tomllib.load(f)["package"]["name"]
        for spec in BENCHMARKED_CRATES.get(impl, []):
            pins.setdefault(resolve_crate(lock, package, spec), []).append(impl)
    return pins


def sparse_index_path(name: str) -> str:
    """The crate's file path in the crates.io sparse index."""
    name = name.lower()
    if len(name) <= 2:
        return f"{len(name)}/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[:2]}/{name[2:4]}/{name}"


def latest_crate_version(index_lines: list[str], locked: str) -> str:
    """Newest non-yanked release to compare `locked` against: the newest stable
    one, or the newest of any kind when `locked` is itself a pre-release."""
    versions = [
        entry["vers"]
        for line in index_lines
        if line.strip() and not (entry := json.loads(line))["yanked"]
    ]
    if not is_prerelease(locked):
        versions = [v for v in versions if not is_prerelease(v)]
    return max([*versions, locked], key=semver_key)


def check_crate(name: str, locked: str, used_by: list[str]) -> Row:
    # The sparse index is a static CDN, unlike the rate-limited crates.io API.
    req = urllib.request.Request(
        SPARSE_INDEX.format(sparse_index_path(name)),
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_S) as resp:
        lines = resp.read().decode().splitlines()
    return Row("crate", name, locked, latest_crate_version(lines, locked), used_by)


# --- Report ---


def render_report(rows: list[Row], errors: list[str]) -> str:
    outdated = [r for r in rows if r.outdated]
    lines = ["## Outdated benchmarked implementations", ""]
    if outdated:
        lines += [
            "| Source | Dependency | Pinned | Latest | Used by |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {r.source} | `{r.name}` | {r.pinned} | {r.latest} | "
            f"{', '.join(r.used_by) or '—'} |"
            for r in outdated
        ]
    else:
        lines.append("Every checked dependency is on its latest release.")
    lines += ["", f"{len(rows)} dependencies checked, {len(outdated)} outdated."]
    if errors:
        lines += ["", "### Lookups that failed", ""]
        lines += [f"- {e}" for e in errors]
    return "\n".join(lines) + "\n"


def run_checks() -> tuple[list[Row], list[str]]:
    jobs = [
        (check_submodule, (path, url))
        for path, url in list_submodules()
        if path not in SKIPPED_SUBMODULES
    ]
    jobs += [
        (check_crate, (name, version, used_by))
        for (name, version), used_by in sorted(collect_crate_pins().items())
    ]

    rows: list[Row] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [(pool.submit(fn, *a), a[0]) for fn, a in jobs]
        for future, name in futures:
            try:
                rows.append(future.result())
            except Exception as e:  # report every failure, never treat it as current
                errors.append(f"`{name}`: {e}")
    return rows, errors


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Report benchmarked implementations with a newer upstream release."
    )
    ap.add_argument("--output", help="also write the markdown report to this file")
    args = ap.parse_args()
    try:
        return run(args.output)
    except Exception:
        # An uncaught exception would exit 1, which reads as "outdated".
        traceback.print_exc()
        return 2


def run(output: str | None) -> int:
    rows, errors = run_checks()
    report = render_report(rows, errors)
    print(report, end="")
    if output:
        with open(output, "w") as f:
            f.write(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(report)
    if os.environ.get("GITHUB_ACTIONS"):
        for r in rows:
            if r.outdated:
                print(f"::warning::{r.name} {r.pinned} -> {r.latest}")

    if errors:
        return 2
    return 1 if any(r.outdated for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
