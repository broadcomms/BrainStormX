"""Helpers for persisting TTS scripts and audio renditions."""

from __future__ import annotations

import hashlib
import os
import time
import wave
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flask import current_app

from app.extensions import db
from app.models import Document, DocumentAudio
from app.service.routes.tts import get_provider


@dataclass(slots=True)
class TTSOptions:
	provider: Optional[str] = None
	voice: Optional[str] = None
	speed: float = 1.0
	fmt: str = "wav"


class TTSScriptManager:
	"""Coordinates script persistence and cached audio for documents."""

	AUDIO_SUBDIR = Path("uploads") / "document_audio"

	def __init__(self, *, instance_path: Optional[Path] = None) -> None:
		self.instance_path = Path(instance_path or current_app.instance_path)

	# ------------------------------------------------------------------
	# Script helpers
	# ------------------------------------------------------------------
	def save_script(self, document: Document, script: str) -> None:
		document.tts_script = script

	# ------------------------------------------------------------------
	# Audio management
	# ------------------------------------------------------------------
	def ensure_audio(
		self,
		document: Document,
		*,
		options: Optional[TTSOptions] = None,
		force: bool = False,
	) -> DocumentAudio:
		if not document.tts_script:
			raise ValueError("Document has no TTS script to render")

		options = options or TTSOptions()
		script_hash = hashlib.sha256(document.tts_script.encode("utf-8")).hexdigest()

		if not force:
			existing = self._get_cached_audio(document, script_hash)
			if existing:
				return existing

		# Remove stale audio entries
		self.purge_audio(document)

		provider = get_provider(options.provider)
		rel_path = self._render_audio(document, provider, options)

		audio = DocumentAudio()
		audio.document_id = document.id
		audio.audio_file_path = str(rel_path)
		audio.audio_sha256 = script_hash
		audio.storage_backend = "local"
		audio.duration_seconds = self._calculate_duration(rel_path)
		db.session.add(audio)
		return audio

	def purge_audio(self, document: Document) -> None:
		for audio in list(getattr(document, "audios", []) or []):
			abs_path = Path(current_app.instance_path) / audio.audio_file_path
			try:
				if abs_path.exists():
					abs_path.unlink()
			except Exception as exc:  # pragma: no cover - best effort cleanup
				current_app.logger.warning("Failed to remove audio file %s: %s", abs_path, exc)
			db.session.delete(audio)

	# ------------------------------------------------------------------
	# Internal helpers
	# ------------------------------------------------------------------
	def _audio_dir(self, document: Document) -> Path:
		path = self.instance_path / self.AUDIO_SUBDIR / f"document_{document.id}"
		path.mkdir(parents=True, exist_ok=True)
		return path

	def _render_audio(self, document: Document, provider, options: TTSOptions) -> Path:
		directory = self._audio_dir(document)
		timestamp = int(time.time())
		filename = f"tts_{timestamp}.{'mp3' if options.fmt == 'mp3' else 'wav'}"
		absolute = directory / filename

		with absolute.open("wb") as fh:
			kwargs = {"fmt": options.fmt, "speed": options.speed}
			if options.voice:
				kwargs["voice"] = options.voice
			for chunk in provider.synth_stream(document.tts_script, **kwargs):
				fh.write(chunk)

		relative = self.AUDIO_SUBDIR / f"document_{document.id}" / filename
		return relative

	def _get_cached_audio(self, document: Document, script_hash: str) -> Optional[DocumentAudio]:
		audios = getattr(document, "audios", []) or []
		for audio in sorted(audios, key=lambda a: getattr(a, "created_at", 0) or 0, reverse=True):
			if audio.audio_sha256 == script_hash:
				return audio
		return None

	def _calculate_duration(self, relative_path: Path) -> Optional[int]:
		absolute = Path(current_app.instance_path) / relative_path
		if not absolute.exists() or absolute.suffix.lower() != ".wav":
			return None
		try:
			with closing(wave.open(str(absolute), "rb")) as wav_file:
				frames = wav_file.getnframes()
				rate = wav_file.getframerate()
				return int(frames / float(rate))
		except Exception:
			return None


def get_manager() -> TTSScriptManager:
	return TTSScriptManager()
