"""AWS Transcribe streaming provider (scaffold).

This is a minimal non-blocking wrapper around the Amazon Transcribe
Streaming SDK. Network calls occur in async context; partial and final
events are normalized into ProviderEvent dataclasses.

Real credentials and region selection are handled externally (env vars
or explicit boto config). Error handling is intentionally conservative
at this stage; production integration should add retries / backoff.
"""
from __future__ import annotations
import asyncio
import os
from typing import AsyncIterator, Optional

try:
    from amazon_transcribe.client import TranscribeStreamingClient
    from amazon_transcribe.handlers import TranscriptResultStreamHandler
    from amazon_transcribe.model import TranscriptEvent as AwsTranscriptEvent
except Exception:  # pragma: no cover - optional dependency not installed in dev yet
    TranscribeStreamingClient = None  # type: ignore
    TranscriptResultStreamHandler = object  # type: ignore
    AwsTranscriptEvent = object  # type: ignore

from .provider import (
    TranscriptionProvider, ProviderConfig,
    TranscriptPartialEvent, TranscriptFinalEvent, ProviderEvent
)


class _AwsHandler(TranscriptResultStreamHandler):  # type: ignore[misc]
    def __init__(self, stream, queue: 'asyncio.Queue[ProviderEvent]'):
        super().__init__(stream)
        self._queue = queue

    async def handle_transcript_event(self, event: AwsTranscriptEvent):  # type: ignore[override]
        for result in event.transcript.results:  # type: ignore[attr-defined]
            if not result.alternatives:
                continue
            alt = result.alternatives[0]
            text = alt.transcript
            is_partial = getattr(result, 'is_partial', False)
            start = alt.items[0].start_time if getattr(alt, 'items', None) else None
            end = alt.items[-1].end_time if getattr(alt, 'items', None) else None
            words = []
            if getattr(alt, 'items', None):
                for wi in alt.items:
                    words.append({
                        'word': getattr(wi, 'content', ''),
                        'start': getattr(wi, 'start_time', None),
                        'end': getattr(wi, 'end_time', None),
                        'type': getattr(wi, 'type', None),
                    })
            evt: ProviderEvent
            if is_partial:
                evt = TranscriptPartialEvent(text=text, start_time=start, end_time=None, is_final=False, words=words)
            else:
                evt = TranscriptFinalEvent(text=text, start_time=start, end_time=end, is_final=True, words=words)
            await self._queue.put(evt)


class AwsTranscribeStreamingProvider(TranscriptionProvider):
    """Thin async wrapper around Amazon Transcribe Streaming.

    The SDK is optional; call ``is_available()`` before constructing in code paths
    where the dependency may not be installed. This prevents hard RuntimeErrors
    during feature‑flagged / locally disabled transcription.
    """

    @classmethod
    def is_available(cls) -> bool:
        return TranscribeStreamingClient is not None  # type: ignore

    def __init__(self, region: Optional[str] = None):
        self._region = region or os.getenv('AWS_REGION', 'us-east-1')
        self._client = None
        self._stream = None
        self._queue: 'asyncio.Queue[ProviderEvent]' = asyncio.Queue()
        self._consumer_task: Optional[asyncio.Task] = None
        self._opened = False
        # Lazy caches for capability checks
        self._vocabularies = None
        self._vocab_filters = None

    async def ensure_capabilities(self, vocabulary_name: Optional[str], vocab_filter_name: Optional[str]) -> dict:
        """Optionally verify that custom vocabulary / filter exist.

        Returns a dict with missing items, e.g. { 'missing_vocabulary': 'name' }.
        If the SDK or client fails, returns empty (non-fatal for streaming start here).
        """
        if TranscribeStreamingClient is None:
            return {}
        try:
            if self._client is None:
                self._client = TranscribeStreamingClient(region=self._region)
            missing = {}
            # NOTE: The streaming SDK may not expose list vocabulary APIs directly; this is a placeholder.
            # In a full integration you would use boto3 transcribe client for these checks.
            _ = vocabulary_name, vocab_filter_name  # silence unused in placeholder
            return missing
        except Exception:
            return {}

    async def open_stream(self, session_id: str, config: ProviderConfig) -> None:  # type: ignore[override]
        if TranscribeStreamingClient is None:
            raise RuntimeError(
                'Amazon Transcribe SDK not installed. Install with: '
                "pip install amazon-transcribe --upgrade"
            )
        self._client = TranscribeStreamingClient(region=self._region)
        self._stream = await self._client.start_stream_transcription(
            language_code=config.language_code,
            media_sample_rate_hz=config.sample_rate_hz,
            media_encoding='pcm'
        )
        handler = _AwsHandler(self._stream.output_stream, self._queue)
        self._consumer_task = asyncio.create_task(handler.handle_events())
        self._opened = True

    async def write(self, chunk: bytes) -> None:  # type: ignore[override]
        if not self._opened or not self._stream:
            raise RuntimeError('Stream not opened')
        # Each chunk is raw PCM16 mono little endian
        await self._stream.input_stream.send_audio_event(audio_chunk=chunk)

    async def aresults(self) -> AsyncIterator[ProviderEvent]:  # type: ignore[override]
        while True:
            evt = await self._queue.get()
            yield evt

    async def close(self) -> None:  # type: ignore[override]
        if self._stream:
            await self._stream.input_stream.end_stream()
        if self._consumer_task:
            self._consumer_task.cancel()
        self._opened = False
