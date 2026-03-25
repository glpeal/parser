#!/usr/bin/env python3
"""
Telegram Channel Parser
- Поиск каналов по ключевому слову или через tgstat.ru
- Фильтрация по наличию комментариев и минимальному числу подписчиков
- Экспорт папки-приглашения t.me/addlist/...
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

# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

load_dotenv()

API_ID: int = int(os.getenv("API_ID", "0"))
API_HASH: str = os.getenv("API_HASH", "")
MIN_SUBSCRIBERS: int = int(os.getenv("MIN_SUBSCRIBERS", "1000"))
MAX_CHANNELS_PER_FOLDER: int = int(os.getenv("MAX_CHANNELS_PER_FOLDER", "200"))
SESSION_NAME: str = os.getenv("SESSION_NAME", "parser_session")

STORAGE_FILE = Path("channels.json")

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("errors.log", encoding="utf-8")],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Импорт pyrogram (pyrofork устанавливается в пространство имён pyrogram)
# ---------------------------------------------------------------------------

try:
    from pyrogram import Client, errors
    from pyrogram.enums import ChatType
    from pyrogram.raw import functions, types as raw_types
except ImportError:
    print(
        "Ошибка: библиотека pyrogram не найдена.\n"
        "Выполните: pip install -r requirements.txt"
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Хранилище channels.json
# ---------------------------------------------------------------------------

def _load() -> dict:
    if STORAGE_FILE.exists():
        try:
            return json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_updated": None, "channels": []}


def _save(data: dict) -> None:
    data["last_updated"] = datetime.now().isoformat()
    STORAGE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_channels(new: list[dict]) -> None:
    data = _load()
    index: dict[str, dict] = {ch["username"]: ch for ch in data["channels"]}
    for ch in new:
        index[ch["username"]] = ch
    data["channels"] = list(index.values())
    _save(data)


# ---------------------------------------------------------------------------
# Парсинг tgstat.ru
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


async def parse_tgstat(category_url: str, pages: int = 3) -> list[str]:
    """Возвращает список username с tgstat.ru."""
    found: list[str] = []
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(headers=_HEADERS, connector=connector) as session:
        for page in range(1, pages + 1):
            url = f"{category_url.rstrip('/')}?page={page}"
            print(f"  [tgstat] Страница {page}: {url}")
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        print(f"  [tgstat] HTTP {resp.status}, останавливаем.")
                        break
                    html = await resp.text()
            except Exception as exc:
                log.error("tgstat page %d: %s", page, exc)
                print(f"  [tgstat] Ошибка: {exc}")
                break

            soup = BeautifulSoup(html, "html.parser")
            before = len(found)

            # Основной способ: /channel/@username в href
            for tag in soup.find_all("a", href=True):
                m = re.search(r"/channel/@([\w]+)", tag["href"])
                if m:
                    uname = m.group(1).lower()
                    if uname not in found:
                        found.append(uname)

            # Резервный: @username в тексте страницы
            if len(found) == before:
                for text in soup.stripped_strings:
                    m = re.search(r"@([\w]{5,32})", text)
                    if m:
                        uname = m.group(1).lower()
                        if uname not in found:
                            found.append(uname)

            added = len(found) - before
            print(f"  [tgstat] +{added} каналов (всего {len(found)})")
            if added == 0:
                print("  [tgstat] Новых нет, останавливаем.")
                break

            await asyncio.sleep(random.uniform(1.5, 3.0))

    return found


# ---------------------------------------------------------------------------
# Поиск через Telegram
# ---------------------------------------------------------------------------

async def search_telegram(client: Client, query: str, limit: int = 50) -> list[str]:
    """Глобальный поиск каналов по ключевому слову."""
    found: list[str] = []
    print(f"  [поиск] Запрос: «{query}», лимит {limit}")
    try:
        async for chat in client.search_global(query, limit=limit):
            if chat.type in (ChatType.CHANNEL, ChatType.SUPERGROUP):
                uname = chat.username.lower() if chat.username else str(chat.id)
                if uname not in found:
                    found.append(uname)
            await asyncio.sleep(0.05)
    except errors.FloodWait as exc:
        print(f"  [поиск] FloodWait {exc.value}с, ждём...")
        await asyncio.sleep(exc.value)
    except Exception as exc:
        log.error("search_telegram: %s", exc)
        print(f"  [поиск] Ошибка: {exc}")

    print(f"  [поиск] Найдено: {len(found)}")
    return found


# ---------------------------------------------------------------------------
# Проверка каналов (комментарии + подписчики)
# ---------------------------------------------------------------------------

async def _check_one(client: Client, username: str) -> dict | None:
    """Проверяет один канал. Возвращает dict или None."""
    chat = await client.get_chat(username)
    members = getattr(chat, "members_count", 0) or 0
    linked = getattr(chat, "linked_chat", None)
    if members >= MIN_SUBSCRIBERS and linked is not None:
        return {
            "username": username,
            "title": getattr(chat, "title", username),
            "subscribers": members,
            "has_comments": True,
        }
    return None


async def filter_channels(client: Client, usernames: list[str]) -> list[dict]:
    """Фильтрует каналы по комментариям и подписчикам."""
    result: list[dict] = []
    total = len(usernames)

    for idx, uname in enumerate(usernames, 1):
        print(f"  [{idx}/{total}] @{uname} ...", end=" ", flush=True)
        for attempt in range(2):
            try:
                ch = await _check_one(client, uname)
                if ch:
                    print(f"ОК ({ch['subscribers']} подп.)")
                    result.append(ch)
                else:
                    chat = await client.get_chat(uname)
                    members = getattr(chat, "members_count", 0) or 0
                    linked = getattr(chat, "linked_chat", None)
                    if members < MIN_SUBSCRIBERS:
                        print(f"мало подписчиков ({members})")
                    else:
                        print("нет комментариев")
                break
            except errors.FloodWait as exc:
                wait = exc.value + 1
                print(f"FloodWait {wait}с...", end=" ", flush=True)
                await asyncio.sleep(wait)
            except errors.UsernameNotOccupied:
                print("не найден")
                break
            except errors.ChannelPrivate:
                print("приватный")
                break
            except errors.UsernameInvalid:
                print("неверный username")
                break
            except Exception as exc:
                log.error("checker @%s: %s", uname, exc)
                print(f"ошибка: {exc}")
                break

        await asyncio.sleep(random.uniform(1.0, 2.0))

    return result


# ---------------------------------------------------------------------------
# Создание папки-приглашения
# ---------------------------------------------------------------------------

async def _free_filter_id(client: Client) -> int:
    try:
        raw = await client.invoke(functions.messages.GetDialogFilters())
        # В разных версиях схемы результат — либо объект с .filters, либо список
        filters = raw.filters if hasattr(raw, "filters") else raw
        used = {f.id for f in filters if hasattr(f, "id")}
        for fid in range(2, 256):
            if fid not in used:
                return fid
    except Exception as exc:
        log.error("_free_filter_id: %s", exc)
    return 2


async def create_folder_invite(client: Client, usernames: list[str], title: str) -> str:
    """Создаёт папку Telegram и возвращает t.me/addlist/..."""
    if not usernames:
        raise ValueError("Список каналов пуст")

    batch = usernames[:MAX_CHANNELS_PER_FOLDER]
    print(f"  [папка] Резолвим {len(batch)} каналов...")

    peers = []
    for uname in batch:
        try:
            peers.append(await client.resolve_peer(uname))
            await asyncio.sleep(0.2)
        except Exception as exc:
            log.error("resolve_peer @%s: %s", uname, exc)
            print(f"  [папка] Пропуск @{uname}: {exc}")

    if not peers:
        raise ValueError("Не удалось резолвить ни одного канала")

    fid = await _free_filter_id(client)
    print(f"  [папка] Создаём фильтр #{fid} «{title}»...")

    dialog_filter = raw_types.DialogFilter(
        id=fid,
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
        functions.messages.UpdateDialogFilter(id=fid, filter=dialog_filter)
    )

    print("  [папка] Генерируем ссылку...")
    res = await client.invoke(
        functions.chatlists.ExportChatlistInvite(
            chatlist=raw_types.InputChatlistDialogFilter(filter_id=fid),
            title=title,
            peers=peers,
        )
    )
    return res.invite.url


# ---------------------------------------------------------------------------
# Меню
# ---------------------------------------------------------------------------

def _menu() -> None:
    print(
        "\n"
        "╔══════════════════════════════════════╗\n"
        "║    Telegram Channel Parser           ║\n"
        "╠══════════════════════════════════════╣\n"
        "║  1. Поиск по ключевому слову         ║\n"
        "║  2. Парсинг с tgstat.ru              ║\n"
        "║  3. Перепроверить сохранённые каналы ║\n"
        "║  4. Создать ссылку-приглашение папки ║\n"
        "║  5. Показать сохранённые каналы      ║\n"
        "║  6. Выход                            ║\n"
        "╚══════════════════════════════════════╝"
    )


def _show_table(channels: list[dict]) -> None:
    if not channels:
        print("  Список пуст.")
        return
    print(f"\n  {'№':<4} {'Username':<28} {'Подписчики':>12}  Назв.")
    print("  " + "─" * 64)
    for i, ch in enumerate(channels, 1):
        mark = "+" if ch.get("has_comments") else " "
        print(
            f"  {i:<4} @{ch['username']:<27} {ch['subscribers']:>12}  "
            f"[{mark}] {ch.get('title','')[:22]}"
        )
    print(f"\n  Итого: {len(channels)}  ([+] = есть комментарии)")


async def _opt1(client: Client) -> None:
    q = input("  Ключевое слово: ").strip()
    if not q:
        return
    lim = input("  Лимит (по умолч. 50): ").strip()
    lim = int(lim) if lim.isdigit() else 50
    names = await search_telegram(client, q, lim)
    print(f"\n  Проверяем {len(names)} каналов...")
    found = await filter_channels(client, names)
    if found:
        upsert_channels(found)
        print(f"  Сохранено: {len(found)}")
    else:
        print("  Подходящих не найдено.")


async def _opt2(client: Client) -> None:
    url = input("  URL категории tgstat.ru\n  Пример: https://tgstat.ru/ru/news\n  > ").strip()
    if not url:
        return
    pg = input("  Страниц (по умолч. 3): ").strip()
    pg = int(pg) if pg.isdigit() else 3
    names = await parse_tgstat(url, pg)
    print(f"\n  Проверяем {len(names)} каналов...")
    found = await filter_channels(client, names)
    if found:
        upsert_channels(found)
        print(f"  Сохранено: {len(found)}")
    else:
        print("  Подходящих не найдено.")


async def _opt3(client: Client) -> None:
    data = _load()
    chs = data.get("channels", [])
    if not chs:
        print("  Нет сохранённых каналов.")
        return
    names = [ch["username"] for ch in chs]
    print(f"\n  Перепроверяем {len(names)} каналов...")
    found = await filter_channels(client, names)
    upsert_channels(found)
    print(f"  Обновлено: {len(found)}")


async def _opt4(client: Client) -> None:
    data = _load()
    chs = [ch for ch in data.get("channels", []) if ch.get("has_comments")]
    if not chs:
        print("  Нет каналов с комментариями в сохранённых.")
        return
    print(f"  Доступно {len(chs)} каналов.")
    title = input("  Название папки: ").strip() or "Мои каналы"
    names = [ch["username"] for ch in chs]
    try:
        link = await create_folder_invite(client, names, title)
        print(f"\n  Ссылка готова:\n  {link}")
    except Exception as exc:
        log.error("create_folder_invite: %s", exc)
        print(f"  Ошибка: {exc}")


def _opt5() -> None:
    data = _load()
    print(f"\n  Обновлено: {data.get('last_updated', '—')}")
    _show_table(data.get("channels", []))


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main() -> None:
    if not API_ID or not API_HASH:
        print(
            "Ошибка: заполните API_ID и API_HASH в файле .env\n"
            "Данные берутся на https://my.telegram.org -> API development tools"
        )
        sys.exit(1)

    print("Подключение к Telegram...")
    async with Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH) as client:
        me = await client.get_me()
        print(f"Вошли как: {me.first_name} (@{me.username})\n")

        while True:
            _menu()
            choice = input("  Выбор [1-6]: ").strip()
            if choice == "1":
                await _opt1(client)
            elif choice == "2":
                await _opt2(client)
            elif choice == "3":
                await _opt3(client)
            elif choice == "4":
                await _opt4(client)
            elif choice == "5":
                _opt5()
            elif choice == "6":
                print("  Выход.")
                break
            else:
                print("  Неверный ввод.")


if __name__ == "__main__":
    asyncio.run(main())
