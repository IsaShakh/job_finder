import re
from .models import Decision, Salary


def parse_salary(raw):
    text = raw.lower().replace("\xa0", " ").replace("\u202f", " ")
    numbers = [int(re.sub(r"\D", "", n)) for n in re.findall(r"\d{1,3}(?:[ ]\d{3})+|\d+", text)]
    currency = next((c for pattern, c in [(r"₽|руб|rur|rub", "RUB"), (r"\$|usd|доллар", "USD"), (r"€|eur|евро", "EUR"), (r"₸|тенге|kzt", "KZT")] if re.search(pattern,text)), "unknown")
    period = next((p for pattern, p in [(r"месяц|month", "month"), (r"час|hour", "hour"), (r"год|year", "year"), (r"смен|день|day", "day")] if re.search(pattern,text)), "unknown")
    if not numbers:
        return Salary(currency=currency, period=period, raw=raw)
    if len(numbers) == 1:
        lo = None if re.search(r"\bдо\s+\d|up to\s+\d", text) else numbers[0]
        hi = None if re.search(r"\bот\s+\d|from\s+\d", text) else numbers[0]
    elif len(numbers) == 2:
        lo, hi = numbers
    else:
        lo, hi = None, None
    return Salary(lo, hi, currency, period, raw)


def evaluate(vacancy):
    text = (vacancy.title + "\n" + vacancy.description + "\n" + vacancy.requirements).lower()
    if vacancy.closed:
        return Decision("closed", "Вакансия закрыта")
    if vacancy.test_required:
        return Decision("rejected", "Пропущена: предложено пройти тест")
    if not re.search(r"\bpython\b|\bпитон\w*", text):
        return Decision("rejected", "Python не упомянут в задачах или требованиях")
    if vacancy.remote is False:
        return Decision("rejected", "Удалённая работа не предусмотрена")
    if vacancy.remote is None:
        return Decision("needs_clarification", "Не подтверждён удалённый формат")
    trips = re.sub(r"без командиров\w*|командировки не (?:требуются|предусмотрены)|no (?:business )?travel", "", text)
    if re.search(r"командиров|business travel|travel required", trips):
        return Decision("rejected", "Указаны командировки")
    if re.search(r"(?:только|обязательно|необходимо|нужно|must|only).{0,55}(?:прожива|находи|москв|рф|росси|resid|located)|(?:удал[её]н\w*|remote).{0,30}(?:из |within |from )", text):
        return Decision("needs_clarification", "Нужно уточнить территориальные ограничения удалённой работы")
    middle = bool(re.search(r"\b(?:middle|mid|senior|lead|principal|staff)\b|мид[д]?л|сеньор|сениор|ведущ\w*|старший|руководител", text))
    junior = bool(re.search(r"\b(?:junior|trainee|intern)\b|джун|стаж[её]р", vacancy.title.lower()))
    salary = vacancy.salary
    if salary is None:
        if middle:
            return Decision("ready", "Зарплата не указана; явно указан Middle или выше")
        return Decision("rejected" if junior else "needs_clarification", "Без зарплаты нужен подтверждённый Middle или выше")
    lo, hi = salary.minimum, salary.maximum
    if salary.currency != "RUB" or salary.period != "month":
        return Decision("needs_clarification", "Нужен актуальный курс или подтверждённая месячная сумма")
    if (lo is None and hi is None) or any(x is not None and x <= 0 for x in (lo,hi)) or (lo is not None and hi is not None and lo > hi):
        return Decision("needs_clarification", "Не удалось однозначно определить зарплатную вилку")
    if hi is not None:
        return Decision("ready" if hi >= 150000 else "rejected", "Вилка допускает 150 000 ₽" if hi >= 150000 else "Максимум ниже 150 000 ₽")
    if lo >= 150000:
        return Decision("ready", "От 150 000 ₽ в месяц; налоговый режим не пересчитывается")
    if middle:
        return Decision("ready", "Middle+; верхняя граница не указана — уточнить зарплату")
    return Decision("needs_clarification", "Нижняя граница ниже 150 000 ₽, максимум и Middle+ не подтверждены")
