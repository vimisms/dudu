# DuDu — Setup Guide

## 1. Prerequisites

| Tool | Why | Install |
|---|---|---|
| Node.js 18+ | Frontend build + the Filesystem MCP server runs via `npx` | https://nodejs.org |
| Rust + Cargo | Tauri shell | https://www.rust-lang.org/tools/install |
| Tauri CLI | `npm install` in `frontend/` pulls in `@tauri-apps/cli` | — |
| Python 3.11 | Backend | https://python.org |
| A working microphone + speakers | Voice pipeline | — |

Windows users: Tauri also needs the WebView2 runtime (preinstalled on
Windows 11) and the "Desktop development with C++" workload from the Visual
Studio Build Tools.

## 2. Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # (Windows) or: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env        # (Windows) or: cp .env.example .env
```

Edit `.env`:
- `OPENAI_API_KEY` — the LLM behind the LangGraph agent.
- `KB_REPO_PATH` — already defaults to `C:\campair-TraceAI`; point it at
  whatever local D365/SQL/KQL repo you want the assistant to read.
- `ZOMATO_MCP_URL` / `ZOMATO_MCP_API_KEY` — confirm the real URL and auth
  scheme against Zomato's own MCP/developer documentation before relying on
  this; it isn't independently verified here.

### Download the voice models

1. **Whisper (STT)** — nothing to download manually; `faster-whisper` pulls
   the `small` model (configurable via `WHISPER_MODEL_SIZE`) from Hugging
   Face on first run and caches it.
2. **Piper (TTS)** — download a voice, e.g. `en_US-lessac-medium`, from
   https://github.com/rhasspy/piper/releases (grab both the `.onnx` and
   `.onnx.json` files) and place them at the path set in
   `PIPER_VOICE_PATH` (default `backend/models/piper/`).
3. **OpenWakeWord (wake word)** — "wake up" / "go to sleep" are *not*
   built-in phrases. Either:
   - Train custom models using openWakeWord's training notebook
     (https://github.com/dscripka/openWakeWord#training-new-models) and drop
     the resulting `.onnx` files at `backend/models/wakeword/wake_up.onnx`
     and `backend/models/wakeword/go_to_sleep.onnx`, or
   - Leave them out for now — `wake_word.py` falls back to openWakeWord's
     bundled demo models (e.g. "hey jarvis") so you can test the rest of the
     pipeline immediately, then swap in your trained models later.

### Filesystem MCP

No manual install needed — `mcp_config.json` spawns it on demand via
`npx -y @modelcontextprotocol/server-filesystem <KB_REPO_PATH>`. First run
will download the package; make sure the machine has npm registry access.

### Local RAG / semantic search (`rag_search_server.py`)

This gives the assistant meaning-based search over `campair-TraceAI` — the
tool it uses for "what should I do when X is stuck" style questions.
Everything is local: embeddings and the vector index both live on your
machine, nothing is sent to a cloud API for this.

- **First run**: `sentence-transformers` downloads the embedding model
  (`BAAI/bge-small-en-v1.5` by default) once, ~130MB, then it's fully
  offline. If you'd rather not download anything, set
  `KB_EMBEDDING_BACKEND=tfidf` in `.env` — a classic keyword-weighted vector
  representation with zero model download, at the cost of not generalizing
  across synonyms/phrasing the way real embeddings do.
- **Indexing**: the first time it runs it indexes the whole repo (well under
  a minute for this repo's size); after that, a background thread re-syncs
  every `KB_REINDEX_INTERVAL_SECONDS` (default 300s = 5 min), only
  re-embedding files that actually changed since the last pass. Since you're
  editing `campair-TraceAI` throughout the day, this means new ICM
  write-ups/TSGs/queries become searchable within a few minutes without you
  doing anything. To force it sooner, ask the assistant to reindex (it has a
  `reindex_kb` tool), or lower `KB_REINDEX_INTERVAL_SECONDS`.
- **Where it's stored**: `backend/kb_index/` (gitignored — it's derived,
  regenerable, and holds vectors of your ICM content, so keep it local).
- **Swapping backends**: `KB_VECTOR_BACKEND=chroma` (recommended, in
  `requirements.txt`) or `numpy` (zero extra dependency, fine up to tens of
  thousands of chunks — this repo is a few thousand). If you ever switch
  `KB_EMBEDDING_MODEL`, ask for a full reindex (`reindex_kb(full=true)`)
  since old and new vectors aren't comparable.

### Run the backend

```bash
python main.py
# -> WebSocket + REST on ws://127.0.0.1:8756/ws and http://127.0.0.1:8756
```

Sanity check without a microphone:

```bash
curl -X POST http://127.0.0.1:8756/command -H "Content-Type: application/json" -d "{\"text\": \"Find the SQL view for inventory closing\"}"
```

## 3. Frontend

```bash
cd frontend
npm install
```

Add your 4 GIFs to `src/assets/gifs/` (see the README stub already there),
then generate app icons once:

```bash
npx tauri icon path/to/some-square-logo.png
```

Run in dev mode (make sure the backend is already running):

```bash
npm run tauri dev
```

Build a distributable desktop app:

```bash
npm run tauri build
```

## 4. How the pieces talk to each other

```
 mic ──> OpenWakeWord ──> (awake) ──> VAD-gated recording ──> faster-whisper
                                                                    │
                                                                    ▼
 Tauri/React UI  <──WebSocket (state, transcript, audio)── FastAPI backend <── LangGraph ReAct agent
      (idle/listening/                                            │
       thinking/talking                          ┌──────────┬─────┴──────┬──────────────┐
       GIFs)                              Filesystem MCP  kb_search   kb_rag_search   Zomato MCP
                                          (read/list/grep) (keyword   (local vector    (remote HTTP)
                                                             content   DB, semantic
                                                             search)   search, auto-
                                                                       resyncs every
                                                                       few minutes)
                                                      all three local tools scoped to
                                                            campair-TraceAI
```

State transitions are pushed from the backend the instant they happen, so
the avatar GIF swaps immediately without the UI ever calling into or
blocking on the agent directly.
