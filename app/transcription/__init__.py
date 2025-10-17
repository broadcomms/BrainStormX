"""Transcription provider abstraction layer.

This package defines a lightweight, pluggable interface used by the
Socket.IO transcription gateway. Concrete providers (e.g. AWS Transcribe,
local Whisper, Vosk) implement the async streaming interface so the rest
of the app remains provider‑agnostic.

Design goals:
 - Minimal surface area (open_stream, write, aresults iterator, close)
 - Async friendly; provider may run internal tasks
 - Normalized partial/final event structure
 - Extensible metadata (words, confidence, speaker label, etc.)
"""

from .provider import (
    TranscriptionProvider,
    TranscriptPartialEvent,
    TranscriptFinalEvent,
    ProviderEvent,
    ProviderConfig,
)

__all__ = [
    'TranscriptionProvider',
    'TranscriptPartialEvent',
    'TranscriptFinalEvent',
    'ProviderEvent',
    'ProviderConfig',
]
