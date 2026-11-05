import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from job_finder.async_store import AsyncStore
from job_finder.config import Config
from job_finder.dedupe import (DEDUP_COSINE_THRESHOLD, EmbeddingClient, cosine,
                               content_hash, embedding_input, extract_links,
                               merge_kind, normalize_company, normalize_title)
from job_finder.engine import Engine
from job_finder.models import Letter, Salary, Vacancy
from tests.test_rules import vacancy


class NormalizationTests(unittest.TestCase):
    def test_company_normalization(self):
        cases = [
            ("ООО «Ромашка»", "ромашка"),
            ("ACME Inc.", "acme"),
            ("Точка, ООО и Компания", "точка и компания"),
            ("", ""),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_company(raw), expected)

    def test_title_normalization(self):
        self.assertEqual(normalize_title("Senior Python Developer"), "senior python developer")
        self.assertEqual(normalize_title("Сеньор-разработчик Python"), "senior разработчик python")
        self.assertEqual(normalize_title("  Python,   Dev!  "), "python dev")

    def test_exact_key_is_stable(self):
        first = (normalize_company("ООО «Альфа»"), normalize_title("Python разработчик"))
        second = (normalize_company("Альфа"), normalize_title("python разработчик"))
        self.assertEqual(first, second)


class TextFeatureTests(unittest.TestCase):
    def test_content_hash_detects_changes(self):
        job = vacancy(description="Описание A", requirements="FastAPI")
        self.assertEqual(content_hash(job), content_hash(vacancy(description="Описание A", requirements="FastAPI")))
        self.assertNotEqual(content_hash(job), content_hash(vacancy(description="Описание B", requirements="FastAPI")))
        self.assertNotEqual(content_hash(job), content_hash(vacancy(description="Описание A", requirements="Django")))

    def test_extract_links(self):
        job = vacancy(description="Вакансия https://hh.ru/vacancy/123 и https://habr.com/ru/companies/x/vacancies/456")
        self.assertEqual(extract_links(job), [("hh", "123"), ("habr", "456")])
        self.assertEqual(extract_links(vacancy(description="Без ссылок")), [])

    def test_embedding_input_prefers_requirements(self):
        job = vacancy(title="Python", company="Акме", description="Длинное описание",
                      requirements="FastAPI, asyncio")
        self.assertEqual(embedding_input(job), "Python\nАкме\nFastAPI, asyncio")

    def test_embedding_input_falls_back_to_description(self):
        job = vacancy(title="Python", company="Акме",
                      description="О компании: мы большие. " + "работы " * 300,
                      requirements="")
        text = embedding_input(job)
        self.assertNotIn("О компании", text)
        self.assertLessEqual(len(text), 900)


class CosineTests(unittest.TestCase):
    def test_cosine_math(self):
        self.assertAlmostEqual(cosine([1, 0, 0], [1, 0, 0]), 1.0)
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0.0)
        self.assertAlmostEqual(cosine([1, 1], [1, 1]), 1.0)
        self.assertEqual(cosine([1, 0], [1, 0, 0]), 0.0)
        self.assertEqual(cosine([], []), 0.0)

    def test_merge_policy_is_conservative(self):
        self.assertEqual(merge_kind(exact=["a"], links=[], embeddings=[]), "merge")
        self.assertEqual(merge_kind(exact=[], links=["b"], embeddings=[]), "merge")
        self.assertEqual(merge_kind(exact=[], links=[], embeddings=["c"]), "possible")
        self.assertEqual(merge_kind(exact=[], links=[], embeddings=[]), None)


class FakeEmbeddings(EmbeddingClient):
    def __init__(self, config, vectors):
        super().__init__(config)
        self.vectors = vectors
        self.available = True
        self.calls = 0

    async def embed(self, text):
        self.calls += 1
        lowered = text.lower()
        for key, vector in self.vectors.items():
            if key in lowered:
                return vector, len(vector), "test-model"
        return [0.0, 0.0, 0.0], 3, "test-model"


class SequenceEmbeddings(FakeEmbeddings):
    """Возвращает векторы по очереди — для пар с одинаковым текстом."""
    def __init__(self, config, sequence):
        super().__init__(config, {})
        self.sequence = list(sequence)

    async def embed(self, text):
        self.calls += 1
        return self.sequence.pop(0)


class DedupStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(__import__("os").environ, {}, clear=True):
            self.config = Config.load(Path(self.temp.name))
        self.store = AsyncStore(self.config.db_path)
        await self.store.start()
        self.addAsyncCleanup(self.store.close)

    async def test_exact_key_is_candidate_not_merge_without_embeddings(self):
        await self.store.discover("remoteok", vacancy(id="1", title="Python dev", company="Акме"))
        await self.store.refresh("remoteok", vacancy(id="1", title="Python dev", company="Акме"),
                                 norm_company="акме", norm_title="python dev",
                                 content_hash_value="h1")
        await self.store.discover("hh", vacancy(id="900001", title="Python Developer", company="Акме"))
        self.assertEqual(await self.store.exact_candidates("hh:900001", "акме", "python dev"),
                         ["remoteok:1"])
        # Без подтверждения эмбеддингом точный ключ не сливает (раздел 8).
        self.assertIsNone((await self.store.get("remoteok:1"))["cluster_id"])

    async def test_exact_key_confirmed_by_embedding_merges_with_hh_rep(self):
        await self.store.discover("remoteok", vacancy(id="1", title="Python dev", company="Акме"))
        await self.store.refresh("remoteok", vacancy(id="1", title="Python dev", company="Акме"),
                                 norm_company="акме", norm_title="python dev",
                                 content_hash_value="h1")
        vectors = {"python dev": [1.0, 0.0]}
        embeddings = FakeEmbeddings(self.config, vectors)
        vector, dim, model = await embeddings.embed("Python dev Акме")
        await self.store.embedding_candidates("remoteok:1", vector, model, dim, "h1")
        await self.store.discover("hh", vacancy(id="900001", title="Python dev", company="Акме"))
        topk = await self.store.embedding_candidates("hh:900001", vector, model, dim, "h2")
        self.assertEqual(topk, ["remoteok:1"])
        result = await self.store.join_cluster("hh:900001", topk)
        self.assertEqual(result["kind"], "merged")
        self.assertEqual((await self.store.get("remoteok:1"))["cluster_id"], result["cluster_id"])
        self.assertEqual((await self.store.get("hh:900001"))["cluster_id"], result["cluster_id"])
        cursor = await self.store._conn.execute(
            "SELECT rep_job_id FROM clusters WHERE id=?", (result["cluster_id"],))
        rep = (await cursor.fetchone())[0]
        await cursor.close()
        self.assertEqual(rep, "hh:900001")  # представитель — hh

    async def test_direct_link_merges(self):
        await self.store.discover("hh", vacancy(id="900002", title="X", company="Б"))
        await self.store.discover("remoteok", vacancy(id="7", title="Y", company="В"))
        result = await self.store.join_cluster("remoteok:7", ["hh:900002"])
        self.assertEqual(result["kind"], "merged")
        self.assertEqual((await self.store.get("remoteok:7"))["cluster_id"], result["cluster_id"])
        self.assertEqual((await self.store.get("hh:900002"))["cluster_id"], result["cluster_id"])

    async def test_skipped_cluster_skips_new_rows(self):
        await self.store.discover("remoteok", vacancy(id="1", title="Python", company="Акме"))
        await self.store.refresh("remoteok", vacancy(id="1", title="Python", company="Акме"),
                                 norm_company="акме", norm_title="python", content_hash_value="h")
        await self.store.discover("hh", vacancy(id="900003", title="Python", company="Акме"))
        await self.store.refresh("hh", vacancy(id="900003", title="Python", company="Акме"),
                                 norm_company="акме", norm_title="python", content_hash_value="h")
        merged = await self.store.join_cluster("hh:900003", ["remoteok:1"])
        self.assertEqual(merged["kind"], "merged")
        self.assertTrue(await self.store.cluster_skip("hh:900003"))
        # Будущая публикация с тем же точным ключом — сразу skipped (16.3).
        self.assertTrue(await self.store.skipped_cluster(["remoteok:1"]))
        await self.store.discover("remoteok", vacancy(id="9", title="Python", company="Акме"))
        self.assertEqual(await self.store.exact_candidates("remoteok:9", "акме", "python"),
                         ["hh:900003", "remoteok:1"])
        cursor = await self.store._conn.execute("SELECT status FROM clusters")
        self.assertEqual((await cursor.fetchone())[0], "skipped")
        await cursor.close()

    async def test_embedding_candidates_are_possible_duplicates_not_merged(self):
        await self.store.discover("remoteok", vacancy(id="1", title="Python developer", company="Акме"))
        await self.store.refresh("remoteok", vacancy(id="1", title="Python developer", company="Акме"),
                                 norm_company="акме", norm_title="python developer",
                                 content_hash_value="h1")
        vectors = {"python developer": [1.0, 0.0, 0.0]}
        embeddings = FakeEmbeddings(self.config, vectors)
        vector, dim, model = await embeddings.embed("Python developer Акме FastAPI")
        await self.store.embedding_candidates("remoteok:1", vector, model, dim, "h1")
        await self.store.discover("remoteok", vacancy(id="2", title="Python engineer", company="Акме"))
        possible = await self.store.embedding_candidates("remoteok:2", vector, model, dim, "h2")
        self.assertEqual(possible, ["remoteok:1"])
        await self.store.mark_possible_duplicates("remoteok:2", possible)
        self.assertEqual((await self.store.get("remoteok:2"))["possible_duplicates"], ["remoteok:1"])
        # «Возможно, дубль» не объединяется автоматически.
        self.assertIsNone((await self.store.get("remoteok:2"))["cluster_id"])

    async def test_old_rows_outside_window_are_not_candidates(self):
        await self.store.discover("remoteok", vacancy(id="1", title="Python", company="Акме"))
        await self.store._conn.execute(
            "UPDATE jobs SET created='2020-01-01T00:00:00+00:00' WHERE id='remoteok:1'")
        await self.store._conn.commit()
        vectors = {"python": [1.0, 0.0]}
        embeddings = FakeEmbeddings(self.config, vectors)
        vector, dim, model = await embeddings.embed("python x")
        await self.store.embedding_candidates("remoteok:1", vector, model, dim, "h1")
        await self.store.discover("remoteok", vacancy(id="2", title="Python", company="Акме"))
        self.assertEqual(await self.store.embedding_candidates("remoteok:2", vector, model, dim, "h2"), [])

    async def test_content_change_triggers_reassessment(self):
        await self.store.discover("hh", vacancy(id="900004", title="Python", company="Акме"))
        await self.store.refresh("hh", vacancy(id="900004", title="Python", company="Акме"),
                                 norm_company="акме", norm_title="python",
                                 content_hash_value="hash-old")
        await self.store.decide("hh:900004", "rejected", "Python не упомянут", rules_version="v3")
        # Описание изменилось → rules_version сброшен → переоценка в первом раунде.
        await self.store.refresh("hh", vacancy(id="900004", title="Python", company="Акме",
                                               description="Python, FastAPI"),
                                 norm_company="акме", norm_title="python",
                                 content_hash_value="hash-new")
        row = await self.store.get("hh:900004")
        self.assertIsNone(row["rules_version"])
        self.assertEqual(await self.store.stale_row(), "hh:900004")


class EngineDedupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(__import__("os").environ, {}, clear=True):
            self.config = Config.load(Path(self.temp.name))
        self.store = AsyncStore(self.config.db_path)
        await self.store.start()
        self.addAsyncCleanup(self.store.close)

    class Writer:
        async def generate(self, job):
            return Letter("Письмо", ["python_backend"], [], [])

    class Probe:
        name = "remoteok"
        def __init__(self, jobs):
            self.jobs = jobs
        async def search(self, criteria, since):
            from job_finder.adapters.base import SearchHit, SearchReport
            return SearchReport(hits=[SearchHit(native_id=job.id) for job in self.jobs])
        async def fetch(self, native_id):
            for job in self.jobs:
                if job.id == str(native_id):
                    return job
            return vacancy(id=native_id, closed=True)
        async def close(self):
            pass

    async def test_duplicate_publication_joins_cluster_and_skipped_cluster_blocks(self):
        first = vacancy(id="1", title="Python разработчик", company="Акме", salary=Salary(200000))
        second = vacancy(id="2", title="Python разработчик", company="Акме", salary=Salary(200000))
        embeddings = FakeEmbeddings(self.config, {"python": [1.0, 0.0, 0.0]})
        engine = Engine(self.config, self.store, [self.Probe([first, second])], self.Writer(),
                        embeddings=embeddings)
        await self.store.round_request()
        await engine.run_pending_rounds()
        await engine.prepare("remoteok:1")
        await engine.prepare("remoteok:2")
        rows = [row for row in await self.store.rows(limit=10) if row["id"].startswith("remoteok:")]
        clusters = {row["cluster_id"] for row in rows}
        self.assertEqual(clusters, {1})
        # /skip по кластеру — обе строки пропущены.
        self.assertTrue(await self.store.cluster_skip("remoteok:1"))
        self.assertEqual((await self.store.get("remoteok:1"))["status"], "skipped")
        self.assertEqual((await self.store.get("remoteok:2"))["status"], "skipped")

    async def test_exact_key_without_embedding_match_is_possible_not_merged(self):
        first = vacancy(id="1", title="Python разработчик", company="Акме", salary=Salary(200000))
        second = vacancy(id="2", title="Python Разработчик!", company="Акме", salary=Salary(200000))
        # Один точный ключ, но эмбеддинги не подтверждают тождество.
        embeddings = SequenceEmbeddings(self.config, [([1.0, 0.0, 0.0], 3, "m"),
                                                      ([0.0, 1.0, 0.0], 3, "m")])
        engine = Engine(self.config, self.store, [self.Probe([first, second])], self.Writer(),
                        embeddings=embeddings)
        await self.store.round_request()
        await engine.run_pending_rounds()
        await engine.prepare("remoteok:1")
        await engine.prepare("remoteok:2")
        self.assertIsNone((await self.store.get("remoteok:2"))["cluster_id"])
        possible = (await self.store.get("remoteok:2"))["possible_duplicates"]
        self.assertEqual(possible, ["remoteok:1"])

    async def test_embedding_possible_duplicate_is_marked(self):
        first = vacancy(id="1", title="Python разработчик", company="Акме", salary=Salary(200000))
        second = vacancy(id="2", title="Python developer", company="Акме", salary=Salary(200000))
        embeddings = FakeEmbeddings(self.config, {"python": [1.0, 0.0, 0.0]})
        engine = Engine(self.config, self.store, [self.Probe([first, second])], self.Writer(),
                        embeddings=embeddings)
        await self.store.round_request()
        await engine.run_pending_rounds()
        await engine.prepare("remoteok:1")
        await engine.prepare("remoteok:2")
        self.assertEqual((await self.store.get("remoteok:2"))["possible_duplicates"], ["remoteok:1"])
        self.assertIsNone((await self.store.get("remoteok:2"))["cluster_id"])
