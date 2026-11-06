import unittest
from job_finder.config import PROFILES
from job_finder.models import Salary, Vacancy
from job_finder.rules import evaluate, evaluate_all, parse_salary

RU = PROFILES["ru"]
INTL = PROFILES["intl"]


def vacancy(**changes):
    values = dict(id="123", title="Python разработчик", description="Разработка API на Python", remote=True)
    values.update(changes)
    return Vacancy(**values)


def status(job, profile=RU):
    return evaluate(job, profile).status


class SalaryParsingTests(unittest.TestCase):
    def test_salary_text_is_not_guessed(self):
        for raw, minimum, maximum, currency, period in [
            ("от 150 000 ₽ за месяц, до вычета налогов", 150000, None, "RUB", "month"),
            ("100 000 – 200 000 руб. за месяц на руки", 100000, 200000, "RUB", "month"),
            ("до 150 000 ₽ за месяц", None, 150000, "RUB", "month"),
            ("от 2 000 $ за час", 2000, None, "USD", "hour"),
            ("от 200 000 ₽", 200000, None, "RUB", "unknown"),
        ]:
            with self.subTest(raw=raw):
                value = parse_salary(raw)
                self.assertEqual((value.minimum, value.maximum, value.currency, value.period),
                                 (minimum, maximum, currency, period))


class CommonRuleTests(unittest.TestCase):
    def test_python_is_required_everywhere(self):
        self.assertEqual(status(vacancy(title="Java developer", description="Java и Spring",
                                        salary=Salary(200000))), "rejected")
        self.assertEqual(status(vacancy(title="Java developer", description="Java и Spring",
                                        salary=Salary(200000)), INTL), "rejected")

    def test_company_python_is_not_relevance(self):
        self.assertEqual(status(vacancy(title="Java developer", description="Java",
                                        company="Python Inc", salary=Salary(200000))), "rejected")

    def test_closed_wins(self):
        self.assertEqual(status(vacancy(closed=True, salary=Salary(200000))), "closed")

    def test_test_requirement_never_rejects(self):
        job = vacancy(test_required=True, salary=Salary(200000))
        self.assertEqual(status(job), "ready")

    def test_trips_rule_is_ru_only(self):
        self.assertEqual(status(vacancy(description="Python, готовность к командировкам",
                                        salary=Salary(200000))), "rejected")
        self.assertEqual(status(vacancy(description="Python, готовность к командировкам",
                                        salary=Salary(200000)), INTL), "ready")
        self.assertEqual(status(vacancy(description="Python, без командировок",
                                        salary=Salary(200000))), "ready")
        self.assertEqual(status(vacancy(description="Python, no business travel",
                                        salary=Salary(200000))), "ready")


class RuFormatTests(unittest.TestCase):
    """7.1: каждый исход формата RU."""
    def test_remote_without_geo_fits(self):
        self.assertEqual(status(vacancy(salary=Salary(200000))), "ready")

    def test_remote_only_russia_fits(self):
        self.assertEqual(status(vacancy(remote_geo="только РФ", salary=Salary(200000))), "ready")

    def test_remote_foreign_only_rejected(self):
        for geo in ("US-only", "EMEA", "outside the US", "вне РФ"):
            with self.subTest(geo=geo):
                self.assertEqual(status(vacancy(remote_geo=geo, salary=Salary(200000))), "rejected")

    def test_moscow_office_fits_with_label(self):
        assessment = evaluate(vacancy(remote=False, location="Москва", salary=Salary(200000)), RU)
        self.assertEqual(assessment.status, "ready")
        self.assertIn("Офис/гибрид, Москва", assessment.reason)

    def test_moscow_and_region_fits(self):
        self.assertEqual(status(vacancy(remote=None, location="Москва и МО",
                                        salary=Salary(200000))), "ready")

    def test_moscow_oblast_is_subtracted(self):
        for location in ("Московская область", "Подмосковье"):
            with self.subTest(location=location):
                self.assertEqual(status(vacancy(remote=None, location=location,
                                                salary=Salary(200000))), "needs_clarification")

    def test_office_outside_moscow_rejected(self):
        self.assertEqual(status(vacancy(remote=False, location="Санкт-Петербург",
                                        salary=Salary(200000))), "rejected")

    def test_remote_unknown_without_city_needs_clarification(self):
        self.assertEqual(status(vacancy(remote=None, salary=Salary(200000))), "needs_clarification")

    def test_relocation_to_moscow_fits(self):
        for text in ("Релокация в Москву", "relocation to Moscow"):
            with self.subTest(text=text):
                assessment = evaluate(vacancy(description="Python. " + text,
                                              salary=Salary(200000)), RU)
                self.assertEqual(assessment.status, "ready")
                self.assertIn("Релокация в Москву", assessment.reason)

    def test_relocation_without_city_needs_clarification(self):
        self.assertEqual(status(vacancy(description="Python. Предлагаем relocation",
                                        salary=Salary(200000))), "needs_clarification")

    def test_relocation_to_other_city_rejected(self):
        for text in ("Релокация в Казань", "relocation to Belgrade"):
            with self.subTest(text=text):
                self.assertEqual(status(vacancy(description="Python. " + text,
                                                salary=Salary(200000))), "rejected")


class IntlFormatTests(unittest.TestCase):
    """7.2: каждый исход формата intl."""
    def test_remote_without_geo_fits(self):
        self.assertEqual(status(vacancy(salary=Salary(100000)), INTL), "ready")

    def test_remote_worldwide_fits(self):
        self.assertEqual(status(vacancy(remote_geo="worldwide", salary=Salary(100000)), INTL), "ready")

    def test_remote_foreign_only_rejected(self):
        for geo in ("US-only", "EMEA", "outside the US"):
            with self.subTest(geo=geo):
                self.assertEqual(status(vacancy(remote_geo=geo, salary=Salary(300000)), INTL), "rejected")

    def test_dubai_office_fits(self):
        for location in ("Дубай", "Dubai"):
            with self.subTest(location=location):
                self.assertEqual(status(vacancy(remote=False, location=location,
                                                salary=Salary(200000)), INTL), "ready")

    def test_relocation_to_dubai_fits(self):
        self.assertEqual(status(vacancy(description="Python. relocation to Dubai",
                                        salary=Salary(200000)), INTL), "ready")

    def test_relocation_without_city_office_allowed(self):
        # Решение 06.11.2026: предложенная релокация допускает офис (intl).
        assessment = evaluate(vacancy(description="Python. relocation",
                                      salary=Salary(200000)), INTL)
        self.assertEqual(assessment.status, "ready")
        self.assertIn("релокация предлагается", assessment.reason)

    def test_relocation_to_other_city_office_allowed(self):
        # Решение 06.11.2026: релокация в другой город допускает офис (intl).
        assessment = evaluate(vacancy(description="Python. relocation to Warsaw",
                                      salary=Salary(200000)), INTL)
        self.assertEqual(assessment.status, "ready")
        self.assertIn("релокация в Warsaw", assessment.reason)

    def test_nothing_specified_needs_clarification(self):
        self.assertEqual(status(vacancy(remote=None, salary=Salary(200000)), INTL), "needs_clarification")

    def test_office_outside_dubai_rejected(self):
        self.assertEqual(status(vacancy(remote=False, location="Варшава",
                                        salary=Salary(200000)), INTL), "rejected")

    def test_junior_rejected_always_even_with_salary(self):
        # Приоритет регионального правила над общим (R9).
        self.assertEqual(status(vacancy(title="Junior Python", salary=Salary(400000)), INTL), "rejected")


class RuSalaryTableTests(unittest.TestCase):
    """7.3: каждая клетка единой таблицы зарплатных решений."""
    def test_fixed_at_or_above_threshold_ready(self):
        for salary in (Salary(150000, 150000), Salary(250000, 250000)):
            with self.subTest(salary=salary):
                self.assertEqual(status(vacancy(salary=salary)), "ready")

    def test_fixed_below_threshold_rejected(self):
        self.assertEqual(status(vacancy(salary=Salary(149999, 149999))), "rejected")

    def test_range_with_top_at_threshold_ready(self):
        self.assertEqual(status(vacancy(salary=Salary(100000, 150000))), "ready")

    def test_range_max_below_threshold_rejected_regardless_of_grade_and_stack(self):
        # Senior + FastAPI всё равно отклоняются: максимум ниже порога.
        job = vacancy(title="Senior Python", requirements="FastAPI", salary=Salary(120000, 149999))
        self.assertEqual(status(job), "rejected")

    def test_from_at_threshold_ready(self):
        self.assertEqual(status(vacancy(salary=Salary(150000, None))), "ready")

    def test_from_below_threshold_with_middle_ready(self):
        self.assertEqual(status(vacancy(title="Middle Python", salary=Salary(100000, None))), "ready")

    def test_from_below_threshold_unset_grade_with_stack_ready(self):
        self.assertEqual(status(vacancy(requirements="FastAPI, asyncio",
                                        salary=Salary(100000, None))), "ready")

    def test_from_below_threshold_unset_grade_without_stack_needs_clarification(self):
        job = vacancy(title="Backend-разработчик", description="Работа на Python",
                      salary=Salary(100000, None))
        assessment = evaluate(job, RU)
        self.assertEqual(assessment.status, "needs_clarification")
        self.assertIn("уточните зарплату", assessment.reason)

    def test_from_below_threshold_junior_needs_clarification(self):
        self.assertEqual(status(vacancy(title="Junior Python",
                                        salary=Salary(100000, None))), "needs_clarification")

    def test_yearly_salary_is_normalized_to_month(self):
        job = vacancy(salary=Salary(2400000, 2400000, "RUB", "year", "2 400 000 ₽ в год"))
        assessment = evaluate(job, RU)
        self.assertEqual(assessment.status, "ready")
        self.assertIn("Годовая зарплата ≈ 200000 ₽/мес", assessment.reason)
        self.assertEqual(status(vacancy(salary=Salary(1200000, 1200000, "RUB", "year"))), "rejected")

    def test_hourly_salary_needs_clarification(self):
        self.assertEqual(status(vacancy(title="Middle Python",
                                        salary=Salary(3000, 4000, "RUB", "hour"))), "needs_clarification")

    def test_unknown_period_needs_clarification(self):
        self.assertEqual(status(vacancy(salary=Salary(200000, 200000, "RUB", "unknown"))),
                         "needs_clarification")

    def test_foreign_currency_ignores_threshold_with_middle(self):
        self.assertEqual(status(vacancy(title="Middle Python",
                                        salary=Salary(500, 500, "USD", "month"))), "ready")

    def test_foreign_currency_unset_grade_with_stack_ready(self):
        self.assertEqual(status(vacancy(requirements="Django",
                                        salary=Salary(500, 500, "USD", "month"))), "ready")

    def test_foreign_currency_unset_grade_without_stack_rejected(self):
        job = vacancy(title="Backend-разработчик", description="Работа на Python",
                      salary=Salary(500, 500, "USD", "month"))
        self.assertEqual(status(job), "rejected")

    def test_no_salary_with_middle_ready(self):
        self.assertEqual(status(vacancy(title="Middle Python", salary=None)), "ready")
        self.assertEqual(status(vacancy(title="Senior Python", salary=None)), "ready")

    def test_no_salary_unset_grade_with_stack_ready(self):
        self.assertEqual(status(vacancy(title="Python-разработчик", requirements="SQLAlchemy, pytest",
                                        salary=None)), "ready")
        # Слово «Python» в названии считается стеком.
        self.assertEqual(status(vacancy(title="Python-разработчик", salary=None)), "ready")

    def test_no_salary_unset_grade_without_stack_rejected(self):
        self.assertEqual(status(vacancy(title="Backend-разработчик", salary=None)), "rejected")

    def test_no_salary_junior_rejected(self):
        self.assertEqual(status(vacancy(title="Junior Python", salary=None)), "rejected")

    def test_junior_with_suitable_salary_passes_in_ru(self):
        # Общее правило уровня: Junior проходит с подходящей зарплатой.
        self.assertEqual(status(vacancy(title="Junior Python", salary=Salary(160000, 160000))), "ready")

    def test_invalid_range_needs_clarification(self):
        self.assertEqual(status(vacancy(title="Senior Python",
                                        salary=Salary(200000, 100000))), "needs_clarification")


class IntlSalaryTests(unittest.TestCase):
    def test_salary_never_affects_intl_outcome(self):
        # Порога нет; валюта и суммы не влияют на исход.
        self.assertEqual(status(vacancy(salary=Salary(100000, 100000)), INTL), "ready")
        self.assertEqual(status(vacancy(salary=Salary(500, 500, "USD", "month")), INTL), "ready")
        self.assertEqual(status(vacancy(salary=None), INTL), "ready")


class GradeTests(unittest.TestCase):
    def test_mentoring_by_senior_is_not_the_vacancy_grade(self):
        job = vacancy(title="Backend-разработчик",
                      description="Python. Работать с senior разработчиками, наставник senior.")
        assessment = evaluate(job, RU)
        self.assertEqual(assessment.status, "rejected")
        self.assertIsNone(assessment.grade)

    def test_explicit_grade_in_requirements_is_accepted_with_evidence(self):
        job = vacancy(description="Python. Требуемый уровень: Middle+.", salary=Salary(100000, None))
        assessment = evaluate(job, RU)
        self.assertEqual(assessment.status, "ready")
        self.assertEqual(assessment.grade, "middle")
        self.assertTrue(assessment.grade_evidence)

    def test_junior_with_senior_mentor_is_not_middle(self):
        job = vacancy(title="Junior Python разработчик",
                      description="Python. Ищем junior разработчика под руководством senior.")
        self.assertEqual(status(job), "rejected")

    def test_mixed_junior_middle_title_counts_as_middle(self):
        job = vacancy(title="Junior/Middle Python", salary=Salary(100000, None))
        assessment = evaluate(job, RU)
        self.assertEqual(assessment.status, "ready")
        self.assertEqual(assessment.grade, "middle")

    def test_senior_title_alone_is_middle_plus(self):
        job = vacancy(title="Senior Python Developer", salary=None)
        assessment = evaluate(job, RU)
        self.assertEqual(assessment.status, "ready")
        self.assertEqual(assessment.grade, "middle")


class ProfileSelectionTests(unittest.TestCase):
    """R1: оценка по всем активным профилям; проходит, если проходит любой."""
    def test_passes_if_any_profile_passes(self):
        # RUB-зарплата ниже порога: RU отклоняет, intl пропускает (порога нет).
        job = vacancy(salary=Salary(100000, 100000))
        self.assertEqual(evaluate(job, RU).status, "rejected")
        assessment = evaluate_all(job, (RU, INTL))
        self.assertEqual(assessment.status, "ready")
        self.assertEqual(assessment.profile, "intl")

    def test_ru_wins_for_moscow_office(self):
        job = vacancy(remote=False, location="Москва", salary=Salary(200000))
        self.assertEqual(evaluate(job, INTL).status, "rejected")
        assessment = evaluate_all(job, (RU, INTL))
        self.assertEqual(assessment.status, "ready")
        self.assertEqual(assessment.profile, "ru")
        self.assertIn("Офис/гибрид, Москва", assessment.reason)

    def test_needs_clarification_beats_rejection(self):
        # Junior + «от 100k» без верха: RU — уточнение, intl — отказ (R9).
        job = vacancy(title="Junior Python", salary=Salary(100000, None))
        self.assertEqual(evaluate(job, RU).status, "needs_clarification")
        self.assertEqual(evaluate(job, INTL).status, "rejected")
        self.assertEqual(evaluate_all(job, (RU, INTL)).status, "needs_clarification")

    def test_reason_marks_profile(self):
        job = vacancy(title="Java", salary=Salary(200000))
        assessment = evaluate_all(job, (RU, INTL))
        self.assertTrue(assessment.reason.startswith("профиль "))
