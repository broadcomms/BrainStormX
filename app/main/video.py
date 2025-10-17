from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user

from .transcript_repository import TranscriptRepository
from .video_library import get_video_asset

video_bp = Blueprint("video", __name__)


def _repository() -> TranscriptRepository:
    instance_dir = Path(current_app.instance_path)
    static_root = current_app.static_folder or ""
    static_dir = Path(static_root)
    model_path_env = os.getenv("VOSK_MODEL_PATH")
    model_path = Path(model_path_env) if model_path_env else None
    ffmpeg_bin = os.getenv("FFMPEG_BIN", "ffmpeg")
    return TranscriptRepository(instance_dir=instance_dir, static_dir=static_dir, model_path=model_path, ffmpeg_bin=ffmpeg_bin)


@video_bp.route("/api/transcripts/<int:video_id>")
def get_transcript(video_id: int):
    """Return a transcript for the requested video, generating it if needed."""
    asset = get_video_asset(video_id)
    if asset is None:
        return jsonify({"error": f"Video {video_id} not found"}), 404

    language = (request.args.get("lang") or "en").lower()
    repo = _repository()

    try:
        transcript = repo.load(asset, language)
    except FileNotFoundError:
        try:
            transcript = repo.ensure(asset, language)
        except Exception as exc:  # pragma: no cover - expose reason to client
            fallback = None
            if language != "en":
                try:
                    fallback = repo.load(asset, "en")
                except FileNotFoundError:
                    pass
            if fallback is not None:
                return jsonify({**fallback, "requestedLanguage": language, "notice": str(exc)}), 200
            return jsonify({"error": str(exc)}), 500
    return jsonify(transcript)


@video_bp.route("/api/videos/<int:video_id>/progress", methods=["POST"])
def update_progress(video_id: int):
    """Persist the viewer's playback position for a video."""
    data = request.get_json(silent=True) or {}
    progress = data.get("progress")
    user_id = current_user.id if getattr(current_user, "is_authenticated", False) else None

    # TODO: Persist `progress` for (`user_id`, `video_id`) once storage is connected.
    return jsonify({
        "success": True,
        "videoId": video_id,
        "userId": user_id,
        "progress": progress,
    })


@video_bp.route("/api/transcripts/generate", methods=["POST"])
def generate_transcript():
    """Force transcript generation for a specific video/language."""
    payload = request.get_json(silent=True) or {}
    video_id = payload.get("video_id") or payload.get("videoId")
    language = (payload.get("language") or "en").lower()
    force = bool(payload.get("force", True))

    if video_id is None:
        return jsonify({"error": "video_id is required"}), 400

    try:
        video_id_int = int(video_id)
    except (TypeError, ValueError):
        return jsonify({"error": "video_id must be an integer"}), 400

    asset = get_video_asset(video_id_int)
    if asset is None:
        return jsonify({"error": f"Video {video_id_int} not found"}), 404

    repo = _repository()
    try:
        transcript = repo.ensure(asset, language, force=force)
    except Exception as exc:  # pragma: no cover - propagate failure reason
        return jsonify({"error": str(exc)}), 500

    return jsonify(transcript)