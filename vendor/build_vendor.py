#!/usr/bin/env python3
"""Build all vendored C/C++ libraries from source to local install prefixes."""

import glob
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

# Vendored build tools (currently just nasm) install here. The directory is
# prepended to PATH before any library is built so a fresh machine needs no
# system-wide nasm — see build_nasm() and _prepend_vendor_bin_to_path().
INSTALL_COMMON_BIN = os.path.join(INSTALL_COMMON, "bin")

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


def _prepend_vendor_bin_to_path():
    """Put the vendored tools bin dir (nasm) at the front of PATH.

    Every build step copies os.environ, so mutating PATH here makes the vendored
    nasm visible to cmake (aom), meson (dav1d), cargo/nasm-rs (rav1d), and the
    libjpeg-turbo / mozjpeg / SVT-AV1 / libwebp SIMD assembler invocations —
    without requiring a system-wide nasm install.
    """
    os.makedirs(INSTALL_COMMON_BIN, exist_ok=True)
    path = os.environ.get("PATH", "")
    if INSTALL_COMMON_BIN not in path.split(os.pathsep):
        os.environ["PATH"] = INSTALL_COMMON_BIN + os.pathsep + path


def build_nasm():
    """Build the NASM assembler from the vendored submodule.

    NASM assembles the x86-64 SIMD kernels in aom, dav1d, rav1d, libjpeg-turbo,
    mozjpeg, SVT-AV1 and libwebp. It is the one build tool not provided by the
    C/C++/Rust language toolchains, so vendoring it lets a fresh machine run the
    benchmark without `apt install nasm` / `brew install nasm`. Must run before
    every other step so they pick up the installed binary via PATH.
    """
    label = "nasm"
    sentinel = os.path.join(INSTALL_COMMON_BIN, "nasm")
    if is_built(sentinel):
        print(f"  [{label}] Already built, skipping.")
        return

    src = os.path.join(VENDOR_DIR, "nasm")
    if not os.path.exists(os.path.join(src, "autogen.sh")):
        raise RuntimeError(
            "vendor/nasm submodule is empty — run "
            "`git submodule update --init --depth 1 vendor/nasm`"
        )
    # NASM is built in-tree (cwd = the submodule). Its autotools build does not
    # support a VPATH/out-of-tree build of the perl-generated x86 instruction
    # tables (the recipes redirect into x86/ without creating it first). The
    # nasm submodule is marked `ignore = dirty` in .gitmodules so these build
    # artifacts never surface as repo changes.
    # A git checkout ships no ./configure; autogen.sh generates it (autoconf).
    if not os.path.exists(os.path.join(src, "configure")):
        run(["sh", "autogen.sh"], cwd=src, label=label)
    run(["./configure"], cwd=src, label=label)
    # The default target builds only the nasm/ndisasm binaries — no man pages,
    # so asciidoc/xmlto are not required.
    run(["make", f"-j{os.cpu_count() or 1}"], cwd=src, label=label)

    os.makedirs(INSTALL_COMMON_BIN, exist_ok=True)
    shutil.copy2(os.path.join(src, "nasm"), sentinel)
    print(f"  [{label}] Installed nasm to {sentinel}")


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
    cmake expects, while forwarding every other invocation to real nasm. The
    vendored nasm (built by build_nasm()) is preferred; a system nasm on PATH is
    used as a fallback.
    """
    nasm_real = os.path.join(INSTALL_COMMON_BIN, "nasm")
    if not os.path.exists(nasm_real):
        nasm_real = shutil.which("nasm")
    if not nasm_real:
        raise RuntimeError("nasm not found (vendored build missing and none on PATH)")

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


def build_libjxl():
    label = "libjxl"
    sentinel = os.path.join(INSTALL_COMMON, "lib", "libjxl.a")
    if not is_built(sentinel):
        sentinel = os.path.join(INSTALL_COMMON, "lib64", "libjxl.a")
    # jpegli ships inside libjxl. Its jpegli-static target is EXCLUDE_FROM_ALL
    # and has no install rule, so we build it explicitly and stage it. Treat the
    # staged jpegli archive as a second sentinel so an existing libjxl install
    # (built before jpegli support) is upgraded.
    jpegli_lib_dst = os.path.join(INSTALL_COMMON, "lib64", "libjpegli-static.a")
    # jpegli's XYB path also needs jxl_extras-internal (jxl::extras::EncodeJpeg /
    # DecodeJpeg, which apply the sRGB<->XYB color transform + ICC a raw jpegli or
    # plain libjpeg decode cannot). Stage it as a third sentinel so an install
    # made before XYB support gets upgraded.
    extras_lib_dst = os.path.join(INSTALL_COMMON, "lib64", "libjxl_extras-internal.a")
    if (
        is_built(sentinel)
        and is_built(jpegli_lib_dst)
        and is_built(extras_lib_dst)
    ):
        print(f"  [{label}] Already built (incl. jpegli + extras), skipping.")
        return
    src = os.path.join(VENDOR_DIR, "libjxl")
    if not is_built(sentinel):
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
    else:
        # libjxl is already built; only the jpegli / extras archives still need
        # staging in the existing, already-configured build dir.
        print(f"  [{label}] Already built; building + staging jpegli + extras only.")
    _build_and_stage_jpegli(label)
    _build_and_stage_jxl_extras(label)


def _build_and_stage_jpegli(label):
    """Build the jpegli-static target and stage it into INSTALL_COMMON.

    jpegli (Google's perceptually-tuned JPEG codec) lives in the libjxl source
    tree but is an EXCLUDE_FROM_ALL target with no install() rule, so it must be
    built explicitly and copied into the install prefix by hand (the same
    approach used for rav1d/ssimulacra2). The generated <jpeglib.h> headers are
    staged alongside so the jpegli benchmark can compile against the API.
    """
    build_dir = os.path.join(BUILD_DIR, "libjxl")

    def _find_jpegli_lib():
        candidate = os.path.join(build_dir, "lib", "libjpegli-static.a")
        if os.path.exists(candidate):
            return candidate
        matches = glob.glob(
            os.path.join(build_dir, "**", "libjpegli-static.a"), recursive=True
        )
        return matches[0] if matches else None

    # jpegli-static is EXCLUDE_FROM_ALL. On Linux the default build already
    # produces it (the jpegli libjpeg.so target depends on it); elsewhere build
    # it explicitly. Reuse an existing artifact rather than rebuilding so this
    # stays idempotent even when the build dir is stale.
    src_lib = _find_jpegli_lib()
    if src_lib is None:
        run(
            ["cmake", "--build", ".", "--target", "jpegli-static", "--parallel"],
            cwd=build_dir,
            env=os.environ.copy(),
            label=label,
        )
        src_lib = _find_jpegli_lib()
    if src_lib is None:
        print("ERROR: jpegli-static built but libjpegli-static.a not found")
        sys.exit(1)

    lib_dst_dir = os.path.join(INSTALL_COMMON, "lib64")
    os.makedirs(lib_dst_dir, exist_ok=True)
    shutil.copy2(src_lib, os.path.join(lib_dst_dir, "libjpegli-static.a"))

    inc_src_dir = os.path.join(build_dir, "lib", "include", "jpegli")
    inc_dst_dir = os.path.join(INSTALL_COMMON, "include", "jpegli")
    os.makedirs(inc_dst_dir, exist_ok=True)
    for header in ("jconfig.h", "jpeglib.h", "jmorecfg.h"):
        shutil.copy2(
            os.path.join(inc_src_dir, header),
            os.path.join(inc_dst_dir, header),
        )
    print(f"  [{label}] Staged jpegli-static + headers into {INSTALL_COMMON}")


def _build_and_stage_jxl_extras(label):
    """Build the jxl_extras-internal archive and stage it into INSTALL_COMMON.

    jxl::extras::EncodeJpeg / DecodeJpeg carry the sRGB<->XYB color transform and
    XYB ICC handling that a raw jpegli (or plain libjpeg) encode/decode cannot do,
    so the XYB jpegli variant and its scoring decoder link against this archive.
    It is defined in jxl_extras.cmake, which libjxl only includes when tools or
    testing is enabled, and (like jpegli-static) it is EXCLUDE_FROM_ALL with no
    install rule. So enable tools via a cheap reconfigure -- this only *defines*
    the target; we build just this one archive, never the tool executables -- and
    copy it by hand. Every other library it needs (libjxl.a, which as a static
    archive carries all of jxl's internal symbols, plus libjxl_cms.a,
    libjxl_threads.a, libhwy.a, libbrotli*.a, libjpegli-static.a) is already
    installed by the main libjxl build.
    """
    build_dir = os.path.join(BUILD_DIR, "libjxl")
    archive = "libjxl_extras-internal.a"

    def _find_extras_lib():
        candidate = os.path.join(build_dir, "lib", archive)
        if os.path.exists(candidate):
            return candidate
        matches = glob.glob(
            os.path.join(build_dir, "**", archive), recursive=True
        )
        return matches[0] if matches else None

    src_lib = _find_extras_lib()
    if src_lib is None:
        # jxl_extras.cmake is gated behind JPEGXL_ENABLE_TOOLS OR BUILD_TESTING,
        # both OFF in the main configure. Turn tools ON so the target exists; the
        # tool executables stay unbuilt because we only ask for this archive.
        # Disable the optional image codecs (APNG/GIF/EXR/system-JPEG/sjpeg): the
        # XYB jpegli enc/dec path needs none of them, and leaving them in would
        # make the archive depend on libpng/giflib/OpenEXR at our link step. The
        # apng/gif/jpg codec sources compile unconditionally but only reference
        # their libraries when find_package(PNG/GIF/JPEG) succeeds, so the lever
        # is CMAKE_DISABLE_FIND_PACKAGE_* (they then compile as stubs).
        run(
            [
                "cmake",
                "-DJPEGXL_ENABLE_TOOLS=ON",
                "-DCMAKE_DISABLE_FIND_PACKAGE_PNG=ON",
                "-DCMAKE_DISABLE_FIND_PACKAGE_GIF=ON",
                "-DCMAKE_DISABLE_FIND_PACKAGE_JPEG=ON",
                "-DJPEGXL_ENABLE_OPENEXR=OFF",
                "-DJPEGXL_ENABLE_SJPEG=OFF",
                ".",
            ],
            cwd=build_dir,
            env=os.environ.copy(),
            label=label,
        )
        run(
            [
                "cmake",
                "--build",
                ".",
                "--target",
                "jxl_extras-internal",
                "--parallel",
            ],
            cwd=build_dir,
            env=os.environ.copy(),
            label=label,
        )
        src_lib = _find_extras_lib()
    if src_lib is None:
        print(f"ERROR: jxl_extras-internal built but {archive} not found")
        sys.exit(1)

    lib_dst_dir = os.path.join(INSTALL_COMMON, "lib64")
    os.makedirs(lib_dst_dir, exist_ok=True)
    shutil.copy2(src_lib, os.path.join(lib_dst_dir, archive))
    print(f"  [{label}] Staged jxl_extras-internal into {INSTALL_COMMON}")


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


def main(targets=None):
    print("=" * 70)
    print("BUILDING VENDORED DEPENDENCIES")
    print("=" * 70)

    # nasm is built first; prepend its install dir to PATH so every later step
    # (and any tool they spawn) resolves the vendored assembler.
    _prepend_vendor_bin_to_path()

    steps = [
        # nasm must precede every library that assembles x86-64 SIMD kernels.
        ("nasm", build_nasm),
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
        # Image-quality metrics come from the published iqa-cli binary, installed
        # from crates.io via `cargo install` (see bench_lib/build.install_iqa_cli);
        # no in-repo source and no vendored metric binary.
    ]

    # Optional positional args select a subset of steps to build (order
    # preserved). The Rust lint/test tasks only need `nasm` to compile rav1e's
    # SIMD; building the full C/C++ stack for them would be wasteful.
    if targets:
        known = {name for name, _ in steps}
        unknown = [t for t in targets if t not in known]
        if unknown:
            print(
                f"\nERROR: unknown build target(s): {', '.join(unknown)}\n"
                f"Available: {', '.join(name for name, _ in steps)}"
            )
            sys.exit(2)
        wanted = set(targets)
        steps = [(name, fn) for name, fn in steps if name in wanted]

    for name, fn in steps:
        print(f"\n[{name}]")
        fn()

    print("\n" + "=" * 70)
    if targets:
        print(f"Vendored target(s) built successfully: {', '.join(targets)}.")
    else:
        print("All vendored dependencies built successfully.")
    print("=" * 70)


if __name__ == "__main__":
    main(sys.argv[1:])
