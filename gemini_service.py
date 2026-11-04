"""Обёртка над официальным клиентом Google Gen AI (пакет `google-genai`)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Причины, по которым Gemini вернул пустой ответ.
BLOCKED_FINISH_REASONS = {
    "SAFETY",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
    "RECITATION",
    "IMAGE_SAFETY",
}

BLOCKED_REPLY = (
    "🚫 Gemini отказался отвечать на этот запрос "
    "(сработали фильтры безопасности). Попробуйте переформулировать."
)
EMPTY_REPLY = (
    "🤔 Gemini вернул пустой ответ. Попробуйте задать вопрос иначе "
    "или приложить другое изображение."
)
DEFAULT_IMAGE_PROMPT = "Опиши это изображение и выдели главное."


@dataclass(slots=True)
class GeminiReply:
    """Ответ модели вместе с репликами, которые нужно сохранить в историю."""

    text: str
    user_content: types.Content
    model_content: types.Content | None


class GeminiService:
    """Асинхронные запросы к Gemini с учётом истории диалога."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        timeout: float = 120.0,
    ) -> None:
        self._model = model
        self._client = genai.Client(
            api_key=api_key,
            # google-genai ожидает таймаут в миллисекундах.
            http_options=types.HttpOptions(timeout=int(timeout * 1000)),
        )
        self._config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.7,
        )

    @property
    def model(self) -> str:
        return self._model

    async def ask(
        self,
        history: Sequence[types.Content],
        *,
        prompt: str = "",
        image: tuple[bytes, str] | None = None,
    ) -> GeminiReply:
        """Отправить реплику (текст и/или изображение) и вернуть ответ модели."""
        parts: list[types.Part] = []
        cleaned_prompt = prompt.strip()
        if cleaned_prompt:
            parts.append(types.Part(text=cleaned_prompt))
        if image is not None:
            data, mime_type = image
            parts.append(types.Part.from_bytes(data=data, mime_type=mime_type))
        if not parts:
            parts.append(types.Part(text=DEFAULT_IMAGE_PROMPT))

        user_content = types.Content(role="user", parts=parts)
        contents = [*history, user_content]

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=contents,
            config=self._config,
        )

        text = _extract_text(response)
        if not text:
            finish_reason = _finish_reason(response)
            logger.warning("Gemini вернул пустой ответ (finish_reason=%s)", finish_reason)
            text = BLOCKED_REPLY if finish_reason in BLOCKED_FINISH_REASONS else EMPTY_REPLY
            # Пустую реплику модели в историю не пишем.
            return GeminiReply(text=text, user_content=user_content, model_content=None)

        return GeminiReply(
            text=text,
            user_content=user_content,
            model_content=types.Content(role="model", parts=[types.Part(text=text)]),
        )

    async def close(self) -> None:
        """Освободить HTTP-сессию клиента (не критично, но чисто)."""
        close = getattr(self._client.aio, "aclose", None)
        if close is None:
            return
        try:
            await close()
        except Exception:  # pragma: no cover - best effort при остановке
            logger.debug("Не удалось закрыть клиент Gemini", exc_info=True)


def _extract_text(response: types.GenerateContentResponse) -> str:
    """Собрать текст ответа, пропуская «размышления» модели."""
    try:
        text = response.text
    except Exception:  # pragma: no cover - зависит от версии SDK
        text = None
    if isinstance(text, str) and text.strip():
        return text.strip()

    chunks: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "thought", False):
                continue
            part_text = getattr(part, "text", None)
            if part_text:
                chunks.append(part_text)
    return "\n".join(chunks).strip()


def _finish_reason(response: types.GenerateContentResponse) -> str:
    for candidate in getattr(response, "candidates", None) or []:
        reason = getattr(candidate, "finish_reason", None)
        if reason is not None:
            return getattr(reason, "name", str(reason))
    feedback = getattr(response, "prompt_feedback", None)
    reason = getattr(feedback, "block_reason", None)
    return getattr(reason, "name", str(reason or ""))
