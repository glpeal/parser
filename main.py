#!/usr/bin/env python3
"""
Telegram Channel Parser — единый файл
Ищет каналы по теме, фильтрует по наличию комментариев и подписчикам,
экспортирует папку-приглашение t.me/addlist/...
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pyrogram import Client, errors
from pyrogram.raw import functions, types as raw_types

# ---------------------------------------------------------------------------
# Загрузка конфига
# ---------------------------------------------------------------------------

load_dotenv()

API_ID: int = int(os.getenv("API_ID", "0"))
API_HASH: str = os.getenv("API_HASH", "")
MIN_SUBSCRIBERS: int = int(os.getenv("MIN_SUBSCRIBERS", "1000"))
MAX_CHANNELS_PER_FOLDER: int = int(os.getenv("MAX_CHANNELS_PER_FOLDER", "200"))
SESSION_NAME: str = os.getenv("SESSION_NAME", "parser_session")

STORAGE_FILE = Path("channels.json")
LOG_FILE = "errors.log"

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------

def load_storage() -> dict:
    if STORAGE_FILE.exists():
        try:
            with open(STORAGE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_updated": None, "channels": []}


def save_storage(data: dict) -> None:
    data["last_updated"] = datetime.now().isoformat()
    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_saved_usernames() -> list[str]:
    data = load_storage()
    return [ch["username"] for ch in data.get("channels", [])]


def upsert_channels(new_channels: list[dict]) -> None:
    """Добавляет или обновляет каналы в хранилище."""
    data = load_storage()
    existing: dict[str, dict] = {ch["username"]: ch for ch in data.get("channels", [])}
    for ch in new_channels:
        existing[ch["username"]] = ch
    data["channels"] = list(existing.values())
    save_storage(data)


# ---------------------------------------------------------------------------
# parser/tgstat.py  — парсинг tgstat.ru
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


async def parse_tgstat(category_url: str, pages: int = 3) -> list[str]:
    """
    Скрейпит список каналов с tgstat.ru по URL категории.
    Возвращает список @username.
    """
    usernames: list[str] = []
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        for page in range(1, pages + 1):
            url = f"{category_url.rstrip('/')}?page={page}"
            print(f"  [tgstat] Страница {page}: {url}")
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        print(f"  [tgstat] Статус {resp.status}, пропускаем страницу {page}")
                        break
                    html = await resp.text()
            except Exception as e:
                logger.error("tgstat fetch error page %d: %s", page, e)
                print(f"  [tgstat] Ошибка загрузки страницы {page}: {e}")
                break

            soup = BeautifulSoup(html, "html.parser")

            # Ищем ссылки вида /channel/@username или t.me/username
            found_on_page = 0
            for a in soup.find_all("a", href=True):
                href: str = a["href"]
                # Формат: /channel/@cryptonews или /ru/channel/@name
                m = re.search(r"/channel/@([\w]+)", href)
                if m:
                    uname = m.group(1).lower()
                    if uname not in usernames:
                        usernames.append(uname)
                        found_on_page += 1

            # Запасной вариант: t.me/username в тексте
            if found_on_page == 0:
                for text in soup.stripped_strings:
                    m = re.search(r"@([\w]{5,})", text)
                    if m:
                        uname = m.group(1).lower()
                        if uname not in usernames:
                            usernames.append(uname)
                            found_on_page += 1

            print(f"  [tgstat] Найдено на странице: {found_on_page}, всего: {len(usernames)}")
            if found_on_page == 0:
                print("  [tgstat] Пусто, останавливаем.")
                break

            await asyncio.sleep(random.uniform(1.5, 3.0))

    return usernames


# ---------------------------------------------------------------------------
# parser/tg_search.py  — поиск через Telegram
# ---------------------------------------------------------------------------

async def search_telegram(client: Client, query: str, limit: int = 50) -> list[str]:
    """
    Ищет каналы через глобальный поиск Telegram.
    Возвращает список username.
    """
    usernames: list[str] = []
    print(f"  [tg_search] Запрос: «{query}», лимит {limit}")
    try:
        async for chat in client.search_global(query, limit=limit):
            if chat.type.value in ("channel", "supergroup"):
                if chat.username:
                    usernames.append(chat.username.lower())
                else:
                    usernames.append(str(chat.id))
            await asyncio.sleep(0.1)
    except errors.FloodWait as e:
        print(f"  [tg_search] FloodWait {e.value}с, ждём...")
        await asyncio.sleep(e.value)
    except Exception as e:
        logger.error("search_telegram error: %s", e)
        print(f"  [tg_search] Ошибка: {e}")

    print(f"  [tg_search] Найдено каналов: {len(usernames)}")
    return usernames


# ---------------------------------------------------------------------------
# parser/checker.py  — проверка наличия комментариев и подписчиков
# ---------------------------------------------------------------------------

async def filter_channels_with_comments(
    client: Client,
    usernames: list[str],
) -> list[dict]:
    """
    Проверяет каждый канал:
      - есть linked_chat (комментарии включены)
      - members_count >= MIN_SUBSCRIBERS
    Возвращает список словарей с метаданными.
    """
    result: list[dict] = []
    total = len(usernames)
    for idx, username in enumerate(usernames, 1):
        print(f"  [{idx}/{total}] @{username} ...", end=" ", flush=True)
        try:
            chat = await client.get_chat(username)
            members = getattr(chat, "members_count", 0) or 0
            linked = getattr(chat, "linked_chat", None)
            has_comments = linked is not None

            if members < MIN_SUBSCRIBERS:
                print(f"мало подписчиков ({members}), пропуск")
            elif not has_comments:
                print(f"нет комментариев, пропуск")
            else:
                title = getattr(chat, "title", username)
                print(f"OK — «{title}» ({members} подп.)")
                result.append(
                    {
                        "username": username,
                        "title": title,
                        "subscribers": members,
                        "has_comments": True,
                    }
                )
        except errors.FloodWait as e:
            print(f"FloodWait {e.value}с ...")
            await asyncio.sleep(e.value)
            # повторная попытка
            try:
                chat = await client.get_chat(username)
                members = getattr(chat, "members_count", 0) or 0
                linked = getattr(chat, "linked_chat", None)
                if members >= MIN_SUBSCRIBERS and linked is not None:
                    result.append(
                        {
                            "username": username,
                            "title": getattr(chat, "title", username),
                            "subscribers": members,
                            "has_comments": True,
                        }
                    )
            except Exception:
                pass
        except errors.UsernameNotOccupied:
            print("не найден, пропуск")
        except errors.ChannelPrivate:
            print("приватный, пропуск")
        except Exception as e:
            logger.error("checker error @%s: %s", username, e)
            print(f"ошибка: {e}")

        await asyncio.sleep(random.uniform(1.0, 2.5))

    return result


# ---------------------------------------------------------------------------
# folder/creator.py  — создание папки и ссылки-приглашения
# ---------------------------------------------------------------------------

async def _get_next_filter_id(client: Client) -> int:
    """Возвращает первый свободный ID фильтра диалогов."""
    try:
        result = await client.invoke(functions.messages.GetDialogFilters())
        used_ids = set()
        for f in result.filters:
            if hasattr(f, "id"):
                used_ids.add(f.id)
        for fid in range(2, 256):
            if fid not in used_ids:
                return fid
    except Exception as e:
        logger.error("get_next_filter_id: %s", e)
    return 2


async def create_folder_invite(
    client: Client,
    usernames: list[str],
    title: str,
) -> str:
    """
    Создаёт папку Telegram и возвращает ссылку-приглашение t.me/addlist/...
    """
    if not usernames:
        raise ValueError("Список каналов пуст")

    capped = usernames[:MAX_CHANNELS_PER_FOLDER]
    print(f"  [folder] Разрешаем {len(capped)} peer'ов...")

    peers = []
    for uname in capped:
        try:
            peer = await client.resolve_peer(uname)
            peers.append(peer)
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error("resolve_peer @%s: %s", uname, e)
            print(f"  [folder] Не удалось разрешить @{uname}: {e}")

    if not peers:
        raise ValueError("Не удалось разрешить ни одного peer")

    filter_id = await _get_next_filter_id(client)
    print(f"  [folder] Создаём папку (filter_id={filter_id}) «{title}»...")

    # Создаём фильтр
    dialog_filter = raw_types.DialogFilter(
        id=filter_id,
        title=title,
        pinned_peers=[],
        include_peers=peers,
        exclude_peers=[],
        contacts=False,
        non_contacts=False,
        groups=False,
        broadcasts=True,
        bots=False,
        exclude_muted=False,
        exclude_read=False,
        exclude_archived=False,
    )
    await client.invoke(
        functions.messages.UpdateDialogFilter(id=filter_id, filter=dialog_filter)
    )
    print("  [folder] Фильтр создан, генерируем ссылку...")

    # Экспортируем ссылку
    result = await client.invoke(
        functions.chatlists.ExportChatlistInvite(
            chatlist=raw_types.InputChatlistDialogFilter(filter_id=filter_id),
            title=title,
            peers=peers,
        )
    )
    return result.invite.url


# ---------------------------------------------------------------------------
# Вспомогательный вывод
# ---------------------------------------------------------------------------

def print_channels_table(channels: list[dict]) -> None:
    if not channels:
        print("  Нет сохранённых каналов.")
        return
    print(f"\n  {'#':<4} {'Username':<30} {'Подписчики':>12}  {'Комментарии'}")
    print("  " + "-" * 65)
    for i, ch in enumerate(channels, 1):
        comments = "✓" if ch.get("has_comments") else "—"
        title = ch.get("title", "")[:28]
        print(
            f"  {i:<4} @{ch['username']:<28} {ch['subscribers']:>12}  {comments}  {title}"
        )
    print(f"\n  Итого: {len(channels)} каналов")


# ---------------------------------------------------------------------------
# Меню
# ---------------------------------------------------------------------------

def print_menu() -> None:
    print(
        "\n"
        "╔══════════════════════════════════════╗\n"
        "║   Telegram Channel Parser            ║\n"
        "╠══════════════════════════════════════╣\n"
        "║  1. Поиск по ключевому слову         ║\n"
        "║  2. Парсинг с tgstat.ru              ║\n"
        "║  3. Перепроверить сохранённые каналы ║\n"
        "║  4. Создать ссылку-приглашение папки ║\n"
        "║  5. Показать сохранённые каналы      ║\n"
        "║  6. Выход                            ║\n"
        "╚══════════════════════════════════════╝"
    )


async def menu_search_keyword(client: Client) -> None:
    query = input("  Введите ключевое слово: ").strip()
    if not query:
        print("  Пустой запрос, отмена.")
        return
    limit = input("  Лимит каналов (по умолчанию 50): ").strip()
    limit = int(limit) if limit.isdigit() else 50

    print("\n  Поиск в Telegram...")
    usernames = await search_telegram(client, query, limit)

    print(f"\n  Проверяем {len(usernames)} каналов на комментарии и подписчиков...")
    found = await filter_channels_with_comments(client, usernames)

    if found:
        upsert_channels(found)
        print(f"\n  Сохранено {len(found)} каналов.")
    else:
        print("\n  Подходящих каналов не найдено.")


async def menu_parse_tgstat(client: Client) -> None:
    url = input("  Введите URL категории tgstat.ru\n  (например https://tgstat.ru/ru/news): ").strip()
    if not url:
        print("  Пустой URL, отмена.")
        return
    pages_str = input("  Количество страниц (по умолчанию 3): ").strip()
    pages = int(pages_str) if pages_str.isdigit() else 3

    print(f"\n  Парсим tgstat.ru ({pages} стр.)...")
    usernames = await parse_tgstat(url, pages)

    print(f"\n  Проверяем {len(usernames)} каналов...")
    found = await filter_channels_with_comments(client, usernames)

    if found:
        upsert_channels(found)
        print(f"\n  Сохранено {len(found)} каналов.")
    else:
        print("\n  Подходящих каналов не найдено.")


async def menu_recheck(client: Client) -> None:
    data = load_storage()
    channels = data.get("channels", [])
    if not channels:
        print("  Нет сохранённых каналов для проверки.")
        return

    usernames = [ch["username"] for ch in channels]
    print(f"\n  Перепроверяем {len(usernames)} каналов...")
    found = await filter_channels_with_comments(client, usernames)
    upsert_channels(found)
    print(f"\n  Обновлено {len(found)} каналов.")


async def menu_create_folder(client: Client) -> None:
    data = load_storage()
    channels = [ch for ch in data.get("channels", []) if ch.get("has_comments")]
    if not channels:
        print("  Нет подходящих каналов (с комментариями).")
        return

    title = input("  Название папки: ").strip() or "Мои каналы"
    usernames = [ch["username"] for ch in channels]
    print(f"\n  Создаём папку «{title}» из {len(usernames)} каналов...")

    try:
        link = await create_folder_invite(client, usernames, title)
        print(f"\n  ✅ Ссылка готова:\n  {link}")
    except Exception as e:
        logger.error("create_folder_invite: %s", e)
        print(f"\n  Ошибка при создании папки: {e}")


def menu_show_channels() -> None:
    data = load_storage()
    channels = data.get("channels", [])
    updated = data.get("last_updated", "—")
    print(f"\n  Последнее обновление: {updated}")
    print_channels_table(channels)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main() -> None:
    if API_ID == 0 or not API_HASH:
        print(
            "Ошибка: API_ID и API_HASH не заданы.\n"
            "Скопируйте .env.example в .env и заполните данные с https://my.telegram.org"
        )
        sys.exit(1)

    print("Запуск Telegram-клиента...")
    async with Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH) as client:
        print("Клиент подключён.\n")

        while True:
            print_menu()
            choice = input("  Выберите пункт [1-6]: ").strip()

            if choice == "1":
                await menu_search_keyword(client)
            elif choice == "2":
                await menu_parse_tgstat(client)
            elif choice == "3":
                await menu_recheck(client)
            elif choice == "4":
                await menu_create_folder(client)
            elif choice == "5":
                menu_show_channels()
            elif choice == "6":
                print("  Выход.")
                break
            else:
                print("  Неверный выбор, попробуйте снова.")


if __name__ == "__main__":
    asyncio.run(main())
