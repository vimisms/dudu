"""Pluggable embedding backends.

Primary (recommended): local sentence-transformers model -- true semantic
(meaning-based) vectors, runs on CPU, no cloud API call, one-time ~100-400MB
model download the first time it's used.

Fallback: scikit-learn TF-IDF. This is NOT semantic search (no synonym/
meaning generalization -- "aging"/"ageing" won't automatically match each
other the way real embeddings would), but it's a legitimate local vector
representation that needs no model download at all, so it's what lets this
degrade gracefully (or be tested) on a machine without internet access to
Hugging Face, and is used for the offline verification in this build.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
from loguru import logger


class Embedder:
    name: str = "base"
    dim: int = 0

    def embed(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError

    def needs_fit(self) -> bool:
        return False


class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        self._model = SentenceTransformer(model_name)
        self.name = f"sentence-transformers:{model_name}"
        self.dim = self._model.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray(
            self._model.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False),
            dtype=np.float32,
        )


class TfidfEmbedder(Embedder):
    """Zero-download fallback. Needs `fit_corpus()` over ALL documents before
    first use (and again on a full reindex) since TF-IDF weights depend on
    the whole corpus's vocabulary -- unlike a neural embedder, it can't
    meaningfully vectorize new text against a stale vocabulary. Incremental
    adds between full reindexes reuse the existing fitted vectorizer (new
    vocabulary terms in those files are simply not captured until the next
    full reindex)."""

    def __init__(self, persist_path: Path, max_features: int = 4096) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: PLC0415

        self.name = "tfidf"
        self._persist_path = persist_path
        self._max_features = max_features
        self._vectorizer: "TfidfVectorizer | None" = None
        self.dim = max_features
        if persist_path.exists():
            with open(persist_path, "rb") as f:
                self._vectorizer = pickle.load(f)
            self.dim = len(self._vectorizer.vocabulary_)

    def needs_fit(self) -> bool:
        return self._vectorizer is None

    def fit_corpus(self, texts: list[str]) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: PLC0415

        self._vectorizer = TfidfVectorizer(max_features=self._max_features, stop_words="english")
        self._vectorizer.fit(texts)
        self.dim = len(self._vectorizer.vocabulary_)
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._persist_path, "wb") as f:
            pickle.dump(self._vectorizer, f)

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._vectorizer is None:
            raise RuntimeError("TfidfEmbedder.fit_corpus() must be called before embed().")
        matrix = self._vectorizer.transform(texts)
        return matrix.toarray().astype(np.float32)


def get_embedder(backend: str, index_dir: Path, model_name: str = "BAAI/bge-small-en-v1.5") -> Embedder:
    backend = (backend or "sentence_transformers").lower()
    if backend == "tfidf":
        return TfidfEmbedder(index_dir / "tfidf_vectorizer.pkl")
    try:
        return SentenceTransformerEmbedder(model_name)
    except Exception as exc:  # noqa: BLE001 - missing package, no internet, etc.
        logger.warning(
            "Falling back to TF-IDF embeddings -- sentence-transformers unavailable ({}). "
            "Install it (see requirements.txt) for real semantic search.", exc,
        )
        return TfidfEmbedder(index_dir / "tfidf_vectorizer.pkl")
