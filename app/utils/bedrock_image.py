import base64
import io
import json
import logging
from typing import Optional, Tuple, Dict, Any, List, cast
from enum import Enum

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageDraw
import numpy as np

from app.utils.llm_bedrock import get_bedrock_runtime_client
from app.utils.image_processing import _ensure_rgb
from app.config import Config

logger = logging.getLogger(__name__)


def _to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _load_image(image_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def _resize_square(img: Image.Image, size: int = 768) -> Image.Image:
    # Keep aspect, pad to square canvas
    # Ensure minimum size for Nova Canvas (320px minimum)
    size = max(size, 512)  # Use 512 as safe minimum
    w, h = img.size
    scale = size / max(w, h)
    nw, nh = int(w * scale), int(h * scale)
    # Pillow 10+ uses Image.Resampling.LANCZOS; fallback for older versions
    try:
        resample = Image.Resampling.LANCZOS  # type: ignore[attr-defined]
    except Exception:
        resample = Image.LANCZOS  # type: ignore[attr-defined]
    resized = img.resize((nw, nh), resample)
    canvas = Image.new("RGB", (size, size), (255, 255, 255))
    ox = (size - nw) // 2
    oy = (size - nh) // 2
    canvas.paste(resized, (ox, oy))
    return canvas


def _find_b64_in_obj(obj: Any) -> Optional[str]:
    """Recursively search for a plausible base64-encoded image string in a JSON-like object."""
    # Heuristic: strings containing base64 alphabet and of reasonable size
    def looks_like_b64(s: str) -> bool:
        if not isinstance(s, str) or len(s) < 200:
            return False
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r")
        sample = s[:400]
        return all(ch in allowed for ch in sample)

    if isinstance(obj, dict):
        # Prefer common keys first
        for key in ("image", "base64", "b64_json", "data", "b64", "bytes"):
            if key in obj and isinstance(obj[key], str) and looks_like_b64(obj[key]):
                return obj[key]
        # Nested common containers
        for key in ("source", "content", "image", "output", "result"):
            if key in obj:
                found = _find_b64_in_obj(obj[key])
                if found:
                    return found
        # Fallback: scan all values
        for v in obj.values():
            found = _find_b64_in_obj(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_b64_in_obj(item)
            if found:
                return found
    elif isinstance(obj, str) and looks_like_b64(obj):
        return obj
    return None


def _soft_subject_mask(img: Image.Image) -> Image.Image:
    """Create a soft circular mask favoring the center (approximate head/shoulders)."""
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    # Elliptical mask centered
    ellipse = Image.new("L", (w, h), 0)
    from PIL import ImageDraw
    draw = ImageDraw.Draw(ellipse)
    pad_w, pad_h = int(0.12 * w), int(0.18 * h)
    draw.ellipse((pad_w, pad_h, w - pad_w, h - pad_h), fill=255)
    # Feather
    mask = ellipse.filter(ImageFilter.GaussianBlur(radius=int(min(w, h) * 0.05)))
    return mask


def _gradient_background(size: tuple[int, int], theme: str) -> Image.Image:
    w, h = size
    top, bottom = (255, 255, 255), (240, 240, 240)
    if theme == "corporate_blue":
        top, bottom = (230, 240, 255), (180, 200, 245)
    elif theme == "studio_gray":
        top, bottom = (245, 245, 245), (200, 200, 200)
    elif theme == "modern_teal":
        top, bottom = (224, 245, 242), (170, 220, 215)
    elif theme == "warm_beige":
        top, bottom = (250, 244, 234), (228, 210, 190)
    elif theme == "tech_dark":
        top, bottom = (40, 44, 52), (22, 24, 28)
    elif theme == "minimalist":
        top, bottom = (255, 255, 255), (242, 242, 242)

    bg = Image.new("RGB", (w, h), top)
    d = ImageDraw.Draw(bg)
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        d.line([(0, y), (w, y)], fill=(r, g, b))
    return bg


class BackgroundStyle(Enum):
    CORPORATE_BLUE = "professional gradient background in corporate blue and white"
    STUDIO_GRAY = "clean studio background in soft gray tones"
    MODERN_TEAL = "modern gradient background in teal and light blue"
    WARM_BEIGE = "warm professional background in beige and cream tones"
    TECH_DARK = "sophisticated dark background with subtle tech patterns"
    MINIMALIST = "pure white minimalist background with soft shadows"


class EnhancementStyle(Enum):
    CORPORATE = "modern corporate style, professional lighting, high detail"
    LINKEDIN = "linkedin-style headshot, business professional, crisp and clean"
    EXECUTIVE = "executive portrait style, premium lighting, sophisticated"
    CREATIVE = "creative professional style, artistic lighting, contemporary"
    FRIENDLY = "approachable professional style, warm lighting, inviting"
    AUTHORITATIVE = "authoritative business style, strong lighting, confident"


def _detect_face_region(img: Image.Image) -> Optional[Tuple[int, int, int, int]]:
    """
    Simple face detection using image analysis. Returns (left, top, right, bottom) or None.
    This is a basic implementation - in production, use a proper face detection library.
    """
    try:
        # Convert to grayscale for analysis
        gray = img.convert('L')
        
        # Apply edge detection to find prominent features
        edges = gray.filter(ImageFilter.FIND_EDGES)
        
        # For now, assume face is in the center 60% of the image
        w, h = img.size
        margin_w, margin_h = int(w * 0.2), int(h * 0.2)
        return (margin_w, margin_h, w - margin_w, h - margin_h)
    except Exception:
        return None


def _apply_professional_lighting(img: Image.Image) -> Image.Image:
    """Apply professional lighting adjustments to enhance facial features."""
    try:
        # Enhance brightness and contrast for professional look
        brightness = ImageEnhance.Brightness(img)
        img = brightness.enhance(1.1)
        
        # Increase contrast slightly for definition
        contrast = ImageEnhance.Contrast(img)
        img = contrast.enhance(1.15)
        
        # Enhance color saturation for healthy skin tones
        color = ImageEnhance.Color(img)
        img = color.enhance(1.05)
        
        # Apply subtle sharpening for crisp details
        sharpness = ImageEnhance.Sharpness(img)
        img = sharpness.enhance(1.2)
        
        return img
    except Exception:
        return img


def _create_professional_variant(img: Image.Image, style: EnhancementStyle) -> Image.Image:
    """Create different professional variants based on enhancement style."""
    try:
        if style == EnhancementStyle.CORPORATE:
            # Clean, conservative enhancement
            brightness = ImageEnhance.Brightness(img).enhance(1.08)
            contrast = ImageEnhance.Contrast(brightness).enhance(1.12)
            return ImageEnhance.Color(contrast).enhance(1.03)
            
        elif style == EnhancementStyle.EXECUTIVE:
            # Strong, authoritative enhancement
            contrast = ImageEnhance.Contrast(img).enhance(1.25)
            sharpness = ImageEnhance.Sharpness(contrast).enhance(1.3)
            return ImageEnhance.Brightness(sharpness).enhance(1.05)
            
        elif style == EnhancementStyle.CREATIVE:
            # Artistic, contemporary enhancement
            color = ImageEnhance.Color(img).enhance(1.15)
            brightness = ImageEnhance.Brightness(color).enhance(1.12)
            return ImageEnhance.Contrast(brightness).enhance(1.1)
            
        elif style == EnhancementStyle.FRIENDLY:
            # Warm, approachable enhancement
            brightness = ImageEnhance.Brightness(img).enhance(1.15)
            color = ImageEnhance.Color(brightness).enhance(1.08)
            return ImageEnhance.Contrast(color).enhance(1.05)
            
        else:  # LINKEDIN, AUTHORITATIVE
            # Balanced professional enhancement
            return _apply_professional_lighting(img)
            
    except Exception:
        return img


def _fallback_enhancement_pipeline(
    base_img: Image.Image,
    *,
    enhancement_style: str,
    background_style: str,
    remove_background_only: bool,
    enable_lighting: bool,
    enable_edge_refinement: bool,
    metadata: Dict[str, Any],
) -> Tuple[bytes, str]:
    """Local PIL-based enhancement used when Bedrock is unavailable or disabled."""
    metadata["fallback_used"] = True
    steps = cast(List[str], metadata.setdefault("processing_steps", []))
    steps.append("fallback_pipeline")
    work_img = base_img.copy()

    try:
        style_enum = EnhancementStyle[enhancement_style.upper()]
    except Exception:
        style_enum = EnhancementStyle.CORPORATE
    if enable_lighting:
        work_img = _apply_professional_lighting(work_img)
    work_img = _create_professional_variant(work_img, style_enum)

    if remove_background_only:
        # Produce a transparent PNG with softened mask
        mask = _soft_subject_mask(base_img)
        subject = work_img.convert("RGBA")
        canvas = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
        canvas.paste(subject, mask=mask)
        metadata["fallback_background"] = "transparent"
        return _to_png_bytes(canvas), "png"

    try:
        bg_enum = BackgroundStyle[background_style.upper()]
        bg_key = bg_enum.name.lower()
    except Exception:
        bg_key = "corporate_blue"
    metadata["fallback_background"] = bg_key
    background = _gradient_background(base_img.size, bg_key)
    mask = _soft_subject_mask(base_img)
    composite = Image.composite(work_img, background, mask)
    if enable_edge_refinement:
        try:
            composite = composite.filter(ImageFilter.SMOOTH_MORE)
        except Exception:
            pass
    return _to_png_bytes(composite), "png"


def enhance_headshot(
    image_bytes: bytes,
    *,
    enhancement_style: str = "corporate",
    background_style: str = "corporate_blue", 
    size: int = 1024,
    use_bedrock: bool = True,
    remove_background_only: bool = False,
    use_professional_pipeline: bool = False,
    outfit_enhancement: bool = False,
    enable_smart_crop: bool = True,
    enable_lighting: bool = True,
    enable_edge_refinement: bool = True,
) -> Tuple[bytes, str, Dict[str, Any]]:
    """
    Use Amazon Bedrock image model to enhance a headshot with professional AI processing.
    
    Args:
        image_bytes: Raw image data
        enhancement_style: Style of enhancement (corporate, linkedin, executive, etc.)
        background_style: Background style (corporate_blue, studio_gray, etc.)
        size: Output size (will be made square)
        use_bedrock: Whether to attempt Bedrock AI enhancement
    
    Returns:
        Tuple of (enhanced_image_bytes, file_extension, metadata_dict)
    """
    metadata: Dict[str, Any] = {
        "enhancement_style": enhancement_style,
        "background_style": background_style,
        "bedrock_used": False,
        "fallback_used": False,
        "processing_steps": []
    }
    processing_steps = cast(List[str], metadata["processing_steps"])
    
    # Preprocess for better results
    try:
        img = _load_image(image_bytes)
        processing_steps.append("image_loaded")
        metadata["original_size"] = img.size
    except Exception as e:
        logger.error(f"Failed to load image: {e}")
        return image_bytes, "jpg", metadata

    # Resize and prepare - ensure minimum size for Nova Canvas
    target_size = min(max(size, 512), 1536)
    img = _resize_square(img, target_size)
    processing_steps.append("resized_square")
    metadata["processed_size"] = img.size
    metadata["target_size"] = target_size
    
    # Validate image size for Nova Canvas
    if img.size[0] < 320 or img.size[1] < 320:
        logger.warning(f"Image size {img.size} may be too small for Nova Canvas (min 320px)")
        metadata["size_warning"] = f"Image size {img.size} may be too small"
    
    # Apply face detection for better cropping
    face_region = _detect_face_region(img)
    if face_region:
        metadata["face_detected"] = True
        processing_steps.append("face_detected")
    
    pre_bytes = _to_png_bytes(img)

    fallback_reason: Optional[str] = None

    if use_bedrock:
        try:
            if use_professional_pipeline:
                from app.utils.professional_headshot import create_professional_headshot_simple

                pipeline_bytes, ext, pipeline_metadata = create_professional_headshot_simple(
                    image_bytes,
                    background_style,
                    enable_smart_crop=enable_smart_crop,
                    enable_lighting=enable_lighting,
                    enable_edge_refinement=enable_edge_refinement
                )
                metadata.update(pipeline_metadata)
                metadata["professional_pipeline_used"] = True
                metadata["bedrock_used"] = True
                return pipeline_bytes, ext, metadata

            bedrock_bytes: Optional[bytes]
            if remove_background_only:
                bedrock_bytes = _remove_background_with_bedrock(pre_bytes, img.size, metadata)
            else:
                bedrock_bytes = _enhance_with_bedrock(
                    pre_bytes, img.size, enhancement_style, background_style, metadata
                )
            if bedrock_bytes:
                metadata["bedrock_used"] = True
                return bedrock_bytes, "png", metadata
            fallback_reason = "empty_response"
        except Exception as e:
            logger.error(f"Bedrock enhancement failed: {e}")
            metadata["bedrock_error"] = str(e)
            fallback_reason = "exception"
            try:
                from app.utils.telemetry import log_event
                log_event("photo.enhance.bedrock_error", metadata)
            except Exception:
                pass
    else:
        metadata["bedrock_skipped"] = True
        fallback_reason = "disabled"

    # Fallback pipeline (local PIL adjustments)
    metadata["fallback_reason"] = fallback_reason or "unknown"
    fallback_bytes, fallback_ext = _fallback_enhancement_pipeline(
        img,
        enhancement_style=enhancement_style,
        background_style=background_style,
        remove_background_only=remove_background_only,
        enable_lighting=enable_lighting,
        enable_edge_refinement=enable_edge_refinement,
        metadata=metadata,
    )
    return fallback_bytes, fallback_ext, metadata


def _remove_background_with_bedrock(
    image_bytes: bytes,
    image_size: Tuple[int, int],
    metadata: Dict[str, Any],
) -> Optional[bytes]:
    """Remove background using Nova Canvas BACKGROUND_REMOVAL task."""
    try:
        client = get_bedrock_runtime_client()
        model_id = Config.BEDROCK_IMAGE_MODEL_ID
        metadata["bedrock_model"] = model_id
        metadata["task_type"] = "BACKGROUND_REMOVAL"

        # Request body for background removal
        b64_input = base64.b64encode(image_bytes).decode("utf-8")
        
        body = {
            "taskType": "BACKGROUND_REMOVAL",
            "backgroundRemovalParams": {
                "image": b64_input
            }
        }
        
        logger.debug(f"Sending background removal request to Nova Canvas")
        resp = client.invoke_model(
            modelId=model_id,
            accept="application/json",
            contentType="application/json",
            body=json.dumps(body).encode("utf-8"),
        )
        
        raw_body = resp.get("body")
        if hasattr(raw_body, "read"):
            raw = raw_body.read()
        else:
            raw = raw_body or b"{}"
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        payload = json.loads(raw or "{}")
        
        logger.debug(f"Background removal response: {payload}")
        
        # Nova Canvas response format
        if isinstance(payload, dict) and "images" in payload:
            images = payload["images"]
            if images and len(images) > 0:
                b64_data = images[0]
                if b64_data:
                    try:
                        enhanced_bytes = base64.b64decode(b64_data)
                        _ = _load_image(enhanced_bytes)
                        metadata["background_removal_success"] = True
                        return enhanced_bytes
                    except Exception as decode_error:
                        logger.debug(f"Failed to decode image data: {decode_error}")
                        metadata["decode_error"] = str(decode_error)
        
        return None
    except Exception as e:
        logger.error(f"Background removal failed: {e}")
        metadata["background_removal_error"] = str(e)
        return None


def _enhance_with_bedrock(
    image_bytes: bytes,
    image_size: Tuple[int, int],
    enhancement_style: str,
    background_style: str,
    metadata: Dict[str, Any],
) -> Optional[bytes]:
    """Enhanced Bedrock integration with better prompts and error handling."""
    try:
        client = get_bedrock_runtime_client()
        model_id = Config.BEDROCK_IMAGE_MODEL_ID
        metadata["bedrock_model"] = model_id

        # Prompt text - focused on preserving facial features
        style_prompts = {
            "corporate": "professional business headshot with enhanced lighting only",
            "linkedin": "LinkedIn-style professional portrait with improved lighting",
            "executive": "executive portrait with premium lighting setup",
            "creative": "creative professional headshot with artistic lighting",
            "friendly": "approachable professional portrait with warm lighting",
            "authoritative": "commanding business portrait with strong lighting",
        }
        background_prompts = {
            "corporate_blue": "clean corporate gradient background in professional blue tones",
            "studio_gray": "neutral studio background in soft gray, professional photography setup",
            "modern_teal": "contemporary gradient background in teal and blue, modern professional",
            "warm_beige": "warm professional background in beige and cream, approachable corporate",
            "tech_dark": "sophisticated dark background with subtle patterns, tech professional",
            "minimalist": "pure white background with subtle shadows, minimalist professional",
        }
        style_text = style_prompts.get(enhancement_style, style_prompts["corporate"])
        bg_text = background_prompts.get(background_style, background_prompts["corporate_blue"])
        text_prompt = (
            f"PRESERVE the person's exact face only cropped out as captured in the image "
            f"DO NOT alter any face, eyes, nose, mouth, or any facial hair characteristics. "
            f"ONLY crop the user and apply new background with {bg_text} and improve lighting. "
            f"Keep the person's natural appearance and cross check to ensure 100% identical. "
            f"Apply {style_text} lighting enhancement without changing any facial details. "
            f"Maintain original face as is, only enhance lighting and background. "
        )
        metadata["bedrock_prompt"] = text_prompt

        # Request body for Nova Canvas
        b64_input = base64.b64encode(image_bytes).decode("utf-8")
        
        # Nova Canvas API format
        body = {
            "taskType": "IMAGE_VARIATION",
            "imageVariationParams": {
                "text": text_prompt,
                "images": [b64_input]
            },
            "imageGenerationConfig": {
                "numberOfImages": 1,
                "quality": "standard",
                "height": min(max(512, image_size[1]), 1024),
                "width": min(max(512, image_size[0]), 1024),
                "cfgScale": 2.0  # Very conservative to preserve original face
            }
        }
        
        model_configs = [body]

        for i, body in enumerate(model_configs):
            try:
                resp = client.invoke_model(
                    modelId=model_id,
                    accept="application/json",
                    contentType="application/json",
                    body=json.dumps(body).encode("utf-8"),
                )
                raw_body = resp.get("body")
                if hasattr(raw_body, "read"):
                    raw = raw_body.read()
                else:
                    raw = raw_body or b"{}"
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                payload = json.loads(raw or "{}")
                metadata[f"bedrock_config_{i}"] = "attempted"
                logger.debug(f"Nova Canvas response payload: {payload}")

                # Nova Canvas response format
                if isinstance(payload, dict) and "images" in payload:
                    images = payload["images"]
                    if images and len(images) > 0:
                        b64_data = images[0]
                        if b64_data:
                            try:
                                enhanced_bytes = base64.b64decode(b64_data)
                                _ = _load_image(enhanced_bytes)
                                metadata[f"bedrock_config_{i}"] = "success"
                                return enhanced_bytes
                            except Exception as decode_error:
                                logger.debug(f"Failed to decode image data: {decode_error}")
                                metadata[f"bedrock_config_{i}_decode_error"] = str(decode_error)
                else:
                    b64_any = _find_b64_in_obj(payload)
                    if b64_any:
                        try:
                            enhanced_bytes = base64.b64decode(b64_any)
                            _ = _load_image(enhanced_bytes)
                            metadata[f"bedrock_config_{i}"] = "success_scanned"
                            return enhanced_bytes
                        except Exception:
                            pass

                try:
                    metadata[f"bedrock_config_{i}_payload_keys"] = list(payload.keys()) if isinstance(payload, dict) else "non-dict"
                except Exception:
                    pass
            except Exception as config_error:
                metadata[f"bedrock_config_{i}_error"] = str(config_error)
                logger.debug(f"Bedrock config {i} failed: {config_error}")
                continue

        return None
    except Exception as e:
        logger.error(f"Bedrock enhancement failed: {e}")
        metadata["bedrock_error"] = str(e)
        return None


def generate_style_variants(
    image_bytes: bytes, 
    styles: Optional[List[str]] = None
) -> Dict[str, Tuple[bytes, str]]:
    """
    Generate multiple style variants of a headshot for user selection.
    
    Args:
        image_bytes: Original image data
        styles: List of style names to generate
        
    Returns:
        Dict mapping style names to (image_bytes, extension) tuples
    """
    if styles is None:
        styles = ["corporate", "linkedin", "executive", "creative"]
        
    variants = {}
    
    for style in styles:
        try:
            enhanced_bytes, ext, _ = enhance_headshot(
                image_bytes,
                enhancement_style=style,
                background_style="corporate_blue",
                use_bedrock=True
            )
            variants[style] = (enhanced_bytes, ext)
        except Exception as e:
            logger.error(f"Failed to generate {style} variant: {e}")
            
    return variants
