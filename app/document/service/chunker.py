"""Chunking helpers for document processing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from langchain.text_splitter import RecursiveCharacterTextSplitter


@dataclass(slots=True)
class ChunkPayload:
	order: int
	content: str
	metadata: dict


class DocumentChunker:
	def __init__(
		self,
		*,
		chunk_size: int = 1200,
		chunk_overlap: float = 0.1,
		separators: Optional[List[str]] = None,
	) -> None:
		self.splitter = RecursiveCharacterTextSplitter(
			chunk_size=chunk_size,
			chunk_overlap=int(chunk_size * chunk_overlap),
			separators=separators
			or [
				"\n## ",
				"\n### ",
				"\n",
				" ",
			],
		)

	def chunk(self, text: str, *, headings: Optional[List[str]] = None) -> List[ChunkPayload]:
		documents = self.splitter.create_documents([text])
		payloads: List[ChunkPayload] = []
		for index, doc in enumerate(documents):
			payloads.append(
				ChunkPayload(
					order=index,
					content=doc.page_content,
					metadata={
						"order": index,
						"source": "document",
						"headings": headings or [],
					},
				)
			)
		return payloads


def chunk_text(text: str, *, headings: Optional[List[str]] = None) -> List[ChunkPayload]:
	return DocumentChunker().chunk(text, headings=headings)
