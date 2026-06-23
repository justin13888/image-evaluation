"""Shared image canonicalization for every sweep.

Turning a source image into the 8-bit P6 PPM the codecs consume is the one
pre-processing step every sweep (quality, scaling, effort) must agree on — if
they diverged, a "lossless" round-trip could differ purely because the input was
prepared differently. This module is that single code path: one ImageMagick
conversion (``convert … -depth 8 ppm:``), one optional downscale, one atomic
publish. Keep it dependency-light (PIL + ImageMagick) so it has no import cycle
with ``runner``/``scaling``/``effort``, which all call it.
"""

import math
import os
import subprocess
import tempfile
from typing import Dict, Optional

from PIL import Image as PILImage


def single_thread_env() -> Dict[str, str]:
    """Process environment that pins rayon-/OMP-based codecs (and iqa-cli) to a
    single thread, so a parallel pool of one-thread tasks saturates the CPU
    without oversubscribing it."""
    return {**os.environ, "RAYON_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}


def image_pixels(path: str) -> int:
    """Pixel count (w*h) of an image, via PIL. 0 if unreadable."""
    try:
        with PILImage.open(path) as im:
            w, h = im.size
            return int(w) * int(h)
    except Exception:
        return 0


def to_canonical_ppm(src: str, out_ppm: str, target_px: Optional[int] = None) -> bool:
    """Canonicalize ``src`` into an 8-bit P6 PPM at ``out_ppm`` (forced 8-bit,
    since not every implementation handles 16-bit PPM), optionally downscaling to
    ~``target_px`` pixels (aspect-preserving) when ``target_px`` is given.

    The conversion goes to a unique temp file that is atomically ``os.replace``d
    onto ``out_ppm``, so a concurrent reader (the parallel pre-generation pool)
    never observes a half-written PPM. The temp name carries no ``.ppm``
    extension, so the output format is forced with ImageMagick's ``ppm:`` prefix
    rather than inferred from the suffix.

    Returns True on success (including when ``out_ppm`` already exists); False
    only when a requested downscale cannot measure the source's pixel count.
    """
    if os.path.exists(out_ppm):
        return True
    resize: list[str] = []
    if target_px is not None:
        src_px = image_pixels(src)
        if src_px <= 0:
            return False
        # Percentage resize keeps the aspect ratio; the *actual* pixel count is
        # read back from the result by callers, not assumed from target_px.
        pct = 100.0 * math.sqrt(target_px / src_px)
        resize = ["-resize", f"{pct}%"]
    os.makedirs(os.path.dirname(out_ppm) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(out_ppm) or ".", suffix=".tmp")
    os.close(fd)
    try:
        subprocess.run(
            ["convert", src, *resize, "-depth", "8", f"ppm:{tmp}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.replace(tmp, out_ppm)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return True
