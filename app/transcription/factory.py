"""Provider factory for transcription services.

Centralizes the logic of selecting and instantiating a concrete
TranscriptionProvider based on configuration/environment variables.

Usage:
    provider, name = create_provider(os.getenv('TRANSCRIPTION_PROVIDER', 'vosk'))

Returned name is a normalized symbolic identifier suitable for
emitting to clients.
"""
from __future__ import annotations
import os
import logging
from typing import Tuple

from .provider import TranscriptionProvider
from .aws_transcribe import AwsTranscribeStreamingProvider  # type: ignore
from .vosk_provider import VoskStreamingProvider  # type: ignore

_ALIAS_MAP = {
    'aws': 'aws_transcribe',
    'aws_transcribe': 'aws_transcribe',
    'amazon': 'aws_transcribe',
    'vosk': 'vosk',
    'local_vosk': 'vosk',
    'offline': 'vosk',
}

def create_provider(raw_name: str | None) -> Tuple[TranscriptionProvider, str]:
    name = (raw_name or os.getenv('TRANSCRIPTION_PROVIDER') or os.getenv('STT_PROVIDER') or 'vosk').strip().lower()
    name = _ALIAS_MAP.get(name, name)
    if name == 'aws_transcribe':
        if not AwsTranscribeStreamingProvider.is_available():  # type: ignore[attr-defined]
            raise RuntimeError('AWS Transcribe unavailable: install amazon-transcribe and configure credentials.')
        inst = AwsTranscribeStreamingProvider()
        logging.getLogger(__name__).info('Selected transcription provider: aws_transcribe (region=%s)', os.getenv('AWS_REGION'))
        return inst, 'aws_transcribe'
    if name == 'vosk':
        if not VoskStreamingProvider.is_available():  # type: ignore[attr-defined]
            raise RuntimeError('Vosk unavailable: pip install vosk and set VOSK_MODEL_PATH to model directory.')
        inst = VoskStreamingProvider()
        logging.getLogger(__name__).info('Selected transcription provider: vosk (model_path=%s)', os.getenv('VOSK_MODEL_PATH'))
        return inst, 'vosk'
    raise ValueError(f'Unknown transcription provider: {name}')
