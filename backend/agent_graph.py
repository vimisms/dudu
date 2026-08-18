"""LangGraph agent definition. Rebuilt whenever the MCP tool list changes
(e.g. after a reconnect), otherwise cached."""
from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from loguru import logger

from config import settings
from mcp_clients import get_agent_tools

SYSTEM_PROMPT = """You are DuDu, a spoken-first voice assistant and this
user's day-to-day support/DRI copilot for D365 F&O costing & inventory
incidents.

HOW INPUT REACHES YOU -- read this before responding to anything:
The user talks to you out loud. Their speech is transcribed and handed to you
as text. That transcription IS how you hear them, so:
- NEVER say you cannot hear audio, that you only accept typed input, that you
  are text-only, or anything else about how messages reach you. From the user's
  side they just spoke to you and you heard them. Saying otherwise is both
  confusing and, as far as they're concerned, wrong.
- Never describe your own plumbing (transcription, TTS, "your app may read this
  aloud"). Just answer, the way a colleague on a call would.
- If a message is garbled or you genuinely can't tell what was meant, ask them
  to say it again -- exactly like a person who half-caught a sentence. Don't
  explain that it was a transcription problem.
- Transcripts arrive without reliable punctuation or capitalisation, and speech
  recognition mangles domain vocabulary. Read through it charitably: "invent
  close"/"inventory clothes" = InventClosing, "recal"/"recall" =
  recalculation, "invent sum" = InventSum, "kusto"/"custo" = Kusto, "eye see em"
  = ICM. Spoken numbers may arrive as words or be run together -- an ICM number
  is a long digit string, so normalise it before searching.
- A short, vague utterance ("what about closing", "is it stuck") is a real
  question asked mid-conversation, not an error. Use the conversation so far to
  resolve it, or ask ONE brief clarifying question.

REMINDERS:
When the user asks to be reminded about something ("remind me in 20 minutes to
check the close", "ping me in an hour"), call set_reminder with the delay in
minutes and the thing to be reminded of, phrased so it makes sense SPOKEN back
to them later ("check whether the inventory close finished"), not as a note to
yourself. It will chime and be read aloud at the right time. Confirm in one
short line. list_reminders and cancel_reminders manage what's pending.

You can act using these families of tools:

1. semantic_search_kb (local vector search, MEANING-based) -- your FIRST
   STOP for open-ended / triage questions: "what should I do when X is
   stuck", "have we seen anything like this", "how do I investigate Y". It
   finds relevant passages even when the user's wording shares no words with
   the source document, because it searches by meaning, not literal text.
   Optionally filter by doc_type (icm-investigations, tsgs, sql-reference,
   kusto, table-reference, schemas, icm-health). This index is kept fresh
   automatically as the repo is edited -- if a result seems stale, you can
   call reindex_kb to force an immediate re-sync, or kb_index_status to check
   how old the index is.

2. kb_search tools (list_kb_index, search_kb_content) -- for EXACT lookups:
   a specific SQL query, a table/field name, an ICM number. list_kb_index
   gives you the curated ICM title index (_private/icm_titles.txt: one line
   per ICM, "<ICM number> TAB <short title>") plus folder listings.
   search_kb_content greps literal file contents (with light spelling-variant
   handling, e.g. "aging"/"ageing") when you know roughly what word to look
   for. Prefer semantic_search_kb when the question is a description of a
   symptom rather than a specific keyword.

3. Filesystem tools (read_file, read_multiple_files, list_directory,
   search_files -- filename matching only, get_file_info), scoped to a local
   repo containing Microsoft Dynamics 365 finance/inventory integrations, SQL
   view scripts, and KQL dashboards.

   Be deliberate about read_file -- it is the single biggest cause of a slow
   answer. Some investigation write-ups in this repo are ENORMOUS (several are
   100KB-250KB, i.e. tens of thousands of tokens each). Reading one of those
   "just to be safe" can add tens of seconds and may crowd out everything else.
   So:
   - The semantic search results are usually enough on their own: each hit
     already carries its source file, ICM number and section, plus the matching
     passage. Answer directly from them when they cover the question.
   - Call get_file_info first if you're unsure of a file's size.
   - Read at most 1-2 files per question, and only when the snippets are
     genuinely insufficient (e.g. you need the exact steps of a Mitigation
     section you've only seen the start of).
   - Prefer a second, more targeted semantic_search_kb call over reading a
     large file end to end.

   Repo layout worth knowing:
   - docs/icm-investigations/*.md -- one write-up per past ICM (Microsoft
     support incident), named "ICM<number>_<ShortDescription>_
     Investigation.md". Consistent structure: problem statement, root-cause
     analysis/hypotheses, diagnostic queries, and a "Mitigation" section near
     the end = the actual fix/workaround applied. Some are marked pending/
     unresolved rather than fixed; say so plainly if that's the case.
   - docs/tsgs/ -- troubleshooting guides (broader playbooks, not tied to one
     ICM; often the best FIRST-STEPS source for "X is stuck" questions,
     structured as named Cases/Playbooks with symptoms + resolution steps).
   - _private/icm_titles.txt -- the master ICM index.
   - docs/sql-reference/, docs/kusto/, docs/table-reference/, docs/schemas/ --
     supporting SQL/KQL/table reference material.
   - icm-health/ -- SLA tracking and queue-health dashboards, not root-cause
     write-ups.

4. Zomato tools, for finding food (menus, restaurants) and placing orders,
   including building a cart and handing off to QR-based checkout.

5. QuickCommerce tools (search_products, get_item_details, check_delivery_eta,
   group_search, group_eta, check_credits, list_platforms), for real-time
   grocery/product search, price comparison and delivery ETA across Indian
   quick-commerce and marketplace apps (BlinkIt, Zepto, Swiggy Instamart,
   BigBasket, DMart, JioMart, Flipkart Minutes, Amazon, Nykaa, Myntra,
   Flipkart). Most tools need a latitude/longitude (and sometimes a pincode) --
   ask the user for their location if you don't have it. Use group_search to
   compare a product across several platforms in one call.

6. Twilio tools, for telephony such as placing voice calls and sending SMS.
   These take real-world actions and cost money, so always confirm the exact
   number and message/purpose with the user before calling or texting.

7. Web search tools (google_search, google_local, google_shopping), for
   anything on the open web: general lookups/how-tos/news (google_search),
   finding local businesses like doctors/restaurants (google_local, ask for or
   infer a location), and products/prices (google_shopping). Use these whenever
   the answer isn't in the local knowledge base.

When you present web-search results, format them as clean, scannable markdown:
a one-line intro, then a numbered list where each item's title is a markdown
link to its URL; include the snippet for web results, address + phone + rating
for places, and price + seller for products. Never dump raw JSON.

(Tools from external servers that need credentials, e.g. Zomato/QuickCommerce/
Twilio/SerpAPI, only appear when their API keys are configured; if a capability
isn't available, say so rather than pretending to have acted.)

Handling a live/new issue the user describes (e.g. "closing and
recalculation is stuck, what should I do"):
- Treat this as triage, not a lookup. Call semantic_search_kb (try it
  unfiltered, and again filtered to doc_type="tsgs" and
  doc_type="icm-investigations" if the first pass is thin) to gather
  candidate guidance, then read_file the most relevant matches in full.
- Structure your answer in three parts, pulled from what you actually found
  -- do not invent steps: (1) Initial steps -- what to check/try first
  (from a TSG's "When to use this guide" / early playbook steps), (2)
  Analysis -- how to confirm the root cause (diagnostic queries, what to run
  and what the result would mean), (3) Mitigation -- the fix(es) that
  worked on similar past ICMs, and whether each is confirmed-working or
  still pending.
- Always cite the ICM number(s) and/or TSG filename(s) you drew from.
- If nothing relevant turns up, say so directly -- do not guess or
  generalize from unrelated ICMs.

Answering exact lookups (a specific query, a fix for a known ICM):
- Use kb_search or search_files to find the 1-3 most relevant documents,
  then read_file the best match(es) in full before answering.
- Always cite the ICM number and/or filename.

Rules:
- You'll be read aloud by TTS, but don't sacrifice usefulness for brevity:
  simple factual questions get 1-3 spoken sentences; triage/troubleshooting
  questions get the full structured answer above, spoken as short steps.
- Never read a raw SQL/KQL query aloud line-by-line -- describe what it does
  in one sentence ("this checks which InventClosing record is stuck") and
  say the full query is available in the transcript; the full text (query
  included) still goes to the transcript either way.
- Before placing any Zomato order or completing checkout, briefly confirm the
  item, restaurant, and total with the user in your reply.
- Never invent file contents, query results, or menu items -- always call a
  tool to check.
"""

_agent = None
_agent_lock = asyncio.Lock()  # two concurrent tasks must not both build the agent
_checkpointer = MemorySaver()
_llm = None  # cached chat model; rebuilding one per summary call is wasteful

# Whether the system prompt is baked into the graph (preferred) or has to be
# prepended to every turn's message list. Decided once, in _prompt_kwargs().
_PROMPT_IN_GRAPH = False


def agent_is_ready() -> bool:
    """True once the agent + all MCP tools have finished loading."""
    return _agent is not None


def _prompt_kwargs() -> dict:
    """Pass SYSTEM_PROMPT to create_react_agent under whichever keyword this
    version of LangGraph uses.

    The parameter has been renamed twice (messages_modifier -> state_modifier ->
    prompt), so pinning to one name silently breaks on upgrade -- and the
    failure mode is nasty: the agent still runs, just with no system prompt and
    therefore no idea about the repo layout or the triage format. Baking the
    prompt into the graph also means it is NOT re-sent as a new SystemMessage on
    every turn, which is what previously made conversation history unusable.
    """
    global _PROMPT_IN_GRAPH
    try:
        params = inspect.signature(create_react_agent).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        params = {}
    for key in ("prompt", "state_modifier", "messages_modifier"):
        if key in params:
            _PROMPT_IN_GRAPH = True
            return {key: SYSTEM_PROMPT}
    logger.warning(
        "This LangGraph build exposes no system-prompt parameter on "
        "create_react_agent; falling back to prepending it per turn."
    )
    _PROMPT_IN_GRAPH = False
    return {}


def _build_llm():
    """Picks the chat model class based on LLM_PROVIDER -- LangGraph's
    create_react_agent only needs a LangChain chat model that supports tool
    calling, and OpenAI, Anthropic (Claude), and Google (Gemini) all do, so
    swapping providers is a config change, not a code change. Imports are
    lazy so you only need the ONE provider package actually installed for
    whichever LLM_PROVIDER you choose (see requirements.txt)."""
    provider = settings.llm_provider.lower()

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic  # noqa: PLC0415

        return ChatAnthropic(model=settings.agent_model, api_key=settings.anthropic_api_key, temperature=0.2)

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI  # noqa: PLC0415

        return ChatGoogleGenerativeAI(
            model=settings.agent_model, google_api_key=settings.google_api_key, temperature=0.2
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI  # noqa: PLC0415

        return ChatOpenAI(model=settings.agent_model, api_key=settings.openai_api_key, temperature=0.2)

    if provider == "azure_openai":
        # Same langchain-openai package as the "openai" branch above -- no
        # new install needed if you've already run requirements.txt.
        from langchain_openai import AzureChatOpenAI  # noqa: PLC0415

        return AzureChatOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            azure_deployment=settings.azure_openai_deployment,
            api_version=settings.azure_openai_api_version,
            api_key=settings.azure_openai_api_key,
            temperature=1,  # gpt-5 reasoning models only accept the default temperature of 1
        )

    raise ValueError(
        f"Unknown LLM_PROVIDER={provider!r} -- set it to 'openai', 'azure_openai', 'anthropic', or 'google' in .env."
    )


def get_llm():
    """Cached chat model. Shared by the agent and the summarizer."""
    global _llm
    if _llm is None:
        _llm = _build_llm()
    return _llm


async def get_agent():
    """Build (once) and return the compiled LangGraph ReAct agent.

    Guarded by a lock: warm-up at boot and a fast first command can otherwise
    race, each spawning its own full set of MCP subprocesses.
    """
    global _agent
    if _agent is not None:
        return _agent
    async with _agent_lock:
        if _agent is None:
            tools = await get_agent_tools()
            llm = get_llm()
            _agent = create_react_agent(llm, tools, checkpointer=_checkpointer, **_prompt_kwargs())
            logger.info(
                "LangGraph agent ready ({}/{}) with {} tools",
                settings.llm_provider,
                settings.agent_model,
                len(tools),
            )
    return _agent


def _chunk_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


_USER_INSTRUCTIONS_PATH = Path(__file__).resolve().parent / "user_instructions.md"
_user_instructions_cache: tuple[float, str] = (0.0, "")


def load_user_instructions() -> str:
    """Your standing instructions from user_instructions.md.

    Re-read whenever the file changes on disk (mtime check, so it's a stat call
    per turn, not a read). Editing the file takes effect on your very next
    instruction -- no restart -- which is the whole point: formatting
    preferences are exactly the thing you want to tweak and immediately retry.

    Kept separate from SYSTEM_PROMPT and injected per turn rather than baked
    into the graph, because the compiled agent captures its prompt at build
    time and would otherwise pin whatever the file said at startup.
    """
    global _user_instructions_cache
    try:
        mtime = _USER_INSTRUCTIONS_PATH.stat().st_mtime
    except OSError:
        return ""  # file deleted -- no standing instructions, not an error
    cached_mtime, cached_text = _user_instructions_cache
    if mtime == cached_mtime:
        return cached_text
    try:
        raw = _USER_INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        logger.warning("Could not read user_instructions.md: {}", exc)
        return cached_text
    # Drop the explanatory header above the '---' divider; it's for you, not
    # the model, and it would otherwise read as instructions.
    body = raw.split("\n---\n", 1)[-1].strip()
    _user_instructions_cache = (mtime, body)
    logger.info("Loaded standing instructions ({} chars)", len(body))
    return body


def _runtime_facts() -> str:
    """Configured values the agent needs but shouldn't have to ask for.

    These live in .env, so without this the agent would have no way to know
    them and would keep asking questions the user has already answered once in
    configuration -- exactly the round-trip the standing instructions forbid.
    """
    lines = []
    if settings.twilio_from_number:
        lines.append(
            f"- When sending SMS, always send FROM {settings.twilio_from_number} "
            "(the user's Twilio number). Never ask which number to send from."
        )
    if not lines:
        return ""
    return "Configured facts about this user's setup:\n" + "\n".join(lines)


def _build_messages(user_text: str, history: list[tuple[str, str]] | None) -> list:
    """Assemble one turn's messages: optional system prompt, replayed history,
    then the new question.

    Why replay history instead of relying on the checkpointer: every task gets
    its own thread_id so that several instructions can run genuinely in
    parallel without interleaving into one shared message list. That isolation
    is correct, but it also meant the MemorySaver never had anything to restore
    and follow-ups like "what about the second one?" had no referent. Replaying
    the last few exchanges gives back follow-up support while keeping tasks
    independent.
    """
    messages: list = []
    if not _PROMPT_IN_GRAPH:
        messages.append(SystemMessage(content=SYSTEM_PROMPT))

    facts = _runtime_facts()
    if facts:
        messages.append(SystemMessage(content=facts))

    standing = load_user_instructions()
    if standing:
        # Last system message wins on conflicts, which is what you want: these
        # are the user's own rules and should override the built-in defaults.
        messages.append(
            SystemMessage(
                content=(
                    "The user's standing instructions. These apply to EVERY reply and "
                    "override the general guidance above wherever they conflict:\n\n"
                    f"{standing}"
                )
            )
        )

    for role, text in history or []:
        if not text:
            continue
        messages.append(HumanMessage(content=text) if role == "user" else AIMessage(content=text))
    messages.append(HumanMessage(content=user_text))
    return messages


async def run_agent(
    user_text: str,
    thread_id: str = "default",
    on_chunk: Callable[[str], Awaitable[None]] | None = None,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    history: list[tuple[str, str]] | None = None,
) -> str:
    """Run one turn while exposing the latest answer text and tool activity."""
    agent = await get_agent()
    final_text = ""
    generation_text = ""
    generation_id = None
    announced_tools: set[str] = set()
    # Timing breakdown. "This is slow" is unactionable without knowing whether
    # the time went to the model thinking, a tool running, or the answer being
    # written -- these three have completely different fixes.
    t_start = time.monotonic()
    t_first_token: float | None = None
    tool_marks: list[tuple[str, float]] = []

    async for message, _metadata in agent.astream(
        {"messages": _build_messages(user_text, history)},
        config={"configurable": {"thread_id": thread_id}},
        stream_mode="messages",
    ):
        if not isinstance(message, AIMessageChunk):
            continue

        if message.id and message.id != generation_id:
            generation_id = message.id
            generation_text = ""

        for tool_chunk in message.tool_call_chunks or []:
            tool_name = tool_chunk.get("name")
            if tool_name and tool_name not in announced_tools:
                announced_tools.add(tool_name)
                elapsed = time.monotonic() - t_start
                tool_marks.append((tool_name, elapsed))
                logger.info("[{:.1f}s] tool -> {}", elapsed, tool_name)
                if on_progress:
                    await on_progress(f"Searching with {tool_name.replace('_', ' ')}")

        piece = _chunk_text(message.content)
        if piece:
            if t_first_token is None:
                t_first_token = time.monotonic() - t_start
            generation_text += piece
            final_text = generation_text
            if on_chunk:
                await on_chunk(final_text)

    total = time.monotonic() - t_start
    logger.info(
        "Agent turn: {:.1f}s total | first answer token at {} | {} tool call(s): {}",
        total,
        f"{t_first_token:.1f}s" if t_first_token is not None else "never",
        len(tool_marks),
        ", ".join(f"{n}@{t:.1f}s" for n, t in tool_marks) or "none",
    )
    return final_text


SUMMARY_PROMPT = """You summarize completed assistant work for a notification card.
Given the user's original instruction and the assistant's full answer, write a
concise 4-5 line summary of what was done and the key result. Plain sentences,
no markdown headers, no code blocks, no bullet symbols. Do not restate the whole
answer -- capture the outcome and anything the user must know."""


async def summarize_result(instruction: str, output: str) -> str:
    """Produce a 4-5 line plain-text summary of a finished task's output."""
    llm = get_llm()
    messages = [
        SystemMessage(content=SUMMARY_PROMPT),
        HumanMessage(content=f"INSTRUCTION:\n{instruction}\n\nFULL ANSWER:\n{output}"),
    ]
    result = await llm.ainvoke(messages)
    summary = (result.content or "").strip()
    return summary or "Task completed."
