"""Простая память диалога: история сообщений в RAM, отдельно для каждого user_id."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from google.genai import types

DROPPED_MEDIA_NOTE = "[вложение удалено из памяти, чтобы не рос расход памяти]"


class DialogueMemory:
    """Хранит последние сообщения диалога каждого пользователя.

    Память живёт в процессе: при перезапуске бота история обнуляется.
    Ограничены и число сообщений, и суммарный объём вложений: PDF и фото
    лежат в памяти целиком, поэтому старые вложения заменяются пометкой.
    """

    def __init__(
        self,
        max_contents: int = 20,
        max_media_bytes: int = 15 * 1024 * 1024,
    ) -> None:
        self._max_contents = max(2, max_contents)
        self._max_media_bytes = max(0, max_media_bytes)
        self._history: dict[int, list[types.Content]] = defaultdict(list)
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, user_id: int) -> asyncio.Lock:
        """Замок пользователя: один запрос за раз, иначе история перемешается."""
        return self._locks[user_id]

    def history(self, user_id: int) -> list[types.Content]:
        """Копия истории — Gemini не должен видеть наш изменяемый список."""
        return list(self._history.get(user_id, ()))

    def extend(self, user_id: int, *contents: types.Content | None) -> None:
        """Дописать сообщения в историю (None пропускается)."""
        items = self._history[user_id]
        items.extend(item for item in contents if item is not None)
        self._trim(items)
        self._drop_old_media(items)

    def clear(self, user_id: int) -> int:
        """Сбросить историю пользователя. Возвращает число удалённых сообщений."""
        return len(self._history.pop(user_id, []))

    def messages_count(self, user_id: int) -> int:
        return len(self._history.get(user_id, ()))

    def _trim(self, items: list[types.Content]) -> None:
        if len(items) > self._max_contents:
            del items[: len(items) - self._max_contents]
        # История должна начинаться с реплики пользователя.
        while items and items[0].role != "user":
            items.pop(0)

    def _drop_old_media(self, items: list[types.Content]) -> None:
        """Заменить самые старые вложения пометками, если превышен объём."""
        total = _media_bytes(items)
        for index, item in enumerate(items):
            if total <= self._max_media_bytes:
                return
            new_parts: list[types.Part] = []
            dropped = False
            for part in item.parts or []:
                inline = getattr(part, "inline_data", None)
                if inline is not None and getattr(inline, "data", None):
                    total -= len(inline.data)
                    new_parts.append(types.Part(text=DROPPED_MEDIA_NOTE))
                    dropped = True
                else:
                    new_parts.append(part)
            if dropped:
                items[index] = types.Content(role=item.role, parts=new_parts)


def _media_bytes(items: list[types.Content]) -> int:
    total = 0
    for item in items:
        for part in item.parts or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                total += len(data)
    return total
