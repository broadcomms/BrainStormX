"""LLM enrichment utilities for BrainStormX document processing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Optional

from flask import current_app

from app.config import Config
from app.utils.llm_bedrock import get_chat_llm, is_bedrock_configured


@dataclass(slots=True)
class LLMEnrichmentResult:
	title: str
	description: str
	summary: str
	markdown: str
	tts_script: str
	raw_response: str


def _fallback_summary(text: str) -> LLMEnrichmentResult:
	trimmed = text.strip()
	if len(trimmed) > 2000:
		trimmed = trimmed[:2000]
	title = trimmed.splitlines()[0].strip() if trimmed else "Untitled Document"
	summary = " ".join(trimmed.split()[:120])
	markdown = trimmed
	tts = summary
	return LLMEnrichmentResult(
		title=title or "Untitled Document",
		description=summary,
		summary=summary,
		markdown=markdown,
		tts_script=tts,
		raw_response="fallback",
	)


class DocumentLLMEnricher:
	"""Turns normalized document text into rich metadata using Nova Lite."""

	def __init__(self, *, temperature: float = 0.2) -> None:
		self.temperature = temperature

	def enrich(self, *, text: str, headings: Optional[list[str]] = None) -> LLMEnrichmentResult:
		if not text.strip():
			raise ValueError("Document text is empty; cannot call LLM enrichment")

		if not is_bedrock_configured():
			current_app.logger.warning(
				"Bedrock credentials not configured – falling back to heuristic summary"
			)
			return _fallback_summary(text)

		prompt = self._build_prompt(text=text, headings=headings or [])

		try:
			llm = get_chat_llm({"temperature": self.temperature, "top_p": 0.9})
			response = llm.invoke(prompt)
			raw_text = self._extract_text(response)
		except Exception as exc:  # pragma: no cover - upstream service error
			current_app.logger.exception("Nova Lite enrichment failed: %s", exc)
			return _fallback_summary(text)

		parsed = self._parse_response(raw_text)
		if parsed is None:
			current_app.logger.warning("Failed to parse LLM response; using fallback heuristics")
			return _fallback_summary(text)

		return LLMEnrichmentResult(
			title=parsed.get("title") or "Untitled Document",
			description=parsed.get("description") or parsed.get("summary") or "",
			summary=parsed.get("summary") or "",
			markdown=parsed.get("markdown") or text,
			tts_script=parsed.get("tts_script") or parsed.get("summary") or "",
			raw_response=raw_text,
		)

	def _build_prompt(self, *, text: str, headings: list[str]) -> list[Dict[str, str]]:
		system = {
			"role": "system",
			"content": (
				"You are an expert document analyst helping BrainStormX transform "
				"uploaded files into a rich AI-ready representation. Always return "
				"valid JSON encoded in UTF-8."
			),
		}
		user_content = (
			"Analyze the provided document content and produce the following JSON "
			"object:\n"
			"{\n"
			"  \"title\": <short compelling title>,\n"
			"  \"description\": <1-2 sentence overview>,\n"
			"  \"summary\": <detailed but concise summary>,\n"
			"  \"markdown\": <markdown version preserving structure>,\n"
			"  \"tts_script\": <friendly narration about the document>.\n"
			"}\n\n"
			"Respond with JSON only."
		)
		if headings:
			user_content += "\nHeadings detected: " + ", ".join(headings[:10])
		user_content += "\n\nDocument Content:\n" + text
		user = {"role": "user", "content": user_content}
		return [system, user]

	def _extract_text(self, response) -> str:
		if hasattr(response, "content") and isinstance(response.content, str):
			return response.content
		if isinstance(response, dict) and "content" in response:
			return str(response["content"])
		return str(response)

	def _parse_response(self, raw_text: str) -> Optional[Dict[str, str]]:
		try:
			return json.loads(raw_text)
		except json.JSONDecodeError:
			# attempt to locate JSON block manually
			start = raw_text.find("{")
			end = raw_text.rfind("}")
			if start == -1 or end == -1:
				return None
			try:
				return json.loads(raw_text[start : end + 1])
			except Exception:
				return None


def enrich_document(text: str, headings: Optional[list[str]] = None) -> LLMEnrichmentResult:
	return DocumentLLMEnricher().enrich(text=text, headings=headings)
