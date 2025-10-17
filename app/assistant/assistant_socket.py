from __future__ import annotations

from typing import Any, Dict, Optional

from flask import current_app, request
from flask_socketio import Namespace, emit, join_room

from botocore.exceptions import ClientError

from app.assistant.assistant_controller import (
    controller,
    _phase_snapshot,
    _rbac_payload,
    _sidebar_actions,
    _sidebar_threads,
    _timebox_payload,
)
from app.assistant.schemas import AssistantQuery, AssistantReply
from app.extensions import db, socketio


class AssistantNamespace(Namespace):
    def on_connect(self) -> None:  # pragma: no cover - network layer
        pass

    def on_join(self, data):  # pragma: no cover - network layer
        workshop_id = _safe_int(data.get("workshop_id"))
        user_id = _safe_int(data.get("user_id"))
        room = f"workshop_{workshop_id}" if workshop_id is not None else None
        if room:
            join_room(room)
        emit("assistant:ready", {"ok": True})
        try:
            if workshop_id is not None:
                target_sid = getattr(request, "sid", None)
                emit_assistant_state(
                    workshop_id,
                    user_id=user_id,
                    room=target_sid,
                    include_sidebar=True,
                    include_phase_snapshot=True,
                )
        except Exception:
            current_app.logger.exception("assistant_join_state_failed")

    def on_ask(self, data):  # pragma: no cover - network layer
        try:
            payload = AssistantQuery.model_validate(data)
        except Exception as exc:
            emit("assistant:error", {"error": str(exc)})
            return

        try:
            context = controller.context_fabric.build(payload.workshop_id, payload.user_id)
            persona_cfg = controller.persona_router.select(payload, context)
            thread = controller.ensure_thread(payload)
            timebox = _timebox_payload(context)
            ack_payload = {
                "thread_id": thread.id,
                "persona": persona_cfg.name.value,
                "persona_label": persona_cfg.description,
                "phase": context.workshop.current_phase or "—",
                "phase_label": context.workshop.current_phase or "—",
                "timer": timebox["formatted"],
                "timer_seconds": timebox["remaining_seconds"],
                "timer_total_seconds": timebox["total_seconds"],
                "timebox_active": timebox["active"],
                "timer_paused": timebox["paused"],
                "rbac": _rbac_payload(context.rbac),
                "workshop_title": context.workshop.title,
                "workshop_status": timebox["workshop_status"],
                "phase_snapshot": _phase_snapshot(context),
                "sidebar": {
                    "actions": _sidebar_actions(context),
                    "threads": _sidebar_threads(thread, []),
                },
            }
            # Lightweight memory surface for ACK badge; safe best-effort and non-blocking.
            try:
                memory_info = controller.memory.retrieve(
                    query=payload.text,
                    workshop_id=payload.workshop_id,
                    user_id=payload.user_id,
                    thread_id=thread.id,
                )
                ack_payload["memory"] = memory_info.as_meta()
            except Exception:
                # Do not fail ACK if memory is disabled/unavailable
                pass
            emit("assistant:ack", ack_payload)
            rate_limited = False
            try:
                reply, meta = controller.handle_query(
                    payload,
                    context=context,
                    persona=persona_cfg,
                    thread_id=thread.id,
                )
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code") if hasattr(exc, "response") else None
                if error_code == "ServiceUnavailableException":
                    current_app.logger.warning("assistant_rate_limited", exc_info=True)
                    rate_limited = True
                    fallback_text = (
                        "I'm encountering heavy traffic right now, so I couldn't complete that request. "
                        "Please wait a few seconds and try again."
                    )
                    reply = AssistantReply(
                        persona=persona_cfg.name,
                        text=fallback_text,
                        ui_hints={
                            "chips": ["Try again in a few seconds"],
                            "followups": ["Please repeat the question once the assistant is available."],
                        },
                        proposed_actions=[],
                    )
                    meta = {
                        "persona": persona_cfg.name.value,
                        "persona_label": persona_cfg.description,
                        "tool_results": [],
                        "plan": None,
                        "error": {
                            "code": "rate_limited",
                            "message": "Upstream LLM returned ServiceUnavailableException",
                        },
                    }
                else:
                    raise
            for result in meta.get("tool_results", []):
                emit("assistant:tool_result", result)
            if not rate_limited:
                for chunk in _stream_text(reply.text):
                    emit("assistant:token", {"delta": chunk})
                    socketio.sleep(0)
            assistant_turn = controller.persist_turns(payload, reply, meta, thread.id)
            meta.update(
                {
                    "phase_snapshot": _phase_snapshot(context),
                    "sidebar": {
                        "actions": _sidebar_actions(context),
                        "threads": _sidebar_threads(thread, []),
                    },
                    "timer": timebox["formatted"],
                    "timer_seconds": timebox["remaining_seconds"],
                    "timer_total_seconds": timebox["total_seconds"],
                    "timebox_active": timebox["active"],
                    "timer_paused": timebox["paused"],
                    "workshop_status": timebox["workshop_status"],
                }
            )
            db.session.commit()
            controller.record_memory_event(payload, reply, thread.id, meta)
            emit(
                "assistant:reply",
                {
                    "thread_id": thread.id,
                    "reply": reply.model_dump(),
                    "meta": meta,
                    "turn_id": assistant_turn.id,
                },
            )
        except Exception as exc:
            current_app.logger.exception("assistant_socket_error")
            db.session.rollback()
            emit("assistant:error", {"error": str(exc)})


def register_namespace(socketio):  # pragma: no cover - convenience hook
    socketio.on_namespace(AssistantNamespace("/assistant"))


def _stream_text(text: str | None, *, chunk_size: int = 180) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    buffer = []
    count = 0
    for char in text:
        buffer.append(char)
        count += 1
        if count >= chunk_size and char in {" ", "\n", "\t", ".", ","}:
            chunks.append("".join(buffer))
            buffer = []
            count = 0
    if buffer:
        chunks.append("".join(buffer))
    if not chunks:
        return [text]
    return chunks


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            return int(stripped)
        return int(value)
    except (TypeError, ValueError):
        return None


def emit_assistant_state(
    workshop_id: int,
    *,
    user_id: Optional[int] = None,
    room: Optional[str] = None,
    include_sidebar: bool = False,
    include_phase_snapshot: bool = False,
) -> None:
    try:
        context = controller.context_fabric.build(workshop_id, user_id)
    except Exception:
        current_app.logger.exception(
            "assistant_state_context_failed",
            extra={"workshop_id": workshop_id, "user_id": user_id},
        )
        return

    timebox = _timebox_payload(context)
    payload: Dict[str, Any] = {
        "workshop_id": workshop_id,
        "workshop_title": context.workshop.title,
        "phase": context.workshop.current_phase,
        "phase_label": context.workshop.current_phase,
        "timer": timebox["formatted"],
        "timer_seconds": timebox["remaining_seconds"],
        "timer_total_seconds": timebox["total_seconds"],
        "timebox_active": timebox["active"],
        "timer_paused": timebox["paused"],
        "workshop_status": timebox["workshop_status"],
    }

    if user_id is not None:
        payload["rbac"] = _rbac_payload(context.rbac)

    if include_phase_snapshot:
        payload["phase_snapshot"] = _phase_snapshot(context)

    if include_sidebar:
        payload["sidebar"] = {
            "actions": _sidebar_actions(context),
        }

    target_room = room or f"workshop_{workshop_id}"
    try:
        socketio.emit(
            "assistant:state",
            payload,
            namespace="/assistant",
            to=target_room,
        )
    except Exception:
        current_app.logger.exception(
            "assistant_state_emit_failed",
            extra={"workshop_id": workshop_id, "room": target_room},
        )
