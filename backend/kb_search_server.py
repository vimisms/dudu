"""A small custom MCP server that adds full-text search over the knowledge-base
repo (KB_REPO_PATH). Runs as its own stdio MCP server, alongside the official
Filesystem MCP server.

Why this exists: the official @modelcontextprotocol/server-filesystem's
`search_files` tool only matches FILE/DIRECTORY NAMES (substring, case-insensitive)
-- it does not look inside file contents. That's fine for "open X.md", but it
means a question like "do we have any past ICMs about inventory aging" will
silently miss real matches whenever the relevant word only appears in the body
of the document, or under a different name/spelling than the filename uses
(e.g. this repo has both "...StockAgeingReport..." and "...InventoryAgingReport..."
filenames -- British vs. American spelling -- which a naive single-keyword
filename search would not reliably catch across both).

This server gives the agent two tools instead:
  - list_kb_index    : a fast "table of contents" (ICM index + doc folder tree)
                        so the agent can browse before deciding what to read.
  - search_kb_content : greps file *contents* (not just names) across the repo,
                        with simple spelling-variant handling, and returns
                        matching files ranked by hit count with line snippets.

Run standalone for a quick manual test:
    python kb_search_server.py --self-test "inventory aging"
"""
import os
import re
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

KB_REPO_PATH = Path(os.environ.get("KB_REPO_PATH", r"C:\campair-TraceAI"))

# Only search text-ish files, and skip anything that isn't useful/readable as
# text or would be huge (this repo has multi-MB .xml/.sqlplan trace dumps).
SEARCHABLE_EXTENSIONS = {".md", ".txt", ".sql", ".kql", ".json", ".ps1", ".py", ".yml", ".yaml"}
MAX_FILE_BYTES = 3_000_000  # skip anything bigger than ~3MB when grepping content
SKIP_DIR_NAMES = {".git", ".venv", "node_modules", "__pycache__", ".schema_cache", "renfro"}

# Known spelling/phrasing variants worth expanding a query with. Extend this
# as you notice the agent missing real matches.
SYNONYM_GROUPS = [
    {"aging", "ageing"},
    {"icm", "incident"},
    {"root cause", "rca", "root-cause"},
]

# Generic filler words to drop before searching -- NOT domain words. A spoken
# question like "give me the SQL query to get query id from query store" is
# mostly scaffolding around 4 real concepts (sql, query, id, store); matching
# it as one literal phrase (the original, buggy approach) finds nothing,
# because nobody phrases documentation the way a spoken question is phrased.
STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "is", "are", "was", "were",
    "and", "or", "for", "with", "from", "by", "at", "as", "it", "its",
    "this", "that", "do", "does", "did", "have", "has", "had", "give",
    "me", "please", "can", "could", "would", "get", "provide", "show",
    "find", "tell", "us", "our", "we", "you", "your", "i", "any", "some",
    "what", "how", "past",
}

mcp = FastMCP("kb-search")


def _iter_searchable_files():
    for root, dirs, files in os.walk(KB_REPO_PATH):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        for name in files:
            if Path(name).suffix.lower() in SEARCHABLE_EXTENSIONS:
                path = Path(root) / name
                try:
                    if path.stat().st_size <= MAX_FILE_BYTES:
                        yield path
                except OSError:
                    continue


def _expand_query_terms(query: str) -> list[str]:
    """Tokenizes the query into individual meaningful words (dropping filler
    words), then expands each with known synonyms. Per-word matching -- not
    whole-phrase matching -- is what lets a naturally-phrased question find
    documentation that never uses that exact wording."""
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_]*", query.lower())
    terms = {w for w in words if w not in STOPWORDS and len(w) > 1}
    if not terms:  # everything got stripped (e.g. a 2-word question) -- fall back
        terms = set(words)
    expanded = set(terms)
    for group in SYNONYM_GROUPS:
        if terms & group:  # only pull in a synonym group if the query actually hit it
            expanded.update(group)
    return sorted(expanded)


@mcp.tool()
def list_kb_index() -> str:
    """Returns a fast overview of the knowledge base: the curated ICM title
    index (if present) plus the folder/file listing under docs/icm-investigations
    and icm-health. Call this FIRST for broad questions like "do we have any
    past ICMs about X" before doing a full content search -- it's cheaper and
    often enough to spot the right document by name alone.
    """
    chunks: list[str] = []

    titles_file = KB_REPO_PATH / "_private" / "icm_titles.txt"
    if titles_file.exists():
        text = titles_file.read_text(encoding="utf-8", errors="ignore")
        chunks.append(f"=== ICM title index ({titles_file}) ===\n{text[:20000]}")

    for folder in ("docs/icm-investigations", "icm-health", "docs/tsgs"):
        d = KB_REPO_PATH / folder
        if d.exists():
            names = sorted(p.name for p in d.rglob("*") if p.is_file())
            listing = "\n".join(names[:300])
            chunks.append(f"=== Files under {folder} ===\n{listing}")

    if not chunks:
        return f"No index files found under {KB_REPO_PATH}."
    return "\n\n".join(chunks)


@mcp.tool()
def search_kb_content(query: str, max_files: int = 8, snippets_per_file: int = 3) -> str:
    """Full-text search INSIDE knowledge-base files (not just filenames) for
    `query`. The query is tokenized into individual meaningful words (filler
    words like "give me the" are dropped) and each is searched independently,
    then expanded with known spelling/phrasing variants (e.g. "aging" <->
    "ageing", "ICM" <-> "incident") -- so a naturally-phrased question finds
    documentation that never uses that exact wording. Files are ranked first
    by how many DISTINCT query concepts they cover (a file matching 3 of your
    4 keywords beats one that repeats 1 keyword 50 times), then by hit count.
    Returns the top matches with line snippets so you can decide which
    file(s) to read in full with the filesystem tools' read_file.
    """
    terms = _expand_query_terms(query)
    term_patterns = [(t, re.compile(re.escape(t), re.IGNORECASE)) for t in terms]

    # (distinct_terms_matched, total_hits, path, snippets)
    results: list[tuple[int, int, Path, list[str]]] = []
    for path in _iter_searchable_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()
        hits: list[str] = []
        total_hits = 0
        terms_matched: set[str] = set()
        for i, line in enumerate(lines):
            matched_here = [t for t, p in term_patterns if p.search(line)]
            if matched_here:
                total_hits += 1
                terms_matched.update(matched_here)
                if len(hits) < snippets_per_file:
                    start, end = max(0, i - 1), min(len(lines), i + 2)
                    context = "\n".join(lines[start:end]).strip()
                    hits.append(f"  line {i + 1}: ...{context}...")
        if terms_matched:
            results.append((len(terms_matched), total_hits, path, hits))

    if not results:
        return f"No content matches for {query!r} (searched for: {terms})."

    results.sort(key=lambda r: (r[0], r[1]), reverse=True)
    out = [
        f"Top {min(max_files, len(results))} matches for {query!r} "
        f"(searched for: {terms}):\n"
    ]
    for terms_matched, total_hits, path, hits in results[:max_files]:
        rel = path.relative_to(KB_REPO_PATH)
        out.append(
            f"### {rel}  ({terms_matched}/{len(terms)} concepts, {total_hits} hits)\n"
            + "\n".join(hits)
        )
    return "\n\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--self-test":
        print(search_kb_content(sys.argv[2]))
    else:
        mcp.run(transport="stdio")
