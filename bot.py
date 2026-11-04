"""Telegram-бот с Gemini на aiogram 3.x.

Запуск:  python bot.py
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import sys
import time
from pathlib import Path

# Интерпретатор может работать с безопасным sys.path (флаг -P / PYTHONSAFEPATH),
# тогда каталог скрипта в путь не попадает — добавляем его сами, чтобы работали
# импорты config / formatting / gemini_service / storage.
PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

from access import AccessList
from config import Config, ConfigError
from formatting import render_chunks, strip_tags
from gemini_service import Attachment, GeminiService
from storage import DialogueMemory

logger = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Bot API не отдаёт ботам файлы больше 20 МБ — проверяем до скачивания.
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
PDF_MIME = "application/pdf"
TEXT_MIME = "text/plain"
TEXT_EXTENSIONS = (".txt", ".md", ".csv", ".json", ".log", ".yml", ".yaml")

WELCOME_TEXT = (
    "👋 <b>Привет!</b>\n\n"
    "Я бот на базе <b>{model}</b>. Что умею:\n"
    "• отвечать на текстовые вопросы и вести диалог;\n"
    "• разбирать изображения — пришлите фото с подписью или без;\n"
    "• читать документы — PDF и текстовые файлы;\n"
    "• помнить контекст переписки (у каждого пользователя свой).\n\n"
    "Команды: /clear — очистить контекст, /help — подсказка."
)

HELP_TEXT = (
    "<b>Как пользоваться</b>\n\n"
    "1. Напишите вопрос текстом — я отвечу и запомню контекст.\n"
    "2. Пришлите фото (можно с подписью) — опишу, что на нём, "
    "или отвечу на вопрос по картинке.\n"
    "3. Пришлите документ — PDF или текстовый файл (.txt, .md, .csv, .json). "
    "Спрошу про содержание: можно добавить подпись с конкретным вопросом.\n"
    "4. /clear — начать разговор с чистого листа.\n\n"
    "Файл до 20 МБ (лимит Telegram). История хранится в памяти процесса "
    "и пропадает при перезапуске бота."
)

FALLBACK_TEXT = (
    "Пока я понимаю текст, фотографии и документы (PDF, txt).\n"
    "Пришлите вопрос, фото или файл. /help — краткая инструкция."
)

UNSUPPORTED_DOCUMENT_TEXT = (
    "Пока не умею читать такие файлы 🤷\n"
    "Поддерживаются PDF и текстовые файлы: .txt, .md, .csv, .json, .log, .yml."
)

TOO_BIG_TEXT = (
    "Файл больше 20 МБ — Telegram не отдаёт такие файлы ботам.\n"
    "Пришлите версию поменьше или разбейте документ на части."
)

ACCESS_REQUEST_TEXT = (
    "🔒 Доступ к боту — по заявке.\n\n"
    "Ваш Telegram ID: <code>{user_id}</code>\n"
    "Передайте его администратору: после разрешения бот заработает."
)
ACCESS_BLOCKED_TEXT = "⛔ Администратор закрыл вам доступ к боту."
ACCESS_GRANTED_TEXT = "✅ Доступ открыт! Напишите /start — и можно спрашивать."
ACCESS_DENIED_TEXT = "⛔ Заявка на доступ отклонена администратором."
ADMIN_ONLY_TEXT = "Эта команда доступна только администратору."
ALLOW_USAGE = (
    "Использование: <code>/allow 123456789</code>\n"
    "Можно сразу с именем: <code>/allow 123456789 Аня</code>"
)
DENY_USAGE = "Использование: <code>/deny 123456789</code>"

# Заявки, отправленные админам: user_id -> имя (чтобы подписать после «Разрешить»)
_PENDING_USERS: dict[int, str] = {}

UNKNOWN_COMMAND_TEXT = "🤷 Не знаю такой команды. Доступно: /help и /clear."

ERROR_TEXT = "⚠️ Не получилось получить ответ от Gemini. Попробуйте ещё раз чуть позже."
RATE_LIMIT_TEXT = "⏳ Слишком много запросов к Gemini подряд. Подождите минуту и повторите."
TIMEOUT_TEXT = "⌛️ Gemini слишком долго думает. Попробуйте отправить запрос ещё раз."
NO_USER_TEXT = "🤨 Не могу определить автора сообщения, поэтому не могу запомнить контекст."
AUTH_ERROR_TEXT = (
    "🔑 Gemini отклонил API-ключ. Проверьте GEMINI_API_KEY в файле .env "
    "и перезапустите бота."
)


async def cmd_start(message: Message, service: GeminiService, memory: DialogueMemory) -> None:
    memory.clear(message.from_user.id)
    await message.answer(
        WELCOME_TEXT.format(model=service.model), link_preview_options=NO_PREVIEW
    )


async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, link_preview_options=NO_PREVIEW)


async def cmd_clear(message: Message, memory: DialogueMemory) -> None:
    removed = memory.clear(message.from_user.id)
    if removed:
        await message.answer(f"🧹 Контекст очищен: убрано сообщений — {removed}.")
    else:
        await message.answer("🧹 Контекст и так пуст. Задайте новый вопрос.")


async def handle_unknown_command(message: Message) -> None:
    await message.answer(UNKNOWN_COMMAND_TEXT)


async def cmd_allow(message: Message, access: AccessList, admin_ids: set[int]) -> None:
    if not _is_admin(message, admin_ids):
        await message.answer(ADMIN_ONLY_TEXT)
        return
    target = _parse_target(message)
    if target is None:
        await message.answer(ALLOW_USAGE)
        return

    user_id, name = target
    access.allow(user_id, name)
    access.save()
    suffix = f" — {html.escape(name)}" if name else ""
    await message.answer(f"✅ Доступ открыт: <code>{user_id}</code>{suffix}")
    try:
        await message.bot.send_message(user_id, ACCESS_GRANTED_TEXT)
    except Exception:
        logger.info("Пользователь %s ещё не писал боту, уведомление не доставлено", user_id)


async def cmd_deny(message: Message, access: AccessList, admin_ids: set[int]) -> None:
    if not _is_admin(message, admin_ids):
        await message.answer(ADMIN_ONLY_TEXT)
        return
    target = _parse_target(message)
    if target is None:
        await message.answer(DENY_USAGE)
        return

    user_id, _ = target
    access.block(user_id)
    access.save()
    await message.answer(f"⛔ Доступ закрыт: <code>{user_id}</code>")


async def cmd_users(message: Message, access: AccessList, admin_ids: set[int]) -> None:
    if not _is_admin(message, admin_ids):
        await message.answer(ADMIN_ONLY_TEXT)
        return

    lines = ["<b>Доступ к боту</b>", ""]
    if admin_ids:
        admins = ", ".join(f"<code>{user_id}</code>" for user_id in sorted(admin_ids))
        lines.append(f"Админы: {admins}")

    allowed = access.items()
    if allowed:
        lines.append("Разрешены:")
        for user_id, name in allowed:
            suffix = f" — {html.escape(name)}" if name else ""
            lines.append(f"• <code>{user_id}</code>{suffix}")
    else:
        lines.append("Белый список пуст: доступ только у админов.")

    if access.blocked:
        blocked = ", ".join(f"<code>{user_id}</code>" for user_id in sorted(access.blocked))
        lines.extend(["", f"Заблокированы: {blocked}"])

    await message.answer("\n".join(lines), link_preview_options=NO_PREVIEW)


async def on_access_callback(
    callback: CallbackQuery,
    bot: Bot,
    access: AccessList,
    admin_ids: set[int],
) -> None:
    if callback.from_user.id not in admin_ids:
        await callback.answer(ADMIN_ONLY_TEXT, show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in {"allow", "deny"}:
        await callback.answer()
        return
    try:
        user_id = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    name = _PENDING_USERS.pop(user_id, "")
    suffix = f" — {html.escape(name)}" if name else ""
    if parts[1] == "allow":
        access.allow(user_id, name)
        access.save()
        await callback.answer("Доступ открыт")
        text = f"✅ Доступ открыт: <code>{user_id}</code>{suffix}"
        notice = ACCESS_GRANTED_TEXT
    else:
        access.block(user_id)
        access.save()
        await callback.answer("Пользователь заблокирован")
        text = f"⛔ Доступ закрыт: <code>{user_id}</code>{suffix}"
        notice = ACCESS_DENIED_TEXT

    edit = getattr(callback.message, "edit_text", None)
    if edit is not None:
        try:
            await edit(text)
        except TelegramBadRequest:
            logger.debug("Не удалось изменить сообщение заявки", exc_info=True)

    try:
        await bot.send_message(user_id, notice)
    except Exception:
        logger.info("Не удалось уведомить пользователя %s", user_id)


def _is_admin(message: Message, admin_ids: set[int]) -> bool:
    return bool(message.from_user) and message.from_user.id in admin_ids


def _parse_target(message: Message) -> tuple[int, str] | None:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        return None
    raw = parts[1].strip().lstrip("@")
    if not raw.isdigit():
        return None
    name = parts[2].strip() if len(parts) > 2 else ""
    return int(raw), name


async def handle_text(
    message: Message,
    service: GeminiService,
    memory: DialogueMemory,
) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    await _answer(message, service, memory, prompt=text, attachment=None, action=ChatAction.TYPING)


async def handle_photo(
    message: Message,
    bot: Bot,
    service: GeminiService,
    memory: DialogueMemory,
) -> None:
    if not message.photo:
        return

    best_photo = message.photo[-1]
    buffer = io.BytesIO()
    try:
        await bot.download(best_photo, destination=buffer)
    except Exception:
        logger.exception("Не удалось скачать фото (user_id=%s)", message.from_user.id)
        await _safe_answer(message, "😔 Не получилось скачать изображение. Отправьте его ещё раз.")
        return

    # Telegram сжимает фотографии в JPEG.
    attachment = Attachment(data=buffer.getvalue(), mime_type="image/jpeg")
    caption = (message.caption or "").strip()
    await _answer(
        message,
        service,
        memory,
        prompt=caption,
        attachment=attachment,
        action=ChatAction.UPLOAD_PHOTO,
    )


async def handle_document(
    message: Message,
    bot: Bot,
    service: GeminiService,
    memory: DialogueMemory,
) -> None:
    document = message.document
    if document is None:
        return

    mime_type = _document_mime(document)
    if mime_type is None:
        await message.answer(UNSUPPORTED_DOCUMENT_TEXT, link_preview_options=NO_PREVIEW)
        return

    if (document.file_size or 0) > MAX_DOCUMENT_BYTES:
        await message.answer(TOO_BIG_TEXT, link_preview_options=NO_PREVIEW)
        return

    buffer = io.BytesIO()
    try:
        await bot.download(document, destination=buffer)
    except Exception:
        logger.exception("Не удалось скачать документ (user_id=%s)", message.from_user.id)
        await _safe_answer(message, "😔 Не получилось скачать файл. Отправьте его ещё раз.")
        return

    data = buffer.getvalue()
    if len(data) > MAX_DOCUMENT_BYTES:
        await message.answer(TOO_BIG_TEXT, link_preview_options=NO_PREVIEW)
        return

    attachment = Attachment(data=data, mime_type=mime_type, file_name=document.file_name)
    caption = (message.caption or "").strip()
    await _answer(
        message,
        service,
        memory,
        prompt=caption,
        attachment=attachment,
        action=ChatAction.UPLOAD_DOCUMENT,
    )


def _document_mime(document: Document) -> str | None:
    """Определить тип файла: PDF и текст отправляем в Gemini как есть."""
    mime_type = (document.mime_type or "").lower()
    file_name = (document.file_name or "").lower()

    if mime_type == PDF_MIME or file_name.endswith(".pdf"):
        return PDF_MIME
    if mime_type.startswith("text/") or mime_type in {"application/json", "application/xml"}:
        return TEXT_MIME
    if file_name.endswith(TEXT_EXTENSIONS):
        return TEXT_MIME
    return None


async def handle_other(message: Message) -> None:
    await message.answer(FALLBACK_TEXT)


async def _answer(
    message: Message,
    service: GeminiService,
    memory: DialogueMemory,
    *,
    prompt: str,
    attachment: Attachment | None,
    action: ChatAction,
) -> None:
    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        await _safe_answer(message, NO_USER_TEXT)
        return

    sender = ChatActionSender(bot=message.bot, chat_id=message.chat.id, action=action)
    logger.info(
        "Запрос от user_id=%s (%s, история: %s сообщений)",
        user_id,
        _attachment_kind(attachment),
        memory.messages_count(user_id),
    )

    try:
        async with sender, memory.lock(user_id):
            history = memory.history(user_id)
            reply = await service.ask(history, prompt=prompt, attachment=attachment)
            memory.extend(user_id, reply.user_content, reply.model_content)
    except asyncio.TimeoutError:
        logger.warning("Таймаут ответа Gemini (user_id=%s)", user_id)
        await _safe_answer(message, TIMEOUT_TEXT)
        return
    except Exception as error:  # noqa: BLE001 - бот не должен падать из-за одного запроса
        logger.exception("Ошибка запроса к Gemini (user_id=%s)", user_id)
        await _safe_answer(message, _describe_error(error))
        return

    await _send_markdown(message, reply.text)
    logger.info("Ответ отправлен user_id=%s (%s символов)", user_id, len(reply.text))


async def _send_markdown(message: Message, text: str) -> None:
    for chunk in render_chunks(text):
        try:
            await message.answer(
                chunk,
                parse_mode=ParseMode.HTML,
                link_preview_options=NO_PREVIEW,
            )
        except TelegramBadRequest as error:
            logger.warning("Telegram отклонил разметку (%s), отправляю простым текстом", error.message)
            await message.answer(strip_tags(chunk), parse_mode=None, link_preview_options=NO_PREVIEW)


async def _safe_answer(message: Message, text: str) -> None:
    try:
        await message.answer(text)
    except TelegramBadRequest:
        logger.exception("Не удалось отправить сообщение об ошибке")


class AccessMiddleware(BaseMiddleware):
    """Пускает к боту только админов и пользователей из белого списка."""

    def __init__(
        self,
        access: AccessList,
        admin_ids: set[int],
        cooldown: float = 600.0,
    ) -> None:
        self._access = access
        self._admin_ids = admin_ids
        self._cooldown = cooldown
        self._notified_at: dict[int, float] = {}

    async def __call__(self, handler, event, data):  # noqa: ANN001 - сигнатура aiogram
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        text = (getattr(event, "text", None) or "").strip()
        if text.startswith("/whoami"):
            await _safe_answer(event, f"Ваш Telegram ID: <code>{user.id}</code>")
            return None

        if user.id in self._admin_ids or self._access.is_allowed(user.id):
            return await handler(event, data)

        if self._access.is_blocked(user.id):
            await _safe_answer(event, ACCESS_BLOCKED_TEXT)
            return None

        await self._notify_admins(event, user, data.get("bot"))
        await _safe_answer(event, ACCESS_REQUEST_TEXT.format(user_id=user.id))
        return None

    async def _notify_admins(self, message: Message, user, bot) -> None:
        now = time.monotonic()
        last = self._notified_at.get(user.id)
        if last is not None and now - last < self._cooldown:
            return
        self._notified_at[user.id] = now
        if bot is None:
            return

        name = user.full_name or str(user.id)
        _PENDING_USERS[user.id] = name
        username = f"@{user.username}" if user.username else "—"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Разрешить", callback_data=f"access:allow:{user.id}"
                    ),
                    InlineKeyboardButton(
                        text="⛔ Заблокировать", callback_data=f"access:deny:{user.id}"
                    ),
                ]
            ]
        )
        text = (
            "🔔 <b>Заявка на доступ</b>\n\n"
            f"Имя: {html.escape(name)}\n"
            f"Username: {username}\n"
            f"ID: <code>{user.id}</code>"
        )
        for admin_id in sorted(self._admin_ids):
            try:
                await bot.send_message(admin_id, text, reply_markup=keyboard)
            except Exception:
                logger.warning("Заявка не ушла админу %s", admin_id, exc_info=True)


def _attachment_kind(attachment: Attachment | None) -> str:
    if attachment is None:
        return "текст"
    if attachment.mime_type.startswith("image/"):
        return "фото"
    name = f" {attachment.file_name}" if attachment.file_name else ""
    size_mb = len(attachment.data) / (1024 * 1024)
    return f"файл{name}, {size_mb:.1f} МБ"


def _describe_error(error: Exception) -> str:
    text = str(error).upper()
    if "TIMEOUT" in text or "TIMED OUT" in text or "DEADLINE_EXCEEDED" in text:
        return TIMEOUT_TEXT
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return RATE_LIMIT_TEXT
    if (
        "API_KEY" in text
        or "API KEY" in text
        or "401" in text
        or "403" in text
        or "PERMISSION_DENIED" in text
    ):
        return AUTH_ERROR_TEXT
    return ERROR_TEXT


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def main() -> None:
    _configure_logging()
    try:
        config = Config.from_env()
    except ConfigError as error:
        logger.error("%s", error)
        raise SystemExit(1) from error

    bot = Bot(
        token=config.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    memory = DialogueMemory(
        max_contents=config.history_limit,
        max_media_bytes=int(config.history_media_mb * 1024 * 1024),
    )
    service = GeminiService(
        api_key=config.gemini_api_key,
        model=config.gemini_model,
        system_prompt=config.system_prompt,
        timeout=config.request_timeout,
    )

    access = AccessList.load(config.access_file, config.allowed_seed)
    admin_ids = set(config.admin_ids)
    if not admin_ids:
        logger.warning(
            "ADMIN_IDS не задан: фильтр доступа выключен, бот открыт для всех. "
            "Узнать свой ID можно командой /whoami"
        )

    dispatcher = Dispatcher()
    # Значения из workflow_data aiogram подставит в аргументы хендлеров по имени.
    dispatcher["service"] = service
    dispatcher["memory"] = memory
    dispatcher["access"] = access
    dispatcher["admin_ids"] = admin_ids

    if admin_ids:
        dispatcher.message.outer_middleware(AccessMiddleware(access, admin_ids))

    dispatcher.message.register(cmd_start, CommandStart())
    dispatcher.message.register(cmd_help, Command("help"))
    dispatcher.message.register(cmd_clear, Command("clear"))
    dispatcher.message.register(cmd_allow, Command("allow"))
    dispatcher.message.register(cmd_deny, Command("deny"))
    dispatcher.message.register(cmd_users, Command("users"))
    dispatcher.message.register(handle_unknown_command, F.text.startswith("/"))
    dispatcher.message.register(handle_photo, F.photo)
    dispatcher.message.register(handle_document, F.document)
    dispatcher.message.register(handle_text, F.text)
    dispatcher.message.register(handle_other)
    dispatcher.callback_query.register(on_access_callback, F.data.startswith("access:"))

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info(
            "Бот запущен. Модель: %s. Админы: %s, в белом списке: %s",
            config.gemini_model,
            sorted(admin_ids) or "—",
            len(access.allowed),
        )
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        await service.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
    except SystemExit as exit_error:
        sys.exit(exit_error.code)
