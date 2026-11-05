import argparse
import asyncio
import fcntl
import getpass
import os
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from dotenv import dotenv_values, set_key
from .config import Config
from .store import Store


@contextmanager
def process_lock(state_dir):
    state_dir.mkdir(parents=True,exist_ok=True)
    os.chmod(state_dir,0o700)
    with (state_dir/"agent.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Агент уже запущен. Сначала остановите другой экземпляр") from None
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(),fcntl.LOCK_UN)


def save_env_values(path, values):
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        fd=os.open(str(path),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
    os.chmod(path,0o600)
    for key,value in values.items():
        set_key(str(path),key,str(value),quote_mode="always")
    os.chmod(path,0o600)


def initialize(root):
    root.mkdir(parents=True,exist_ok=True)
    path=root/".env"
    if not path.exists():
        example=Path(__file__).resolve().parents[1]/".env.example"
        content=example.read_text(encoding="utf-8") if example.exists() else "TELEGRAM_BOT_TOKEN=\n"
        fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as target:
            target.write(content)
    state=root/".state"
    state.mkdir(exist_ok=True)
    os.chmod(state,0o700)
    print("Локальные настройки подготовлены: "+str(path))


def configure(root):
    initialize(root)
    print("Ключи сохраняются только в .env на этом компьютере. Ввод ключей скрыт.")
    values={}
    token=getpass.getpass("Токен Telegram-бота от @BotFather (Enter — оставить): ").strip()
    if token:
        values["TELEGRAM_BOT_TOKEN"]=token
    # Existing settings can be invalid (or contain a key pasted as the URL).
    # Do not validate or display them before the user can correct them.
    base=input("Адрес API модели (для OpenAI: https://api.openai.com/v1; Enter — оставить): ").strip()
    if base:
        values["LLM_BASE_URL"]=base
    key=getpass.getpass("API-ключ модели (Enter — оставить): ").strip()
    if key:
        values["LLM_API_KEY"]=key
    model=input("Название модели (Enter — оставить): ").strip()
    if model:
        values["LLM_MODEL"]=model
    candidate={**dotenv_values(root/".env",interpolate=False),**values}
    try:
        Config.from_values(root,candidate)
    except ValueError as exc:
        raise ValueError("Изменения не сохранены. "+str(exc)) from None
    try:
        Config.from_values(root,{**candidate,**os.environ})
    except ValueError as exc:
        raise ValueError("Изменения не сохранены: проверьте переменные окружения терминала. "+str(exc)) from None
    save_env_values(root/".env",values)
    overridden=[name for name in values if name in os.environ and os.environ[name]!=values[name]]
    if overridden:
        print("Внимание: переменные окружения имеют приоритет над сохранёнными значениями: "+", ".join(overridden))
    print("Настройки сохранены. Теперь выполните: python -m job_finder pair")


async def pair(config):
    if not config.telegram_token:
        raise ValueError("Сначала сохраните токен бота командой configure")
    code=secrets.token_urlsafe(18)
    base="https://api.telegram.org/bot"+config.telegram_token+"/"
    async with httpx.AsyncClient(timeout=40,follow_redirects=False) as client:
        async def call(method,payload):
            try:
                response=await client.post(base+method,json=payload)
                data=response.json()
                if not data.get("ok"):
                    raise ValueError("Telegram отклонил запрос. Проверьте токен и отсутствие другого бота-поллера")
                return data["result"]
            except httpx.HTTPError:
                raise ValueError("Telegram недоступен. Проверьте подключение") from None
        webhook=await call("getWebhookInfo",{})
        if webhook.get("url"):
            raise ValueError("У бота уже настроен webhook. Используйте отдельного бота для агента")
        me=await call("getMe",{})
        print("Откройте личный чат https://t.me/"+me["username"])
        print("Отправьте ему эту одноразовую команду в течение 5 минут:\n/start "+code)
        offset=0
        deadline=time.monotonic()+300
        while time.monotonic()<deadline:
            updates=await call("getUpdates",{"offset":offset,"timeout":20,"allowed_updates":["message","callback_query"]})
            for update in updates:
                offset=update["update_id"]+1
                message=update.get("message",{})
                sender=message.get("from",{})
                chat=message.get("chat",{})
                if message.get("text")=="/start "+code and chat.get("type")=="private" and sender.get("id")==chat.get("id") and not sender.get("is_bot"):
                    save_env_values(config.root/".env",{"TELEGRAM_OWNER_ID":sender["id"]})
                    store=Store(config.db_path)
                    store.set("telegram_offset",offset)
                    store.enqueue("Telegram привязан. Агент в режиме просмотра. /help — команды.")
                    store.close()
                    print("Владелец привязан. Запустите агента командой run.")
                    return
        raise ValueError("Время привязки истекло. Запустите pair снова")


def doctor(config):
    checks=[("Токен Telegram",bool(config.telegram_token)),("Владелец Telegram",config.owner_id>0),
            ("Ключ и модель для писем",bool(config.llm_api_key and config.llm_model)),
            ("Токен API HH (вход через api-login)",config.api_token_path.exists())]
    print("Источник вакансий: API HH (без браузера)")
    for label,ok in checks:
        print(("✓ " if ok else "○ ")+label)
    print("Проверка локальная: токены и вход на сайте пока не проверялись по сети.")


async def api_login(config):
    from .adapters.hh import authorize
    username=input("Email или телефон HH: ").strip()
    if not username:
        raise ValueError("Логин не введён")
    password=getpass.getpass("Пароль (Enter — вход по коду из SMS/письма): ").strip() or None
    def otp_code():
        return input("Код из SMS/письма: ").strip()
    def captcha_solve(png):
        path=config.state_dir/"captcha.png"
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(png)
        print("Капча сохранена в "+str(path))
        try:
            subprocess.run(["open",str(path)],check=False)
        except FileNotFoundError:
            pass
        return input("Текст с картинки: ").strip()
    me=await authorize(config,username,password,otp_code=otp_code,captcha_solve=captcha_solve)
    print("Вход через API подтверждён: %s %s" % (me.get("first_name") or "",me.get("last_name") or ""))


def telegram_tasks(bot):
    from .telegram import TelegramError
    async def polling():
        while True:
            try:
                await bot.poll_once()
                await asyncio.sleep(0.2)
            except TelegramError:
                # Telegram only reports results; transport failure
                # must not control the local worker.
                print("Telegram недоступен. Скрипт продолжает работу; связь проверится через 10 секунд.")
                await asyncio.sleep(10)
    async def delivery():
        while True:
            try:
                await bot.flush()
            except TelegramError:
                await asyncio.sleep(10)
            await asyncio.sleep(1.1)
    return polling, delivery


async def run_worker(config, store, label):
    from .adapters import build_adapters
    from .engine import Engine
    from .letters import LetterGenerator
    from .telegram import Telegram
    adapters=build_adapters(config)
    engine=Engine(config,store,adapters,LetterGenerator(config))
    bot=Telegram(config,store)
    polling,delivery=telegram_tasks(bot)
    print("Скрипт запущен: поиск запускается командой /search. Остановка здесь: Ctrl+C.")
    print("Источники вакансий: "+label+". Telegram показывает результаты; /status — состояние.")
    tasks=[asyncio.create_task(engine.run()),asyncio.create_task(polling()),asyncio.create_task(delivery())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for adapter in adapters:
            await adapter.close()


async def api_command(config, command):
    from .adapters import build_adapters
    from .async_store import AsyncStore
    from .engine import Engine
    from .letters import LetterGenerator
    if command=="run" and (not config.telegram_token or config.owner_id<=0):
        raise ValueError("Сначала настройте Telegram: configure, затем pair")
    if "hh" in config.sources and not config.api_token_path.exists():
        raise ValueError("Сначала выполните вход в API HH: python -m job_finder api-login")
    store=AsyncStore(config.db_path)
    await store.start()
    try:
        adapters=build_adapters(config)
        try:
            engine=Engine(config,store,adapters,LetterGenerator(config))
            if command=="search":
                await store.round_request()
                while True:
                    await engine.run_pending_rounds()
                    if not await engine.resume_waiting():
                        break
                print("Поиск закончен. Источники: "+", ".join(config.sources))
                return
            await run_worker(config,store,", ".join(config.sources))
        finally:
            for adapter in adapters:
                await adapter.close()
    finally:
        await store.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description="Личный агент поиска вакансий hh.ru с отчётами в Telegram")
    parser.add_argument("--root",type=Path,default=Path.cwd(),help="Папка проекта и его локальных настроек")
    parser.add_argument("command",choices=["init","configure","pair","doctor","api-login","search","run","status"])
    args=parser.parse_args(argv)
    root=args.root.resolve()
    try:
        if args.command=="init":
            initialize(root)
            return 0
        if args.command=="configure":
            configure(root)
            return 0
        config=Config.load(root)
        if args.command=="doctor":
            doctor(config)
            return 0
        if args.command=="status":
            store=Store(config.db_path)
            print("Пауза: "+("да" if store.is_paused() else "нет"))
            print(store.counts())
            store.close()
            return 0
        with process_lock(config.state_dir):
            if args.command=="pair":
                asyncio.run(pair(config))
            elif args.command=="api-login":
                asyncio.run(api_login(config))
            else:
                asyncio.run(api_command(config,args.command))
        return 0
    except KeyboardInterrupt:
        print("Агент остановлен. Поиск можно запустить снова командой run или search.")
        return 0
    except (ValueError,RuntimeError) as exc:
        print(str(exc),file=sys.stderr)
        return 1
    except Exception as exc:
        # Do not render HTTP exceptions: they may contain credentials
        # or page text. State remains available for recovery.
        print("Операция остановлена ("+type(exc).__name__+"). Проверьте настройки и вход в HH.",file=sys.stderr)
        return 1


if __name__=="__main__":
    sys.exit(main())
