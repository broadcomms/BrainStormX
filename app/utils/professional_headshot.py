"""
Streamlined Professional Headshot Pipeline
Focus on what works: background removal + professional background replacement
Enhanced with professional lighting, smart cropping, and edge refinement
"""
from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any, Dict, TypedDict, cast

import cv2
import numpy as np
from cv2 import data as cv2_data
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from app.config import Config
from app.utils.llm_bedrock import get_bedrock_runtime_client

logger = logging.getLogger(__name__)


class HeadshotMetadataRequired(TypedDict):
    stages: list[str]


class HeadshotMetadata(HeadshotMetadataRequired, total=False):
    original_dimensions: tuple[int, int] | None
    final_dimensions: tuple[int, int] | None
    nova_canvas_modified: bool
    restored_original_size: bool


def create_professional_headshot_simple(
    image_bytes: bytes,
    background_style: str = "corporate_blue",
    enable_smart_crop: bool = False,
    enable_lighting: bool = True,
    enable_edge_refinement: bool = True,
) -> tuple[bytes, str, HeadshotMetadata]:
    """
    High-quality 3-stage pipeline:
    1. Remove background (Nova Canvas) - preserve original dimensions
    2. Minimal post-processing (lighting + edge refinement combined)
    3. Add professional background - maximum quality output
    """
    stages: list[str] = []
    metadata: HeadshotMetadata = {
        "stages": stages,
        "original_dimensions": None,
        "final_dimensions": None,
    }
    
    # Get original dimensions and preserve them
    original_img = cast(Image.Image, Image.open(io.BytesIO(image_bytes)))
    original_size = original_img.size
    metadata["original_dimensions"] = original_size
    
    # Stage 1: Remove background (preserve original dimensions)
    transparent_bytes = _remove_background_nova_hq(image_bytes, original_size, metadata)
    if not transparent_bytes:
        raise RuntimeError("Background removal failed")
    
    # Stage 2: Combined post-processing (single pass for quality)
    if enable_lighting or enable_edge_refinement:
        enhanced_bytes = _enhance_combined_hq(transparent_bytes, enable_lighting, enable_edge_refinement, metadata)
        if enhanced_bytes:
            transparent_bytes = enhanced_bytes
    
    # Stage 3: Add professional background (maximum quality)
    final_bytes = _add_professional_background_hq(transparent_bytes, background_style, original_size, metadata)
    if not final_bytes:
        raise RuntimeError("Background addition failed")
    
    return final_bytes, "jpg", metadata


def _remove_background_nova_hq(
    image_bytes: bytes,
    original_size: tuple[int, int],
    metadata: HeadshotMetadata,
) -> bytes | None:
    """High-quality background removal preserving original dimensions"""
    # Work with original image for Nova Canvas constraints only
    img = cast(Image.Image, Image.open(io.BytesIO(image_bytes)))
    w, h = img.size
    nova_input_bytes = image_bytes
    
    # Only modify if absolutely necessary for Nova Canvas
    needs_modification = False
    
    # Check aspect ratio (Nova Canvas max 4:1)
    aspect_ratio = max(w, h) / min(w, h)
    if aspect_ratio > 4.0:
        needs_modification = True
        if w > h:
            new_w = int(h * 4)
            crop_x = (w - new_w) // 2
            img = img.crop((crop_x, 0, crop_x + new_w, h))
        else:
            new_h = int(w * 4)
            crop_y = (h - new_h) // 2
            img = img.crop((0, crop_y, w, crop_y + new_h))
        w, h = img.size
    
    # Check minimum dimensions
    if w < 320 or h < 320:
        needs_modification = True
        scale = max(320 / w, 320 / h)
        new_w, new_h = int(w * scale), int(h * scale)
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    
    # Only re-encode if necessary
    if needs_modification:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)  # PNG for lossless
        nova_input_bytes = buf.getvalue()
        metadata["nova_canvas_modified"] = True
    
    # Send to Nova Canvas
    client = cast(Any, get_bedrock_runtime_client())
    model_id = Config.BEDROCK_IMAGE_MODEL_ID
    b64_input = base64.b64encode(nova_input_bytes).decode("utf-8")
    
    body = {
        "taskType": "BACKGROUND_REMOVAL",
        "backgroundRemovalParams": {
            "image": b64_input
        }
    }
    
    resp: Any = client.invoke_model(
        modelId=model_id,
        accept="application/json",
        contentType="application/json",
        body=json.dumps(body).encode("utf-8"),
    )
    
    raw_body: Any = resp.get("body") if isinstance(resp, dict) else None
    if hasattr(raw_body, "read"):
        raw = cast(Any, raw_body).read()
    else:
        raw = raw_body or b"{}"
    if isinstance(raw, (bytes, bytearray)):
        body_text = bytes(raw).decode("utf-8", errors="ignore")
    elif isinstance(raw, str):
        body_text = raw
    else:
        body_text = "{}"
    payload = cast(Dict[str, Any], json.loads(body_text or "{}"))
    
    images = payload.get("images") if isinstance(payload, dict) else None
    if isinstance(images, list) and images:
        result_bytes = base64.b64decode(images[0])
        
        # Resize back to original dimensions if modified
        if needs_modification and original_size != img.size:
            result_img = cast(Image.Image, Image.open(io.BytesIO(result_bytes)))
            result_img = result_img.resize(original_size, Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            result_img.save(buf, format="PNG", optimize=False)
            result_bytes = buf.getvalue()
            metadata["restored_original_size"] = True
        
        metadata["stages"].append("background_removal_hq_success")
        return result_bytes
        
    metadata["stages"].append("background_removal_failed")
    return None


def _enhance_combined_hq(
    image_bytes: bytes,
    enable_lighting: bool,
    enable_edge_refinement: bool,
    metadata: HeadshotMetadata,
) -> bytes | None:
    """Combined high-quality enhancement in single pass"""
    img = cast(Image.Image, Image.open(io.BytesIO(image_bytes))).convert("RGBA")
    
    # Edge refinement first (if enabled)
    if enable_edge_refinement:
        alpha = img.split()[-1]
        alpha_np = np.array(alpha)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))  # Smaller kernel
        alpha_smooth = cv2.morphologyEx(alpha_np, cv2.MORPH_CLOSE, kernel)
        alpha_smooth = cv2.GaussianBlur(alpha_smooth, (1, 1), 0.3)  # Minimal blur
        refined_alpha = Image.fromarray(alpha_smooth, 'L')
        r, g, b = img.split()[:3]
        img = Image.merge('RGBA', (r, g, b, refined_alpha))
    
    # Lighting enhancement (if enabled)
    if enable_lighting:
        r, g, b, a = img.split()
        rgb_img = Image.merge('RGB', (r, g, b))
        
        # Minimal professional adjustments
        brightness = ImageEnhance.Brightness(rgb_img).enhance(1.05)  # Reduced
        contrast = ImageEnhance.Contrast(brightness).enhance(1.08)   # Reduced
        color = ImageEnhance.Color(contrast).enhance(1.02)          # Reduced
        
        r_new, g_new, b_new = color.split()
        img = Image.merge('RGBA', (r_new, g_new, b_new, a))
    
    # Save as PNG to preserve transparency
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    
    metadata["stages"].append("combined_enhancement_hq")
    return buf.getvalue()


def _add_professional_background_hq(
    image_bytes: bytes,
    bg_style: str,
    original_size: tuple[int, int],
    metadata: HeadshotMetadata,
) -> bytes | None:
    """Add professional background with maximum quality"""
    # Load subject with transparency
    subject = cast(Image.Image, Image.open(io.BytesIO(image_bytes))).convert("RGBA")
    metadata["final_dimensions"] = original_size
    
    # Create professional background matching original size
    background = _create_gradient_background(bg_style, original_size)
    
    # High-quality composite
    final_img = Image.alpha_composite(background.convert("RGBA"), subject)
    
    # Convert to RGB for JPEG output
    rgb_img = Image.new("RGB", final_img.size, (255, 255, 255))
    rgb_img.paste(final_img, mask=final_img.split()[-1] if final_img.mode == "RGBA" else None)
    
    # Maximum quality JPEG output
    buf = io.BytesIO()
    rgb_img.save(buf, format="JPEG", quality=98, optimize=False, subsampling=0)  # Maximum quality
    
    metadata["stages"].append("background_addition_hq_success")
    return buf.getvalue()


def _create_gradient_background(bg_style: str, size: tuple[int, int]) -> Image.Image:
    """Create professional gradient background"""
    w, h = size
    
    # Define professional color schemes matching UI options
    gradients = {
        "corporate_blue": [(230, 240, 255), (180, 200, 245)],  # Light to medium blue
        "studio_gray": [(245, 245, 245), (200, 200, 200)],     # Light to medium gray  
        "minimalist": [(255, 255, 255), (248, 248, 248)],      # White to off-white
        "warm_beige": [(250, 244, 234), (228, 210, 190)],      # Warm beige gradient
        "tech_dark": [(40, 44, 52), (22, 24, 28)]              # Dark professional
    }
    
    top_color, bottom_color = gradients.get(bg_style, gradients["corporate_blue"])
    
    # Create vertical gradient
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    
    for y in range(h):
        # Calculate blend ratio
        ratio = y / max(1, h - 1)
        
        # Interpolate colors
        r = int(top_color[0] * (1 - ratio) + bottom_color[0] * ratio)
        g = int(top_color[1] * (1 - ratio) + bottom_color[1] * ratio)
        b = int(top_color[2] * (1 - ratio) + bottom_color[2] * ratio)
        
        # Draw horizontal line
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    
    # Add subtle vignette for professional look
    _add_subtle_vignette(img)
    
    return img


def _smart_crop_headshot(image_bytes: bytes, metadata: HeadshotMetadata) -> bytes | None:
    """Smart cropping using face detection for optimal headshot composition"""
    # Convert to OpenCV format
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    
    # Load face cascade
    face_cascade = cv2.CascadeClassifier(cv2_data.haarcascades + 'haarcascade_frontalface_default.xml')
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = face_cascade.detectMultiScale(gray, 1.1, 4)
    
    if len(faces) > 0:
        # Use largest face
        face = max(faces, key=lambda x: x[2] * x[3])
        x, y, fw, fh = face
        
        # Calculate optimal crop for headshot (face + shoulders)
        face_center_x = x + fw // 2
        face_center_y = y + fh // 2
        
        # Headshot should include head and shoulders
        crop_height = int(fh * 3.5)  # Include shoulders
        crop_width = int(crop_height * 0.8)  # Portrait aspect
        
        # Ensure minimum dimensions for Nova Canvas (320px minimum)
        crop_height = max(crop_height, 320)
        crop_width = max(crop_width, 320)
        
        # Center crop around face, slightly above center
        crop_x = max(0, face_center_x - crop_width // 2)
        crop_y = max(0, face_center_y - int(crop_height * 0.35))  # Face in upper third
        
        # Ensure crop doesn't exceed image bounds
        crop_x = min(crop_x, w - crop_width)
        crop_y = min(crop_y, h - crop_height)
        crop_width = min(crop_width, w - crop_x)
        crop_height = min(crop_height, h - crop_y)
        
        # Final check - if still too small, skip cropping
        if crop_width < 320 or crop_height < 320:
            metadata["stages"].append("smart_crop_skipped_too_small")
            return None
        
        # Crop image
        cropped = img[crop_y:crop_y+crop_height, crop_x:crop_x+crop_width]
        
        # Convert back to bytes
        success, buffer = cv2.imencode('.jpg', cropped)
        if not success:
            metadata["stages"].append("smart_crop_encode_failed")
            return None
        metadata["stages"].append("smart_crop_success")
        return buffer.tobytes()
    
    metadata["stages"].append("smart_crop_no_face")
    return None


def _refine_edges(image_bytes: bytes, metadata: HeadshotMetadata) -> bytes | None:
    """Refine edges after background removal using morphological operations"""
    img = cast(Image.Image, Image.open(io.BytesIO(image_bytes))).convert("RGBA")
    
    # Extract alpha channel
    alpha = img.split()[-1]
    
    # Convert to numpy for morphological operations
    alpha_np = np.array(alpha)
    
    # Morphological closing to fill small gaps
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    alpha_closed = cv2.morphologyEx(alpha_np, cv2.MORPH_CLOSE, kernel)
    
    # Slight gaussian blur for smoother edges
    alpha_smooth = cv2.GaussianBlur(alpha_closed, (3, 3), 0.5)
    
    # Convert back to PIL
    refined_alpha = Image.fromarray(alpha_smooth, 'L')
    
    # Reconstruct RGBA image
    r, g, b = img.split()[:3]
    refined_img = Image.merge('RGBA', (r, g, b, refined_alpha))
    
    # Save to bytes
    buf = io.BytesIO()
    refined_img.save(buf, format="PNG")
    
    metadata["stages"].append("edge_refinement_success")
    return buf.getvalue()


def _enhance_professional_lighting(image_bytes: bytes, metadata: HeadshotMetadata) -> bytes | None:
    """Enhance lighting for professional appearance using PIL"""
    img = cast(Image.Image, Image.open(io.BytesIO(image_bytes))).convert("RGBA")
    
    # Split channels
    r, g, b, a = img.split()
    rgb_img = Image.merge('RGB', (r, g, b))
    
    # Professional lighting adjustments
    # 1. Slight brightness boost
    brightness = ImageEnhance.Brightness(rgb_img)
    rgb_img = brightness.enhance(1.1)
    
    # 2. Contrast enhancement for definition
    contrast = ImageEnhance.Contrast(rgb_img)
    rgb_img = contrast.enhance(1.15)
    
    # 3. Color saturation for healthy skin tone
    color = ImageEnhance.Color(rgb_img)
    rgb_img = color.enhance(1.05)
    
    # 4. Subtle sharpening for professional crispness
    rgb_img = rgb_img.filter(ImageFilter.UnsharpMask(radius=1, percent=120, threshold=3))
    
    # Reconstruct with original alpha
    r_new, g_new, b_new = rgb_img.split()
    enhanced_img = Image.merge('RGBA', (r_new, g_new, b_new, a))
    
    # Save to bytes
    buf = io.BytesIO()
    enhanced_img.save(buf, format="PNG")
    
    metadata["stages"].append("lighting_enhancement_success")
    return buf.getvalue()


def _add_subtle_vignette(img: Image.Image) -> None:
    """Add subtle vignette effect for professional look"""
    w, h = img.size
    
    # Create vignette mask
    vignette = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(vignette)
    
    # Create subtle radial gradient
    center_x, center_y = w // 2, h // 2
    max_radius = min(w, h) // 2
    
    for radius in range(max_radius, 0, -5):
        # Very subtle effect - only darken edges slightly
        alpha = max(200, 255 - (max_radius - radius) // 3)
        draw.ellipse([
            center_x - radius, center_y - radius,
            center_x + radius, center_y + radius
        ], fill=alpha)
    
    # Apply vignette very subtly
    vignette_img = Image.new("RGBA", (w, h), (0, 0, 0, 20))  # Very light overlay
    img.paste(vignette_img, mask=ImageOps.invert(vignette))


