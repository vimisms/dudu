"""MCP server (stdio) exposing local, semantic (meaning-based) search over
the knowledge-base repo, backed by rag/ (chunker + embedder + vector store +
incremental indexer).

This is the third and most important search tool for triage-style questions
("what should I do when X is stuck") -- unlike kb_search_server.py's
search_kb_content (literal keyword matching) or the filesystem MCP's
search_files (filename matching), this one matches on MEANING, so it finds
relevant guidance even when your wording doesn't share any words with the
source document.

Because campair-TraceAI gets edited throughout the day, a background thread
here re-syncs the index on an interval (KB_REINDEX_INTERVAL_SECONDS, default
5 minutes) for the whole lifetime of this process, in addition to the
on-demand reindex_kb tool for "I just added a new ICM, index it now".

Run standalone for a quick manual test:
    python rag_search_server.py --self-test "what should I do when closing and recal stuck"
"""
import contextlib
import os
import sys
import threading
import time
from pathlib import Path

from loguru import logger
from mcp.server.fastmcp import FastMCP

from rag.embeddings import get_embedder
from rag.indexer import KnowledgeBaseIndexer
from rag.vector_store import get_vector_store

KB_REPO_PATH = Path(os.environ.get("KB_REPO_PATH", r"C:\campair-TraceAI"))
KB_INDEX_DIR = Path(os.environ.get("KB_INDEX_DIR", "./kb_index"))
EMBEDDING_BACKEND = os.environ.get("KB_EMBEDDING_BACKEND", "sentence_transformers")
VECTOR_BACKEND = os.environ.get("KB_VECTOR_BACKEND", "chroma")
EMBEDDING_MODEL = os.environ.get("KB_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
REINDEX_INTERVAL_SECONDS = int(os.environ.get("KB_REINDEX_INTERVAL_SECONDS", "300"))
REINDEX_INITIAL_DELAY_SECONDS = int(os.environ.get("KB_REINDEX_INITIAL_DELAY_SECONDS", "45"))
# How long a search will wait for an in-progress reindex before giving up.
QUERY_LOCK_TIMEOUT_S = float(os.environ.get("KB_QUERY_LOCK_TIMEOUT_S", "10"))

_lock = threading.Lock()       # serialises index reads/writes
_init_lock = threading.Lock()  # guards one-time construction below

# The embedder, vector store and indexer used to be built at MODULE IMPORT time.
# That was the wrong place for three reasons:
#   1. Loading sentence-transformers pulls in torch and (first run) downloads a
#      ~130MB model. Doing that before mcp.run() means the parent's MCP
#      `initialize` handshake gets no answer until it finishes -- or never, if
#      it fails -- and the parent just reports "unhandled errors in a TaskGroup".
#   2. Any exception up here kills the process before the server exists, so the
#      failure is invisible. get_embedder/get_vector_store have fallbacks, but a
#      corrupt tfidf pickle or vectors.npy still raises out of the fallback path.
#   3. A hard crash in a native dependency (a torch DLL load error) takes the
#      whole server down rather than degrading to "semantic search unavailable".
# Now they're built on first use, and a failure is reported as a readable tool
# result while the rest of the server keeps working.
_components: dict = {"embedder": None, "store": None, "indexer": None, "error": None}


def _get_components() -> dict:
    """Build the RAG stack once, on first use. Never raises."""
    if _components["indexer"] is not None or _components["error"] is not None:
        return _components
    with _init_lock:
        if _components["indexer"] is not None or _components["error"] is not None:
            return _components
        try:
            # stdout is the MCP protocol channel on a stdio server: one stray
            # print() from a dependency (chroma migrations, HF download notices)
            # corrupts the JSON-RPC stream and the session dies with exactly the
            # opaque TaskGroup error above. Send anything printed during init to
            # stderr, where it shows up in the log instead.
            with contextlib.redirect_stdout(sys.stderr):
                embedder = get_embedder(EMBEDDING_BACKEND, KB_INDEX_DIR, EMBEDDING_MODEL)
                store = get_vector_store(VECTOR_BACKEND, KB_INDEX_DIR)
                indexer = KnowledgeBaseIndexer(KB_REPO_PATH, KB_INDEX_DIR, embedder, store)
            _components.update(embedder=embedder, store=store, indexer=indexer)
            logger.info("RAG stack ready: embedder={} store={}", embedder.name, VECTOR_BACKEND)
        except Exception as exc:  # noqa: BLE001 - report, don't die
            _components["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("Could not initialise the RAG stack")
    return _components


mcp = FastMCP("kb-rag-search")


def _background_reindex_loop() -> None:
    # Delay the first sync so the other MCP servers (esp. the npx filesystem
    # server) finish their handshake before this CPU-heavy embed loop starts;
    # otherwise they can time out under contention. Then re-sync on an interval
    # so edits made during the day get picked up automatically.
    time.sleep(REINDEX_INITIAL_DELAY_SECONDS)
    while True:
        try:
            state = _get_components()
            if state["error"]:
                logger.warning("Skipping background reindex -- RAG stack unavailable: {}", state["error"])
            else:
                with _lock:
                    state["indexer"].sync()
        except Exception:  # noqa: BLE001 - never let the background loop die
            logger.exception("Background KB reindex failed; will retry next interval.")
        time.sleep(REINDEX_INTERVAL_SECONDS)


@mcp.tool()
def semantic_search_kb(query: str, top_k: int = 6, doc_type: str | None = None) -> str:
    """Meaning-based search over the knowledge base -- finds relevant
    passages even when your wording doesn't literally match the source text
    (unlike search_kb_content, which needs shared keywords). Use this for
    open-ended / triage-style questions like "what should I do when X is
    stuck" or "have we seen anything like this before", not for looking up
    an exact SQL query or table name (use search_kb_content or read_file for
    those). Optionally filter to one doc_type: icm-investigations, tsgs,
    sql-reference, kusto, table-reference, schemas, icm-health.
    Returns the top matching chunks with their source file, ICM number (if
    any), and section title, so you can read_file the full source afterward.
    """
    state = _get_components()
    if state["error"]:
        return (
            f"Semantic search is unavailable on this machine ({state['error']}). "
            "Use search_kb_content for keyword lookups instead, and tell the user "
            "semantic search is down rather than guessing an answer."
        )

    # The background reindex holds _lock for its whole pass, which on a first
    # run is minutes. Blocking here would burn the agent's entire task budget
    # waiting, and the user would just see the task time out. Fail fast with
    # something the agent can act on instead.
    if not _lock.acquire(timeout=QUERY_LOCK_TIMEOUT_S):
        return (
            "The knowledge-base index is currently rebuilding, so semantic search "
            "is briefly unavailable. Use search_kb_content for this question, or "
            "tell the user to retry in a few minutes."
        )
    try:
        qvec = state["embedder"].embed([query])[0]
        results = state["store"].query(qvec, top_k=top_k, doc_type=doc_type)
    finally:
        _lock.release()

    if not results:
        return f"No semantic matches for {query!r}. Try search_kb_content, or reindex_kb if the KB was just updated."

    out = [f"Top {len(results)} semantic matches for {query!r}:\n"]
    for r in results:
        m = r["metadata"]
        header = f"### {m['source']}"
        if m.get("icm_number"):
            header += f"  (ICM {m['icm_number']})"
        if m.get("section_title"):
            header += f"  — {m['section_title']}"
        header += f"  [score {r['score']:.2f}]"
        out.append(header + "\n" + r["document"][:800])
    return "\n\n".join(out)


@mcp.tool()
def reindex_kb(full: bool = False) -> str:
    """Manually trigger a knowledge-base reindex right now, instead of
    waiting for the background sync interval. Use this right after telling
    the user you've noted a new ICM/investigation for them, or whenever they
    say they just updated the KB and want it searchable immediately. Pass
    full=True only after an embedding-model change (rare)."""
    state = _get_components()
    if state["error"]:
        return f"Cannot reindex -- the RAG stack failed to start: {state['error']}"
    with _lock:
        summary = state["indexer"].sync(full=full)
    return f"Reindex complete: {summary}"


@mcp.tool()
def kb_index_status() -> str:
    """Returns quick stats about the current index (file/chunk counts,
    embedding backend in use, last sync time) -- use to sanity-check
    freshness before trusting a search result on something time-sensitive."""
    state = _get_components()
    if state["error"]:
        return f"Semantic search is DOWN on this machine: {state['error']}"
    indexer, embedder = state["indexer"], state["embedder"]
    age = f"{time.time() - indexer.last_sync_time:.0f}s ago" if indexer.last_sync_time else "never"
    return (
        f"Embedder: {embedder.name} (dim={embedder.dim})\n"
        f"Vector store: {VECTOR_BACKEND}\n"
        f"Last sync: {age}\n"
        f"Last sync summary: {indexer.last_sync_summary}"
    )


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--self-test":
        # Diagnostic path: run this directly to see the REAL error when the
        # parent only reports "unhandled errors in a TaskGroup".
        #   .venv\Scripts\python.exe rag_search_server.py --self-test "closing stuck"
        state = _get_components()
        if state["error"]:
            print(f"RAG stack failed to initialise: {state['error']}", file=sys.stderr)
            raise SystemExit(1)
        print(f"Embedder: {state['embedder'].name}  Store: {VECTOR_BACKEND}", file=sys.stderr)
        state["indexer"].sync()
        print(semantic_search_kb(sys.argv[2]))
    else:
        threading.Thread(target=_background_reindex_loop, daemon=True).start()
        mcp.run(transport="stdio")
