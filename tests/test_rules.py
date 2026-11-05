import unittest
from job_finder.models import Vacancy, Salary
from job_finder.rules import evaluate, parse_salary


def vacancy(**changes):
    values = dict(id="123", title="Python разработчик", description="Разработка API на Python", remote=True)
    values.update(changes)
    return Vacancy(**values)


class SelectionTests(unittest.TestCase):
    def test_salary_boundaries_and_grade(self):
        cases = [
            (Salary(150000, 150000), "Junior", "ready"),
            (Salary(149999, 149999), "Senior", "rejected"),
            (Salary(100000, 200000), "Junior", "ready"),
            (Salary(None, 150000), "Junior", "ready"),
            (Salary(150000, None), "Junior", "ready"),
            (Salary(100000, None), "Middle", "ready"),
            (Salary(100000, None), "Junior", "needs_clarification"),
            (None, "Senior", "ready"),
            (None, "Junior/Middle", "ready"),
            (None, "Junior", "rejected"),
            (None, "от 3 лет опыта", "needs_clarification"),
            (Salary(3000, 4000, "USD"), "Middle", "needs_clarification"),
            (Salary(2000, 3000, period="hour"), "Middle", "needs_clarification"),
            (Salary(200000, 100000), "Senior", "needs_clarification"),
        ]
        for salary, grade, expected in cases:
            with self.subTest(salary=salary, grade=grade):
                self.assertEqual(evaluate(vacancy(title=grade+" Python", salary=salary)).status, expected)

    def test_company_python_is_not_relevance(self):
        self.assertEqual(evaluate(vacancy(title="Java developer", description="Java и Spring", company="Python Inc", salary=Salary(200000))).status, "rejected")

    def test_missing_skills_do_not_reduce_coverage(self):
        self.assertEqual(evaluate(vacancy(title="Senior QA", description="Python желательно, Kubernetes обязателен", salary=None)).status, "ready")

    def test_closed_and_remote_constraints(self):
        for change, status in [({"test_required":True},"ready"), ({"closed":True},"closed"), ({"remote":False},"rejected"), ({"remote":None},"needs_clarification"), ({"description":"Python, готовность к командировкам"},"rejected"), ({"description":"Python. Удалённо только из Москвы"},"needs_clarification")]:
            with self.subTest(change=change):
                self.assertEqual(evaluate(vacancy(salary=Salary(200000), **change)).status,status)

    def test_test_requirement_never_rejects(self):
        # Правило пропуска тестов (05.11.2026): тест не отклоняет вакансию,
        # метка добавляется в карточку движком, а не правилами.
        job=vacancy(test_required=True,salary=Salary(200000))
        self.assertEqual(evaluate(job).status,"ready")

    def test_salary_text_is_not_guessed(self):
        for raw, minimum, maximum, currency, period in [
            ("от 150 000 ₽ за месяц, до вычета налогов",150000,None,"RUB","month"),
            ("100 000 – 200 000 руб. за месяц на руки",100000,200000,"RUB","month"),
            ("до 150 000 ₽ за месяц",None,150000,"RUB","month"),
            ("от 2 000 $ за час",2000,None,"USD","hour"),
            ("от 200 000 ₽",200000,None,"RUB","unknown"),
        ]:
            with self.subTest(raw=raw):
                value=parse_salary(raw)
                self.assertEqual((value.minimum,value.maximum,value.currency,value.period),(minimum,maximum,currency,period))

    def test_unparseable_salary_requires_clarification(self):
        self.assertEqual(evaluate(vacancy(title="Senior Python",salary=parse_salary("по договоренности ???"))).status,"needs_clarification")

    def test_mentoring_by_senior_is_not_the_vacancy_grade(self):
        self.assertEqual(evaluate(vacancy(title="Python developer",description="Python. Работать с senior разработчиками, наставник senior.")).status,"needs_clarification")

    def test_explicit_grade_in_requirements_is_accepted(self):
        self.assertEqual(evaluate(vacancy(title="Python developer",description="Python. Требуемый уровень: Middle+.")).status,"ready")

    def test_junior_with_senior_mentor_is_not_middle(self):
        job=vacancy(title="Junior Python разработчик",description="Python. Ищем junior разработчика под руководством senior.")
        self.assertEqual(evaluate(job).status,"rejected")
