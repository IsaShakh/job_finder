"""Root-scoped configuration; loading never creates state or reveals credentials."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

RESUME_ID = 'b424f687ff113123eb0039ed1f7a496e413267'


@dataclass(frozen=True)
class Config:
    root: Path
    state_dir: Path
    db_path: Path
    profile_dir: Path
    telegram_token: str = field(default='', repr=False)
    owner_id: int = 0
    llm_base_url: str = 'https://api.openai.com/v1'
    llm_api_key: str = field(default='', repr=False)
    llm_model: str = ''
    resume_id: str = RESUME_ID
    live: bool = False
    headless: bool = False
    search_interval: int = 21600
    send_interval: int = 90
    max_search_pages: int = 100

    @classmethod
    def load(cls, root: Path):
        root = Path(root).expanduser().resolve()
        env = {**dotenv_values(root / '.env', interpolate=False), **os.environ}

        def value(name, default=''):
            return str(env.get(name) or default).strip()

        def positive(name, default):
            raw = value(name, str(default))
            if not re.fullmatch(r'[0-9]+', raw) or int(raw) <= 0:
                raise ValueError(name + ': нужно положительное целое число')
            return int(raw)

        def boolean(name):
            raw = value(name, 'false').lower()
            if raw not in ('true', 'false', '1', '0'):
                raise ValueError(name + ': допустимы true или false')
            return raw in ('true', '1')

        token = value('TELEGRAM_BOT_TOKEN')
        if token and not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
            raise ValueError('Некорректный TELEGRAM_BOT_TOKEN')
        owner = positive('TELEGRAM_OWNER_ID', 0) if value('TELEGRAM_OWNER_ID') else 0
        resume_id = value('HH_RESUME_ID', RESUME_ID)
        if resume_id != RESUME_ID:
            raise ValueError('HH_RESUME_ID не совпадает с согласованным резюме')
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
                   profile_dir=state / 'browser-profile', telegram_token=token,
                   owner_id=owner, llm_base_url=base, llm_api_key=value('LLM_API_KEY'),
                   llm_model=value('LLM_MODEL'), resume_id=resume_id,
                   live=boolean('HH_LIVE'), headless=boolean('HH_HEADLESS'),
                   search_interval=positive('SEARCH_INTERVAL', 21600),
                   send_interval=positive('SEND_INTERVAL', 90),
                   max_search_pages=positive('MAX_SEARCH_PAGES', 100))
