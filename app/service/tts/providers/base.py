from __future__ import annotations

from typing import Iterable, Optional


class SynthesisProvider:
    """Abstract TTS provider interface.

    Providers should yield encoded audio bytes. For initial implementation we
    target WAV (PCM16) for local Piper and allow MP3 for Polly later.
    """

    name: str = "abstract"

    def synth_stream(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: float = 1.0,
        fmt: str = "wav",
    ) -> Iterable[bytes]:
        raise NotImplementedError

    def get_word_marks(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: float = 1.0,
    ) -> list[dict]:
        """Optional capability: return word-level timing marks.

        Returns a list of dicts like {"time": <ms_from_start:int>, "value": <word:str>}.
        Providers that don't support marks should return an empty list.
        """
        return []
