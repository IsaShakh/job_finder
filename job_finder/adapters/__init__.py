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
from .wwr import WWRAdapter

REGISTRY = {
    "hh": HHClient,
    "fake": FakeAdapter,
    "remoteok": RemoteOKAdapter,
    "habr": HabrAdapter,
    "wwr": WWRAdapter,
}


def build_adapters(config, registry=None):
    """Создаёт экземпляры адаптеров по config.sources (SOURCES в .env)."""
    registry = registry or REGISTRY
    adapters = []
    for name in config.sources:
        if name not in registry:
            raise AdapterError("Неизвестный источник в SOURCES: " + name)
        adapters.append(registry[name](config))
    return adapters
