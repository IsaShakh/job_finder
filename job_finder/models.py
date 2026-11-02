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
    test_required: bool = False
    closed: bool = False
    already_applied: bool = False
    requirements: str = ""

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
class Outcome:
    status: str
    evidence: str = ""
    reason: str = ""


class AttentionRequired(Exception):
    """Authentication, challenge or site change requiring a human."""


class VacancyUnavailable(Exception):
    """HH explicitly denies access to one vacancy for the signed-in account."""
