from __future__ import annotations
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional, Protocol, Union

@dataclass
class ProviderConfig:
    language_code: str = 'en-US'
    sample_rate_hz: int = 16000
    media_encoding: str = 'pcm16'
    enable_partials: bool = True
    vocab_name: Optional[str] = None
    extras: Dict[str, str] = field(default_factory=dict)

@dataclass
class TranscriptPartialEvent:
    text: str
    start_time: Optional[float] = None
    # For partials end_time is usually None
    end_time: Optional[float] = None
    is_final: bool = False
    confidence: Optional[float] = None
    words: Optional[List[Dict[str, Union[str, float]]]] = None

@dataclass
class TranscriptFinalEvent:
    text: str
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    is_final: bool = True
    confidence: Optional[float] = None
    words: Optional[List[Dict[str, Union[str, float]]]] = None

ProviderEvent = Union[TranscriptPartialEvent, TranscriptFinalEvent]


class TranscriptionProvider(Protocol):
    """Abstract streaming transcription provider.

    Lifecycle:
      1. await open_stream(session_id, config)
      2. Repeated await write(pcm_bytes)
      3. Iterate over events via aresults() (async iterator)
      4. await close() when done or on error
    """

    async def open_stream(self, session_id: str, config: ProviderConfig) -> None: ...
    async def write(self, chunk: bytes) -> None: ...
    async def aresults(self) -> AsyncIterator[ProviderEvent]: ...
    async def close(self) -> None: ...
