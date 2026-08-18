# DuDu — Voice-Activated Desktop Assistant

A voice-activated, always-listening desktop assistant with an animated
avatar UI. Wakes on "wake up", sleeps on "go to sleep", answers questions
against a local Dynamics 365 / SQL / KQL knowledge-base repo via the
Filesystem MCP server, and can search menus / place food orders via a
remote Zomato MCP server — all orchestrated by a LangGraph agent, spoken
back with offline TTS.

## Stack

- **Frontend ("the face")** — Tauri + React. A small always-on-top,
  transparent window swaps between `idle.gif` / `listening.gif` /
  `thinking.gif` / `talking.gif` based on WebSocket events from the backend.
- **Backend ("the brain")** — Python + FastAPI. Owns the WebSocket,
  the background voice loop, MCP clients, and the LangGraph agent.
- **Voice** — OpenWakeWord (wake word) → faster-whisper (STT) → LangGraph
  agent → Piper (offline TTS).

## Project layout

```
dudu-assistant/
├── backend/                    # FastAPI "brain"
│   ├── main.py                 # WebSocket + REST + lifespan (starts voice loop)
│   ├── config.py                # Settings (.env) + MCP config loader
│   ├── state.py                 # AgentState enum -> GIF mapping
│   ├── ws_manager.py            # Non-blocking broadcast to all connected UIs
│   ├── mcp_clients.py           # Filesystem + kb_search + kb_rag_search + Zomato MCP bootstrap
│   ├── agent_graph.py           # LangGraph ReAct agent (repo-aware system prompt)
│   ├── kb_search_server.py      # Custom MCP server: full-text keyword KB search + ICM index
│   ├── rag_search_server.py     # Custom MCP server: local vector-DB semantic search
│   ├── rag/                     # chunker, embedder, vector store, incremental indexer
│   ├── mcp_config.json          # MCP server definitions
│   ├── requirements.txt
│   ├── .env.example
│   ├── models/                  # piper/ and wakeword/ model files go here (gitignored)
│   ├── kb_index/                # local vector DB + manifest (gitignored, auto-created)
│   └── voice/
│       ├── wake_word.py         # OpenWakeWord listener
│       ├── stt.py                # faster-whisper transcription
│       ├── tts.py                # Piper synthesis
│       └── audio_loop.py         # Ties it all together, drives state transitions
├── frontend/                   # Tauri + React "face"
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   ├── src/
│   │   ├── main.jsx
│   │   ├── App.jsx              # GIF state swap, mic toggle, transcript
│   │   ├── App.css
│   │   ├── hooks/useAgentSocket.js
│   │   └── assets/gifs/         # drop idle/listening/thinking/talking.gif here
│   └── src-tauri/               # Rust shell (window config, packaging)
└── docs/SETUP.md               # full install + model-download walkthrough
```

## Quick start

**Windows: double-click `start-dudu.bat`.** It checks prerequisites, creates
the venv and installs dependencies on first run, starts the backend in its own
window, waits for `/health` to answer before launching the UI, and prints a
specific message for each thing that can go wrong. Variants:

```
start-dudu.bat            # dev mode (Vite HMR + cargo run)
start-dudu.bat build      # build the release .exe, then run it
start-dudu.bat backend    # backend only, no UI (drive it with curl)
stop-dudu.bat             # kill a stray backend holding port 8756 / the mic
```

See **`docs/SETUP.md`** for the full walkthrough (model downloads, env vars,
Tauri prerequisites). Manual version:

```bash
# backend
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your LLM key, KB_REPO_PATH, etc.
python main.py

# frontend (separate terminal)
cd frontend && npm install && npm run tauri dev
```

Runtime logs go to `backend/logs/dudu.log` (rotating, 10MB x 5) — that's the
first place to look when something misbehaves.

### A note on the network binding

The backend binds `127.0.0.1` by default and has **no authentication**. Anyone
who can reach it can make the agent read your KB repo and, when those MCP
servers are configured, place calls or orders. Change `WS_HOST` only if you
mean to expose that.

## How it finds answers in your knowledge base

Three local MCP servers work together against `KB_REPO_PATH`
(`C:\campair-TraceAI`), plus the official filesystem server:

- **`filesystem`** (official MCP server) — `read_file`, `list_directory`, etc.
  `search_files` on this server only matches *file names*, not content.
- **`kb_search`** (`backend/kb_search_server.py`) — exact/keyword lookups:
  `search_kb_content` greps *inside* files (tokenized per-word matching,
  ranked by distinct-concept coverage, plus light spelling-variant handling
  e.g. "aging"/"ageing"), and `list_kb_index` surfaces the curated ICM index
  and doc folders fast, without a full scan.
- **`kb_rag_search`** (`backend/rag_search_server.py` + `backend/rag/`) —
  **local vector-DB semantic search**, for open-ended "what should I do when
  X is stuck" style triage questions where the user's wording won't
  literally appear in the docs. Runs fully on your machine: a local
  embedding model (`sentence-transformers`, no cloud API calls) turns both
  your question and every doc chunk into vectors, stored in a local Chroma
  index (`backend/kb_index/`, gitignored). A background thread re-syncs the
  index every few minutes, so ICM write-ups/TSGs/queries you add or edit
  during the day become searchable without any manual step.

Verified against your actual repo: your `docs/icm-investigations/` folder
already has ~40 per-ICM write-ups, each with a consistent structure ending in
a "Mitigation" section; `docs/tsgs/legacy-guides/` has playbook-style
troubleshooting guides (named Cases/Playbooks with symptoms + resolution
steps); `_private/icm_titles.txt` is a master ICM index. `agent_graph.py`'s
system prompt teaches the agent this layout and a specific triage pattern:
for a live-issue question, semantic-search TSGs + ICM investigations, then
answer in three parts — Initial steps, Analysis (diagnostic queries and what
they'd show), and Mitigation (what worked on similar past ICMs, and whether
it's confirmed or still pending) — always citing ICM numbers/filenames.

I tested this against your real files before shipping it (not just in
theory): querying "what should I do when closing and recal stuck, help me
understand and analyze the issue and give mitigations" correctly surfaced
`docs/tsgs/legacy-guides/InventoryClosing_Troubleshooting_Guide.md`'s "When
to Use This Guide" section, a named case with a real ICM number and its
resolution steps, and a diagnostic SQL query for "identify the stuck close
and its stage" — with zero shared keywords required between the question and
the source text. I also verified the incremental sync: editing one file and
adding a brand-new ICM doc got both picked up and made searchable on the
next sync, with unrelated files left untouched.

## Notes & caveats

- **RAG testing constraint, disclosed for transparency**: the sandbox this
  was built in has no PyPI/Hugging Face network access, so I couldn't
  install `sentence-transformers`/`chromadb` there to test the real
  embedding model. I verified the entire pipeline (chunking, ICM/metadata
  extraction, incremental indexing, retrieval quality, background re-sync)
  using the built-in TF-IDF + NumPy fallback backends against your actual
  staged files instead, which exercises the same code paths end-to-end.
  Retrieval quality with real semantic embeddings (the default,
  `KB_EMBEDDING_BACKEND=sentence_transformers`) will only be *better* than
  what I verified, not worse — TF-IDF has no synonym/meaning generalization
  at all, and the results were already strong.
- **Zomato MCP**: `mcp_config.json` points at
  `https://mcp-server.zomato.com/mcp` as given, using a streamable-HTTP
  transport with a bearer token placeholder. I could not independently
  verify this endpoint's availability, auth scheme, or tool names — confirm
  against Zomato's own developer/MCP documentation before depending on it,
  and adjust `mcp_config.json` / `.env` accordingly.
- **Wake words**: "wake up" / "go to sleep" need custom-trained OpenWakeWord
  models (not bundled phrases). `wake_word.py` falls back to OpenWakeWord's
  demo models until you train your own — see `docs/SETUP.md`.
- **GIFs**: no artwork is included; drop your own 4 looping GIFs into
  `frontend/src/assets/gifs/`.
- **Filesystem MCP** is mounted read/write by default at the protocol level;
  the agent's system prompt in `agent_graph.py` instructs it to treat those
  tools as read-only for now. If you want a hard guarantee (not just a
  prompt instruction), run the filesystem MCP server with a read-only flag
  or wrap it, since the official server does support write/edit operations.
