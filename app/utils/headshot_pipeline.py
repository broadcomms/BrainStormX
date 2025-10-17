"""
Professional Headshot Pipeline - World-class AI-powered headshot editing
Combines multiple Nova Canvas tasks for professional results while preserving facial identity
"""
import base64
import io
import json
import logging
from typing import Optional, Tuple, Dict, Any, List
from enum import Enum
from dataclasses import dataclass

from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import numpy as np

from app.utils.llm_bedrock import get_bedrock_runtime_client
from app.utils.image_processing import _ensure_rgb
from app.config import Config

logger = logging.getLogger(__name__)


@dataclass
class HeadshotConfig:
    """Configuration for professional headshot generation"""
    style: str = "corporate"
    background: str = "corporate_blue"
    lighting: str = "professional"
    crop_style: str = "headshot"  # headshot, bust, full_upper
    outfit_style: Optional[str] = None  # business_suit, blazer, casual_professional
    quality: str = "premium"


class ProfessionalStyle(Enum):
    CORPORATE_EXECUTIVE = "corporate_executive"
    LINKEDIN_PROFESSIONAL = "linkedin_professional"
    CREATIVE_PROFESSIONAL = "creative_professional"
    MEDICAL_PROFESSIONAL = "medical_professional"
    TECH_PROFESSIONAL = "tech_professional"
    ACADEMIC_PROFESSIONAL = "academic_professional"


class BackgroundTheme(Enum):
    CORPORATE_GRADIENT = "corporate_gradient"
    STUDIO_NEUTRAL = "studio_neutral"
    OFFICE_ENVIRONMENT = "office_environment"
    MODERN_MINIMALIST = "modern_minimalist"
    EXECUTIVE_LIBRARY = "executive_library"
    TECH_WORKSPACE = "tech_workspace"


def create_professional_headshot(
    image_bytes: bytes,
    config: HeadshotConfig,
    preserve_identity: bool = True
) -> Tuple[bytes, str, Dict[str, Any]]:
    """
    Create a professional headshot using multi-stage AI pipeline
    
    Stage 1: Background Removal (preserve subject)
    Stage 2: Professional Cropping & Framing
    Stage 3: Outfit Enhancement (optional)
    Stage 4: Background Replacement (outpainting)
    Stage 5: Professional Lighting & Polish
    """
    metadata = {
        "pipeline_stages": [],
        "config": config.__dict__,
        "preserve_identity": preserve_identity,
        "processing_time": 0
    }
    
    try:
        # Stage 1: Remove background while preserving subject
        stage1_result = _remove_background_preserve_subject(image_bytes, metadata)
        if not stage1_result:
            raise RuntimeError("Stage 1: Background removal failed")
        
        # Stage 2: Professional cropping and framing
        stage2_result = _apply_professional_cropping(stage1_result, config, metadata)
        if stage2_result is None:
            raise RuntimeError("Stage 2: Professional cropping failed")
        
        # Stage 3: Outfit enhancement (if requested)
        stage3_result: bytes = stage2_result
        if config.outfit_style:
            stage3_candidate = _enhance_professional_outfit(stage2_result, config, metadata)
            if stage3_candidate is None:
                logger.warning("Stage 3: Outfit enhancement failed, continuing with original")
            else:
                stage3_result = stage3_candidate
        
        # Stage 4: Professional background replacement
        stage4_result = _replace_professional_background(stage3_result, config, metadata)
        if stage4_result is None:
            raise RuntimeError("Stage 4: Background replacement failed")
        
        # Skip Stage 5 to preserve user's original face
        metadata["pipeline_success"] = True
        return stage4_result, "png", metadata
        
    except Exception as e:
        logger.error(f"Professional headshot pipeline failed: {e}")
        metadata["pipeline_error"] = str(e)
        raise


def _remove_background_preserve_subject(image_bytes: bytes, metadata: Dict[str, Any]) -> Optional[bytes]:
    """Stage 1: Remove background while preserving subject identity"""
    try:
        client = get_bedrock_runtime_client()
        model_id = Config.BEDROCK_IMAGE_MODEL_ID
        
        b64_input = base64.b64encode(image_bytes).decode("utf-8")
        
        body = {
            "taskType": "BACKGROUND_REMOVAL",
            "backgroundRemovalParams": {
                "image": b64_input
            }
        }
        
        resp = client.invoke_model(
            modelId=model_id,
            accept="application/json",
            contentType="application/json",
            body=json.dumps(body).encode("utf-8"),
        )
        
        payload = _parse_response(resp)
        if payload and "images" in payload and payload["images"]:
            result = base64.b64decode(payload["images"][0])
            metadata["pipeline_stages"].append("background_removal_success")
            return result
            
        return None
    except Exception as e:
        logger.error(f"Background removal failed: {e}")
        metadata["pipeline_stages"].append(f"background_removal_failed: {e}")
        return None


def _apply_professional_cropping(image_bytes: bytes, config: HeadshotConfig, metadata: Dict[str, Any]) -> Optional[bytes]:
    """Stage 2: Apply professional cropping using PIL for transparent images"""
    try:
        # Use PIL for cropping since Nova Canvas doesn't handle transparent images well
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        
        # Find the bounding box of non-transparent pixels
        bbox = img.getbbox()
        if not bbox:
            metadata["pipeline_stages"].append("professional_cropping_failed: no content")
            return None
            
        # Crop to content with professional margins
        left, top, right, bottom = bbox
        width = right - left
        height = bottom - top
        
        # Add professional margins based on crop style
        margin_factors = {
            "headshot": 0.15,  # Tight crop for headshots
            "bust": 0.25,     # More space for bust shots
            "full_upper": 0.35 # Most space for upper body
        }
        
        margin_factor = margin_factors.get(config.crop_style, 0.15)
        margin_w = int(width * margin_factor)
        margin_h = int(height * margin_factor)
        
        # Calculate crop bounds with margins
        crop_left = max(0, left - margin_w)
        crop_top = max(0, top - margin_h)
        crop_right = min(img.width, right + margin_w)
        crop_bottom = min(img.height, bottom + margin_h)
        
        # Crop the image
        cropped = img.crop((crop_left, crop_top, crop_right, crop_bottom))
        
        # Resize to standard dimensions while maintaining aspect ratio
        target_size = 1024
        cropped.thumbnail((target_size, target_size), Image.Resampling.LANCZOS)
        
        # Create final canvas
        final_img = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
        
        # Center the cropped image
        paste_x = (target_size - cropped.width) // 2
        paste_y = (target_size - cropped.height) // 2
        final_img.paste(cropped, (paste_x, paste_y), cropped)
        
        # Convert back to bytes
        buf = io.BytesIO()
        final_img.save(buf, format="PNG")
        
        metadata["pipeline_stages"].append("professional_cropping_success")
        return buf.getvalue()
        
    except Exception as e:
        logger.error(f"Professional cropping failed: {e}")
        metadata["pipeline_stages"].append(f"professional_cropping_failed: {e}")
        return None


def _enhance_professional_outfit(image_bytes: bytes, config: HeadshotConfig, metadata: Dict[str, Any]) -> Optional[bytes]:
    """Stage 3: Enhance outfit using inpainting to preserve face"""
    try:
        client = get_bedrock_runtime_client()
        model_id = Config.BEDROCK_IMAGE_MODEL_ID
        
        # Create a mask for clothing area (avoid face)
        mask_b64 = _create_clothing_mask(image_bytes)
        b64_input = base64.b64encode(image_bytes).decode("utf-8")
        
        outfit_prompts = {
            "business_suit": "professional business suit with crisp shirt and tie, executive appearance",
            "blazer": "professional blazer with dress shirt, smart business casual", 
            "casual_professional": "professional casual attire, polished and workplace appropriate"
        }

        style_key = config.outfit_style or "business_suit"
        prompt = outfit_prompts.get(style_key, outfit_prompts["business_suit"])
        
        body = {
            "taskType": "INPAINTING",
            "inPaintingParams": {
                "text": prompt,
                "image": b64_input,
                "maskImage": mask_b64
            },
            "imageGenerationConfig": {
                "numberOfImages": 1,
                "quality": config.quality,
                "height": 1024,
                "width": 1024,
                "cfgScale": 4.0
            }
        }
        
        resp = client.invoke_model(
            modelId=model_id,
            accept="application/json",
            contentType="application/json",
            body=json.dumps(body).encode("utf-8"),
        )
        
        payload = _parse_response(resp)
        if payload and "images" in payload and payload["images"]:
            result = base64.b64decode(payload["images"][0])
            metadata["pipeline_stages"].append("outfit_enhancement_success")
            return result
            
        return None
    except Exception as e:
        logger.error(f"Outfit enhancement failed: {e}")
        metadata["pipeline_stages"].append(f"outfit_enhancement_failed: {e}")
        return None


def _replace_professional_background(image_bytes: bytes, config: HeadshotConfig, metadata: Dict[str, Any]) -> Optional[bytes]:
    """Stage 4: Replace background using PIL composite"""
    try:
        # Use PIL to composite subject onto generated background
        subject_img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        
        # Create professional background
        bg_img = _create_professional_background(config.background, subject_img.size)
        
        # Composite subject onto background
        final_img = Image.alpha_composite(bg_img.convert("RGBA"), subject_img)
        
        # Convert to RGB
        final_rgb = Image.new("RGB", final_img.size, (255, 255, 255))
        final_rgb.paste(final_img, mask=final_img.split()[-1] if final_img.mode == "RGBA" else None)
        
        # Convert back to bytes
        buf = io.BytesIO()
        final_rgb.save(buf, format="PNG")
        
        metadata["pipeline_stages"].append("background_replacement_success")
        return buf.getvalue()
        
    except Exception as e:
        logger.error(f"Background replacement failed: {e}")
        metadata["pipeline_stages"].append(f"background_replacement_failed: {e}")
        return None


def _apply_professional_polish(image_bytes: bytes, config: HeadshotConfig, metadata: Dict[str, Any]) -> Optional[bytes]:
    """Stage 5: Apply final professional polish and lighting"""
    try:
        client = get_bedrock_runtime_client()
        model_id = Config.BEDROCK_IMAGE_MODEL_ID
        
        b64_input = base64.b64encode(image_bytes).decode("utf-8")
        
        lighting_prompts = {
            "professional": "professional photography lighting, soft key light with subtle fill, natural skin tones",
            "executive": "premium executive portrait lighting, sophisticated and authoritative",
            "creative": "creative professional lighting with artistic flair, contemporary and polished"
        }
        
        prompt = f"Apply final professional polish: {lighting_prompts.get(config.lighting, lighting_prompts['professional'])}. Enhance image quality, sharpness, and professional appearance while preserving all facial features exactly as they are."
        
        body = {
            "taskType": "IMAGE_VARIATION",
            "imageVariationParams": {
                "text": prompt,
                "images": [b64_input]
            },
            "imageGenerationConfig": {
                "numberOfImages": 1,
                "quality": "premium",
                "height": 1024,
                "width": 1024,
                "cfgScale": 4.0
            }
        }
        
        resp = client.invoke_model(
            modelId=model_id,
            accept="application/json",
            contentType="application/json",
            body=json.dumps(body).encode("utf-8"),
        )
        
        payload = _parse_response(resp)
        if payload and "images" in payload and payload["images"]:
            result = base64.b64decode(payload["images"][0])
            metadata["pipeline_stages"].append("professional_polish_success")
            return result
            
        return None
    except Exception as e:
        logger.error(f"Professional polish failed: {e}")
        metadata["pipeline_stages"].append(f"professional_polish_failed: {e}")
        return None


def _create_background_mask(image_bytes: bytes) -> str:
    """Create a mask for background area (for outpainting)"""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        w, h = img.size
        
        # Create mask where transparent areas (background) are white, subject is black
        mask = Image.new("L", (w, h), 0)  # Start with black
        
        # If image has transparency, use alpha channel
        if img.mode == "RGBA":
            alpha = img.split()[-1]
            # Invert alpha: transparent areas become white (to be painted)
            mask = ImageOps.invert(alpha)
        else:
            # Create a simple edge-based mask
            mask = Image.new("L", (w, h), 255)  # White background to be painted
            # Create a rough subject area (center ellipse) as black
            from PIL import ImageDraw
            draw = ImageDraw.Draw(mask)
            margin_w, margin_h = int(w * 0.2), int(h * 0.15)
            draw.ellipse((margin_w, margin_h, w - margin_w, h - margin_h), fill=0)
        
        # Convert mask to base64
        buf = io.BytesIO()
        mask.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
        
    except Exception as e:
        logger.error(f"Mask creation failed: {e}")
        # Return a simple center mask
        mask = Image.new("L", (1024, 1024), 255)
        from PIL import ImageDraw
        draw = ImageDraw.Draw(mask)
        draw.ellipse((200, 150, 824, 900), fill=0)
        buf = io.BytesIO()
        mask.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")


def _create_clothing_mask(image_bytes: bytes) -> str:
    """Create a mask for clothing area (avoid face for inpainting)"""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        w, h = img.size
        
        # Create mask for clothing area (lower 60% of image, avoid face)
        mask = Image.new("L", (w, h), 0)  # Start with black (don't paint)
        
        from PIL import ImageDraw
        draw = ImageDraw.Draw(mask)
        
        # Paint clothing area (lower portion, avoid face)
        face_boundary = int(h * 0.4)  # Assume face is in upper 40%
        clothing_top = face_boundary
        clothing_bottom = h
        
        # Create clothing mask (white = paint this area)
        draw.rectangle((0, clothing_top, w, clothing_bottom), fill=255)
        
        # Convert mask to base64
        buf = io.BytesIO()
        mask.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
        
    except Exception as e:
        logger.error(f"Clothing mask creation failed: {e}")
        # Return a simple lower-body mask
        mask = Image.new("L", (1024, 1024), 0)
        from PIL import ImageDraw
        draw = ImageDraw.Draw(mask)
        draw.rectangle((0, 400, 1024, 1024), fill=255)
        buf = io.BytesIO()
        mask.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")


def _create_professional_background(bg_type: str, size: Tuple[int, int]) -> Image.Image:
    """Create professional background using PIL"""
    w, h = size
    
    if bg_type == "corporate_gradient":
        # Blue gradient
        bg = Image.new("RGB", (w, h), (230, 240, 255))
        from PIL import ImageDraw
        draw = ImageDraw.Draw(bg)
        for y in range(h):
            t = y / max(1, h - 1)
            r = int(230 * (1 - t) + 180 * t)
            g = int(240 * (1 - t) + 200 * t)
            b = int(255 * (1 - t) + 245 * t)
            draw.line([(0, y), (w, y)], fill=(r, g, b))
    elif bg_type == "studio_neutral":
        # Gray gradient
        bg = Image.new("RGB", (w, h), (245, 245, 245))
        from PIL import ImageDraw
        draw = ImageDraw.Draw(bg)
        for y in range(h):
            t = y / max(1, h - 1)
            gray = int(245 * (1 - t) + 200 * t)
            draw.line([(0, y), (w, y)], fill=(gray, gray, gray))
    else:
        # Default white
        bg = Image.new("RGB", (w, h), (255, 255, 255))
    
    return bg


def _parse_response(resp) -> Optional[Dict[str, Any]]:
    """Parse Nova Canvas response"""
    try:
        raw_body = resp.get("body")
        if hasattr(raw_body, "read"):
            raw = raw_body.read()
        else:
            raw = raw_body or b"{}"
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        return json.loads(raw or "{}")
    except Exception as e:
        logger.error(f"Response parsing failed: {e}")
        return None


def get_professional_presets() -> Dict[str, HeadshotConfig]:
    """Get predefined professional headshot configurations"""
    return {
        "corporate_executive": HeadshotConfig(
            style="corporate_executive",
            background="corporate_gradient",
            lighting="executive",
            crop_style="headshot",
            outfit_style="business_suit",
            quality="premium"
        ),
        "linkedin_professional": HeadshotConfig(
            style="linkedin_professional", 
            background="studio_neutral",
            lighting="professional",
            crop_style="headshot",
            outfit_style="blazer",
            quality="premium"
        ),
        "creative_professional": HeadshotConfig(
            style="creative_professional",
            background="modern_minimalist",
            lighting="creative",
            crop_style="bust",
            outfit_style="casual_professional",
            quality="premium"
        ),
        "medical_professional": HeadshotConfig(
            style="medical_professional",
            background="studio_neutral",
            lighting="professional",
            crop_style="headshot",
            outfit_style="blazer",
            quality="premium"
        ),
        "tech_professional": HeadshotConfig(
            style="tech_professional",
            background="tech_workspace",
            lighting="professional",
            crop_style="bust",
            outfit_style="casual_professional",
            quality="premium"
        )
    }