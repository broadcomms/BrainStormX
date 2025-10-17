"""In-memory job scheduler backed by the DocumentProcessingJob table."""

from __future__ import annotations

import threading
from datetime import datetime
from queue import PriorityQueue
from typing import Callable, Optional, Tuple

from flask import current_app

from app.extensions import db, socketio
from app.models import Document, DocumentProcessingJob
from app.document.service.pipeline import DocumentProcessingPipeline, PipelineResult


JobHandler = Callable[[DocumentProcessingJob], PipelineResult]


class DocumentJobScheduler:
	"""Schedules document processing jobs with persistence and worker threads."""

	def __init__(self, *, worker_count: int = 2, job_handler: Optional[JobHandler] = None) -> None:
		self.worker_count = worker_count
		self.job_handler = job_handler or self._default_handler
		self._queue: PriorityQueue[Tuple[int, float, int]] = PriorityQueue()
		self._workers: list[threading.Thread] = []
		self._lock = threading.Lock()
		self._app = None

	# ------------------------------------------------------------------
	# Lifecycle
	# ------------------------------------------------------------------
	def init_app(self, app) -> None:
		self._app = app

	def enqueue(self, document_id: int, *, force: bool = False) -> DocumentProcessingJob:
		if self._app is None:
			raise RuntimeError("DocumentJobScheduler not initialized with Flask app")

		with self._app.app_context():
			if not force:
				existing = (
					DocumentProcessingJob.query.filter_by(document_id=document_id)
					.filter(DocumentProcessingJob.status.in_(["pending", "in_progress"]))
					.first()
				)
				if existing:
					current_app.logger.info("Job already pending for document %s", document_id)
					return existing

			job = DocumentProcessingJob()
			job.document_id = document_id
			job.job_type = "document_force" if force else "document_full"
			job.status = "pending"
			job.priority = 0 if force else 10
			job.created_at = datetime.utcnow()
			db.session.add(job)
			db.session.commit()

			self._queue.put((job.priority, job.created_at.timestamp(), job.id))
			self._ensure_workers()
			return job

	# ------------------------------------------------------------------
	# Worker management
	# ------------------------------------------------------------------
	def _ensure_workers(self) -> None:
		with self._lock:
			while len(self._workers) < self.worker_count:
				worker = threading.Thread(target=self._worker_loop, name="document-job-worker", daemon=True)
				self._workers.append(worker)
				worker.start()

	def _worker_loop(self) -> None:
		assert self._app is not None
		while True:
			priority, _, job_id = self._queue.get()
			with self._app.app_context():
				job = db.session.get(DocumentProcessingJob, job_id)
				if not job:
					self._queue.task_done()
					continue
				if job.status in {"in_progress", "completed"}:
					self._queue.task_done()
					continue
				self._mark_job_started(job)
				try:
					result = self.job_handler(job)
					self._mark_job_completed(job, result)
				except Exception as exc:  # pragma: no cover - failure path
					self._mark_job_failed(job, exc)
				finally:
					self._queue.task_done()

	# ------------------------------------------------------------------
	# Job state transitions
	# ------------------------------------------------------------------
	def _mark_job_started(self, job: DocumentProcessingJob) -> None:
		job.status = "in_progress"
		job.started_at = datetime.utcnow()
		job.attempts = (job.attempts or 0) + 1
		db.session.commit()
		self._emit_progress(job.document_id, stage="queued", status="in_progress")

	def _mark_job_completed(self, job: DocumentProcessingJob, result: PipelineResult) -> None:
		job.status = "completed"
		job.completed_at = datetime.utcnow()
		job.error_message = None
		db.session.commit()
		self._emit_done(job.document_id, result)

	def _mark_job_failed(self, job: DocumentProcessingJob, exc: Exception) -> None:
		db.session.rollback()
		refreshed = db.session.get(DocumentProcessingJob, job.id)
		if not refreshed:
			return
		refreshed.status = "failed"
		refreshed.completed_at = datetime.utcnow()
		refreshed.error_message = str(exc)
		db.session.commit()
		self._emit_failed(refreshed.document_id, str(exc))

	# ------------------------------------------------------------------
	# Default handler
	# ------------------------------------------------------------------
	def _default_handler(self, job: DocumentProcessingJob) -> PipelineResult:
		pipeline = DocumentProcessingPipeline()
		force = job.job_type == "document_force"
		return pipeline.run(job.document_id, force=force)

	# ------------------------------------------------------------------
	# Socket events
	# ------------------------------------------------------------------
	def _emit_progress(self, document_id: int, *, stage: str, status: str) -> None:
		payload = {"documentId": document_id, "stage": stage, "status": status}
		room = self._workspace_room(document_id)
		if room:
			socketio.emit("doc_processing_progress", payload, to=room)
		socketio.emit("doc_processing_progress", payload)

	def _emit_done(self, document_id: int, result: PipelineResult) -> None:
		payload = {
			"documentId": document_id,
			"status": "completed",
			"chunkCount": result.chunk_count,
			"contentSha": result.content_sha256,
			"title": result.title,
			"summary": result.summary,
			"totalPages": result.total_pages,
		}
		room = self._workspace_room(document_id)
		if room:
			socketio.emit("doc_processing_done", payload, to=room)
		socketio.emit("doc_processing_done", payload)

	def _emit_failed(self, document_id: int, error: str) -> None:
		payload = {
			"documentId": document_id,
			"status": "failed",
			"error": error,
		}
		room = self._workspace_room(document_id)
		if room:
			socketio.emit("doc_processing_failed", payload, to=room)
		socketio.emit("doc_processing_failed", payload)

	def _workspace_room(self, document_id: int) -> Optional[str]:
		document = db.session.get(Document, document_id)
		if not document or not document.workspace_id:
			return None
		return f"workspace_{document.workspace_id}"


scheduler = DocumentJobScheduler()


def init_scheduler(app) -> DocumentJobScheduler:
	scheduler.init_app(app)
	return scheduler
