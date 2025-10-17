from __future__ import annotations

from flask import current_app, request
import re

from app.extensions import socketio, db
from app.service.tts import providers as providers_mod
from app.models import Workshop


@socketio.on("tts_request")
def handle_tts_request(payload):  # type: ignore
    """Synthesize text-to-speech and stream audio chunks to the requesting SID.

    Events emitted to the requester only:
      - tts_audio_start { mime: 'audio/wav' | 'audio/mpeg' }
      - tts_audio_chunk (binary)
      - tts_complete { duration?: float, audio_url?: str }
      - tts_error { message }
    """
    sid = getattr(request, "sid", None)
    if not sid:
        return

    def _emit(event_name: str, payload):
        server = getattr(socketio, "server", None)
        if server is not None:
            server.emit(event_name, payload, to=sid, namespace="/")
        else:
            socketio.emit(event_name, payload, to=sid, namespace="/")
    text = (payload or {}).get("text", "").strip()
    workshop_id = (payload or {}).get("workshop_id")
    provider_name = (payload or {}).get("provider")
    voice = (payload or {}).get("voice")
    try:
        speed = float((payload or {}).get("speed") or 0)
    except Exception:
        speed = 0
    # Fallback to workshop-scoped defaults stored server-side if client omitted values
    try:
        ws = getattr(request, 'workshop', None)
        if ws is None and workshop_id:
            try:
                ws = db.session.get(Workshop, int(workshop_id))
            except Exception:
                ws = None
        if not provider_name:
            provider_name = (getattr(ws, 'tts_provider', None) or current_app.config.get('TTS_PROVIDER') or 'piper')
        if not voice:
            voice = getattr(ws, 'tts_voice', None) or current_app.config.get('TTS_VOICE')
        if not speed or speed <= 0:
            speed = getattr(ws, 'tts_speed_default', None) or float(current_app.config.get('TTS_SPEED_DEFAULT', 1.0))
    except Exception:
        provider_name = provider_name or 'piper'
        voice = voice or None
        speed = speed or 1.0

    # Clamp to reasonable range for Piper mapping AFTER fallbacks are applied
    if speed <= 0:
        speed = 1.0
    if speed < 0.25:
        speed = 0.25
    if speed > 3.0:
        speed = 3.0

    fmt = (payload or {}).get("format") or ("mp3" if provider_name == "polly" else "wav")
    current_app.logger.debug(
        "TTS request: sid=%s provider=%s voice=%s fmt=%s len(text)=%d",
        sid, provider_name, voice, fmt, len(text),
    )
    if not text:
        _emit("tts_error", {"message": "No text provided"})
        return
    try:
        provider = providers_mod.get_provider(provider_name)
        mime = "audio/mpeg" if (provider_name == "polly" and fmt == "mp3") else "audio/wav"
        _emit("tts_audio_start", {"mime": mime})

        # --- Sentence-level chunking for faster first audio ---
        def _chunk_text(t: str, max_len: int = 500):
            # Split on sentence terminators and newlines, keep punctuation
            parts = re.findall(r"[^.!?\n]+[.!?]?\s*", t)
            buf = ""
            for p in parts:
                if len(buf) + len(p) > max_len and buf:
                    yield buf.strip()
                    buf = p
                else:
                    buf += p
            if buf.strip():
                yield buf.strip()

        kwargs = {"speed": speed, "fmt": fmt}
        if voice:
            # Providers may interpret voice as a voice id or a model path
            kwargs["voice"] = voice

        for seg in _chunk_text(text):
            for audio_bytes in provider.synth_stream(seg, **kwargs):
                _emit("tts_audio_chunk", audio_bytes)
            _emit("tts_flush", {})

        _emit("tts_complete", {})
    except Exception as e:  # pragma: no cover
        msg = str(e)
        hint = None
        if isinstance(e, PermissionError) or 'Permission denied' in msg:
            hint = (
                "Piper binary lacks execute permission or is quarantined. "
                "On macOS, try: xattr -d com.apple.quarantine /path/to/piper; "
                "or chmod +x /path/to/piper"
            )
        current_app.logger.error(f"TTS error via {provider_name}: {e}")
        _emit("tts_error", {"message": msg, **({"hint": hint} if hint else {})})
