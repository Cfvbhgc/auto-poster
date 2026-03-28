# таски для celery - отправка постов в каналы
# используем requests напрямую чтобы не тащить aiogram в воркер

import json
import requests
from celery import Celery
from config import BOT_TOKEN, REDIS_URL

# создаем celery приложение
app = Celery("auto_poster", broker=REDIS_URL, backend=REDIS_URL)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Moscow",
    enable_utc=True,
)

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# отправка текстового сообщения через апи телеграма
def _send_message(chat_id, text, parse_mode="HTML"):
    resp = requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    })
    return resp.json()


# отправка фото с подписью
def _send_photo(chat_id, photo_file_id, caption="", parse_mode="HTML"):
    resp = requests.post(f"{TELEGRAM_API}/sendPhoto", json={
        "chat_id": chat_id,
        "photo": photo_file_id,
        "caption": caption,
        "parse_mode": parse_mode,
    })
    return resp.json()


@app.task(name="send_post", bind=True, max_retries=3, default_retry_delay=30)
def send_post(self, post_data, channel_ids):
    """
    Основная таска - отправляет пост во все указанные каналы.
    post_data - словарь с полями text, photo_id (опционально)
    channel_ids - список chat_id каналов
    """
    import redis as redis_lib
    r = redis_lib.from_url(REDIS_URL)

    results = {}
    for ch_id in channel_ids:
        try:
            if post_data.get("photo_id"):
                # есть фото - отправляем с картинкой
                result = _send_photo(ch_id, post_data["photo_id"], caption=post_data.get("text", ""))
            else:
                # просто текст
                result = _send_message(ch_id, post_data["text"])

            results[str(ch_id)] = result.get("ok", False)

            # обновляем статистику в редисе
            if result.get("ok"):
                r.hincrby("stats:sent", str(ch_id), 1)
                r.hincrby("stats:total", "count", 1)
        except Exception as exc:
            results[str(ch_id)] = False
            # повторяем если что-то пошло не так
            raise self.retry(exc=exc)

    # помечаем пост как отправленный
    post_id = post_data.get("id")
    if post_id:
        stored = r.get(f"post:{post_id}")
        if stored:
            post = json.loads(stored)
            post["status"] = "sent"
            post["results"] = results
            r.set(f"post:{post_id}", json.dumps(post, ensure_ascii=False))

    return results


@app.task(name="schedule_post")
def schedule_post(post_data, channel_ids, eta_timestamp):
    """
    Планирует отправку поста на определенное время.
    Просто создает send_post таску с eta.
    """
    from datetime import datetime
    eta = datetime.fromtimestamp(eta_timestamp)
    send_post.apply_async(args=[post_data, channel_ids], eta=eta)
    return {"scheduled": True, "eta": str(eta)}
