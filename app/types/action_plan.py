from __future__ import annotations

from typing import TypedDict


class _ActionPlanRequired(TypedDict):
    phase: str
    description: str


class ActionPlanItem(_ActionPlanRequired, total=False):
    owner: str
    owner_role: str
    duration_minutes: int
    dependencies: list[str]
    status: str
    notes: str
    resources: list[str]


ActionPlan = list[ActionPlanItem]
