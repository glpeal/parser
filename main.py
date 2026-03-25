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
        self.character: str | None = None  # кастомный промпт персонажа

    def save(self):
        STATE_FILE.write_text(
            json.dumps(
                {
                    "is_active": self.is_active,
                    "channels":  self.channels,
                    "admin_id":  self.admin_id,
                    "min_delay": self.min_delay,
                    "max_delay": self.max_delay,
                    "character": self.character,
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
                self.is_active  = d.get("is_active", False)
                self.channels   = d.get("channels", [])
                self.admin_id   = d.get("admin_id")
                self.min_delay  = d.get("min_delay", 30)
                self.max_delay  = d.get("max_delay", 120)
                self.character  = d.get("character")
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
/list — список каналов
/character add <prompt> — задать характер (на англ.)
/character remove — сбросить характер"""


# ── Groq: общий вызов ─────────────────────────────────────────────────────────
async def _groq_call(system: str, user: str, max_tokens: int = 60, temperature: float = 0.9) -> str:
    from groq import AsyncGroq
    client = AsyncGroq(api_key=GROQ_API_KEY)
    resp = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return resp.choices[0].message.content.strip()


async def generate_comment(post_text: str) -> str:
    """Короткий комментарий к посту канала без знаков препинания."""
    import re

    if state.character:
        system = (
            state.character + "\n\n"
            "Write a short comment in Russian, max 10 words, no punctuation marks at all "
            "(no dots, commas, exclamation marks, question marks, dashes, quotes). "
            "React to the post naturally."
        )
    else:
        system = (
            "Пиши короткие живые комментарии на русском языке под постами в Telegram. "
            "Максимум одно короткое предложение или фраза — не более 10 слов. "
            "Никаких знаков препинания: без точек запятых восклицательных и вопросительных знаков тире скобок кавычек. "
            "Никаких шаблонных фраз. Своё мнение или реакция по теме поста. "
            "Пиши как живой человек в чате — коротко и по делу."
        )

    comment = await _groq_call(system, f"Напиши комментарий к посту:\n\n{post_text[:1000]}")
    comment = re.sub(r'[.!?,;:…\-—–()\[\]{}"\'\'«»]', '', comment).strip()
    return comment


async def generate_reply(user_text: str) -> str:
    """Ответ на личное сообщение с учётом характера."""
    if state.character:
        system = (
            state.character + "\n\n"
            "You are chatting via Telegram. Reply in Russian, naturally and conversationally. "
            "Keep it brief — 1-3 sentences max."
        )
    else:
        system = (
            "Ты дружелюбный собеседник в Telegram. "
            "Отвечай на русском языке — живо, кратко, по делу. "
            "1-3 предложения максимум."
        )

    return await _groq_call(system, user_text, max_tokens=200, temperature=0.85)


async def transcribe_voice(client, message) -> str | None:
    """Скачивает голосовое сообщение и транскрибирует через Groq Whisper."""
    import os
    from groq import AsyncGroq

    path = None
    try:
        path = await message.download()
        groq_client = AsyncGroq(api_key=GROQ_API_KEY)
        with open(path, "rb") as f:
            resp = await groq_client.audio.transcriptions.create(
                file=(os.path.basename(path), f),
                model="whisper-large-v3",
                language="ru",
            )
        return resp.text.strip() or None
    except Exception as e:
        log.error("transcribe_voice error: %s", e)
        return None
    finally:
        if path and os.path.exists(path):
            os.remove(path)


# ── Вступление в канал и группу обсуждений ───────────────────────────────────
async def join_channel_and_discussion(client, username: str) -> tuple[bool, str]:
    """
    Вступает в канал и в его группу обсуждений (linked_chat).
    Возвращает (успех, описание).
    """
    from pyrogram import errors as pyro_errors

    lines = []
    try:
        await client.join_chat(username)
        lines.append(f"вступил в канал @{username}")
    except pyro_errors.UserAlreadyParticipant:
        lines.append(f"уже в канале @{username}")
    except Exception as e:
        return False, str(e)

    # Получаем linked_chat (группа обсуждений)
    try:
        chat = await client.get_chat(username)
        linked = getattr(chat, "linked_chat", None)
        if linked:
            discussion_id = linked.id
            try:
                await client.join_chat(discussion_id)
                discussion_title = getattr(linked, "title", str(discussion_id))
                lines.append(f"вступил в чат обсуждений «{discussion_title}»")
            except pyro_errors.UserAlreadyParticipant:
                discussion_title = getattr(linked, "title", str(discussion_id))
                lines.append(f"уже в чате обсуждений «{discussion_title}»")
            except Exception as e:
                lines.append(f"не удалось вступить в чат обсуждений: {e}")
        else:
            lines.append("группа обсуждений не найдена")
    except Exception as e:
        lines.append(f"ошибка получения linked_chat: {e}")

    return True, "\n".join(lines)


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

    # ── Если не в сессии — отвечаем как AI-собеседник ────────────────────────
    if not admin_sessions.get(user_id):
        has_voice = bool(message.voice)
        if not text and not has_voice:
            return

        from pyrogram.enums import ChatAction

        # Прочитать через 2 сек, потом печатать и отвечать
        await asyncio.sleep(2)
        await client.read_chat_history(message.chat.id)
        await client.send_chat_action(message.chat.id, ChatAction.TYPING)

        try:
            if has_voice:
                input_text = await transcribe_voice(client, message)
                if not input_text:
                    await message.reply("не смог распознать голосовое")
                    return
            else:
                input_text = text

            reply = await generate_reply(input_text)
            await message.reply(reply)
        except Exception as e:
            log.error("generate_reply error: %s", e)
        return

    # ── /status ───────────────────────────────────────────────────────────────
    if text == "/status":
        status = "✅ Активен" if state.is_active else "⛔ Остановлен"
        chs = "\n".join(f"  • @{c}" for c in state.channels) or "  (пусто)"
        char_preview = (state.character[:60] + "…") if state.character and len(state.character) > 60 else (state.character or "не задан")
        await message.reply(
            f"📊 Статус: {status}\n"
            f"⏱ Задержка: {state.min_delay}–{state.max_delay} сек\n"
            f"🎭 Характер: {char_preview}\n"
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
            await message.reply(f"⏳ Вступаю в @{ch}...")
            joined, detail = await join_channel_and_discussion(client, ch)
            if not joined:
                await message.reply(f"❌ Не удалось вступить в @{ch}: {detail}")
            else:
                state.channels.append(ch)
                state.save()
                await message.reply(
                    f"✅ @{ch} добавлен\n{detail}\nВсего каналов: {len(state.channels)}"
                )

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

    # ── /character add <prompt> ───────────────────────────────────────────────
    elif text.startswith("/character add "):
        prompt = text[15:].strip()
        if not prompt:
            await message.reply("❌ Укажите промпт: /character add You are a sarcastic tech blogger")
        else:
            state.character = prompt
            state.save()
            preview = (prompt[:80] + "…") if len(prompt) > 80 else prompt
            await message.reply(f"🎭 Характер установлен:\n{preview}")

    # ── /character remove ─────────────────────────────────────────────────────
    elif text == "/character remove":
        if not state.character:
            await message.reply("⚠️ Характер не был задан")
        else:
            state.character = None
            state.save()
            await message.reply("🎭 Характер сброшен — используется стандартный промпт")

    # ── Неизвестная команда → показать меню ───────────────────────────────────
    else:
        await message.reply(MENU)


# Кэш: username канала → id группы обсуждений
_discussion_cache: dict[str, int] = {}


async def get_discussion_id(client, channel_username: str) -> int | None:
    """Возвращает chat_id группы обсуждений для канала (с кэшем)."""
    if channel_username in _discussion_cache:
        return _discussion_cache[channel_username]
    try:
        chat = await client.get_chat(channel_username)
        linked = getattr(chat, "linked_chat", None)
        if linked:
            _discussion_cache[channel_username] = linked.id
            return linked.id
    except Exception as e:
        log.error("get_discussion_id @%s: %s", channel_username, e)
    return None


# ── Обработчик постов в каналах ───────────────────────────────────────────────
async def handle_post(client, message) -> None:
    try:
        chat_username = getattr(message.chat, "username", None)
        log.info("[пост] chat=%s active=%s channels=%s", chat_username, state.is_active, state.channels)

        if not chat_username:
            log.info("[пост] пропуск — нет username у чата")
            return
        if chat_username.lower() not in state.channels:
            log.info("[пост] пропуск — канал не в списке")
            return
        if not state.is_active:
            log.info("[пост] пропуск — комментинг выключен (отправьте /start)")
            return

        post_text = message.text or message.caption or ""
        if len(post_text) < 50:
            log.info("[пост] пропуск — текст слишком короткий (%d симв.)", len(post_text))
            return

        delay = random.randint(state.min_delay, state.max_delay)
        log.info("[%s] Новый пост — ждём %d сек...", chat_username, delay)
        await asyncio.sleep(delay)

        # Генерация комментария
        comment: str | None = None
        for attempt in range(2):
            try:
                log.info("[%s] Запрос к Groq (попытка %d)...", chat_username, attempt + 1)
                comment = await generate_comment(post_text)
                log.info("[%s] Groq ответил: %s", chat_username, comment[:80])
                break
            except Exception as e:
                log.error("Groq ошибка (попытка %d/2): %s", attempt + 1, e)
                if attempt == 0:
                    await asyncio.sleep(5)

        if not comment:
            log.error("[%s] Не удалось получить комментарий — пропуск", chat_username)
            return

        # Отправляем в группу обсуждений как ответ на пост
        from pyrogram import errors as pyro_errors

        discussion_id = await get_discussion_id(client, chat_username)
        if not discussion_id:
            log.error("[%s] Группа обсуждений не найдена — пропуск", chat_username)
            return

        try:
            await client.send_message(discussion_id, comment, reply_to_message_id=message.id)
        except pyro_errors.FloodWait as e:
            log.warning("FloodWait %d сек — ждём...", e.value)
            await asyncio.sleep(e.value)
            await client.send_message(discussion_id, comment, reply_to_message_id=message.id)
        except Exception as e:
            log.error("[%s] Ошибка отправки в обсуждения: %s", chat_username, e)
            return

        ts = datetime.now().strftime("%H:%M:%S")
        log.info("[%s] @%s → «%s»", ts, chat_username, comment)

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
