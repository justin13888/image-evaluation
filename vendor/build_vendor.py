#!/usr/bin/env python3
"""Build all vendored C/C++ libraries from source to local install prefixes."""

import os
import shutil
import stat
import subprocess
import sys

# Project root is one level up from this script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
VENDOR_DIR = SCRIPT_DIR

# Install prefixes
INSTALL_COMMON = os.path.join(VENDOR_DIR, "install", "common")
INSTALL_LIBJPEG_TURBO = os.path.join(VENDOR_DIR, "install", "libjpeg-turbo")
INSTALL_MOZJPEG = os.path.join(VENDOR_DIR, "install", "mozjpeg")

# Build directories
BUILD_DIR = os.path.join(VENDOR_DIR, "build")

# Compiler flags
OPT_FLAGS = ["-O3", "-march=native", "-fPIC"]


def run(cmd, cwd=None, env=None, label=None):
    """Run a command, exit on failure."""
    label_str = f"[{label}] " if label else ""
    print(f"  {label_str}$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, env=env)
    if result.returncode != 0:
        print(f"\nERROR: Command failed (exit {result.returncode})")
        sys.exit(result.returncode)


def cmake_build(
    src_dir,
    build_subdir,
    install_prefix,
    cmake_args=None,
    label=None,
    extra_env=None,
):
    """Configure and build a CMake project."""
    build_dir = os.path.join(BUILD_DIR, build_subdir)
    os.makedirs(build_dir, exist_ok=True)
    os.makedirs(install_prefix, exist_ok=True)

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    cflags = " ".join(OPT_FLAGS)
    env["CFLAGS"] = cflags
    env["CXXFLAGS"] = cflags

    configure_cmd = [
        "cmake",
        src_dir,
        f"-DCMAKE_INSTALL_PREFIX={install_prefix}",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=OFF",
        "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
        "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
    ] + (cmake_args or [])

    run(configure_cmd, cwd=build_dir, env=env, label=label)
    run(["cmake", "--build", ".", "--parallel"], cwd=build_dir, env=env, label=label)
    run(["cmake", "--install", "."], cwd=build_dir, env=env, label=label)


def is_built(sentinel_path):
    """Check if a library has already been built."""
    return os.path.exists(sentinel_path)


def build_zlib():
    label = "zlib"
    sentinel = os.path.join(INSTALL_COMMON, "lib", "libz.a")
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return
    src = os.path.join(VENDOR_DIR, "zlib")
    cmake_build(
        src,
        "zlib",
        INSTALL_COMMON,
        cmake_args=["-DZLIB_BUILD_EXAMPLES=OFF"],
        label=label,
    )


def build_mimalloc():
    label = "mimalloc"
    # mimalloc installs into a versioned subdir: lib64/mimalloc-<ver>/libmimalloc.a
    import glob as _glob

    matches = _glob.glob(
        os.path.join(INSTALL_COMMON, "lib*", "mimalloc-*", "libmimalloc.a")
    )
    sentinel = (
        matches[0] if matches else os.path.join(INSTALL_COMMON, "lib", "libmimalloc.a")
    )
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return
    src = os.path.join(VENDOR_DIR, "mimalloc")
    cmake_build(
        src,
        "mimalloc",
        INSTALL_COMMON,
        cmake_args=[
            "-DMI_BUILD_TESTS=OFF",
            "-DMI_BUILD_SHARED=OFF",
        ],
        label=label,
    )


def build_libjpeg_turbo():
    label = "libjpeg-turbo"
    sentinel = os.path.join(INSTALL_LIBJPEG_TURBO, "lib64", "libturbojpeg.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_LIBJPEG_TURBO, "lib", "libturbojpeg.a")
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return
    src = os.path.join(VENDOR_DIR, "libjpeg-turbo")
    cmake_build(
        src,
        "libjpeg-turbo",
        INSTALL_LIBJPEG_TURBO,
        cmake_args=[
            "-DENABLE_SHARED=OFF",
            f"-DZLIB_ROOT={INSTALL_COMMON}",
        ],
        label=label,
    )


def build_mozjpeg():
    label = "mozjpeg"
    sentinel = os.path.join(INSTALL_MOZJPEG, "lib64", "libturbojpeg.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_MOZJPEG, "lib", "libturbojpeg.a")
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return
    src = os.path.join(VENDOR_DIR, "mozjpeg")
    cmake_build(
        src,
        "mozjpeg",
        INSTALL_MOZJPEG,
        cmake_args=[
            "-DENABLE_SHARED=OFF",
            f"-DCMAKE_PREFIX_PATH={INSTALL_COMMON}",
            f"-DZLIB_ROOT={INSTALL_COMMON}",
            f"-DPNG_ROOT={INSTALL_COMMON}",
            # Enable CMP0074 so <Pkg>_ROOT variables are respected by find_package
            "-DCMAKE_POLICY_DEFAULT_CMP0074=NEW",
            # mozjpeg requires CMake 2.8.12; CMake ≥4.x dropped compat < 3.5
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        ],
        label=label,
    )


def build_libpng():
    label = "libpng"
    sentinel = os.path.join(INSTALL_COMMON, "lib", "libpng.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_COMMON, "lib64", "libpng.a")
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return
    src = os.path.join(VENDOR_DIR, "libpng")
    cmake_build(
        src,
        "libpng",
        INSTALL_COMMON,
        cmake_args=[
            "-DPNG_SHARED=OFF",
            "-DPNG_TESTS=OFF",
            f"-DZLIB_ROOT={INSTALL_COMMON}",
        ],
        label=label,
    )


def build_spng():
    label = "spng"
    sentinel = os.path.join(INSTALL_COMMON, "lib", "libspng_static.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_COMMON, "lib64", "libspng_static.a")
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return
    src = os.path.join(VENDOR_DIR, "spng")
    cmake_build(
        src,
        "spng",
        INSTALL_COMMON,
        cmake_args=[
            f"-DCMAKE_PREFIX_PATH={INSTALL_COMMON}",
        ],
        label=label,
    )


def build_dav1d():
    label = "dav1d"
    sentinel = os.path.join(INSTALL_COMMON, "lib", "libdav1d.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_COMMON, "lib64", "libdav1d.a")
    if not is_built(sentinel):
        # Some distros put it in lib/x86_64-linux-gnu or similar
        import glob as _glob

        matches = _glob.glob(
            os.path.join(INSTALL_COMMON, "**", "libdav1d.a"), recursive=True
        )
        if matches:
            sentinel = matches[0]
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return

    src = os.path.join(VENDOR_DIR, "dav1d")
    build_dir = os.path.join(BUILD_DIR, "dav1d")
    os.makedirs(build_dir, exist_ok=True)
    os.makedirs(INSTALL_COMMON, exist_ok=True)

    env = os.environ.copy()
    cflags = " ".join(OPT_FLAGS)
    env["CFLAGS"] = cflags

    run(
        [
            "meson",
            "setup",
            build_dir,
            src,
            f"--prefix={INSTALL_COMMON}",
            "--default-library=static",
            "--buildtype=release",
            "-Denable_tools=false",
            "-Denable_tests=false",
        ],
        label=label,
        env=env,
    )
    run(["ninja", "-C", build_dir], label=label, env=env)
    run(["ninja", "-C", build_dir, "install"], label=label, env=env)


def _make_nasm_shim() -> str:
    """
    Return path to a nasm shim script, creating it if needed.

    Root cause: aom cmake's test_nasm() runs
        nasm -hf
    and checks whether the output contains the literal string "-Ox" to confirm
    multipass optimization support.  In nasm 2.x, `nasm -hf` printed a full
    help page that included "-Ox".  In nasm 3.x, `-hf` only lists output
    formats; "-Ox" moved to a separate help section (`nasm -h`), so the grep
    never matches and cmake rejects nasm 3.x as "unsupported".

    The shim intercepts the `-hf` invocation and appends the "-Ox" line that
    cmake expects, while forwarding every other invocation to real nasm.
    """
    nasm_real = shutil.which("nasm")
    if not nasm_real:
        raise RuntimeError("nasm not found in PATH")

    shim_path = os.path.join(BUILD_DIR, "nasm_shim.sh")
    os.makedirs(BUILD_DIR, exist_ok=True)
    with open(shim_path, "w") as f:
        f.write(f"""\
#!/bin/bash
# nasm shim: aom cmake test_nasm() greps for "-Ox" in `nasm -hf` output.
# nasm 3.x moved -Ox out of the -hf help text, breaking that check.
# Intercept -hf and append the expected line; pass everything else through.
if [[ "$*" == "-hf" ]]; then
    {nasm_real} -hf 2>&1
    echo "    -Ox                     enable multipass optimization"
    exit 0
fi
exec {nasm_real} "$@"
""")
    os.chmod(
        shim_path,
        os.stat(shim_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH,
    )
    return shim_path


def build_aom():
    label = "aom"
    sentinel = os.path.join(INSTALL_COMMON, "lib", "libaom.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_COMMON, "lib64", "libaom.a")
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return
    src = os.path.join(VENDOR_DIR, "aom")
    nasm_shim = _make_nasm_shim()
    cmake_build(
        src,
        "aom",
        INSTALL_COMMON,
        cmake_args=[
            "-DENABLE_EXAMPLES=OFF",
            "-DENABLE_TESTS=OFF",
            "-DENABLE_TOOLS=OFF",
            # Do NOT set CONFIG_AV1_DECODER=0: libavif's codec_aom.c includes
            # aom/aom_decoder.h unconditionally, so the decoder headers must
            # be installed even though dav1d handles all actual decoding.
            # Point cmake at the shim so test_nasm() passes on nasm 3.x.
            # Real nasm compilations are forwarded unchanged by the shim.
            f"-DCMAKE_ASM_NASM_COMPILER={nasm_shim}",
        ],
        label=label,
    )


def build_svt_av1():
    label = "svt-av1"
    sentinel = os.path.join(INSTALL_COMMON, "lib", "libSvtAv1Enc.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_COMMON, "lib64", "libSvtAv1Enc.a")
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return
    src = os.path.join(VENDOR_DIR, "SVT-AV1")
    cmake_build(
        src,
        "svt-av1",
        INSTALL_COMMON,
        cmake_args=[
            "-DBUILD_APPS=OFF",
            "-DBUILD_TESTING=OFF",
            "-DBUILD_DEC=OFF",
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        ],
        label=label,
    )


def build_libgav1():
    label = "libgav1"
    sentinel = os.path.join(INSTALL_COMMON, "lib", "libgav1.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_COMMON, "lib64", "libgav1.a")
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return
    src = os.path.join(VENDOR_DIR, "libgav1")
    cmake_build(
        src,
        "libgav1",
        INSTALL_COMMON,
        cmake_args=[
            "-DLIBGAV1_ENABLE_TESTS=OFF",
            "-DLIBGAV1_ENABLE_EXAMPLES=OFF",
            # libgav1 validates this option must be exactly 0 or 1 (not ON/OFF);
            # newer CMake's EQUAL no longer coerces ON->1, so pass 1 explicitly.
            "-DLIBGAV1_THREADPOOL_USE_STD_MUTEX=1",
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        ],
        label=label,
    )


def build_rav1d():
    label = "rav1d"
    target_dir = os.path.join(BUILD_DIR, "rav1d")
    binary = os.path.join(target_dir, "release", "librav1d.a")
    installed = os.path.join(INSTALL_COMMON, "lib", "librav1d.a")
    if os.path.exists(installed):
        print(f"  [{label}] Already built, skipping.")
        return

    manifest = os.path.join(VENDOR_DIR, "rav1d", "Cargo.toml")
    os.makedirs(target_dir, exist_ok=True)
    os.makedirs(os.path.join(INSTALL_COMMON, "lib"), exist_ok=True)

    env = os.environ.copy()
    env["RUSTFLAGS"] = "-C target-cpu=native"

    print(f"  [{label}] Building librav1d.a...")
    run(
        [
            "cargo",
            "build",
            "--lib",
            "--release",
            "--manifest-path",
            manifest,
            "--target-dir",
            target_dir,
        ],
        env=env,
        label=label,
    )

    # Copy to install prefix
    shutil.copy2(binary, installed)
    print(f"  [{label}] Installed librav1d.a to {installed}")


def _libavif_cache_is_current(build_dir):
    """True iff libavif's existing CMakeCache enables all desired codecs.

    Guards against a stale cache (e.g. built before SVT/libgav1 support was
    added) silently skipping the rebuild because the sentinel library already
    exists. Makes the AVIF_CODEC_* set below the single source of truth.
    """
    cache = os.path.join(build_dir, "CMakeCache.txt")
    if not os.path.exists(cache):
        return False
    wanted = (
        "AVIF_CODEC_DAV1D:STRING=SYSTEM",
        "AVIF_CODEC_AOM:STRING=SYSTEM",
        "AVIF_CODEC_SVT:STRING=SYSTEM",
        "AVIF_CODEC_LIBGAV1:STRING=SYSTEM",
    )
    with open(cache) as f:
        text = f.read()
    return all(w in text for w in wanted)


def _patch_libavif_for_svt4(src):
    """Make libavif's codec_svt.c build against the pinned SVT-AV1 >=4.x.

    libavif 1.2.1 sets the SVT config field `enable_adaptive_quantization`, which
    SVT-AV1 4.x renamed to `aq_mode` (identical semantics: 2 = CRF). Patch the
    vendored source in place so the SVT codec compiles. Idempotent: a no-op once
    already patched (or if a future libavif/SVT pin no longer needs it).
    """
    path = os.path.join(src, "src", "codec_svt.c")
    old = "svt_config->enable_adaptive_quantization = 2;"
    new = "svt_config->aq_mode = 2;  // SVT-AV1 >=4.x renamed enable_adaptive_quantization"
    with open(path) as f:
        text = f.read()
    if old in text:
        with open(path, "w") as f:
            f.write(text.replace(old, new))
        print("  [libavif] Patched codec_svt.c for SVT-AV1 >=4.x (aq_mode).")


def _restore_libavif_svt(src):
    """Restore codec_svt.c after building so the (submodule) working tree stays
    clean. The patch above is reapplied on each rebuild, so this is safe."""
    try:
        subprocess.run(
            ["git", "-C", src, "checkout", "--", "src/codec_svt.c"],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass


def build_libavif():
    label = "libavif"
    build_dir = os.path.join(BUILD_DIR, "libavif")
    sentinel = os.path.join(INSTALL_COMMON, "lib", "libavif.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_COMMON, "lib64", "libavif.a")
    if is_built(sentinel):
        if _libavif_cache_is_current(build_dir):
            print(f"  [{label}] Already built, skipping.")
            return
        # Stale codec configuration: wipe the build dir + installed lib so the
        # rebuild below reconfigures with the current AVIF_CODEC_* flags.
        print(f"  [{label}] Codec flags changed; forcing reconfigure/rebuild.")
        if os.path.isdir(build_dir):
            shutil.rmtree(build_dir)
        for cand in (
            os.path.join(INSTALL_COMMON, "lib", "libavif.a"),
            os.path.join(INSTALL_COMMON, "lib64", "libavif.a"),
        ):
            if os.path.exists(cand):
                os.remove(cand)
    src = os.path.join(VENDOR_DIR, "libavif")
    _patch_libavif_for_svt4(src)

    # Build pkg-config paths to find vendored dav1d and aom
    pkg_lib_dirs = [
        os.path.join(INSTALL_COMMON, "lib", "pkgconfig"),
        os.path.join(INSTALL_COMMON, "lib64", "pkgconfig"),
        os.path.join(INSTALL_COMMON, "share", "pkgconfig"),
    ]
    # Also search subdirs (e.g. x86_64-linux-gnu)
    import glob as _glob

    for extra in _glob.glob(os.path.join(INSTALL_COMMON, "lib", "*", "pkgconfig")):
        pkg_lib_dirs.append(extra)

    existing_pkg = os.environ.get("PKG_CONFIG_PATH", "")
    pkg_config_path = os.pathsep.join(
        pkg_lib_dirs + ([existing_pkg] if existing_pkg else [])
    )

    cmake_build(
        src,
        "libavif",
        INSTALL_COMMON,
        cmake_args=[
            "-DAVIF_CODEC_DAV1D=SYSTEM",
            "-DAVIF_CODEC_AOM=SYSTEM",
            "-DAVIF_CODEC_SVT=SYSTEM",
            "-DAVIF_CODEC_LIBGAV1=SYSTEM",
            "-DAVIF_BUILD_APPS=OFF",
            "-DAVIF_BUILD_TESTS=OFF",
            "-DAVIF_LIBYUV=OFF",
            "-DAVIF_LIBSHARPYUV=OFF",
            f"-DCMAKE_PREFIX_PATH={INSTALL_COMMON}",
        ],
        label=label,
        extra_env={"PKG_CONFIG_PATH": pkg_config_path},
    )
    _restore_libavif_svt(src)


def build_libjxl():
    label = "libjxl"
    sentinel = os.path.join(INSTALL_COMMON, "lib", "libjxl.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_COMMON, "lib64", "libjxl.a")
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return
    src = os.path.join(VENDOR_DIR, "libjxl")
    cmake_build(
        src,
        "libjxl",
        INSTALL_COMMON,
        cmake_args=[
            "-DBUILD_TESTING=OFF",
            "-DJPEGXL_ENABLE_TOOLS=OFF",
            "-DJPEGXL_ENABLE_MANPAGES=OFF",
            "-DJPEGXL_ENABLE_BENCHMARK=OFF",
            "-DJPEGXL_ENABLE_EXAMPLES=OFF",
            # libjxl's third_party/sjpeg requires CMake 2.8.7; CMake ≥4.x dropped compat < 3.5
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        ],
        label=label,
    )


def build_libwebp():
    label = "libwebp"
    sentinel = os.path.join(INSTALL_COMMON, "lib", "libwebp.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_COMMON, "lib64", "libwebp.a")
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return
    src = os.path.join(VENDOR_DIR, "libwebp")
    cmake_build(
        src,
        "libwebp",
        INSTALL_COMMON,
        cmake_args=[
            "-DWEBP_BUILD_CWEBP=OFF",
            "-DWEBP_BUILD_DWEBP=OFF",
            "-DWEBP_BUILD_GIF2WEBP=OFF",
            "-DWEBP_BUILD_IMG2WEBP=OFF",
            "-DWEBP_BUILD_EXTRAS=OFF",
        ],
        label=label,
    )


def build_ssimulacra2():
    label = "ssimulacra2"
    binary = os.path.join(BUILD_DIR, "ssimulacra2", "release", "ssimulacra2_rs")
    if os.path.exists(binary):
        print(f"  [{label}] Already built, skipping.")
        return

    manifest = os.path.join(VENDOR_DIR, "ssimulacra2", "ssimulacra2_bin", "Cargo.toml")
    target_dir = os.path.join(BUILD_DIR, "ssimulacra2")
    os.makedirs(target_dir, exist_ok=True)

    env = os.environ.copy()
    env["RUSTFLAGS"] = "-C target-cpu=native"

    print(f"  [{label}] Building ssimulacra2_rs...")
    run(
        [
            "cargo",
            "build",
            "--release",
            "--no-default-features",
            "--features",
            "avif",
            "--manifest-path",
            manifest,
            "--target-dir",
            target_dir,
        ],
        env=env,
        label=label,
    )


def main():
    print("=" * 70)
    print("BUILDING VENDORED DEPENDENCIES")
    print("=" * 70)

    steps = [
        ("zlib", build_zlib),
        ("mimalloc", build_mimalloc),
        ("libpng", build_libpng),  # must precede mozjpeg (mozjpeg find_package(PNG))
        ("libjpeg-turbo", build_libjpeg_turbo),
        ("mozjpeg", build_mozjpeg),
        ("spng", build_spng),
        ("dav1d", build_dav1d),
        ("aom", build_aom),
        ("svt-av1", build_svt_av1),
        ("libgav1", build_libgav1),
        ("rav1d", build_rav1d),
        ("libavif", build_libavif),
        ("libjxl", build_libjxl),
        ("libwebp", build_libwebp),
        ("ssimulacra2", build_ssimulacra2),
    ]

    for name, fn in steps:
        print(f"\n[{name}]")
        fn()

    print("\n" + "=" * 70)
    print("All vendored dependencies built successfully.")
    print("=" * 70)


if __name__ == "__main__":
    main()
