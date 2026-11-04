"""Простая память диалога: история сообщений в RAM, отдельно для каждого user_id."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from google.genai import types


class DialogueMemory:
    """Хранит последние сообщения диалога каждого пользователя.

    Память живёт в процессе: при перезапуске бота история обнуляется.
    Размер истории ограничен, чтобы запросы к Gemini не росли бесконечно.
    """

    def __init__(self, max_contents: int = 20) -> None:
        self._max_contents = max(2, max_contents)
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
