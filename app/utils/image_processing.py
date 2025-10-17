from __future__ import annotations

import io
from typing import cast

from PIL import Image, ImageOps


def _ensure_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def normalize_image_bytes(
    data: bytes,
    *,
    max_dim: int = 2048,  # Increased for better quality
    target_format: str = "JPEG",
    quality: int = 95,    # Increased quality
) -> tuple[bytes, str]:
    """
    Normalize raw image bytes for consistent processing and storage.

    - Fix EXIF orientation
    - Convert to RGB (flatten alpha onto white)
    - Constrain to a square canvas with max dimension
    - Encode to JPEG by default

    Returns: (normalized_bytes, extension_without_dot)
    """
    img = cast(Image.Image, Image.open(io.BytesIO(data)))
    # Fix EXIF orientation if present
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    img = _ensure_rgb(img)

    # Constrain dimensions while preserving aspect ratio (use high-quality resampling)
    w, h = img.size
    scale = min(1.0, float(max_dim) / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

    # High-quality encode
    buf = io.BytesIO()
    fmt = target_format.upper()
    if fmt == "JPG":
        fmt = "JPEG"
    
    if fmt == "JPEG":
        # Maximum quality JPEG settings
        img.save(buf, format=fmt, quality=quality, optimize=False, subsampling=0)
    else:
        img.save(buf, format=fmt, quality=quality, optimize=True)
    
    ext = "jpg" if fmt == "JPEG" else fmt.lower()
    return buf.getvalue(), ext
