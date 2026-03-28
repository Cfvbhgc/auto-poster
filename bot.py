#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ===== АВТО-ПОСТЕР БОТ =====
# бот для автоматической публикации постов в телеграм каналы
# умеет: создавать посты, планировать время, отправлять в несколько каналов, показывать статистику
# написано на aiogram 3.x + celery + redis

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta

import redis
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, REDIS_URL, ADMIN_ID, CHANNEL_IDS

# настраиваем логирование
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("auto_poster")

# инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# подключаемся к редису для хранения постов и статистики
r = redis.from_url(REDIS_URL, decode_responses=True)


# состояния FSM для создания поста
class PostCreation(StatesGroup):
    waiting_text = State()        # ждем текст поста
    waiting_photo = State()       # ждем фото (опционально)
    waiting_schedule = State()    # ждем время публикации
    confirm = State()             # подтверждение


# состояния для управления каналами
class ChannelManagement(StatesGroup):
    waiting_channel_id = State()  # ждем id канала для добавления


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

# получить список каналов из редиса (или дефолтные из конфига)
def get_channels():
    stored = r.get("channels")
    if stored:
        return json.loads(stored)
    # если в редисе нет - берем из конфига и сохраняем
    r.set("channels", json.dumps(CHANNEL_IDS))
    return CHANNEL_IDS


# сохранить каналы в редис
def save_channels(channels):
    r.set("channels", json.dumps(channels))


# сохранить пост в редис
def save_post(post_data):
    post_id = post_data["id"]
    r.set(f"post:{post_id}", json.dumps(post_data, ensure_ascii=False))
    # добавляем в список всех постов
    r.lpush("posts:all", post_id)
    return post_id


# получить все посты
def get_all_posts(limit=20):
    post_ids = r.lrange("posts:all", 0, limit - 1)
    posts = []
    for pid in post_ids:
        data = r.get(f"post:{pid}")
        if data:
            posts.append(json.loads(data))
    return posts


# получить запланированные посты (которые еще не отправлены)
def get_scheduled_posts():
    all_posts = get_all_posts(50)
    return [p for p in all_posts if p.get("status") == "scheduled"]


# проверка что пользователь - админ
def is_admin(user_id):
    return user_id == ADMIN_ID


# ==================== ХЭНДЛЕРЫ КОМАНД ====================

@router.message(Command("start"))
async def cmd_start(message: Message):
    """Приветственное сообщение"""
    if not is_admin(message.from_user.id):
        await message.answer("Извини, у тебя нет доступа к этому боту.")
        return

    await message.answer(
        "Привет! Я бот для автопостинга в каналы.\n\n"
        "Вот что я умею:\n"
        "/newpost - создать новый пост\n"
        "/schedule - посмотреть запланированные посты\n"
        "/channels - управление каналами\n"
        "/stats - статистика отправок\n"
        "/help - помощь\n\n"
        "Давай начнем! Жми /newpost чтобы создать пост."
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Справка по командам"""
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "<b>Справка по боту:</b>\n\n"
        "<b>/newpost</b> - начать создание нового поста. "
        "Сначала отправь текст, потом можно прикрепить фото. "
        "После этого выбери время публикации.\n\n"
        "<b>/schedule</b> - список запланированных постов, "
        "которые еще не были отправлены.\n\n"
        "<b>/channels</b> - посмотреть и настроить каналы, "
        "в которые будут отправляться посты.\n\n"
        "<b>/stats</b> - статистика: сколько постов отправлено, "
        "в какие каналы и когда.\n\n"
        "<b>/cancel</b> - отменить текущее действие.",
        parse_mode="HTML"
    )


# ==================== ОТМЕНА ДЕЙСТВИЯ ====================

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена любого текущего действия"""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нечего отменять, ты и так в главном меню.")
        return
    await state.clear()
    await message.answer("Действие отменено. Можешь начать заново.")


# ==================== СОЗДАНИЕ ПОСТА ====================

@router.message(Command("newpost"))
async def cmd_newpost(message: Message, state: FSMContext):
    """Начало создания нового поста"""
    if not is_admin(message.from_user.id):
        return

    await state.set_state(PostCreation.waiting_text)
    await message.answer(
        "Отправь мне текст поста.\n"
        "Можно использовать HTML разметку (bold, italic, ссылки).\n\n"
        "Для отмены жми /cancel"
    )


# получаем текст поста
@router.message(PostCreation.waiting_text, F.text)
async def process_post_text(message: Message, state: FSMContext):
    """Сохраняем текст и предлагаем добавить фото"""
    if message.text.startswith("/"):
        # это команда а не текст поста, пропускаем
        return

    await state.update_data(text=message.text)

    # кнопки - добавить фото или пропустить
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить фото", callback_data="add_photo")],
        [InlineKeyboardButton(text="Без фото, продолжить", callback_data="skip_photo")],
    ])

    await message.answer("Текст сохранен! Хочешь добавить фото к посту?", reply_markup=keyboard)
    await state.set_state(PostCreation.waiting_photo)


# пользователь хочет добавить фото
@router.callback_query(PostCreation.waiting_photo, F.data == "add_photo")
async def ask_for_photo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("Отправь мне фото для поста.")


# пользователь пропускает фото
@router.callback_query(PostCreation.waiting_photo, F.data == "skip_photo")
async def skip_photo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(photo_id=None)
    await state.set_state(PostCreation.waiting_schedule)

    # предлагаем варианты времени
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправить сейчас", callback_data="send_now")],
        [InlineKeyboardButton(text="Через 30 минут", callback_data="send_30m")],
        [InlineKeyboardButton(text="Через 1 час", callback_data="send_1h")],
        [InlineKeyboardButton(text="Через 3 часа", callback_data="send_3h")],
        [InlineKeyboardButton(text="Завтра в 10:00", callback_data="send_tomorrow")],
    ])
    await callback.message.answer("Когда опубликовать пост?", reply_markup=keyboard)


# получаем фото
@router.message(PostCreation.waiting_photo, F.photo)
async def process_post_photo(message: Message, state: FSMContext):
    """Сохраняем file_id фотки"""
    # берем самое большое разрешение (последний элемент)
    photo = message.photo[-1]
    await state.update_data(photo_id=photo.file_id)
    await state.set_state(PostCreation.waiting_schedule)

    # предлагаем варианты времени
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправить сейчас", callback_data="send_now")],
        [InlineKeyboardButton(text="Через 30 минут", callback_data="send_30m")],
        [InlineKeyboardButton(text="Через 1 час", callback_data="send_1h")],
        [InlineKeyboardButton(text="Через 3 часа", callback_data="send_3h")],
        [InlineKeyboardButton(text="Завтра в 10:00", callback_data="send_tomorrow")],
    ])
    await message.answer("Фото сохранено! Когда опубликовать пост?", reply_markup=keyboard)


# обработка выбора времени
@router.callback_query(PostCreation.waiting_schedule, F.data.startswith("send_"))
async def process_schedule(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    data = await state.get_data()
    post_text = data.get("text", "")
    photo_id = data.get("photo_id")

    # считаем время отправки
    now = datetime.now()
    choice = callback.data

    if choice == "send_now":
        send_at = now
        time_label = "сейчас"
    elif choice == "send_30m":
        send_at = now + timedelta(minutes=30)
        time_label = "через 30 минут"
    elif choice == "send_1h":
        send_at = now + timedelta(hours=1)
        time_label = "через 1 час"
    elif choice == "send_3h":
        send_at = now + timedelta(hours=3)
        time_label = "через 3 часа"
    elif choice == "send_tomorrow":
        tomorrow = now + timedelta(days=1)
        send_at = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
        time_label = "завтра в 10:00"
    else:
        send_at = now
        time_label = "сейчас"

    # создаем пост
    post_id = str(uuid.uuid4())[:8]
    channels = get_channels()

    post_data = {
        "id": post_id,
        "text": post_text,
        "photo_id": photo_id,
        "created_at": now.isoformat(),
        "send_at": send_at.isoformat(),
        "status": "scheduled" if choice != "send_now" else "sending",
        "channel_count": len(channels),
    }

    # сохраняем в редис
    save_post(post_data)

    # отправляем через celery
    from tasks import send_post, schedule_post

    if choice == "send_now":
        # отправляем сразу
        send_post.delay(
            {"id": post_id, "text": post_text, "photo_id": photo_id},
            channels
        )
        await callback.message.answer(
            f"Пост отправляется в {len(channels)} канал(ов)!\n"
            f"ID поста: <code>{post_id}</code>",
            parse_mode="HTML"
        )
    else:
        # планируем на потом
        eta_ts = send_at.timestamp()
        schedule_post.delay(
            {"id": post_id, "text": post_text, "photo_id": photo_id},
            channels,
            eta_ts
        )
        await callback.message.answer(
            f"Пост запланирован на {time_label}!\n"
            f"Время: {send_at.strftime('%d.%m.%Y %H:%M')}\n"
            f"Каналов: {len(channels)}\n"
            f"ID поста: <code>{post_id}</code>\n\n"
            f"Посмотреть все запланированные: /schedule",
            parse_mode="HTML"
        )

    await state.clear()


# ==================== РАСПИСАНИЕ ====================

@router.message(Command("schedule"))
async def cmd_schedule(message: Message):
    """Показываем запланированные посты"""
    if not is_admin(message.from_user.id):
        return

    scheduled = get_scheduled_posts()

    if not scheduled:
        await message.answer("Нет запланированных постов. Создай новый через /newpost")
        return

    text = "<b>Запланированные посты:</b>\n\n"
    for i, post in enumerate(scheduled, 1):
        send_at = post.get("send_at", "неизвестно")
        preview = post.get("text", "")[:50]
        has_photo = "с фото" if post.get("photo_id") else "без фото"
        post_id = post.get("id", "?")

        text += (
            f"{i}. <code>{post_id}</code>\n"
            f"   Текст: {preview}...\n"
            f"   Время: {send_at}\n"
            f"   Тип: {has_photo}\n\n"
        )

    # кнопка для отмены постов
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить список", callback_data="refresh_schedule")],
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "refresh_schedule")
async def refresh_schedule(callback: CallbackQuery):
    """Обновляем список запланированных"""
    await callback.answer("Обновляю...")

    scheduled = get_scheduled_posts()
    if not scheduled:
        await callback.message.edit_text("Нет запланированных постов.")
        return

    text = "<b>Запланированные посты:</b>\n\n"
    for i, post in enumerate(scheduled, 1):
        send_at = post.get("send_at", "неизвестно")
        preview = post.get("text", "")[:50]
        has_photo = "с фото" if post.get("photo_id") else "без фото"
        post_id = post.get("id", "?")
        text += f"{i}. <code>{post_id}</code> | {send_at} | {has_photo}\n   {preview}...\n\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить список", callback_data="refresh_schedule")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


# ==================== УПРАВЛЕНИЕ КАНАЛАМИ ====================

@router.message(Command("channels"))
async def cmd_channels(message: Message):
    """Показываем список каналов и кнопки управления"""
    if not is_admin(message.from_user.id):
        return

    channels = get_channels()

    if channels:
        text = "<b>Текущие каналы для публикации:</b>\n\n"
        for i, ch_id in enumerate(channels, 1):
            text += f"{i}. <code>{ch_id}</code>\n"
    else:
        text = "Каналы не настроены. Добавь хотя бы один."

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить канал", callback_data="add_channel")],
        [InlineKeyboardButton(text="Очистить все каналы", callback_data="clear_channels")],
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "add_channel")
async def add_channel_start(callback: CallbackQuery, state: FSMContext):
    """Начинаем процесс добавления канала"""
    await callback.answer()
    await state.set_state(ChannelManagement.waiting_channel_id)
    await callback.message.answer(
        "Отправь мне ID канала (число, начинается с -100...).\n"
        "Бот должен быть администратором в этом канале.\n\n"
        "/cancel для отмены"
    )


@router.message(ChannelManagement.waiting_channel_id, F.text)
async def process_channel_id(message: Message, state: FSMContext):
    """Добавляем канал в список"""
    try:
        channel_id = int(message.text.strip())
    except ValueError:
        await message.answer("Это не похоже на ID канала. Отправь число.")
        return

    channels = get_channels()
    if channel_id in channels:
        await message.answer("Этот канал уже в списке!")
        await state.clear()
        return

    channels.append(channel_id)
    save_channels(channels)
    await state.clear()
    await message.answer(
        f"Канал <code>{channel_id}</code> добавлен!\n"
        f"Всего каналов: {len(channels)}\n\n"
        f"Не забудь добавить бота как администратора в этот канал.",
        parse_mode="HTML"
    )


@router.callback_query(F.data == "clear_channels")
async def clear_channels(callback: CallbackQuery):
    """Удаляем все каналы"""
    await callback.answer()
    save_channels([])
    await callback.message.edit_text("Все каналы удалены. Добавь новые через /channels")


# ==================== СТАТИСТИКА ====================

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Показываем статистику отправок"""
    if not is_admin(message.from_user.id):
        return

    # общее количество отправленных постов
    total = r.hget("stats:total", "count") or "0"

    # статистика по каналам
    per_channel = r.hgetall("stats:sent") or {}

    # считаем посты по статусам
    all_posts = get_all_posts(100)
    sent_count = len([p for p in all_posts if p.get("status") == "sent"])
    scheduled_count = len([p for p in all_posts if p.get("status") == "scheduled"])
    sending_count = len([p for p in all_posts if p.get("status") == "sending"])

    text = (
        "<b>Статистика бота:</b>\n\n"
        f"Всего отправлено сообщений: <b>{total}</b>\n"
        f"Постов создано: <b>{len(all_posts)}</b>\n"
        f"  - отправлено: {sent_count}\n"
        f"  - запланировано: {scheduled_count}\n"
        f"  - в процессе: {sending_count}\n\n"
    )

    if per_channel:
        text += "<b>По каналам:</b>\n"
        for ch_id, count in per_channel.items():
            text += f"  <code>{ch_id}</code>: {count} сообщ.\n"
    else:
        text += "Пока нет данных по каналам."

    # кнопка сброса статистики
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сбросить статистику", callback_data="reset_stats")],
        [InlineKeyboardButton(text="Обновить", callback_data="refresh_stats")],
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "reset_stats")
async def reset_stats(callback: CallbackQuery):
    """Сбрасываем всю статистику"""
    await callback.answer("Статистика сброшена!")
    r.delete("stats:total")
    r.delete("stats:sent")
    await callback.message.edit_text("Статистика сброшена. Смотри /stats")


@router.callback_query(F.data == "refresh_stats")
async def refresh_stats(callback: CallbackQuery):
    """Обновляем статистику"""
    await callback.answer("Обновляю...")

    total = r.hget("stats:total", "count") or "0"
    per_channel = r.hgetall("stats:sent") or {}
    all_posts = get_all_posts(100)
    sent_count = len([p for p in all_posts if p.get("status") == "sent"])
    scheduled_count = len([p for p in all_posts if p.get("status") == "scheduled"])

    text = (
        f"<b>Статистика (обновлено):</b>\n\n"
        f"Всего сообщений: <b>{total}</b>\n"
        f"Постов: {len(all_posts)} (отправлено: {sent_count}, в очереди: {scheduled_count})\n\n"
    )
    if per_channel:
        text += "<b>По каналам:</b>\n"
        for ch_id, count in per_channel.items():
            text += f"  <code>{ch_id}</code>: {count}\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сбросить статистику", callback_data="reset_stats")],
        [InlineKeyboardButton(text="Обновить", callback_data="refresh_stats")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


# ==================== ЗАПУСК БОТА ====================

async def main():
    """Главная функция запуска"""
    logger.info("Запускаем бота...")

    # проверяем подключение к редису
    try:
        r.ping()
        logger.info("Redis подключен")
    except Exception as e:
        logger.error(f"Не могу подключиться к Redis: {e}")
        return

    # удаляем вебхук если был (на всякий случай)
    await bot.delete_webhook(drop_pending_updates=True)

    logger.info("Бот запущен и слушает сообщения...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
