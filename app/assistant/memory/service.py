from __future__ import annotations

import inspect
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from flask import current_app, has_app_context
from botocore.exceptions import BotoCoreError, ClientError

from .models import MemoryRetrieval, MemorySnippet
from .settings import AgentCoreMemorySettings

try:
    from bedrock_agentcore.memory import MemoryClient  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    MemoryClient = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


def _get_logger():
    if has_app_context():  # pragma: no branch - light utility
        return current_app.logger
    return logger


class NullMemoryService:
    """Fallback service used when AgentCore memory is disabled or unavailable."""

    enabled: bool = False

    def __init__(self, reason: Optional[str] = None) -> None:
        self.reason = reason or "memory_disabled"

    def retrieve(
        self,
        *,
        query: str,
        workshop_id: int,
        user_id: Optional[int],
        thread_id: Optional[int],
    ) -> MemoryRetrieval:
        return MemoryRetrieval.empty()

    def store(
        self,
        *,
        user_text: Optional[str],
        assistant_text: Optional[str],
        workshop_id: int,
        user_id: Optional[int],
        thread_id: Optional[int],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        return None


class AgentMemoryService:
    """Thin wrapper around the AgentCore Memory client used by the assistant."""

    def __init__(self, settings: AgentCoreMemorySettings) -> None:
        self.settings = settings
        self.enabled = bool(settings.enabled and settings.memory_id)
        self._client: Any = None  # Treated as dynamic; SDK typing varies
        self._create_event_params: set[str] = {"memory_id", "actor_id", "session_id", "messages"}
        self._message_modes: list[str] = ["tuple", "structured"]
        self._supports_metadata_kw = False
        self._supports_attributes_kw = False
        if not self.enabled:
            return
        if MemoryClient is None:
            _get_logger().warning("agent_memory_import_failed", extra={"memory_id": settings.memory_id})
            self.enabled = False
            return
        try:
            self._client = MemoryClient(region_name=settings.region)
            self._inspect_client()
        except Exception as exc:  # pragma: no cover - network availability
            _get_logger().warning(
                "agent_memory_client_init_failed",
                extra={"error": str(exc), "memory_id": settings.memory_id},
            )
            self.enabled = False

    # -- Retrieval -----------------------------------------------------------------
    def retrieve(
        self,
        *,
        query: str,
        workshop_id: int,
        user_id: Optional[int],
        thread_id: Optional[int],
    ) -> MemoryRetrieval:
        if not self.enabled or not self._client:
            return MemoryRetrieval.empty()
        query_text = (query or "").strip()
        if not query_text:
            return MemoryRetrieval.empty()

        actor_id = self._derive_actor_id(user_id, workshop_id)
        session_id = self._derive_session_id(thread_id, workshop_id)

        snippets = []
        errors: list[str] = []
        start = time.time()
        namespaces = self.settings.formatted_namespaces(actor_id, session_id, workshop_id)

        for namespace in namespaces:
            try:
                records = self._client.retrieve_memories(  # type: ignore[attr-defined]
                    memory_id=self.settings.memory_id,
                    namespace=namespace,
                    query=query_text,
                    top_k=self.settings.top_k,
                )
            except (ClientError, BotoCoreError) as exc:  # pragma: no cover
                errors.append(f"{namespace}:{getattr(exc, 'response', str(exc))}")
                self._log("warning", "agent_memory_retrieval_failed", namespace=namespace, error=str(exc))
                continue
            except Exception as exc:  # pragma: no cover
                errors.append(f"{namespace}:{str(exc)}")
                self._log("warning", "agent_memory_retrieval_error", namespace=namespace, error=str(exc))
                continue

            for record in self._iter_records(records):
                snippet = self._coerce_record(namespace, record)
                if snippet:
                    snippets.append(snippet)

        latency_ms = int((time.time() - start) * 1000)

        if self.settings.debug_log and snippets:
            self._log(
                "info",
                "agent_memory_retrieval_success",
                count=len(snippets),
                namespaces=namespaces,
                latency_ms=latency_ms,
            )

        return MemoryRetrieval(
            snippets=snippets,
            namespaces=namespaces,
            latency_ms=latency_ms,
            errors=errors,
        )

    # -- Persistence ----------------------------------------------------------------
    def store(
        self,
        *,
        user_text: Optional[str],
        assistant_text: Optional[str],
        workshop_id: int,
        user_id: Optional[int],
        thread_id: Optional[int],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.enabled or not self._client:
            return

        base_messages = self._build_messages_source(user_text, assistant_text)
        if not base_messages:
            return

        payload_metadata = dict(metadata or {})
        payload_metadata.setdefault("workshop_id", workshop_id)
        payload_metadata.setdefault("thread_id", thread_id)

        actor_id = self._derive_actor_id(user_id, workshop_id)
        session_id = self._derive_session_id(thread_id, workshop_id)

        # Prepare base kwargs and optional namespace
        kwargs: Dict[str, Any] = {
            "memory_id": self.settings.memory_id,
            "actor_id": actor_id,
            "session_id": session_id,
        }
        try:
            ns_list = self.settings.formatted_namespaces(actor_id, session_id, workshop_id)
            if ns_list:
                kwargs["namespace"] = ns_list[0]
        except Exception:
            pass

        metadata_text = None
        metadata_strategy = "none"
        metadata_kwargs: Dict[str, Any] = {}
        if payload_metadata:
            if self._supports_metadata_kw:
                metadata_kwargs["metadata"] = payload_metadata
                metadata_strategy = "metadata_kw"
            elif self._supports_attributes_kw:
                metadata_kwargs["attributes"] = payload_metadata
                metadata_strategy = "attributes_kw"
            else:
                metadata_text = self._format_metadata_system_message(payload_metadata)
                metadata_strategy = "embedded" if metadata_text else "dropped"

        if metadata_kwargs:
            metadata_kwargs = {k: v for k, v in metadata_kwargs.items() if k in self._create_event_params}
            if not metadata_kwargs and metadata_strategy.endswith("_kw"):
                metadata_strategy = "dropped"

        success = False
        success_message_mode = None
        errors: list[str] = []

        message_modes = []
        for mode in self._message_modes:
            if mode not in message_modes:
                message_modes.append(mode)

        for mode in message_modes:
            formatted_messages = self._format_messages(mode, base_messages, metadata_text)
            if not formatted_messages:
                continue
            attempt_kwargs = {key: value for key, value in kwargs.items() if key in self._create_event_params}
            attempt_kwargs.update(metadata_kwargs)
            attempt_kwargs["messages"] = formatted_messages
            try:
                self._client.create_event(**attempt_kwargs)  # type: ignore[attr-defined, call-arg]
                success = True
                success_message_mode = mode
                break
            except TypeError as exc:
                errors.append(f"{mode}:{exc}")
                continue
            except (ClientError, BotoCoreError) as exc:  # pragma: no cover
                self._log("warning", "agent_memory_store_failed", error=str(exc))
                return
            except Exception as exc:  # pragma: no cover
                self._log("warning", "agent_memory_store_error", error=str(exc))
                return

        if not success:
            self._log("warning", "agent_memory_store_error", error="; ".join(errors) or "unknown")
            return

        if self.settings.debug_log:
            extra: Dict[str, Any] = {
                "actor_id": actor_id,
                "session_id": session_id,
                "message_mode": success_message_mode or "unknown",
                "metadata_strategy": metadata_strategy,
            }
            if payload_metadata:
                extra["metadata_keys"] = sorted(payload_metadata.keys())
            if metadata_strategy == "embedded" and metadata_text:
                extra["metadata_embedded"] = True
            elif metadata_strategy == "dropped":
                extra["metadata_dropped"] = True
            self._log("info", "agent_memory_store_success", **extra)

    # -- Internals ------------------------------------------------------------------
    def _inspect_client(self) -> None:
        if not self._client:
            return
        try:
            create_event = getattr(self._client, "create_event")
            signature = inspect.signature(create_event)
        except Exception:
            return

        params = {name for name in signature.parameters.keys() if name != "self"}
        if params:
            self._create_event_params = params

        self._supports_metadata_kw = "metadata" in self._create_event_params
        self._supports_attributes_kw = "attributes" in self._create_event_params

        messages_param = signature.parameters.get("messages")
        if messages_param and messages_param.annotation is not inspect._empty:
            annotation = str(messages_param.annotation)
            if "Dict" in annotation or "Mapping" in annotation:
                self._message_modes = ["structured", "tuple"]
            elif "Tuple" in annotation:
                self._message_modes = ["tuple", "structured"]

    @staticmethod
    def _build_messages_source(
        user_text: Optional[str],
        assistant_text: Optional[str],
    ) -> list[Dict[str, str]]:
        messages: list[Dict[str, str]] = []
        if user_text and user_text.strip():
            messages.append({"role": "user", "text": user_text.strip()})
        if assistant_text and assistant_text.strip():
            messages.append({"role": "assistant", "text": assistant_text.strip()})
        return messages

    def _format_messages(
        self,
        mode: str,
        base_messages: list[Dict[str, str]],
        metadata_text: Optional[str],
    ) -> list[Any]:
        formatted: list[Any] = []
        if mode == "structured":
            for message in base_messages:
                formatted.append(
                    {
                        "role": message["role"],
                        "content": [{"type": "text", "text": message["text"]}],
                    }
                )
            if metadata_text:
                formatted.append(
                    {
                        "role": "other",
                        "content": [{"type": "text", "text": metadata_text}],
                    }
                )
        elif mode == "tuple":
            for message in base_messages:
                formatted.append((message["text"], message["role"].upper()))
            if metadata_text:
                formatted.append((metadata_text, "OTHER"))
        return formatted

    @staticmethod
    def _derive_actor_id(user_id: Optional[int], workshop_id: int) -> str:
        if user_id is not None:
            return f"user-{user_id}"
        return f"workshop-{workshop_id}"

    @staticmethod
    def _derive_session_id(thread_id: Optional[int], workshop_id: int) -> str:
        if thread_id is not None:
            return f"thread-{thread_id}"
        return f"workshop-{workshop_id}"

    def _log(self, level: str, message: str, **kwargs: Any) -> None:
        log = _get_logger()
        if hasattr(log, level):
            getattr(log, level)(message, extra={"memory": kwargs})
        else:  # pragma: no cover - defensive
            log.log(logging.INFO, message, extra={"memory": kwargs})

    @staticmethod
    def _iter_records(records: Any) -> Iterable[Dict[str, Any]]:
        if not records:
            return []
        if isinstance(records, dict):
            return [records]
        if isinstance(records, list):
            return [item for item in records if isinstance(item, dict)]
        return []

    @staticmethod
    def _coerce_record(namespace: str, record: Dict[str, Any]) -> Optional[MemorySnippet]:
        content = record.get("content") if isinstance(record, dict) else None
        if isinstance(content, dict):
            text = content.get("text")
        else:
            text = record.get("text") if isinstance(record.get("text"), str) else None
        text_value = (text or "").strip()
        if not text_value:
            return None
        relevance = record.get("score") if isinstance(record.get("score"), (int, float)) else None
        created_at = record.get("createdAt") or record.get("created_at")
        parsed_ts: Optional[datetime] = None
        if isinstance(created_at, str):
            try:
                parsed_ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                parsed_ts = None
        snippet = MemorySnippet(
            namespace=namespace,
            text=text_value,
            relevance=float(relevance) if relevance is not None else None,
            created_at=parsed_ts,
            raw=record,
        )
        return snippet

    @staticmethod
    def _format_metadata_system_message(metadata: Dict[str, Any]) -> Optional[str]:
        if not metadata:
            return None
        try:
            compact = json.dumps(metadata, sort_keys=True, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            return None
        return f"metadata::{compact}"
