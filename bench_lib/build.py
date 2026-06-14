"""Compilation orchestration for Rust and C++ implementations."""

import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import chain

from colorama import Fore

from bench_lib.models import (
    IMPLEMENTATIONS,
    NULL_IMPLEMENTATIONS,
    ImageFormat,
    Implementation,
    safe_print,
)

# Project root (directory containing bench_lib/)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(PROJECT_ROOT, "vendor")
VENDOR_COMMON = os.path.join(VENDOR_DIR, "install", "common")
# Vendored build tools (nasm) install here. build_vendor.py sets this on PATH
# for its own subprocess; we re-apply it in this (parent) process so the cargo
# and C++ builds below also resolve the vendored nasm without a system install.
VENDOR_COMMON_BIN = os.path.join(VENDOR_COMMON, "bin")
VENDOR_LIBJPEG_TURBO = os.path.join(VENDOR_DIR, "install", "libjpeg-turbo")
VENDOR_MOZJPEG = os.path.join(VENDOR_DIR, "install", "mozjpeg")

# iqa-cli (the image-quality metrics CLI) is no longer vendored in-repo; it is
# installed from crates.io, pinned to a version that tracks the iqa crate. The
# binary is installed into target/bin (see install_iqa_cli) so it lives with the
# workspace build and is removed by `mise run clean` (which wipes target/).
IQA_CLI_VERSION = "0.2.0"

# Per-build-directory locks to prevent concurrent cmake/make in the same dir
_build_dir_locks: dict[str, threading.Lock] = {}
_build_dir_locks_lock = threading.Lock()

# Build directories already (re)built during this process. Multiple
# implementations (e.g. a codec's encode and decode binaries) share a build
# directory, and `make` builds every target in it, so the second implementation
# can skip. We track this per-run rather than checking whether the binary exists
# on disk, so that source or harness-header changes are still recompiled on the
# next `./bench run`.
_built_dirs: set[str] = set()
_built_dirs_lock = threading.Lock()


def _get_build_dir_lock(build_dir: str) -> threading.Lock:
    with _build_dir_locks_lock:
        if build_dir not in _build_dir_locks:
            _build_dir_locks[build_dir] = threading.Lock()
        return _build_dir_locks[build_dir]


def _drop_stale_cmake_cache(build_dir: str):
    """Remove CMakeCache.txt/CMakeFiles only when the cache was generated for a
    different source tree (e.g. the repo moved from /var/data/... to
    /var/home/...). Keeping a valid cache lets `make` rebuild incrementally and
    pick up changed sources without a full clean rebuild every run."""
    cmake_cache = os.path.join(build_dir, "CMakeCache.txt")
    cmake_files = os.path.join(build_dir, "CMakeFiles")
    if not os.path.exists(cmake_cache):
        return

    expected_src = os.path.abspath(os.path.dirname(build_dir))
    cached_src = None
    try:
        with open(cmake_cache) as f:
            for line in f:
                if line.startswith("CMAKE_HOME_DIRECTORY:"):
                    cached_src = line.split("=", 1)[1].strip()
                    break
    except OSError:
        cached_src = None

    if cached_src is None or os.path.abspath(cached_src) != expected_src:
        os.remove(cmake_cache)
        if os.path.exists(cmake_files):
            shutil.rmtree(cmake_files)


def run_build_command(command, cwd, step_name, max_lines=100, env=None):
    """Helper function to run commands with detailed error logging."""
    try:
        subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        print(f"\n    ✗ {step_name} failed")
        print(f"    Command: {' '.join(command)}")

        # Helper to print the tail of the log
        def print_log_tail(content, label):
            if content:
                lines = content.strip().split("\n")
                count = len(lines)
                # Get the last `max_lines` lines (where compiler errors usually live)
                tail = lines[-max_lines:]
                print(f"\n    --- {label} (Last {len(tail)} of {count} lines) ---")
                for line in tail:
                    print(f"      {line}")

        print_log_tail(e.stdout, "STDOUT")
        print_log_tail(e.stderr, "STDERR")
        print(f"\n✗ Build process aborted during {step_name}.")
        sys.exit(1)


def build_project(impl: Implementation):
    """Build a project by name."""
    if impl.build == "rust":
        build_rust_project(impl)
    elif impl.build == "cpp":
        build_cpp_project(impl)
    else:
        raise ValueError(f"Unknown build ecosystem: {impl.build}")


def build_rust_project(impl: Implementation):
    """
    Build a Rust project.

    Assumes the project language is Rust.
    """
    assert impl.build == "rust", "build_rust_project() called with non-Rust project"

    run_build_command(
        [
            "cargo",
            "build",
            "--release",
        ],  # Note: We don't have a granular way to determine the binary name, so we just build everything
        cwd=".",
        step_name=f"{impl.name} (Cargo Build)",
    )


def _get_vendor_prefix_path(impl_name: str) -> str:
    """Return semicolon-separated CMake prefix path for vendored libs."""
    if "libjpeg-turbo" in impl_name:
        return f"{VENDOR_COMMON};{VENDOR_LIBJPEG_TURBO}"
    if "mozjpeg" in impl_name:
        return f"{VENDOR_COMMON};{VENDOR_MOZJPEG}"
    return VENDOR_COMMON


def _get_pkg_config_path(prefix: str) -> str:
    """Build PKG_CONFIG_PATH from a vendor install prefix."""
    dirs = [
        os.path.join(prefix, "lib", "pkgconfig"),
        os.path.join(prefix, "lib64", "pkgconfig"),
        os.path.join(prefix, "share", "pkgconfig"),
    ]
    existing = os.environ.get("PKG_CONFIG_PATH", "")
    return os.pathsep.join(dirs + ([existing] if existing else []))


def build_cpp_project(impl):
    """Build a C++ project with thread-safe logging."""
    assert impl.build == "cpp", "build_cpp_project() called with non-C++ project"

    bin_path = impl.bin
    build_dir = os.path.dirname(bin_path)

    # Use a prefix so the user can track which thread is doing what
    prefix = f"[{impl.name}]".ljust(15)

    safe_print(f"{Fore.CYAN}{prefix} Starting build...")

    abs_build_dir = os.path.abspath(build_dir)
    build_lock = _get_build_dir_lock(abs_build_dir)
    try:
        with build_lock:
            # If this shared directory was already (re)built this run, skip:
            # `make` built every target in it, including this binary.
            with _built_dirs_lock:
                already_built = abs_build_dir in _built_dirs
            if already_built:
                safe_print(f"{Fore.GREEN}{prefix} ✓ Build complete (shared build dir).")
                return os.path.exists(bin_path)

            os.makedirs(build_dir, exist_ok=True)
            # Drop the CMake cache only if it points at a different source tree
            # (e.g. the repo moved); otherwise keep it for incremental rebuilds.
            _drop_stale_cmake_cache(build_dir)

            cmake_prefix = _get_vendor_prefix_path(impl.name)
            pkg_config_path = _get_pkg_config_path(VENDOR_COMMON)

            env = os.environ.copy()
            env["PKG_CONFIG_PATH"] = pkg_config_path

            # 1. Configure with CMake
            run_build_command(
                [
                    "cmake",
                    "..",
                    "-DCMAKE_BUILD_TYPE=Release",
                    "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                    "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
                    f"-DCMAKE_PREFIX_PATH={cmake_prefix}",
                ],
                cwd=build_dir,
                step_name=f"{impl.name} (CMake)",
                env=env,
            )

            # 2. Build with Make
            run_build_command(
                ["make", "-j"],
                cwd=build_dir,
                step_name=f"{impl.name} (Make)",
                env=env,
            )

            # 3. Verify
            if not os.path.exists(bin_path):
                safe_print(f"{Fore.RED}{prefix} ✗ Binary not found: {bin_path}")
                return False
            else:
                with _built_dirs_lock:
                    _built_dirs.add(abs_build_dir)
                safe_print(f"{Fore.GREEN}{prefix} ✓ Build complete.")
                return True

    except Exception as e:
        safe_print(f"{Fore.RED}{prefix} ✗ Build failed: {str(e)}")
        return False


def build_vendor_deps():
    """Build all vendored C/C++ libraries and ssimulacra2."""
    build_vendor_script = os.path.join(VENDOR_DIR, "build_vendor.py")
    try:
        subprocess.run(
            [sys.executable, build_vendor_script],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"\n✗ Vendor dependency build failed: {e}")
        sys.exit(1)


def install_iqa_cli(env):
    """Install the pinned iqa-cli binary from crates.io into target/bin.

    The published crate bundles the vendored C++/lcms2 sources, so this needs
    only a C++ toolchain (already required for the C++ implementations) — no git
    submodules. `cargo install` is a no-op when the pinned version is already
    present, so re-running `./bench compile` is cheap."""
    print(
        f"{Fore.BLUE}\n[Step 2b/3] Installing iqa-cli {IQA_CLI_VERSION} from crates.io..."
    )
    try:
        subprocess.run(
            [
                "cargo",
                "install",
                "iqa-cli",
                "--version",
                f"={IQA_CLI_VERSION}",
                "--locked",
                "--root",
                os.path.join(PROJECT_ROOT, "target"),
            ],
            check=True,
            env=env,
        )
        print("  ✓ iqa-cli installed")
    except subprocess.CalledProcessError as e:
        print(f"  ✗ iqa-cli install failed: {e}")
        sys.exit(1)


def build_projects(formats: list[ImageFormat]):
    """Build Rust and C++ projects."""

    print(f"{Fore.BLUE}{'=' * 70}\nBUILDING PROJECTS\n{'=' * 70}")

    # Build vendored C/C++ libraries and ssimulacra2
    print(f"{Fore.BLUE}\n[Step 1/3] Building vendored dependencies...")
    build_vendor_deps()

    # build_vendor_deps() runs in a child process, so its PATH change does not
    # reach here. Re-apply it so the cargo / C++ builds below also see the
    # vendored nasm (e.g. any nasm-rs-based crate).
    if os.path.isdir(VENDOR_COMMON_BIN):
        _path = os.environ.get("PATH", "")
        os.environ["PATH"] = VENDOR_COMMON_BIN + os.pathsep + _path

    # Build Rust projects
    print(f"{Fore.BLUE}\n[Step 2/3] Building Rust projects...")
    try:
        env = os.environ.copy()
        env["RUSTFLAGS"] = "-C target-cpu=native"
        subprocess.run(["cargo", "build", "--release"], check=True, env=env)
        print("  ✓ Rust build complete")
    except subprocess.CalledProcessError as e:
        print(f"  ✗ Rust build failed: {e}")
        sys.exit(1)

    # Install the published iqa-cli metrics binary (not built from in-repo source).
    install_iqa_cli(env)

    # Build C++ projects
    print(f"{Fore.BLUE}\n[Step 3/3] Building C++ projects...")

    seen: set[str] = set()
    to_build = []
    for fmt in formats:
        for impl in chain(
            NULL_IMPLEMENTATIONS,
            (impl for impl in IMPLEMENTATIONS if impl.format == fmt),
        ):
            # Secondary-knob variants reuse their base's binary (no build target of
            # their own), so skip them here — the base impl already builds the dir.
            if impl.build == "cpp" and not impl.is_variant and impl.name not in seen:
                seen.add(impl.name)
                to_build.append(impl)
    num_workers = min(len(to_build), 3) if to_build else 1
    print(f"{Fore.YELLOW}Starting {num_workers} simultaneous C++ builds...\n")
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        results = list(executor.map(build_cpp_project, to_build))

    # Check results
    if all(results):
        print(f"\n{Fore.GREEN}All C++ builds succeeded.")
    else:
        print(f"\n{Fore.RED}Some C++ builds failed.")
        sys.exit(1)

    print("\n✓ Build phase complete\n")
