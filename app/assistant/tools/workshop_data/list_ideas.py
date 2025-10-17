from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import func

from app.assistant.tools.base import BaseTool
from app.assistant.tools.types import ToolResult, ToolSchema
from app.models import BrainstormIdea, BrainstormTask, IdeaCluster, IdeaVote, db


class ListIdeasTool(BaseTool):
    """List individual ideas captured in the current workshop, optionally filtered by cluster.

    By default, targets the latest clustering_voting task's clusters to ensure ideas are grouped.
    If cluster_id is provided, returns only that cluster's ideas.
    """

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_ideas",
            namespace="workshop",
            description=(
                "List individual ideas captured during brainstorming or discussion phases. "
                "During brainstorming/warm-up/discussion: returns all ideas from current task (unclustered). "
                "After clustering: returns ideas grouped by clusters. "
                "Optionally filter by cluster_id to get ideas from a specific cluster."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    # Accept auto-injected user_id for strict schema environments
                    "user_id": {"type": "integer", "minimum": 1},
                    "cluster_id": {"type": "integer", "minimum": 1},
                    "limit_per_cluster": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
                    "order": {"type": "string", "enum": ["asc", "desc"], "default": "asc"},
                },
                "required": ["workshop_id"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "clusters": {"type": "array"},
                    "ideas": {"type": "array"},
                },
            },
            requires_auth=True,
            requires_workshop=True,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop_id = params.get("workshop_id")
        workshop = self.ensure_workshop(workshop_id)

        cluster_id = params.get("cluster_id")
        try:
            limit_per_cluster = int(params.get("limit_per_cluster") or 200)
        except Exception:
            limit_per_cluster = 200
        limit_per_cluster = max(1, min(limit_per_cluster, 500))
        order = (params.get("order") or "asc").strip().lower()
        order_by = BrainstormIdea.id.asc() if order == "asc" else BrainstormIdea.id.desc()

        # Check if we're in brainstorming phase (before clustering)
        current_task = workshop.current_task if hasattr(workshop, 'current_task') else None
        if current_task and current_task.task_type in ('brainstorming', 'warm-up', 'discussion'):
            # Return ideas from current brainstorming task (no clusters yet)
            ideas_q = (
                BrainstormIdea.query
                .filter(BrainstormIdea.task_id == current_task.id)
                .order_by(order_by)
                .limit(limit_per_cluster)
            )
            ideas: List[BrainstormIdea] = ideas_q.all()
            
            items = [
                {
                    "idea_id": i.id,
                    "text": (i.corrected_text or i.content or "").strip(),
                    "timestamp": i.timestamp.isoformat() if getattr(i, "timestamp", None) else None,
                    "source": getattr(i, "source", "human"),
                    "participant_id": i.participant_id,
                }
                for i in ideas
            ]
            
            return ToolResult(
                success=True, 
                data={
                    "ideas": items, 
                    "clusters": [],
                    "total_count": len(items),
                    "phase": "brainstorming"
                },
                metadata={"phase": current_task.task_type, "task_id": current_task.id}
            )

        # Use the most recent clustering_voting task so we group ideas by the latest clusters
        task: Optional[BrainstormTask] = (
            BrainstormTask.query
            .filter_by(workshop_id=workshop_id, task_type="clustering_voting")
            .order_by(BrainstormTask.created_at.desc())
            .first()
        )
        if not task:
            return ToolResult(success=True, data={"clusters": [], "ideas": [], "total_count": 0}, metadata={"reason": "no_clustering_task"})

        if cluster_id:
            # Validate cluster belongs to the chosen task
            cluster: Optional[IdeaCluster] = (
                IdeaCluster.query.filter_by(id=cluster_id, task_id=task.id).first()
            )
            if not cluster:
                return ToolResult(success=False, error="Cluster not found for latest clustering task")

            ideas_q = (
                BrainstormIdea.query
                .filter(BrainstormIdea.cluster_id == cluster.id)
                .order_by(order_by)
                .limit(limit_per_cluster)
            )
            ideas: List[BrainstormIdea] = ideas_q.all()

            items = [
                {
                    "idea_id": i.id,
                    "text": (i.corrected_text or i.content or "").strip(),
                    "timestamp": i.timestamp.isoformat() if getattr(i, "timestamp", None) else None,
                }
                for i in ideas
            ]
            cluster_out = {
                "cluster_id": cluster.id,
                "name": cluster.name,
                "ideas_count": cluster.ideas.count(),  # type: ignore[attr-defined]
            }
            return ToolResult(success=True, data={"ideas": items, "clusters": [cluster_out]})

        # No specific cluster: return grouped by cluster with capped items
        clusters: List[IdeaCluster] = task.clusters.order_by(IdeaCluster.id.asc()).all()
        grouped: List[Dict[str, Any]] = []
        for c in clusters:
            ideas_q = c.ideas.order_by(order_by).limit(limit_per_cluster)  # type: ignore[attr-defined]
            ideas: List[BrainstormIdea] = ideas_q.all()  # type: ignore[assignment]
            grouped.append(
                {
                    "cluster_id": c.id,
                    "name": c.name,
                    "ideas_count": c.ideas.count(),  # type: ignore[attr-defined]
                    "ideas": [
                        {
                            "idea_id": i.id,
                            "text": (i.corrected_text or i.content or "").strip(),
                            "timestamp": i.timestamp.isoformat() if getattr(i, "timestamp", None) else None,
                        }
                        for i in ideas
                    ],
                }
            )

        return ToolResult(success=True, data={"clusters": grouped, "ideas": []})
