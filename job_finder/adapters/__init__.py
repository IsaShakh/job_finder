"""Реестр адаптеров источников (этап 2 плана).

Новый адаптер = файл в этом пакете + регистрация в REGISTRY + имя
в строке конфига SOURCES (через запятую). Контракт — adapters.base.SourceAdapter.
"""
from .base import AdapterError, SearchHit, SearchReport, SourceAdapter, strip_html
from .hh import HHAPIError, HHClient
from .fake import FakeAdapter
from .remoteok import RemoteOKAdapter

REGISTRY = {
    "hh": HHClient,
    "fake": FakeAdapter,
    "remoteok": RemoteOKAdapter,
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
