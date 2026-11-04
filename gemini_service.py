"""Обёртка над официальным клиентом Google Gen AI (пакет `google-genai`)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Gemini принимает вложение инлайном (без Files API) до 20 МБ на весь запрос;
# держим запас, чтобы вместе с историей диалога не упереться в лимит.
MAX_INLINE_BYTES = 18 * 1024 * 1024

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
    "или приложить другой файл."
)

DEFAULT_IMAGE_PROMPT = "Опиши это изображение и выдели главное."
DEFAULT_PDF_PROMPT = (
    "Проанализируй документ: кратко перескажи содержание, затем выдели ключевые пункты."
)
DEFAULT_TEXT_PROMPT = "Прочитай этот файл и кратко перескажи содержание."


@dataclass(slots=True)
class Attachment:
    """Вложение к запросу: фото или файл (PDF, txt)."""

    data: bytes
    mime_type: str
    file_name: str | None = None

    @property
    def default_prompt(self) -> str:
        if self.mime_type.startswith("image/"):
            return DEFAULT_IMAGE_PROMPT
        if self.mime_type == "application/pdf":
            return DEFAULT_PDF_PROMPT
        return DEFAULT_TEXT_PROMPT

    @property
    def label(self) -> str:
        if self.file_name:
            return f"Файл: {self.file_name}"
        return "Изображение" if self.mime_type.startswith("image/") else "Документ"


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
        attachment: Attachment | None = None,
    ) -> GeminiReply:
        """Отправить реплику (текст и/или файл) и вернуть ответ модели."""
        contents = _fit_inline_limit(history, attachment)

        parts: list[types.Part] = []
        if attachment is not None and attachment.file_name:
            parts.append(types.Part(text=attachment.label))

        cleaned_prompt = prompt.strip()
        if cleaned_prompt:
            parts.append(types.Part(text=cleaned_prompt))
        elif attachment is not None:
            parts.append(types.Part(text=attachment.default_prompt))

        if attachment is not None:
            parts.append(
                types.Part.from_bytes(data=attachment.data, mime_type=attachment.mime_type)
            )

        if not parts:
            parts.append(types.Part(text="Привет! Чем можешь помочь?"))

        user_content = types.Content(role="user", parts=parts)
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=[*contents, user_content],
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


def _inline_bytes(contents: Sequence[types.Content], extra: int = 0) -> int:
    total = extra
    for content in contents:
        for part in content.parts or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                total += len(data)
    return total


def _fit_inline_limit(
    history: Sequence[types.Content],
    attachment: Attachment | None,
) -> list[types.Content]:
    """Уложиться в лимит инлайн-вложений: старые файлы заменяем пометкой.

    История хранится в памяти, и суммарно вложения могут превысить лимит
    Gemini. Тогда самые старые вложения выпадают из запроса — текст диалога
    при этом сохраняется.
    """
    contents = [*history]
    extra = len(attachment.data) if attachment is not None else 0
    if _inline_bytes(contents, extra) <= MAX_INLINE_BYTES:
        return contents

    for index, content in enumerate(contents):
        if _inline_bytes(contents, extra) <= MAX_INLINE_BYTES:
            break
        new_parts: list[types.Part] = []
        dropped = False
        for part in content.parts or []:
            if getattr(part, "inline_data", None) is not None:
                new_parts.append(types.Part(text="[вложение больше не прикладывается]"))
                dropped = True
            else:
                new_parts.append(part)
        if dropped:
            contents[index] = types.Content(role=content.role, parts=new_parts)
            logger.info("Вложение из истории не отправлено: превышен лимит запроса")

    return contents


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
