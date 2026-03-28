# Auto-Poster Bot

Телеграм-бот для автоматической публикации постов в каналы.

## Возможности

- Создание постов (текст + фото)
- Планирование публикации на определённое время
- Отправка в несколько каналов одновременно
- Просмотр статистики по отправкам
- Управление списком каналов

## Стек

- Python 3.11
- aiogram 3.x
- Celery + Redis (отложенные задачи)
- Docker Compose

## Быстрый старт

1. Скопировать `.env.example` в `.env` и заполнить:

```bash
cp .env.example .env
```

2. Указать в `.env`:
   - `BOT_TOKEN` — токен бота от @BotFather
   - `ADMIN_ID` — ваш Telegram user ID
   - `CHANNEL_IDS` — ID каналов через запятую (бот должен быть админом)

3. Запустить:

```bash
docker-compose up --build
```

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие |
| `/newpost` | Создать новый пост |
| `/schedule` | Запланированные посты |
| `/channels` | Управление каналами |
| `/stats` | Статистика |
| `/cancel` | Отменить текущее действие |

## Запуск без Docker

```bash
pip install -r requirements.txt

# терминал 1: redis
redis-server

# терминал 2: celery воркер
celery -A tasks worker --loglevel=info

# терминал 3: бот
python bot.py
```

## Структура

```
auto-poster/
├── bot.py              # основной файл бота (хэндлеры, FSM, логика)
├── tasks.py            # Celery таски для отправки постов
├── config.py           # загрузка конфигурации
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```
