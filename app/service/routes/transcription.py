from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import asdict, is_dataclass
from typing import Any, AsyncIterator, Dict, Optional, cast

from flask import Blueprint
from flask_socketio import emit, join_room

from app import socketio
from app.transcription.dao import TranscriptContext, TranscriptWriter
from app.transcription.factory import create_provider
from app.transcription.provider import ProviderConfig, ProviderEvent, TranscriptionProvider


bp = Blueprint("rt", __name__)


class SessionState:
    __slots__ = (
        "provider",
        "queue",
        "loop",
        "thread",
        "active",
        "context",
        "partial_dialogue_id",
        "last_partial_text",
    )

    def __init__(self, provider: TranscriptionProvider) -> None:
        self.provider = provider
        self.queue: Optional[asyncio.Queue[Optional[bytes]]] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.active = False
        self.context: Optional[TranscriptContext] = None
        self.partial_dialogue_id: Optional[int] = None
        self.last_partial_text: Optional[str] = None


_sessions: Dict[str, SessionState] = {}
_writer = TranscriptWriter()


def _event_payload(event: ProviderEvent) -> Dict[str, Any]:
    if is_dataclass(event):
        payload: Dict[str, Any] = asdict(event)
    elif isinstance(event, dict):
        payload = dict(event)
    else:
        payload = {"text": str(event)}
    payload.setdefault("is_final", getattr(event, "is_final", False))
    payload.setdefault("start_time", getattr(event, "start_time", None))
    payload.setdefault("end_time", getattr(event, "end_time", None))
    return payload


def _start_runner(session_id: str, state: SessionState, config: ProviderConfig) -> None:
    provider = state.provider

    async def _runner() -> None:
        queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        state.queue = queue
        state.loop = asyncio.get_running_loop()
        state.active = True

        await provider.open_stream(session_id, config)

        async def _audio_writer() -> None:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                await provider.write(chunk)

        async def _reader() -> None:
            results_iter = cast(AsyncIterator[ProviderEvent], provider.aresults())
            async for event in results_iter:
                payload = _event_payload(event)
                payload["session_id"] = session_id
                socketio.emit("transcript_update", payload, to=session_id)
                ctx = state.context
                text = payload.get("text")
                if ctx and text:
                    is_final = bool(payload.get("is_final"))
                    start_time = payload.get("start_time")
                    end_time = payload.get("end_time")
                    if is_final:
                        transcript_id = _writer.record_final(
                            ctx,
                            text,
                            start_time if isinstance(start_time, (int, float)) else None,
                            end_time if isinstance(end_time, (int, float)) else None,
                            state.partial_dialogue_id,
                        )
                        state.partial_dialogue_id = None
                        state.last_partial_text = None
                        first_name, last_name = _writer.resolve_speaker_name(ctx)
                        final_payload = {
                            "workshop_id": ctx.workshop_id,
                            "transcript_id": transcript_id,
                            "user_id": ctx.user_id,
                            "first_name": first_name,
                            "last_name": last_name,
                            "entry_type": "human",
                            "task_id": ctx.task_id,
                            "text": text,
                            "startTs": start_time,
                            "endTs": end_time,
                        }
                        socketio.emit(
                            "transcript_final",
                            final_payload,
                            to=f"workshop_room_{ctx.workshop_id}",
                        )
                    else:
                        if text != state.last_partial_text:
                            state.partial_dialogue_id = _writer.record_partial(
                                ctx,
                                text,
                                state.partial_dialogue_id,
                            )
                            state.last_partial_text = text
                            socketio.emit(
                                "stt_partial",
                                {
                                    "workshop_id": ctx.workshop_id,
                                    "user_id": ctx.user_id,
                                    "text": text,
                                    "startTs": start_time,
                                },
                                to=f"workshop_room_{ctx.workshop_id}",
                            )

        writer_task = asyncio.create_task(_audio_writer())
        try:
            await _reader()
        finally:
            writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer_task
            state.active = False
            try:
                await provider.close()
            except Exception:
                pass

    loop: asyncio.AbstractEventLoop | None = None
    try:
        loop = asyncio.new_event_loop()
        state.loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_runner())
    finally:
        if loop is not None:
            try:
                loop.close()
            finally:
                state.loop = None
                state.queue = None


@socketio.on("start_session")
def start_session(data: Dict[str, Any]) -> None:
    session_id_raw = data.get("session_id")
    session_id = str(session_id_raw) if session_id_raw is not None else ""
    if not session_id:
        emit("transcript_error", {"message": "session_id is required"})
        return
    join_room(session_id)

    try:
        provider, _ = create_provider(data.get("provider"))
    except Exception as exc:  # pragma: no cover - defensive
        emit("transcript_error", {"session_id": session_id, "message": str(exc)})
        return

    config = ProviderConfig(
        language_code=str(data.get("language_code", "en-US")),
        sample_rate_hz=int(data.get("sample_rate_hz", 16000)),
        vocab_name=data.get("vocab_name"),
    )

    state = SessionState(provider)
    _sessions[session_id] = state

    thread = threading.Thread(target=_start_runner, args=(session_id, state, config), daemon=True)
    state.thread = thread
    thread.start()
    emit("transcript_ready", {"session_id": session_id})


@socketio.on("audio_chunk")
def audio_chunk(data: Dict[str, Any]) -> None:
    session_id_raw = data.get("session_id")
    session_id = str(session_id_raw) if session_id_raw is not None else ""
    chunk = data.get("pcm16")
    if not session_id or not isinstance(chunk, (bytes, bytearray)):
        emit("transcript_error", {"message": "Invalid audio payload"})
        return

    state = _sessions.get(session_id)
    if state is None or state.queue is None or state.loop is None:
        emit("transcript_error", {"session_id": session_id, "message": "Session inactive"})
        return

    def _enqueue() -> None:
        if state.queue is not None:
            state.queue.put_nowait(bytes(chunk))

    state.loop.call_soon_threadsafe(_enqueue)


@socketio.on("stop_session")
def stop_session(data: Dict[str, Any]) -> None:
    session_id_raw = data.get("session_id")
    session_id = str(session_id_raw) if session_id_raw is not None else ""
    state = _sessions.pop(session_id, None)
    if state is None:
        return

    if state.loop and state.queue:
        def _stop_queue() -> None:
            if state.queue is not None:
                state.queue.put_nowait(None)

        state.loop.call_soon_threadsafe(_stop_queue)

    if state.thread and state.thread.is_alive():
        state.thread.join(timeout=2.0)

    emit("transcript_stopped", {"session_id": session_id})


