#!/usr/bin/env python3
"""
Offline tests for scripts/check_updates.py: tag parsing and selection, semver
ordering, sparse-index and lockfile resolution, and report rendering. No network.
"""

import json
import os
import sys
import tempfile
import tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_updates  # noqa: E402
from check_updates import (  # noqa: E402
    BENCHMARKED_CRATES,
    BETA_TAG_RULES,
    NO_CRATES_IO_CODEC,
    Row,
    latest_crate_version,
    latest_release_tag,
    locked_version,
    parse_ls_remote,
    parse_release_tag,
    pinned_release_tag,
    render_report,
    resolve_crate,
    semver_key,
    sparse_index_path,
)

LS_REMOTE = "\n".join(
    [
        "aaa1\trefs/tags/v1.6.0",  # annotated: tag object ...
        "c0ff\trefs/tags/v1.6.0^{}",  # ... and the peeled commit
        "bbb2\trefs/tags/v1.10.0",
        "ccc3\trefs/tags/v1.11.0-rc1",
        "ddd4\trefs/tags/v2.0.0beta",
        "eee5\trefs/tags/1.99.0",  # different prefix family
        "fff6\trefs/heads/main",
    ]
)


def test_release_tags():
    assert parse_release_tag("v1.6.0") == ("v", (1, 6, 0))
    assert parse_release_tag("1.5.3") == ("", (1, 5, 3))
    assert parse_release_tag("3.1.4.1") == ("", (3, 1, 4, 1))
    assert parse_release_tag("nasm-2.16.03") == ("nasm-", (2, 16, 3))
    for pre in ["v1.11.0-rc1", "v2.0.0beta", "1.0.0-alpha.1", "latest"]:
        assert parse_release_tag(pre) is None, pre

    tags = parse_ls_remote(LS_REMOTE)
    assert tags["v1.6.0"] == "c0ff", "peeled commit must win over the tag object"
    assert "main" not in tags
    assert pinned_release_tag("c0ff", tags) == "v1.6.0"
    assert pinned_release_tag("ccc3", tags) is None, "pre-release pins are not releases"
    assert pinned_release_tag("9999", tags) is None
    # aom tags one commit both v3.14.1-rc1 and v3.14.1: the release wins.
    shared = {"v3.14.1-rc1": "abc", "v3.14.1": "abc", "v3.14": "abc"}
    assert pinned_release_tag("abc", shared) == "v3.14.1"
    # Numeric, not lexicographic (1.10 > 1.6), pre-releases skipped, prefix-scoped.
    assert latest_release_tag("v", tags) == "v1.10.0"
    assert latest_release_tag("", tags) == "1.99.0"

    # libjpeg-turbo betas are x.y.90+, with no suffix to exclude them by.
    turbo = {t: t for t in ["3.1.4.1", "3.1.90", "3.2.0", "3.2.90", "3.2.91"]}
    is_beta = BETA_TAG_RULES["vendor/libjpeg-turbo"]
    assert latest_release_tag("", turbo, is_beta) == "3.2.0"
    assert latest_release_tag("", turbo) == "3.2.91"
    print("✓ release tags: parsing, peeling, family-scoped selection, beta rules")


def index_line(vers, yanked=False):
    return json.dumps({"name": "demo", "vers": vers, "yanked": yanked})


def test_semver():
    assert semver_key("0.5.0-rc1") < semver_key("0.5.0") < semver_key("0.5.1")
    assert semver_key("1.0.9") < semver_key("1.0.10")
    assert semver_key("1.0.0-rc.9") < semver_key("1.0.0-rc.10")
    assert semver_key("1.0.0-alpha") < semver_key("1.0.0-alpha.1")
    assert semver_key("1.0.0-1") < semver_key("1.0.0-alpha"), "numeric < alphanumeric"
    assert semver_key("1.2.3+build") == semver_key("1.2.3")

    index = [
        index_line("0.5.0-rc1"),
        index_line("0.5.2"),
        index_line("0.5.10"),
        index_line("0.6.0", yanked=True),
        index_line("0.7.0-rc.1"),
        "",
    ]
    assert latest_crate_version(index, "0.5.2") == "0.5.10"
    assert latest_crate_version(index, "0.5.0-rc1") == "0.7.0-rc.1"
    assert latest_crate_version([index_line("0.1.0-alpha")], "0.1.0-alpha") == (
        "0.1.0-alpha"
    )
    # A locked version newer than the index (e.g. a stale CDN) is never "outdated".
    assert latest_crate_version(index, "0.5.11") == "0.5.11"

    assert sparse_index_path("a") == "1/a"
    assert sparse_index_path("cc") == "2/cc"
    assert sparse_index_path("png") == "3/p/png"
    assert sparse_index_path("Zune-JPEG") == "zu/ne/zune-jpeg"
    print("✓ semver: precedence, sparse index, stable/pre-release comparison target")


def test_lockfile_resolution():
    lock = tomllib.loads(
        """
        [[package]]
        name = "bench-demo"
        version = "0.1.0"
        dependencies = ["anyhow", "image", "zune-jpeg 0.5.5"]

        [[package]]
        name = "anyhow"
        version = "1.0.100"
        source = "registry+https://github.com/rust-lang/crates.io-index"

        [[package]]
        name = "image"
        version = "0.25.9"
        source = "registry+https://github.com/rust-lang/crates.io-index"
        dependencies = ["png", "zune-jpeg 0.5.5"]

        [[package]]
        name = "png"
        version = "0.18.0"
        source = "registry+https://github.com/rust-lang/crates.io-index"

        [[package]]
        name = "zune-jpeg"
        version = "0.4.21"
        source = "registry+https://github.com/rust-lang/crates.io-index"

        [[package]]
        name = "zune-jpeg"
        version = "0.5.5"
        source = "registry+https://github.com/rust-lang/crates.io-index"
        """
    )
    assert locked_version(lock, "bench-demo", "anyhow") == "1.0.100"
    assert locked_version(lock, "bench-demo", "zune-jpeg") == "0.5.5"
    assert resolve_crate(lock, "bench-demo", "image") == ("image", "0.25.9")
    # A codec image pulls in resolves under image, not the implementation crate.
    assert resolve_crate(lock, "bench-demo", "image/png") == ("png", "0.18.0")
    assert resolve_crate(lock, "bench-demo", "image/zune-jpeg") == (
        "zune-jpeg",
        "0.5.5",
    )
    expect_lookup_error(
        lambda: locked_version(lock, "bench-demo", "missing"), "an unlocked dependency"
    )
    print("✓ lockfile: benchmarked crates resolve to their locked versions")


def expect_lookup_error(fn, what):
    try:
        fn()
    except LookupError:
        pass
    else:
        raise AssertionError(f"{what} must be a lookup failure")


def test_benchmarked_crates_cover_repo():
    # Every listed crate resolves against the real lockfile, and every Rust
    # implementation is either listed or deliberately excluded.
    pins = check_updates.collect_crate_pins()
    impls = {i for used_by in pins.values() for i in used_by}
    assert impls == set(BENCHMARKED_CRATES), impls ^ set(BENCHMARKED_CRATES)
    assert not NO_CRATES_IO_CODEC & set(BENCHMARKED_CRATES)
    crates = {name for name, _ in pins}
    assert {"png", "zune-jpeg", "image"} <= crates
    assert not {"anyhow", "bytemuck", "zune-core"} & crates, "helpers are not codecs"
    print(
        f"✓ BENCHMARKED_CRATES: {len(pins)} codec pins cover the Rust implementations"
    )


def test_incomplete_repo_is_a_failure():
    # Finding nothing, or an implementation nobody listed, must never read as
    # "everything current".
    real_root = check_updates.REPO_ROOT
    with tempfile.TemporaryDirectory() as root:
        check_updates.REPO_ROOT = root
        try:
            expect_lookup_error(check_updates.list_submodules, "a missing .gitmodules")
            impl = os.path.join(root, "implementations", "rust", "new-codec")
            os.makedirs(impl)
            with open(os.path.join(impl, "Cargo.toml"), "w") as f:
                f.write('[package]\nname = "bench-new-codec"\n')
            with open(os.path.join(root, "Cargo.lock"), "w") as f:
                f.write("version = 4\n")
            expect_lookup_error(
                check_updates.collect_crate_pins, "an unlisted implementation"
            )
        finally:
            check_updates.REPO_ROOT = real_root
    print("✓ incomplete repo: missing submodules / unlisted implementation fail")


def test_report():
    rows = [
        Row("submodule", "vendor/dav1d", "1.5.3", "1.5.4"),
        Row("crate", "rav1e", "0.8.1", "0.8.1", ["rav1e"]),
    ]
    report = render_report(rows, ["`vendor/aom`: timed out"])
    assert "| submodule | `vendor/dav1d` | 1.5.3 | 1.5.4 | — |" in report
    assert "rav1e" not in report, "current dependencies are not listed"
    assert "2 dependencies checked, 1 outdated." in report
    assert "- `vendor/aom`: timed out" in report

    current = render_report([rows[1]], [])
    assert "Every checked dependency is on its latest release." in current
    assert "Lookups that failed" not in current
    print("✓ report: outdated rows, totals, failures")


def main():
    test_release_tags()
    test_semver()
    test_lockfile_resolution()
    test_benchmarked_crates_cover_repo()
    test_incomplete_repo_is_a_failure()
    test_report()
    print("\nAll dependency freshness checks passed.")


if __name__ == "__main__":
    main()
