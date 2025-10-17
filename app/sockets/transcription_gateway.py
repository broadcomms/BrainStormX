"""Socket.IO handlers for live transcription (scaffold).

Events (client -> server):
  stt_start { workshop_id, language, sampleRate }
  stt_audio_chunk { workshop_id, seq, sampleRate, payloadBase64, codec }
  stt_stop { workshop_id }

Server emits:
  stt_partial { workshop_id, user_id, text, startTs }
  transcript_final { workshop_id, transcript_id, user_id, text, startTs, endTs }
  stt_error { workshop_id, message }

Persistence:
  - Dialogue rows (optional for partials) & Transcript rows for finals.

NOTE: This is a minimal first pass; production version should add
authZ, rate limiting, provider pooling, and better error handling.
"""
from __future__ import annotations
import asyncio
import threading
import base64
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Tuple
import re
import os

from flask_socketio import emit
from flask import current_app

from app import socketio  # assumes global socketio instance in app.__init__
from app.models import db, Transcript, Dialogue, Workshop, WorkshopParticipant
from app.transcription import ProviderConfig
from app.transcription.factory import create_provider

# Optional direct Vosk simplified path (bypasses async provider wrapper) for reliability
try:  # pragma: no cover - optional dependency
    from vosk import Model as _VoskModel, KaldiRecognizer as _VoskRecognizer  # type: ignore
except Exception:  # noqa
    _VoskModel = None  # type: ignore
    _VoskRecognizer = None  # type: ignore

# Shared Vosk model (loaded once) and helpers
_VOSK_MODEL_SINGLETON: dict[str, object | None] = {
    'model': None,
    'path': None,
}

def _load_vosk_model_simple(sample_rate: int):  # type: ignore[unused-ignore]
    if _VoskModel is None:
        raise RuntimeError('Vosk not installed. pip install vosk and download a model.')
    if _VOSK_MODEL_SINGLETON['model'] is not None:
        return _VOSK_MODEL_SINGLETON['model']
    model_path = os.getenv('VOSK_MODEL_PATH')
    if not model_path or not os.path.isdir(model_path):
        raise RuntimeError('VOSK_MODEL_PATH not set or invalid; set env var to downloaded model directory.')
    _VOSK_MODEL_SINGLETON['model'] = _VoskModel(model_path)  # type: ignore[call-arg,assignment]
    _VOSK_MODEL_SINGLETON['path'] = model_path  # type: ignore[assignment]
    return _VOSK_MODEL_SINGLETON['model']

log = logging.getLogger(__name__)

def _audit(event: str, **fields):
    """Emit a structured audit log line for compliance / traceability.

    Format: AUDIT | event=... key=value ...  (values with whitespace are JSON quoted)
    """
    parts = [f"event={event}"]
    for k, v in fields.items():
        if v is None:
            continue
        sv = str(v)
        if ' ' in sv or '\t' in sv:
            import json as _json
            sv = _json.dumps(sv)
        parts.append(f"{k}={sv}")
    log.info('AUDIT | ' + ' '.join(parts))

_sessions: Dict[Tuple[int, int], dict] = {}

def _seconds_to_dt(seconds: float | None) -> datetime | None:
    if seconds is None:
        return None
    try:
        return datetime.utcfromtimestamp(float(seconds))
    except Exception:
        return None

def _authorized(workshop_id: int, user_id: int | None) -> bool:
    """Return True if user may access transcription for the workshop.

    Rules:
      - Workshop must exist and have transcription_enabled flag True
      - User must be creator OR an accepted participant (status == 'accepted')
    """
    if user_id is None:
        return False
    ws: Workshop | None = db.session.get(Workshop, workshop_id)
    if not ws:
        return False
    if ws.created_by_id == user_id:
        return True
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop_id, user_id=user_id, status='accepted'
    ).first()
    return participant is not None

@socketio.on('stt_start')
def stt_start(data):
    """Start (or reuse) a transcription session for a workshop/user."""
    log.debug(f"[DEBUG] stt_start received: {data}")
    workshop_id = int(data.get('workshop_id'))
    raw_uid = getattr(getattr(data, 'user_id', None), 'user_id', None) or data.get('user_id')
    try:
        user_id_val = int(raw_uid) if raw_uid is not None else None
    except (TypeError, ValueError):
        user_id_val = None
    user_id = user_id_val
    language = data.get('language', 'en-US')
    sample_rate = int(data.get('sampleRate') or data.get('sample_rate') or 16000)

    if not _authorized(workshop_id, user_id):
        log.debug("[DEBUG] stt_start: Authorization failed for workshop_id=%s, user_id=%s", workshop_id, user_id)
        _audit('transcription_start_denied', workshop_id=workshop_id, user_id=user_id, reason='unauthorized')
        emit('stt_error', {'workshop_id': workshop_id, 'message': 'Unauthorized'})
        return

    key = (workshop_id, int(user_id))  # type: ignore[arg-type]
    log.debug("[DEBUG] stt_start: Authorization passed for workshop_id=%s, user_id=%s", workshop_id, user_id)
    force = bool(data.get('force'))

    # Reuse or replace existing session
    existing_session = _sessions.get(key)
    if existing_session is not None:
        task = existing_session.get('task')
        thread_alive = bool(task and hasattr(task, 'is_alive') and task.is_alive())
        is_active = existing_session.get('active', False)
        last_chunk = existing_session.get('last_chunk_ts') or 0
        stale = (time.time() - last_chunk) > 30 if last_chunk else False
        if is_active and thread_alive and not force and not stale:
            log.debug("Session (%s, %s) already active; returning existing readiness (idempotent)", workshop_id, user_id)
            _audit('transcription_start_idempotent', workshop_id=workshop_id, user_id=user_id, provider=existing_session.get('provider_name'))
            ready_payload = {
                'workshop_id': workshop_id,
                'user_id': user_id,
                'provider': existing_session.get('provider_name') or 'unknown'
            }
            mp = existing_session.get('model_path')
            if mp:
                ready_payload['model_path'] = mp
            socketio.emit('stt_ready', ready_payload, to=f'workshop_room_{workshop_id}')
            return
        log.debug("Session (%s, %s) being replaced (stale=%s, force=%s, active=%s, thread_alive=%s)", workshop_id, user_id, stale, force, is_active, thread_alive)
        _audit('transcription_start_replace', workshop_id=workshop_id, user_id=user_id, stale=stale, force=force)
        prev = _sessions.pop(key, None)
        if prev:
            queue = prev.get('queue')
            loop = prev.get('loop')
            try:
                if queue is not None:
                    if isinstance(loop, asyncio.AbstractEventLoop) and not loop.is_closed():
                        loop.call_soon_threadsafe(queue.put_nowait, None)
                    else:
                        queue.put_nowait(None)  # type: ignore
            except Exception:
                pass

    ws = db.session.get(Workshop, workshop_id)
    log.debug("[DEBUG] stt_start: Workshop %s exists: %s, transcription_enabled: %s", workshop_id, ws is not None, getattr(ws, 'transcription_enabled', False) if ws else 'N/A')
    if not ws or not getattr(ws, 'transcription_enabled', False):
        log.debug("[DEBUG] stt_start: Transcription disabled for workshop %s", workshop_id)
        _audit('transcription_start_denied', workshop_id=workshop_id, user_id=user_id, reason='feature_disabled')
        emit('stt_error', {'workshop_id': workshop_id, 'message': 'Transcription disabled'})
        return

    requested_provider = data.get('provider') or data.get('stt_provider')
    if isinstance(requested_provider, str):
        requested_provider = requested_provider.strip() or None
    try:
        provider, provider_name_emit = create_provider(requested_provider or os.getenv('TRANSCRIPTION_PROVIDER') or os.getenv('STT_PROVIDER'))
    except Exception as e:  # noqa
        emit('stt_error', {'workshop_id': workshop_id, 'message': str(e)})
        return

    cfg = ProviderConfig(language_code=language, sample_rate_hz=sample_rate)
    model_path = os.getenv('VOSK_MODEL_PATH') if provider_name_emit == 'vosk' else None

    if provider_name_emit == 'aws_transcribe':
        vocab_name = (data.get('vocabulary') or data.get('vocab') or ws and getattr(ws, 'stt_vocab_name', None) or None)
        if not vocab_name:
            vocab_name = os.getenv('AWS_TRANSCRIBE_VOCABULARY_NAME')
        vocab_filter_name = (data.get('vocabulary_filter') or data.get('vocab_filter') or None)
        if not vocab_filter_name:
            vocab_filter_name = os.getenv('AWS_TRANSCRIBE_VOCABULARY_FILTER_NAME')
        if vocab_name or vocab_filter_name:
            try:
                loop = asyncio.new_event_loop()
                try:
                    missing = loop.run_until_complete(provider.ensure_capabilities(vocab_name, vocab_filter_name))  # type: ignore[attr-defined]
                finally:
                    loop.close()
                if missing:
                    emit('stt_error', {
                        'workshop_id': workshop_id,
                        'code': 'CAPABILITY_MISSING',
                        'message': 'One or more transcription resources are missing',
                        'missing': missing,
                    })
                    return
            except Exception:
                log.exception('Capability check failed; proceeding without validation')

    # SPECIAL CASE: simplified synchronous Vosk path (user requested less complexity)
    # If provider is Vosk we bypass the async/thread orchestration and feed audio
    # chunks directly into a KaldiRecognizer stored in the session. This avoids
    # event loop edge-cases seen in development and provides immediate, reliable
    # partial + final emission + DB persistence.
    if provider_name_emit == 'vosk':
        try:
            model = _load_vosk_model_simple(sample_rate)
            recognizer = _VoskRecognizer(model, sample_rate)  # type: ignore[arg-type]
            try:
                recognizer.SetWords(True)  # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception as e:  # noqa
            emit('stt_error', { 'workshop_id': workshop_id, 'message': str(e) })
            return

        log.debug(f"[SIMPLE VOSK] Session ({workshop_id},{user_id}) initialized (model={_VOSK_MODEL_SINGLETON['path']})")
        _audit('transcription_start', workshop_id=workshop_id, user_id=user_id, provider='vosk', model=_VOSK_MODEL_SINGLETON['path'])
        _sessions[key] = {
            'provider_name': 'vosk',
            'recognizer': recognizer,
            'seq': 0,
            'partials': 0,
            'finals': 0,
            'active': True,
            'model_path': _VOSK_MODEL_SINGLETON['path'],
            'language': language,
            'last_chunk_ts': time.time(),
        }
        ready_payload = {
            'workshop_id': workshop_id,
            'user_id': user_id,
            'provider': 'vosk',
            'model_path': _VOSK_MODEL_SINGLETON['path'],
        }
        log.debug(f"[SIMPLE VOSK] Emitting stt_ready to room and caller: {ready_payload}")
        # Broadcast to room (others may show caption source) and directly to initiating client
        socketio.emit('stt_ready', ready_payload, to=f'workshop_room_{workshop_id}')
        try:
            emit('stt_ready', ready_payload)  # direct acknowledgement
        except Exception:
            pass
        return  # Done; no async path

    # Use an internal queue to bridge sync socket handlers and async provider (non-Vosk providers)
    audio_queue: asyncio.Queue[bytes] = asyncio.Queue()

    def _map_provider_exception(exc: Exception) -> dict:
        """Return a structured error payload for common AWS Transcribe failures."""
        msg = str(exc)
        code = 'PROVIDER_ERROR'
        hint = None
        s = msg.lower()
        if 'subscriptionrequired' in s or 'subscriptionrequiredexception' in s:
            code = 'SUBSCRIPTION_REQUIRED'
            hint = 'Enable Amazon Transcribe in the AWS console or attach required permissions.'
        elif 'accessdenied' in s or 'accessdeniedexception' in s:
            code = 'ACCESS_DENIED'
            hint = 'Check IAM policy (transcribe:StartStreamTranscription) or SCP restrictions.'
        elif 'unrecognizedclientexception' in s or 'signaturedoesnotmatch' in s:
            code = 'INVALID_CREDENTIALS'
            hint = 'Verify AWS keys / session token and region; rotate exposed credentials.'
        elif 'throttl' in s:
            code = 'THROTTLED'
            hint = 'Reduce concurrent sessions or add backoff.'
        elif 'limitexceeded' in s:
            code = 'LIMIT_EXCEEDED'
            hint = 'Account concurrency or request quota exceeded.'
        elif 'network' in s or 'connection' in s:
            code = 'NETWORK'
            hint = 'Transient network issue; retry may succeed.'
        return {k: v for k, v in {
            'code': code,
            'message': msg,
            'hint': hint,
        }.items() if v is not None}

    async def _runner():
        try:
            loop = asyncio.get_running_loop()
            # Store loop and mark session as active BEFORE opening stream
            # This prevents timing race where chunks arrive before session is ready
            session_ref = _sessions.get(key)  # type: ignore[arg-type]
            if session_ref is not None:
                session_ref['loop'] = loop
            # Open provider stream ONCE (thread main no longer opens it)
            await provider.open_stream(f"ws{workshop_id}_u{user_id}", cfg)
            if session_ref is not None:
                session_ref['active'] = True
                log.debug(f"Session ({workshop_id}, {user_id}) marked active after provider initialization")
            _audit('transcription_start', workshop_id=workshop_id, user_id=user_id, provider=provider_name_emit, model=model_path)
            # Emit readiness now that provider is opened
            ready_payload = { 'workshop_id': workshop_id, 'user_id': user_id, 'provider': provider_name_emit }
            if requested_provider and requested_provider.lower() != provider_name_emit:
                ready_payload['provider_alias'] = requested_provider
            if model_path:
                ready_payload['model_path'] = model_path
            socketio.emit('stt_ready', ready_payload, to=f'workshop_room_{workshop_id}')
            # Two concurrent tasks: feeding audio & reading results
            async def _feeder():
                while True:
                    chunk = await audio_queue.get()
                    if chunk is None:  # type: ignore
                        break
                    try:
                        await provider.write(chunk)
                    except Exception as werr:  # noqa
                        log.exception('Write error')
                        socketio.emit('stt_error', { 'workshop_id': workshop_id, 'message': str(werr) }, to=f'workshop_room_{workshop_id}')
            async def _reader():
                from typing import cast, AsyncIterator

                async for evt in cast(AsyncIterator[Any], provider.aresults()):
                    try:
                        if evt.is_final:
                            # Server-side guard: ignore human finals while facilitator is speaking
                            try:
                                from app.sockets.state import is_facilitator_playing
                                if is_facilitator_playing(int(workshop_id)):
                                    # Still update stats but do not persist/emit
                                    sess_ref = _sessions.get(key)  # type: ignore[arg-type]
                                    if sess_ref is not None:
                                        sess_ref['finals'] = sess_ref.get('finals', 0) + 1
                                    continue
                            except Exception:
                                pass
                            transcript = Transcript()  # type: ignore[call-arg]
                            transcript.workshop_id = workshop_id
                            transcript.user_id = user_id
                            try:
                                transcript.entry_type = 'human'
                            except Exception:
                                pass
                            transcript.raw_stt_transcript = evt.text
                            transcript.processed_transcript = evt.text
                            transcript.language = language
                            transcript.start_timestamp = None if evt.start_time is None else _seconds_to_dt(evt.start_time)
                            transcript.end_timestamp = None if evt.end_time is None else _seconds_to_dt(evt.end_time)
                            db.session.add(transcript)
                            db.session.flush()
                            final_dialogue = Dialogue()  # type: ignore[call-arg]
                            final_dialogue.workshop_id = workshop_id
                            final_dialogue.speaker_id = user_id
                            final_dialogue.transcript_id = transcript.transcript_id
                            final_dialogue.dialogue_text = evt.text
                            final_dialogue.is_final = True
                            db.session.add(final_dialogue)
                            db.session.commit()
                            # Clear any cached partial id (final completed)
                            sess_ref = _sessions.get(key)  # type: ignore[arg-type]
                            if sess_ref:
                                sess_ref.pop('partial_dialogue_id', None)
                            sess_ref = _sessions.get(key)  # type: ignore[arg-type]
                            if sess_ref is not None:
                                sess_ref['finals'] = sess_ref.get('finals', 0) + 1
                            # Include basic speaker name for immediate rendering
                            try:
                                speaker = WorkshopParticipant.query.filter_by(workshop_id=workshop_id, user_id=user_id).first()
                                sp_first = getattr(getattr(speaker, 'user', None), 'first_name', None)
                                sp_last = getattr(getattr(speaker, 'user', None), 'last_name', None)
                            except Exception:
                                sp_first = None
                                sp_last = None
                            socketio.emit('transcript_final', {  # type: ignore[arg-type]
                                'workshop_id': workshop_id,
                                'transcript_id': transcript.transcript_id,
                                'user_id': user_id,
                                'first_name': sp_first,
                                'last_name': sp_last,
                                'entry_type': 'human',
                                'task_id': None,
                                'text': evt.text,
                                'startTs': evt.start_time,
                                'endTs': evt.end_time,
                            }, to=f'workshop_room_{workshop_id}')  # type: ignore[arg-type]
                        else:
                            # Persist or update a single in-progress Dialogue row for this speaker.
                            sess_ref = _sessions.get(key)  # type: ignore[arg-type]
                            partial_id = None
                            if sess_ref:
                                partial_id = sess_ref.get('partial_dialogue_id')  # type: ignore[assignment]
                            dialogue_prev: Dialogue | None = None
                            if partial_id:
                                dialogue_prev = db.session.get(Dialogue, partial_id)  # type: ignore[arg-type]
                            if dialogue_prev is None:
                                dialogue_prev = Dialogue()  # type: ignore[call-arg]
                                dialogue_prev.workshop_id = workshop_id
                                dialogue_prev.speaker_id = user_id
                                dialogue_prev.transcript_id = None
                                dialogue_prev.dialogue_text = evt.text
                                dialogue_prev.is_final = False
                                db.session.add(dialogue_prev)
                                db.session.flush()  # assign id
                                if sess_ref is not None:
                                    sess_ref['partial_dialogue_id'] = dialogue_prev.dialogue_id
                            else:
                                dialogue_prev.dialogue_text = evt.text
                            db.session.commit()
                            if sess_ref is not None:
                                # Async provider de-dup
                                last_text = sess_ref.get('last_partial_text') if sess_ref else None
                                if last_text != evt.text:
                                    sess_ref['last_partial_text'] = evt.text
                                    sess_ref['partials'] = sess_ref.get('partials', 0) + 1
                                    socketio.emit('stt_partial', {  # type: ignore[arg-type]
                                        'workshop_id': workshop_id,
                                        'user_id': user_id,
                                        'text': evt.text,
                                        'startTs': evt.start_time,
                                    }, to=f'workshop_room_{workshop_id}')  # type: ignore[arg-type]
                    except Exception:  # pragma: no cover - defensive
                        log.exception('Result handling error')
            feeder_task = asyncio.create_task(_feeder())
            reader_task = asyncio.create_task(_reader())
            await reader_task
            feeder_task.cancel()
        except Exception as e:  # noqa
            log.exception('STT provider error')
            err_payload = _map_provider_exception(e)
            err_payload.update({'workshop_id': workshop_id})
            socketio.emit('stt_error', err_payload, to=f'workshop_room_{workshop_id}')  # type: ignore[arg-type]
        finally:
            try:
                await provider.close()
            except Exception:
                pass
            # Mark session as inactive before cleanup
            session_ref = _sessions.get(key)  # type: ignore[arg-type]
            if session_ref is not None:
                log.debug(f"Marking session ({workshop_id}, {user_id}) as inactive")
                session_ref['active'] = False
                session_ref['loop'] = None  # Clear loop reference
            stats = _sessions.pop(key, None)
            if stats is not None:  # only emit once
                _audit('transcription_stop', workshop_id=workshop_id, user_id=user_id,
                       partials=stats.get('partials', 0), finals=stats.get('finals', 0), reason='provider_closed')
                socketio.emit('stt_stopped', {
                    'workshop_id': workshop_id,
                    'user_id': user_id,
                    'partials': stats.get('partials', 0),
                    'finals': stats.get('finals', 0),
                }, to=f'workshop_room_{workshop_id}')  # type: ignore[arg-type]

    # Simplified: single background thread, single provider.open_stream, emit stt_ready once.
    app_obj = None
    try:
        app_obj = current_app._get_current_object()  # type: ignore[attr-defined]
    except Exception:
        pass

    log.debug(f"Creating new session ({workshop_id}, {user_id})")
    _sessions[key] = {
        'provider': provider,
        'provider_name': provider_name_emit,
        'model_path': model_path,
        'task': None,
        'seq': 0,
        'queue': audio_queue,
        'loop': None,
        'partials': 0,
        'finals': 0,
        'active': False,  # flips True after open
        'last_chunk_ts': time.time(),
    }

    def _thread_main():
        """Thread entry point with dual strategy for running the async transcription runner.

        Primary: use asyncio.run(_runner()).
        Fallback: if a RuntimeError indicates an existing running loop (seen in debug / reloader
        environment), manually create an event loop and drive _runner with run_until_complete.

        This avoids the hard failure currently preventing provider initialization.
        """
        ctx_mgr = app_obj.app_context() if app_obj is not None else None  # type: ignore
        try:
            if ctx_mgr:
                ctx_mgr.__enter__()
            try:
                asyncio.run(_runner())
            except RuntimeError as e:
                if 'asyncio.run() cannot be called' in str(e) or 'event loop while another loop is running' in str(e):
                    log.warning('Asyncio.run failed (%s); falling back to manual loop strategy', e)
                    loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(_runner())
                    finally:
                        try:
                            loop.close()
                        except Exception:
                            pass
                else:
                    log.exception('Provider failed to initialize or run (non-loop RuntimeError)')
                    socketio.emit('stt_error', { 'workshop_id': workshop_id, 'message': str(e), 'code': 'PROVIDER_INIT_FAILED' }, to=f'workshop_room_{workshop_id}')
            except Exception as e:
                log.exception('Provider failed to initialize or run (generic)')
                socketio.emit('stt_error', { 'workshop_id': workshop_id, 'message': str(e), 'code': 'PROVIDER_INIT_FAILED' }, to=f'workshop_room_{workshop_id}')
        finally:
            if ctx_mgr:
                try:
                    ctx_mgr.__exit__(None, None, None)
                except Exception:
                    pass

    bg_thread = threading.Thread(target=_thread_main, daemon=True)
    _sessions[key]['task'] = bg_thread
    log.debug(f"Starting background thread for session ({workshop_id}, {user_id})")
    bg_thread.start()


@socketio.on('stt_audio_chunk')
def stt_audio_chunk(data):
    workshop_id = int(data.get('workshop_id'))
    raw_uid = data.get('user_id')
    try:
        user_id = int(raw_uid) if raw_uid is not None else None
    except (TypeError, ValueError):
        user_id = None
    if user_id is None:
        emit('stt_error', { 'workshop_id': workshop_id, 'message': 'Invalid user id' })
        return
    key = (workshop_id, user_id)
    sess = _sessions.get(key)
    log.debug(f"Received stt_audio_chunk for session ({workshop_id}, {user_id}), session exists: {sess is not None}")
    if user_id is None or not sess:
        emit('stt_error', { 'workshop_id': workshop_id, 'message': 'No active session' })
        return
    seq = int(data.get('seq', 0))
    # Simple monotonic guard
    if seq < sess['seq']:
        return
    sess['seq'] = seq

    # SIMPLE VOSK PATH
    if 'recognizer' in sess:
        # decode audio first
        pcm = None
        if 'payloadBase64' in data and data.get('payloadBase64'):
            try:
                pcm = base64.b64decode(data.get('payloadBase64'))
            except Exception:
                emit('stt_error', { 'workshop_id': workshop_id, 'message': 'Invalid audio chunk (base64 decode failed)' })
                return
        elif 'payloadBytes' in data and data.get('payloadBytes') is not None:
            pcm = data.get('payloadBytes')
            if not isinstance(pcm, (bytes, bytearray)):
                emit('stt_error', { 'workshop_id': workshop_id, 'message': 'Invalid audio chunk (bytes type mismatch)' })
                return
        else:
            return
        recognizer = sess['recognizer']
        try:
            # Basic amplitude probe (first 100 samples) to verify non-silence audio reaching server
            try:
                import struct
                probe_samples = min(len(pcm)//2, 100)
                if probe_samples > 0:
                    smps = struct.unpack('<' + 'h'*probe_samples, pcm[:probe_samples*2])
                    max_abs = max(abs(s) for s in smps)
                else:
                    max_abs = -1
            except Exception:
                max_abs = -2
            accepted = recognizer.AcceptWaveform(pcm)  # type: ignore[attr-defined]
            log.debug(f"[SIMPLE VOSK] seq={seq} len={len(pcm)} accepted={accepted} max_abs={max_abs}")
            if accepted:
                import json as _json
                try:
                    result = _json.loads(recognizer.Result())  # type: ignore[attr-defined]
                    text = (result.get('text') or '').strip()
                except Exception:
                    text = ''
                if text:
                    # Persist final
                    transcript = Transcript()  # type: ignore[call-arg]
                    transcript.workshop_id = workshop_id
                    transcript.user_id = user_id
                    try:
                        transcript.entry_type = 'human'
                    except Exception:
                        pass
                    transcript.raw_stt_transcript = text
                    transcript.processed_transcript = text
                    transcript.language = sess.get('language') or 'en-US'
                    transcript.start_timestamp = None
                    transcript.end_timestamp = None
                    db.session.add(transcript)
                    db.session.flush()
                    dialogue_row = Dialogue()  # type: ignore[call-arg]
                    dialogue_row.workshop_id = workshop_id
                    dialogue_row.speaker_id = user_id
                    dialogue_row.transcript_id = transcript.transcript_id
                    dialogue_row.dialogue_text = text
                    dialogue_row.is_final = True
                    db.session.add(dialogue_row)
                    db.session.commit()
                    sess['finals'] = sess.get('finals', 0) + 1
                    # Include names for immediate rendering
                    try:
                        speaker = WorkshopParticipant.query.filter_by(workshop_id=workshop_id, user_id=user_id).first()
                        sp_first = getattr(getattr(speaker, 'user', None), 'first_name', None)
                        sp_last = getattr(getattr(speaker, 'user', None), 'last_name', None)
                    except Exception:
                        sp_first = None
                        sp_last = None
                    socketio.emit('transcript_final', {
                        'workshop_id': workshop_id,
                        'transcript_id': transcript.transcript_id,
                        'user_id': user_id,
                        'first_name': sp_first,
                        'last_name': sp_last,
                        'entry_type': 'human',
                        'task_id': None,
                        'text': text,
                        'startTs': None,
                        'endTs': None,
                    }, to=f'workshop_room_{workshop_id}')
            else:
                import json as _json
                try:
                    pres = _json.loads(recognizer.PartialResult())  # type: ignore[attr-defined]
                    ptext = (pres.get('partial') or '').strip()
                except Exception:
                    ptext = ''
                if ptext:
                    log.debug(f"[SIMPLE VOSK] partial='{ptext}'")
                if ptext:
                    # Suppress partial echoes during facilitator playback to reduce noise on UI
                    try:
                        from app.sockets.state import is_facilitator_playing
                        if is_facilitator_playing(int(workshop_id)):
                            sess['last_chunk_ts'] = time.time()
                            return
                    except Exception:
                        pass
                    # Server-side de-dup for simple Vosk path
                    last_text = sess.get('last_partial_text')
                    if last_text != ptext:
                        sess['last_partial_text'] = ptext
                        sess['partials'] = sess.get('partials', 0) + 1
                        socketio.emit('stt_partial', {
                            'workshop_id': workshop_id,
                            'user_id': user_id,
                            'text': ptext,
                            'startTs': None,
                        }, to=f'workshop_room_{workshop_id}')
            sess['last_chunk_ts'] = time.time()
        except Exception as e:  # noqa
            log.exception('[SIMPLE VOSK] processing error')
            emit('stt_error', { 'workshop_id': workshop_id, 'message': str(e) })
        return
    # Support either base64 or raw binary (ArrayBuffer) sent via Socket.IO
    pcm = None
    if 'payloadBase64' in data and data.get('payloadBase64'):
        try:
            pcm = base64.b64decode(data.get('payloadBase64'))
        except Exception:
            emit('stt_error', { 'workshop_id': workshop_id, 'message': 'Invalid audio chunk (base64 decode failed)' })
            return
    elif 'payloadBytes' in data and data.get('payloadBytes') is not None:
        # payloadBytes expected as binary buffer automatically converted by python-socketio to bytes
        pcm = data.get('payloadBytes')
        if not isinstance(pcm, (bytes, bytearray)):
            emit('stt_error', { 'workshop_id': workshop_id, 'message': 'Invalid audio chunk (bytes type mismatch)' })
            return
    else:
        # nothing to process
        return

    provider = sess['provider']
    queue: asyncio.Queue | None = sess.get('queue')  # type: ignore
    if not queue:
        emit('stt_error', { 'workshop_id': workshop_id, 'message': 'Session queue missing' })
        return
    try:
        first_flag_key = '_debug_first_chunks'
        debug_meta = sess.setdefault(first_flag_key, {'count': 0})
        debug_meta['count'] += 1
        count = debug_meta['count']
        size = len(pcm) if pcm else 0
        if count <= 5:  # limit verbose logging
            log.debug(f"Enqueue audio chunk seq={seq} size={size} bytes (workshop={workshop_id}, user={user_id}) active={sess.get('active')} loop_set={bool(sess.get('loop'))}")
        # Check if session is active before attempting queue operations
        if not sess.get('active', False) and count <= 3:
            log.debug(f"Session ({workshop_id}, {user_id}) not yet active; buffering chunk {seq}")
        loop = sess.get('loop')
        if isinstance(loop, asyncio.AbstractEventLoop) and not loop.is_closed():
            loop.call_soon_threadsafe(queue.put_nowait, pcm)
        else:  # fallback
            queue.put_nowait(pcm)
        sess['last_chunk_ts'] = time.time()
    except Exception as e:  # noqa
        log.exception('Queue put error')
        emit('stt_error', { 'workshop_id': workshop_id, 'message': str(e) })


@socketio.on('stt_stop')
def stt_stop(data):
    workshop_id = int(data.get('workshop_id'))
    raw_uid = data.get('user_id')
    try:
        user_id = int(raw_uid) if raw_uid is not None else None
    except (TypeError, ValueError):
        user_id = None
    if user_id is None:
        return
    key = (workshop_id, user_id)
    log.info(f'[DEBUG] stt_stop: workshop_id={workshop_id}, user_id={user_id}, data={data}')
    sess = _sessions.get(key)
    if not sess:
        log.info(f'[DEBUG] stt_stop: No session found for key {key}')
        return  # nothing to stop

    # SIMPLE VOSK PATH
    if 'recognizer' in sess:
        try:
            import json as _json
            try:
                fres = _json.loads(sess['recognizer'].FinalResult())  # type: ignore[attr-defined]
                ftext = (fres.get('text') or '').strip()
            except Exception:
                ftext = ''
            if ftext:
                transcript = Transcript()  # type: ignore[call-arg]
                transcript.workshop_id = workshop_id
                transcript.user_id = user_id
                try:
                    transcript.entry_type = 'human'
                except Exception:
                    pass
                transcript.raw_stt_transcript = ftext
                transcript.processed_transcript = ftext
                transcript.language = sess.get('language') or 'en-US'
                transcript.start_timestamp = None
                transcript.end_timestamp = None
                db.session.add(transcript)
                db.session.flush()
                dialogue_row = Dialogue()  # type: ignore[call-arg]
                dialogue_row.workshop_id = workshop_id
                dialogue_row.speaker_id = user_id
                dialogue_row.transcript_id = transcript.transcript_id
                dialogue_row.dialogue_text = ftext
                dialogue_row.is_final = True
                db.session.add(dialogue_row)
                db.session.commit()
                sess['finals'] = sess.get('finals', 0) + 1
                try:
                    speaker = WorkshopParticipant.query.filter_by(workshop_id=workshop_id, user_id=user_id).first()
                    sp_first = getattr(getattr(speaker, 'user', None), 'first_name', None)
                    sp_last = getattr(getattr(speaker, 'user', None), 'last_name', None)
                except Exception:
                    sp_first = None
                    sp_last = None
                socketio.emit('transcript_final', {
                    'workshop_id': workshop_id,
                    'transcript_id': transcript.transcript_id,
                    'user_id': user_id,
                    'first_name': sp_first,
                    'last_name': sp_last,
                    'text': ftext,
                    'startTs': None,
                    'endTs': None,
                }, to=f'workshop_room_{workshop_id}')
        except Exception:
            log.exception('[SIMPLE VOSK] finalization error')
        stats = _sessions.pop(key, None) or sess
        _audit('transcription_stop', workshop_id=workshop_id, user_id=user_id,
               partials=stats.get('partials', 0), finals=stats.get('finals', 0), reason='client_stop')
        emit('stt_stop_ack', { 'workshop_id': workshop_id, 'user_id': user_id })
        socketio.emit('stt_stopped', {
            'workshop_id': workshop_id,
            'user_id': user_id,
            'partials': stats.get('partials', 0),
            'finals': stats.get('finals', 0),
        }, to=f'workshop_room_{workshop_id}')
        return
    log.info(f'[DEBUG] stt_stop: Found session {sess.keys()}, sending stop signal')
    task = sess.get('task')
    queue: asyncio.Queue | None = sess.get('queue')  # type: ignore
    if queue:
        try:
            loop = sess.get('loop')
            if isinstance(loop, asyncio.AbstractEventLoop):
                log.info(f'[DEBUG] stt_stop: Sending sentinel via loop.call_soon_threadsafe')
                loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel to terminate feeder
            else:
                log.info(f'[DEBUG] stt_stop: Sending sentinel directly to queue')
                queue.put_nowait(None)  # type: ignore
        except Exception:  # pragma: no cover
            log.exception('Failed to signal feeder termination')
    # Do not emit stt_stopped here; let async runner close and emit with metrics
    # Optionally we could emit a lightweight acknowledgement
    # Mark session as stopping to prevent duplicate stop logic
    sess['stopping'] = True
    log.info(f'[DEBUG] stt_stop: Marked session as stopping, sending stt_stop_ack')
    emit('stt_stop_ack', { 'workshop_id': workshop_id, 'user_id': user_id })
    # Launch a short watchdog to force-close lingering provider if not finished in grace window
    def _watchdog():
        import time
        timeout = 1.5  # seconds grace
        start = time.time()
        while time.time() - start < timeout:
            if task and (
                (hasattr(task, 'done') and getattr(task, 'done')()) or
                (hasattr(task, 'is_alive') and not getattr(task, 'is_alive')())
            ):
                return
            time.sleep(0.05)
        # If still present, force pop & emit minimal stopped (metrics may be partial)
        stats = _sessions.pop(key, None)
        if stats is not None:
            _audit('transcription_stop', workshop_id=workshop_id, user_id=user_id,
                   partials=stats.get('partials', 0), finals=stats.get('finals', 0), reason='watchdog_force')
            socketio.emit('stt_stopped', {
                'workshop_id': workshop_id,
                'user_id': user_id,
                'partials': stats.get('partials', 0),
                'finals': stats.get('finals', 0),
                'forced': True,
            }, to=f'workshop_room_{workshop_id}')
    socketio.start_background_task(_watchdog)
