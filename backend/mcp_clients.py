"""MCP client bootstrap: connects to the Filesystem MCP (local D365/SQL/KQL repo)
and the external Zomato MCP server, and exposes both as LangChain tools that the
LangGraph agent can call.
"""
from __future__ import annotations

import asyncio
import inspect
from contextlib import AsyncExitStack
from fnmatch import fnmatch
from functools import lru_cache

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from loguru import logger

from config import OAUTH_CLIENTS, OAUTH_SERVERS, TOOL_ALLOWLIST, load_mcp_server_config, settings
from mcp_oauth import build_oauth_provider
from reminders import build_local_tools

_client: MultiServerMCPClient | None = None
_session_stack: AsyncExitStack | None = None
_load_lock = asyncio.Lock()
# server name -> httpx.Auth running the MCP OAuth flow, for servers we connect
# to directly instead of through MultiServerMCPClient.
_oauth_providers: dict = {}

# Per-server load budget: enough for an npx cold-start/download, but bounded so a
# hanging/misconfigured server (e.g. one that never answers `initialize`) can't
# block the whole agent from starting.
_LOAD_TIMEOUT_SECONDS = int(settings.mcp_load_timeout_s)
# Longer budget for servers that may need interactive browser sign-in.
_CONSENT_TIMEOUT_SECONDS = 330


async def get_mcp_client() -> MultiServerMCPClient:
    """Lazily create (once) and return the shared MultiServerMCPClient."""
    global _client
    if _client is None:
        server_config = load_mcp_server_config()
        logger.info("Starting MCP servers: {}", list(server_config.keys()))
        _client = MultiServerMCPClient(server_config)
    return _client


async def get_agent_tools() -> list:
    """Returns the combined tool list for the LangGraph agent.

    Servers are loaded independently, and both failures and hangs are isolated:
    if one MCP server is misconfigured, unreachable, needs auth, or never
    responds, it's skipped (with a warning) instead of taking down every other
    tool -- and thus the whole agent. Tool names are discovered at runtime via
    each server's list_tools.
    """
    async with _load_lock:
        return await _load_agent_tools()


async def _load_agent_tools() -> list:
    global _client, _session_stack

    # Close any previous generation of sessions first. Reassigning _session_stack
    # without closing it orphaned every running MCP subprocess (npx filesystem,
    # the two Python KB servers, ...) -- they stayed alive holding stdio pipes
    # and the vector index, and only died when the backend did.
    if _session_stack is not None:
        try:
            await _session_stack.aclose()
        except Exception:  # noqa: BLE001 - best-effort cleanup of a stale generation
            logger.warning("Error closing previous MCP sessions; continuing")
        _session_stack = None

    server_config = load_mcp_server_config()
    _attach_oauth(server_config)
    _client = MultiServerMCPClient(server_config)
    _session_stack = AsyncExitStack()
    tools: list = []
    for name in server_config:
        server_stack = AsyncExitStack()
        # A server doing OAuth for the first time has to wait for a human to
        # click through a browser consent screen. 60s would abort that every
        # time and the server would look permanently broken.
        timeout = _CONSENT_TIMEOUT_SECONDS if name in OAUTH_SERVERS else _LOAD_TIMEOUT_SECONDS
        try:
            # asyncio.timeout(), NOT asyncio.wait_for().
            #
            # wait_for runs the awaitable in a NEW task. The MCP transports are
            # anyio-based and enter cancel scopes / task groups inside that
            # task, but the AsyncExitStack unwinds them later from the caller's
            # task -- and anyio rightly refuses: "Attempted to exit cancel scope
            # in a different task than it was entered in". That error then
            # masks whatever actually went wrong. asyncio.timeout() applies a
            # deadline to the CURRENT task instead, so enter and exit happen in
            # the same task and the real error survives.
            async with asyncio.timeout(timeout):
                if name in _oauth_providers:
                    session = await _open_oauth_session(server_stack, name, server_config[name])
                else:
                    session = await server_stack.enter_async_context(_client.session(name))
                server_tools = await load_mcp_tools(session)
            server_tools = _apply_allowlist(name, server_tools)
            _session_stack.push_async_callback(server_stack.aclose)
            tools.extend(server_tools)
            logger.info("Loaded {} tool(s) from MCP server '{}'", len(server_tools), name)
        except asyncio.TimeoutError:
            await _safe_close(server_stack, name)
            if name in OAUTH_SERVERS:
                logger.warning(
                    "Skipping MCP server '{}' -- no sign-in completed within {}s. "
                    "Restart the backend and finish the browser consent prompt.",
                    name, timeout,
                )
            else:
                logger.warning("Skipping MCP server '{}' -- timed out after {}s", name, timeout)
        except Exception as exc:  # noqa: BLE001 - one bad server must not break the rest
            await _safe_close(server_stack, name)
            logger.warning("Skipping MCP server '{}' -- failed to load:\n{}", name, _explain(exc))

    # Local, in-process tools (reminders) -- these need to reach the WebSocket
    # when they fire, which a stdio subprocess can't do.
    try:
        local = build_local_tools()
        tools.extend(local)
        logger.info("Loaded {} local tool(s): {}", len(local), [t.name for t in local])
    except Exception:  # noqa: BLE001
        logger.exception("Could not load local tools")

    if not tools:
        # Worth shouting about: the agent will still answer, but with no KB
        # access at all, so its answers become plausible-sounding guesses.
        logger.error(
            "NO MCP tools loaded -- every server failed or timed out. The agent "
            "has no knowledge-base or web access. Check Node.js is on PATH and "
            "see the warnings above."
        )
    else:
        logger.info("Total {} MCP tools available: {}", len(tools), [t.name for t in tools])
    return tools


@lru_cache(maxsize=1)
def _adapter_supports_auth() -> bool:
    """Whether this langchain-mcp-adapters build carries `auth` through to the
    streamable-http transport. If it doesn't, an OAuth-marked server just gets
    an anonymous request and a 401."""
    try:
        from langchain_mcp_adapters import sessions  # noqa: PLC0415
    except ImportError:
        return False
    for attr in dir(sessions):
        if "streamable" not in attr.lower():
            continue
        obj = getattr(sessions, attr)
        annotations = getattr(obj, "__annotations__", None) or {}
        if "auth" in annotations:
            return True
        if callable(obj):
            try:
                if "auth" in inspect.signature(obj).parameters:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _apply_allowlist(name: str, tools: list) -> list:
    """Keep only the tools matching this server's "only_tools" patterns.

    Patterns are fnmatch globs ("*message*"), because you usually don't know a
    server's exact generated tool names up front -- so when a filter is active
    we log every name the server offered. That log line is how you discover what
    to put in the allowlist.
    """
    patterns = TOOL_ALLOWLIST.get(name)
    if not patterns:
        return tools

    available = [t.name for t in tools]
    kept = [t for t in tools if any(fnmatch(t.name, p) for p in patterns)]

    if not kept:
        logger.error(
            "'{}': none of {} matched any tool. Available tools were:\n  {}\n"
            "Fix \"only_tools\" in mcp_config.json -- this server is contributing nothing.",
            name, patterns, "\n  ".join(available) or "(none)",
        )
    else:
        logger.info(
            "'{}': allowlist kept {}/{} tool(s): {} (available: {})",
            name, len(kept), len(available), [t.name for t in kept], available,
        )
    return kept


def _attach_oauth(server_config: dict) -> None:
    """Build an OAuth provider per OAuth-marked server.

    Stored in _oauth_providers rather than written into the connection dict:
    langchain-mcp-adapters' StreamableHttpConnection has no `auth` key, so an
    auth object placed there is silently discarded and the request goes out
    anonymously -- a bare 401 with no browser prompt, which looks like broken
    OAuth rather than a dropped credential. We open those sessions ourselves
    instead (see _open_oauth_session).
    """
    _oauth_providers.clear()
    for name in list(OAUTH_SERVERS):
        server = server_config.get(name)
        if not server or not server.get("url"):
            continue
        client_id, client_secret = OAUTH_CLIENTS.get(name, ("", ""))
        provider = build_oauth_provider(
            name,
            server["url"],
            scope=server.pop("oauth_scope", None),
            client_id=client_id,
            client_secret=client_secret,
        )
        if provider is not None:
            _oauth_providers[name] = provider
            how = "pre-registered client" if client_id else "dynamic registration"
            logger.info("OAuth enabled for MCP server '{}' ({})", name, how)


async def _open_oauth_session(stack: AsyncExitStack, name: str, server: dict):
    """Open a streamable-HTTP MCP session with OAuth, bypassing the adapter.

    The MCP SDK's own streamablehttp_client DOES accept `auth=` (verified via
    diagnose_mcp.py) -- it's only langchain-mcp-adapters' connection wrapper
    that can't carry it. So for these servers we skip the wrapper, build the
    transport directly, and hand the resulting session to load_mcp_tools, which
    only needs a ClientSession and doesn't care how it was created.
    """
    from mcp import ClientSession  # noqa: PLC0415
    from mcp.client.streamable_http import streamablehttp_client  # noqa: PLC0415

    provider = _oauth_providers[name]
    streams = await stack.enter_async_context(
        streamablehttp_client(
            server["url"],
            headers=server.get("headers"),
            auth=provider,
        )
    )
    read_stream, write_stream = streams[0], streams[1]  # 3rd item is get_session_id
    session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
    await session.initialize()
    return session


def _explain(exc: BaseException, depth: int = 0) -> str:
    """Flatten an exception into something a human can act on.

    The MCP stdio client runs its plumbing inside an anyio TaskGroup, so when a
    server subprocess dies the only thing that escapes is
    "unhandled errors in a TaskGroup (1 sub-exception)" -- which says nothing at
    all about what actually broke. ExceptionGroup carries the real errors in
    .exceptions; walk into them (and into __cause__ chains) so the log shows the
    underlying ImportError/FileNotFoundError/protocol error instead.
    """
    pad = "  " * depth
    lines = [f"{pad}{type(exc).__name__}: {exc}"]
    subs = getattr(exc, "exceptions", None)  # ExceptionGroup / BaseExceptionGroup
    if subs:
        for sub in subs:
            lines.append(_explain(sub, depth + 1))
    elif exc.__cause__ is not None:
        lines.append(f"{pad}  caused by:")
        lines.append(_explain(exc.__cause__, depth + 1))
    return "\n".join(lines)


async def _safe_close(stack: AsyncExitStack, name: str) -> None:
    """Close a stack without letting an unresponsive server hang startup.

    A server that timed out during `initialize` is quite likely to also hang on
    shutdown; an unbounded aclose() there would stall the whole agent load.
    """
    try:
        # Same reasoning as above: wait_for would close the stack from a
        # different task than the one that opened it, turning a tidy cleanup
        # into a cancel-scope RuntimeError.
        async with asyncio.timeout(10):
            await stack.aclose()
    except Exception:  # noqa: BLE001 - includes TimeoutError; CancelledError still propagates
        logger.warning("MCP server '{}' did not shut down cleanly; abandoning it", name)


async def close_mcp_client() -> None:
    global _client, _session_stack
    if _session_stack is not None:
        await _safe_close(_session_stack, "all")
        _session_stack = None
    _client = None
    _client = None
