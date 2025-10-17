# app/service/routes/speech.py
"""Speech task payload generator.
Spotlights a selected speaker and optionally toggles captions/transcription prominence.
"""
from __future__ import annotations

import json
from flask import current_app

from app.extensions import db
from app.models import BrainstormTask, Workshop, WorkshopPlanItem, User, WorkshopParticipant, WorkshopDocument, Document
from app.tasks.registry import TASK_REGISTRY
from app.utils.value_parsing import bounded_int, safe_int


def _get_plan_item_config(workshop_id: int, ttype: str) -> dict | None:
    """Return config for NEXT matching plan item, prefer config_json over description."""
    ws = db.session.get(Workshop, workshop_id)
    current_idx = ws.current_task_index if ws and isinstance(ws.current_task_index, int) else -1
    try:
        q = (
            WorkshopPlanItem.query
            .filter_by(workshop_id=workshop_id, task_type=ttype, enabled=True)
            .order_by(WorkshopPlanItem.order_index.asc())
        )
        for item in q.all():
            try:
                if item.order_index is not None:
                    order_idx = safe_int(item.order_index, default=current_idx + 1)
                    if order_idx <= current_idx:
                        continue
            except Exception:
                pass
            if getattr(item, 'config_json', None):
                try:
                    data = json.loads(item.config_json) if not isinstance(item.config_json, dict) else item.config_json
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
            if getattr(item, 'description', None):
                try:
                    data = json.loads(item.description)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
        return None
    except Exception:
        return None


def _extract_text_from_document(doc: Document) -> str | None:
    """Best-effort plain text extraction from a Document row.
    If file is a PDF or text, try to read content. Fallback to description.
    Note: heavy parsing libraries are intentionally not imported here; prefer simple read for .txt/.md.
    """
    try:
        # Prefer a text cache if your model supports it
        content = getattr(doc, 'text_content', None)
        if isinstance(content, str) and content.strip():
            return content
    except Exception:
        pass
    # Fallback: read file if path available and extension is safe to read as text
    try:
        path = getattr(doc, 'file_path', None) or getattr(doc, 'path', None)
        name = getattr(doc, 'file_name', '') or ''
        fname = (path or name or '').lower()
        if path:
            import os
            # Build absolute path: file_path is stored relative to instance/uploads
            abs_path = path if os.path.isabs(path) else os.path.join(current_app.instance_path, path)
        else:
            abs_path = None
        if fname.endswith(('.txt', '.md', '.markdown')) and abs_path:
            import os
            if os.path.exists(abs_path):
                with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                    return f.read()
        # Basic PDF text extraction if pypdf is available
        if fname.endswith('.pdf') and abs_path:
            try:
                import importlib
                import importlib.util as importlib_util
                import os
                if os.path.exists(abs_path):
                    # Try pdfminer.six first for better text extraction
                    try:
                        pdfminer_spec = importlib_util.find_spec('pdfminer.high_level') or importlib_util.find_spec('pdfminer')
                        if pdfminer_spec is not None:
                            pdfminer_hl = importlib.import_module('pdfminer.high_level')
                            extract_text = getattr(pdfminer_hl, 'extract_text', None)
                            if callable(extract_text):
                                raw_text = extract_text(abs_path)
                                text = (str(raw_text) if raw_text is not None else '').strip()
                                if text:
                                    return text[:15000]
                    except Exception:
                        pass
                    pypdf_spec = importlib_util.find_spec('pypdf')
                    if pypdf_spec is not None:
                        pypdf = importlib.import_module('pypdf')
                        PdfReader = getattr(pypdf, 'PdfReader', None)
                        if PdfReader is not None:
                            text_parts = []
                            reader = PdfReader(abs_path)
                            for page in getattr(reader, 'pages', []) or []:
                                try:
                                    t = page.extract_text() or ''
                                    if t:
                                        text_parts.append(t)
                                except Exception:
                                    continue
                            text = '\n\n'.join(text_parts).strip()
                            if text:
                                return text[:15000]
            except Exception:
                # Optional dependency; ignore errors and fall back below
                pass
    except Exception:
        pass
    # Last resort: use description or title
    try:
        return getattr(doc, 'description', None) or getattr(doc, 'title', None)
    except Exception:
        return None


def get_speech_payload(workshop_id: int, phase_context: str | None = None):
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return "Workshop not found", 404

    cfg = _get_plan_item_config(workshop_id, "speech") or {}
    delivery_mode_raw = (cfg.get('delivery_mode') or 'direct').strip().lower()
    valid_modes = {'direct', 'reader', 'framing'}
    delivery_mode = delivery_mode_raw if delivery_mode_raw in valid_modes else 'direct'
    speaker_user_id = cfg.get("speaker_user_id")
    cc_enabled = bool(cfg.get("cc_enabled", True))

    title = "Prepared Speech"
    task_description = "A designated speaker will share remarks while everyone else listens and captures notes."
    instructions = "The speaker has the floor. Others, please mute your microphones; use chat for clarifying questions."
    # Allow per-item override via config.duration_sec, else default from registry
    default_duration = safe_int(TASK_REGISTRY.get("speech", {}).get("default_duration", 600), default=600)
    duration = default_duration
    override = cfg.get('duration_sec')
    if override is not None:
        duration = bounded_int(override, default=default_duration, minimum=30, maximum=7200)

    # Try to add a friendly speaker display name if available
    speaker_name = None
    try:
        if speaker_user_id:
            speaker_pk = safe_int(speaker_user_id, default=0)
            if speaker_pk > 0:
                u = db.session.get(User, speaker_pk)
            else:
                u = None
            if u:
                parts = [p for p in [getattr(u, 'first_name', None), getattr(u, 'last_name', None)] if p]
                speaker_name = ' '.join(parts) if parts else (getattr(u, 'username', None) or (u.email.split('@')[0] if getattr(u, 'email', None) else f"User {u.user_id}"))
    except Exception:
        speaker_name = None

    # Enrich payload with participants (id + display_name) for UI labeling
    participants: list[dict] = []
    try:
        plist = WorkshopParticipant.query.filter_by(workshop_id=workshop_id).all()
        for p in plist:
            try:
                u = getattr(p, 'user', None)
                if u:
                    dn_parts = [getattr(u, 'first_name', None) or '', getattr(u, 'last_name', None) or '']
                    dn = (' '.join([s for s in dn_parts if s]).strip()) or (getattr(u, 'email', None) or f"User {p.user_id}")
                else:
                    dn = f"User {getattr(p, 'user_id', '')}"
                participants.append({ 'user_id': getattr(p, 'user_id', None), 'display_name': dn })
            except Exception:
                continue
    except Exception:
        participants = []

    # Resolve tts_script based on delivery mode
    tts_script: str | None = None
    tts_read_time_seconds = 40
    narration = (
        "We’re moving into a short speech. The speaker will share their perspective and key points. "
        "Please keep microphones muted and feel free to note questions in chat. We’ll take a moment afterwards "
        "to address any clarifications and key takeaways."
    )
    framing_points: list[str] | None = None

    if delivery_mode == 'reader':
        # Priority: explicit script_text, else linked document text
        script_text = cfg.get('script_text')
        if isinstance(script_text, str) and script_text.strip():
            tts_script = script_text.strip()
            # Rough estimate: 130 wpm -> seconds
            try:
                w = max(1, len(tts_script.split()))
                tts_read_time_seconds = safe_int(w / 2.2, default=40)
            except Exception:
                tts_read_time_seconds = 60
        else:
            doc_raw = cfg.get('document_id')
            doc_pk = safe_int(doc_raw, default=0)
            if doc_pk > 0:
                link = WorkshopDocument.query.filter_by(workshop_id=workshop_id, document_id=doc_pk).first()
                if link:
                    try:
                        doc = link.document or Document.query.get(doc_pk)
                    except Exception:
                        doc = None
                    if doc:
                        text = _extract_text_from_document(doc)
                        if text and text.strip():
                            tts_script = text.strip()[:15000]
                            try:
                                w = max(1, len(tts_script.split()))
                                tts_read_time_seconds = safe_int(w / 2.2, default=tts_read_time_seconds)
                            except Exception:
                                tts_read_time_seconds = 120
    elif delivery_mode == 'framing':
        raw_points = cfg.get('key_points')
        if isinstance(raw_points, list):
            items = raw_points
        elif raw_points is None:
            items = []
        else:
            items = [raw_points]
        framing_points = [str(item).strip() for item in items if str(item).strip()]
        session_context = cfg.get('framing_context') or phase_context or getattr(ws, 'title', '') or 'this session'
        objective = getattr(ws, 'objective', '') or ''
        lines: list[str] = []
        lines.append(f"Let's set the frame for {session_context}.")
        if objective:
            lines.append(f"Our focus is {objective}.")
        if framing_points:
            lines.append("Key points to hold in mind:")
            for idx, point in enumerate(framing_points, start=1):
                lines.append(f"{idx}. {point}")
        else:
            lines.append("We'll anchor on our goals and collaboration plan as we begin.")
        lines.append("Carry this framing into the next activity.")
        tts_script = "\n".join(lines)
        narration = "Aligning on framing points before we begin."
        try:
            words = max(1, len(tts_script.split()))
            approx_seconds = safe_int(words / 2.3, default=tts_read_time_seconds)
            tts_read_time_seconds = max(30, approx_seconds)
        except Exception:
            tts_read_time_seconds = 45
    else:
        # direct: generic facilitator narration only
        tts_script = narration
        tts_read_time_seconds = 40

    payload = {
        "title": title,
        "task_type": "speech",
        "task_description": task_description,
        "instructions": instructions,
        "task_duration": duration,
        "speaker_user_id": speaker_user_id,
        "speaker_name": speaker_name,
        "cc_enabled": cc_enabled,
        "delivery_mode": delivery_mode,
        "participants": participants,
        "narration": narration,
        "tts_script": tts_script,
        "tts_read_time_seconds": tts_read_time_seconds,
        "phase_context": phase_context or "",
    }
    if framing_points is not None:
        payload["framing_key_points"] = framing_points

    task = BrainstormTask()
    task.workshop_id = workshop_id
    task.task_type = "speech"
    task.title = payload["title"]
    task.description = payload.get("task_description")
    task.duration = safe_int(payload.get("task_duration", duration), default=duration)
    task.status = "pending"
    payload_str = json.dumps(payload)
    task.prompt = payload_str
    task.payload_json = payload_str
    db.session.add(task)
    db.session.flush()

    payload["task_id"] = task.id
    current_app.logger.info(f"[Speech] Created task {task.id} for workshop {workshop_id}")
    return payload


def build_speech_preview(workshop_id: int, cfg: dict, phase_context: str | None = None) -> dict:
    """Build a non-persistent speech payload preview using the provided config.

    This mirrors get_speech_payload behavior but does not create a BrainstormTask or
    read config_json from the plan. Intended for UI preview in the editor.
    """
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return {"error": "Workshop not found"}

    # Normalize delivery mode
    delivery_mode = (cfg.get('delivery_mode') or 'direct').strip().lower()
    speaker_user_id = cfg.get("speaker_user_id")
    cc_enabled = bool(cfg.get("cc_enabled", True))

    title = "Prepared Speech"
    task_description = "A designated speaker will share remarks while everyone else listens and captures notes."
    instructions = "The speaker has the floor. Others, please mute your microphones; use chat for clarifying questions."
    duration = safe_int(TASK_REGISTRY.get("speech", {}).get("default_duration", 600), default=600)
    try:
        val = cfg.get('duration_sec')
        if isinstance(val, (int, str)):
            sval = str(val).strip()
            if sval:
                d = bounded_int(sval, default=duration, minimum=30, maximum=7200)
                duration = d
    except Exception:
        pass

    # Friendly speaker name
    speaker_name = None
    try:
        if speaker_user_id:
            speaker_pk = safe_int(speaker_user_id, default=0)
            u = db.session.get(User, speaker_pk) if speaker_pk > 0 else None
            if u:
                parts = [p for p in [getattr(u, 'first_name', None), getattr(u, 'last_name', None)] if p]
                speaker_name = ' '.join(parts) if parts else (getattr(u, 'username', None) or (u.email.split('@')[0] if getattr(u, 'email', None) else f"User {u.user_id}"))
    except Exception:
        speaker_name = None

    # Participants list (id + display name), useful for preview labels
    participants: list[dict] = []
    try:
        plist = WorkshopParticipant.query.filter_by(workshop_id=workshop_id).all()
        for p in plist:
            try:
                u = getattr(p, 'user', None)
                if u:
                    dn_parts = [getattr(u, 'first_name', None) or '', getattr(u, 'last_name', None) or '']
                    dn = (' '.join([s for s in dn_parts if s]).strip()) or (getattr(u, 'email', None) or f"User {p.user_id}")
                else:
                    dn = f"User {getattr(p, 'user_id', '')}"
                participants.append({ 'user_id': getattr(p, 'user_id', None), 'display_name': dn })
            except Exception:
                continue
    except Exception:
        participants = []

    # Resolve tts_script based on delivery mode (same logic as task builder)
    tts_script: str | None = None
    tts_read_time_seconds = 40
    if delivery_mode == 'reader':
        script_text = cfg.get('script_text')
        if isinstance(script_text, str) and script_text.strip():
            tts_script = script_text.strip()
            try:
                w = max(1, len(tts_script.split()))
                tts_read_time_seconds = safe_int(w / 2.2, default=60)
            except Exception:
                tts_read_time_seconds = 60
        else:
            doc_raw = cfg.get('document_id')
            doc_pk = safe_int(doc_raw, default=0)
            if doc_pk > 0:
                link = WorkshopDocument.query.filter_by(workshop_id=workshop_id, document_id=doc_pk).first()
                if link:
                    try:
                        doc = link.document or Document.query.get(doc_pk)
                    except Exception:
                        doc = None
                    if doc:
                        text = _extract_text_from_document(doc)
                        if text and text.strip():
                            tts_script = text.strip()[:15000]
                            try:
                                w = max(1, len(tts_script.split()))
                                tts_read_time_seconds = safe_int(w / 2.2, default=tts_read_time_seconds)
                            except Exception:
                                tts_read_time_seconds = 120
    else:
        # direct
        tts_script = (
            "We’re moving into a short speech. The speaker will share their perspective and key points. "
            "Please keep microphones muted and feel free to note questions in chat. We’ll take a moment afterwards "
            "to address any clarifications and key takeaways."
        )
        tts_read_time_seconds = 40

    return {
        "title": title,
        "task_type": "speech_preview",
        "task_description": task_description,
        "instructions": instructions,
        "task_duration": duration,
        "speaker_user_id": speaker_user_id,
        "speaker_name": speaker_name,
        "cc_enabled": cc_enabled,
        "delivery_mode": delivery_mode,
        "participants": participants,
        "tts_script": tts_script,
        "tts_read_time_seconds": tts_read_time_seconds,
        "phase_context": phase_context or "",
    }
