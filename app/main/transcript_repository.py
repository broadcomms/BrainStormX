from __future__ import annotations

import json
import os
import subprocess
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:  # Optional dependency installed via requirements.txt
    from vosk import KaldiRecognizer, Model  # type: ignore
except Exception:  # pragma: no cover - Vosk optional during tests
    KaldiRecognizer = None  # type: ignore
    Model = None  # type: ignore

from .video_library import VideoAsset


def _format_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - (hours * 3600) - (minutes * 60)
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"  # WebVTT expects '.' decimal separator


@dataclass
class TranscriptRepository:
    instance_dir: Path
    static_dir: Path
    model_path: Optional[Path] = None
    ffmpeg_bin: str = "ffmpeg"
    sample_rate: int = 16_000
    block_max_duration: float = 8.0
    block_max_words: int = 38

    def __post_init__(self) -> None:
        self.instance_dir = Path(self.instance_dir)
        self.static_dir = Path(self.static_dir)
        self.model_path = Path(self.model_path) if self.model_path else None

        self.transcripts_dir = self.instance_dir / "transcripts"
        self.captions_dir = self.static_dir / "captions"
        self.tmp_dir = self.instance_dir / "tmp"

        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.captions_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        self._model: Optional[Model] = None  # type: ignore[attr-defined]

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def load(self, asset: VideoAsset, language: str = "en") -> Dict[str, Any]:
        path = self._transcript_path(asset, language)
        if not path.exists():
            raise FileNotFoundError(str(path))
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def ensure(self, asset: VideoAsset, language: str = "en", force: bool = False) -> Dict[str, Any]:
        transcript_path = self._transcript_path(asset, language)
        caption_path = self._caption_path(asset, language)
        if not force and transcript_path.exists() and caption_path.exists():
            return self.load(asset, language)
        return self._generate(asset, language)

    def list_languages(self, asset: VideoAsset) -> List[str]:
        langs = []
        for lang in asset.languages:
            path = self._transcript_path(asset, lang)
            if path.exists():
                langs.append(lang)
        return sorted(set(langs)) or list(asset.languages)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _transcript_path(self, asset: VideoAsset, language: str) -> Path:
        return asset.transcript_path(language, base_dir=self.transcripts_dir)

    def _caption_path(self, asset: VideoAsset, language: str) -> Path:
        # For now captions default to asset.caption_basename (English)
        if language != "en":
            basename = f"video-{asset.id}_{language}.vtt"
        else:
            basename = asset.caption_basename
        return self.captions_dir / basename

    def _video_path(self, asset: VideoAsset) -> Path:
        return self.static_dir / "videos" / asset.video_filename

    def _wav_path(self, asset: VideoAsset, language: str) -> Path:
        return self.tmp_dir / f"{asset.slug}_{language}_{self.sample_rate}.wav"

    # ------------------------------------------------------------------
    # Transcript generation pipeline
    # ------------------------------------------------------------------
    def _generate(self, asset: VideoAsset, language: str) -> Dict[str, Any]:
        recognizer = self._get_recognizer()
        video_path = self._video_path(asset)
        if not video_path.exists():
            raise FileNotFoundError(f"Video asset not found: {video_path}")

        wav_path = self._convert_to_wav(video_path, asset, language)
        try:
            words = self._transcribe_audio(recognizer, wav_path)
        finally:
            try:
                wav_path.unlink()
            except Exception:
                pass

        blocks = self._build_blocks(words)
        payload = {
            "videoId": asset.id,
            "language": language,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "blocks": blocks,
        }

        transcript_path = self._transcript_path(asset, language)
        with transcript_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

        caption_path = self._caption_path(asset, language)
        vtt_text = self._blocks_to_vtt(blocks)
        with caption_path.open("w", encoding="utf-8") as handle:
            handle.write(vtt_text)

        return payload

    def _get_recognizer(self) -> KaldiRecognizer:
        if KaldiRecognizer is None or Model is None:
            raise RuntimeError(
                "The 'vosk' package is required for transcript generation. Install it and set VOSK_MODEL_PATH."
            )
        if self._model is None:
            model_path = self.model_path or self._resolve_model_path()
            if not model_path or not model_path.exists():
                raise RuntimeError(
                    "Vosk model not found. Set VOSK_MODEL_PATH to a directory containing the model files."
                )
            self._model = Model(str(model_path))  # type: ignore[arg-type]
        return KaldiRecognizer(self._model, self.sample_rate)  # type: ignore[arg-type]

    def _resolve_model_path(self) -> Path:
        env_path = os.getenv("VOSK_MODEL_PATH")
        if not env_path:
            raise RuntimeError("VOSK_MODEL_PATH environment variable is not set")
        path = Path(env_path)
        if not path.exists():
            raise RuntimeError(f"Vosk model directory not found: {path}")
        return path

    def _convert_to_wav(self, video_path: Path, asset: VideoAsset, language: str) -> Path:
        wav_path = self._wav_path(asset, language)
        cmd = [
            self.ffmpeg_bin,
            "-y",
            "-i",
            str(video_path),
            "-ar",
            str(self.sample_rate),
            "-ac",
            "1",
            "-vn",
            str(wav_path),
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise RuntimeError(
                f"FFmpeg not found (looked for '{self.ffmpeg_bin}'). Install FFmpeg or set FFMPEG_BIN env var."
            )
        except subprocess.CalledProcessError as exc:  # pragma: no cover - surface stderr to caller
            stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else ""
            raise RuntimeError(f"FFmpeg failed to process {video_path}: {stderr}")
        return wav_path

    def _transcribe_audio(self, recognizer: KaldiRecognizer, wav_path: Path) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        with wave.open(str(wav_path), "rb") as wf:
            if wf.getframerate() != self.sample_rate:
                raise RuntimeError(
                    f"Unexpected sample rate {wf.getframerate()} Hz; expected {self.sample_rate} Hz."
                )
            recognizer.SetWords(True)
            while True:
                data = wf.readframes(4000)
                if not data:
                    break
                if recognizer.AcceptWaveform(data):  # type: ignore[attr-defined]
                    segment = json.loads(recognizer.Result())  # type: ignore[attr-defined]
                    results.extend(segment.get("result", []))
            final_segment = json.loads(recognizer.FinalResult())  # type: ignore[attr-defined]
            results.extend(final_segment.get("result", []))
        return results

    def _build_blocks(self, raw_words: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        current_words: List[Dict[str, Any]] = []
        block_start: Optional[float] = None

        for raw in raw_words:
            try:
                start = float(raw.get("start", 0.0))
                end = float(raw.get("end", start))
                text = str(raw.get("word", raw.get("text", ""))).strip()
                confidence = raw.get("confidence") or raw.get("conf")
            except Exception:
                continue
            if not text:
                continue

            word_payload = {
                "text": text,
                "start": round(start, 3),
                "end": round(end, 3),
            }
            if confidence is not None:
                word_payload["confidence"] = float(confidence)

            if not current_words:
                block_start = start
            current_words.append(word_payload)
            block_duration = (end - (block_start or start)) if block_start is not None else 0.0

            if (
                len(current_words) >= self.block_max_words
                or (block_duration >= self.block_max_duration)
            ):
                blocks.append(self._finalize_block(current_words))
                current_words = []
                block_start = None

        if current_words:
            blocks.append(self._finalize_block(current_words))

        return blocks

    def _finalize_block(self, words: List[Dict[str, Any]]) -> Dict[str, Any]:
        start = words[0]["start"]
        end = words[-1]["end"]
        text = " ".join(word["text"] for word in words)
        return {
            "start": start,
            "end": end,
            "text": text,
            "words": words,
        }

    def _blocks_to_vtt(self, blocks: List[Dict[str, Any]]) -> str:
        lines = ["WEBVTT", ""]
        for idx, block in enumerate(blocks, start=1):
            start_ts = _format_timestamp(block.get("start", 0.0))
            end_ts = _format_timestamp(block.get("end", block.get("start", 0.0) + 2.0))
            text = block.get("text") or " ".join(word.get("text", "") for word in block.get("words", []))
            lines.append(str(idx))
            lines.append(f"{start_ts} --> {end_ts}")
            lines.append(text.strip())
            lines.append("")
        return "\n".join(lines)