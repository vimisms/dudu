"""Incremental sync between the on-disk knowledge-base repo and the vector
store, so the index stays current as files are added/edited/deleted -- the
whole point, since this repo gets edited throughout the day.

A manifest (manifest.json, stored alongside the vector index) records each
indexed file's content hash. sync() re-hashes every file on disk, diffs
against the manifest, and only re-chunks + re-embeds files that are new or
whose hash changed; deleted files have their chunks removed. Unchanged files
cost nothing. Call sync(full=True) to force a full rebuild (e.g. after
switching embedding models).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from loguru import logger

from rag.chunker import Chunk, chunk_file, iter_searchable_files
from rag.embeddings import Embedder
from rag.vector_store import VectorStore

SKIP_DIR_NAMES = {".git", ".venv", "node_modules", "__pycache__", ".schema_cache", "renfro"}
MAX_FILE_BYTES = 3_000_000

# Chunks accumulated across files before one embed() call. Large enough to keep
# the embedder's internal batching (batch_size=64) saturated; small enough that
# a crash loses at most this much work and progress stays visible.
EMBED_BATCH_CHUNKS = int(os.environ.get("KB_EMBED_BATCH_CHUNKS", "256"))


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


class KnowledgeBaseIndexer:
    def __init__(self, kb_root: Path, index_dir: Path, embedder: Embedder, vector_store: VectorStore) -> None:
        self.kb_root = kb_root
        self.index_dir = index_dir
        self.embedder = embedder
        self.vector_store = vector_store
        self.manifest_path = index_dir / "manifest.json"
        index_dir.mkdir(parents=True, exist_ok=True)
        self.manifest: dict[str, dict] = self._load_manifest()
        self.last_sync_summary: dict = {}
        self.last_sync_time: float | None = None

    def _load_manifest(self) -> dict[str, dict]:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("Manifest at {} was unreadable, starting fresh.", self.manifest_path)
        return {}

    def _save_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")

    def sync(self, full: bool = False) -> dict:
        t0 = time.time()
        current: dict[str, tuple[str, Path]] = {}
        for path in iter_searchable_files(self.kb_root, SKIP_DIR_NAMES, MAX_FILE_BYTES):
            rel = str(path.relative_to(self.kb_root)).replace("\\", "/")
            current[rel] = (_hash_file(path), path)

        added = [r for r in current if r not in self.manifest]
        removed = [r for r in self.manifest if r not in current]
        changed = [
            r for r in current
            if r in self.manifest and (full or self.manifest[r]["hash"] != current[r][0])
        ]
        unchanged = [r for r in current if r not in added and r not in changed]

        for rel in changed + removed:
            self.vector_store.delete_by_source(rel)

        for rel in removed:
            self.manifest.pop(rel, None)

        to_embed = added + changed

        # Announce the workload up front. Embedding on CPU runs at a few files a
        # second, and with progress logged only every 25 files a first-run sync
        # is indistinguishable from a hang for minutes at a time.
        if to_embed:
            logger.info(
                "KB sync: {} file(s) to embed ({} new, {} changed), {} unchanged. "
                "A first pass with a neural embedder can take several minutes; "
                "progress is saved every 25 files, so interrupting it only costs "
                "the current batch.",
                len(to_embed), len(added), len(changed), len(unchanged),
            )
        else:
            logger.info("KB sync: nothing to do ({} files unchanged)", len(unchanged))

        if self.embedder.needs_fit():
            # TF-IDF-style embedders need the FULL corpus vocabulary, so a fit
            # forces re-embedding everything this pass, not just the diff.
            logger.info("Embedder needs fitting -- doing a full pass over the corpus.")
            all_chunks: list[Chunk] = []
            for rel, (_h, path) in current.items():
                all_chunks.extend(chunk_file(path, self.kb_root))
            self.embedder.fit_corpus([c.embed_text for c in all_chunks])
            for rel in current:
                self.vector_store.delete_by_source(rel)
            if all_chunks:
                vectors = self.embedder.embed([c.embed_text for c in all_chunks])
                self.vector_store.upsert(
                    ids=[c.id for c in all_chunks],
                    vectors=vectors,
                    metadatas=[c.metadata for c in all_chunks],
                    documents=[c.display_text for c in all_chunks],
                )
            for rel in current:
                self.manifest[rel] = {"hash": current[rel][0], "indexed_at": time.time()}
            to_embed = list(current.keys())
        else:
            # Embed in batches that span MULTIPLE files.
            #
            # This used to call embed() once per file. A typical ICM write-up
            # chunks into single digits, so despite the embedder's batch_size=64
            # nearly every forward pass ran on a handful of sequences -- paying
            # the full per-call cost of tokenisation, length-sorting and torch
            # dispatch to do a fraction of the work it could have. On CPU that
            # overhead dominates the actual matrix multiplies, which is why
            # indexing felt far slower than this model should be.
            #
            # Accumulating whole files until EMBED_BATCH_CHUNKS gives the
            # embedder full batches. Files are never split across batches, so a
            # crash still resumes cleanly at file granularity.
            batch: list[Chunk] = []
            pending_files: list[str] = []
            embedded = 0
            embed_seconds = 0.0

            def flush() -> None:
                nonlocal batch, pending_files, embedded, embed_seconds
                if batch:
                    t_batch = time.time()
                    vectors = self.embedder.embed([c.embed_text for c in batch])
                    embed_seconds += time.time() - t_batch
                    self.vector_store.upsert(
                        ids=[c.id for c in batch],
                        vectors=vectors,
                        metadatas=[c.metadata for c in batch],
                        documents=[c.display_text for c in batch],
                    )
                    embedded += len(batch)
                # Only record files whose chunks are now safely in the store.
                for rel_done in pending_files:
                    self.manifest[rel_done] = {"hash": current[rel_done][0], "indexed_at": time.time()}
                batch = []
                pending_files = []
                self._save_manifest()
                self.vector_store.persist()

            total = len(to_embed)
            for done, rel in enumerate(to_embed, start=1):
                batch.extend(chunk_file(current[rel][1], self.kb_root))
                pending_files.append(rel)
                if len(batch) >= EMBED_BATCH_CHUNKS or done == total:
                    flush()
                    rate = embedded / embed_seconds if embed_seconds > 0 else 0.0
                    logger.info(
                        "KB indexing: {}/{} files ({}%), {} chunks embedded at {:.0f} chunks/s",
                        done, total, 100 * done // max(1, total), embedded, rate,
                    )

        self._save_manifest()
        self.vector_store.persist()

        self.last_sync_time = time.time()
        self.last_sync_summary = {
            "added": len(added),
            "changed": len(changed),
            "removed": len(removed),
            "unchanged": len(unchanged),
            "total_files": len(current),
            "total_chunks": self.vector_store.count(),
            "embedder": self.embedder.name,
            "seconds": round(time.time() - t0, 2),
        }
        logger.info("KB sync complete: {}", self.last_sync_summary)
        return self.last_sync_summary
