import os
import io
import base64
import time
from collections import deque, defaultdict
from typing import DefaultDict, Deque
from datetime import datetime
from flask import Blueprint, current_app, request, jsonify, send_from_directory
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app.extensions import db
from app.config import Config
from app.utils.bedrock_image import enhance_headshot
from app.utils.image_processing import normalize_image_bytes
from app.utils.telemetry import log_event
import boto3
from botocore.config import Config as BotoConfig

ALLOWED_EXT = {"png", "jpg", "jpeg", "gif"}

# Simple per-user in-memory rate limiter (best-effort; single-process)
_user_buckets: DefaultDict[tuple[int, str], Deque[float]] = defaultdict(deque)
_LIMIT = 5
_WINDOW = 60  # seconds


def _check_rate_limit(user_id: int, key: str) -> bool:
	now = time.time()
	q = _user_buckets[(user_id, key)]
	# Evict old entries
	while q and now - q[0] > _WINDOW:
		q.popleft()
	if len(q) >= _LIMIT:
		return False
	q.append(now)
	return True


def _ensure_media_dirs():
	os.makedirs(Config.MEDIA_PHOTOS_DIR, exist_ok=True)


def _save_photo_bytes(data: bytes, filename_hint: str) -> str:
	_ensure_media_dirs()
	base = secure_filename(filename_hint) or "photo.png"
	ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
	fname = f"u{current_user.user_id}_{ts}_{base}"
	path = os.path.join(Config.MEDIA_PHOTOS_DIR, fname)
	with open(path, "wb") as f:
		f.write(data)
	return fname


photo_bp = Blueprint("photo_bp", __name__)
media_bp = Blueprint("media_bp", __name__)


@media_bp.route(f"{Config.MEDIA_PHOTOS_URL_PREFIX}/<path:filename>")
@login_required
def media_photos(filename):
	# Serve from instance/uploads/photos
	directory = Config.MEDIA_PHOTOS_DIR
	return send_from_directory(directory, filename)


@photo_bp.route("/upload", methods=["POST"])
@login_required
def upload_photo():
	file = request.files.get("profile_pic")
	if not file or not file.filename:
		return jsonify({"ok": False, "error": "No file uploaded"}), 400
	ext = (file.filename.rsplit(".", 1)[-1] or "").lower()
	if ext not in ALLOWED_EXT:
		return jsonify({"ok": False, "error": "Invalid file type"}), 400
	if hasattr(file, 'content_length') and file.content_length and file.content_length > Config.MAX_UPLOAD_BYTES:
		return jsonify({"ok": False, "error": "File too large"}), 400

	# High-quality normalize and convert to JPEG
	norm_bytes, norm_ext = normalize_image_bytes(file.read(), max_dim=2048, target_format="JPEG", quality=95)
	fname = _save_photo_bytes(norm_bytes, f"upload.{norm_ext}")
	# Update user profile to served URL
	current_user.profile_pic_url = f"{Config.MEDIA_PHOTOS_URL_PREFIX}/{fname}"
	db.session.commit()
	log_event("photo.upload", {"user": current_user.user_id, "url": current_user.profile_pic_url})
	return jsonify({"ok": True, "url": current_user.profile_pic_url})


@photo_bp.route("/capture", methods=["POST"])
@login_required
def capture_photo():
	# Expect JSON with { imageData: 'data:image/png;base64,...' }
	req_json = request.get_json(silent=True) or {}
	data_url = req_json.get("imageData") if request.is_json else None
	if not data_url or "," not in data_url:
		return jsonify({"ok": False, "error": "Invalid image data"}), 400
	header, b64 = data_url.split(",", 1)
	try:
		raw = base64.b64decode(b64)
	except Exception:
		return jsonify({"ok": False, "error": "Bad base64"}), 400

	# High-quality normalize to JPEG
	norm_bytes, norm_ext = normalize_image_bytes(raw, max_dim=2048, target_format="JPEG", quality=95)
	fname = _save_photo_bytes(norm_bytes, f"capture.{norm_ext}")
	current_user.profile_pic_url = f"{Config.MEDIA_PHOTOS_URL_PREFIX}/{fname}"
	db.session.commit()
	log_event("photo.capture", {"user": current_user.user_id, "url": current_user.profile_pic_url})
	return jsonify({"ok": True, "url": current_user.profile_pic_url})


@photo_bp.route("/enhance", methods=["POST"])
@login_required 
def enhance_photo():
	# Rate limit
	if not _check_rate_limit(current_user.user_id, "enhance"):
		log_event("photo.enhance.rate_limited", {"user": current_user.user_id})
		return jsonify({"ok": False, "error": "Too many requests. Please wait a moment and try again."}), 429
	"""Enhanced photo processing with advanced Bedrock AI and fallback options."""
	# Accept either multipart file or JSON data URL
	raw_bytes = None
	filename_hint = "input.png"

	if request.files.get("profile_pic"):
		f = request.files["profile_pic"]
		filename_hint = f.filename or filename_hint
		raw_bytes = f.read()
	elif request.is_json:
		req_json = request.get_json(silent=True) or {}
		data_url = req_json.get("imageData")
		filename_hint = req_json.get("filename") or filename_hint
		if data_url:
			if "," in data_url:
				# data URL case
				_, b64 = data_url.split(",", 1)
				try:
					raw_bytes = base64.b64decode(b64)
				except Exception:
					return jsonify({"ok": False, "error": "Bad base64"}), 400
			else:
				# Treat as a URL path; if it's our media prefix, load from disk
				try:
					media_prefix = Config.MEDIA_PHOTOS_URL_PREFIX.rstrip("/")
					if data_url.startswith(media_prefix + "/"):
						rel_name = data_url[len(media_prefix)+1:]
						file_path = os.path.join(Config.MEDIA_PHOTOS_DIR, rel_name)
						if not os.path.isfile(file_path):
							return jsonify({"ok": False, "error": "Image file not found"}), 400
						with open(file_path, "rb") as fp:
							raw_bytes = fp.read()
					else:
						return jsonify({"ok": False, "error": "Unsupported image URL"}), 400
				except Exception as e:
					return jsonify({"ok": False, "error": f"Failed to read image: {e}"}), 400

	if not raw_bytes:
		return jsonify({"ok": False, "error": "No image provided"}), 400

	# Enhanced parameter handling
	req_json = request.get_json(silent=True) or {}
	enhancement_style = (
		request.form.get("enhancement_style") or 
		(req_json.get("enhancement_style") if request.is_json else None) or 
		"corporate"
	)
	background_style = (
		request.form.get("background_style") or 
		(req_json.get("background_style") if request.is_json else None) or 
		"corporate_blue"
	)
	remove_background_only = bool(
		(request.form.get("remove_background_only") == "true") or 
		(req_json.get("remove_background_only", False) if request.is_json else False)
	)
	use_professional_pipeline = bool(
		(request.form.get("use_professional_pipeline") == "true") or 
		(req_json.get("use_professional_pipeline", False) if request.is_json else False)
	)
	outfit_enhancement = bool(
		(request.form.get("outfit_enhancement") == "true") or 
		(req_json.get("outfit_enhancement", False) if request.is_json else False)
	)
	# New enhancement options
	enable_smart_crop = (
		request.form.get("enable_smart_crop") == "true" or 
		(req_json.get("enable_smart_crop", False) if request.is_json else False)
	)
	enable_lighting = (
		request.form.get("enable_lighting") == "true" or 
		(req_json.get("enable_lighting", True) if request.is_json else True)
	)
	enable_edge_refinement = (
		request.form.get("enable_edge_refinement") == "true" or 
		(req_json.get("enable_edge_refinement", True) if request.is_json else True)
	)
	# Allow callers to opt out of Bedrock when local fallback is desired (tests, offline dev)
	use_bedrock = True
	if request.is_json:
		use_bedrock = bool(req_json.get("use_bedrock", True))
	else:
		flag = request.form.get("use_bedrock")
		if flag is not None:
			use_bedrock = flag.lower() not in {"false", "0", "off"}

	# Process with enhanced AI system
	try:
		out_bytes, ext, metadata = enhance_headshot(
			raw_bytes, 
			enhancement_style=enhancement_style,
			background_style=background_style,
			use_bedrock=use_bedrock,
			remove_background_only=remove_background_only,
			use_professional_pipeline=use_professional_pipeline,
			outfit_enhancement=outfit_enhancement,
			enable_smart_crop=enable_smart_crop,
			enable_lighting=enable_lighting,
			enable_edge_refinement=enable_edge_refinement
		)
		# High-quality normalize output
		norm_bytes, norm_ext = normalize_image_bytes(out_bytes, max_dim=2048, target_format="JPEG", quality=98)
		fname = _save_photo_bytes(norm_bytes, f"enhanced_{enhancement_style}.{norm_ext}")
		current_user.profile_pic_url = f"{Config.MEDIA_PHOTOS_URL_PREFIX}/{fname}"
		db.session.commit()
		log_event("photo.enhance", {
			"user": current_user.user_id,
			"url": current_user.profile_pic_url,
			"style": enhancement_style,
			"background": background_style,
			"metadata": metadata,
			"enhancements": {
				"smart_crop": enable_smart_crop,
				"lighting": enable_lighting,
				"edge_refinement": enable_edge_refinement
			}
		})
        
		return jsonify({
			"ok": True, 
			"url": current_user.profile_pic_url,
			"metadata": metadata,
			"enhancement_style": enhancement_style,
			"background_style": background_style,
			"enhancements_applied": {
				"smart_crop": enable_smart_crop,
				"lighting": enable_lighting,
				"edge_refinement": enable_edge_refinement
			}
		})
		
	except Exception as e:
		log_event("photo.enhance.error", {"user": current_user.user_id, "error": str(e)})
		return jsonify({
			"ok": False, 
			"error": f"Enhancement failed: {str(e)}"
		}), 500


@photo_bp.route("/variants", methods=["POST"])
@login_required
def generate_variants():
	if not _check_rate_limit(current_user.user_id, "variants"):
		return jsonify({"ok": False, "error": "Too many requests. Please wait."}), 429
	"""Generate multiple style variants for user selection."""
	# Get image data
	raw_bytes = None
	if request.files.get("profile_pic"):
		raw_bytes = request.files["profile_pic"].read()
	elif request.is_json:
		req_json = request.get_json(silent=True) or {}
		data_url = req_json.get("imageData")
		if data_url and "," in data_url:
			_, b64 = data_url.split(",", 1)
			try:
				raw_bytes = base64.b64decode(b64)
			except Exception:
				return jsonify({"ok": False, "error": "Bad base64"}), 400

	if not raw_bytes:
		return jsonify({"ok": False, "error": "No image provided"}), 400

	# Generate variants
	from app.utils.bedrock_image import generate_style_variants
	try:
		variants = generate_style_variants(raw_bytes)
		
		# Save all variants and return URLs
		variant_urls = {}
		for style, (img_bytes, ext) in variants.items():
			# High-quality normalize
			norm_bytes, norm_ext = normalize_image_bytes(img_bytes, max_dim=2048, target_format="JPEG", quality=98)
			fname = _save_photo_bytes(norm_bytes, f"variant_{style}.{norm_ext}")
			variant_urls[style] = f"{Config.MEDIA_PHOTOS_URL_PREFIX}/{fname}"
			
		return jsonify({
			"ok": True,
			"variants": variant_urls
		})
		
	except Exception as e:
		return jsonify({
			"ok": False,
			"error": f"Variant generation failed: {str(e)}"
		}), 500


	@photo_bp.route("/validate", methods=["POST"])
	@login_required
	def validate_photo():
		"""Validate a saved photo using AWS Rekognition heuristics.
		Expects JSON { image_url: "/media/photos/filename.jpg" }.
		Returns JSON { ok, passed, message, details }.
		"""
		if not (Config.AWS_ACCESS_KEY_ID and Config.AWS_SECRET_ACCESS_KEY and Config.AWS_REGION):
			return jsonify({"ok": False, "error": "AWS not configured"}), 400

		req = request.get_json(silent=True) or {}
		image_url = req.get("image_url") or ""
		media_prefix = Config.MEDIA_PHOTOS_URL_PREFIX.rstrip("/")
		if not image_url.startswith(media_prefix + "/"):
			return jsonify({"ok": False, "error": "Unsupported image URL"}), 400
		rel_name = image_url[len(media_prefix)+1:]
		file_path = os.path.join(Config.MEDIA_PHOTOS_DIR, rel_name)
		if not os.path.isfile(file_path):
			return jsonify({"ok": False, "error": "File not found"}), 404

		try:
			with open(file_path, "rb") as f:
				img_bytes = f.read()

			rek = boto3.client(
				"rekognition",
				region_name=Config.AWS_REGION,
				aws_access_key_id=Config.AWS_ACCESS_KEY_ID,
				aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY,
				aws_session_token=Config.AWS_SESSION_TOKEN,
				config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"})
			)

			resp = rek.detect_faces(Image={"Bytes": img_bytes}, Attributes=["ALL"])
			faces = resp.get("FaceDetails", [])
			if not faces:
				return jsonify({"ok": True, "passed": False, "message": "No face detected", "details": resp})
			f0 = faces[0]

			# Heuristic checks
			eyes_open = (f0.get("EyesOpen", {}).get("Value") is True)
			sharpness = (f0.get("Quality", {}).get("Sharpness") or 0)
			brightness = (f0.get("Quality", {}).get("Brightness") or 0)
			conf = f0.get("Confidence", 0)

			passed = eyes_open and sharpness >= 30 and brightness >= 30 and conf >= 90
			msg = None
			if not eyes_open:
				msg = "Eyes appear closed"
			elif sharpness < 30:
				msg = "Image may be blurry"
			elif brightness < 30:
				msg = "Image may be too dark"
			elif conf < 90:
				msg = "Low face confidence"
			else:
				msg = "Quality checks passed"

			log_event("photo.validate", {"user": current_user.user_id, "url": image_url, "passed": passed})
			return jsonify({"ok": True, "passed": passed, "message": msg, "details": {"eyes_open": eyes_open, "sharpness": sharpness, "brightness": brightness, "confidence": conf}})

		except Exception as e:
			log_event("photo.validate.error", {"user": current_user.user_id, "error": str(e)})
			return jsonify({"ok": False, "error": f"Validation error: {e}"}), 500


@photo_bp.route("/styles", methods=["GET"])
@login_required
def get_available_styles():
	"""Get available enhancement and background styles."""
	from app.utils.bedrock_image import EnhancementStyle, BackgroundStyle
	
	enhancement_styles = [
		{
			"key": style.name.lower(),
			"name": style.name.replace("_", " ").title(),
			"description": _get_style_description(style)
		}
		for style in EnhancementStyle
	]
	
	background_styles = [
		{
			"key": style.name.lower(), 
			"name": style.name.replace("_", " ").title(),
			"description": style.value
		}
		for style in BackgroundStyle
	]
	
	return jsonify({
		"enhancement_styles": enhancement_styles,
		"background_styles": background_styles
	})


def _get_style_description(style):
	"""Get user-friendly descriptions for enhancement styles."""
	descriptions = {
		"CORPORATE": "Clean, professional style perfect for business cards and company profiles",
		"LINKEDIN": "Optimized for LinkedIn and professional networking platforms", 
		"EXECUTIVE": "Premium executive style with authoritative presence",
		"CREATIVE": "Contemporary creative professional style with artistic flair",
		"FRIENDLY": "Warm and approachable professional style for client-facing roles",
		"AUTHORITATIVE": "Strong, confident business style for leadership positions"
	}
	return descriptions.get(style.name, style.value)
