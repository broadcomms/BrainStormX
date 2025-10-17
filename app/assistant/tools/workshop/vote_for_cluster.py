from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import func

from app.assistant.tools.base import BaseTool, ToolExecutionError
from app.assistant.tools.database.cast_vote import CastVoteTool, MAX_DOTS_PER_VOTE
from app.assistant.tools.types import ToolResult, ToolSchema
from app.models import BrainstormTask, IdeaCluster, WorkshopParticipant, db


class VoteForClusterTool(BaseTool):
    """Cast a vote for a named cluster, resolving the cluster and participant automatically."""

    def __init__(self) -> None:
        self._delegate = CastVoteTool()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="vote_for_cluster",
            namespace="workshop",
            description=(
                "Cast a vote for an idea cluster by name. Automatically resolves the cluster "
                "and the current participant, then records the vote using the workshop's voting quotas."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "cluster_name": {
                        "type": "string",
                        "description": "Name (or partial name) of the cluster to vote for",
                        "minLength": 1,
                    },
                    "cluster_id": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional cluster ID to disambiguate when multiple clusters share a similar name.",
                    },
                    "vote_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_DOTS_PER_VOTE,
                        "description": "Number of dots to allocate (defaults to 1).",
                    },
                    # Auto-injected when authenticated via ToolRegistry
                    "user_id": {"type": "integer", "minimum": 1},
                },
                "required": ["workshop_id"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "cluster_id": {"type": "integer"},
                    "cluster_name": {"type": "string"},
                    "vote_points": {"type": "integer"},
                    "participant_votes": {"type": "integer"},
                    "dots_remaining": {"type": "integer"},
                },
            },
            requires_auth=True,
            requires_workshop=True,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop = self.ensure_workshop(params.get("workshop_id"))

        user_id = params.get("user_id")
        if not user_id:
            return ToolResult(success=False, error="user_id is required to cast a vote")

        participant = (
            WorkshopParticipant.query
            .filter_by(workshop_id=workshop.id, user_id=user_id)
            .first()
        )
        if not participant:
            return ToolResult(success=False, error="You must be a workshop participant to vote")

        vote_count = params.get("vote_count")
        try:
            vote_count_int = int(vote_count) if vote_count is not None else 1
        except (TypeError, ValueError):
            return ToolResult(success=False, error="vote_count must be an integer")
        if vote_count_int < 1:
            return ToolResult(success=False, error="vote_count must be at least 1")
        if vote_count_int > MAX_DOTS_PER_VOTE:
            vote_count_int = MAX_DOTS_PER_VOTE

        try:
            cluster = self._resolve_cluster(workshop.id, params)
        except ToolExecutionError as exc:
            return ToolResult(success=False, error=str(exc))

        if cluster is None:
            query_name = params.get("cluster_name")
            return ToolResult(
                success=False,
                error=f"Could not find a cluster matching '{query_name}'.",
            )

        delegate_result = self._delegate.execute(
            {
                "workshop_id": workshop.id,
                "cluster_id": cluster.id,
                "participant_id": participant.id,
                "vote_count": vote_count_int,
            }
        )

        if not delegate_result.success:
            return ToolResult(success=False, error=delegate_result.error)

        data = delegate_result.data or {}
        response_payload = {
            "cluster_id": cluster.id,
            "cluster_name": cluster.name,
            "vote_points": data.get("total_votes"),
            "participant_votes": data.get("participant_votes"),
            "dots_remaining": data.get("dots_remaining"),
        }

        return ToolResult(
            success=True,
            data=response_payload,
            rows_affected=delegate_result.rows_affected,
            metadata=delegate_result.metadata,
        )

    # ------------------------------------------------------------------
    def _resolve_cluster(self, workshop_id: int, params: Dict[str, Any]) -> Optional[IdeaCluster]:
        cluster_id = params.get("cluster_id")
        if cluster_id:
            cluster = db.session.get(IdeaCluster, cluster_id)
            if not self._cluster_matches_workshop(cluster, workshop_id):
                raise ToolExecutionError("Cluster not found for this workshop")
            return cluster

        name = (params.get("cluster_name") or "").strip()
        if not name:
            raise ToolExecutionError("cluster_name is required when cluster_id is not provided")

        base_query = (
            db.session.query(IdeaCluster)
            .join(BrainstormTask)
            .filter(BrainstormTask.workshop_id == workshop_id)
        )

        # Prefer exact (case-insensitive) match first
        exact_match = (
            base_query
            .filter(func.lower(IdeaCluster.name) == name.lower())
            .order_by(IdeaCluster.id.asc())
            .first()
        )
        if exact_match:
            return exact_match

        # Fallback to partial match
        partial_match = (
            base_query
            .filter(IdeaCluster.name.ilike(f"%{name}%"))
            .order_by(IdeaCluster.id.asc())
            .first()
        )
        return partial_match

    @staticmethod
    def _cluster_matches_workshop(cluster: Optional[IdeaCluster], workshop_id: int) -> bool:
        if not cluster or cluster.task is None:
            return False
        return cluster.task.workshop_id == workshop_id
