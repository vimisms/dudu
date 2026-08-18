"""Diagnose why an MCP server won't load.

    .venv\\Scripts\\python.exe diagnose_mcp.py            # all servers
    .venv\\Scripts\\python.exe diagnose_mcp.py twilio     # just one

Why this exists: stdio MCP servers fail *invisibly*. The child process writes
its real error to stderr, which the MCP client doesn't surface, so the parent
only ever sees "BrokenResourceError: Connection lost" -- true, and useless. This
runs each server the same way the app does but keeps stderr, so you see the
actual message.

For HTTP servers it checks the two things that produce a bare 401:
whether your installed langchain-mcp-adapters can pass an `auth` object through
at all, and what the server says in its WWW-Authenticate header.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys

from config import OAUTH_SERVERS, load_mcp_server_config


def line(char: str = "-") -> None:
    print(char * 72)


def check_adapter_auth_support() -> bool:
    """Does this langchain-mcp-adapters build accept `auth` for streamable_http?

    This is the single most likely cause of an unexplained 401: if the adapter
    doesn't know the key, it drops it silently, the request goes out with no
    credentials, and the OAuth flow never runs -- which is why no browser opens.
    """
    print("\n[adapter] Checking OAuth pass-through support")
    try:
        from langchain_mcp_adapters import sessions  # noqa: PLC0415
    except ImportError as exc:
        print(f"  ! Could not import langchain_mcp_adapters.sessions: {exc}")
        return False

    supported = False
    for attr in dir(sessions):
        if "streamable" not in attr.lower():
            continue
        obj = getattr(sessions, attr)
        keys = getattr(obj, "__annotations__", None)
        if keys:
            has = "auth" in keys
            print(f"  {attr}: {'auth SUPPORTED' if has else 'no auth key'} ({len(keys)} keys)")
            supported = supported or has
        elif callable(obj):
            try:
                params = inspect.signature(obj).parameters
                if "auth" in params:
                    print(f"  {attr}(): accepts auth=")
                    supported = True
            except (TypeError, ValueError):
                pass

    try:
        from mcp.client.streamable_http import streamablehttp_client  # noqa: PLC0415

        has = "auth" in inspect.signature(streamablehttp_client).parameters
        print(f"  mcp.streamablehttp_client: {'accepts auth=' if has else 'NO auth param'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! mcp.client.streamable_http unavailable: {exc}")

    try:
        from mcp.client.auth import OAuthClientProvider  # noqa: PLC0415, F401

        print("  mcp.client.auth.OAuthClientProvider: present")
    except ImportError:
        print("  ! mcp.client.auth.OAuthClientProvider MISSING -- upgrade: pip install -U 'mcp>=1.9.2'")

    if not supported:
        print("  => Your adapter will DROP the auth object. That is the 401.")
        print("     Fix: pip install -U langchain-mcp-adapters")
    return supported


def probe_http(name: str, server: dict) -> None:
    url = server["url"]
    print(f"\n[{name}] HTTP probe -> {url}")
    try:
        import httpx  # noqa: PLC0415
    except ImportError:
        print("  ! httpx not installed; skipping")
        return
    try:
        resp = httpx.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
            timeout=15,
            follow_redirects=True,
        )
        print(f"  status: {resp.status_code}")
        auth_header = resp.headers.get("www-authenticate")
        if auth_header:
            print(f"  WWW-Authenticate: {auth_header}")
            print("  => Server advertises OAuth. DuDu's flow should handle this")
            print("     IF the adapter can pass `auth` through (see [adapter] above).")
        elif resp.status_code == 401:
            print("  => 401 with NO WWW-Authenticate header. The server wants")
            print("     credentials but doesn't advertise how -- OAuth discovery")
            print("     can't start. You likely need an account/allowlist first,")
            print("     or a different endpoint path.")
        body = resp.text[:400]
        if body.strip():
            print(f"  body: {body}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! request failed: {type(exc).__name__}: {exc}")


def probe_stdio(name: str, server: dict) -> None:
    cmd = [server["command"], *server.get("args", [])]
    printable = [("<auth>" if ":" in a and "/" in a else a) for a in cmd]
    print(f"\n[{name}] stdio probe")
    print(f"  command: {' '.join(printable)}")
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{'
            '"protocolVersion":"2024-11-05","capabilities":{},'
            '"clientInfo":{"name":"diagnose","version":"1"}}}\n',
            capture_output=True,
            text=True,
            timeout=90,
            env=server.get("env"),
            shell=(sys.platform == "win32"),  # npx is a .cmd on Windows
        )
    except subprocess.TimeoutExpired:
        print("  => Still running after 90s without answering initialize.")
        print("     Usually a cold npx download; try again once it's cached.")
        return
    except FileNotFoundError:
        print(f"  ! '{server['command']}' not found on PATH.")
        return

    print(f"  exit code: {proc.returncode}")
    if proc.stdout.strip():
        print(f"  stdout (first 300): {proc.stdout[:300]}")
    if proc.stderr.strip():
        print("  stderr — THIS IS THE REAL ERROR:")
        for ln in proc.stderr.strip().splitlines()[:25]:
            print(f"    {ln}")
    if proc.returncode != 0 and not proc.stderr.strip():
        print("  => Exited non-zero silently. Check the auth argument format.")


def main() -> None:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    config = load_mcp_server_config()

    line("=")
    print("DuDu MCP diagnostics")
    print(f"servers configured: {', '.join(config) or '(none)'}")
    print(f"oauth-marked: {', '.join(OAUTH_SERVERS) or '(none)'}")
    line("=")

    if any(n in OAUTH_SERVERS for n in config) and not only:
        check_adapter_auth_support()

    for name, server in config.items():
        if only and name != only:
            continue
        line()
        if server.get("url"):
            if name in OAUTH_SERVERS:
                check_adapter_auth_support()
            probe_http(name, server)
        else:
            probe_stdio(name, server)
    line("=")
    print("Done. Paste the output above if you want help reading it.")


if __name__ == "__main__":
    main()
