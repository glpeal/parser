#!/usr/bin/env python3
"""
Telegram Userbot Auto-Commenter — всё в одном файле
Автоматически комментирует посты в каналах с помощью Groq AI.

Запуск:
    pip install -r requirements.txt
    # заполните .env (API_ID, API_HASH, GROQ_API_KEY)
    python main.py

При первом запуске Pyrogram запросит номер телефона и код подтверждения.
Для управления ботом отправьте себе секретный код из ADMIN_CODE —
откроется панель управления.
"""

import asyncio
import json
import logging
import os
import random
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Конфиг ───────────────────────────────────────────────────────────────────
API_ID: int   = int(os.getenv("API_ID", "0"))
API_HASH: str = os.getenv("API_HASH", "")
GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")
ADMIN_CODE    = os.getenv("ADMIN_CODE", ".secret123")
SESSION_NAME  = os.getenv("SESSION_NAME", "commenter_session")

STATE_FILE = Path("state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Состояние ─────────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.is_active: bool      = False
        self.channels: list[str]  = []
        self.admin_id: int | None = None
        self.min_delay: int       = 30
        self.max_delay: int       = 120

    def save(self):
        STATE_FILE.write_text(
            json.dumps(
                {
                    "is_active": self.is_active,
                    "channels":  self.channels,
                    "admin_id":  self.admin_id,
                    "min_delay": self.min_delay,
                    "max_delay": self.max_delay,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def load(self):
        if STATE_FILE.exists():
            try:
                d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self.is_active = d.get("is_active", False)
                self.channels  = d.get("channels", [])
                self.admin_id  = d.get("admin_id")
                self.min_delay = d.get("min_delay", 30)
                self.max_delay = d.get("max_delay", 120)
                log.info(
                    "Состояние загружено: активен=%s, каналов=%d",
                    self.is_active, len(self.channels),
                )
            except Exception as e:
                log.error("Ошибка загрузки state.json: %s", e)


state = State()

# ── Сессии админ-панели ───────────────────────────────────────────────────────
admin_sessions: dict[int, bool] = {}

MENU = """🤖 Панель управления

/status — состояние бота
/add @channel — добавить канал
/remove @channel — удалить канал
/start — начать комментинг
/stop — остановить
/delay 30 120 — задержка (мин макс секунд)
/list — список каналов"""


# ── Groq: генерация комментария ───────────────────────────────────────────────
async def generate_comment(post_text: str) -> str:
    """Генерирует живой комментарий через Groq (llama-3.3-70b-versatile)."""
    from groq import AsyncGroq

    client = AsyncGroq(api_key=GROQ_API_KEY)
    resp = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role": "system",
                "content": (
                    "Пиши живые, естественные комментарии на русском языке под постами в Telegram. "
                    "1–3 предложения. Никаких шаблонных фраз («отличный пост», «спасибо за информацию», «интересно»). "
                    "Добавляй своё мнение, аргумент или задавай уместный вопрос. "
                    "Соответствуй тону поста: если пост серьёзный — серьёзно, если лёгкий — живо."
                ),
            },
            {
                "role": "user",
                "content": f"Напиши комментарий к посту:\n\n{post_text[:1000]}",
            },
        ],
        max_tokens=200,
        temperature=0.8,
    )
    return resp.choices[0].message.content.strip()


# ── Обработчик личных сообщений (админ-панель) ────────────────────────────────
async def handle_admin_message(client, message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return

    text = (message.text or "").strip()

    # ── Секретный триггер ──────────────────────────────────────────────────────
    if text == ADMIN_CODE:
        admin_sessions[user_id] = True
        state.admin_id = user_id
        state.save()
        await message.reply(MENU)
        return

    # ── Только для авторизованных ──────────────────────────────────────────────
    if not admin_sessions.get(user_id):
        return

    # ── /status ───────────────────────────────────────────────────────────────
    if text == "/status":
        status = "✅ Активен" if state.is_active else "⛔ Остановлен"
        chs = "\n".join(f"  • @{c}" for c in state.channels) or "  (пусто)"
        await message.reply(
            f"📊 Статус: {status}\n"
            f"⏱ Задержка: {state.min_delay}–{state.max_delay} сек\n"
            f"📢 Каналы ({len(state.channels)}):\n{chs}"
        )

    # ── /add @channel ─────────────────────────────────────────────────────────
    elif text.startswith("/add "):
        ch = text[5:].strip().lstrip("@").lower()
        if not ch:
            await message.reply("❌ Укажите username канала: /add @channel")
        elif ch in state.channels:
            await message.reply(f"⚠️ @{ch} уже есть в списке")
        else:
            state.channels.append(ch)
            state.save()
            await message.reply(f"✅ @{ch} добавлен\nВсего каналов: {len(state.channels)}")

    # ── /remove @channel ──────────────────────────────────────────────────────
    elif text.startswith("/remove "):
        ch = text[8:].strip().lstrip("@").lower()
        if ch in state.channels:
            state.channels.remove(ch)
            state.save()
            await message.reply(f"✅ @{ch} удалён\nОсталось каналов: {len(state.channels)}")
        else:
            await message.reply(f"❌ @{ch} не найден в списке")

    # ── /start ────────────────────────────────────────────────────────────────
    elif text == "/start":
        if not state.channels:
            await message.reply("⚠️ Сначала добавьте каналы: /add @channel")
        elif state.is_active:
            await message.reply("⚠️ Комментинг уже запущен")
        else:
            state.is_active = True
            state.save()
            await message.reply(
                f"✅ Авто-комментинг запущен\n"
                f"Каналов в работе: {len(state.channels)}\n"
                f"Задержка: {state.min_delay}–{state.max_delay} сек"
            )

    # ── /stop ─────────────────────────────────────────────────────────────────
    elif text == "/stop":
        if not state.is_active:
            await message.reply("⚠️ Комментинг уже остановлен")
        else:
            state.is_active = False
            state.save()
            await message.reply("⛔ Авто-комментинг остановлен")

    # ── /delay min max ────────────────────────────────────────────────────────
    elif text.startswith("/delay "):
        parts = text[7:].strip().split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            await message.reply("❌ Формат: /delay 30 120")
        else:
            mn, mx = int(parts[0]), int(parts[1])
            if mn >= mx:
                await message.reply("❌ Минимум должен быть меньше максимума")
            elif mn < 5:
                await message.reply("❌ Минимальная задержка — 5 секунд")
            else:
                state.min_delay, state.max_delay = mn, mx
                state.save()
                await message.reply(f"✅ Задержка установлена: {mn}–{mx} сек")

    # ── /list ─────────────────────────────────────────────────────────────────
    elif text == "/list":
        if not state.channels:
            await message.reply("📋 Список каналов пуст\nДобавьте: /add @channel")
        else:
            lines = "\n".join(f"{i + 1}. @{c}" for i, c in enumerate(state.channels))
            await message.reply(f"📋 Каналы ({len(state.channels)}):\n{lines}")

    # ── Неизвестная команда → показать меню ───────────────────────────────────
    else:
        await message.reply(MENU)


# ── Обработчик постов в каналах ───────────────────────────────────────────────
async def handle_post(client, message) -> None:
    try:
        chat_username = getattr(message.chat, "username", None)
        if not chat_username:
            return
        if chat_username.lower() not in state.channels:
            return
        if not state.is_active:
            return

        post_text = message.text or message.caption or ""
        if len(post_text) < 50:
            return

        delay = random.randint(state.min_delay, state.max_delay)
        log.info("[%s] Новый пост — ждём %d сек...", chat_username, delay)
        await asyncio.sleep(delay)

        comment: str | None = None
        for attempt in range(2):
            try:
                comment = await generate_comment(post_text)
                break
            except Exception as e:
                log.error("Groq ошибка (попытка %d/2): %s", attempt + 1, e)
                if attempt == 0:
                    await asyncio.sleep(5)

        if not comment:
            log.error("[%s] Не удалось сгенерировать комментарий — пропуск", chat_username)
            return

        from pyrogram import errors as pyro_errors

        try:
            await message.reply(comment)
        except pyro_errors.FloodWait as e:
            log.warning("FloodWait %d сек — ждём...", e.value)
            await asyncio.sleep(e.value)
            await message.reply(comment)

        ts = datetime.now().strftime("%H:%M:%S")
        log.info("[%s] @%s → «%s…»", ts, chat_username, comment[:50])

    except Exception as e:
        log.error("handle_post: неожиданная ошибка — %s", e)


# ── Точка входа ───────────────────────────────────────────────────────────────
async def main() -> None:
    errors = []
    if not API_ID or not API_HASH:
        errors.append("API_ID и API_HASH — получить на https://my.telegram.org → API development tools")
    if not GROQ_API_KEY:
        errors.append("GROQ_API_KEY — получить на https://console.groq.com")
    if errors:
        print("❌ Не заполнены обязательные переменные в .env:")
        for e in errors:
            print(f"   • {e}")
        sys.exit(1)

    state.load()

    from pyrogram import Client, filters

    app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH)

    @app.on_message(filters.private)
    async def _on_private(client, message):
        await handle_admin_message(client, message)

    @app.on_message(filters.channel)
    async def _on_channel(client, message):
        await handle_post(client, message)

    print("=" * 52)
    print("  Telegram Userbot Auto-Commenter")
    print("=" * 52)

    async with app:
        me = await app.get_me()
        print(f"  Аккаунт : {me.first_name} (@{me.username})")
        print(f"  Статус  : {'✅ активен' if state.is_active else '⛔ остановлен'}")
        print(f"  Каналов : {len(state.channels)}")
        print(f"  Задержка: {state.min_delay}–{state.max_delay} сек")
        print(f"  Триггер : отправьте «{ADMIN_CODE}» себе для панели управления")
        print("=" * 52)
        print("  Для остановки нажмите Ctrl+C")

        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n  Остановка...")


if __name__ == "__main__":
    asyncio.run(main())
