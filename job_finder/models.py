from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Salary:
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    currency: str = "RUB"
    period: str = "month"
    raw: str = ""


@dataclass(frozen=True)
class Vacancy:
    id: str
    title: str
    description: str
    company: str = ""
    salary: Optional[Salary] = None
    remote: Optional[bool] = None
    remote_geo: Optional[str] = None
    location: Optional[str] = None
    test_required: bool = False
    closed: bool = False
    requirements: str = ""
    salary_gross: Optional[bool] = None
    published_at: Optional[str] = None
    language: str = "ru"

    @property
    def url(self):
        if not self.id.isdigit():
            raise ValueError("Некорректный ID вакансии")
        return "https://hh.ru/vacancy/" + self.id

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        # Служебные поля строки хранятся рядом с данными вакансии;
        # «возможно, дубль» (этап 3) не является полем модели.
        data.pop("possible_duplicates", None)
        if data.get("salary") is not None:
            data["salary"] = Salary(**data["salary"])
        return cls(**data)


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str


@dataclass(frozen=True)
class Letter:
    text: str
    fact_ids: list = field(default_factory=list)
    added_claims: list = field(default_factory=list)
    preparation: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Advice:
    text: str
    grounds: str


@dataclass(frozen=True)
class WriterResult:
    """Результат writer v2: три артефакта с независимыми статусами (R10)."""
    score: Optional[int] = None
    score_reason: str = ""
    score_status: str = "unavailable"   # ok|unavailable|failed
    letter: Optional[Letter] = None
    letter_status: str = "ok"
    advice: list = field(default_factory=list)
    advice_status: str = "ok"
    usage: Optional[dict] = None        # {model, prompt_tokens, completion_tokens}
    error: str = ""


class AttentionRequired(Exception):
    """Authentication, challenge or site change requiring a human."""


class RateLimited(Exception):
    """Источник временно ограничил доступ; повторить позже по таймеру."""


class VacancyUnavailable(Exception):
    """HH explicitly denies access to one vacancy for the signed-in account."""
