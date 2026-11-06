"""Root-scoped configuration; loading never creates state or reveals credentials."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


@dataclass(frozen=True)
class Profile:
    """Профиль отбора (правила v3): регион, зарплатный порог, целевой город."""
    name: str
    region: str            # 'RU' | 'intl'
    threshold: int = 0     # порог RUB/мес; 0 = без порога
    city: str = ""


# Активные профили по умолчанию (раздел 7 плана); выбор — PROFILES в .env.
# ru — Россия (офис Москва, порог 150 000 ₽/мес); intl — белый список стран
# (Сербия, Польша, Грузия, Армения, Казахстан, Таиланд, Австралия, Канада,
# ОАЭ), без порога. Белый список задан в rules.py (решение 06.11.2026).
PROFILES = {
    "ru": Profile(name="ru", region="RU", threshold=150000, city="Москва"),
    "intl": Profile(name="intl", region="intl", threshold=0, city="Дубай"),
}


@dataclass(frozen=True)
class Config:
    root: Path
    state_dir: Path
    db_path: Path
    api_token_path: Path
    telegram_token: str = field(default='', repr=False)
    owner_id: int = 0
    llm_base_url: str = 'https://api.openai.com/v1'
    llm_api_key: str = field(default='', repr=False)
    llm_model: str = ''
    llm_embedding_model: str = 'text-embedding-3-small'
    sources: tuple = ('hh',)
    profiles: tuple = (PROFILES["ru"], PROFILES["intl"])
    tg_channels: tuple = ()   # (name, region) — каналы для адаптера telegram_channels
    max_search_pages: int = 100

    @classmethod
    def load(cls, root: Path):
        root = Path(root).expanduser().resolve()
        env = {**dotenv_values(root / '.env', interpolate=False), **os.environ}
        return cls.from_values(root, env)

    @classmethod
    def from_values(cls, root: Path, env):
        root = Path(root).expanduser().resolve()
        def value(name, default=''):
            return str(env.get(name) or default).strip()

        def positive(name, default):
            raw = value(name, str(default))
            if not re.fullmatch(r'[0-9]+', raw) or int(raw) <= 0:
                raise ValueError(name + ': нужно положительное целое число')
            return int(raw)

        def list_of(name, default):
            raw = value(name, default)
            items = tuple(item.strip() for item in raw.split(',') if item.strip())
            return items or (default,)

        def profiles():
            names = list_of('PROFILES', 'ru,intl')
            resolved = []
            for name in names:
                if name not in PROFILES:
                    raise ValueError('PROFILES: неизвестный профиль ' + name)
                resolved.append(PROFILES[name])
            return tuple(resolved)

        def tg_channels():
            raw = value('TG_CHANNELS', '')
            channels = []
            for item in raw.split(','):
                item = item.strip()
                if not item:
                    continue
                if ':' in item:
                    name, region = item.rsplit(':', 1)
                else:
                    name, region = item, 'RU'
                name = name.strip().lstrip('@')
                region = region.strip().lower()
                if region not in ('ru', 'intl'):
                    raise ValueError('TG_CHANNELS: регион канала %s должен быть RU или intl' % name)
                channels.append((name, region))
            return tuple(channels)

        token = value('TELEGRAM_BOT_TOKEN')
        if token and not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
            raise ValueError('Некорректный TELEGRAM_BOT_TOKEN')
        owner = positive('TELEGRAM_OWNER_ID', 0) if value('TELEGRAM_OWNER_ID') else 0
        base = value('LLM_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
        try:
            url = urlsplit(base)
            valid = (url.scheme == 'https' and url.hostname and not url.username
                     and not url.password and not url.query and not url.fragment
                     and not any(char.isspace() for char in base))
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('LLM_BASE_URL: нужен HTTPS URL без пароля и параметров')
        state = root / '.state'
        return cls(root=root, state_dir=state, db_path=state / 'agent.sqlite3',
                   api_token_path=state / 'tokens' / 'hh.json',
                   telegram_token=token,
                   owner_id=owner, llm_base_url=base, llm_api_key=value('LLM_API_KEY'),
                   llm_model=value('LLM_MODEL'),
                   llm_embedding_model=value('LLM_EMBEDDING_MODEL', 'text-embedding-3-small'),
                   sources=list_of('SOURCES', 'hh'),
                   profiles=profiles(),
                   tg_channels=tg_channels(),
                   max_search_pages=positive('MAX_SEARCH_PAGES', 100))
