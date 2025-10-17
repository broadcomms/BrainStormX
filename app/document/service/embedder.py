"""Embedding providers for document chunks."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, List

from flask import current_app


class EmbeddingError(RuntimeError):
	pass


class EmbeddingProvider:
	"""Common interface for embedding strategies."""

	vector_size: int = 384

	def embed(self, texts: Iterable[str]) -> List[List[float]]:
		raise NotImplementedError


@lru_cache(maxsize=1)
def _load_sentence_transformer(model_name: str):
	try:
		from sentence_transformers import SentenceTransformer
	except Exception as exc:  # pragma: no cover - dependency missing
		raise EmbeddingError(
			"sentence-transformers is required for semantic embeddings. Please install it."
		) from exc
	model = SentenceTransformer(model_name, device="cpu")
	return model


class SentenceTransformerEmbedder(EmbeddingProvider):
	def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
		super().__init__()
		self.model_name = model_name

	def embed(self, texts: Iterable[str]) -> List[List[float]]:
		texts_list = [t if isinstance(t, str) else str(t) for t in texts]
		if not texts_list:
			return []
		model = _load_sentence_transformer(self.model_name)
		embeddings = model.encode(texts_list, show_progress_bar=False, convert_to_numpy=True)
		return embeddings.tolist()


class DummyEmbedder(EmbeddingProvider):
	"""Fallback provider that returns zero vectors for development/testing."""

	def embed(self, texts: Iterable[str]) -> List[List[float]]:  # pragma: no cover - trivial
		texts_list = list(texts)
		return [[0.0 for _ in range(self.vector_size)] for _ in texts_list]


def get_default_embedder() -> EmbeddingProvider:
	try:
		# Keep the model configurable through env, default to MiniLM for CPU usage.
		from os import getenv

		model_name = getenv("DOCUMENT_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
		return SentenceTransformerEmbedder(model_name=model_name)
	except EmbeddingError as exc:
		current_app.logger.warning("Embedding provider unavailable, falling back to dummy vectors: %s", exc)
		return DummyEmbedder()
