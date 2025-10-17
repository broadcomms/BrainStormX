"""Document service package exposing pipeline utilities."""

from .pipeline import DocumentProcessingPipeline, PipelineContext, PipelineResult, run_pipeline
from .extractors import extract_content, ExtractionResult
from .normalizer import normalize_text, NormalizationResult
from .llm_enrichment import enrich_document, LLMEnrichmentResult
from .chunker import chunk_text, ChunkPayload
from .embedder import EmbeddingProvider, get_default_embedder
from .tts_reader import TTSScriptManager, TTSOptions, get_manager

__all__ = [
    "DocumentProcessingPipeline",
    "PipelineContext",
    "PipelineResult",
    "run_pipeline",
    "extract_content",
    "ExtractionResult",
    "normalize_text",
    "NormalizationResult",
    "enrich_document",
    "LLMEnrichmentResult",
    "chunk_text",
    "ChunkPayload",
    "EmbeddingProvider",
    "get_default_embedder",
    "TTSScriptManager",
    "TTSOptions",
    "get_manager",
]
