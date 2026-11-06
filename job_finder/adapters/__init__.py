"""Реестр адаптеров источников (этап 2 плана).

Новый адаптер = файл в этом пакете + регистрация в REGISTRY + имя
в строке конфига SOURCES (через запятую). Контракт — adapters.base.SourceAdapter.
"""
from .base import (AdapterError, SearchHit, SearchReport, SourceAdapter,
                   parse_rss, rfc822_to_iso, strip_html)
from .hh import HHAPIError, HHClient
from .fake import FakeAdapter
from .habr import HabrAdapter
from .remoteok import RemoteOKAdapter
from .telegram_channels import TelegramChannelsAdapter
from .wwr import WWRAdapter

REGISTRY = {
    "hh": HHClient,
    "fake": FakeAdapter,
    "remoteok": RemoteOKAdapter,
    "habr": HabrAdapter,
    "wwr": WWRAdapter,
    "telegram": TelegramChannelsAdapter,
}


def _telegram_client_factory(config):
    """Один общий Telethon-клиент на все экземпляры каналов (сессия одна)."""
    holder = {}

    async def factory(close=False):
        if close:
            client = holder.pop("client", None)
            if client is not None:
                await client.disconnect()
            return None
        client = holder.get("client")
        if client is None:
            from telethon import TelegramClient
            from .telegram_channels import load_credentials
            api_id, api_hash = load_credentials(config)
            client = TelegramClient(str(config.state_dir / "tg_probe.session"),
                                    api_id, api_hash, system_version="job-finder-0.1")
            await client.start()
            holder["client"] = client
        return client

    return factory


def build_adapters(config, registry=None):
    """Создаёт экземпляры адаптеров по config.sources (SOURCES в .env).

    'telegram' разворачивается в экземпляры каналов из TG_CHANNELS
    (канал = экземпляр, 16.1) с одним общим Telethon-клиентом."""
    registry = registry or REGISTRY
    adapters = []
    for name in config.sources:
        if name == "telegram":
            factory = _telegram_client_factory(config)
            for channel, region in config.tg_channels:
                adapters.append(TelegramChannelsAdapter(config, channel, region, factory))
            continue
        if name not in registry:
            raise AdapterError("Неизвестный источник в SOURCES: " + name)
        adapters.append(registry[name](config))
    return adapters
