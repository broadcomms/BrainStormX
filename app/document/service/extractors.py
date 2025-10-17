"""Document extraction utilities.

This module provides a small strategy layer for turning uploaded documents into
normalized text that the downstream pipeline can operate on.  The design keeps
the implementation modular so we can mix and match extractors (plain text,
PDF, OCR, etc.) without touching the pipeline itself.

Key goals:
- Offer a consistent return type (`ExtractionResult`).
- Gracefully degrade when optional dependencies (pytesseract, pdf2image) are
  missing – we surface actionable error messages instead of hard crashes.
- Keep the logic side-effect free; all I/O happens against a provided path and
  the caller decides how to persist/cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from flask import current_app


class ExtractionError(RuntimeError):
	"""Raised when a concrete extractor fails irrecoverably."""


@dataclass(slots=True)
class ExtractionResult:
	"""Represents the output of an extractor."""

	content: str
	total_pages: Optional[int] = None
	metadata: Dict[str, object] = field(default_factory=dict)


class BaseExtractor:
	"""Common interface for all extractors."""

	name: str = "base"

	def can_handle(self, *, mime_type: Optional[str], extension: str) -> bool:
		raise NotImplementedError

	def extract(self, path: Path, *, mime_type: Optional[str] = None) -> ExtractionResult:
		raise NotImplementedError


class PlainTextExtractor(BaseExtractor):
	name = "plain-text"

	def can_handle(self, *, mime_type: Optional[str], extension: str) -> bool:
		ext = extension.lower()
		return ext in {"txt", "md", "rtf"} or (mime_type or "").startswith("text/")

	def extract(self, path: Path, *, mime_type: Optional[str] = None) -> ExtractionResult:
		try:
			content = path.read_text(encoding="utf-8")
		except UnicodeDecodeError:
			content = path.read_text(encoding="latin-1")
		return ExtractionResult(content=content, metadata={"extractor": self.name})


class PDFTextExtractor(BaseExtractor):
	name = "pdf-text"

	def can_handle(self, *, mime_type: Optional[str], extension: str) -> bool:
		ext = extension.lower()
		return ext == "pdf" or (mime_type == "application/pdf")

	def extract(self, path: Path, *, mime_type: Optional[str] = None) -> ExtractionResult:
		try:
			from pdfminer.high_level import extract_text
			from pdfminer.pdfparser import PDFSyntaxError
		except Exception as exc:  # pragma: no cover - dependency missing during tests
			raise ExtractionError(
				"pdfminer.six is required to extract text from PDF documents"
			) from exc

		try:
			text = extract_text(str(path))
		except PDFSyntaxError as exc:
			raise ExtractionError("Unable to parse PDF file; it might be corrupt") from exc

		metadata: Dict[str, object] = {"extractor": self.name}
		pages = None
		try:
			from pdfminer.pdfpage import PDFPage

			with path.open("rb") as fp:
				pages = sum(1 for _ in PDFPage.get_pages(fp))
		except Exception:
			pages = None

		if not text.strip():
			raise ExtractionError("PDF text extraction returned empty content")

		return ExtractionResult(content=text, total_pages=pages, metadata=metadata)


class PDFOCRExtractor(BaseExtractor):
	"""Fallback extractor for image-heavy PDFs using OCR."""

	name = "pdf-ocr"

	def can_handle(self, *, mime_type: Optional[str], extension: str) -> bool:
		ext = extension.lower()
		return ext == "pdf" or (mime_type == "application/pdf")

	def extract(self, path: Path, *, mime_type: Optional[str] = None) -> ExtractionResult:
		try:
			import pytesseract
			from pdf2image import convert_from_path
		except Exception as exc:  # pragma: no cover - optional dependency guard
			raise ExtractionError(
				"OCR extraction requires 'pytesseract' and 'pdf2image'. "
				"Install the optional OCR dependencies and ensure the Tesseract"
				" binary is available."
			) from exc

		try:
			images = convert_from_path(str(path))
		except Exception as exc:
			raise ExtractionError("Failed to render PDF pages for OCR") from exc

		content_parts = []
		for index, image in enumerate(images, start=1):
			try:
				text = pytesseract.image_to_string(image)
			except Exception as exc:  # pragma: no cover - upstream OCR failure
				current_app.logger.warning(
					"OCR failed for page %s of %s: %s", index, path.name, exc
				)
				text = ""
			content_parts.append(text)

		combined = "\n".join(part.strip() for part in content_parts if part.strip())
		if not combined:
			raise ExtractionError("OCR could not detect text in the PDF")

		return ExtractionResult(
			content=combined,
			total_pages=len(images),
			metadata={"extractor": self.name, "ocr": True},
		)


class ImageOCRExtractor(BaseExtractor):
	name = "image-ocr"

	def can_handle(self, *, mime_type: Optional[str], extension: str) -> bool:
		ext = extension.lower()
		return ext in {"png", "jpg", "jpeg", "tiff", "bmp"} or (
			mime_type or ""
		).startswith("image/")

	def extract(self, path: Path, *, mime_type: Optional[str] = None) -> ExtractionResult:
		try:
			from PIL import Image
			import pytesseract
		except Exception as exc:  # pragma: no cover - optional dependency guard
			raise ExtractionError(
				"Image OCR requires Pillow and pytesseract. Install both to "
				"enable image-based document ingestion."
			) from exc

		try:
			with Image.open(path) as image:
				text = pytesseract.image_to_string(image)
		except Exception as exc:
			raise ExtractionError("Failed to perform OCR on the supplied image") from exc

		cleaned = text.strip()
		if not cleaned:
			raise ExtractionError("OCR produced no textual content for the image")

		return ExtractionResult(
			content=cleaned,
			metadata={"extractor": self.name, "ocr": True},
		)


class ExtractorRegistry:
	"""Simple ordered registry used by the pipeline."""

	def __init__(self) -> None:
		self._extractors = [
			PlainTextExtractor(),
			PDFTextExtractor(),
			PDFOCRExtractor(),
			ImageOCRExtractor(),
		]

	def select(self, *, mime_type: Optional[str], extension: str) -> BaseExtractor:
		for extractor in self._extractors:
			if extractor.can_handle(mime_type=mime_type, extension=extension):
				return extractor
		raise ExtractionError(f"No extractor registered for extension '{extension}'")


registry = ExtractorRegistry()


def extract_content(path: Path, *, mime_type: Optional[str] = None) -> ExtractionResult:
	extension = path.suffix.lstrip(".")
	extractor = registry.select(mime_type=mime_type, extension=extension)
	return extractor.extract(path, mime_type=mime_type)
