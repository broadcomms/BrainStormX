from __future__ import annotations

from typing import Iterable, Optional

import boto3

from .base import SynthesisProvider


class PollyProvider(SynthesisProvider):
    name = "polly"

    def __init__(self, region: str):
        self.client = boto3.client("polly", region_name=region)

    def synth_stream(self, text: str, *, voice: str | None = None, speed: float = 1.0, fmt: str = "mp3") -> Iterable[bytes]:
        # Speed control can be done with SSML prosody; left for later.
        output_format = "mp3" if fmt == "mp3" else "pcm"
        voice_id = voice or "Joanna"
        resp = self.client.synthesize_speech(Text=text, VoiceId=voice_id, OutputFormat=output_format)
        stream = resp.get("AudioStream")
        if not stream:
            return
        CHUNK = 16384
        while True:
            data = stream.read(CHUNK)
            if not data:
                break
            yield data

    def get_word_marks(self, text: str, *, voice: Optional[str] = None, speed: float = 1.0) -> list[dict]:
        """Return word-level timing marks using Polly speech marks API.

        Note: Polly returns times in milliseconds relative to start of audio for 'word' marks.
        """
        voice_id = voice or "Joanna"
        # Speed control could be applied with SSML; for now we ignore speed for marks.
        resp = self.client.synthesize_speech(
            Text=text,
            VoiceId=voice_id,
            OutputFormat="json",
            SpeechMarkTypes=["word"],
        )
        stream = resp.get("AudioStream")
        if not stream:
            return []
        # Stream consists of JSON lines, one per word mark
        marks: list[dict] = []
        buf = b""
        CHUNK = 8192
        while True:
            data = stream.read(CHUNK)
            if not data:
                break
            buf += data
        try:
            # Polly returns newline-delimited JSON objects
            for line in buf.decode("utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                # Example: {"time":1140,"type":"word","start":0,"end":5,"value":"Hello"}
                import json as _json
                try:
                    obj = _json.loads(line)
                    if obj.get("type") == "word":
                        marks.append({"time": int(obj.get("time", 0)), "value": str(obj.get("value", ""))})
                except Exception:
                    continue
        except Exception:
            return []
        return marks
