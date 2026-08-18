"""Central settings, loaded once from .env. Import `settings` everywhere else."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from loguru import logger
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM -- set llm_provider to pick which one create_react_agent uses;
    # only that provider's API key needs to be real, the others can stay blank.
    llm_provider: str = "openai"  # "openai" | "azure_openai" | "anthropic" | "google"
    agent_model: str = "gpt-4o-mini"  # e.g. "claude-opus-5" / "claude-sonnet-5" / "gemini-2.5-pro" for the other providers
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""

    # Azure OpenAI (used only if llm_provider = "azure_openai")
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""       # e.g. https://<your-resource-name>.openai.azure.com/
    azure_openai_deployment: str = ""     # the DEPLOYMENT name you chose in Azure, not the base model name
    azure_openai_api_version: str = "2024-08-01-preview"  # confirm this matches what your deployment supports

    # Knowledge base (Filesystem MCP root)
    kb_repo_path: str = r"C:\campair-TraceAI"

    # Local RAG (rag_search_server.py) -- vector index for semantic/triage search
    kb_index_dir: str = "./kb_index"
    kb_embedding_backend: str = "sentence_transformers"  # or "tfidf" (zero-download fallback)
    kb_vector_backend: str = "chroma"                    # or "numpy" (zero-dependency fallback)
    kb_embedding_model: str = "BAAI/bge-small-en-v1.5"
    kb_reindex_interval_seconds: int = 300  # background re-sync cadence; repo is edited daily

    # Zomato MCP
    zomato_mcp_url: str = "https://mcp-server.zomato.com/mcp"
    zomato_mcp_api_key: str = ""

    # QuickCommerce MCP (hosted) -- grocery/product search + delivery ETA
    quickcommerce_api_key: str = ""

    # Twilio. Two credential styles both work against the REST API:
    #   API Key + Secret (SK.../secret)  -- preferred: scoped and revocable
    #   Account SID + Auth Token         -- what a plain `curl -u SID:TOKEN` uses
    # The MCP server takes one string, ACCOUNT_SID/USERNAME:PASSWORD, so
    # twilio_mcp_auth below assembles whichever you've actually configured.
    twilio_account_sid: str = ""
    twilio_api_key: str = ""
    twilio_api_secret: str = ""
    twilio_auth_token: str = ""
    # The number SMS is sent FROM, E.164. Surfaced to the agent so you never
    # have to dictate it.
    twilio_from_number: str = ""

    # ICM MCP (remote, Azure API Management)
    icm_mcp_api_key: str = ""

    # SerpAPI (Google web/local/shopping search)
    serpapi_api_key: str = ""

    # How long to wait for one MCP server to hand over its tool list. The npx
    # servers (filesystem, twilio) resolve and sometimes download a package
    # first, which on a cold cache or a slow network routinely exceeds 60s --
    # and a timeout here means silently losing every tool that server provides.
    mcp_load_timeout_s: int = 150

    # ICM MCP is fronted by Azure API Management, which does NOT support OAuth
    # dynamic client registration (its /register endpoint returns 404). Set an
    # app registration's client ID here to skip registration and go straight to
    # authorization. Leave blank for servers that do support DCR (e.g. Swiggy).
    icm_oauth_client_id: str = ""
    icm_oauth_client_secret: str = ""

    # Swiggy Builders Club MCP (food ordering / Instamart / Dineout). No key --
    # it authenticates via OAuth with dynamic client registration on first use.
    # Off by default so a browser consent prompt never appears unrequested.
    enable_swiggy: bool = False

    # Wake words
    wake_word_on: str = "wake_up"
    wake_word_off: str = "go_to_sleep"
    # "openwakeword" = fast on-device models but ONLY the bundled phrases
    # (alexa/hey_jarvis/...). "whisper" = transcribe short speech snippets and
    # match any custom word (e.g. "dudu"), VAD-gated so it only runs on speech.
    wake_engine: str = "openwakeword"

    # How speech reaches the agent:
    #   "hold_to_talk" (default) -- true push-to-talk. Hold the Talk button (or
    #       the spacebar) while speaking, release to send. The mic device is
    #       open ONLY while held, and releasing is an explicit end-of-speech
    #       signal, so there's no silence-detection delay at all: release and it
    #       transcribes immediately. Lowest latency and the strongest privacy
    #       story of the three.
    #   "push_to_talk" -- tap the mic once and it keeps listening; each
    #       utterance is end-pointed by silence and sent. Closest to Claude's /
    #       Gemini's voice mode. Hands-free, but costs ~700ms per turn waiting
    #       to be sure you've stopped.
    #   "wake_word" -- classic always-on assistant. Mic can stay on all day but
    #       only utterances starting with "Dudu" are acted on.
    voice_mode: str = "hold_to_talk"

    # Whether the microphone is live at startup. Default OFF: an always-on mic
    # that transcribes every noise it hears is a surprising thing to opt into by
    # default. Click the mic chip in the UI to start listening.
    mic_enabled_on_start: bool = False

    # Trailing silence that ends a wake utterance, in ms. Dead time on every
    # turn: you stop speaking and nothing happens until it elapses. 700ms is a
    # snappier default than the original 900; go lower for faster response, but
    # too low starts cutting you off at natural mid-sentence pauses.
    wake_trailing_silence_ms: int = 700

    # Voice models
    piper_voice_path: str = "./models/piper/en_US-lessac-medium.onnx"
    # Use the ".en" English-only variants -- meaningfully faster and more
    # accurate than the multilingual ones at the same size. "base.en" is a good
    # balance on CPU; "tiny.en" if the logged RTF is still near 1.0.
    whisper_model_size: str = "base.en"

    # Microphone input gain applied to captured audio before wake-word/STT.
    # Bump this (e.g. 3-6) if your mic is quiet and the wake word won't trigger.
    mic_gain: float = 1.0
    # Wake-word detection score threshold (0-1); lower = more sensitive.
    wake_threshold: float = 0.5

    # Voice capture boundaries (after the wake word):
    # how long to wait for you to START talking, how much trailing silence ends
    # the instruction, and a hard cap on one utterance.
    listen_leadin_s: float = 6.0
    silence_ms_to_stop: int = 2000
    # Cap for VAD-ended capture (wake-word / push-to-talk modes), where the end
    # of speech is *inferred* and a runaway recording is a real risk.
    max_utterance_s: int = 30
    # Cap for hold-to-talk, where YOU decide when it ends by letting go. This is
    # only a runaway guard (a key stuck down), not a limit on how much you're
    # allowed to say, so it's generous -- 30s was silently truncating long
    # instructions mid-sentence.
    max_hold_s: int = 180

    # Hard cap on how long a single task may run before it's stopped and
    # reported back (so a slow/looping tool can't spin forever). A semantic
    # search plus a couple of read_file calls plus the final answer routinely
    # needs more than 90s on a cold index, so this defaults generously.
    task_timeout_s: int = 180

    # How long the post-task summarizer LLM call may take before falling back
    # to a truncated summary (the full answer is already on screen by then).
    summary_timeout_s: int = 25

    # How many previous (question, answer) exchanges to replay into a new task
    # so follow-ups like "and the second one?" resolve. 0 disables follow-ups.
    history_turns: int = 4

    # Server. 127.0.0.1 deliberately: the agent can read your KB repo and (when
    # configured) place calls/orders, and there is NO authentication on /command
    # or /ws -- binding 0.0.0.0 exposes all of that to your whole network.
    # Only change this if you understand that.
    ws_host: str = "127.0.0.1"
    ws_port: int = 8756

    # Rotating log file, relative to backend/ (set to "" to log to console only).
    log_file: str = "./logs/dudu.log"
    log_level: str = "INFO"

    @property
    def twilio_mcp_auth(self) -> str:
        """The ACCOUNT_SID/API_KEY:API_SECRET string the Twilio MCP requires.

        An API Key is mandatory here. Twilio's REST API happily accepts
        Account SID + Auth Token as basic auth (that's what a plain `curl -u`
        does), but @twilio-alpha/mcp validates the credential shape and rejects
        it with "Error: Invalid AccountSid" -- confirmed via diagnose_mcp.py.
        So an auth token alone can't drive the MCP server, however well it works
        with curl. Create a Standard API key at:
        Twilio Console > Account > API keys & tokens > Create API key.
        """
        if not self.twilio_account_sid:
            return ""
        if self.twilio_api_key and self.twilio_api_secret:
            return f"{self.twilio_account_sid}/{self.twilio_api_key}:{self.twilio_api_secret}"
        return ""


settings = Settings()

# Servers whose mcp_config.json entry carried "auth": "oauth". Populated by
# load_mcp_server_config(); read by mcp_clients when opening each session.
OAUTH_SERVERS: set[str] = set()

# server name -> list of fnmatch patterns from "only_tools". Every tool name and
# description is re-sent to the model on EVERY request, so an unfiltered server
# (Twilio ships ~197 tools) taxes latency and cost on every single turn, and
# makes tool selection worse by burying the useful ones in noise.
TOOL_ALLOWLIST: dict[str, list[str]] = {}

# server name -> (client_id, client_secret) for OAuth servers that do NOT
# support dynamic client registration. Empty means "register dynamically".
OAUTH_CLIENTS: dict[str, tuple[str, str]] = {}


def load_mcp_server_config() -> dict:
    """Read mcp_config.json and expand ${ENV_VAR} placeholders against `settings`/env."""
    raw = (BACKEND_DIR / "mcp_config.json").read_text(encoding="utf-8")

    def _expand(match: re.Match) -> str:
        key = match.group(1)
        # prefer explicit settings fields, fall back to raw environment
        val = str(getattr(settings, key.lower(), os.environ.get(key, "")))
        # values land inside JSON string literals, so escape backslashes/quotes
        # (e.g. a Windows path C:\campair-TraceAI would break json.loads otherwise)
        return val.replace("\\", "\\\\").replace('"', '\\"')

    expanded = re.sub(r"\$\{([A-Z_]+)\}", _expand, raw)
    config = json.loads(expanded)
    # strip helper "_comment" keys before handing to the MCP client
    for server in config.values():
        server.pop("_comment", None)

    # Launch the local stdio servers with THIS interpreter (the venv), not a bare
    # "python" off PATH -- otherwise they run under system Python where `mcp`,
    # `chromadb`, etc. aren't installed and crash on import.
    for server in config.values():
        if server.get("command") == "python":
            server["command"] = sys.executable

    # Merge the parent environment under each server's explicit env block.
    # Depending on the MCP SDK version, supplying `env` can REPLACE the child's
    # environment rather than extend it -- and a Windows process started without
    # SYSTEMROOT / PATH / TEMP fails to load native extension DLLs (numpy, torch,
    # sqlite via chromadb) and dies before it can answer `initialize`. The parent
    # then only sees "unhandled errors in a TaskGroup", with no clue why.
    # Explicit values still win over inherited ones.
    for server in config.values():
        if "env" in server:
            server["env"] = {**os.environ, **server["env"]}

    # Zomato is optional and needs a real remote URL + bearer token. Skip it
    # unless an API key is configured, so its absence doesn't break the agent.
    if not settings.zomato_mcp_api_key:
        config.pop("zomato", None)

    # QuickCommerce needs an X-API-Key; skip until one is configured.
    if not settings.quickcommerce_api_key:
        config.pop("quickcommerce", None)

    # Twilio: skip unless we can build a complete auth string from EITHER an
    # API key+secret or an account SID+auth token (an empty auth arg would just
    # make npx error out with something unhelpful).
    if not settings.twilio_mcp_auth:
        if settings.twilio_account_sid and settings.twilio_auth_token:
            # Distinguish "not configured" from "configured with the one
            # credential type this server refuses" -- otherwise it just
            # silently disappears from the tool list.
            logger.warning(
                "Twilio skipped: an Auth Token works with curl but @twilio-alpha/mcp "
                "requires an API Key. Create one (Console > Account > API keys & "
                "tokens > Create API key, type Standard) and set TWILIO_API_KEY / "
                "TWILIO_API_SECRET."
            )
        config.pop("twilio", None)

    # SerpAPI needs a key; skip until one is configured.
    if not settings.serpapi_api_key:
        config.pop("serpapi", None)

    # Swiggy needs no key, but its first connection opens a browser for OAuth
    # consent -- so it stays off until explicitly enabled.
    if not settings.enable_swiggy:
        config.pop("swiggy_food", None)

    # ICM is fronted by Azure API Management, which has no dynamic client
    # registration endpoint -- without a pre-registered client ID the OAuth
    # flow dies with a 404 and a wall of traceback on EVERY startup. Skip it
    # until there's a client ID to use, so the noise doesn't bury real errors.
    if not settings.icm_oauth_client_id:
        if "my-icm-mcp-server-c03f0519" in config:
            logger.info(
                "ICM MCP skipped: no ICM_OAUTH_CLIENT_ID set, and this endpoint "
                "does not support dynamic client registration. See .env.example."
            )
        config.pop("my-icm-mcp-server-c03f0519", None)

    # "auth": "oauth" is OUR marker, not part of the MCP connection schema --
    # pop it and record the server name so mcp_clients can attach an OAuth
    # provider. Leaving it in the dict would make the adapter reject it.
    OAUTH_SERVERS.clear()
    TOOL_ALLOWLIST.clear()
    OAUTH_CLIENTS.clear()
    for name, server in config.items():
        if server.pop("auth", None) == "oauth":
            OAUTH_SERVERS.add(name)
        client_id = (server.pop("oauth_client_id", "") or "").strip()
        client_secret = (server.pop("oauth_client_secret", "") or "").strip()
        if client_id:
            OAUTH_CLIENTS[name] = (client_id, client_secret)
        # "only_tools" is also ours, not part of the MCP connection schema.
        patterns = server.pop("only_tools", None)
        if patterns:
            TOOL_ALLOWLIST[name] = list(patterns)

    return config
