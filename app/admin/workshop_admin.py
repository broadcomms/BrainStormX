"""Workshop administration helpers for the back-office."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any, Dict, List, Tuple, TypedDict, cast

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import (
    BrainstormIdea,
    ChatMessage,
    IdeaCluster,
    IdeaVote,
    Workshop,
    WorkshopParticipant,
    WorkshopDocument,
)


class WorkshopSnapshot(TypedDict):
    workshop: Workshop
    participants: List[WorkshopParticipant]
    ideas: List[BrainstormIdea]
    clusters: List[Tuple[IdeaCluster, int]]
    messages: List[ChatMessage]
    documents: List[WorkshopDocument]


class WorkshopAdmin:
    """Aggregate analytics and exports for workshops."""

    @staticmethod
    def workshop_status_counts() -> List[Tuple[str, int]]:
        return (
            db.session.query(Workshop.status, func.count(Workshop.id))
            .group_by(Workshop.status)
            .all()
        )

    @staticmethod
    def average_participants() -> float:
        subquery = (
            db.session.query(
                WorkshopParticipant.workshop_id,
                func.count(WorkshopParticipant.id).label("participant_count"),
            )
            .group_by(WorkshopParticipant.workshop_id)
            .subquery()
        )
        result = db.session.query(func.avg(subquery.c.participant_count)).scalar()
        return float(result or 0.0)

    @staticmethod
    def most_active_workshops(limit: int = 10) -> List[Tuple[str, int]]:
        return (
            db.session.query(Workshop.title, func.count(ChatMessage.id).label("message_count"))
            .join(ChatMessage, Workshop.id == ChatMessage.workshop_id)
            .group_by(Workshop.id)
            .order_by(func.count(ChatMessage.id).desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_workshop_analytics() -> Dict[str, object]:
        return {
            "status_counts": WorkshopAdmin.workshop_status_counts(),
            "average_participants": WorkshopAdmin.average_participants(),
            "ideas_generated": int(db.session.query(func.count(BrainstormIdea.id)).scalar() or 0),
            "most_active_workshops": WorkshopAdmin.most_active_workshops(),
        }

    @staticmethod
    def workshop_snapshot(workshop: Workshop) -> WorkshopSnapshot:
        participants = (
            WorkshopParticipant.query.filter_by(workshop_id=workshop.id)
            .order_by(WorkshopParticipant.joined_timestamp.asc())
            .all()
        )
        ideas = BrainstormIdea.query.filter(
            BrainstormIdea.task.has(workshop_id=workshop.id)
        ).all()
        cluster_rows = (
            db.session.query(IdeaCluster, func.count(IdeaVote.id).label("votes"))
            .outerjoin(IdeaVote)
            .filter(IdeaCluster.task.has(workshop_id=workshop.id))
            .group_by(IdeaCluster.id)
            .all()
        )
        clusters: List[Tuple[IdeaCluster, int]] = [
            (cluster, int(votes or 0))
            for cluster, votes in cast(List[Tuple[IdeaCluster, int | None]], cluster_rows)
        ]
        messages = (
            ChatMessage.query.filter_by(workshop_id=workshop.id)
            .order_by(ChatMessage.timestamp.asc())
            .limit(50)
            .all()
        )
        documents = (
            WorkshopDocument.query.options(selectinload(WorkshopDocument.document))  # type: ignore[arg-type]
            .filter_by(workshop_id=workshop.id)
            .order_by(WorkshopDocument.added_at.desc(), WorkshopDocument.id.desc())
            .all()
        )

        snapshot: WorkshopSnapshot = {
            "workshop": workshop,
            "participants": participants,
            "ideas": ideas,
            "clusters": clusters,
            "messages": messages,
            "documents": documents,
        }
        return snapshot

    @staticmethod
    def export_workshop_data(workshop: Workshop) -> Dict[str, Any]:
        snapshot = WorkshopAdmin.workshop_snapshot(workshop)

        participants = []
        for participant in snapshot["participants"]:
            user = participant.user
            participants.append(
                {
                    "id": participant.id,
                    "user_id": participant.user_id,
                    "display_name": user.display_name if user else None,
                    "email": user.email if user else None,
                    "role": participant.role,
                    "status": participant.status,
                    "joined_at": participant.joined_timestamp.isoformat() if participant.joined_timestamp else None,
                }
            )

        ideas = []
        for idea in snapshot["ideas"]:
            participant = idea.participant
            participant_user = participant.user if participant else None
            ideas.append(
                {
                    "id": idea.id,
                    "content": idea.content,
                    "participant_id": idea.participant_id,
                    "participant_name": participant_user.display_name if participant_user else None,
                    "timestamp": idea.timestamp.isoformat() if idea.timestamp else None,
                    "cluster_id": idea.cluster_id,
                }
            )

        clusters = []
        for cluster, votes in snapshot["clusters"]:
            clusters.append(
                {
                    "id": cluster.id,
                    "name": cluster.name,
                    "description": cluster.description,
                    "votes": votes,
                    "idea_ids": [idea.id for idea in cluster.ideas.all()],
                }
            )

        messages = [
            {
                "id": message.id,
                "user_id": message.user_id,
                "username": message.username,
                "message": message.message,
                "timestamp": message.timestamp.isoformat() if message.timestamp else None,
                "message_type": getattr(message, "message_type", "user"),
            }
            for message in snapshot["messages"]
        ]

        documents = []
        for link in snapshot["documents"]:
            doc = link.document
            documents.append(
                {
                    "link_id": link.id,
                    "document_id": link.document_id,
                    "title": getattr(doc, "title", None),
                    "file_name": getattr(doc, "file_name", None),
                    "file_path": getattr(doc, "file_path", None),
                    "description": getattr(doc, "description", None),
                    "file_size": getattr(doc, "file_size", None),
                    "uploaded_at": doc.uploaded_at.isoformat() if getattr(doc, "uploaded_at", None) else None,
                    "added_at": link.added_at.isoformat() if link.added_at else None,
                    "uploaded_by_id": getattr(doc, "uploaded_by_id", None),
                    "uploader_name": doc.uploader.display_name if getattr(doc, "uploader", None) else None,
                }
            )

        workshop_data = {
            "id": workshop.id,
            "title": workshop.title,
            "objective": workshop.objective,
            "status": workshop.status,
            "date_time": workshop.date_time.isoformat() if workshop.date_time else None,
            "duration": workshop.duration,
            "workspace_id": workshop.workspace_id,
            "created_by": workshop.created_by_id,
            "created_at": workshop.created_at.isoformat() if workshop.created_at else None,
            "updated_at": workshop.updated_at.isoformat() if workshop.updated_at else None,
        }

        return {
            "workshop": workshop_data,
            "participants": participants,
            "ideas": ideas,
            "clusters": clusters,
            "messages": messages,
            "documents": documents,
        }

    @staticmethod
    def export_workshop_csv(workshop: Workshop) -> str:
        data = WorkshopAdmin.export_workshop_data(workshop)
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        writer.writerow(["section", "id", "name", "details", "extra"])

        for participant in data["participants"]:
            writer.writerow(
                [
                    "participant",
                    participant["id"],
                    participant["display_name"] or "Guest",
                    participant.get("email") or "",
                    f"role={participant['role']}; status={participant['status']}",
                ]
            )

        for idea in data["ideas"]:
            writer.writerow(
                [
                    "idea",
                    idea["id"],
                    idea.get("participant_name") or "Anonymous",
                    idea["content"].replace("\n", " ")[:200],
                    f"cluster_id={idea['cluster_id']}; timestamp={idea['timestamp']}",
                ]
            )

        for cluster in data["clusters"]:
            writer.writerow(
                [
                    "cluster",
                    cluster["id"],
                    cluster["name"],
                    cluster.get("description") or "",
                    f"votes={cluster['votes']}; idea_count={len(cluster['idea_ids'])}",
                ]
            )

        for message in data["messages"]:
            writer.writerow(
                [
                    "message",
                    message["id"],
                    message["username"],
                    message["message"].replace("\n", " ")[:200],
                    message.get("timestamp") or "",
                ]
            )

        for document in data.get("documents", []):
            writer.writerow(
                [
                    "document",
                    document.get("document_id") or document.get("link_id"),
                    document.get("title") or "",
                    document.get("file_name") or "",
                    "link_id={lid}; added_at={added}; uploaded_by={uploader}".format(
                        lid=document.get("link_id"),
                        added=document.get("added_at") or "",
                        uploader=document.get("uploader_name")
                        or document.get("uploaded_by_id")
                        or "",
                    ),
                ]
            )

        return buffer.getvalue()

    @staticmethod
    def export_workshop_pdf(workshop: Workshop) -> bytes:
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter

        def draw_line(text: str, y: float, font_size: int = 10) -> float:
            pdf.setFont("Helvetica", font_size)
            pdf.drawString(40, y, text[:130])
            return y - (font_size + 6)

        snapshot = WorkshopAdmin.workshop_snapshot(workshop)
        y = height - 60
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(40, y, f"Workshop Report: {workshop.title}")
        y -= 30

        meta_lines = [
            f"Objective: {workshop.objective or 'N/A'}",
            f"Status: {workshop.status}",
            f"Scheduled: {workshop.date_time.strftime('%Y-%m-%d %H:%M UTC')}",
            f"Duration: {workshop.duration or 'N/A'} minutes",
            f"Participants: {len(snapshot['participants'])}",
            f"Ideas generated: {len(snapshot['ideas'])}",
        ]
        for line in meta_lines:
            y = draw_line(line, y, 12)

        y -= 10
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(40, y, "Participants")
        y -= 18
        for participant in snapshot["participants"]:
            name = participant.user.display_name if participant.user else "Guest"
            joined = participant.joined_timestamp.strftime("%Y-%m-%d %H:%M") if participant.joined_timestamp else ""
            y = draw_line(f"- {name} ({joined})", y)
            if y < 80:
                pdf.showPage()
                y = height - 60

        y -= 4
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(40, y, "Idea Clusters")
        y -= 18
        for cluster, votes in snapshot["clusters"]:
            cluster_name = cluster.name or "Unnamed cluster"
            y = draw_line(f"- {cluster_name} (votes: {votes})", y)
            if y < 80:
                pdf.showPage()
                y = height - 60

        y -= 4
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(40, y, "Documents")
        y -= 18
        if snapshot["documents"]:
            for link in snapshot["documents"]:
                doc = link.document
                title = getattr(doc, "title", None) or "Missing document"
                file_name = getattr(doc, "file_name", None)
                added_at = link.added_at.strftime("%Y-%m-%d %H:%M") if link.added_at else ""
                details = f"{file_name or ''}".strip()
                line = f"- {title}"
                if details:
                    line += f" ({details})"
                if added_at:
                    line += f" — added {added_at}"
                y = draw_line(line, y)
                if y < 80:
                    pdf.showPage()
                    y = height - 60
        else:
            y = draw_line("- No documents linked", y)
            if y < 80:
                pdf.showPage()
                y = height - 60

        y -= 4
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(40, y, "Recent Messages")
        y -= 18
        for message in snapshot["messages"]:
            timestamp = message.timestamp.strftime("%H:%M") if message.timestamp else ""
            y = draw_line(f"[{timestamp}] {message.username}: {message.message}", y)
            if y < 80:
                pdf.showPage()
                y = height - 60

        pdf.showPage()
        pdf.save()
        buffer.seek(0)
        return buffer.read()