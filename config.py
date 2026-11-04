"""Конфигурация приложения.

Все секреты читаются из файла `.env` (рядом с этим файлом), значения по
умолчанию — из переменных окружения процесса.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"

# override=False: переменные, уже заданные в окружении, имеют приоритет.
load_dotenv(ENV_PATH, override=False)

DEFAULT_MODEL = "gemini-2.5-flash"

DEFAULT_SYSTEM_PROMPT = (
    "Ты — полезный ассистент в Telegram. Отвечай на языке пользователя, "
    "кратко и по существу. Оформляй ответ Markdown: списки, **жирный текст**, "
    "`код` и блоки кода. Не используй таблицы, если можно обойтись списком."
)


class ConfigError(RuntimeError):
    """Не хватает обязательных настроек."""


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Config:
    """Настройки бота, собранные из окружения."""

    telegram_token: str
    gemini_api_key: str
    gemini_model: str = DEFAULT_MODEL
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    history_limit: int = 20
    history_media_mb: float = 15.0
    request_timeout: float = 120.0

    @classmethod
    def from_env(cls) -> "Config":
        token = _env_str("TELEGRAM_BOT_TOKEN")
        api_key = _env_str("GEMINI_API_KEY")

        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", token),
                ("GEMINI_API_KEY", api_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Не заданы обязательные переменные окружения: "
                + ", ".join(missing)
                + f".\nСкопируйте .env.example в {ENV_PATH} и заполните значения."
            )

        return cls(
            telegram_token=token,
            gemini_api_key=api_key,
            gemini_model=_env_str("GEMINI_MODEL", DEFAULT_MODEL),
            system_prompt=_env_str("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
            history_limit=max(2, _env_int("HISTORY_LIMIT", 20)),
            history_media_mb=max(0.0, _env_float("HISTORY_MEDIA_MB", 15.0)),
            request_timeout=max(10.0, _env_float("REQUEST_TIMEOUT", 120.0)),
        )
