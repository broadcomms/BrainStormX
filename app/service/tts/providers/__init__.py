from __future__ import annotations

import os
from .piper_provider import PiperProvider
try:
    from .polly_provider import PollyProvider  # optional
except Exception:  # pragma: no cover - optional dependency
    PollyProvider = None  # type: ignore


def get_provider(name: str | None = None):
    name = (name or os.getenv("TTS_PROVIDER", "piper")).lower()
    if name == "polly" and PollyProvider is not None:
        region = os.getenv("AWS_REGION", "us-east-1")
        return PollyProvider(region=region)
    # Default to Piper
    return PiperProvider(
        piper_bin=os.getenv("PIPER_BIN", "/usr/local/bin/piper"),
        model_path=os.getenv("PIPER_MODEL", "./en_US-amy-medium.onnx"),
    )
