"""Список доступа: кого пускать к боту.

Состояние хранится в JSON-файле рядом с приложением (в Docker — на volume),
поэтому переживает перезапуск контейнера. Админы из ADMIN_IDS пускаются всегда
и не могут быть заблокированы через бота.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

ALLOWED = "allowed"
BLOCKED = "blocked"
UNKNOWN = "unknown"


@dataclass
class AccessList:
    """Белый и чёрный списки по Telegram user_id."""

    path: Path
    allowed: dict[int, str] = field(default_factory=dict)
    blocked: set[int] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path, seed: tuple[int, ...] = ()) -> "AccessList":
        access = cls(path=path)
        access._read()
        for user_id in seed:
            access.allowed.setdefault(user_id, "")
        if seed:
            access.save()
        return access

    def status(self, user_id: int) -> str:
        if user_id in self.allowed:
            return ALLOWED
        if user_id in self.blocked:
            return BLOCKED
        return UNKNOWN

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self.allowed

    def is_blocked(self, user_id: int) -> bool:
        return user_id in self.blocked

    def allow(self, user_id: int, name: str = "") -> bool:
        """Разрешить доступ. True — если пользователь был не в списке."""
        was_allowed = user_id in self.allowed
        self.blocked.discard(user_id)
        if name:
            self.allowed[user_id] = name
        else:
            self.allowed.setdefault(user_id, "")
        return not was_allowed

    def block(self, user_id: int) -> None:
        self.allowed.pop(user_id, None)
        self.blocked.add(user_id)

    def unblock(self, user_id: int) -> None:
        self.blocked.discard(user_id)

    def items(self) -> list[tuple[int, str]]:
        return sorted(self.allowed.items(), key=lambda item: item[1] or str(item[0]))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "allowed": {str(user_id): name for user_id, name in self.allowed.items()},
            "blocked": sorted(self.blocked),
        }
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, self.path)
        except OSError:
            logger.exception("Не удалось сохранить список доступа в %s", self.path)

    def _read(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Файл доступа %s повреждён, начинаем с пустого списка", self.path)
            return

        raw_allowed = payload.get("allowed") or {}
        if isinstance(raw_allowed, dict):
            for key, value in raw_allowed.items():
                try:
                    self.allowed[int(key)] = str(value)
                except (TypeError, ValueError):
                    logger.warning("Пропущен некорректный id в списке доступа: %r", key)
        elif isinstance(raw_allowed, list):
            for key in raw_allowed:
                try:
                    self.allowed[int(key)] = ""
                except (TypeError, ValueError):
                    logger.warning("Пропущен некорректный id в списке доступа: %r", key)

        for key in payload.get("blocked") or []:
            try:
                self.blocked.add(int(key))
            except (TypeError, ValueError):
                logger.warning("Пропущен некорректный id в чёрном списке: %r", key)
