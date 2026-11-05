"""Пробное чтение Telegram-каналов через Telethon (этап 0.5, задача job_finder_v2-0te.2).

Скрипт разведки, не часть приложения: входит в канал, читает последние посты
четырёх выбранных каналов и сохраняет фикстуры для тестов адаптера.

Использование:
    # 1. Вход (интерактивно: код из Telegram, при необходимости 2FA):
    TG_API_ID=... TG_API_HASH=... .venv/bin/python scripts/tg_probe.py --login
    # 2. Чтение каналов и сохранение фикстур:
    TG_API_ID=... TG_API_HASH=... .venv/bin/python scripts/tg_probe.py --read

Ключи читаются из переменных окружения или файла .env (TG_API_ID/TG_API_HASH).
Если Telegram недоступен напрямую — TG_PROXY=socks5://127.0.0.1:1080
(или https://...), формат Telethon proxy.
Сессия сохраняется в .state/tg_probe.session (0600). Только чтение.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

CHANNELS = ["nopremote", "remotegeekjob", "pythonrabota", "job_python"]
POSTS_PER_CHANNEL = 30  # читаем с запасом; в фикстуры отбираем до 5 подходящих


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


async def read_channels():
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
        for name in CHANNELS:
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["login", "read"])
    args = parser.parse_args(argv)
    if args.command == "login":
        asyncio.run(login())
    else:
        asyncio.run(read_channels())
    return 0


if __name__ == "__main__":
    sys.exit(main())
