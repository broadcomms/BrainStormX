"""Minimal extract_text implementation backed by PyPDF2 for test usage."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyPDF2 import PdfReader


def extract_text(path: str | bytes | Path, *_, **__) -> str:
    reader = PdfReader(str(path))
    buffers = []
    for page in reader.pages:
        text: Optional[str] = page.extract_text()
        if text:
            buffers.append(text)
    return "\n".join(buffers)
