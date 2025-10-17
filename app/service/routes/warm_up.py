"""Warm-up task payload generator and integration helpers."""
from __future__ import annotations

import copy
import json
from threading import RLock
from typing import Any, Dict, List, Optional, Sequence, cast

from flask import current_app

from langchain_core.prompts import PromptTemplate

from app.extensions import db
from app.models import BrainstormTask, Workshop, WorkshopPlanItem
from app.tasks.registry import TASK_REGISTRY
from app.utils.data_aggregation import get_pre_workshop_context_json
from app.utils.json_utils import extract_json_block
from app.utils.llm_bedrock import get_chat_llm
from app.utils.telemetry import log_event


class WarmupGenerationError(RuntimeError):
    """Raised when the warm-up payload cannot be generated."""


WARM_TASK_TYPE = "warm-up"
LEGACY_WARM_TASK_NAMES = {"introduction"}
CANONICAL_WARM_TASK_NAMES = {WARM_TASK_TYPE, WARM_TASK_TYPE.replace("-", "_")}
WARM_TASK_ALIASES = CANONICAL_WARM_TASK_NAMES | LEGACY_WARM_TASK_NAMES

_warmup_cache_lock = RLock()
_warmup_cache: Dict[int, Dict[str, Any]] = {}


def cache_warmup_payload(workshop_id: int, payload: Dict[str, Any]) -> None:
    """Persist a defensive copy of the latest warm-up payload for a workshop."""
    with _warmup_cache_lock:
        _warmup_cache[int(workshop_id)] = copy.deepcopy(payload)
    try:
        log_event("warmup.cache.store", {
            "workshop_id": int(workshop_id),
            "options": len(payload.get("options") or []),
            "selected_index": payload.get("selected_index"),
            "task_id": payload.get("task_id"),
        })
    except Exception:
        pass


def get_cached_warmup_payload(workshop_id: int) -> Optional[Dict[str, Any]]:
    with _warmup_cache_lock:
        cached = _warmup_cache.get(int(workshop_id))
    if cached is None:
        try:
            log_event("warmup.cache.miss", {"workshop_id": int(workshop_id)})
        except Exception:
            pass
        return None
    snapshot = copy.deepcopy(cached)
    try:
        log_event("warmup.cache.hit", {
            "workshop_id": int(workshop_id),
            "options": len(snapshot.get("options") or []),
            "selected_index": snapshot.get("selected_index"),
            "task_id": snapshot.get("task_id"),
        })
    except Exception:
        pass
    return snapshot


def clear_warmup_cache(workshop_id: int) -> None:
    with _warmup_cache_lock:
        _warmup_cache.pop(int(workshop_id), None)
    try:
        log_event("warmup.cache.clear", {"workshop_id": int(workshop_id)})
    except Exception:
        pass


def _coerce_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:  # pragma: no cover - defensive import path
        from langchain_core.messages import BaseMessage  # type: ignore

        if isinstance(value, BaseMessage):
            content = getattr(value, "content", None)
            if isinstance(content, str):
                return content
    except Exception:
        pass
    if isinstance(value, dict):
        for key in ("content", "text", "generated_text", "message"):
            val = value.get(key)
            if isinstance(val, str):
                return val
    return str(value)


def _compute_read_time(text: str, *, minimum: int = 30, default: int = 75) -> int:
    try:
        words = len((text or "").split())
        if words <= 0:
            return default
        seconds = int(round(words / 2.3))
        return max(minimum, seconds)
    except Exception:
        return default


def _truncate(text: str, limit: int = 6000) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _resolve_duration_seconds(cfg: Dict[str, Any] | None) -> int:
    cfg = cfg or {}
    duration = int(TASK_REGISTRY.get(WARM_TASK_TYPE, {}).get("default_duration", 180))
    override = cfg.get("duration_sec") or cfg.get("duration")
    try:
        if override is None or override == "":
            return duration
        candidate = int(float(str(override).strip()))
        if 30 <= candidate <= 1800:
            return candidate
    except Exception:
        pass
    return duration


def _collect_workshop_overview(ws: Workshop) -> str:
    organizer = getattr(ws, "organizer", None)
    organizer_name = None
    if organizer:
        for attr in ("display_name", "first_name", "email"):
            organizer_name = getattr(organizer, attr, None)
            if organizer_name:
                break
    if not organizer_name:
        organizer_name = "Unknown organizer"

    try:
        participant_count = (
            ws.participants.count() if hasattr(ws.participants, "count") else len(ws.participants)  # type: ignore[arg-type]
        )
    except Exception:
        participant_count = 0

    lines = [
        f"Title: {ws.title}",
        f"Objective: {ws.objective or 'TBD'}",
        f"Organizer: {organizer_name}",
        f"Scheduled: {ws.date_time.strftime('%Y-%m-%d %H:%M UTC') if ws.date_time else 'Not scheduled'}",
        f"Expected Duration: {ws.duration or 'TBD'} minutes",
        f"Participants Invited: {participant_count}",
    ]
    return "\n".join(lines)


def _summarize_plan(ws: Workshop) -> str:
    try:
        plan_seq = cast(Sequence[WorkshopPlanItem], getattr(ws, "plan_items", []) or [])
        plan_items = sorted(list(plan_seq), key=lambda it: getattr(it, "order_index", 0))
    except Exception:
        plan_items = []
    if not plan_items:
        return "No normalized plan items available."

    current_idx = ws.current_task_index if isinstance(ws.current_task_index, int) else -1
    lines: List[str] = []
    for idx, item in enumerate(plan_items):
        marker = "→" if idx == current_idx else ("✓" if 0 <= current_idx > idx else "•")
        task_label = (item.task_type or "task").replace("_", " ")
        phase = (item.phase or "").strip()
        duration = f"{item.duration or 0}s"
        if phase and phase.lower() != task_label.lower():
            label = f"{task_label} – {phase}"
        else:
            label = task_label
        lines.append(f"{marker} {idx + 1}. {label} ({duration})")
        if len(lines) >= 8:
            break
    return "\n".join(lines)


def _collect_organizer_hints(cfg: Dict[str, Any] | None, *, duration_sec: int) -> str:
    cfg = cfg or {}
    key_points = cfg.get("key_points")
    if isinstance(key_points, list):
        kp = [f"- {str(x).strip()}" for x in key_points if str(x).strip()]
    elif isinstance(key_points, str):
        kp = [f"- {line.strip()}" for line in key_points.splitlines() if line.strip()]
    else:
        kp = []
    hints = [
        f"Warm-up prompt (optional seed): {cfg.get('warmup_prompt') or 'None provided'}",
        f"Preferred style: {cfg.get('style') or 'Fun, safe, inclusive'}",
        f"Audience: {cfg.get('audience') or 'Workshop participants'}",
        f"Available timebox: {duration_sec} seconds",
    ]
    if kp:
        hints.append("Key points:")
        hints.extend(kp)
    return "\n".join(hints)


def _get_framing_context(workshop_id: int) -> Dict[str, Any]:
    try:
        framing_task = (
            BrainstormTask.query
            .filter_by(workshop_id=workshop_id, task_type="framing")
            .order_by(BrainstormTask.created_at.desc())
            .first()
        )
        if not framing_task or not framing_task.payload_json:
            return {}
        payload = json.loads(framing_task.payload_json)
        if not isinstance(payload, dict):
            return {}
        panel = payload.get("facilitator_panel") if isinstance(payload.get("facilitator_panel"), dict) else {}
        return {
            "tts_script": payload.get("tts_script") or "",
            "warmup_segue": panel.get("warmup_segue") if isinstance(panel, dict) else panel or "",
            "warmup_instruction": panel.get("warmup_instruction") if isinstance(panel, dict) else "",
            "participation_norms": panel.get("participation_norms") if isinstance(panel, dict) else [],
            "problem_statement": payload.get("problem_statement", ""),
            "success_criteria": payload.get("success_criteria", []),
        }
    except Exception as exc:
        current_app.logger.warning("[WarmUp] Could not retrieve framing context: %s", exc)
        return {}


def _get_next_phase_context(ws: Workshop) -> Dict[str, Any]:
    try:
        plan_seq = cast(Sequence[WorkshopPlanItem], getattr(ws, "plan_items", []) or [])
        items = sorted(list(plan_seq), key=lambda it: getattr(it, "order_index", 0))
    except Exception:
        items = []
    normalized: List[tuple[str, WorkshopPlanItem]] = []
    for item in items:
        t_raw = (item.task_type or "").strip().lower()
        t_norm = t_raw.replace("_", "-")
        normalized.append((t_norm, item))
    warm_indices = [idx for idx, (t, _) in enumerate(normalized) if t in WARM_TASK_ALIASES]
    if warm_indices:
        idx = warm_indices[0]
        if idx + 1 < len(normalized):
            next_item = normalized[idx + 1][1]
            return {
                "next_task_type": (next_item.task_type or "").replace("-", "_") or "brainstorming",
                "next_phase": next_item.phase or next_item.task_type or "Ideation",
                "next_duration": next_item.duration or 0,
            }
    return {"next_task_type": "brainstorming", "next_phase": "Ideation", "next_duration": 600}


def _score_warmup_option(option: Dict[str, Any], context: Dict[str, Any]) -> float:
    score = 0.0

    available = context.get("available_time") or 0
    timer = option.get("timer_sec") or 0
    if available:
        diff = abs(timer - available)
        if diff <= 30:
            score += 30
        elif diff <= 60:
            score += 20
        elif diff <= 120:
            score += 10
    else:
        score += 10 if 60 <= timer <= 300 else 0

    participants = context.get("participant_count") or 0
    mode = (option.get("mode") or "solo").lower()
    if mode == "solo":
        score += 20
    elif mode == "pairs" and participants >= 2:
        score += 20 if participants % 2 == 0 else 15
    elif mode == "groups" and participants >= 4:
        score += 20 if participants >= 6 else 10

    if context.get("needs_energy_boost", True):
        title = (option.get("title") or "").lower()
        if "energ" in title:
            score += 25
        elif "quick" in title or "pulse" in title:
            score += 15

    objective = set((context.get("objective") or "").lower().split())
    prompt_keywords = set((option.get("prompt") or "").lower().split())
    score += min(25, len(objective & prompt_keywords) * 5)
    return score


def _gather_prompt_inputs(
    ws: Workshop,
    cfg: Dict[str, Any] | None,
    phase_context: Optional[str],
    framing_context: Dict[str, Any],
    next_phase: Dict[str, Any],
    duration_sec: int,
) -> Dict[str, str]:
    overview = _collect_workshop_overview(ws)
    hints = _collect_organizer_hints(cfg, duration_sec=duration_sec)
    plan_snapshot = _summarize_plan(ws)
    try:
        prework = get_pre_workshop_context_json(ws.id)
    except Exception:
        prework = ""

    norms = framing_context.get("participation_norms")
    if isinstance(norms, list):
        norms_line = ", ".join(str(n).strip() for n in norms if str(n).strip())
    elif isinstance(norms, str):
        norms_line = norms.strip()
    else:
        norms_line = ""

    framing_summary = ""
    if framing_context:
        framing_summary = (
            "Previous Framing Recap:\n"
            f"- Problem reminder: {framing_context.get('problem_statement') or 'TBD'}\n"
            f"- Participation norms already stated: {norms_line or 'Not provided'}\n"
            f"- Last segue line: {framing_context.get('warmup_segue') or 'None provided'}\n"
            f"- Seed prompt: {framing_context.get('warmup_instruction') or 'None provided'}"
        )

    next_phrase = (
        f"Next: {next_phase.get('next_phase', 'Ideation')}"
        f" ({next_phase.get('next_task_type', 'brainstorming')})."
    )

    return {
        "workshop_overview": overview,
        "organizer_hints": hints,
        "agenda_snapshot": plan_snapshot,
        "prework_data": _truncate(prework, 5000),
        "phase_context": (phase_context or "Warm-up").strip() or "Warm-up",
        "framing_summary": framing_summary,
        "next_phase_info": next_phrase,
        "hint_timebox_seconds": str(duration_sec),
    }


def invoke_warm_up_model(inputs: Dict[str, str]) -> Dict[str, Any]:
    template = """
You are facilitating a workshop warm-up activity. You must create an ice-breaker that:
1. Follows naturally from what was JUST said in framing (don't repeat it)
2. Energizes participants for active participation
3. Smoothly transitions to the next phase

{framing_summary}

Current Context:
{workshop_overview}

Organizer Preferences:
{organizer_hints}

Coming Next:
{next_phase_info}

Workshop Flow:
{agenda_snapshot}

Background Info:
{prework_data}

Generate a JSON response with:
- facilitator_intro: string (1-2 sentences bridging from framing, acknowledge what was said)
- participation_recap: string (<=30 words, reminder without repeating full list)
- warm_up_instructions: string (<=40 words, clear participant instructions)
- task_duration: integer (seconds, 60-300)
- narration: string (<=150 words, natural facilitator speech)
- tts_script: string (70-120 words, natural speaking voice, no repetition of framing content)
- estimated_read_time: integer (seconds for TTS)
- options: array of 3-5 warm-up activities:
  - title: string (<=8 words)
  - prompt: string (exact question/activity)
  - mode: "solo" | "pairs" | "groups"
  - timer_sec: integer
  - energy_level: "low" | "medium" | "high"
- selected_index: integer (best option for this context)
- handoff_phrase: string (1 sentence to transition to the next phase)

Rules:
- DON'T repeat what was said in framing
- DO acknowledge and build upon it
- Keep energy appropriate for the available time ({hint_timebox_seconds} seconds)
- Ensure selected option fits participant count and time
- Return ONLY strict JSON. No markdown, commentary, or trailing commas.
"""

    try:
        llm = get_chat_llm(
            model_kwargs={
                "temperature": 0.6,
                "max_tokens": 1500,
                "top_p": 0.85,
            }
        )
        prompt = PromptTemplate.from_template(template)
        chain = prompt | llm
        raw = chain.invoke(inputs)
        text = _coerce_to_text(raw)
        json_block = extract_json_block(text) or text
        data = json.loads(json_block)
        if not isinstance(data, dict):
            raise WarmupGenerationError("Model output was not a JSON object.")
        return data
    except WarmupGenerationError:
        raise
    except Exception as exc:
        current_app.logger.error("[WarmUp] LLM generation failed: %s", exc, exc_info=True)
        raise WarmupGenerationError(f"Warm-up generation error: {exc}") from exc


def _normalize_content(
    raw: Dict[str, Any] | None,
    ws: Workshop,
    plan_duration: int,
    scoring_context: Dict[str, Any],
) -> Dict[str, Any]:
    if raw is None:
        raise WarmupGenerationError("LLM response is empty; cannot build warm-up content.")
    if not isinstance(raw, dict):
        raise WarmupGenerationError("LLM response must be a JSON object.")

    def _require_string(key: str) -> str:
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        raise WarmupGenerationError(f"LLM response missing required string field '{key}'.")

    def _require_int(key: str, *, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
        val = raw.get(key)
        if val is None:
            raise WarmupGenerationError(f"LLM response missing required integer field '{key}'.")
        try:
            num = int(val)
        except Exception as exc:
            raise WarmupGenerationError(f"LLM response missing required integer field '{key}'.") from exc
        if minimum is not None and num < minimum:
            raise WarmupGenerationError(f"LLM field '{key}' below minimum {minimum}.")
        if maximum is not None and num > maximum:
            raise WarmupGenerationError(f"LLM field '{key}' above maximum {maximum}.")
        return num

    options_raw = raw.get("options")
    if not isinstance(options_raw, list) or not options_raw:
        raise WarmupGenerationError("LLM response must include at least one warm-up option.")

    cleaned: List[Dict[str, Any]] = []
    for idx, opt in enumerate(options_raw):
        if not isinstance(opt, dict):
            raise WarmupGenerationError("Each warm-up option must be an object.")
        title = opt.get("title")
        prompt = opt.get("prompt")
        mode = opt.get("mode")
        timer = opt.get("timer_sec")
        energy = opt.get("energy_level")
        if not isinstance(title, str) or not title.strip():
            raise WarmupGenerationError(f"Warm-up option {idx} missing title.")
        if not isinstance(prompt, str) or not prompt.strip():
            raise WarmupGenerationError(f"Warm-up option {idx} missing prompt.")
        if not isinstance(mode, str) or not mode.strip():
            raise WarmupGenerationError(f"Warm-up option {idx} missing mode.")
        if not isinstance(timer, (int, float, str)):
            raise WarmupGenerationError(f"Warm-up option {idx} missing timer_sec.")
        if not isinstance(energy, str) or not energy.strip():
            raise WarmupGenerationError(f"Warm-up option {idx} missing energy_level.")
        try:
            timer_int = int(timer)
        except Exception as exc:
            raise WarmupGenerationError(f"Warm-up option {idx} has invalid timer_sec.") from exc
        cleaned.append(
            {
                "title": title.strip(),
                "prompt": prompt.strip(),
                "mode": mode.strip().lower(),
                "timer_sec": timer_int,
                "energy_level": energy.strip().lower(),
            }
        )

    options = cleaned

    context = dict(scoring_context)
    context.setdefault("available_time", plan_duration)

    scores = [(idx, _score_warmup_option(opt, context)) for idx, opt in enumerate(options)]
    best_index = max(scores, key=lambda item: item[1])[0] if scores else 0

    selected_index_raw = raw.get("selected_index", best_index)
    try:
        llm_selected = int(selected_index_raw)
    except Exception:
        llm_selected = best_index
    if not 0 <= llm_selected < len(options):
        llm_selected = best_index

    selected_option = options[llm_selected]
    timer = selected_option.get("timer_sec") or plan_duration or 180
    if plan_duration:
        timer = plan_duration
        selected_option["timer_sec"] = plan_duration

    facilitator_intro = _require_string("facilitator_intro")
    participation_recap = _require_string("participation_recap")
    warm_up_instructions = _require_string("warm_up_instructions")
    narration = _require_string("narration")
    tts_script = _require_string("tts_script")
    handoff_phrase = _require_string("handoff_phrase")
    _require_int("task_duration", minimum=30, maximum=1800)
    estimated_raw = raw.get("estimated_read_time")
    if estimated_raw is None:
        estimated_read_time = _compute_read_time(tts_script)
    else:
        try:
            estimated_read_time = int(estimated_raw)
        except Exception:
            estimated_read_time = _compute_read_time(tts_script)
    if estimated_read_time < 30:
        estimated_read_time = _compute_read_time(tts_script)

    return {
        "facilitator_intro": facilitator_intro,
        "participation_recap": participation_recap,
        "warm_up_instructions": warm_up_instructions,
        "task_duration": timer,
        "narration": narration,
        "tts_script": tts_script,
        "estimated_read_time": estimated_read_time,
        "handoff_phrase": handoff_phrase,
        "options": options,
        "selected_index": llm_selected,
        "selected_option": selected_option,
    }


def _build_facilitator_panel(content: Dict[str, Any]) -> Dict[str, Any]:
    option = content.get("selected_option") or {}
    return {
        "task_title": f"Warm-Up: {option.get('title', 'Icebreaker')}",
        "intro_text": content.get("facilitator_intro"),
        "instructions": content.get("warm_up_instructions"),
        "activity_prompt": option.get("prompt"),
        "mode": option.get("mode"),
        "timer": option.get("timer_sec"),
        "participation_reminder": content.get("participation_recap"),
        "handoff": content.get("handoff_phrase"),
    }


def get_warm_up_payload(workshop_id: int, phase_context: Optional[str] = None) -> Dict[str, Any]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        raise WarmupGenerationError(f"Workshop {workshop_id} not found")
    cfg = _get_plan_item_config(workshop_id, WARM_TASK_TYPE) or {}
    return build_warm_up_payload(ws, cfg, phase_context)


def build_warm_up_payload(ws: Workshop, cfg: Dict[str, Any], phase_context: Optional[str]) -> Dict[str, Any]:
    duration_sec = _resolve_duration_seconds(cfg)
    framing_context = _get_framing_context(ws.id)
    next_phase = _get_next_phase_context(ws)
    prompt_inputs = _gather_prompt_inputs(ws, cfg, phase_context, framing_context, next_phase, duration_sec)

    raw_response = invoke_warm_up_model(prompt_inputs)

    try:
        participant_count = (
            ws.participants.count() if hasattr(ws.participants, "count") else len(ws.participants)  # type: ignore[arg-type]
        )
    except Exception:
        participant_count = 0
    scoring_context = {
        "participant_count": participant_count,
        "available_time": duration_sec,
        "objective": ws.objective or "",
        "needs_energy_boost": True,
    }

    content = _normalize_content(raw_response, ws, duration_sec, scoring_context)
    selected_option = content["selected_option"]
    task_duration = content["task_duration"]

    facilitator_panel = _build_facilitator_panel(content)

    payload: Dict[str, Any] = {
        "task_id": None,
        "task_type": WARM_TASK_TYPE,
        "title": selected_option.get("title", "Warm-Up"),
        "task_description": selected_option.get("prompt"),
        "description": selected_option.get("prompt"),
        "instructions": content.get("warm_up_instructions"),
        "task_duration": task_duration,
        "duration": task_duration,
        "phase_context": phase_context or "Warm-up",
        "facilitator_intro": content.get("facilitator_intro"),
        "narration": content.get("narration"),
        "tts_script": content.get("tts_script"),
        "tts_read_time_seconds": content.get("estimated_read_time"),
        "estimated_read_time": content.get("estimated_read_time"),
        "options": content.get("options"),
        "selected_index": content.get("selected_index"),
        "selected_option": selected_option,
        "participation_norms": content.get("participation_recap"),
        "handoff_phrase": content.get("handoff_phrase"),
        "facilitator_panel": facilitator_panel,
        "mode": selected_option.get("mode"),
        "energy_level": selected_option.get("energy_level", "medium"),
        "next_phase": next_phase,
    }

    task = BrainstormTask()
    task.workshop_id = ws.id
    task.task_type = WARM_TASK_TYPE
    task.title = payload["title"]
    task.description = payload.get("description")
    task.duration = int(task_duration)
    task.status = "pending"
    payload_json = json.dumps(payload)
    task.prompt = payload_json
    task.payload_json = payload_json

    db.session.add(task)
    db.session.flush()

    payload["task_id"] = task.id
    current_app.logger.info("[WarmUp] Created task %s for workshop %s", task.id, ws.id)
    try:
        cache_warmup_payload(ws.id, payload)
    except Exception:
        current_app.logger.debug("[WarmUp] Unable to cache payload for workshop %s", ws.id, exc_info=True)
    return payload


def build_warm_up_preview(workshop_id: int, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return {"error": "Workshop not found"}
    cfg = cfg or _get_plan_item_config(workshop_id, WARM_TASK_TYPE) or {}
    duration_sec = _resolve_duration_seconds(cfg)
    framing_context = _get_framing_context(ws.id)
    next_phase = _get_next_phase_context(ws)
    prompt_inputs = _gather_prompt_inputs(ws, cfg, "Preview", framing_context, next_phase, duration_sec)

    try:
        raw_response = invoke_warm_up_model(prompt_inputs)
    except WarmupGenerationError as exc:
        return {"error": str(exc)}

    scoring_context = {
        "participant_count": 0,
        "available_time": duration_sec,
        "objective": ws.objective or "",
        "needs_energy_boost": True,
    }
    content = _normalize_content(raw_response, ws, duration_sec, scoring_context)
    return {
        "preview": True,
        "options": content.get("options"),
        "selected_index": content.get("selected_index"),
        "tts_script": content.get("tts_script"),
        "estimated_read_time": content.get("estimated_read_time"),
    }


def _get_plan_item_config(workshop_id: int, task_type: str) -> Optional[Dict[str, Any]]:
    try:
        aliases = {
            task_type,
            task_type.replace("_", "-"),
            task_type.replace("-", "_"),
        }
        item = (
            WorkshopPlanItem.query
            .filter(
                WorkshopPlanItem.workshop_id == workshop_id,
                WorkshopPlanItem.enabled.is_(True),
                WorkshopPlanItem.task_type.in_(aliases),
            )
            .order_by(WorkshopPlanItem.order_index.asc())
            .first()
        )
        if item and item.config_json:
            if isinstance(item.config_json, str):
                return json.loads(item.config_json)
            if isinstance(item.config_json, dict):
                return item.config_json
    except Exception:
        pass
    return None

