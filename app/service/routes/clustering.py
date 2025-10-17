# app/service/routes/clustering.py
"""
Clustering and Voting
- Correct ideas (grammar/spelling/clarity) and group into clusters
- Cluster ideas with labels and descriptions (gists)
- Detect duplicate ideas; map canonical <-> duplicate relationships
- Persist updates; return UI-ready payload with orphans and representatives
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Set

from flask import current_app
from langchain_core.prompts import PromptTemplate

from app.extensions import db
from app.models import Workshop, BrainstormTask, BrainstormIdea, IdeaCluster, WorkshopParticipant, WorkshopPlanItem
from app.config import Config
from app.utils.json_utils import extract_json_block
from app.utils.data_aggregation import get_pre_workshop_context_json
from app.utils.llm_bedrock import get_chat_llm, get_chat_llm_pro

# ---------------------------- helpers & plumbing ----------------------------
def _strip_agenda_durations(pre_workshop_data: str) -> str:
    """Remove duration_minutes from agenda items to prevent LLM confusion.
    
    Agenda durations are planning estimates for entire phases, not task timers.
    Removing them forces LLM to assess actual task complexity and generate
    appropriate task_duration values independently.
    """
    try:
        context_dict = json.loads(pre_workshop_data) if isinstance(pre_workshop_data, str) else pre_workshop_data
        if isinstance(context_dict, dict) and 'agenda' in context_dict:
            agenda = context_dict.get('agenda', {})
            if isinstance(agenda, dict) and 'items' in agenda:
                for item in agenda.get('items', []):
                    if isinstance(item, dict):
                        item.pop('duration_minutes', None)
                        item.pop('estimated_duration', None)
        return json.dumps(context_dict, ensure_ascii=False)
    except Exception as exc:
        current_app.logger.warning(
            f"[Clustering] Failed to strip agenda durations: {exc}, using original context"
        )
        return pre_workshop_data


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
        current_app.logger.debug("[Voting] Unable to load plan-level config for %s", task_type, exc_info=True)
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
        current_app.logger.debug("[Voting] Failed to load historical payload for types %s", task_types, exc_info=True)
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

    current_label = "voting"
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
def _prepare_voting_prompt_inputs(workshop_id: int, phase_context: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        raise ValueError(f"Workshop {workshop_id} not found")

    cfg = _plan_item_config(workshop_id, "voting") or {}

    overview = _collect_workshop_overview(ws)

    framing_payload = _load_latest_task_payload(workshop_id, ["framing"])
    framing_summary = {
        "problem_statement": framing_payload.get("problem_statement") if framing_payload else None,
        "constraints": framing_payload.get("constraints") if framing_payload else None,
        "success_criteria": framing_payload.get("success_criteria") if framing_payload else None,
        "context_summary": framing_payload.get("context_summary") if framing_payload else None,
        "key_insights": framing_payload.get("key_insights") if framing_payload else None,
    }
    
    warmup_payload = _load_latest_task_payload(
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
        
    brainstorming_payload = _load_latest_task_payload(workshop_id, ["brainstorming"])
    brainstorming_summary = {}
    if isinstance(brainstorming_payload, dict):
        brainstorming_summary = {
            "title": brainstorming_payload.get("title"),
            "instructions": brainstorming_payload.get("instructions"),
            "task_description": brainstorming_payload.get("task_description"),
            "participation_norms": brainstorming_payload.get("participation_norms"),
            "handoff_phrase": brainstorming_payload.get("handoff_phrase"),
            "selected_option": brainstorming_payload.get("selected_option"),
            "energy_level": brainstorming_payload.get("energy_level"),
        }

    try:
        prework_raw = get_pre_workshop_context_json(workshop_id)
    except Exception:
        prework_raw = ""
    prework_limit = getattr(Config, "VOTING_PREWORK_CHAR_LIMIT", 4800)
    prework_data, prework_truncated = _truncate_text(prework_raw, int(prework_limit))

    next_phase = _build_next_phase_snapshot(ws)
    resolved_phase = (phase_context or "feasibility").strip() or "Feasibility"

    prompt_inputs: Dict[str, Any] = {
        "workshop_overview": json.dumps(overview, ensure_ascii=False, indent=2),
        "framing_json": json.dumps(framing_summary, ensure_ascii=False, indent=2),
        "warmup_json": json.dumps(warmup_summary, ensure_ascii=False, indent=2),
        "brainstorming_json": json.dumps(brainstorming_summary, ensure_ascii=False, indent=2),
        "prework_data": prework_data,
        "current_phase_label": resolved_phase,
        "next_phase_json": json.dumps(next_phase, ensure_ascii=False, indent=2),

    }

    metadata = {
        "workshop_id": workshop_id,
        "prework_truncated": prework_truncated,
        "phase_context": resolved_phase,
    }
    return prompt_inputs, metadata



CLUSTERING_PROMPT_TEMPLATE = """
You are the workshop facilitator, research analyst, and expert editor. 
You correct idea text for clarity, cluster ideas into themes, and set up the voting phase that follows brainstorming.
After clustering, prepare the market research context based on the clustering results.

Workshop Snapshot (JSON):
{workshop_overview}

Framing Highlights (JSON):
{framing_json}

Warm-Up Summary (JSON):
{warmup_json}

Brainstorming Summary (JSON):
{brainstorming_json}

Pre-Workshop Research (truncated when necessary):
{pre_workshop_data}

Current Phase Label: {current_phase_label}

Phase Context Narrative:
{phase_context}

Submitted Ideas (indexed):
{ideas_text}

Idea Reference Map (JSON):
{ideas_json}

Upcoming Phase (JSON):
{next_phase_json}

CRITICAL DURATION RULES:

The pre_workshop_data may contain an AGENDA with time estimates. These are HIGH-LEVEL PLANNING ESTIMATES
for entire workshop phases, NOT actual task timers. DO NOT use agenda durations for task_duration.

Calculate task_duration based on THIS activity's complexity:
- Simple clustering (5-10 ideas): 300-600 seconds (5-10 minutes)
- Medium clustering (11-20 ideas): 600-900 seconds (10-15 minutes)  
- Complex clustering (20+ ideas): 900-1200 seconds (15-20 minutes)

MANDATORY CONSISTENCY REQUIREMENTS:
1. task_duration MUST be in SECONDS (not minutes)
2. task_duration range: 180-1800 seconds for clustering_voting
3. If narration mentions time, it MUST match task_duration/60 EXACTLY (±5%)
4. Convert task_duration to minutes for narration: divide by 60, round to nearest minute
5. Example: task_duration=600 → narration must say "10 minutes" (not "several minutes", not agenda values)

CORRECT WORKFLOW:
Step 1: Count ideas ({ideas_text}) → assess complexity → decide task_duration in SECONDS
Step 2: Convert for narration: task_duration / 60 = X minutes
Step 3: Write narration using EXACT time from Step 2
Step 4: Verify narration time matches task_duration

CORRECT EXAMPLES:
✓ task_duration: 300, narration: "You have 5 minutes to review the clusters and vote"
✓ task_duration: 600, narration: "Take 10 minutes to analyze and vote on the clusters"
✓ task_duration: 720, narration: "You have 12 minutes for this voting phase"

INCORRECT EXAMPLES:
❌ task_duration: 900, narration: "You have 15 minutes" (if this matches agenda but not idea count)
❌ task_duration: 120, narration: "You have 15 minutes" (650% mismatch!)
❌ task_duration: 300, narration: "Take a few minutes to vote" (vague, must specify "5 minutes")

Instructions:
1. Correct each idea for grammar, clarity, and spelling without changing meaning.
2. Cluster the ideas into 3-7 themes with concise labels and gists.
3. Provide a short description for each cluster and select a representative idea.
4. Detect near-duplicate ideas and map them to the canonical idea inside the same cluster.
5. When referencing ideas, always use the `idea_id` from the reference map (indices are provided only for readability).

Rules:
- Use only the idea identifiers provided. Do not invent new ideas.
- If an idea does not clearly fit a cluster, leave it unassigned (it becomes an orphan).
- narration and tts_script must be single paragraphs, natural facilitator voice, no bullet points or numbering, no markdown.
- narration: purpose → method → invite review → voting action → timing cue.
- tts_script: 90–180 words covering purpose, how to review, how to vote, and timing cues.
- Return strictly valid JSON. No comments, trailing commas, or markdown fences.

Respond with ONLY the JSON object matching this contract:
{{
  "title": "Vote on Idea Clusters",
  "task_type": "clustering_voting",
  "task_description": <string>,
  "instructions": <string>,
  "task_duration": <int>,
  "corrected": [{{"idea_id": <int>, "corrected_text": <string>}}],
  "clusters": [{{"label": <string>, "name": <string>, "gist": <string>, "description": <string>, "idea_ids": [<int>], "idea_indices": [<int>], "representative_id": <int>, "examples": [<int>]}}],
  "duplicates": [{{"canonical_id": <int>, "duplicate_ids": [<int>]}}],
  "rationale": <string>,
  "market_research_context": <string>,
  "market_target_segment": <string>,
  "market_positioning": <string>,
  "go_to_market_strategy": <string>,
  "competitive_alternatives": <string>,
  "narration": <string>,
  "tts_script": <string>,
  "tts_read_time_seconds": <int>
}}
"""

CLUSTERING_PROMPT = PromptTemplate.from_template(CLUSTERING_PROMPT_TEMPLATE)


@dataclass
class ClusteringContext:
    workshop: Workshop
    phase_label: str
    ideas: List[BrainstormIdea]
    ideas_text: str
    ideas_json: List[Dict[str, Any]]
    index_to_id: Dict[int, int]
    id_to_index: Dict[int, int]
    ideas_by_id: Dict[int, BrainstormIdea]
    prompt_inputs: Dict[str, Any]
    metadata: Dict[str, Any]


def _prepare_clustering_context(
    workshop_id: int,
    phase_context: str,
    ideas: Sequence[BrainstormIdea],
) -> ClusteringContext:
    workshop = db.session.get(Workshop, workshop_id)
    if not workshop:
        raise ValueError(f"Workshop {workshop_id} not found")

    prompt_inputs, metadata = _prepare_voting_prompt_inputs(workshop_id, phase_context)

    ideas_list = list(ideas)
    ideas_text = "\n".join(f"{idx}: {idea.content}" for idx, idea in enumerate(ideas_list))
    ideas_json = [
        {"idea_id": idea.id, "text": idea.corrected_text or idea.content or ""}
        for idea in ideas_list
    ]
    index_to_id = {idx: idea.id for idx, idea in enumerate(ideas_list)}
    id_to_index = {idea.id: idx for idx, idea in enumerate(ideas_list)}
    ideas_by_id = {idea.id: idea for idea in ideas_list}

    try:
        pre_workshop_data = get_pre_workshop_context_json(workshop_id)
        # Strip agenda duration_minutes to prevent LLM confusion with task duration
        pre_workshop_data = _strip_agenda_durations(pre_workshop_data)
    except Exception:
        pre_workshop_data = ""

    prompt_inputs.update(
        {
            "ideas_text": ideas_text,
            "ideas_json": json.dumps(ideas_json, ensure_ascii=False, indent=2),
            "phase_context": phase_context,
            "pre_workshop_data": pre_workshop_data or prompt_inputs.get("prework_data", ""),
        }
    )

    if "prework_data" not in prompt_inputs:
        prompt_inputs["prework_data"] = prompt_inputs.get("pre_workshop_data", "")
    if "pre_workshop_data" not in prompt_inputs:
        prompt_inputs["pre_workshop_data"] = prompt_inputs.get("prework_data", "")

    metadata.update(
        {
            "idea_count": len(ideas_list),
            "workshop_title": workshop.title,
        }
    )

    return ClusteringContext(
        workshop=workshop,
        phase_label=metadata.get("phase_context", phase_context),
        ideas=ideas_list,
        ideas_text=ideas_text,
        ideas_json=ideas_json,
        index_to_id=index_to_id,
        id_to_index=id_to_index,
        ideas_by_id=ideas_by_id,
        prompt_inputs=prompt_inputs,
        metadata=metadata,
    )


def _invoke_clustering_llm(ctx: ClusteringContext) -> str:
    llm = get_chat_llm_pro(model_kwargs={"temperature": 0.45, "max_tokens": 3200})
    chain = CLUSTERING_PROMPT | llm
    try:
        raw = chain.invoke(ctx.prompt_inputs)
    except Exception as exc:
        current_app.logger.error(
            "[Clustering] LLM failure for workshop %s: %s",
            ctx.workshop.id,
            exc,
            exc_info=True,
        )
        raise

    if hasattr(raw, "content") and isinstance(raw.content, str):  # type: ignore[attr-defined]
        return raw.content  # type: ignore[return-value]
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and "content" in raw:
        return str(raw["content"])
    return str(raw)


REQUIRED_CONTRACT_KEYS = {
    "title",
    "task_type",
    "task_description",
    "instructions",
    "task_duration",
    "clusters",
    "corrected",
}


def _parse_llm_contract(raw_text: str) -> Dict[str, Any]:
    block = extract_json_block(raw_text)
    if not block:
        raise ValueError("LLM output did not contain valid JSON")
    payload = json.loads(block)
    missing = REQUIRED_CONTRACT_KEYS - set(payload.keys())
    if missing:
        raise ValueError(f"LLM missing required keys: {sorted(missing)}")
    clusters = payload.get("clusters")
    if not isinstance(clusters, list):
        raise ValueError("LLM contract 'clusters' must be a list")
    payload.setdefault("corrected", [])
    payload.setdefault("duplicates", [])
    payload.setdefault("narration", "")
    payload.setdefault("tts_script", "")
    payload.setdefault("tts_read_time_seconds", 60)
    return payload

# ------------------------------- persistence --------------------------------

def _apply_corrections(
    ideas_by_id: Dict[int, BrainstormIdea],
    corrected: Sequence[Dict[str, Any]] | None,
) -> List[Dict[str, Any]]:
    """Persist corrected text and return normalized payload entries."""

    out: List[Dict[str, Any]] = []
    if not corrected:
        return out

    for rec in corrected:
        raw_id = rec.get("idea_id") if isinstance(rec, dict) else None
        if raw_id is None:
            continue
        try:
            idea_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        idea = ideas_by_id.get(idea_id)
        if not idea:
            continue
        text_raw = rec.get("corrected_text") if isinstance(rec, dict) else None
        corrected_text = str(text_raw or "").strip()
        if not corrected_text:
            continue

        idea.corrected_text = corrected_text
        participant_id = idea.participant_id
        participant_user_id = getattr(getattr(idea, "participant", None), "user_id", None)
        out.append(
            {
                "idea_id": idea_id,
                "corrected_text": corrected_text,
                "participant_id": participant_id,
                "user_id": participant_user_id,
            }
        )

    return out


def _apply_duplicates(
    ideas_by_id: Dict[int, BrainstormIdea],
    duplicates: Sequence[Dict[str, Any]] | None,
) -> List[Dict[str, Any]]:
    """Persist duplicate relationships and return normalized metadata."""

    mapping: List[Dict[str, Any]] = []
    if not duplicates:
        return mapping

    for grp in duplicates:
        if not isinstance(grp, dict):
            continue
        raw_canonical = grp.get("canonical_id")
        if raw_canonical is None:
            continue
        try:
            canonical_id = int(raw_canonical)
        except (TypeError, ValueError):
            continue
        if canonical_id not in ideas_by_id:
            continue

        dup_ids: List[int] = []
        for raw_dup in grp.get("duplicate_ids") or []:
            try:
                dup_id = int(raw_dup)
            except (TypeError, ValueError):
                continue
            if dup_id == canonical_id or dup_id not in ideas_by_id:
                continue
            idea_dup = ideas_by_id[dup_id]
            if hasattr(idea_dup, "duplicate_of_id"):
                idea_dup.duplicate_of_id = canonical_id
            dup_ids.append(dup_id)

        if not dup_ids:
            continue

        mapping.append(
            {
                "canonical_id": canonical_id,
                "duplicate_ids": dup_ids,
                "canonical_participant_id": ideas_by_id[canonical_id].participant_id,
                "duplicate_participant_ids": [ideas_by_id[d].participant_id for d in dup_ids],
            }
        )

    return mapping


def _persist_clusters_and_links(
    task: BrainstormTask,
    clusters_in: Sequence[Dict[str, Any]] | None,
    ctx: ClusteringContext,
) -> Tuple[List[Dict[str, Any]], Set[int]]:
    """Create clusters, link ideas, and emit normalized cluster payload."""

    processed: List[Dict[str, Any]] = []
    assigned: Set[int] = set()

    if not clusters_in:
        return processed, assigned

    for idx, cluster_data in enumerate(clusters_in):
        if not isinstance(cluster_data, dict):
            continue

        label_raw = cluster_data.get("label")
        name_raw = cluster_data.get("name")
        gist_raw = cluster_data.get("gist") or cluster_data.get("description")
        description_raw = cluster_data.get("description") or gist_raw

        label = str(label_raw or name_raw or f"Cluster {idx + 1}").strip()
        display_name = str(name_raw or label).strip() or label
        gist = str(gist_raw or "").strip()
        description = str(description_raw or gist).strip()

        raw_rep = cluster_data.get("representative_id")
        representative_id: Optional[int] = None
        try:
            representative_id = int(raw_rep) if raw_rep is not None else None
        except (TypeError, ValueError):
            representative_id = None

        examples_raw = cluster_data.get("examples") or []
        examples: List[int] = []
        for raw_example in examples_raw:
            try:
                examples.append(int(raw_example))
            except (TypeError, ValueError):
                continue

        idea_ids: List[int] = []
        if cluster_data.get("idea_ids"):
            for raw_id in cluster_data.get("idea_ids", []):
                try:
                    iid = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if iid in ctx.ideas_by_id:
                    idea_ids.append(iid)
        elif cluster_data.get("idea_indices"):
            for raw_idx in cluster_data.get("idea_indices", []):
                try:
                    idx_val = int(raw_idx)
                except (TypeError, ValueError):
                    continue
                mapped_id = ctx.index_to_id.get(idx_val)
                if mapped_id is not None:
                    idea_ids.append(mapped_id)

        if not idea_ids:
            continue

        cluster = IdeaCluster()
        cluster.task_id = task.id
        cluster.name = display_name
        cluster.description = description or gist or display_name
        if hasattr(cluster, "theme_gist"):
            setattr(cluster, "theme_gist", gist or description)
        if hasattr(cluster, "representative_idea_id") and representative_id in ctx.ideas_by_id:
            cluster.representative_idea_id = representative_id

        db.session.add(cluster)
        db.session.flush()

        linked_ids: List[int] = []
        for idea_id in idea_ids:
            idea = ctx.ideas_by_id.get(idea_id)
            if not idea:
                continue
            idea.cluster_id = cluster.id
            assigned.add(idea_id)
            linked_ids.append(idea_id)

        if representative_id not in linked_ids and linked_ids:
            representative_id = linked_ids[0]
        if not examples:
            examples = linked_ids[:3]

        processed.append(
            {
                "id": cluster.id,
                "label": label,
                "name": display_name,
                "gist": gist or description,
                "description": description or gist or display_name,
                "idea_ids": linked_ids,
                "idea_indices": [ctx.id_to_index[iid] for iid in linked_ids if iid in ctx.id_to_index],
                "representative_id": representative_id,
                "examples": examples[:3],
            }
        )

    return processed, assigned



# ----------------------------- public API -----------------------------------


def _reset_participant_dots(workshop: Workshop) -> Dict[int, int]:
    participants = (
        WorkshopParticipant.query
        .filter_by(workshop_id=workshop.id, status="accepted")
        .all()
    )
    default_dots = int(getattr(workshop, "dots_per_user", 5) or 5)
    mapping: Dict[int, int] = {}
    for participant in participants:
        participant.dots_remaining = default_dots
        mapping[participant.user_id] = default_dots
    return mapping


def _persist_clustering_outputs(
    ctx: ClusteringContext,
    contract: Dict[str, Any],
    source_task_id: int,
) -> Dict[str, Any]:
    ideas_by_id = ctx.ideas_by_id

    task = BrainstormTask()
    task.workshop_id = ctx.workshop.id
    task.task_type = "clustering_voting"

    title = str(contract.get("title") or "").strip() or "Vote on Idea Clusters"
    description = str(contract.get("task_description") or "").strip()
    instructions = str(contract.get("instructions") or "").strip()

    # Extract and validate task_duration (strict mode respects AI_STRICT_PHASES config)
    strict_mode = current_app.config.get('AI_STRICT_PHASES', False)
    
    if "task_duration" not in contract:
        error_msg = "[Clustering] LLM failed to provide task_duration"
        if strict_mode:
            current_app.logger.error(f"{error_msg} - refusing heuristic fallback (strict mode)")
            raise ValueError("LLM failed to generate task_duration - refusing heuristic fallback")
        else:
            current_app.logger.warning(f"{error_msg}, defaulting to 300 seconds")
            duration_val = 300
    else:
        try:
            raw_duration = contract.get("task_duration")
            if raw_duration is None:
                raise ValueError("task_duration is None")
            duration_val = int(float(raw_duration))
        except (TypeError, ValueError) as exc:
            error_msg = f"[Clustering] Invalid task_duration '{contract.get('task_duration')}'"
            if strict_mode:
                current_app.logger.error(f"{error_msg} - refusing heuristic fallback (strict mode)")
                raise ValueError(f"LLM provided invalid task_duration: {exc}")
            else:
                current_app.logger.warning(f"{error_msg}, defaulting to 300 seconds")
                duration_val = 300
    
    # Apply guardrails (log when clamping occurs)
    original_duration = duration_val
    duration_val = max(120, min(duration_val, 1800))
    if duration_val != original_duration:
        current_app.logger.info(
            f"[Clustering] Duration clamped from {original_duration}s to {duration_val}s "
            f"(valid range: 120-1800 seconds)"
        )

    try:
        tts_val = int(float(contract.get("tts_read_time_seconds", 90)))
    except (TypeError, ValueError):
        tts_val = max(45, duration_val // 2)
    tts_val = max(45, min(tts_val, 600))

    narration = str(contract.get("narration") or "").strip()
    tts_script = str(contract.get("tts_script") or "").strip()

    task.title = title
    task.description = description or None
    task.duration = duration_val
    task.status = "pending"
    db.session.add(task)
    db.session.flush()

    corrected_norm = _apply_corrections(ideas_by_id, contract.get("corrected"))
    duplicates_norm = _apply_duplicates(ideas_by_id, contract.get("duplicates"))
    clusters_norm, assigned_ids = _persist_clusters_and_links(task, contract.get("clusters"), ctx)

    all_ids = {idea.id for idea in ctx.ideas}
    orphan_ids = sorted(all_ids - assigned_ids)
    orphan_ideas = [
        {
            "idea_id": idea_id,
            "text": (
                ctx.ideas_by_id[idea_id].corrected_text
                or ctx.ideas_by_id[idea_id].content
                or ""
            ),
        }
        for idea_id in orphan_ids
    ]

    participants_dots = _reset_participant_dots(ctx.workshop)

    try:
        metadata_serialized = json.loads(json.dumps(ctx.metadata, default=str))
    except Exception:
        metadata_serialized = dict(ctx.metadata)

    payload: Dict[str, Any] = {
        "task_id": task.id,
        "task_type": "clustering_voting",
        "title": title,
        "task_description": description,
        "instructions": instructions,
        "task_duration": duration_val,
        "clusters": clusters_norm,
        "corrected": corrected_norm,
        "duplicates": duplicates_norm,
        "orphan_idea_ids": orphan_ids,
        "orphan_ideas": orphan_ideas,
        "narration": narration,
        "market_research_context": contract.get("market_research_context") or "",
        "market_target_segment": contract.get("market_target_segment") or "",
        "market_positioning": contract.get("market_positioning") or "",
        "go_to_market_strategy": contract.get("go_to_market_strategy") or "",
        "competitive_alternatives": contract.get("competitive_alternatives") or "",
        "rationale": contract.get("rationale") or "",
        "tts_script": tts_script,
        "tts_read_time_seconds": tts_val,
        "participants_dots": participants_dots,
        "phase_label": ctx.phase_label,
        "ideas": ctx.ideas_json,
        "metadata": metadata_serialized,
        "workshop_id": ctx.workshop.id,
        "source_task_id": source_task_id,
    }

    payload_str = json.dumps(payload, ensure_ascii=False)
    task.prompt = payload_str
    task.payload_json = payload_str

    _emit_clustering_events(
        ctx.workshop.id,
        task.id,
        corrected_norm,
        duplicates_norm,
        clusters_norm,
        orphan_ids,
    )

    current_app.logger.info(
        "[Clustering] Persisted task %s with %s clusters (%s ideas assigned, %s orphans)",
        task.id,
        len(clusters_norm),
        len(assigned_ids),
        len(orphan_ids),
    )

    return payload


def get_clustering_voting_payload(workshop_id: int, previous_task_id: int, phase_context: str):
    """Fetch brainstorming ideas, cluster them via LLM, persist results, return payload."""

    ideas = (
        BrainstormIdea.query
        .filter_by(task_id=previous_task_id)
        .order_by(BrainstormIdea.id.asc())
        .all()
    )
    if not ideas:
        return "No ideas found from the previous task to cluster.", 400

    try:
        ctx = _prepare_clustering_context(workshop_id, phase_context, ideas)
    except ValueError as exc:
        return str(exc), 404
    except Exception as exc:
        current_app.logger.error(
            "[Clustering] Failed to prepare context for workshop %s: %s",
            workshop_id,
            exc,
            exc_info=True,
        )
        return "Error preparing clustering inputs.", 500

    if not ctx.ideas:
        return "No ideas available for clustering.", 400

    try:
        raw_output = _invoke_clustering_llm(ctx)
    except Exception as exc:
        return f"Error generating clustering task: {exc}", 500

    try:
        contract = _parse_llm_contract(raw_output)
    except ValueError as exc:
        current_app.logger.error(
            "[Clustering] Invalid LLM contract for workshop %s: %s\nRaw: %s",
            workshop_id,
            exc,
            raw_output,
        )
        return f"Invalid clustering task format: {exc}", 500

    try:
        payload = _persist_clustering_outputs(ctx, contract, previous_task_id)
    except (ValueError, json.JSONDecodeError) as exc:
        current_app.logger.error(
            "[Clustering] Contract persistence error for workshop %s: %s",
            workshop_id,
            exc,
            exc_info=True,
        )
        db.session.rollback()
        return f"Invalid clustering task format: {exc}", 500
    except Exception as exc:
        current_app.logger.error(
            "[Clustering] Unexpected error creating clustering task for workshop %s: %s",
            workshop_id,
            exc,
            exc_info=True,
        )
        db.session.rollback()
        return "Server error creating clustering task.", 500

    return payload


def _emit_clustering_events(
    workshop_id: int,
    task_id: int,
    corrected: Sequence[Dict[str, Any]] | None,
    duplicates: Sequence[Dict[str, Any]] | None,
    clusters: Sequence[Dict[str, Any]],
    orphan_ids: Sequence[int],
) -> None:
    if corrected:
        _emit_event_safe(
            "ideas.corrected",
            {"workshop_id": workshop_id, "task_id": task_id, "items": list(corrected)},
        )
    if duplicates:
        _emit_event_safe(
            "ideas.duplicates_merged",
            {"workshop_id": workshop_id, "task_id": task_id, "items": list(duplicates)},
        )
    _emit_event_safe(
        "clusters.updated",
        {
            "workshop_id": workshop_id,
            "task_id": task_id,
            "clusters": list(clusters),
            "orphan_idea_ids": list(orphan_ids),
        },
    )


def _emit_event_safe(name: str, data: Dict[str, Any]) -> None:
    """
    No-op if your realtime layer isn't wired.
    Replace with your socket/queue (e.g., socketio.emit(name, data)).
    """
    try:
        emitter = getattr(current_app, "event_emitter", None)
        if callable(emitter):
            emitter(name, data)
    except Exception:
        current_app.logger.debug("[Voting] emit failed for %s", name, exc_info=True)