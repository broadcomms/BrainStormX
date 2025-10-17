"""End-to-end document processing pipeline."""

from __future__ import annotations

import hashlib
import mimetypes
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Optional

from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import Chunk, Document, DocumentProcessingLog

from .chunker import ChunkPayload, DocumentChunker
from .embedder import EmbeddingProvider, get_default_embedder
from .extractors import ExtractionError, ExtractionResult, extract_content
from .llm_enrichment import LLMEnrichmentResult, enrich_document
from .normalizer import NormalizationResult, normalize_text
from .tts_reader import TTSScriptManager, TTSOptions, get_manager


@dataclass(slots=True)
class PipelineResult:
	document_id: int
	chunk_count: int
	total_pages: Optional[int]
	content_sha256: str
	title: str
	summary: str


@dataclass(slots=True)
class PipelineContext:
	embedder: EmbeddingProvider
	chunker: DocumentChunker
	tts_manager: TTSScriptManager
	pregenerate_audio: bool = False


class DocumentProcessingPipeline:
	"""Coordinates extraction, enrichment, chunking, embedding, and persistence."""

	def __init__(self, *, context: Optional[PipelineContext] = None) -> None:
		self.context = context or PipelineContext(
			embedder=get_default_embedder(),
			chunker=DocumentChunker(),
			tts_manager=get_manager(),
			pregenerate_audio=current_app.config.get("DOCUMENT_PREGENERATE_AUDIO", False),
		)

	def run(self, document_id: int, *, force: bool = False) -> PipelineResult:
		document = db.session.get(Document, document_id)
		if not document:
			raise ValueError(f"Document {document_id} not found")

		if document.processing_status == "processing" and not force:
			raise RuntimeError("Document is already being processed")

		document.processing_status = "processing"
		document.processing_started_at = datetime.utcnow()
		document.processing_attempts = (document.processing_attempts or 0) + 1
		db.session.commit()

		current_app.logger.info(
			"Document pipeline start",
			extra={
				"doc_id": document.id,
				"workspace_id": document.workspace_id,
				"stage": "start",
			},
		)

		extraction: ExtractionResult
		normalization: NormalizationResult
		enrichment: LLMEnrichmentResult
		chunk_payloads: list[ChunkPayload]
		embeddings: list[list[float]]

		try:
			extraction = self._extract(document, force=force)
			normalization = self._normalize(document, extraction)
			enrichment = self._enrich(document, normalization)
			chunk_payloads = self._chunk(document, normalization)
			embeddings = self._embed(document, chunk_payloads)
			result = self._persist(
				document,
				extraction=extraction,
				normalization=normalization,
				enrichment=enrichment,
				chunk_payloads=chunk_payloads,
				embeddings=embeddings,
			)
			document.processing_status = "completed"
			document.last_processed_at = datetime.utcnow()
			db.session.commit()
			current_app.logger.info(
				"Document pipeline completed",
				extra={
					"doc_id": document.id,
					"workspace_id": document.workspace_id,
					"chunk_count": result.chunk_count,
					"stage": "complete",
				},
			)
			return result
		except Exception as exc:
			current_app.logger.exception("Document processing failed for %s: %s", document_id, exc)
			db.session.rollback()
			document = db.session.get(Document, document_id)
			if document:
				document.processing_status = "failed"
				db.session.commit()
			raise

	# ------------------------------------------------------------------
	# Stages
	# ------------------------------------------------------------------
	def _extract(self, document: Document, *, force: bool) -> ExtractionResult:
		with self._stage(document, "extract") as log:
			if document.content and document.content_sha256 and not force:
				log.total_pages = log.total_pages or 1
				log.processed_pages = log.processed_pages or 1
				return ExtractionResult(
					content=document.content,
					total_pages=None,
					metadata={"extractor": "existing"},
				)

			path = Path(current_app.instance_path) / document.file_path
			if not path.exists():
				raise FileNotFoundError(f"Document file missing: {path}")
			mime_type, _ = mimetypes.guess_type(path.name)
			extraction = extract_content(path, mime_type=mime_type)
			if extraction.total_pages is not None:
				log.total_pages = extraction.total_pages
				log.processed_pages = extraction.total_pages
			else:
				log.processed_pages = len(extraction.content.splitlines()) or 1
			return extraction

	def _normalize(self, document: Document, extraction: ExtractionResult) -> NormalizationResult:
		with self._stage(document, "normalize") as log:
			result = normalize_text(extraction.content)
			log.processed_pages = len(result.content.splitlines()) or 1
			return result

	def _enrich(self, document: Document, normalization: NormalizationResult) -> LLMEnrichmentResult:
		with self._stage(document, "llm_enrichment") as log:
			result = enrich_document(normalization.content, normalization.headings)
			log.processed_pages = len(result.summary.split()) or len(result.markdown.splitlines()) or 1
			return result

	def _chunk(self, document: Document, normalization: NormalizationResult) -> list[ChunkPayload]:
		with self._stage(document, "chunk") as log:
			payloads = self.context.chunker.chunk(normalization.content, headings=normalization.headings)
			log.processed_pages = len(payloads)
			return payloads

	def _embed(self, document: Document, chunk_payloads: list[ChunkPayload]) -> list[list[float]]:
		with self._stage(document, "embed") as log:
			texts = [chunk.content for chunk in chunk_payloads]
			embeddings = self.context.embedder.embed(texts)
			if len(embeddings) != len(chunk_payloads):
				raise RuntimeError("Embedding provider returned inconsistent vector counts")
			log.processed_pages = len(embeddings)
			return embeddings

	def _persist(
		self,
		document: Document,
		*,
		extraction: ExtractionResult,
		normalization: NormalizationResult,
		enrichment: LLMEnrichmentResult,
		chunk_payloads: list[ChunkPayload],
		embeddings: list[list[float]],
	) -> PipelineResult:
		with self._stage(document, "persist") as log:
			content_sha = hashlib.sha256(normalization.content.encode("utf-8")).hexdigest()

			# Update document metadata
			document.title = enrichment.title or document.title
			document.description = enrichment.description or document.description
			document.summary = enrichment.summary
			document.markdown = enrichment.markdown
			document.tts_script = enrichment.tts_script
			document.content = normalization.content
			document.version = (document.version or 0) + 1
			document.content_sha256 = content_sha

			# Replace chunks
			Chunk.query.filter(Chunk.document_id == document.id).delete(synchronize_session=False)
			db.session.flush()
			for payload, vector in zip(chunk_payloads, embeddings):
				chunk = Chunk()
				chunk.document_id = document.id
				chunk.content = payload.content
				chunk.meta_data = payload.metadata
				chunk.vector = vector
				db.session.add(chunk)

			# Persist TTS script and optional audio
			self.context.tts_manager.save_script(document, enrichment.tts_script)
			if self.context.pregenerate_audio:
				try:
					self.context.tts_manager.ensure_audio(document, options=TTSOptions(), force=True)
				except Exception as exc:  # pragma: no cover - optional stage
					current_app.logger.warning("TTS pre-generation failed for doc %s: %s", document.id, exc)

			log.processed_pages = len(chunk_payloads)
			if extraction.total_pages is not None:
				log.total_pages = extraction.total_pages

			return PipelineResult(
				document_id=document.id,
				chunk_count=len(chunk_payloads),
				total_pages=extraction.total_pages,
				content_sha256=content_sha,
				title=document.title,
				summary=document.summary or "",
			)

	# ------------------------------------------------------------------
	# Stage helper
	# ------------------------------------------------------------------
	@contextmanager
	def _stage(self, document: Document, stage: str):
		current_app.logger.info(
			"Document pipeline stage start",
			extra={"doc_id": document.id, "workspace_id": document.workspace_id, "stage": stage},
		)

		log = DocumentProcessingLog()
		log.document_id = document.id
		log.stage = stage
		log.status = "processing"
		log.started_at = datetime.utcnow()
		db.session.add(log)
		db.session.commit()
		start = perf_counter()
		try:
			yield log
			log.status = "completed"
			log.completed_at = datetime.utcnow()
			db.session.commit()
			duration_ms = int((perf_counter() - start) * 1000)
			current_app.logger.info(
				"Document pipeline stage complete",
				extra={
					"doc_id": document.id,
					"workspace_id": document.workspace_id,
					"stage": stage,
					"duration_ms": duration_ms,
					"processed_pages": getattr(log, "processed_pages", None),
					"total_pages": getattr(log, "total_pages", None),
				},
			)
		except Exception as exc:
			log.status = "failed"
			log.error_message = str(exc)
			log.completed_at = datetime.utcnow()
			db.session.commit()
			current_app.logger.error(
				"Document pipeline stage failed",
				extra={
					"doc_id": document.id,
					"workspace_id": document.workspace_id,
					"stage": stage,
					"error": str(exc),
				},
			)
			raise


def run_pipeline(document_id: int, *, force: bool = False) -> PipelineResult:
	pipeline = DocumentProcessingPipeline()
	return pipeline.run(document_id, force=force)
