"""Bedrock client helpers with resilient retry behavior.

Utilities to initialize AWS Bedrock runtime clients and LangChain LLM/Embeddings
with adaptive retries, exponential backoff, and jitter to smooth over transient
throttling (HTTP 429/503).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Callable, ClassVar, Dict, Optional, Sequence, Tuple, TypeVar

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from flask import current_app
from pydantic import PrivateAttr

from app.config import Config

try:
    from langchain_aws import ChatBedrock, BedrockEmbeddings
except Exception:  # pragma: no cover - dependency import guard
    ChatBedrock = None  # type: ignore
    BedrockEmbeddings = None  # type: ignore

T = TypeVar("T")

logger = logging.getLogger(__name__)

def get_bedrock_runtime_client():
    """Return a configured boto3 Bedrock Runtime client.

    Uses explicit credentials from Config if provided, else falls back
    to standard AWS credential resolution (env vars, profiles, etc.).
    """
    kwargs: Dict[str, Any] = {"region_name": Config.AWS_REGION}

    if Config.AWS_ACCESS_KEY_ID and Config.AWS_SECRET_ACCESS_KEY:
        kwargs.update(
            dict(
                aws_access_key_id=Config.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY,
            )
        )
        if Config.AWS_SESSION_TOKEN:
            kwargs["aws_session_token"] = Config.AWS_SESSION_TOKEN

    kwargs["config"] = BotoConfig(
        retries={
            "mode": "adaptive",
            "max_attempts": Config.BEDROCK_BOTO_MAX_ATTEMPTS,
        }
    )

    return boto3.client("bedrock-runtime", **kwargs)


def is_bedrock_configured() -> bool:
    """Best-effort check whether Bedrock is likely configured.

    We avoid making a network call. We consider it configured if:
    - An AWS region is set, and
    - Either explicit access keys are provided via Config, or
      boto3 can resolve credentials from the default chain.
    """
    try:
        if not Config.AWS_REGION:
            return False

        # Explicit keys present
        if Config.AWS_ACCESS_KEY_ID and Config.AWS_SECRET_ACCESS_KEY:
            return True

        # Try default resolution chain without network calls
        session = boto3.Session()
        creds = session.get_credentials()
        return creds is not None
    except Exception:
        return False
 

def _resolve_logger() -> logging.Logger:
    """Return Flask app logger when available, else module logger."""
    try:
        return current_app.logger  # type: ignore[attr-defined]
    except Exception:
        return logger


def _is_retryable_message(message: str, patterns: Sequence[str]) -> bool:
    lowered = message.lower()
    return any(token in lowered for token in patterns)


if ChatBedrock is not None:

    class _RetryableChatBedrock(ChatBedrock):
        """ChatBedrock variant with configurable retry + jittered backoff."""

        _retry_max_attempts: int = PrivateAttr(default=1)
        _retry_base_delay: float = PrivateAttr(default=0.5)
        _retry_max_delay: float = PrivateAttr(default=0.5)
        _retry_jitter_factor: float = PrivateAttr(default=0.0)

        _retryable_error_codes: ClassVar[Sequence[str]] = (
            "ServiceUnavailableException",
            "ThrottlingException",
            "TooManyRequestsException",
            "RequestTimeout",
            "RequestTimeoutException",
            "ProvisionedThroughputExceededException",
        )
        _retryable_status_codes: ClassVar[Sequence[int]] = (408, 409, 429, 500, 502, 503, 504)
        _retryable_message_tokens: ClassVar[Sequence[str]] = (
            "too many requests",
            "throttl",
            "rate exceeded",
            "service unavailable",
            "temporarily unable"
        )

        def __init__(
            self,
            *,
            retry_max_attempts: Optional[int] = None,
            retry_base_delay: Optional[float] = None,
            retry_max_delay: Optional[float] = None,
            retry_jitter_factor: Optional[float] = None,
            **data: Any,
        ) -> None:
            super().__init__(**data)
            base_delay = max(0.01, retry_base_delay or Config.BEDROCK_RETRY_BASE_DELAY_SECONDS)
            max_delay = max(base_delay, retry_max_delay or Config.BEDROCK_RETRY_MAX_DELAY_SECONDS)
            factor = Config.BEDROCK_RETRY_JITTER_FACTOR if retry_jitter_factor is None else retry_jitter_factor

            object.__setattr__(self, "_retry_max_attempts", max(1, retry_max_attempts or Config.BEDROCK_RETRY_MAX_ATTEMPTS))
            object.__setattr__(self, "_retry_base_delay", base_delay)
            object.__setattr__(self, "_retry_max_delay", max_delay)
            object.__setattr__(self, "_retry_jitter_factor", max(0.0, min(1.0, factor)))

        # ------------- Public ChatModel overrides -------------

        def invoke(self, input: Any, config: Optional[Any] = None, *, stop: Optional[list[str]] = None, **kwargs: Any) -> Any:
            if self._retry_max_attempts <= 1:
                return super().invoke(input, config=config, stop=stop, **kwargs)

            def _call() -> Any:
                return super(_RetryableChatBedrock, self).invoke(input, config=config, stop=stop, **kwargs)

            return self._run_with_retry(_call)

        async def ainvoke(self, input: Any, config: Optional[Any] = None, *, stop: Optional[list[str]] = None, **kwargs: Any) -> Any:
            if self._retry_max_attempts <= 1:
                return await super().ainvoke(input, config=config, stop=stop, **kwargs)

            async def _call() -> Any:
                return await super(_RetryableChatBedrock, self).ainvoke(input, config=config, stop=stop, **kwargs)

            return await self._run_async_with_retry(_call)

        # ------------- Internal helpers -------------

        def _run_with_retry(self, func: Callable[[], T]) -> T:
            attempt = 1
            while True:
                try:
                    return func()
                except Exception as exc:  # pragma: no cover - network dependent
                    if not self._should_retry(exc) or attempt >= self._retry_max_attempts:
                        raise
                    delay = self._backoff_delay(attempt)
                    code, status = self._extract_error_details(exc)
                    self._log_retry_event(code, status, attempt, delay, exc)
                    time.sleep(delay)
                    attempt += 1

        async def _run_async_with_retry(self, func: Callable[[], Any]) -> Any:
            attempt = 1
            while True:
                try:
                    return await func()
                except Exception as exc:  # pragma: no cover - network dependent
                    if not self._should_retry(exc) or attempt >= self._retry_max_attempts:
                        raise
                    delay = self._backoff_delay(attempt)
                    code, status = self._extract_error_details(exc)
                    self._log_retry_event(code, status, attempt, delay, exc, asynchronous=True)
                    await asyncio.sleep(delay)
                    attempt += 1

        def _should_retry(self, exc: Exception) -> bool:
            code, status = self._extract_error_details(exc)
            if code and code in self._retryable_error_codes:
                return True
            if status and status in self._retryable_status_codes:
                return True
            return _is_retryable_message(str(exc), self._retryable_message_tokens)

        @staticmethod
        def _extract_error_details(exc: Exception) -> Tuple[Optional[str], Optional[int]]:
            code: Optional[str] = None
            status: Optional[int] = None
            if isinstance(exc, ClientError):
                error_body = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
                code = error_body.get("Code") or error_body.get("Code".lower())
                metadata = exc.response.get("ResponseMetadata", {}) if hasattr(exc, "response") else {}
                status = metadata.get("HTTPStatusCode")
            else:
                response = getattr(exc, "response", None)
                if isinstance(response, dict):
                    error_body = response.get("Error", {})
                    code = code or error_body.get("Code")
                    metadata = response.get("ResponseMetadata", {})
                    status = status or metadata.get("HTTPStatusCode")
            return code, status

        def _backoff_delay(self, attempt: int) -> float:
            capped = min(self._retry_max_delay, self._retry_base_delay * (2 ** (attempt - 1)))
            if self._retry_jitter_factor <= 0:
                return capped
            jitter_span = capped * self._retry_jitter_factor
            delay = capped + random.uniform(0, jitter_span)
            return min(self._retry_max_delay, delay)

        def _log_retry_event(
            self,
            code: Optional[str],
            status: Optional[int],
            attempt: int,
            delay: float,
            exc: Exception,
            *,
            asynchronous: bool = False,
        ) -> None:
            log = _resolve_logger()
            log.warning(
                "Bedrock request throttled (%s, code=%s, status=%s) – attempt %s/%s, backing off %.2fs%s",
                exc.__class__.__name__,
                code or "unknown",
                status or "n/a",
                attempt,
                self._retry_max_attempts,
                delay,
                " (async)" if asynchronous else "",
            )


def get_chat_llm(model_kwargs: Optional[Dict[str, Any]] = None):
    """Return a configured ChatBedrock Default (Nova Lite) LLM for chat/text generation.

    model_kwargs will be passed to the underlying provider (temperature, max_tokens, etc.).
    """
    if ChatBedrock is None:
        raise RuntimeError("langchain-aws not installed. Please install 'langchain-aws'.")

    client = get_bedrock_runtime_client()
    llm = _RetryableChatBedrock(
        model=Config.BEDROCK_MODEL_ID,
        client=client,
        model_kwargs=model_kwargs or {},
        retry_max_attempts=Config.BEDROCK_RETRY_MAX_ATTEMPTS,
        retry_base_delay=Config.BEDROCK_RETRY_BASE_DELAY_SECONDS,
        retry_max_delay=Config.BEDROCK_RETRY_MAX_DELAY_SECONDS,
        retry_jitter_factor=Config.BEDROCK_RETRY_JITTER_FACTOR,
    )
    return llm
 
def get_chat_llm_pro(model_kwargs: Optional[Dict[str, Any]] = None):
    """Return a configured ChatBedrock Nova Pro LLM for chat/text generation.

    model_kwargs will be passed to the underlying provider (temperature, max_tokens, etc.).
    """
    if ChatBedrock is None:
        raise RuntimeError("langchain-aws not installed. Please install 'langchain-aws'.")

    client = get_bedrock_runtime_client()
    llm = _RetryableChatBedrock(
        model=Config.BEDROCK_NOVA_PRO,
        client=client,
        model_kwargs=model_kwargs or {},
        retry_max_attempts=Config.BEDROCK_RETRY_MAX_ATTEMPTS,
        retry_base_delay=Config.BEDROCK_RETRY_BASE_DELAY_SECONDS,
        retry_max_delay=Config.BEDROCK_RETRY_MAX_DELAY_SECONDS,
        retry_jitter_factor=Config.BEDROCK_RETRY_JITTER_FACTOR,
    )
    return llm


def get_chat_llm_claude(model_kwargs: Optional[Dict[str, Any]] = None):
    """Return a configured ChatBedrock Claude LLM for chat/text generation.

    model_kwargs will be passed to the underlying provider (temperature, max_tokens, etc.).
    """
    if ChatBedrock is None:
        raise RuntimeError("langchain-aws not installed. Please install 'langchain-aws'.")

    client = get_bedrock_runtime_client()
    llm = _RetryableChatBedrock(
        model=Config.BEDROCK_CLAUDE_SONNET,
        client=client,
        model_kwargs=model_kwargs or {},
        retry_max_attempts=Config.BEDROCK_RETRY_MAX_ATTEMPTS,
        retry_base_delay=Config.BEDROCK_RETRY_BASE_DELAY_SECONDS,
        retry_max_delay=Config.BEDROCK_RETRY_MAX_DELAY_SECONDS,
        retry_jitter_factor=Config.BEDROCK_RETRY_JITTER_FACTOR,
    )
    return llm


def get_text_embeddings(model_id: Optional[str] = None):
    """Return BedrockEmbeddings for text embedding generation.

    Default to Titan Text Embeddings v2 if no model specified.
    """
    if BedrockEmbeddings is None:
        raise RuntimeError("langchain-aws not installed. Please install 'langchain-aws'.")

    client = get_bedrock_runtime_client()
    emb = BedrockEmbeddings(
        model_id=model_id or "amazon.titan-embed-text-v2:0",
        region_name=Config.AWS_REGION,
        client=client,
    )
    return emb
