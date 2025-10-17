"""Utilities for managing configurable admin settings."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from flask import current_app


class ConfigManager:
    """Provide read/write access to admin configuration overrides."""

    CONFIG_FILENAME = "admin_overrides.json"

    ENV_KEY_MAP = {
        ("app", "name"): "APP_NAME",
        ("app", "timezone"): "DEFAULT_TIMEZONE",
        ("app", "workshop_default_duration"): "WORKSHOP_DEFAULT_DURATION",
        ("app", "max_workshop_participants"): "MAX_WORKSHOP_PARTICIPANTS",
        ("app", "enable_ai_features"): "ENABLE_AI_FEATURES",
        ("app", "bedrock_model_id"): "BEDROCK_MODEL_ID",
        ("bedrock", "model_id"): "BEDROCK_MODEL_ID",
        ("bedrock", "nova_pro"): "BEDROCK_NOVA_PRO",
        ("bedrock", "image_model"): "BEDROCK_IMAGE_MODEL_ID",
        ("bedrock", "video_model"): "BEDROCK_NOVA_VIDEO",
        ("bedrock", "speech_model"): "BEDROCK_NOVA_SPEECH",
        ("mail", "server"): "MAIL_SERVER",
        ("mail", "port"): "MAIL_PORT",
        ("mail", "username"): "MAIL_USERNAME",
    }

    @classmethod
    def _config_path(cls) -> Path:
        instance_path = Path(current_app.instance_path)
        instance_path.mkdir(parents=True, exist_ok=True)
        return instance_path / cls.CONFIG_FILENAME

    @classmethod
    def load_overrides(cls) -> Dict[str, Dict[str, str]]:
        path = cls._config_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current_app.logger.warning("Invalid admin overrides file detected; ignoring contents.")
            return {}

    @classmethod
    def _save_overrides(cls, overrides: Dict[str, Dict[str, str]]) -> None:
        path = cls._config_path()
        path.write_text(json.dumps(overrides, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _as_bool(value: Optional[str], default: bool = False) -> bool:
        if value is None:
            return default
        return value.lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _decode_value(value: str) -> Any:
        lowered = value.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        if value.isdigit():
            try:
                return int(value)
            except ValueError:
                return value
        try:
            return float(value)
        except ValueError:
            return value

    @classmethod
    def get_environment_config(cls) -> Dict[str, Dict[str, Any]]:
        overrides = cls.load_overrides()
        base: Dict[str, Dict[str, Any]] = {
            "aws": {
                "region": os.getenv("AWS_REGION"),
            },
            "bedrock": {
                "model_id": os.getenv("BEDROCK_MODEL_ID"),
                "nova_pro": os.getenv("BEDROCK_NOVA_PRO"),
                "image_model": os.getenv("BEDROCK_IMAGE_MODEL_ID"),
                "video_model": os.getenv("BEDROCK_NOVA_VIDEO"),
                "speech_model": os.getenv("BEDROCK_NOVA_SPEECH"),
            },
            "mail": {
                "server": os.getenv("MAIL_SERVER"),
                "port": os.getenv("MAIL_PORT"),
                "use_tls": cls._as_bool(os.getenv("MAIL_USE_TLS", "false")),
                "username": os.getenv("MAIL_USERNAME"),
            },
            "app": {
                "name": os.getenv("APP_NAME", "BrainStormX"),
                "debug": os.getenv("FLASK_ENV") == "development",
                "timezone": os.getenv("DEFAULT_TIMEZONE", "UTC"),
                "workshop_default_duration": os.getenv("WORKSHOP_DEFAULT_DURATION"),
                "max_workshop_participants": os.getenv("MAX_WORKSHOP_PARTICIPANTS"),
                "enable_ai_features": cls._as_bool(os.getenv("ENABLE_AI_FEATURES", "true"), True),
                "bedrock_model_id": os.getenv("BEDROCK_MODEL_ID"),
            },
        }

        for section, values in overrides.items():
            section_dict = base.setdefault(section, {})
            for key, stored in values.items():
                section_dict[key] = cls._decode_value(stored)

        return base

    @classmethod
    def update_config(cls, section: str, key: str, value: Any) -> None:
        overrides = cls.load_overrides()
        section_store = overrides.setdefault(section, {})
        normalized = cls._normalize_value(value)

        if normalized is None:
            section_store.pop(key, None)
        else:
            section_store[key] = normalized

        if not section_store:
            overrides.pop(section, None)

        cls._save_overrides(overrides)

        env_key = cls.ENV_KEY_MAP.get((section, key))
        if env_key:
            if normalized is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = normalized

    @staticmethod
    def _normalize_value(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        text = str(value).strip()
        return text or None