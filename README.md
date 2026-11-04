# Gemini Telegram Bot

Telegram-бот, который отвечает через **Gemini 2.5 Flash**: принимает текст и фотографии,
помнит контекст диалога для каждого пользователя и отдаёт ответ с Markdown-разметкой.

Стек: [aiogram 3](https://docs.aiogram.dev/) + официальный SDK
[google-genai](https://github.com/googleapis/python-genai).

## Возможности

- **Текст** → ответ Gemini в Markdown (жирный, курсив, списки, код, таблицы, цитаты).
- **Фото** (с подписью или без) → изображение уходит в Gemini вместе с текстом, ответ возвращается в чат.
- **Память диалога**: история хранится в памяти процесса отдельно для каждого `user_id`
  (последние `HISTORY_LIMIT` сообщений).
- **`/clear`** — сброс контекста текущего пользователя.
- Длинные ответы автоматически разбиваются на несколько сообщений (лимит Telegram — 4096 символов),
  HTML-теги при этом не рвутся. Если Telegram отверг разметку, текст уходит без форматирования.

## 1. Получить токен Telegram

1. Откройте [@BotFather](https://t.me/BotFather) в Telegram.
2. Отправьте `/newbot`, задайте имя и username бота.
3. Скопируйте токен вида `1234567890:AA...` — это `TELEGRAM_BOT_TOKEN`.

## 2. Получить ключ Google Gemini

1. Откройте [Google AI Studio → API keys](https://aistudio.google.com/apikey).
2. Войдите в Google-аккаунт и нажмите **Create API key** (при первом запуске может потребоваться создать проект).
3. Скопируйте ключ вида `AIza...` — это `GEMINI_API_KEY`.
   Ключ бесплатного тарифа подходит для личного использования; лимиты и цены — в
   [документации Gemini](https://ai.google.dev/gemini-api/docs/pricing).

## 3. Установка и запуск

Нужен Python **3.10+**.

```bash
cd gemini_tg_bot
```

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# откройте .env и вставьте свои ключи
python bot.py
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# откройте .env и вставьте свои ключи
python bot.py
```

Если PowerShell не даёт активировать venv, разрешите выполнение скриптов на текущую сессию:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

После запуска в логе появится `Бот запущен. Модель: …` — напишите боту в Telegram.

## 4. Запуск в Docker

Способ без установки Python: всё собирается в образ `python:3.12-slim`.
Нужны только Docker Desktop (Windows/macOS) или docker + docker compose (Linux).

```bash
cd gemini_tg_bot
cp .env.example .env      # Windows: Copy-Item .env.example .env
# откройте .env и вставьте свои ключи

docker compose up -d --build
docker compose logs -f       # в логе будет "Бот запущен. Модель: …"
```

Остановить / обновить / удалить контейнер:

```bash
docker compose down                 # остановить
docker compose up -d --build        # пересобрать после правок кода
docker compose restart              # перезапустить
docker compose down --rmi local     # удалить и контейнер, и образ
```

Без compose то же самое одной командой:

```bash
docker build -t gemini-tg-bot .
docker run -d --name gemini-tg-bot \
  --env-file .env \
  --restart unless-stopped \
  gemini-tg-bot
docker logs -f gemini-tg-bot
```

Ключи внутрь образа **не записываются** — они передаются в контейнер при запуске
(`--env-file` / `env_file`), поэтому один и тот же образ можно поднимать на любом ноуте.

### Перенос на другой ноут

```bash
git clone <адрес-вашего-репозитория>
cd gemini_tg_bot
cp .env.example .env      # вписать TELEGRAM_BOT_TOKEN и GEMINI_API_KEY
docker compose up -d --build
```

Профиль Telegram-бота один: если тот же токен уже запущен где-то ещё (в том числе
`python bot.py` на старом ноуте), новый экземпляр получит `409 Conflict` — сначала
остановите старый (`docker compose down` или `Ctrl+C`).

## Настройки (`.env`)

| Переменная | Обязательна | По умолчанию | Назначение |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | да | — | токен от @BotFather |
| `GEMINI_API_KEY` | да | — | ключ Google AI Studio |
| `GEMINI_MODEL` | нет | `gemini-2.5-flash` | любая модель Gemini, доступная ключу |
| `HISTORY_LIMIT` | нет | `20` | сколько последних сообщений держать в памяти |
| `REQUEST_TIMEOUT` | нет | `120` | таймаут запроса к Gemini, секунды |
| `SYSTEM_PROMPT` | нет | встроенная инструкция | стиль и роль ассистента |
| `LOG_LEVEL` | нет | `INFO` | уровень логирования |

## Команды бота

- `/start` — приветствие и сброс контекста.
- `/help` — краткая инструкция.
- `/clear` — очистить историю текущего пользователя.

## Если модель недоступна

Google выдаёт новым аккаунтам не все модели: `gemini-2.5-flash` может ответить
`404 ... no longer available to new users`, а свежие версии иногда отдают
`503 UNAVAILABLE` при высокой нагрузке. В этом случае смените строку в `.env`
на доступную модель (список отдаёт `client.models.list()`) и перезапустите бота.
Код при этом менять не нужно — модель читается из `GEMINI_MODEL`.

## Структура проекта

```
gemini_tg_bot/
├── bot.py                 # точка входа: aiogram-роутеры и хендлеры
├── config.py              # чтение .env, настройки
├── gemini_service.py      # обёртка над google-genai (текст + изображения)
├── storage.py             # память диалога по user_id
├── formatting.py          # Markdown → HTML для Telegram, разбивка на сообщения
├── requirements.txt
├── Dockerfile             # образ python:3.12-slim, запуск от непривилегированного пользователя
├── docker-compose.yml     # env_file: .env, restart: unless-stopped
├── .dockerignore
├── .env.example
└── README.md
```

## Что важно знать

- История живёт **в оперативной памяти**: после перезапуска бота контекст обнуляется.
  Для постоянного хранения замените `DialogueMemory` на хранилище в SQLite/Redis —
  интерфейс (`history` / `extend` / `clear`) для этого уже выделен отдельным классом.
- Запросы одного пользователя выполняются последовательно (asyncio-lock), поэтому история
  не перемешивается при быстрой отправке нескольких сообщений.
- Изображения передаются в Gemini как JPEG (Telegram сжимает фото) и сохраняются в истории
  диалога, чтобы можно было задавать уточняющие вопросы по картинке.
- Файл `.env` в `.gitignore` — не коммитьте ключи.
