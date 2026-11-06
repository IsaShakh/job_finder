"""Пробное чтение Telegram-каналов (этап 0.5, задача job_finder_v2-0te.2).

Скрипт разведки, не часть приложения: читает последние посты четырёх
выбранных каналов и сохраняет фикстуры для тестов адаптера. Два способа:

  --preview  — публичные веб-превью t.me/s/<канал> (HTML), ключи не нужны;
               подходит только для публичных каналов, видно ~20 последних
               постов. Фикстуры: <канал>.preview.json.
  --login/--read — Telethon (MTProto): нужны TG_API_ID/TG_API_HASH с
               my.telegram.org, работает и для закрытых каналов.

Использование:
    # Без ключей (нужен только доступ к t.me — VPN/прокси при необходимости):
    TG_PROXY=socks5://127.0.0.1:1080 .venv/bin/python scripts/tg_probe.py --preview
    # Telethon: вход (интерактивно: код из Telegram, при необходимости 2FA):
    TG_API_ID=... TG_API_HASH=... .venv/bin/python scripts/tg_probe.py --login
    # Telethon: чтение каналов и сохранение фикстур:
    TG_API_ID=... TG_API_HASH=... .venv/bin/python scripts/tg_probe.py --read

Ключи читаются из переменных окружения или файла .env (TG_API_ID/TG_API_HASH).
TG_PROXY=socks5://127.0.0.1:1080 (или https://...) — прокси для Telethon
и для HTTP-запросов к t.me. Сессия — в .state/tg_probe.session (0600).
Только чтение.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

from dotenv import dotenv_values

CHANNELS = ["nopremote", "remotegeekjob", "pythonrabota", "job_python"]
POSTS_PER_CHANNEL = 30  # читаем с запасом; в фикстуры отбираем до 5 подходящих
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

BLOCK_RE = re.compile(r'<div class="[^"]*js-widget_message"[^>]*data-post="([^"]+)"')
TEXT_RE = re.compile(r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', re.S)
TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')


def root_dir():
    return Path(__file__).resolve().parents[1]


def credentials():
    env = {**dotenv_values(root_dir() / ".env", interpolate=False), **os.environ}
    api_id = env.get("TG_API_ID", "").strip()
    api_hash = env.get("TG_API_HASH", "").strip()
    if not api_id or not api_hash:
        raise SystemExit("Нужны TG_API_ID и TG_API_HASH (my.telegram.org) в окружении или .env")
    return int(api_id), api_hash


def client_kwargs():
    """Опциональный прокси из TG_PROXY (Telegram часто недоступен напрямую).

    Формат: socks5://127.0.0.1:1080 или https://user:pass@host:port
    (как в переменной proxy у Telethon/TelegramClient)."""
    env = {**dotenv_values(root_dir() / ".env", interpolate=False), **os.environ}
    proxy = env.get("TG_PROXY", "").strip()
    if proxy:
        import python_socks
    return {"proxy": proxy} if proxy else {}


def session_file():
    path = root_dir() / ".state" / "tg_probe.session"
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    return path


async def login():
    from telethon import TelegramClient
    api_id, api_hash = credentials()
    client = TelegramClient(str(session_file()), api_id, api_hash,
                            system_version="tg-probe-0.5", **client_kwargs())
    await client.start()
    me = await client.get_me()
    print("Вход подтверждён: %s (@%s)" % (me.first_name or "", me.username or ""))
    await client.disconnect()


async def read_channels(channels=None):
    from telethon import TelegramClient
    from telethon.errors import ChannelPrivateError
    api_id, api_hash = credentials()
    client = TelegramClient(str(session_file()), api_id, api_hash,
                            system_version="tg-probe-0.5", **client_kwargs())
    await client.start()
    fixtures_dir = root_dir() / "docs" / "research" / "fixtures" / "tg_channels"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    report = {"read_at": datetime.now(timezone.utc).isoformat(), "channels": {}}
    try:
        for name in (channels or CHANNELS):
            try:
                entity = await client.get_entity(name)
                posts = []
                async for message in client.iter_messages(entity, limit=POSTS_PER_CHANNEL):
                    if not message.text:
                        continue
                    posts.append({
                        "id": message.id,
                        "date": message.date.isoformat(),
                        "text": message.text,
                        "views": getattr(message, "views", None),
                        "has_media": bool(message.media),
                        "url": "https://t.me/%s/%s" % (name, message.id),
                    })
                report["channels"][name] = {"title": getattr(entity, "title", name),
                                            "posts_read": len(posts)}
                # В фикстуры — до 5 вакансионных постов (достаточно для тестов адаптера).
                fixture = posts[:5]
                (fixtures_dir / (name + ".json")).write_text(
                    json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print("%s: прочитано %s постов, фикстура %s" % (name, len(posts), len(fixture)))
            except ChannelPrivateError:
                report["channels"][name] = {"error": "channel_private"}
                print("%s: канал недоступен аккаунту" % name)
    finally:
        await client.disconnect()
    (fixtures_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Отчёт: " + str(fixtures_dir / "report.json"))


def parse_preview_posts(html):
    """Достаёт посты со страницы t.me/s: id, дата, текст, ссылка."""
    blocks = list(BLOCK_RE.finditer(html))
    posts = []
    for index, match in enumerate(blocks):
        channel, message_id = match.group(1).split("/", 1)
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(html)
        chunk = html[match.end():end]
        text_match = TEXT_RE.search(chunk)
        if not text_match:
            continue
        raw = re.sub(r"<br\s*/?>", "\n", text_match.group(1))
        text = unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        if not text:
            continue
        time_match = TIME_RE.search(chunk)
        posts.append({
            "id": int(message_id),
            "date": time_match.group(1) if time_match else "",
            "text": text,
            "url": "https://t.me/%s/%s" % (channel, message_id),
            "source": "t.me/s preview",
        })
    return posts


async def read_preview(channels=None):
    """Чтение публичных каналов через t.me/s — без ключей my.telegram.org."""
    import httpx
    env = {**dotenv_values(root_dir() / ".env", interpolate=False), **os.environ}
    proxy = env.get("TG_PROXY", "").strip() or None
    fixtures_dir = root_dir() / "docs" / "research" / "fixtures" / "tg_channels"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    report = {"read_at": datetime.now(timezone.utc).isoformat(),
              "source": "t.me/s web preview (без MTProto-ключей)", "channels": {}}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy=proxy,
                                 headers={"User-Agent": USER_AGENT}) as client:
        for name in (channels or CHANNELS):
            try:
                response = await client.get("https://t.me/s/" + name)
                if response.status_code == 429:
                    report["channels"][name] = {"error": "rate_limited"}
                    print("%s: Telegram ограничил запросы (429); подождите и повторите" % name)
                    continue
                if response.status_code != 200:
                    report["channels"][name] = {"error": "http_%d" % response.status_code}
                    print("%s: HTTP %d — канал недоступен или структура изменилась" % (name, response.status_code))
                    continue
                posts = parse_preview_posts(response.text)[:POSTS_PER_CHANNEL]
                fixture = posts[:5]
                (fixtures_dir / (name + ".preview.json")).write_text(
                    json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                report["channels"][name] = {"posts_read": len(posts), "fixture_posts": len(fixture)}
                print("%s: постов %s, фикстура %s" % (name, len(posts), len(fixture)))
            except httpx.HTTPError as exc:
                report["channels"][name] = {"error": type(exc).__name__}
                print("%s: %s — проверьте доступ к t.me (VPN/прокси TG_PROXY)" % (name, type(exc).__name__))
    (fixtures_dir / "preview-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Отчёт: " + str(fixtures_dir / "preview-report.json"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["login", "read", "preview"])
    parser.add_argument("--channels", default="",
                        help="Каналы через запятую (по умолчанию — встроенный список)")
    args = parser.parse_args(argv)
    channels = [name.strip().lstrip("@") for name in args.channels.split(",")
                if name.strip()] or None
    if args.command == "login":
        asyncio.run(login())
    elif args.command == "preview":
        asyncio.run(read_preview(channels))
    else:
        asyncio.run(read_channels(channels))
    return 0


if __name__ == "__main__":
    sys.exit(main())
