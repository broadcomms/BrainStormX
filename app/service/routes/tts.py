from __future__ import annotations

from typing import Any, Dict, Iterator

from flask import Blueprint, Response, current_app, jsonify, request

from app.service.tts.providers import get_provider


tts_bp = Blueprint("tts_bp", __name__)


@tts_bp.route("/service/speech/speak", methods=["GET", "POST"])
def speak() -> Response | tuple[Response, int]:
    """Stream synthesized speech audio as HTTP response.

    Query/Form/JSON params:
      - text: required text to synthesize
      - provider: piper (default) | polly
      - voice: provider-specific voice id
      - speed: float multiplier (provider support varies)
      - format: wav | mp3 (provider support varies)
    """
    try:
        payload: Dict[str, Any] = {}
        if request.is_json:
            payload = request.get_json(silent=True) or {}
        # Accept also query string and form values
        text = (payload.get("text")
                or request.values.get("text")
                or "").strip()
        if not text:
            return jsonify({"success": False, "message": "Missing 'text'"}), 400

        provider_name = (payload.get("provider")
                         or request.values.get("provider")
                         or None)
        voice = (payload.get("voice")
                 or request.values.get("voice")
                 or None)
        speed_raw = (payload.get("speed")
                     or request.values.get("speed")
                     or None)
        try:
            speed = float(speed_raw) if speed_raw is not None else 1.0
        except Exception:
            speed = 1.0
        fmt = (payload.get("format")
               or request.values.get("format")
               or ("mp3" if (provider_name or "").lower() == "polly" else "wav"))
        fmt = fmt.lower()

        provider = get_provider(provider_name)
        mime = "audio/mpeg" if (provider.name == "polly" and fmt == "mp3") else "audio/wav"

        def generate() -> Iterator[bytes]:
            try:
                kwargs: Dict[str, Any] = {"speed": speed, "fmt": fmt}
                if voice:
                    kwargs["voice"] = voice
                for chunk in provider.synth_stream(text, **kwargs):
                    yield chunk
            except Exception as e:  # pragma: no cover
                current_app.logger.error(f"TTS REST error via {provider.name}: {e}")
                # Stop the stream; client will see truncated audio
                return

        return Response(generate(), mimetype=mime)
    except Exception as e:  # pragma: no cover
        current_app.logger.exception("TTS REST failure")
        return jsonify({"success": False, "message": str(e)}), 500


@tts_bp.route("/service/speech/marks", methods=["GET", "POST"])
def marks() -> Response | tuple[Response, int]:
    """Return word-level timing marks when supported by the TTS provider.

    Params similar to /service/speech/speak but returns JSON:
      { success: true, provider: "polly"|"piper", marks: [ { time: ms, value: word }, ... ] }
    """
    try:
        payload: Dict[str, Any] = {}
        if request.is_json:
            payload = request.get_json(silent=True) or {}
        text = (payload.get("text") or request.values.get("text") or "").strip()
        if not text:
            return jsonify({"success": False, "message": "Missing 'text'"}), 400
        provider_name = (payload.get("provider") or request.values.get("provider") or None)
        voice = (payload.get("voice") or request.values.get("voice") or None)
        speed_raw = (payload.get("speed") or request.values.get("speed") or None)
        try:
            speed = float(speed_raw) if speed_raw is not None else 1.0
        except Exception:
            speed = 1.0
        provider = get_provider(provider_name)
        marks: list[dict[str, Any]] = []
        try:
            marks = provider.get_word_marks(text, voice=voice, speed=speed) or []
        except Exception as e:  # pragma: no cover
            current_app.logger.info(f"TTS marks not available from {provider.name}: {e}")
            marks = []
        return jsonify({"success": True, "provider": provider.name, "marks": marks})
    except Exception as e:  # pragma: no cover
        current_app.logger.exception("TTS marks failure")
        return jsonify({"success": False, "message": str(e)}), 500
