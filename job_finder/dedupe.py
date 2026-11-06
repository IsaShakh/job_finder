"""Дедупликация (раздел 8 плана): консервативное слияние.

Кандидаты = точный ключ `(norm_company, norm_title)` ∪ прямые ссылки
`hh.ru/vacancy/<id>`/`habr.com/.../vacancies/...` в тексте ∪ эмбеддинг-top-K.
Точный ключ и прямые ссылки — жёсткое доказательство тождества → кластер;
эмбеддинг-совпадение — «возможно, дубль» и не объединяется автоматически.
Вектор — «название + компания + требования» (при пустых требованиях — первые
~800 символов описания с вычищенными шаблонами); порог косинуса 0.93 —
гипотеза, подбирается на размеченных примерах.
"""
import hashlib
import math
import re

DEDUP_COSINE_THRESHOLD = 0.93
EMBEDDING_K = 5
EMBEDDING_WINDOW_DAYS = 30

HH_LINK_RE = re.compile(r"hh\.ru/vacancy/(\d+)", re.I)
HABR_LINK_RE = re.compile(r"habr\.com/[\w./-]*vacanc\w*/(\d+)", re.I)
LEGAL_FORMS = {"ооо", "ао", "пао", "зао", "ип", "llc", "ltd", "inc", "gmbh",
               "corp", "co", "plc", "oy", "ab", "ag", "sarl", "bv", "nv"}
SENIOR_VARIANTS = {"сеньор", "сениор", "сеньёр", "senior"}
TEMPLATE_BLOCKS = (re.compile(r"о компании.{0,400}", re.I | re.S),
                   re.compile(r"мы —.{0,200}", re.I | re.S),
                   re.compile(r"about (?:the )?company.{0,400}", re.I | re.S))


def normalize_company(name):
    """Нормализация названия компании: регистр, пунктуация, юрформы."""
    text = (name or "").lower()
    text = re.sub(r"[^\wа-яё]+", " ", text, flags=re.I)
    return " ".join(token for token in text.split() if token not in LEGAL_FORMS)


def normalize_title(title):
    """Нормализация названия: регистр, пунктуация, варианты Senior."""
    text = (title or "").lower()
    text = re.sub(r"[^\wа-яё]+", " ", text, flags=re.I)
    tokens = ["senior" if token in SENIOR_VARIANTS else token for token in text.split()]
    return " ".join(tokens)


def content_hash(vacancy):
    """Хэш описания/требований — детект изменения содержимого (16.2)."""
    payload = (vacancy.description or "") + "\n" + (vacancy.requirements or "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def embedding_input(vacancy):
    """Вход эмбеддинга: название + компания + требования; при пустых
    требованиях — первые ~800 символов описания с вычисткой шаблонов."""
    parts = [vacancy.title, vacancy.company]
    requirements = (vacancy.requirements or "").strip()
    if requirements:
        parts.append(requirements)
    else:
        description = vacancy.description or ""
        for pattern in TEMPLATE_BLOCKS:
            description = pattern.sub(" ", description)
        description = re.sub(r"\s+", " ", description).strip()
        parts.append(description[:800])
    return "\n".join(part for part in parts if part)


def extract_links(vacancy):
    """Прямые ссылки в тексте: [(source, native_id)] — жёсткое доказательство."""
    text = (vacancy.description or "") + "\n" + (vacancy.requirements or "")
    links = [("hh", match) for match in HH_LINK_RE.findall(text)]
    links += [("habr", match) for match in HABR_LINK_RE.findall(text)]
    return links


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def merge_kind(exact, links, embeddings):
    """Консервативная политика слияния (раздел 8)."""
    if exact or links:
        return "merge"
    if embeddings:
        return "possible"
    return None


class EmbeddingClient:
    """OpenAI-совместимые эмбеддинги; валидация ответа, санитизация ошибок.

    Использует отдельную модель LLM_EMBEDDING_MODEL (по умолчанию
    text-embedding-3-small) — чат-модель эмбеддинги не отдаёт."""

    def __init__(self, config, http_client=None):
        self.config = config
        self.http_client = http_client
        self.available = bool(config.llm_api_key and config.llm_embedding_model)

    async def embed(self, text):
        """→ (vector, dim, model). Ошибки санитизированы, не блокируют очередь."""
        if not self.available:
            raise RuntimeError("Эмбеддинги недоступны: нет ключа/модели LLM")
        import httpx
        payload = {"model": self.config.llm_embedding_model, "input": text}
        headers = {"Authorization": "Bearer " + self.config.llm_api_key}
        try:
            if self.http_client is not None:
                response = await self.http_client.post(
                    self.config.llm_base_url + "/embeddings", json=payload,
                    headers=headers, timeout=30)
            else:
                async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                    response = await client.post(
                        self.config.llm_base_url + "/embeddings", json=payload,
                        headers=headers, timeout=30)
        except httpx.HTTPError:
            raise RuntimeError("Сервис эмбеддингов недоступен") from None
        if response.status_code != 200:
            raise RuntimeError("Сервис эмбеддингов отклонил запрос (HTTP %d)" % response.status_code)
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError("Некорректный ответ сервиса эмбеддингов") from None
        data = (body or {}).get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data or not isinstance(data[0].get("embedding"), list):
            raise RuntimeError("Некорректный ответ сервиса эмбеддингов")
        vector = [float(item) for item in data[0]["embedding"]]
        return vector, len(vector), (body.get("model") or self.config.llm_model)
