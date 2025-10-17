# app/service/routes/brainstorming.py
import json
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

from app.config import Config
from app.extensions import db
from app.models import BrainstormIdea, BrainstormTask, Workshop, WorkshopPlanItem
from app.utils.agenda_utils import strip_agenda_durations
from app.utils.data_aggregation import get_pre_workshop_context_json
from app.utils.json_utils import extract_json_block
from app.utils.llm_bedrock import get_chat_llm
from langchain_core.prompts import PromptTemplate

from app.service.routes.warm_up import get_cached_warmup_payload
from app.workshop.helpers import ensure_ai_participant


def _get_config_max_ai_ideas(cfg: Optional[Dict[str, Any]]) -> Tuple[int, bool]:
    cfg = cfg or {}
    ai_cfg = {}
    if isinstance(cfg.get("ai_ideas"), dict):
        ai_cfg = cfg.get("ai_ideas", {})  # type: ignore[assignment]

    default_enabled = getattr(Config, "BRAINSTORMING_AI_IDEAS_ENABLED", True)
    enabled = bool(ai_cfg.get("enabled", default_enabled))

    if not enabled:
        return 0, False

    default_max = getattr(Config, "BRAINSTORMING_AI_IDEAS_MAX_DEFAULT", 3)
    cap = getattr(Config, "BRAINSTORMING_AI_IDEAS_MAX_ABSOLUTE", max(3, default_max))

    try:
        raw_max = ai_cfg.get("max", default_max)
        max_val = int(float(str(raw_max)))
    except Exception:
        max_val = default_max

    if max_val < 0:
        max_val = 0
    if max_val > cap:
        max_val = cap
    return max_val, bool(max_val)


def _truncate_text(value: str, limit: int) -> Tuple[str, bool]:
    if not value:
        return "", False
    if len(value) <= limit:
        return value, False
    truncated = value[: max(0, limit - 3)].rstrip() + "..."
    return truncated, True


def _plan_item_config(workshop_id: int, task_type: str) -> Optional[Dict[str, Any]]:
    aliases = {
        task_type,
        task_type.replace("_", "-"),
        task_type.replace("-", "_"),
    }
    try:
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
        if not item or not item.config_json:
            return None
        if isinstance(item.config_json, str):
            return json.loads(item.config_json)
        if isinstance(item.config_json, dict):
            return item.config_json
    except Exception:
        current_app.logger.debug("[Brainstorming] Unable to load plan-level config for %s", task_type, exc_info=True)
    return None


def _collect_workshop_overview(ws: Workshop) -> Dict[str, Any]:
    try:
        participant_count = (
            ws.participants.count() if hasattr(ws.participants, "count") else len(list(ws.participants))  # type: ignore[arg-type]
        )
    except Exception:
        participant_count = 0
    organizer = getattr(ws, "organizer", None)
    organizer_name = None
    if organizer:
        for attr in ("display_name", "first_name", "email"):
            organizer_name = getattr(organizer, attr, None)
            if organizer_name:
                break
    if not organizer_name:
        organizer_name = "Unknown organizer"

    return {
        "title": ws.title,
        "objective": ws.objective or "TBD",
        "scheduled_for": ws.date_time.isoformat() if ws.date_time else "unscheduled",
        "duration_minutes": ws.duration,
        "status": ws.status,
        "organizer": organizer_name,
        "participant_count": participant_count,
    }


def _load_latest_task_payload(workshop_id: int, task_types: List[str]) -> Optional[Dict[str, Any]]:
    try:
        task = (
            BrainstormTask.query
            .filter(
                BrainstormTask.workshop_id == workshop_id,
                BrainstormTask.task_type.in_(task_types),
            )
            .order_by(BrainstormTask.created_at.desc())
            .first()
        )
        if not task or not task.payload_json:
            return None
        data = json.loads(task.payload_json)
        return data if isinstance(data, dict) else None
    except Exception:
        current_app.logger.debug("[Brainstorming] Failed to load historical payload for types %s", task_types, exc_info=True)
        return None


def _build_next_phase_snapshot(ws: Workshop) -> Dict[str, Any]:
    raw_items = getattr(ws, "plan_items", None)
    items: List[WorkshopPlanItem] = []
    if isinstance(raw_items, list):
        items = list(raw_items)
    elif raw_items is not None:
        try:
            items = list(raw_items)
        except TypeError:
            items = []
    items = sorted(items, key=lambda it: getattr(it, "order_index", 0))
    normalized: List[Tuple[str, WorkshopPlanItem]] = []
    for item in items:
        t_raw = (item.task_type or "").strip().lower()
        normalized.append((t_raw.replace("_", "-"), item))

    current_label = "brainstorming"
    current_idx = None
    for idx, (norm, _) in enumerate(normalized):
        if norm == current_label:
            current_idx = idx
            break
    if current_idx is not None and current_idx + 1 < len(normalized):
        next_item = normalized[current_idx + 1][1]
        return {
            "task_type": next_item.task_type,
            "phase": next_item.phase,
            "duration": next_item.duration,
            "description": (next_item.description or "").strip() or None,
        }
    return {
        "task_type": None,
        "phase": None,
        "duration": None,
        "description": None,
    }


def _prepare_brainstorming_prompt_inputs(workshop_id: int, phase_context: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        raise ValueError(f"Workshop {workshop_id} not found")

    cfg = _plan_item_config(workshop_id, "brainstorming") or {}
    max_ai_ideas, ai_enabled = _get_config_max_ai_ideas(cfg)

    overview = _collect_workshop_overview(ws)

    framing_payload = _load_latest_task_payload(workshop_id, ["framing"])
    framing_summary = {
        "problem_statement": framing_payload.get("problem_statement") if framing_payload else None,
        "constraints": framing_payload.get("constraints") if framing_payload else None,
        "success_criteria": framing_payload.get("success_criteria") if framing_payload else None,
        "context_summary": framing_payload.get("context_summary") if framing_payload else None,
        "key_insights": framing_payload.get("key_insights") if framing_payload else None,
    }

    warmup_payload = get_cached_warmup_payload(workshop_id) or _load_latest_task_payload(
        workshop_id,
        ["warm-up", "warm_up", "introduction"],
    )
    warmup_summary = {}
    if isinstance(warmup_payload, dict):
        warmup_summary = {
            "title": warmup_payload.get("title"),
            "instructions": warmup_payload.get("instructions") or warmup_payload.get("warm_up_instructions"),
            "task_description": warmup_payload.get("task_description"),
            "participation_norms": warmup_payload.get("participation_norms"),
            "handoff_phrase": warmup_payload.get("handoff_phrase"),
            "selected_option": warmup_payload.get("selected_option"),
            "energy_level": warmup_payload.get("energy_level"),
        }

    try:
        prework_raw = get_pre_workshop_context_json(workshop_id)
        # Strip agenda durations to prevent LLM confusion with task duration
        prework_raw = strip_agenda_durations(prework_raw)
    except Exception:
        prework_raw = ""
    prework_limit = getattr(Config, "BRAINSTORMING_PREWORK_CHAR_LIMIT", 4800)
    prework_data, prework_truncated = _truncate_text(prework_raw, int(prework_limit))

    next_phase = _build_next_phase_snapshot(ws)
    resolved_phase = (phase_context or "Brainstorming").strip() or "Brainstorming"

    prompt_inputs: Dict[str, Any] = {
        "workshop_overview": json.dumps(overview, ensure_ascii=False, indent=2),
        "framing_json": json.dumps(framing_summary, ensure_ascii=False, indent=2),
        "warmup_json": json.dumps(warmup_summary, ensure_ascii=False, indent=2),
        "prework_data": prework_data,
        "current_phase_label": resolved_phase,
        "next_phase_json": json.dumps(next_phase, ensure_ascii=False, indent=2),
        "max_ai_ideas": max_ai_ideas,
        "ai_idea_instruction": (
            "Return an empty array for ai_ideas when max_ai_ideas is 0."
            if max_ai_ideas == 0
            else f"Provide at most {max_ai_ideas} ai_ideas."
        ),
    }

    metadata = {
        "workshop_id": workshop_id,
        "max_ai_ideas": max_ai_ideas,
        "ai_enabled": ai_enabled and max_ai_ideas > 0,
        "prework_truncated": prework_truncated,
        "phase_context": resolved_phase,
    }
    return prompt_inputs, metadata


def _normalize_ai_ideas(raw_ideas: Any, max_count: int) -> Tuple[List[Dict[str, Any]], bool]:
    if not max_count or not isinstance(raw_ideas, list):
        return [], False
    normalized: List[Dict[str, Any]] = []
    truncated = False
    for idea in raw_ideas:
        if not isinstance(idea, dict):
            continue
        text = str(idea.get("text") or "").strip()
        if not text:
            continue
        rationale = str(idea.get("rationale") or idea.get("rational") or "").strip()
        tags_val = idea.get("tags")
        tags: List[str] = []
        if isinstance(tags_val, list):
            tags = [str(tag).strip() for tag in tags_val if str(tag).strip()]
        elif isinstance(tags_val, str):
            tags = [segment.strip() for segment in tags_val.split(",") if segment.strip()]
        inspiration = str(idea.get("inspiration") or idea.get("origin") or "mixed").strip() or "mixed"
        include_flag = idea.get("include_in_outputs")
        include_in_outputs = True
        if isinstance(include_flag, bool):
            include_in_outputs = include_flag
        elif isinstance(include_flag, str):
            include_in_outputs = include_flag.strip().lower() in {"true", "1", "yes", "enabled"}

        normalized.append(
            {
                "text": text,
                "rationale": rationale,
                "tags": tags,
                "inspiration": inspiration,
                "source": "ai",
                "include_in_outputs": include_in_outputs,
            }
        )
        if len(normalized) >= max_count:
            break
    if isinstance(raw_ideas, list) and len(raw_ideas) > len(normalized):
        truncated = len(raw_ideas) > max_count
    return normalized, truncated


def generate_brainstorming_text(workshop_id: int, phase_context: str) -> Tuple[Any, int, Dict[str, Any]]:
    """Generates the brainstorming task text using LLM and returns (raw_output, status, metadata)."""
    metadata: Dict[str, Any] = {}
    current_app.logger.debug(
        "[Brainstorming] Generating text for workshop %s, phase: %s",
        workshop_id,
        phase_context,
    )

    try:
        prompt_inputs, metadata = _prepare_brainstorming_prompt_inputs(
            workshop_id,
            phase_context,
        )
    except ValueError as exc:
        return str(exc), 404, metadata
    except Exception as exc:  # pragma: no cover - defensive logging
        current_app.logger.error(
            "[Brainstorming] Failed preparing prompt inputs for workshop %s: %s",
            workshop_id,
            exc,
            exc_info=True,
        )
        return f"Could not prepare brainstorming prompt: {exc}", 500, metadata

    prompt_template = """
You are the AI co-facilitator guiding the brainstorming phase of a collaborative workshop.

Workshop Snapshot (JSON):
{workshop_overview}

Framing Highlights (JSON):
{framing_json}

Warm-Up Recap (JSON):
{warmup_json}

Pre-Workshop Research (truncated when necessary):
{prework_data}

Current Phase Label: {current_phase_label}
Upcoming Phase (JSON):
{next_phase_json}

Your objectives:
1. Craft the next brainstorming task that builds directly from the framing and warm-up context.
2. Keep instructions concise, concrete, and action-oriented.
3. Encourage participants to ground ideas in the problem statement, constraints, and key insights.
4. {ai_idea_instruction}

Output format — respond with EXACTLY one JSON object using double quotes and no trailing commas. Do not include any prose or markdown before or after the JSON:
{{
    "title": string,
    "task_type": "brainstorming",
    "task_description": string,
    "instructions": string,
    "task_duration": integer (seconds, 90-900),
    "narration": string (single paragraph facilitator voice; must repeat task_description verbatim once),
    "tts_script": string (90-180 words, natural facilitator cadence; must repeat task_description verbatim once),
    "tts_read_time_seconds": integer (>= 45),
    "ai_ideas": [
        {{
            "text": string,
            "rationale": string,
            "tags": array of short strings (optional, use [] when none),
            "inspiration": "framing" | "warmup" | "prework" | "mixed"
        }}
    ],
    "ai_ideas_include_in_outputs": boolean
}}

Constraints:
- Use ONLY the provided context; label anything unknown as "TBD".
- "task_description" must be action-oriented and <= 45 words.
- "instructions" should state how many ideas or contributions are expected per participant.
- Keep "narration" and "tts_script" in warm, inclusive first-person facilitator voice.
- Keep the "tts_script" free of bullet points, numbered lists, or SSML tags.
- Limit ai_ideas to at most {max_ai_ideas} items. When {max_ai_ideas} == 0, ai_ideas must be an empty array.
- Each ai_idea must be unique, grounded in different insights, and safe for direct posting to the whiteboard.
- Always include the key "ai_ideas_include_in_outputs" set to true or false, even when ai_ideas is empty.
- If you are unsure about a value, set it to "TBD" (string) or an empty array.
- Use lowercase true/false for booleans and do not quote numeric values.
- Set "ai_ideas_include_in_outputs" to true unless the context clearly requires otherwise.

Return only the JSON object described above. Do not wrap it in markdown fences.
    """

    bedrock_llm = get_chat_llm(
        model_kwargs={
            "temperature": 0.55,
            "max_tokens": 1400,
            "top_p": 0.9,
        }
    )

    prompt = PromptTemplate.from_template(prompt_template)
    chain = prompt | bedrock_llm

    try:
        raw_output = chain.invoke(prompt_inputs)
        current_app.logger.debug(
            "[Brainstorming] Raw LLM output for %s (max_ai_ideas=%s)",
            workshop_id,
            metadata.get("max_ai_ideas"),
        )
        return raw_output, 200, metadata
    except Exception as exc:
        current_app.logger.error(
            "[Brainstorming] LLM error for workshop %s: %s",
            workshop_id,
            exc,
            exc_info=True,
        )
        return f"Error generating brainstorming task: {exc}", 500, metadata

# --- MODIFIED FUNCTION SIGNATURE ---
def get_brainstorming_task_payload(workshop_id: int, phase_context: str):
    """Generates text, creates DB record, returns payload."""
    raw_text, code, metadata = generate_brainstorming_text(workshop_id, phase_context)
    if code != 200:
        return raw_text, code

    metadata = metadata or {}
    max_ai_ideas = int(metadata.get("max_ai_ideas", 0) or 0)

    # Normalize AIMessage/dict to plain string before parsing
    try:
        from langchain_core.messages import AIMessage
    except Exception:
        AIMessage = None  # type: ignore
    try:
        if AIMessage is not None and isinstance(raw_text, AIMessage):
            raw_text = raw_text.content
        elif isinstance(raw_text, dict) and "content" in raw_text:  # type: ignore[reportGeneralTypeIssues]
            raw_text = raw_text.get("content")
    except Exception:
        pass
    if not isinstance(raw_text, str):
        raw_text = str(raw_text)

    json_block = extract_json_block(raw_text)
    if not json_block:
        raw_text_stripped = raw_text.strip()
        if raw_text_stripped.startswith("{") and raw_text_stripped.endswith("}"):
            json_block = raw_text_stripped
    if not json_block:
        current_app.logger.error(
            "[Brainstorming] Could not extract valid JSON for workshop %s. Raw output: %r",
            workshop_id,
            raw_text,
        )
        return "Could not extract valid JSON for brainstorming task.", 500

    try:
        payload = json.loads(json_block)
        if not all(k in payload for k in ["title", "task_description", "instructions", "task_duration"]):
            raise ValueError("Missing required keys in brainstorming JSON payload.")
        payload["task_type"] = "brainstorming"  # Ensure type is set

        title = str(payload.get("title") or "").strip()
        payload["title"] = title or "Collaborative Brainstorming"
        payload["task_description"] = str(payload.get("task_description") or "").strip()
        payload["instructions"] = str(payload.get("instructions") or "").strip()
        payload["narration"] = str(payload.get("narration") or "").strip()
        payload["tts_script"] = str(payload.get("tts_script") or "").strip()

        try:
            duration_raw = payload.get("task_duration", 300)
            duration_val = int(float(duration_raw))
        except Exception:
            duration_val = 300
        payload["task_duration"] = max(90, min(900, duration_val))

        try:
            tts_raw = payload.get("tts_read_time_seconds", 90)
            tts_val = int(float(tts_raw))
        except Exception:
            tts_val = max(45, payload["task_duration"] // 2)
        payload["tts_read_time_seconds"] = max(45, tts_val)

        ideas_normalized, truncated = _normalize_ai_ideas(payload.get("ai_ideas"), max_ai_ideas)
        payload["ai_ideas"] = ideas_normalized
        payload.setdefault("max_ai_ideas", max_ai_ideas)
        payload.setdefault("ai_ideas_include_in_outputs", bool(ideas_normalized))

        include_flag = payload.get("ai_ideas_include_in_outputs")
        if isinstance(include_flag, bool):
            payload["ai_ideas_include_in_outputs"] = include_flag
        elif isinstance(include_flag, str):
            payload["ai_ideas_include_in_outputs"] = include_flag.strip().lower() in {"true", "1", "yes", "enabled"}
        else:
            payload["ai_ideas_include_in_outputs"] = True
        if truncated:
            current_app.logger.info(
                "[Brainstorming] Truncated AI ideas for workshop %s to %s items",
                workshop_id,
                max_ai_ideas,
            )

        # --- Create DB Record ---
        task = BrainstormTask()
        task.workshop_id = workshop_id
        task.task_type = payload["task_type"]
        task.title = payload["title"]
        task.description = payload.get("task_description")
        payload_str = json.dumps(payload)
        task.prompt = payload_str
        task.payload_json = payload_str
        task.duration = int(payload.get("task_duration", 300))  # Default 5 mins
        task.status = "pending"  # Will be set to running by the route
        db.session.add(task)
        db.session.flush()  # Get ID
        payload['task_id'] = task.id  # Add task ID to payload
        if metadata.get("prework_truncated"):
            current_app.logger.info(
                "[Brainstorming] Prework context truncated for workshop %s", workshop_id
            )

        if ideas_normalized:
            try:
                facilitator_participant = ensure_ai_participant(workshop_id)
            except Exception as exc:
                current_app.logger.error(
                    "[Brainstorming] Failed to ensure facilitator participant for workshop %s: %s",
                    workshop_id,
                    exc,
                    exc_info=True,
                )
                facilitator_participant = None

            persisted_ideas: List[BrainstormIdea] = []
            if facilitator_participant is not None:
                for idea_data in ideas_normalized:
                    idea = BrainstormIdea()
                    idea.task_id = task.id
                    idea.participant_id = facilitator_participant.id
                    idea.content = idea_data.get("text", "")
                    idea.rationale = idea_data.get("rationale")
                    idea.source = idea_data.get("source") or "ai"
                    idea.include_in_outputs = bool(idea_data.get("include_in_outputs", True))
                    metadata_blob = {
                        "tags": idea_data.get("tags") or [],
                        "inspiration": idea_data.get("inspiration"),
                    }
                    try:
                        metadata_blob = {k: v for k, v in metadata_blob.items() if v}
                    except Exception:
                        metadata_blob = {}
                    if metadata_blob:
                        idea.metadata_json = json.dumps(metadata_blob)
                    db.session.add(idea)
                    persisted_ideas.append(idea)
                db.session.flush()
                for idx, db_idea in enumerate(persisted_ideas):
                    try:
                        payload["ai_ideas"][idx]["idea_id"] = db_idea.id
                        payload["ai_ideas"][idx]["participant_id"] = facilitator_participant.id
                        payload["ai_ideas"][idx]["include_in_outputs"] = bool(getattr(db_idea, "include_in_outputs", True))
                    except Exception:
                        pass
                current_app.logger.info(
                    "[Brainstorming] Persisted %s AI seed ideas for workshop %s",
                    len(persisted_ideas),
                    workshop_id,
                )
            else:
                current_app.logger.warning(
                    "[Brainstorming] Skipped AI idea persistence; facilitator participant unavailable for workshop %s",
                    workshop_id,
                )

        current_app.logger.info(
            "[Brainstorming] Created task %s for workshop %s (ai_ideas=%s)",
            task.id,
            workshop_id,
            len(payload.get("ai_ideas", [])),
        )
        return payload  # Return dict

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        current_app.logger.error(
            f"[Brainstorming] Payload processing error for workshop {workshop_id}: {e}\nJSON Block: {json_block}",
            exc_info=True
        )
        db.session.rollback()
        return f"Invalid brainstorming task format: {e}", 500
    except Exception as e:
        current_app.logger.error(
            f"[Brainstorming] Unexpected error creating task for workshop {workshop_id}: {e}",
            exc_info=True
        )
        db.session.rollback()
        return "Server error creating brainstorming task.", 500
