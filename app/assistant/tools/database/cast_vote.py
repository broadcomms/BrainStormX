from __future__ import annotations

import time
from typing import Any, Dict, Optional

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.assistant.tools import BaseTool, ToolResult, ToolSchema
from app.assistant.tools.base import ToolExecutionError
from app.assistant.tools.notifier.catalog import EventType
from app.assistant.tools.telemetry import log_tool_event
from app.models import IdeaCluster, IdeaVote, WorkshopParticipant, db

MAX_DOTS_PER_VOTE = 5
MAX_RETRY_ATTEMPTS = 8
RETRY_BACKOFF_SECONDS = 0.02


class CastVoteTool(BaseTool):
    """Record dot-voting results with transactional safety and quotas."""

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="cast_vote",
            namespace="database",
            description="Cast or update a vote allocation for a cluster.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "cluster_id": {"type": "integer", "minimum": 1},
                    "participant_id": {"type": "integer", "minimum": 1},
                    "vote_count": {"type": "integer", "minimum": 1, "maximum": MAX_DOTS_PER_VOTE},
                },
                "required": ["workshop_id", "cluster_id", "participant_id"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "total_votes": {"type": "integer"},
                    "participant_votes": {"type": "integer"},
                    "dots_remaining": {"type": "integer"},
                },
            },
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop = self.ensure_workshop(params.get("workshop_id"))
        vote_count = min(MAX_DOTS_PER_VOTE, params.get("vote_count", 1))

        last_error: Optional[str] = None
        for attempt in range(MAX_RETRY_ATTEMPTS):
            participant = self._lock_participant(workshop.id, params["participant_id"])
            try:
                with db.session.begin_nested():
                    cluster = self._lock_cluster(params["cluster_id"])
                    if cluster.task is None or cluster.task.workshop_id != workshop.id:
                        raise ToolExecutionError("Cluster not part of workshop")

                    existing_vote = (
                        db.session.execute(
                            select(IdeaVote)
                            .where(
                                and_(
                                    IdeaVote.cluster_id == cluster.id,
                                    IdeaVote.participant_id == participant.id,
                                )
                            )
                            .with_for_update()
                        ).scalar_one_or_none()
                    )

                    delta = vote_count if existing_vote is None else vote_count - existing_vote.dots_used

                    if delta > 0 and delta > participant.dots_remaining:
                        raise ToolExecutionError("Not enough dots remaining")

                    if existing_vote:
                        existing_vote.dots_used = vote_count
                    else:
                        db.session.add(
                            IdeaVote(
                                cluster_id=cluster.id,
                                participant_id=participant.id,
                                dots_used=vote_count,
                            )
                        )

                    participant.dots_remaining -= delta

                    total_votes = (
                        db.session.execute(
                            select(db.func.sum(IdeaVote.dots_used)).where(IdeaVote.cluster_id == cluster.id)
                        ).scalar()
                        or 0
                    )

                db.session.commit()
            except ToolExecutionError as exc:
                db.session.rollback()
                return ToolResult(success=False, error=str(exc))
            except IntegrityError as exc:  # pragma: no cover - race protection
                db.session.rollback()
                return ToolResult(success=False, error="Vote constraint violation")
            except OperationalError as exc:
                db.session.rollback()
                last_error = str(exc)
                if attempt < MAX_RETRY_ATTEMPTS - 1:
                    time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                return ToolResult(success=False, error="Database lock, please retry")
            except Exception as exc:  # pragma: no cover
                db.session.rollback()
                return ToolResult(success=False, error=str(exc))
            else:
                break
        else:  # pragma: no cover - defensive, shouldn't hit due to return above
            return ToolResult(success=False, error=last_error or "Unknown error")

        result = ToolResult(
            success=True,
            data={
                "total_votes": total_votes,
                "participant_votes": vote_count,
                "dots_remaining": participant.dots_remaining,
            },
            rows_affected=1,
            metadata={
                "notifier": {
                    "event_type": EventType.VOTE_CAST.value,
                    "payload": {
                        "cluster_id": cluster.id,
                        "total_votes": total_votes,
                        "user_id": participant.user_id,
                    },
                }
            },
        )
        log_tool_event(
            "vote_cast",
            {
                "workshop_id": workshop.id,
                "cluster_id": cluster.id,
                "participant_id": participant.id,
                "remaining_dots": participant.dots_remaining,
            },
        )
        return result

    # ------------------------------------------------------------------
    def _lock_cluster(self, cluster_id: int) -> IdeaCluster:
        cluster = (
            db.session.execute(
                select(IdeaCluster)
                .where(IdeaCluster.id == cluster_id)
                .with_for_update()
            ).scalar_one_or_none()
        )
        if not cluster:
            raise ToolExecutionError("Cluster not found")
        return cluster

    def _lock_participant(self, workshop_id: int, participant_id: int) -> WorkshopParticipant:
        participant = (
            db.session.execute(
                select(WorkshopParticipant)
                .where(
                    and_(
                        WorkshopParticipant.id == participant_id,
                        WorkshopParticipant.workshop_id == workshop_id,
                    )
                )
                .with_for_update()
            ).scalar_one_or_none()
        )
        if not participant:
            raise ToolExecutionError("Participant not found")
        return participant
