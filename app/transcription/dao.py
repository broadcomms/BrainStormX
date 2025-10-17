from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Dialogue, Transcript, WorkshopParticipant, db


@dataclass(frozen=True)
class TranscriptContext:
    """Immutable context necessary for persisting STT events."""

    workshop_id: int
    user_id: int
    language: Optional[str] = None
    task_id: Optional[int] = None


def _seconds_to_dt(seconds: float | None) -> datetime | None:
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc)
    except Exception:
        return None


class TranscriptWriter:
    """Typed facade over SQLAlchemy session for STT persistence."""

    def __init__(self, session: Session | None = None) -> None:
        self._session: Session = session or db.session

    def record_partial(
        self,
        ctx: TranscriptContext,
        text: str,
        partial_id: int | None,
    ) -> int:
        """Create or update a streaming Dialogue row for an in-progress utterance."""
        dialogue: Dialogue | None = None
        if partial_id:
            dialogue = self._session.get(Dialogue, partial_id)

        if dialogue is None:
            dialogue = Dialogue(
                workshop_id=ctx.workshop_id,
                speaker_id=ctx.user_id,
                transcript_id=None,
                dialogue_text=text,
                is_final=False,
            )
            self._session.add(dialogue)
            self._session.flush()
        else:
            dialogue.dialogue_text = text
            dialogue.is_final = False

        self._session.commit()
        return dialogue.dialogue_id

    def record_final(
        self,
        ctx: TranscriptContext,
        text: str,
        start_time: float | None,
        end_time: float | None,
        partial_id: int | None,
    ) -> int:
        """Persist a finalized transcript row and return its identifier."""
        transcript = Transcript(
            workshop_id=ctx.workshop_id,
            user_id=ctx.user_id,
            task_id=ctx.task_id,
            entry_type='human',
            raw_stt_transcript=text,
            processed_transcript=text,
            language=ctx.language,
            start_timestamp=_seconds_to_dt(start_time),
            end_timestamp=_seconds_to_dt(end_time),
        )
        self._session.add(transcript)
        self._session.flush()

        dialogue: Dialogue | None = None
        if partial_id:
            dialogue = self._session.get(Dialogue, partial_id)

        if dialogue is None:
            dialogue = Dialogue(
                workshop_id=ctx.workshop_id,
                speaker_id=ctx.user_id,
                transcript_id=transcript.transcript_id,
                dialogue_text=text,
                is_final=True,
            )
            self._session.add(dialogue)
        else:
            dialogue.transcript_id = transcript.transcript_id
            dialogue.dialogue_text = text
            dialogue.is_final = True

        self._session.commit()
        return transcript.transcript_id

    def resolve_speaker_name(self, ctx: TranscriptContext) -> tuple[str | None, str | None]:
        participant = (
            WorkshopParticipant.query.filter_by(
                workshop_id=ctx.workshop_id,
                user_id=ctx.user_id,
            ).first()
        )
        if not participant:
            return None, None
        user = getattr(participant, 'user', None)
        first = getattr(user, 'first_name', None)
        last = getattr(user, 'last_name', None)
        return first, last
