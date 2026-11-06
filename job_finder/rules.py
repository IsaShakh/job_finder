"""Правила отбора v4 (разделы 7, 7.3 плана; 05–06.11.2026).

`evaluate(vacancy, profile)` оценивает одну вакансию одним профилем;
`evaluate_all` прогоняет её по всем активным профилям — вакансия проходит,
если проходит хотя бы один (R1: факты вакансии важнее дефолта источника).
Грейд возвращается с основанием (grade/grade_evidence) для карточки.
Тест больше не отклоняет вакансию (метка — в движке, 1b).

v4 — белый список стран (06.11.2026): офис/релокация и гео-ограничения
удалёнки только для России (RU) и Сербии, Польши, Грузии, Армении,
Казахстана, Таиланда, Австралии, Канады, ОАЭ (intl).
"""
import re
from dataclasses import dataclass
from typing import Optional

from .models import Salary

# Версия правил: строки, решённые старыми правилами, переоцениваются
# движком при расхождении (16.2). Меняется при изменении критериев.
RULES_VERSION = "v4"

PYTHON_RE = re.compile(r"\bpython\b|\bпитон\w*")
MIDDLE_RE = re.compile(r"\b(?:middle|mid|senior|lead|principal|staff)\b|мид[д]?л|сеньор|сениор|ведущ\w*|старший|руководител|тимлид|техлид")
JUNIOR_RE = re.compile(r"\b(?:junior|trainee|intern)\b|джун|стаж[её]р")
STACK_RES = (
    re.compile(r"\bfastapi\b"), re.compile(r"\bdjango\b"), re.compile(r"\bflask\b"),
    re.compile(r"\basyncio\b"), re.compile(r"\bsqlalchemy\b"), re.compile(r"\bpytest\b"),
    re.compile(r"\baiohttp\b"), re.compile(r"\bpydantic\b"), re.compile(r"\bcelery\b"),
    re.compile(r"\bstarlette\b"), re.compile(r"\btortoise\b"),
)
# Белый список стран (решение 06.11.2026, вторая волна): офис, релокация и
# гео-ограничения удалёнки принимаются только для этих стран. Для RU-профиля
# разрешённая география — Россия (офис Москва — прежняя логика N10).
RU_GEO = {"Россия": ("росси", "рф", "russia", "москв", "moscow")}
INTL_TARGETS = {
    "Сербия": ("серби", "serbia", "serb", "белград", "belgrade", "нови сад", "novi sad"),
    "Польша": ("польш", "poland", "polish", "варшав", "warsaw", "краков", "krakow",
               "вроцлав", "wroclaw", "гданск", "gdansk", "познан", "poznan"),
    "Грузия": ("грузи", "georgia", "georgian", "тбилиси", "tbilisi", "батуми", "batumi"),
    "Армения": ("армени", "armenia", "armenian", "ереван", "yerevan"),
    "Казахстан": ("казахстан", "kazakhstan", "алматы", "almaty", "астан", "astana"),
    "Таиланд": ("таиланд", "thailand", "thai", "бангкок", "bangkok", "пхукет", "phuket"),
    "Австралия": ("австрали", "australia", "australi", "сидней", "sydney",
                  "мельбурн", "melbourne"),
    "Канада": ("канад", "canada", "canad", "торонто", "toronto", "ванкувер", "vancouver",
               "монреал", "montreal"),
    "ОАЭ": ("оаэ", "uae", "дуба", "dubai"),
}
# Ограничения географии удалёнки. EN: «X only»/«only for X»; RU: «только X»
# со словами-странами (полный список «только <что угодно>» не нужен: «только
# удалёнка» не считается географией); EMEA/APAC — регионы-ограничения сами по
# себе. Вакансии, явно исключающие граждан РФ, не проходят никогда.
GEO_ONLY_EN_RE = re.compile(
    r"\b([a-zа-яё-]{2,20}(?:[ -][a-zа-яё-]{2,20})?)[- ]only\b"
    r"|\bonly\s+(?:for|from|within)?\s*([a-zа-яё-]{2,20}(?:[ -][a-zа-яё-]{2,20})?)", re.I)
RU_COUNTRY_WORDS = (
    "казахстан", "узбекистан", "азербайджан", "нидерланд", "филиппин", "индонези",
    "австрали", "скандинав", "сингапур", "аргентин", "белорус", "малайзи", "прибалтик",
    "вьетнам", "таиланд", "болгари", "израил", "румыни", "венгри", "герман", "испани",
    "итали", "франц", "мексик", "бразили", "украин", "европ", "кипр", "турц", "кита",
    "инди", "япони", "коре", "зеланд", "чехи", "америк", "канад", "грузи", "армени",
    "серби", "польш", "росси", "белару", "дуба", "оаэ", "снг", "сша", "рф",
)
RU_GEO_RESTRICT_RE = re.compile(
    r"\bтолько\s+(?:для|из)?\s*(?:граждан|резидентов)?\s*(" + "|".join(RU_COUNTRY_WORDS) + r")\w*",
    re.I)
RF_EXCLUDE_RE = re.compile(
    r"\bвне\s+рф\b|\bтолько\s+не\s+рф\b"
    r"|\bне\s+(?:рассматриваем|принимаем|нанимаем|берем|берём)\s+(?:заявки\s+)?(?:от\s+|из\s+)?(?:граждан\s+)?рф\b"
    r"|\bграждан\s+рф\s+не\b|\bгражданство\s+рф\s+не\b"
    r"|\bno\s+russian\s+(?:citizens|citizenship|passports|residents)\b"
    r"|\bnot\s+(?:accepting|considering|hiring)\s+(?:russian\s+)?(?:citizens|applicants)\b"
    r"|\brussian\s+citizens\s+not\s+(?:accepted|eligible|considered)\b", re.I)
REGION_WORD_RE = re.compile(r"\b(emea|apac)\b", re.I)
GEO_OK_WORDS = ("worldwide", "global", "anywhere", "remote", "удалённо", "удаленно",
                "удалённая", "удаленная", "все", "мир", "ru", "intl")
RELOC_RE = re.compile(r"релок\w*|relocation", re.I)
RELOC_TARGET_RE = re.compile(r"(?:в|to|into?)\s+([а-яёa-z-]{2,40}(?:\s+[а-яёa-z-]{2,40})?)", re.I)
OFFICE_MARK_RE = re.compile(r"офис|office|гибрид|hybrid", re.I)
TRIPS_RE = re.compile(r"командиров|business travel|travel required", re.I)
NO_TRIPS_RE = re.compile(r"без командиров\w*|командировки не (?:требуются|предусмотрены)|no (?:business )?travel", re.I)

# Стоп-паттерны N10: «Московская область»/«Подмосковье» вычитаются
# при проверке Москвы; «Москва и МО» — подходит.
MOSCOW_STOP = ("московская область", "московская обл", "подмосковье")
# Склонения и латинские названия целевых городов релокации RU-профиля.
CITY_STEMS = {"москва": ("москв", "moscow"), "дубай": ("дуба", "dubai")}
RELOC_PHRASE = {"Москва": "Релокация в Москву", "Дубай": "Релокация в Дубай"}


@dataclass(frozen=True)
class Assessment:
    status: str
    reason: str
    profile: str = ""
    grade: Optional[str] = None      # 'middle' | 'junior' | None (не указан)
    grade_evidence: str = ""


def parse_salary(raw):
    text = raw.lower().replace("\xa0", " ").replace("\u202f", " ")
    numbers = [int(re.sub(r"\D", "", n)) for n in re.findall(r"\d{1,3}(?:[ ]\d{3})+|\d+", text)]
    if re.search(r"\bтыс\b|тысяч", text):
        numbers = [number * 1000 for number in numbers]
    currency = next((c for pattern, c in [(r"₽|руб|rur|rub", "RUB"), (r"\$|usd|доллар", "USD"), (r"€|eur|евро", "EUR"), (r"₸|тенге|kzt", "KZT")] if re.search(pattern, text)), "unknown")
    period = next((p for pattern, p in [(r"месяц|month", "month"), (r"час|hour", "hour"), (r"год|year", "year"), (r"смен|день|day", "day")] if re.search(pattern, text)), "unknown")
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


def _snippet(text, match, width=40):
    start = max(0, match.start() - width)
    end = min(len(text), match.end() + width)
    return text[start:end].strip()


def detect_grade(vacancy):
    """Грейд с основанием: (grade, evidence); 'middle'/'junior'/None."""
    title = vacancy.title.lower()
    text = (vacancy.title + "\n" + vacancy.description + "\n" + vacancy.requirements).lower()
    contexts = re.findall(
        r"(?:ищем|вакансия|уровень|позиция|грейд|looking for|position|level|grade)[:\s-]*([^.!?\n]{0,60})", text)
    # Упоминания менторства не повышают грейд вакансии.
    context = title + " " + " ".join(
        re.split(r"под руководством|с наставником|наставник|в команде с|работать с|under supervision|mentored by",
                 part)[0] for part in contexts)
    middle_match = MIDDLE_RE.search(context)
    junior = bool(JUNIOR_RE.search(context))
    junior_title = bool(JUNIOR_RE.search(title))
    middle = bool(middle_match)
    if junior_title and not MIDDLE_RE.search(title):
        explicitly_mixed = bool(re.search(
            r"(?:junior|джун\w*)\s*(?:/|или|or|-)\s*(?:middle|мидд?л)|рассматриваем\s+(?:также\s+)?middle", text))
        middle = middle and explicitly_mixed
    if middle:
        return "middle", "грейд: " + _snippet(context, middle_match)
    if junior:
        junior_match = JUNIOR_RE.search(context)
        return "junior", "грейд: " + (_snippet(context, junior_match) if junior_match else "Junior в вакансии")
    return None, "грейд не указан"


def has_python_stack(vacancy):
    """Python-стек в вакансии; слово «Python» в названии тоже считается (7.1)."""
    if PYTHON_RE.search(vacancy.title.lower()):
        return True
    text = (vacancy.title + "\n" + vacancy.description + "\n" + vacancy.requirements).lower()
    return any(pattern.search(text) for pattern in STACK_RES)


def has_trips(text):
    cleaned = NO_TRIPS_RE.sub("", text)
    return bool(TRIPS_RE.search(cleaned))


def _city_in(location, city):
    """Город в локации с вычетом стоп-паттернов (N10) и латинскими алиасами."""
    if not location:
        return False
    text = location.lower()
    if city == "москва":
        for stop in MOSCOW_STOP:
            text = text.replace(stop, " ")
    stems = CITY_STEMS.get(city, (city,))
    return city in text or any(alias in text for alias in stems)


def _match_target(targets, text):
    """Название страны из targets, чей стем встретился в text; None — нет."""
    lowered = (text or "").lower()
    for country, stems in targets.items():
        if any(stem in lowered for stem in stems):
            return country
    return None


def relocation_target(text, profile):
    """(kind, label): релокация в разрешённую страну/город, в другую или без цели.

    RU-профиль: только Москва (прежнее правило); intl — белый список стран
    (06.11.2026): релокация в страну из списка допускает офис, в другую —
    rejected, без города — needs_clarification."""
    match = RELOC_RE.search(text)
    if not match:
        return None
    window = text[match.end():match.end() + 120]
    if profile.region == "RU":
        stems = CITY_STEMS.get(profile.city.lower(), (profile.city.lower(),))
        for target in RELOC_TARGET_RE.findall(window):
            normalized = target.strip().lower().rstrip(".,;")
            if any(normalized.startswith(stem) for stem in stems):
                return "city", RELOC_PHRASE.get(profile.city, "Релокация в " + profile.city)
            return "other", target.strip()
        return "none", None
    for target in RELOC_TARGET_RE.findall(window):
        country = _match_target(INTL_TARGETS, target)
        if country:
            return "city", "Офис/гибрид, релокация в %s (%s)" % (target.strip().capitalize(), country)
        return "other", target.strip()
    return "none", None


def remote_geo_ok(profile, geo):
    """Удалёнка и ограничения её географии (уточнение 06.11.2026).

    Ограничение на конкретную территорию («Poland only», «только для граждан
    Казахстана», US-only, EMEA…) — rejected для обоих профилей; исключение —
    «только РФ» для RU-профиля (гражданин РФ работает из РФ). Удалёнка без
    ограничений проходит («удалёнка любая» для RU). Исключения граждан РФ
    проверяются отдельно (RF_EXCLUDE_RE) по всему тексту вакансии."""
    if not geo:
        return True
    if RF_EXCLUDE_RE.search(geo):
        return False
    targets = [next(group for group in match.groups() if group)
               for match in GEO_ONLY_EN_RE.finditer(geo)]
    targets += [match.group(1) for match in RU_GEO_RESTRICT_RE.finditer(geo)]
    targets += REGION_WORD_RE.findall(geo)
    if targets:
        if profile.region == "RU":
            return all(_match_target(RU_GEO, target) is not None for target in targets)
        return False
    # Короткая строка без шаблонов — сама территория («US», «Europe»…).
    if re.fullmatch(r"[a-zа-яё-]{2,20}", geo) and geo not in GEO_OK_WORDS:
        return profile.region == "RU" and _match_target(RU_GEO, geo) is not None
    return True


def evaluate_format(profile, vacancy, text):
    """Формат RU/intl (7.1, 7.2): (ok|rejected|needs_clarification, reason, label).

    Белый список стран (06.11.2026, вторая волна): офис и релокация — только
    в разрешённых странах (RU-профиль: Россия/Москва; intl: Сербия, Польша,
    Грузия, Армения, Казахстан, Таиланд, Австралия, Канада, ОАЭ). Удалёнка:
    без ограничений — ок; ограничение на конкретную территорию — rejected
    (для RU исключение «только РФ»); исключения граждан РФ — rejected всегда."""
    city = profile.city.lower()
    geo = (vacancy.remote_geo or "").lower()
    location = vacancy.location or ""
    reloc = relocation_target(text, profile)
    if reloc:
        kind, label = reloc
        if kind == "city":
            return "ok", label
        if kind == "other":
            if profile.region == "intl":
                return "rejected", "Релокация в страну вне списка (%s)" % label
            return "rejected", "Релокация в другой город (%s)" % label
        return "needs_clarification", "Релокация без целевой страны"
    if vacancy.remote is True:
        if not remote_geo_ok(profile, geo):
            return "rejected", "Удалёнка ограничена чужой территорией"
        return "ok", "Удалённая работа подтверждена"
    if profile.region == "RU":
        if _city_in(location, city):
            return "ok", "Офис/гибрид, " + profile.city
        if vacancy.remote is False:
            return "rejected", "Удалённая работа не предусмотрена"
        return "needs_clarification", "Не подтверждён удалённый формат"
    # intl: офис — только в странах белого списка; страна может быть названа
    # в тексте поста канала, а не только в поле локации.
    country = _match_target(INTL_TARGETS, location)
    if country:
        return "ok", "Офис/гибрид, " + country
    if vacancy.remote is False or OFFICE_MARK_RE.search(text):
        country = _match_target(INTL_TARGETS, text)
        if country:
            return "ok", "Офис/гибрид, " + country
        return "rejected", "Офис вне списка стран; удалёнка не предусмотрена"
    return "needs_clarification", "Формат и география не указаны"


def evaluate_salary(profile, vacancy, grade):
    """Единая таблица зарплатных решений 7.3; None — зарплата не влияет."""
    if profile.region == "intl" or not profile.threshold:
        return None
    threshold = profile.threshold
    salary = vacancy.salary
    stack = None  # лениво
    middle = grade == "middle"
    unset = grade is None

    def pass_or_reject(reason_ok):
        if middle or (unset and has_python_stack(vacancy)):
            return "ready", reason_ok
        return "rejected", "Грейд и Python-стек не подтверждены"

    if salary is None:
        return pass_or_reject("Зарплата не указана; Middle+ или Python-стек")
    if salary.currency != "RUB":
        # Порог не применяется; условия как «зарплата не указана».
        return pass_or_reject("Зарплата в " + salary.currency + "; порог не применяется")
    period = salary.period
    if period == "year":
        lo = salary.minimum // 12 if salary.minimum is not None else None
        hi = salary.maximum // 12 if salary.maximum is not None else None
        mark = "Годовая зарплата ≈ %s ₽/мес; " % (lo if lo is not None else hi)
    elif period == "month":
        lo, hi = salary.minimum, salary.maximum
        mark = ""
    else:
        return "needs_clarification", "Зарплата за период «%s»; уточните месячную сумму" % period
    if (lo is None and hi is None) or any(x is not None and x <= 0 for x in (lo, hi)) or (lo is not None and hi is not None and lo > hi):
        return "needs_clarification", "Не удалось однозначно определить зарплатную вилку"
    if hi is not None:
        if hi >= threshold:
            return "ready", mark + "Вилка допускает порог"
        return "rejected", mark + "Максимум ниже порога"
    if lo >= threshold:
        return "ready", mark + "Нижняя граница не ниже порога"
    if middle or (unset and has_python_stack(vacancy)):
        return "ready", mark + "Middle+ или Python-стек; верхняя граница не указана"
    return "needs_clarification", mark + "Нижняя граница ниже порога; уточните зарплату"


def evaluate(vacancy, profile):
    """Оценка одной вакансии одним профилем."""
    grade, grade_evidence = detect_grade(vacancy)
    text = (vacancy.title + "\n" + vacancy.description + "\n" + vacancy.requirements).lower()

    def result(status, reason):
        return Assessment(status, "профиль %s: %s" % (profile.name, reason),
                          profile.name, grade, grade_evidence)

    if vacancy.closed:
        return result("closed", "Вакансия закрыта")
    # Вакансии, явно исключающие граждан РФ, не проходят никогда
    # (уточнение 06.11.2026; проверяется по всему тексту, не только по гео).
    if RF_EXCLUDE_RE.search(text):
        return result("rejected", "Вакансия исключает граждан РФ")
    # Приоритет регионального правила над общим (R9): intl отклоняет Junior.
    if profile.region == "intl" and grade == "junior":
        return result("rejected", "Явный Junior/Trainee не подходит для зарубежного региона")
    if not PYTHON_RE.search(text):
        return result("rejected", "Python не упомянут в задачах или требованиях")
    if profile.region == "RU" and has_trips(text):
        return result("rejected", "Указаны командировки")
    status, reason = evaluate_format(profile, vacancy, text)
    if status != "ok":
        return result(status, reason)
    salary_decision = evaluate_salary(profile, vacancy, grade)
    if salary_decision is not None:
        status, salary_reason = salary_decision
        if status == "ready":
            return result("ready", reason + " · " + salary_reason)
        return result(status, salary_reason)
    return result("ready", reason + " · зарплата без порога в регионе intl" if profile.region == "intl" else reason)


def evaluate_all(vacancy, profiles):
    """Прогон по всем активным профилям; проходит, если проходит любой (R1)."""
    results = [evaluate(vacancy, profile) for profile in profiles]
    for status in ("ready", "needs_clarification", "rejected", "closed", "error"):
        for assessment in results:
            if assessment.status == status:
                return assessment
    return results[0]
