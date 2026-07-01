"""System information collection for reproducibility manifests."""

import datetime
import glob
import os
import platform
import subprocess
from typing import Dict, Any, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_COMMON = os.path.join(PROJECT_ROOT, "vendor", "install", "common")
VENDOR_MOZJPEG = os.path.join(PROJECT_ROOT, "vendor", "install", "mozjpeg")
VENDOR_LIBJPEG_TURBO = os.path.join(PROJECT_ROOT, "vendor", "install", "libjpeg-turbo")


def get_system_info() -> Dict[str, Any]:
    """Collect system information for reproducibility manifest."""
    info = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "os": f"{platform.system()} {platform.release()}",
        "kernel": platform.version(),
        "cpu": "unknown",
        "cores": os.cpu_count() or 0,
    }

    # Try to get CPU info
    try:
        if platform.system() == "Darwin":
            cpu = (
                subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"])
                .decode()
                .strip()
            )
            info["cpu"] = cpu
        elif platform.system() == "Linux":
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        info["cpu"] = line.split(":")[1].strip()
                        break
    except Exception:
        pass

    return info


def get_git_info() -> Dict[str, Any]:
    """Best-effort git provenance for the run: the commit that produced it and
    whether the working tree had uncommitted changes. Empty dict outside a git
    checkout (or when git is unavailable)."""

    def _git(*args: str) -> str:
        return (
            subprocess.check_output(
                ["git", *args], cwd=PROJECT_ROOT, stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )

    try:
        commit = _git("rev-parse", "HEAD")
    except Exception:
        return {}
    try:
        dirty = bool(_git("status", "--porcelain"))
    except Exception:
        dirty = False
    return {"commit": commit, "dirty": dirty}


def get_physical_cores() -> int:
    """Best-effort physical (not logical/SMT) core count.

    Used to size the quality suite's parallel encode pool: one single-threaded
    encode per physical core saturates the machine without the cache thrashing
    that hyperthread oversubscription causes. Falls back to the logical count
    (then 1) when the physical count can't be determined.
    """
    fallback = max(1, os.cpu_count() or 1)

    try:
        if platform.system() == "Darwin":
            out = subprocess.check_output(["sysctl", "-n", "hw.physicalcpu"]).decode()
            return max(1, int(out.strip()))

        if platform.system() == "Linux":
            # Count distinct (physical id, core id) pairs from /proc/cpuinfo; each
            # pair is one physical core regardless of SMT siblings.
            try:
                cores: set[tuple[str, str]] = set()
                phys_id = core_id = None
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if line.startswith("physical id"):
                            phys_id = line.split(":", 1)[1].strip()
                        elif line.startswith("core id"):
                            core_id = line.split(":", 1)[1].strip()
                        elif not line.strip():
                            if phys_id is not None and core_id is not None:
                                cores.add((phys_id, core_id))
                            phys_id = core_id = None
                if cores:
                    return len(cores)
            except Exception:
                pass
            # Fallback: count unique (Core, Socket) rows from lscpu.
            try:
                out = subprocess.check_output(["lscpu", "-p=Core,Socket"]).decode()
                rows = {
                    line
                    for line in out.splitlines()
                    if line and not line.startswith("#")
                }
                if rows:
                    return len(rows)
            except Exception:
                pass
    except Exception:
        pass

    return fallback


def physical_core_cpu_ids() -> list[int]:
    """One logical-CPU id per physical core, ordered ascending.

    The basis for CPU pinning (issue: rigorous-timing affinity): each returned id
    is a distinct physical core's representative logical CPU (the lowest-numbered
    SMT sibling), so a ``taskset -c <id>`` confines work to one physical core
    without an SMT sibling stealing the pipeline. On Linux this picks the min
    ``processor`` per ``(physical id, core id)`` pair from /proc/cpuinfo (lscpu
    fallback); elsewhere it falls back to ``range(get_physical_cores())``.

    Used to size and place the parallel pool — cores ``[1:]`` (core 0 reserved for
    the OS/IO) — and to pick the single dedicated core (the last id) the rigorous
    single-threaded timing is pinned to.
    """
    if platform.system() == "Linux":
        # Min processor id per (physical id, core id); the lowest sibling.
        try:
            reps: Dict[tuple[str, str], int] = {}
            processor = phys_id = core_id = None
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("processor"):
                        processor = int(line.split(":", 1)[1].strip())
                    elif line.startswith("physical id"):
                        phys_id = line.split(":", 1)[1].strip()
                    elif line.startswith("core id"):
                        core_id = line.split(":", 1)[1].strip()
                    elif not line.strip():
                        if (
                            processor is not None
                            and phys_id is not None
                            and core_id is not None
                        ):
                            key = (phys_id, core_id)
                            reps[key] = (
                                processor
                                if key not in reps
                                else min(reps[key], processor)
                            )
                        processor = phys_id = core_id = None
            if reps:
                return sorted(reps.values())
        except Exception:
            pass
        # Fallback: lscpu CPU,Core,Socket — min CPU per (Core, Socket).
        try:
            out = subprocess.check_output(["lscpu", "-p=CPU,Core,Socket"]).decode()
            reps2: Dict[tuple[str, str], int] = {}
            for line in out.splitlines():
                if not line or line.startswith("#"):
                    continue
                cpu_s, core_s, sock_s = line.split(",")[:3]
                key = (core_s, sock_s)
                cpu = int(cpu_s)
                reps2[key] = cpu if key not in reps2 else min(reps2[key], cpu)
            if reps2:
                return sorted(reps2.values())
        except Exception:
            pass

    return list(range(get_physical_cores()))


def get_compiler_versions() -> Dict[str, str]:
    """Get versions of all compilers used."""
    versions = {}

    try:
        versions["rustc"] = (
            subprocess.check_output(["rustc", "--version"]).decode().strip()
        )
    except Exception:
        versions["rustc"] = "not found"

    try:
        clang_out = subprocess.check_output(
            ["clang", "--version"], stderr=subprocess.STDOUT
        ).decode()
        versions["clang"] = clang_out.split("\n")[0].strip()
    except Exception:
        versions["clang"] = "not found"

    try:
        versions["cmake"] = (
            subprocess.check_output(["cmake", "--version"])
            .decode()
            .split("\n")[0]
            .strip()
        )
    except Exception:
        versions["cmake"] = "not found"

    return versions


def _detect_mimalloc_version() -> str:
    """Parse mimalloc version from the vendored header (MI_MALLOC_VERSION)."""
    header = os.path.join(PROJECT_ROOT, "vendor", "mimalloc", "include", "mimalloc.h")
    try:
        with open(header) as f:
            for line in f:
                if "MI_MALLOC_VERSION" in line and "#define" in line:
                    # Format: #define MI_MALLOC_VERSION 217  // major + 2 digits minor
                    parts = line.split()
                    # parts: ['#define', 'MI_MALLOC_VERSION', '217', ...]
                    raw = int(parts[2])
                    major = raw // 100
                    minor = (raw % 100) // 10
                    patch = raw % 10
                    return f"{major}.{minor}.{patch}"
    except Exception:
        pass
    return "unknown"


def _find_pc_file(prefix: str, module_name: str) -> Optional[str]:
    """Search for <module_name>.pc under a vendor install prefix.

    Checks the three standard pkgconfig locations first, then falls back to a
    glob over multiarch lib paths (e.g. lib/x86_64-linux-gnu/pkgconfig) that
    Meson may use on Debian/Ubuntu even with a custom --prefix.
    """
    pc_name = f"{module_name}.pc"
    for subdir in ("lib/pkgconfig", "lib64/pkgconfig", "share/pkgconfig"):
        candidate = os.path.join(prefix, subdir, pc_name)
        if os.path.exists(candidate):
            return candidate
    # Multiarch fallback: lib/<triplet>/pkgconfig/<module>.pc
    for candidate in glob.glob(os.path.join(prefix, "lib", "*", "pkgconfig", pc_name)):
        return candidate
    return None


def _parse_pc_version(pc_path: str) -> str:
    """Extract Version from a .pc file."""
    try:
        with open(pc_path) as f:
            for line in f:
                if line.startswith("Version:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "unknown"


def get_library_versions() -> Dict[str, str]:
    """Attempt to determine versions of image libraries."""
    libraries = {}

    # Libraries installed under VENDOR_COMMON
    common_libs = [
        ("libpng", "libpng"),
        ("libwebp", "libwebp"),
        ("libavif", "libavif"),
        ("dav1d", "dav1d"),
        ("libjxl", "libjxl"),
        ("spng", "libspng"),
        ("aom", "aom"),
        ("svt-av1", "SvtAv1Enc"),
        ("libgav1", "libgav1"),
        ("zlib", "zlib"),
    ]
    for display_name, module_name in common_libs:
        pc = _find_pc_file(VENDOR_COMMON, module_name)
        libraries[display_name] = _parse_pc_version(pc) if pc else "unknown"

    # libjpeg-turbo installs to its own prefix; .pc module is libturbojpeg
    pc = _find_pc_file(VENDOR_LIBJPEG_TURBO, "libturbojpeg")
    libraries["libjpeg-turbo"] = _parse_pc_version(pc) if pc else "unknown"

    # mozjpeg installs to its own prefix; .pc module is libjpeg
    pc = _find_pc_file(VENDOR_MOZJPEG, "libjpeg")
    libraries["mozjpeg"] = _parse_pc_version(pc) if pc else "unknown"

    # jpegli ships inside libjxl and shares its version (no separate .pc).
    libraries["jpegli"] = libraries.get("libjxl", "unknown")

    libraries["mimalloc"] = _detect_mimalloc_version()
    libraries["hyperfine"] = "unknown"

    try:
        hf_version = (
            subprocess.check_output(["hyperfine", "--version"]).decode().strip()
        )
        libraries["hyperfine"] = (
            hf_version.split()[1] if len(hf_version.split()) > 1 else hf_version
        )
    except Exception:
        pass

    return libraries
