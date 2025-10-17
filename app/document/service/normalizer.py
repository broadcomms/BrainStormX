"""Utilities for cleaning and enriching document text prior to LLM use."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, List


@dataclass(slots=True)
class NormalizationResult:
	content: str
	headings: List[str]


class DocumentNormalizer:
	"""Applies best-effort normalization while preserving structure."""

	_heading_re = re.compile(r"^(#{1,6}|\d+\.)(\s+)(?P<title>.+)$", re.MULTILINE)

	def normalize(self, text: str) -> NormalizationResult:
		cleaned = self._normalize_whitespace(text)
		cleaned = self._unescape_misencoded_entities(cleaned)
		headings = self._extract_headings(cleaned)
		return NormalizationResult(content=cleaned, headings=headings)

	def _normalize_whitespace(self, text: str) -> str:
		normalized = unicodedata.normalize("NFKC", text)
		normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
		# Collapse 3+ blank lines but maintain paragraph boundaries
		normalized = re.sub(r"\n{3,}", "\n\n", normalized)
		# Strip trailing whitespace on each line
		normalized = "\n".join(line.rstrip() for line in normalized.splitlines())
		return normalized.strip()

	def _unescape_misencoded_entities(self, text: str) -> str:
		entities = {
			"\u2013": "-",
			"\u2014": "-",
			"\u2018": "'",
			"\u2019": "'",
			"\u201c": '"',
			"\u201d": '"',
		}
		for wrong, right in entities.items():
			text = text.replace(wrong, right)
		return text

	def _extract_headings(self, text: str) -> List[str]:
		matches = self._heading_re.findall(text)
		headings: List[str] = []
		if matches:
			for match in matches:
				title = match[2].strip()
				if title:
					headings.append(title)
		else:
			# Fallback heuristic: treat uppercase lines as headings
			for line in text.splitlines():
				candidate = line.strip()
				if len(candidate) > 5 and candidate == candidate.upper():
					headings.append(candidate)
		return headings


def normalize_text(text: str) -> NormalizationResult:
	return DocumentNormalizer().normalize(text)
