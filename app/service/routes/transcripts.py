from __future__ import annotations

import json
import time
from typing import List

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import select

from app.extensions import db, socketio
from app.models import Transcript, Workshop, LLMUsageLog, ActivityLog, WorkshopParticipant
from app.utils.llm_bedrock import get_bedrock_runtime_client
import re
from app.utils.telemetry import log_event


transcripts_bp = Blueprint("transcripts_bp", __name__)


def _build_context_snippet(transcripts: List[Transcript], limit_chars: int = 12000) -> str:
    buf: List[str] = []
    total = 0
    # Most recent first for selection; reversed when joining
    rows = sorted(transcripts, key=lambda x: x.start_timestamp or x.created_timestamp, reverse=True)
    for t in rows:
        speaker = getattr(getattr(t, 'user', None), 'display_name', None) or str(getattr(t, 'user_id', ''))
        raw = (t.raw_stt_transcript or '').strip()
        if not raw:
            continue
        line = f"{speaker}: {raw}\n"
        if total + len(line) > limit_chars:
            break
        buf.append(line)
        total += len(line)
    return "Recent meeting context (most recent first):\n" + ''.join(reversed(buf))


def _invoke_nova(prompt: str, model_id: str) -> str:
    br = get_bedrock_runtime_client()
    body = {
        "inputText": prompt,
        "textGenerationConfig": {
            "temperature": 0.2,
            "maxTokenCount": 800,
            "topP": 0.9,
        },
    }
    resp = br.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body).encode("utf-8"),
    )
    out = json.loads(resp["body"].read().decode("utf-8"))
    return out.get("results", [{}])[0].get("outputText", "").strip()


def _fallback_polish(text: str) -> str:
    """A lightweight, deterministic cleanup used when Bedrock isn't available.

    Heuristics (English-leaning but safe):
    - Trim whitespace
    - Ensure leading capitalization of sentences
    - Ensure final punctuation (., ?, !) exists; default to period
    - Normalize common contractions: im/ive/dont/cant/wont -> I'm/I've/don't/can't/won't
    - Capitalize standalone pronoun 'i' -> 'I'
    """
    if not text:
        return text

    s = text.strip()

    # Common contractions (case-insensitive)
    repl = {
        r"\bim\b": "I'm",
        r"\bive\b": "I've",
        r"\bdont\b": "don't",
        r"\bcant\b": "can't",
        r"\bwont\b": "won't",
    }
    for pat, val in repl.items():
        s = re.sub(pat, val, s, flags=re.IGNORECASE)

    # Capitalize standalone 'i' pronoun when surrounded by word boundaries
    s = re.sub(r"\bi\b", "I", s)

    # Capitalize first character of each sentence-ish chunk
    def cap_sentence(m: re.Match) -> str:
        return m.group(1) + m.group(2).upper()

    s = re.sub(r"(^|[\.!?]\s+)([a-z])", cap_sentence, s)

    # Ensure closing punctuation
    if not re.search(r"[\.!?]$", s):
        s += "."

    return s


@login_required
@transcripts_bp.post("/api/workshops/<int:workshop_id>/transcripts/<int:transcript_id>/polish")
def polish_transcript(workshop_id: int, transcript_id: int):
    # Basic authZ: organizer can polish any; speaker can polish own
    t = db.session.get(Transcript, transcript_id)
    if not t or t.workshop_id != workshop_id:
        return jsonify({"error": "Transcript not found"}), 404

    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return jsonify({"error": "Workshop not found"}), 404

    is_organizer = (ws.created_by_id == current_user.user_id)
    is_speaker = (t.user_id == current_user.user_id)
    if not (is_organizer or is_speaker):
        return jsonify({"error": "Forbidden"}), 403

    original = (t.raw_stt_transcript or t.processed_transcript or "").strip()
    if not original:
        return jsonify({"error": "No transcript to process"}), 400

    # Context window for better corrections
    recent = db.session.execute(
        select(Transcript).where(Transcript.workshop_id == workshop_id).limit(400)
    ).scalars().all()
    context = _build_context_snippet(list(recent))

    from app.config import Config
    model_id = Config.BEDROCK_MODEL_ID or "amazon.nova-lite-v1:0"

    turn1_prompt = f"""
You are cleaning up a live-meeting transcript line while preserving speaker intent and tone.

{context}

Task:

Utterance (between triple backticks):
```
{original}
```
"""

    start_ms = time.time()
    fallback_used = False
    turn1 = ""
    try:
        turn1 = _invoke_nova(turn1_prompt, model_id)

        turn2_prompt = f"""
You will compare an original utterance and its corrected version.
Goal: Keep grammar/punctuation correct while preserving the speaker's original word choice, idioms, and rhythm as much as possible.
If a word was changed unnecessarily, revert it. Keep fillers only if they change meaning.

Original (between triple backticks):
```
{original}
```

Corrected (between triple backticks):
```
{turn1}
```

Return ONLY the final corrected utterance.
"""
        final_text = _invoke_nova(turn2_prompt, model_id)
    except Exception as e:  # pragma: no cover
        # Log error and gracefully fall back to simple deterministic polish
        log_event('llm_error', {
            'workshop_id': workshop_id,
            'transcript_id': transcript_id,
            'service_used': 'bedrock',
            'model_used': model_id,
            'message': str(e),
        })
        final_text = _fallback_polish(original)
        fallback_used = True
    latency_ms = int((time.time() - start_ms) * 1000)

    # Persist result to transcript
    t.processed_transcript = final_text
    db.session.add(t)

    # Append usage log
    log_row = LLMUsageLog()
    log_row.workshop_id = workshop_id
    log_row.transcript_id = transcript_id
    log_row.service_used = 'fallback' if fallback_used else 'bedrock'
    log_row.model_used = None if fallback_used else model_id
    log_row.prompt_input_size = len(turn1_prompt) + len(original) + len(context)
    log_row.response_size = len(turn1) + len(final_text)
    log_row.token_usage = None
    log_row.latency_ms = latency_ms
    db.session.add(log_row)
    db.session.commit()

    # Realtime broadcast
    socketio.emit(
        'transcript_corrected',
        {
            'transcript_id': transcript_id,
            'workshop_id': workshop_id,
            'processed_text': final_text,
        },
        to=f"workshop_room_{workshop_id}",
    )

    log_event('llm_polish_transcript', {
        'log_id': log_row.id,
        'workshop_id': workshop_id,
        'transcript_id': transcript_id,
        'model': model_id,
        'latency_ms': latency_ms,
    })

    return jsonify({"processed_text": final_text, "log_id": log_row.id})


@login_required
@transcripts_bp.post("/api/workshops/<int:workshop_id>/transcripts/<int:transcript_id>/feedback")
def polish_feedback(workshop_id: int, transcript_id: int):
    payload = request.get_json(silent=True) or {}
    vote = payload.get('feedback_vote')
    comment = payload.get('feedback_comment')
    # Last log row for this transcript
    row = (
        db.session.query(LLMUsageLog)
        .filter(LLMUsageLog.transcript_id == transcript_id)
        .order_by(LLMUsageLog.created_timestamp.desc())
        .first()
    )
    if not row:
        return jsonify({"error": "No usage row found"}), 404
    row.feedback_vote = int(vote) if vote is not None else None
    row.feedback_comment = (comment or '').strip() or None
    db.session.commit()
    return jsonify({"success": True})


@login_required
@transcripts_bp.delete("/api/workshops/<int:workshop_id>/transcripts/<int:transcript_id>")
def delete_transcript(workshop_id: int, transcript_id: int):
    transcript = db.session.get(Transcript, transcript_id)
    if not transcript or transcript.workshop_id != workshop_id:
        return jsonify({"error": "Transcript not found"}), 404

    workshop = db.session.get(Workshop, workshop_id)
    if not workshop:
        return jsonify({"error": "Workshop not found"}), 404

    is_organizer = (workshop.created_by_id == current_user.user_id)
    participant_can_delete = getattr(workshop, 'participant_can_delete_transcripts', True)
    is_speaker = (transcript.user_id == current_user.user_id)
    is_speaker_allowed = participant_can_delete and is_speaker
    if not (is_organizer or is_speaker_allowed):
        return jsonify({"error": "Forbidden"}), 403

    # Detach related dialogue rows and usage logs to satisfy FK constraints
    for dlg in list(getattr(transcript, 'dialogue_rows', []) or []):
        dlg.transcript_id = None
    usage_logs = (
        db.session.query(LLMUsageLog)
        .filter(LLMUsageLog.transcript_id == transcript_id)
        .all()
    )
    for log in usage_logs:
        log.transcript_id = None

    participant = None
    if transcript.user_id:
        participant = (
            db.session.query(WorkshopParticipant)
            .filter_by(workshop_id=workshop_id, user_id=transcript.user_id)
            .first()
        )

    activity = ActivityLog()
    activity.participant_id = participant.id if participant else None
    activity.task_id = transcript.task_id
    activity.action = 'transcript_deleted'
    db.session.add(activity)

    db.session.delete(transcript)
    db.session.commit()

    socketio.emit(
        'transcript_deleted',
        {
            'transcript_id': transcript_id,
            'workshop_id': workshop_id,
        },
        to=f"workshop_room_{workshop_id}",
    )

    log_event('transcript_deleted', {
        'workshop_id': workshop_id,
        'transcript_id': transcript_id,
        'actor_user_id': current_user.user_id,
        'deleted_by': 'organizer' if is_organizer else 'speaker',
        'participant_delete_enabled': participant_can_delete,
    })

    return jsonify({"success": True})
