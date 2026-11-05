"""Root-scoped configuration; loading never creates state or reveals credentials."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


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
    search_interval: int = 21600
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

        def boolean(name):
            raw = value(name, 'false').lower()
            if raw not in ('true', 'false', '1', '0'):
                raise ValueError(name + ': допустимы true или false')
            return raw in ('true', '1')

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
                   api_token_path=state / 'api-token.json',
                   telegram_token=token,
                   owner_id=owner, llm_base_url=base, llm_api_key=value('LLM_API_KEY'),
                   llm_model=value('LLM_MODEL'),
                   search_interval=positive('SEARCH_INTERVAL', 21600),
                   max_search_pages=positive('MAX_SEARCH_PAGES', 100))
