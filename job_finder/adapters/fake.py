"""Детерминированный фейк-адаптер: контракт источника без сети.

Используется для сквозных тестов раунда (этап 2) и при разработке:
возвращает фиксированную выдачу по внутренним данным. Включается
строкой конфига SOURCES=fake.
"""
from .base import SearchHit, SearchReport

# Встроенные данные: (native_id, title, salary).
JOBS = [
    ("1001", "Python разработчик", 220000),
    ("1002", "Middle Python backend", 190000),
    ("1003", "Java developer", 250000),   # не пройдёт правило Python
]


class FakeAdapter:
    name = "fake"

    def __init__(self, config, jobs=None):
        self.config = config
        self.jobs = jobs or JOBS
        self.closed = False

    async def search(self, criteria=None, since=None):
        if self.closed:
            raise RuntimeError("адаптер закрыт")
        return SearchReport(hits=[SearchHit(native_id=item[0]) for item in self.jobs],
                            incomplete=False, notes=[])

    async def fetch(self, native_id):
        if self.closed:
            raise RuntimeError("адаптер закрыт")
        for job_id, title, salary in self.jobs:
            if job_id == str(native_id):
                return _vacancy(job_id, title, salary)
        return _vacancy(str(native_id), "Вакансия недоступна", None, closed=True)

    async def close(self):
        self.closed = True


def _vacancy(job_id, title, salary, closed=False):
    from ..models import Salary, Vacancy
    parsed = Salary(salary, None, "RUB", "month", "от %s ₽" % salary) if salary else None
    return Vacancy(id=str(job_id), title=title,
                   description="Работа с Python и FastAPI" if "Python" in title or "python" in title.lower() else "Работа с Java",
                   company="Фейк Ко", salary=parsed, remote=True, closed=closed,
                   requirements="FastAPI" if salary else "")
