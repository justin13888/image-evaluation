"""Data setup and verification for benchmark datasets."""

import hashlib
import json
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from bench_lib.models import DatasetId

MANIFEST_PATH = Path("data/.manifest.json")
MANIFEST_VERSION = 1

# Bump these to force regeneration of generated datasets
PATHOLOGICAL_GENERATOR_VERSION = 1
TEST_GENERATOR_VERSION = 1

KODAK_BASE_URL = "http://r0k.us/graphics/kodak/kodak/"
DIV2K_URL = "http://data.vision.ee.ethz.ch/cvl/DIV2K/DIV2K_train_HR.zip"
DIV2K_SELECT_N = 20

# Tecnick TESTIMAGES SAMPLING (8-bit RGB, 1200x1200). CC BY-NC-SA 4.0 — downloaded
# on demand (never vendored) to keep non-commercial content out of the repo.
TECNICK_URL = (
    "https://downloads.sourceforge.net/project/testimages/"
    "SAMPLING/8BIT/RGB/SAMPLING_8BIT_RGB_1200x1200.tar.bz2"
)
TECNICK_SELECT_N = 24

# imazen/codec-corpus is vendored as a git submodule (not committed into this repo).
# These datasets reference subdirectories of that submodule's checkout.
CODEC_CORPUS_DIR = Path("vendor/codec-corpus")
CODEC_CORPUS_SUBDIRS = {
    DatasetId.CLIC2025: "clic2025/final-test",
    DatasetId.CID22: "CID22/CID22-512/validation",
    DatasetId.SCREEN: "gb82-sc",
}


# ============================================================================
# Manifest helpers
# ============================================================================


def _compute_sha256(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest() -> dict:
    """Load manifest from disk. Returns empty manifest on missing or invalid JSON."""
    if not MANIFEST_PATH.exists():
        return {"version": MANIFEST_VERSION, "datasets": {}}
    try:
        with open(MANIFEST_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
            print("Warning: Invalid or outdated manifest, resetting.")
            MANIFEST_PATH.unlink(missing_ok=True)
            return {"version": MANIFEST_VERSION, "datasets": {}}
        return data
    except (json.JSONDecodeError, OSError):
        print("Warning: Corrupt manifest, resetting.")
        MANIFEST_PATH.unlink(missing_ok=True)
        return {"version": MANIFEST_VERSION, "datasets": {}}


def _save_manifest(manifest: dict) -> None:
    """Save manifest to disk."""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


def _verify_files(files: dict[str, str]) -> tuple[list[str], list[str]]:
    """
    Verify files exist and SHA-256 checksums match.

    Returns (missing, corrupt) lists of file paths.
    """
    missing = []
    corrupt = []
    for path_str, expected_sha256 in files.items():
        p = Path(path_str)
        if not p.exists():
            missing.append(path_str)
        elif expected_sha256 and _compute_sha256(p) != expected_sha256:
            corrupt.append(path_str)
    return missing, corrupt


# ============================================================================
# Download helpers
# ============================================================================


def _download_with_progress(url: str, dest: Path, desc: str = "") -> None:
    """Download a large file with a progress bar."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _progress(block_num: int, block_size: int, total_size: int) -> None:
        if total_size > 0:
            downloaded = block_num * block_size
            pct = min(100, downloaded * 100 // total_size)
            mb = downloaded / (1024 * 1024)
            total_mb = total_size / (1024 * 1024)
            print(
                f"\r  {desc}: {pct}% ({mb:.1f}/{total_mb:.1f} MB)",
                end="",
                flush=True,
            )

    urllib.request.urlretrieve(url, dest, reporthook=_progress)
    print()  # newline after progress


def _download_file(url: str, dest: Path, retries: int = 3) -> None:
    """Download a small file with retries, no progress display."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries):
        try:
            urllib.request.urlretrieve(url, dest)
            return
        except Exception as e:
            if attempt < retries - 1:
                print(f"  Retry {attempt + 2}/{retries} for {dest.name}...")
            else:
                raise RuntimeError(f"Failed to download {url}: {e}") from e


# ============================================================================
# KODAK dataset
# ============================================================================


def _setup_kodak(force: bool = False) -> dict[str, str]:
    """Download KODAK images and return {path: sha256} dict."""
    print("Setting up KODAK dataset...")
    Path("data/kodak").mkdir(parents=True, exist_ok=True)

    file_checksums: dict[str, str] = {}
    for i in range(1, 25):
        filename = f"kodim{i:02d}.png"
        dest = Path(f"data/kodak/{filename}")
        url = f"{KODAK_BASE_URL}{filename}"

        if dest.exists() and not force:
            print(f"  ✓ {filename} (cached)")
        else:
            print(f"  Downloading {filename}...")
            _download_file(url, dest)
            print(f"  ✓ {filename}")

        file_checksums[str(dest)] = _compute_sha256(dest)

    print("  ✓ KODAK dataset ready (24 images)")
    return file_checksums


# ============================================================================
# DIV2K dataset
# ============================================================================


def _select_diverse_images(
    image_dir: Path,
    n: int = 20,
    patterns: tuple[str, ...] = ("*.png",),
    recursive: bool = False,
) -> list[str]:
    """Select n diverse images using greedy farthest-first traversal (perceptual hash).

    Returns paths relative to ``image_dir`` (POSIX). For a flat directory this is just
    the basename, so existing callers (DIV2K) are unaffected; ``recursive``/``patterns``
    let nested archives with non-PNG sources (Tecnick TIFFs) be selected too.
    """
    try:
        import imagehash
    except ImportError:
        raise RuntimeError("imagehash not installed. Run: uv sync")

    images: list[Path] = []
    for pat in patterns:
        images.extend(image_dir.rglob(pat) if recursive else image_dir.glob(pat))
    images = sorted(set(images))
    if len(images) < n:
        print(f"  Warning: Only {len(images)} images found, selecting all")
        n = len(images)

    print(f"  Computing hashes for {len(images)} images...")
    hashes: dict = {}
    for img_path in images:
        try:
            img = Image.open(img_path)
            hashes[img_path] = imagehash.phash(img, hash_size=16)
        except Exception as e:
            print(f"  Warning: Failed to hash {img_path.name}: {e}")

    if len(hashes) < n:
        n = len(hashes)

    selected = []
    remaining = list(hashes.keys())
    selected.append(remaining.pop(0))

    while len(selected) < n and remaining:
        max_dist = -1
        best_idx = 0
        for i, candidate in enumerate(remaining):
            min_dist = min(hashes[candidate] - hashes[s] for s in selected)
            if min_dist > max_dist:
                max_dist = min_dist
                best_idx = i
        selected.append(remaining.pop(best_idx))
        print(
            f"  Selected {len(selected)}/{n}: {selected[-1].name} (diversity: {max_dist})"
        )

    return [img.relative_to(image_dir).as_posix() for img in selected]


def _setup_div2k(force: bool = False) -> dict[str, str]:
    """Download and extract DIV2K, select diverse subset."""
    print("Setting up DIV2K dataset...")
    Path("data/div2k").mkdir(parents=True, exist_ok=True)

    zip_path = Path("data/div2k/DIV2K_train_HR.zip")
    extract_dir = Path("data/div2k/DIV2K_train_HR")
    selected_txt = Path("data/div2k/selected.txt")

    # Download ZIP
    if not zip_path.exists() or force:
        print("  Downloading DIV2K training set (~3.5GB, this may take a while)...")
        _download_with_progress(DIV2K_URL, zip_path, "DIV2K")
        print("  ✓ DIV2K downloaded")
    else:
        print("  ✓ DIV2K ZIP already downloaded")

    # Extract
    if not extract_dir.exists() or force:
        print("  Extracting DIV2K...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall("data/div2k/")
        print("  ✓ DIV2K extracted")
    else:
        print("  ✓ DIV2K already extracted")

    # Select diverse images
    if not selected_txt.exists() or force:
        print("  Selecting diverse images...")
        selected_names = _select_diverse_images(extract_dir, DIV2K_SELECT_N)
        with open(selected_txt, "w") as f:
            for name in selected_names:
                f.write(f"{name}\n")
        print(f"  ✓ Selected {len(selected_names)} images → {selected_txt}")
    else:
        print("  ✓ DIV2K selection already exists")
        selected_names = [
            line.strip()
            for line in selected_txt.read_text().splitlines()
            if line.strip()
        ]

    # Compute checksums of selected files
    file_checksums: dict[str, str] = {}
    for name in selected_names:
        p = extract_dir / name
        path_key = f"data/div2k/DIV2K_train_HR/{name}"
        if p.exists():
            file_checksums[path_key] = _compute_sha256(p)
        else:
            print(f"  Warning: Selected file not found: {p}")

    print(f"  ✓ DIV2K dataset ready ({len(file_checksums)} selected images)")
    return file_checksums


# ============================================================================
# Pathological dataset
# ============================================================================


def _generate_solid(dest: Path) -> None:
    """3840x2160 solid blue image (#4287f5)."""
    img = Image.new("RGB", (3840, 2160), (66, 135, 245))
    img.save(dest, "PNG")


def _generate_noise(dest: Path) -> None:
    """3840x2160 Gaussian noise image (fixed seed)."""
    rng = np.random.default_rng(42)
    data = np.clip(rng.normal(128, 50, (2160, 3840, 3)), 0, 255).astype(np.uint8)
    img = Image.fromarray(data, "RGB")
    img.save(dest, "PNG")


def _generate_screenshot(dest: Path) -> None:
    """3840x2160 screenshot-like image with flat UI regions."""
    img = Image.new("RGB", (3840, 2160), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 3840, 100], fill=(45, 45, 45))  # dark top bar
    draw.rectangle([0, 100, 800, 2160], fill=(245, 245, 245))  # light sidebar
    # Main content area remains white
    img.save(dest, "PNG")


def _generate_alpha_gradient(dest: Path) -> None:
    """3840x2160 RGBA horizontal gradient: transparent red → opaque blue."""
    width, height = 3840, 2160
    data = np.zeros((height, width, 4), dtype=np.uint8)
    x = np.linspace(0, 1, width, dtype=np.float32)
    data[:, :, 0] = (255 * (1 - x)).astype(np.uint8)  # Red 255→0
    data[:, :, 2] = (255 * x).astype(np.uint8)  # Blue 0→255
    data[:, :, 3] = (255 * x).astype(np.uint8)  # Alpha 0→255
    img = Image.fromarray(data, "RGBA")
    img.save(dest, "PNG")


def _setup_pathological(force: bool = False) -> dict[str, str]:
    """Generate pathological test images and return {path: sha256} dict."""
    print("Setting up pathological dataset...")
    Path("data/pathological").mkdir(parents=True, exist_ok=True)

    generators: dict[str, object] = {
        "data/pathological/solid_4k.png": _generate_solid,
        "data/pathological/noise_4k.png": _generate_noise,
        "data/pathological/screenshot_4k.png": _generate_screenshot,
        "data/pathological/alpha_gradient_4k.png": _generate_alpha_gradient,
    }

    file_checksums: dict[str, str] = {}
    for path_str, generator in generators.items():
        p = Path(path_str)
        if p.exists() and not force:
            print(f"  ✓ {p.name} (cached)")
        else:
            print(f"  Generating {p.name}...")
            generator(p)  # type: ignore[call-arg]
            print(f"  ✓ {p.name}")
        file_checksums[path_str] = _compute_sha256(p)

    print("  ✓ Pathological dataset ready (4 images)")
    return file_checksums


# ============================================================================
# Test dataset
# ============================================================================


def _generate_test_ppm(dest: Path) -> None:
    """1024x1024 random noise PPM (fixed seed 0)."""
    rng = np.random.default_rng(0)
    data = rng.integers(0, 256, (1024, 1024, 3), dtype=np.uint8)
    img = Image.fromarray(data, "RGB")
    img.save(dest, "PPM")


def _setup_test(force: bool = False) -> dict[str, str]:
    """Generate test.ppm and return {path: sha256} dict."""
    print("Setting up test dataset...")
    Path("data").mkdir(parents=True, exist_ok=True)

    dest = Path("data/test.ppm")
    if dest.exists() and not force:
        print("  ✓ test.ppm (cached)")
    else:
        print("  Generating test.ppm...")
        _generate_test_ppm(dest)
        print("  ✓ test.ppm")

    print("  ✓ Test dataset ready")
    return {"data/test.ppm": _compute_sha256(dest)}


# ============================================================================
# codec-corpus datasets (CLIC 2025, CID22, GB82-SC) — vendored as a submodule
# ============================================================================


def _ensure_codec_corpus_submodule(subdir: str) -> None:
    """Ensure the imazen/codec-corpus submodule is checked out for ``subdir``.

    On a fresh clone the submodule is just a gitlink, so this drives
    ``git submodule update --init`` to fetch it on demand (vs. Kodak's HTTP pull).
    """
    target = CODEC_CORPUS_DIR / subdir
    if target.is_dir() and any(target.iterdir()):
        return

    print(f"  Initializing {CODEC_CORPUS_DIR} submodule (this fetches images)...")
    try:
        subprocess.run(
            [
                "git",
                "submodule",
                "update",
                "--init",
                "--depth",
                "1",
                str(CODEC_CORPUS_DIR),
            ],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(
            f"Failed to initialize the codec-corpus submodule ({e}).\n"
            f"Run it manually: git submodule update --init --depth 1 {CODEC_CORPUS_DIR}"
        ) from e

    if not (target.is_dir() and any(target.iterdir())):
        raise RuntimeError(
            f"codec-corpus submodule initialized but '{target}' is empty.\n"
            f"If sparse-checkout is configured, include this path or disable it:\n"
            f"  git -C {CODEC_CORPUS_DIR} sparse-checkout disable"
        )


def _setup_codec_corpus_subset(subdir: str, label: str) -> dict[str, str]:
    """Verify a codec-corpus subdir is present (initializing the submodule if
    needed) and return ``{path: sha256}`` for its PNGs."""
    print(f"Setting up {label} dataset (codec-corpus submodule)...")
    _ensure_codec_corpus_submodule(subdir)

    files = sorted((CODEC_CORPUS_DIR / subdir).glob("*.png"))
    if not files:
        raise RuntimeError(f"No PNG images found in {CODEC_CORPUS_DIR / subdir}")

    file_checksums = {str(p): _compute_sha256(p) for p in files}
    print(f"  ✓ {label} dataset ready ({len(file_checksums)} images)")
    return file_checksums


# ============================================================================
# Tecnick (TESTIMAGES SAMPLING) dataset — downloaded, never vendored (CC BY-NC)
# ============================================================================


def _setup_tecnick(force: bool = False) -> dict[str, str]:
    """Download Tecnick SAMPLING (8-bit RGB, 1200x1200), select a diverse subset."""
    print("Setting up Tecnick (TESTIMAGES SAMPLING) dataset...")
    base = Path("data/tecnick")
    base.mkdir(parents=True, exist_ok=True)

    archive = base / "SAMPLING_8BIT_RGB_1200x1200.tar.bz2"
    selected_txt = base / "selected.txt"
    img_patterns = ("*.tif", "*.tiff", "*.png")

    # Download
    if not archive.exists() or force:
        print("  Downloading Tecnick SAMPLING 8BIT RGB 1200x1200 (~614MB)...")
        _download_with_progress(TECNICK_URL, archive, "Tecnick")
        print("  ✓ Tecnick archive downloaded")
    else:
        print("  ✓ Tecnick archive already downloaded")

    # Extract (if no source images present yet)
    present = any(next(base.rglob(p), None) is not None for p in img_patterns)
    if not present or force:
        print("  Extracting Tecnick archive...")
        with tarfile.open(archive, "r:bz2") as tf:
            try:
                tf.extractall(base, filter="data")  # py>=3.12
            except TypeError:
                tf.extractall(base)
        print("  ✓ Tecnick extracted")
    else:
        print("  ✓ Tecnick already extracted")

    # Select diverse subset
    if not selected_txt.exists() or force:
        print("  Selecting diverse images...")
        selected = _select_diverse_images(
            base, TECNICK_SELECT_N, patterns=img_patterns, recursive=True
        )
        selected_txt.write_text("\n".join(selected) + "\n")
        print(f"  ✓ Selected {len(selected)} images → {selected_txt}")
    else:
        print("  ✓ Tecnick selection already exists")
        selected = [
            line.strip()
            for line in selected_txt.read_text().splitlines()
            if line.strip()
        ]

    file_checksums: dict[str, str] = {}
    for rel in selected:
        p = base / rel
        if p.exists():
            file_checksums[f"data/tecnick/{rel}"] = _compute_sha256(p)
        else:
            print(f"  Warning: Selected file not found: {p}")

    print(f"  ✓ Tecnick dataset ready ({len(file_checksums)} selected images)")
    return file_checksums


# ============================================================================
# Public API
# ============================================================================


def ensure_dataset(dataset_id: DatasetId, force: bool = False) -> None:
    """Ensure dataset is downloaded/generated and verified. Auto-recovers missing files."""
    manifest = _load_manifest()
    datasets_manifest = manifest.setdefault("datasets", {})
    dataset_key = dataset_id.value

    # Fast path: already complete and all files verified
    if not force:
        entry = datasets_manifest.get(dataset_key, {})
        if entry.get("setup_complete"):
            files = entry.get("files", {})
            missing, corrupt = _verify_files(files)

            if not missing and not corrupt:
                return  # All good

            if corrupt:
                print(
                    f"Error: {len(corrupt)} corrupt file(s) detected in '{dataset_key}':"
                )
                for f in corrupt[:5]:
                    print(f"  - {f}")
                if len(corrupt) > 5:
                    print(f"  ... and {len(corrupt) - 5} more")
                print(
                    f"\nTo re-download/regenerate: ./bench setup -d {dataset_key} --force"
                )
                sys.exit(1)

            if missing:
                print(
                    f"  {len(missing)} missing file(s) in '{dataset_key}', recovering..."
                )
            # Fall through to re-run setup (will skip already-present files)

    # Run full setup for the dataset
    if dataset_id == DatasetId.KODAK:
        files = _setup_kodak(force)
        datasets_manifest[dataset_key] = {"files": files, "setup_complete": True}
    elif dataset_id == DatasetId.DIV2K:
        files = _setup_div2k(force)
        datasets_manifest[dataset_key] = {"files": files, "setup_complete": True}
    elif dataset_id == DatasetId.PATHOLOGICAL:
        files = _setup_pathological(force)
        datasets_manifest[dataset_key] = {
            "files": files,
            "generator_version": PATHOLOGICAL_GENERATOR_VERSION,
            "setup_complete": True,
        }
    elif dataset_id == DatasetId.TEST:
        files = _setup_test(force)
        datasets_manifest[dataset_key] = {
            "files": files,
            "generator_version": TEST_GENERATOR_VERSION,
            "setup_complete": True,
        }
    elif dataset_id in CODEC_CORPUS_SUBDIRS:
        labels = {
            DatasetId.CLIC2025: "CLIC 2025",
            DatasetId.CID22: "CID22",
            DatasetId.SCREEN: "Screen content (GB82-SC)",
        }
        files = _setup_codec_corpus_subset(
            CODEC_CORPUS_SUBDIRS[dataset_id], labels[dataset_id]
        )
        datasets_manifest[dataset_key] = {"files": files, "setup_complete": True}
    elif dataset_id == DatasetId.TECNICK:
        files = _setup_tecnick(force)
        datasets_manifest[dataset_key] = {"files": files, "setup_complete": True}
    else:
        raise ValueError(f"Unknown dataset: {dataset_id}")

    _save_manifest(manifest)


def ensure_all_datasets(force: bool = False) -> None:
    """Ensure all datasets are downloaded/generated and verified."""
    for dataset_id in DatasetId:
        ensure_dataset(dataset_id, force)


def verify_dataset(dataset_id: DatasetId) -> bool:
    """
    Verify dataset integrity without downloading or regenerating.

    Returns True if all files are present and checksums match.
    """
    manifest = _load_manifest()
    dataset_key = dataset_id.value
    entry = manifest.get("datasets", {}).get(dataset_key, {})

    if not entry.get("setup_complete"):
        print(f"  Dataset '{dataset_key}' has not been set up.")
        return False

    files = entry.get("files", {})
    missing, corrupt = _verify_files(files)

    if missing:
        print(f"  Missing files ({len(missing)}):")
        for f in missing[:5]:
            print(f"    - {f}")
        if len(missing) > 5:
            print(f"    ... and {len(missing) - 5} more")

    if corrupt:
        print(f"  Corrupt files ({len(corrupt)}):")
        for f in corrupt[:5]:
            print(f"    - {f}")
        if len(corrupt) > 5:
            print(f"    ... and {len(corrupt) - 5} more")

    if not missing and not corrupt:
        print(f"  ✓ '{dataset_key}' is intact ({len(files)} files)")
        return True

    return False
