"""Pluggable local vector storage.

Primary (recommended): a persistent local Chroma collection -- handles
larger corpora and native incremental upsert/delete, no server process
needed (it's an embedded, file-backed database).

Fallback: a plain NumPy array + JSON sidecar. No extra dependency at all
(numpy is already required elsewhere in this project), fine up to tens of
thousands of chunks, which comfortably covers this knowledge base. Used for
the offline verification in this build since chromadb wasn't installable in
the sandbox that built it.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import numpy as np
from loguru import logger

# Kill chromadb's anonymous telemetry (a posthog version mismatch spams
# "Failed to send telemetry event ... capture() takes 1 positional argument").
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY", "False")
logging.getLogger("chromadb.telemetry").setLevel(logging.CRITICAL)
logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)


class VectorStore:
    def upsert(self, ids: list[str], vectors: np.ndarray, metadatas: list[dict], documents: list[str]) -> None:
        raise NotImplementedError

    def delete_by_source(self, source: str) -> None:
        raise NotImplementedError

    def query(self, vector: np.ndarray, top_k: int, doc_type: str | None = None) -> list[dict]:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def persist(self) -> None:
        pass


class NumpyVectorStore(VectorStore):
    def __init__(self, persist_dir: Path) -> None:
        self._dir = persist_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._vectors_path = self._dir / "vectors.npy"
        self._meta_path = self._dir / "meta.json"

        self._ids: list[str] = []
        self._vectors: np.ndarray | None = None
        self._metadatas: list[dict] = []
        self._documents: list[str] = []

        if self._vectors_path.exists() and self._meta_path.exists():
            self._vectors = np.load(self._vectors_path)
            sidecar = json.loads(self._meta_path.read_text(encoding="utf-8"))
            self._ids = sidecar["ids"]
            self._metadatas = sidecar["metadatas"]
            self._documents = sidecar["documents"]

    def _drop_ids(self, drop_ids: set[str]) -> None:
        if not drop_ids or self._vectors is None:
            return
        keep = [i for i, _id in enumerate(self._ids) if _id not in drop_ids]
        self._vectors = self._vectors[keep] if keep else np.empty((0, self._vectors.shape[1]), dtype=np.float32)
        self._ids = [self._ids[i] for i in keep]
        self._metadatas = [self._metadatas[i] for i in keep]
        self._documents = [self._documents[i] for i in keep]

    def upsert(self, ids: list[str], vectors: np.ndarray, metadatas: list[dict], documents: list[str]) -> None:
        self._drop_ids(set(ids))  # replace-on-conflict semantics
        vectors = vectors.astype(np.float32)
        if self._vectors is None or self._vectors.size == 0:
            self._vectors = vectors
        else:
            if self._vectors.shape[1] != vectors.shape[1]:
                logger.warning(
                    "Embedding dimension changed ({} -> {}); clearing the index. "
                    "Run a full reindex.", self._vectors.shape[1], vectors.shape[1],
                )
                self._vectors, self._ids, self._metadatas, self._documents = vectors, [], [], []
            self._vectors = np.vstack([self._vectors, vectors])
        self._ids.extend(ids)
        self._metadatas.extend(metadatas)
        self._documents.extend(documents)

    def delete_by_source(self, source: str) -> None:
        drop = {i for i, m in zip(self._ids, self._metadatas) if m.get("source") == source}
        self._drop_ids(drop)

    def query(self, vector: np.ndarray, top_k: int, doc_type: str | None = None) -> list[dict]:
        if self._vectors is None or len(self._ids) == 0:
            return []
        if doc_type:
            idx = np.array([i for i, m in enumerate(self._metadatas) if m.get("doc_type") == doc_type])
        else:
            idx = np.arange(len(self._ids))
        if idx.size == 0:
            return []

        mat = self._vectors[idx]
        q = vector.astype(np.float32)
        mat_norm = np.linalg.norm(mat, axis=1) + 1e-8
        q_norm = np.linalg.norm(q) + 1e-8
        sims = (mat @ q) / (mat_norm * q_norm)

        order = np.argsort(-sims)[:top_k]
        results = []
        for pos in order:
            real_idx = idx[pos]
            results.append(
                {
                    "id": self._ids[real_idx],
                    "score": float(sims[pos]),
                    "metadata": self._metadatas[real_idx],
                    "document": self._documents[real_idx],
                }
            )
        return results

    def count(self) -> int:
        return len(self._ids)

    def persist(self) -> None:
        if self._vectors is None:
            return
        np.save(self._vectors_path, self._vectors)
        self._meta_path.write_text(
            json.dumps({"ids": self._ids, "metadatas": self._metadatas, "documents": self._documents}),
            encoding="utf-8",
        )


class ChromaVectorStore(VectorStore):
    # Chroma requires collection names of 3-63 chars, so not just "kb".
    def __init__(self, persist_dir: Path, collection_name: str = "kb_docs") -> None:
        import chromadb  # noqa: PLC0415
        from chromadb.config import Settings  # noqa: PLC0415

        self._client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=Settings(anonymized_telemetry=False),  # silence noisy telemetry errors
        )
        self._collection = self._client.get_or_create_collection(collection_name)

    def upsert(self, ids: list[str], vectors: np.ndarray, metadatas: list[dict], documents: list[str]) -> None:
        # Chroma rejects single upserts above ~5461 rows, so chunk it.
        step = 4000
        vlist = vectors.tolist()
        for i in range(0, len(ids), step):
            j = i + step
            self._collection.upsert(
                ids=ids[i:j], embeddings=vlist[i:j], metadatas=metadatas[i:j], documents=documents[i:j]
            )

    def delete_by_source(self, source: str) -> None:
        self._collection.delete(where={"source": source})

    def query(self, vector: np.ndarray, top_k: int, doc_type: str | None = None) -> list[dict]:
        where = {"doc_type": doc_type} if doc_type else None
        res = self._collection.query(query_embeddings=[vector.tolist()], n_results=top_k, where=where)
        out = []
        if not res["ids"] or not res["ids"][0]:
            return out
        for i in range(len(res["ids"][0])):
            out.append(
                {
                    "id": res["ids"][0][i],
                    "score": 1.0 - res["distances"][0][i],  # chroma default: cosine distance
                    "metadata": res["metadatas"][0][i],
                    "document": res["documents"][0][i],
                }
            )
        return out

    def count(self) -> int:
        return self._collection.count()


def get_vector_store(backend: str, index_dir: Path) -> VectorStore:
    backend = (backend or "chroma").lower()
    if backend == "numpy":
        return NumpyVectorStore(index_dir / "numpy_store")
    try:
        return ChromaVectorStore(index_dir / "chroma")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Falling back to the NumPy vector store -- chromadb unavailable ({}).", exc)
        return NumpyVectorStore(index_dir / "numpy_store")
