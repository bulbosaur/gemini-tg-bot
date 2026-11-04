"""Telegram-бот с Gemini на aiogram 3.x.

Запуск:  python bot.py
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
from pathlib import Path

# Интерпретатор может работать с безопасным sys.path (флаг -P / PYTHONSAFEPATH),
# тогда каталог скрипта в путь не попадает — добавляем его сами, чтобы работали
# импорты config / formatting / gemini_service / storage.
PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import LinkPreviewOptions, Message
from aiogram.utils.chat_action import ChatActionSender

from config import Config, ConfigError
from formatting import render_chunks, strip_tags
from gemini_service import GeminiService
from storage import DialogueMemory

logger = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

WELCOME_TEXT = (
    "👋 <b>Привет!</b>\n\n"
    "Я бот на базе <b>{model}</b>. Что умею:\n"
    "• отвечать на текстовые вопросы и вести диалог;\n"
    "• разбирать изображения — пришлите фото с подписью или без;\n"
    "• помнить контекст переписки (у каждого пользователя свой).\n\n"
    "Команды: /clear — очистить контекст, /help — подсказка."
)

HELP_TEXT = (
    "<b>Как пользоваться</b>\n\n"
    "1. Напишите вопрос текстом — я отвечу и запомню контекст.\n"
    "2. Пришлите фото (можно с подписью) — опишу, что на нём, "
    "или отвечу на вопрос по картинке.\n"
    "3. /clear — начать разговор с чистого листа.\n\n"
    "История хранится только в памяти процесса и пропадает при перезапуске бота."
)

FALLBACK_TEXT = (
    "Пока я понимаю только текст и фотографии 🙂\n"
    "Пришлите вопрос текстом или фото. /help — краткая инструкция."
)

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


async def handle_text(
    message: Message,
    service: GeminiService,
    memory: DialogueMemory,
) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    await _answer(message, service, memory, prompt=text, image=None, action=ChatAction.TYPING)


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
    image = (buffer.getvalue(), "image/jpeg")
    caption = (message.caption or "").strip()
    await _answer(message, service, memory, prompt=caption, image=image, action=ChatAction.UPLOAD_PHOTO)


async def handle_other(message: Message) -> None:
    await message.answer(FALLBACK_TEXT)


async def _answer(
    message: Message,
    service: GeminiService,
    memory: DialogueMemory,
    *,
    prompt: str,
    image: tuple[bytes, str] | None,
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
        "фото" if image else "текст",
        memory.messages_count(user_id),
    )

    try:
        async with sender, memory.lock(user_id):
            history = memory.history(user_id)
            reply = await service.ask(history, prompt=prompt, image=image)
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
    memory = DialogueMemory(config.history_limit)
    service = GeminiService(
        api_key=config.gemini_api_key,
        model=config.gemini_model,
        system_prompt=config.system_prompt,
        timeout=config.request_timeout,
    )

    dispatcher = Dispatcher()
    # Значения из workflow_data aiogram подставит в аргументы хендлеров по имени.
    dispatcher["service"] = service
    dispatcher["memory"] = memory
    dispatcher.message.register(cmd_start, CommandStart())
    dispatcher.message.register(cmd_help, Command("help"))
    dispatcher.message.register(cmd_clear, Command("clear"))
    dispatcher.message.register(handle_unknown_command, F.text.startswith("/"))
    dispatcher.message.register(handle_photo, F.photo)
    dispatcher.message.register(handle_text, F.text)
    dispatcher.message.register(handle_other)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот запущен. Модель: %s", config.gemini_model)
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
