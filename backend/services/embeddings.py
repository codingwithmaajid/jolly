"""Text embeddings.

- GeminiEmbedder: Google AI Studio (`gemini-embedding-2`, 768 dims), used
  when EMBEDDING_API_KEY is set.
  * gemini-embedding-2 rejects `taskType`. The task goes into the text
    instead, per https://ai.google.dev/gemini-api/docs/embeddings
    ("Task types with Embeddings 2"):
        query:    "task: question answering | query: {question}"
        document: "title: {title} | text: {content}"   (title "none" if absent)
  * `outputDimensionality: 768` (recommended size; embedding-2 re-normalises
    truncated vectors itself, we normalise again anyway). 768 also fits a
    pgvector `vector(768)` column (indexable up to 2,000 dims).
  * If the configured model answers 400/404 (bad field, unknown model) we
    switch, once and for the life of the process, to `gemini-embedding-001`
    with `taskType`, which that model supports. The switch is sticky so
    queries and stored vectors always come from the same model.
- HashingEmbedder: deterministic, offline, zero-cost fallback (hashed word
  and bigram features). Keeps tests, CI and demos working with no key.

Every embedder exposes `model` and `dim`; chunks store both, and a change in
either triggers a re-index (see ingest.ensure_index).
Vectors are L2-normalised float32, so cosine similarity is a dot product.
"""

import hashlib
import logging
import re
from typing import Protocol, Sequence

import numpy as np
import requests

logger = logging.getLogger(__name__)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
FALLBACK_MODEL = "gemini-embedding-001"
DEFAULT_DIM = 768
_STOP = frozenset(
    "a an the of to in on for and or is was were be been by with at as it this that from "
    "what why when who how did does do which are has have had will shall".split()
)


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: list[str], *, is_query: bool = False,
              titles: Sequence[str | None] | None = None) -> np.ndarray: ...


def _normalise(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


def _stem(w: str) -> str:
    # crude stemming so 'adjourned' ~ 'adjournment' ~ 'adjourn'
    for suf in ("ment", "ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> list[str]:
    """Lower-cased, stop-worded, lightly stemmed word tokens (Latin, Tamil and Devanagari)."""
    words = re.findall(r"[a-z0-9]+|[\u0B80-\u0BFF]+|[\u0900-\u097F]+", text.lower())
    return [_stem(w) for w in words if w not in _STOP]


def prepare_query(text: str) -> str:
    return f"task: question answering | query: {text}"


def prepare_document(text: str, title: str | None = None) -> str:
    return f"title: {title or 'none'} | text: {text}"


class HashingEmbedder:
    model = "local-hash-v1"

    def __init__(self, dim: int = 1024):
        self.dim = dim

    def _bucket(self, feat: str) -> tuple[int, float]:
        h = hashlib.blake2b(feat.encode(), digest_size=8).digest()
        n = int.from_bytes(h, "little")
        return n % self.dim, (1.0 if (n >> 63) & 1 else -1.0)

    def embed(self, texts: list[str], *, is_query: bool = False,
              titles: Sequence[str | None] | None = None) -> np.ndarray:
        m = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            if titles and titles[i]:
                t = f"{titles[i]}. {t}"
            toks = tokens(t)
            feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
            for f in feats:
                j, sign = self._bucket(f)
                m[i, j] += sign
        return _normalise(m)


class GeminiEmbedder:
    def __init__(self, api_key: str, model: str = "gemini-embedding-2", dim: int = DEFAULT_DIM,
                 timeout: float = 20.0, fallback_model: str | None = FALLBACK_MODEL, session=None):
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout = timeout
        self.fallback_model = fallback_model if fallback_model != model else None
        self.http = session or requests

    @staticmethod
    def _uses_prompt_prefix(model: str) -> bool:
        # embedding-2 (and later) take the task in the text; 001 takes taskType.
        return not model.startswith("gemini-embedding-001") and "text-embedding" not in model

    def _request(self, model: str, text: str, is_query: bool, title: str | None) -> dict:
        req: dict = {"model": f"models/{model}", "outputDimensionality": self.dim}
        if self._uses_prompt_prefix(model):
            text = prepare_query(text) if is_query else prepare_document(text, title)
        else:
            req["taskType"] = "QUESTION_ANSWERING" if is_query else "RETRIEVAL_DOCUMENT"
            if not is_query and title:
                req["title"] = title
        req["content"] = {"parts": [{"text": text}]}
        return req

    def _batch(self, model: str, texts: list[str], is_query: bool, titles) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 100):
            batch = texts[start: start + 100]
            tbatch = list(titles[start: start + 100]) if titles else [None] * len(batch)
            resp = self.http.post(
                f"{GEMINI_BASE}/models/{model}:batchEmbedContents",
                headers={"x-goog-api-key": self.api_key},
                json={"requests": [self._request(model, t, is_query, ti) for t, ti in zip(batch, tbatch)]},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            vectors += [e["values"] for e in resp.json()["embeddings"]]
        return vectors

    def embed(self, texts: list[str], *, is_query: bool = False,
              titles: Sequence[str | None] | None = None) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        try:
            vectors = self._batch(self.model, texts, is_query, titles)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code not in (400, 404) or not self.fallback_model:
                raise
            logger.warning("Embedding model %s returned %s; switching to %s for this process.",
                           self.model, code, self.fallback_model)
            self.model, self.fallback_model = self.fallback_model, None
            vectors = self._batch(self.model, texts, is_query, titles)
        return _normalise(np.array(vectors, dtype=np.float32))


def get_embedder(settings) -> Embedder:
    if settings.embedding_api_key:
        dim = getattr(settings, "embedding_dim", DEFAULT_DIM) or DEFAULT_DIM
        return GeminiEmbedder(settings.embedding_api_key, model=settings.embedding_model, dim=dim)
    return HashingEmbedder()
