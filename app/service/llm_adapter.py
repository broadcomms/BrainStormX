"""Minimal LLM adapter and prompt templates for presentation artifacts.

This module defines strict JSON prompts and small helpers to map model responses
to the app's normalized contracts. It does not perform any network call by itself;
you must inject an `invoke` callable that takes a string prompt and returns a
string JSON response from your LLM provider.

Contracts produced by these helpers:
- prioritized: [{ id, title, score, cluster_id?, votes_norm?, scores? { impact?, effort? } }]
- action_items: [{ title, description, owner_participant_id?, status, due_date?, priority? }]
- milestones: [{ title, index, item_indices }]
- tts_script: Short facilitator narration string for the phase (returned with meta helpers)
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
try:  # Prefer typing_extensions for Pydantic v2 compatibility on Python < 3.12
    from typing_extensions import TypedDict, Literal  # type: ignore
except Exception:  # Fall back to typing if extensions not available
    from typing import TypedDict, Literal  # type: ignore
import json
from datetime import date

HAVE_PYDANTIC = False
try:
    # Pydantic v2 imports (optional)
    from pydantic import BaseModel
    from pydantic import field_validator
    HAVE_PYDANTIC = True
except Exception:  # optional dependency missing
    class _BaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

        def dict(self) -> Dict[str, Any]:
            return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}

    BaseModel = _BaseModel  # type: ignore[misc,assignment]
    ValidationError = Exception  # type: ignore[misc]
    # Provide lightweight fallbacks so static analyzers don't complain
    def Field(default: Any = None, **kwargs: Any) -> Any:  # type: ignore[func-returns-value]
        return default

    def validator(*args: Any, **kwargs: Any):  # type: ignore[misc]
        def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
            return fn
        return _wrap


# ------------ Validation helpers (manual fallback) ------------

def _clean_status(v: Any) -> str:
    allowed = {"todo", "in_progress", "blocked", "done"}
    s = str(v or "").strip().lower()
    return s if s in allowed else "todo"


def _clean_due_date(v: Any) -> Optional[str]:
    if not v:
        return None
    try:
        y, m, d = map(int, str(v).split('-'))
        date(y, m, d)
        return f"{y:04d}-{m:02d}-{d:02d}"
    except Exception:
        return None


def _validate_prioritized_item(it: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    out["id"] = it.get("id") if isinstance(it.get("id"), int) else None
    out["title"] = it.get("title") if isinstance(it.get("title"), str) else None
    # coerce score to float if possible
    sc = it.get("score")
    try:
        out["score"] = float(sc) if sc is not None else None
    except Exception:
        out["score"] = None
    out["cluster_id"] = it.get("cluster_id") if isinstance(it.get("cluster_id"), int) else None
    # votes_norm may come as int or float
    vn = it.get("votes_norm")
    try:
        out["votes_norm"] = float(vn) if vn is not None else None
    except Exception:
        out["votes_norm"] = None
    scores = it.get("scores") if isinstance(it.get("scores"), dict) else None
    if scores is not None:
        cleaned: Dict[str, Any] = {}
        for k in ("impact", "effort"):
            try:
                val = scores.get(k)
                cleaned[k] = float(val) if val is not None else None
            except Exception:
                cleaned[k] = None
        out["scores"] = cleaned
    else:
        out["scores"] = None
    return out


def _validate_action_item(it: Dict[str, Any]) -> Dict[str, Any]:
    title = it.get("title")
    if not isinstance(title, str):
        title = str(title or "").strip()
    description = it.get("description", "")
    if not isinstance(description, str):
        description = str(description or "")
    owner = it.get("owner_participant_id")
    owner_id = owner if isinstance(owner, int) else None
    status = _clean_status(it.get("status"))
    due_date = _clean_due_date(it.get("due_date"))
    pr = it.get("priority")
    try:
        priority = int(pr) if pr is not None else None
    except Exception:
        priority = None
    return {
        "title": title,
        "description": description,
        "owner_participant_id": owner_id,
        "status": status,
        "due_date": due_date,
        "priority": priority,
    }


def _validate_milestone(it: Dict[str, Any]) -> Dict[str, Any]:
    title = it.get("title")
    if not isinstance(title, str):
        title = str(title or "").strip()
    idx = it.get("index")
    try:
        index = int(idx) if idx is not None else None
    except Exception:
        index = None
    items = it.get("item_indices")
    if not isinstance(items, list):
        items = []
    cleaned_items: List[int] = []
    for v in items:
        try:
            cleaned_items.append(int(v))
        except Exception:
            continue
    return {"title": title, "index": index, "item_indices": cleaned_items}


# ------------ Prompt builders (strict JSON, escaped braces) ------------

def make_prioritization_prompt(*, objective: str, clusters: List[Dict[str, Any]], vote_counts: Dict[int, int], candidates: List[Dict[str, Any]]) -> str:
        return (
                f"""
You are a product strategy analyst. Prioritize candidate ideas using workshop context.

Inputs:
- Objective: {json.dumps(objective or '')}
- Cluster themes (id -> name/summary): {json.dumps(clusters or [])}
- Vote distribution per cluster (cluster_id -> total_votes): {json.dumps(vote_counts or {})}
- Candidates (list of objects with id, title, cluster_id, votes_norm): {json.dumps(candidates or [])}

Scoring Guidance:
- Consider objective fit, user value/impact, and feasibility signal if present; if missing, infer neutrals.
- Use a composite score 0.0–1.0 (float). You MAY add optional scores.impact and scores.effort on a 0–100 scale,
    or a 1–5 scale (the app will normalize). If unknown, omit these fields.
- Preserve candidate id and cluster_id, and prefer the original title unchanged.

Output STRICT JSON ONLY with this shape:
{{
    "prioritized": [
        {{
            "id": <int>,
            "title": <str>,
            "score": <float>,
            "cluster_id": <int|null>,
            "votes_norm": <float|null>,
            "scores": {{"impact": <number?>, "effort": <number?>}}
        }}
    ],
    "tts_script": <str>,
    "rationale": <str?>
}}
"""
        ).strip()


def make_action_plan_prompt(*, prioritized: List[Dict[str, Any]], participants: List[Dict[str, Any]]) -> str:
        return (
                f"""
You are a delivery lead. Convert the prioritized shortlist into a concise action plan.

Inputs:
- Prioritized shortlist: {json.dumps(prioritized or [])}
- Participants (for owner suggestions; list of {{participant_id, first_name, last_name, email}}): {json.dumps(participants or [])}

Guidance:
- Create 5–12 actionable items. Titles should be clear and outcome-driven.
- Write a concise description (1–2 short sentences, no Markdown).
- Suggest an owner (by participant_id), if a reasonable match exists; otherwise leave null.
- Provide an ISO date due_date when feasible (within 3–12 weeks from now) and an integer priority (1 = highest).
- Status is one of ["todo", "in_progress", "blocked", "done"]. Default to "todo" if unsure.

Output STRICT JSON ONLY with this shape:
{{
    "action_items": [
        {{
            "title": <str>,
            "description": <str>,
            "owner_participant_id": <int|null>,
            "status": <"todo"|"in_progress"|"blocked"|"done">,
            "due_date": <"YYYY-MM-DD"|null>,
            "priority": <int|null>
        }}
    ],
    "milestones": [
        {{ "title": <str>, "index": <int>, "item_indices": [<int>, ...] }}
    ],
    "tts_script": <str>
}}
"""
        ).strip()


def make_milestones_prompt(*, action_items: List[Dict[str, Any]]) -> str:
        return (
                f"""
You are a program manager. Group action items into 3–5 sequential milestones.

Inputs:
- Action items (array of {{title, description, priority?}}): {json.dumps(action_items or [])}

Guidance:
- Balance milestones by scope and logical dependencies.
- Each milestone has a title (e.g., "Kickoff & Foundations").
- Group by position (1-based indices of the array you received); maintain order.

Output STRICT JSON ONLY with this shape:
{{
    "milestones": [
        {{ "title": <str>, "index": <int>, "item_indices": [<int>, ...] }}
    ]
}}
"""
        ).strip()


# ------------ Adapter helpers (no network) ------------

def _safe_json_loads(s: str) -> Dict[str, Any]:
    try:
        return json.loads(s)
    except Exception:
        return {}


def build_prioritized_from_llm(
    *,
    objective: str,
    clusters: List[Dict[str, Any]],
    vote_counts: Dict[int, int],
    candidates: List[Dict[str, Any]],
    invoke: Callable[[str], str],
) -> List[Dict[str, Any]]:
    prompt = make_prioritization_prompt(objective=objective, clusters=clusters, vote_counts=vote_counts, candidates=candidates)
    raw = invoke(prompt)
    data = _safe_json_loads(raw or "{}")
    out = []
    # Prefer manual normalization first; we'll validate strictly after
    for it in (data.get("prioritized") or []):
        out.append(_validate_prioritized_item(it))
    # Strict runtime enforcement with Pydantic v2 TypeAdapter when available
    if HAVE_PYDANTIC:
        try:
            from pydantic import TypeAdapter

            class _ScoresTD(TypedDict, total=False):
                impact: float
                effort: float

            class _PrioritizedTD(TypedDict, total=False):
                id: int
                title: str
                score: float
                cluster_id: int
                votes_norm: float
                scores: Optional[_ScoresTD]

            TypeAdapter(List[_PrioritizedTD]).validate_python(out)
        except Exception as e:
            raise ValueError(f"LLM prioritized output validation failed: {e}")
    return out


def build_prioritized_with_meta_from_llm(
    *,
    objective: str,
    clusters: List[Dict[str, Any]],
    vote_counts: Dict[int, int],
    candidates: List[Dict[str, Any]],
    invoke: Callable[[str], str],
) -> Dict[str, Any]:
    """Return { prioritized: [...], tts_script: str, rationale?: str } strictly parsed."""
    prompt = make_prioritization_prompt(objective=objective, clusters=clusters, vote_counts=vote_counts, candidates=candidates)
    raw = invoke(prompt)
    data = _safe_json_loads(raw or "{}")
    prioritized: List[Dict[str, Any]] = []
    for it in (data.get("prioritized") or []):
        prioritized.append(_validate_prioritized_item(it))
    tts_script = data.get("tts_script") if isinstance(data.get("tts_script"), str) else ""
    rationale = data.get("rationale") if isinstance(data.get("rationale"), str) else None
    if HAVE_PYDANTIC:
        try:
            from pydantic import TypeAdapter

            class _ScoresTD(TypedDict, total=False):
                impact: float
                effort: float

            class _PrioritizedTD(TypedDict, total=False):
                id: int
                title: str
                score: float
                cluster_id: int
                votes_norm: float
                scores: Optional[_ScoresTD]

            TypeAdapter(List[_PrioritizedTD]).validate_python(prioritized)
            if not isinstance(tts_script, str) or not tts_script.strip():
                raise ValueError("tts_script missing or empty")
        except Exception as e:
            raise ValueError(f"LLM prioritized(meta) validation failed: {e}")
    return {"prioritized": prioritized, "tts_script": tts_script, **({"rationale": rationale} if rationale else {})}


def build_action_plan_from_llm(
    *,
    prioritized: List[Dict[str, Any]],
    participants: List[Dict[str, Any]],
    invoke: Callable[[str], str],
) -> List[Dict[str, Any]]:
    prompt = make_action_plan_prompt(prioritized=prioritized, participants=participants)
    raw = invoke(prompt)
    data = _safe_json_loads(raw or "{}")
    out = []
    # Prefer manual normalization first; we'll validate strictly after
    for it in (data.get("action_items") or []):
        out.append(_validate_action_item(it))
    if HAVE_PYDANTIC:
        try:
            from pydantic import TypeAdapter

            class _ActionItemTD(TypedDict, total=False):
                title: str
                description: str
                owner_participant_id: Optional[int]
                status: Literal["todo", "in_progress", "blocked", "done"]
                due_date: Optional[str]
                priority: Optional[int]

            TypeAdapter(List[_ActionItemTD]).validate_python(out)
        except Exception as e:
            raise ValueError(f"LLM action_items output validation failed: {e}")
    return out


def build_action_plan_with_meta_from_llm(
    *,
    prioritized: List[Dict[str, Any]],
    participants: List[Dict[str, Any]],
    invoke: Callable[[str], str],
) -> Dict[str, Any]:
    """Return { action_items: [...], milestones: [...], tts_script: str } strictly parsed."""
    prompt = make_action_plan_prompt(prioritized=prioritized, participants=participants)
    raw = invoke(prompt)
    data = _safe_json_loads(raw or "{}")
    actions: List[Dict[str, Any]] = []
    for it in (data.get("action_items") or []):
        actions.append(_validate_action_item(it))
    # milestones may be provided inline by the LLM in this meta variant
    ms_out: List[Dict[str, Any]] = []
    for it in (data.get("milestones") or []):
        ms_out.append(_validate_milestone(it))
    tts_script = data.get("tts_script") if isinstance(data.get("tts_script"), str) else ""
    if HAVE_PYDANTIC:
        try:
            from pydantic import TypeAdapter

            class _ActionItemTD(TypedDict, total=False):
                title: str
                description: str
                owner_participant_id: Optional[int]
                status: Literal["todo", "in_progress", "blocked", "done"]
                due_date: Optional[str]
                priority: Optional[int]

            class _MilestoneTD(TypedDict):
                title: str
                index: int
                item_indices: List[int]

            TypeAdapter(List[_ActionItemTD]).validate_python(actions)
            # milestones may be empty but must be list if present
            if data.get("milestones") is not None:
                TypeAdapter(List[_MilestoneTD]).validate_python(ms_out)
            if not isinstance(tts_script, str) or not tts_script.strip():
                raise ValueError("tts_script missing or empty")
        except Exception as e:
            raise ValueError(f"LLM action plan(meta) validation failed: {e}")
    return {"action_items": actions, "milestones": ms_out, "tts_script": tts_script}


def build_milestones_from_llm(
    *,
    action_items: List[Dict[str, Any]],
    invoke: Callable[[str], str],
) -> List[Dict[str, Any]]:
    prompt = make_milestones_prompt(action_items=action_items)
    raw = invoke(prompt)
    data = _safe_json_loads(raw or "{}")
    out = []
    # Prefer manual normalization first; we'll validate strictly after
    for it in (data.get("milestones") or []):
        out.append(_validate_milestone(it))
    if HAVE_PYDANTIC:
        try:
            from pydantic import TypeAdapter

            class _MilestoneTD(TypedDict):
                title: str
                index: int
                item_indices: List[int]

            TypeAdapter(List[_MilestoneTD]).validate_python(out)
        except Exception as e:
            raise ValueError(f"LLM milestones output validation failed: {e}")
    return out
