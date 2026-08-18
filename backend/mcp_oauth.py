"""OAuth 2.0 for remote MCP servers -- the flow VS Code / GitHub Copilot runs.

Your .vscode/mcp.json declares the ICM server with a URL and nothing else:

    "my-icm-mcp-server-c03f0519": {
        "url": "https://icm-mcp-prod.azure-api.net/v1/",
        "type": "http"
    }

No key, no header, no token. That is not an omission -- it means the client is
expected to perform the MCP specification's OAuth 2.0 authorization flow:

  1. Call the server; get 401 with a WWW-Authenticate header.
  2. Discover the authorization server's metadata from that header.
  3. Dynamically register this app as an OAuth client (RFC 7591).
  4. Open the user's browser for consent, catch the redirect on localhost.
  5. Exchange the code for an access token; refresh it as needed.

That is exactly why Copilot "asks for authentication" and DuDu previously just
got a silent 401 and dropped the server. The MCP Python SDK ships this flow as
`mcp.client.auth.OAuthClientProvider`, which is an httpx.Auth -- so it can be
handed straight to the streamable-http transport.

Tokens are cached under backend/.oauth_tokens/ so consent is a one-time step
per server rather than something you redo on every restart.
"""
from __future__ import annotations

import asyncio
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from loguru import logger

TOKEN_DIR = Path(__file__).resolve().parent / ".oauth_tokens"
CALLBACK_BIND = "127.0.0.1"  # what the local server listens on
# What we REGISTER as the redirect URI. Providers match this string exactly and
# several (Swiggy among them) whitelist "http://localhost" specifically -- and
# "127.0.0.1" is a different string even though it resolves to the same place.
CALLBACK_HOST = "localhost"
CALLBACK_PORT = 8757  # distinct from the app's 8756
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"
CONSENT_TIMEOUT_S = 300  # you have 5 minutes to finish the browser prompt

_SUCCESS_PAGE = b"""<!doctype html><html><head><meta charset="utf-8">
<title>DuDu - signed in</title></head>
<body style="font-family:system-ui;text-align:center;padding-top:14vh">
<h2 style="color:#1d6b3f">Signed in</h2>
<p>DuDu now has access to this MCP server.<br>You can close this tab.</p>
</body></html>"""


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


class FileTokenStorage:
    """Persists the OAuth token + dynamic client registration to disk.

    Implements mcp.client.auth.TokenStorage structurally (the SDK only needs
    these four coroutines). Without persistence you'd re-consent in a browser
    every single time the backend restarts.
    """

    def __init__(
        self,
        server_name: str,
        preset_client_id: str = "",
        preset_client_secret: str = "",
    ) -> None:
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        self._tokens = TOKEN_DIR / f"{_slug(server_name)}.tokens.json"
        self._client = TOKEN_DIR / f"{_slug(server_name)}.client.json"
        self._name = server_name
        # A pre-registered client, for servers that DON'T support Dynamic Client
        # Registration. Returning client info here makes the SDK skip the
        # registration call entirely -- which is the fix for Azure-fronted
        # servers that answer the registration endpoint with 404.
        self._preset_client_id = preset_client_id
        self._preset_client_secret = preset_client_secret

    @staticmethod
    def _read(path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Discarding unreadable OAuth cache at {}", path)
            return None

    @staticmethod
    def _write(path: Path, payload) -> None:
        try:
            path.write_text(
                payload.model_dump_json(indent=2)
                if hasattr(payload, "model_dump_json")
                else json.dumps(payload, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:  # noqa: BLE001
            logger.warning("Could not persist OAuth data to {}: {}", path, exc)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken  # noqa: PLC0415

        data = self._read(self._tokens)
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens) -> None:
        self._write(self._tokens, tokens)
        logger.info("Stored OAuth token for '{}'", self._name)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull  # noqa: PLC0415

        data = self._read(self._client)
        if data:
            return OAuthClientInformationFull.model_validate(data)
        if self._preset_client_id:
            logger.info(
                "Using the pre-registered OAuth client for '{}' (skipping dynamic registration)",
                self._name,
            )
            info = {
                "client_id": self._preset_client_id,
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": (
                    "client_secret_post" if self._preset_client_secret else "none"
                ),
            }
            if self._preset_client_secret:
                info["client_secret"] = self._preset_client_secret
            return OAuthClientInformationFull.model_validate(info)
        return None

    async def set_client_info(self, info) -> None:
        self._write(self._client, info)


# Served to any request that ISN'T the callback. If the provider ever returns
# its parameters in the URL fragment (invisible to a server), the script hands
# them back as a query string so the flow can still complete.
_WAITING_PAGE = b"""<!doctype html><html><head><meta charset="utf-8">
<title>DuDu - waiting</title></head>
<body style="font-family:system-ui;text-align:center;padding-top:14vh">
<p>Waiting for sign-in to complete...</p>
<script>
  if (window.location.hash && window.location.hash.length > 1) {
    window.location.replace("/callback?" + window.location.hash.substring(1));
  }
</script>
</body></html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        has_code = bool(params.get("code"))
        has_error = bool(params.get("error"))

        # Only a request actually carrying the authorization result counts.
        #
        # This handler used to accept the FIRST request of any kind, which meant
        # a browser's automatic /favicon.ico fetch -- or any probe, prefetch or
        # stray reload -- could land first and be recorded as "a redirect with
        # no code", aborting a sign-in that was otherwise about to succeed.
        if not (has_code or has_error):
            logger.debug("Ignoring non-callback request to {}", parsed.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_WAITING_PAGE)
            return

        _CallbackHandler.result = {
            "code": (params.get("code") or [None])[0],
            "state": (params.get("state") or [None])[0],
            "error": (params.get("error") or [None])[0],
        }
        logger.info(
            "OAuth callback received on {} ({})",
            parsed.path,
            "error" if has_error else "authorization code",
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_SUCCESS_PAGE)

    def log_message(self, *args) -> None:  # silence per-request stderr spam
        return


async def _await_callback() -> tuple[str, str | None]:
    """Run a one-shot localhost server and wait for the OAuth redirect."""
    _CallbackHandler.result = {}
    server = HTTPServer((CALLBACK_BIND, CALLBACK_PORT), _CallbackHandler)
    # serve_forever in a thread; the handler sets .result, we poll for it.
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="oauth-callback")
    thread.start()
    try:
        for _ in range(CONSENT_TIMEOUT_S * 10):
            if _CallbackHandler.result:
                break
            await asyncio.sleep(0.1)
    finally:
        server.shutdown()
        server.server_close()

    result = _CallbackHandler.result
    if not result:
        raise TimeoutError(
            f"No OAuth redirect received within {CONSENT_TIMEOUT_S}s. If you did "
            f"sign in, check the provider is redirecting to exactly {REDIRECT_URI}."
        )
    if result.get("error"):
        raise RuntimeError(f"Authorization was refused: {result['error']}")
    if not result.get("code"):
        # Can't normally happen now that non-callback requests are ignored, but
        # keep the guard so a malformed redirect fails loudly rather than
        # handing an empty code to the token exchange.
        raise RuntimeError("OAuth redirect arrived without an authorization code")
    return result["code"], result.get("state")


async def _open_browser(url: str) -> None:
    logger.warning(
        "\n"
        "=================================================================\n"
        " DuDu needs you to sign in to an MCP server.\n"
        " A browser tab should have opened. If not, paste this URL:\n\n"
        " {}\n"
        "=================================================================",
        url,
    )
    # Never let a headless/odd environment turn "couldn't launch a browser"
    # into a hang -- the URL is printed above either way.
    await asyncio.to_thread(lambda: webbrowser.open(url))


def build_oauth_provider(
    server_name: str,
    server_url: str,
    scope: str | None = None,
    client_id: str = "",
    client_secret: str = "",
):
    """Return an httpx.Auth implementing the MCP OAuth flow, or None if this
    MCP SDK build doesn't ship one.

    Pass client_id when the server does NOT support Dynamic Client Registration
    (Azure/Entra-fronted servers typically don't -- their registration endpoint
    answers 404 and the flow dies before a browser ever opens). With a
    pre-registered client the SDK skips registration and goes straight to
    authorization.
    """
    try:
        from mcp.client.auth import OAuthClientProvider  # noqa: PLC0415
        from mcp.shared.auth import OAuthClientMetadata  # noqa: PLC0415
    except ImportError as exc:  # noqa: BLE001
        logger.warning(
            "This 'mcp' package has no OAuth client ({}). Upgrade it "
            "(pip install -U 'mcp>=1.9.2') to authenticate to remote MCP servers.",
            exc,
        )
        return None

    metadata = {
        "client_name": "DuDu Assistant",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }
    if scope:
        metadata["scope"] = scope

    try:
        return OAuthClientProvider(
            server_url=server_url,
            client_metadata=OAuthClientMetadata.model_validate(metadata),
            storage=FileTokenStorage(server_name, client_id, client_secret),
            redirect_handler=_open_browser,
            callback_handler=_await_callback,
        )
    except Exception as exc:  # noqa: BLE001 - never let auth setup kill startup
        logger.warning("Could not build an OAuth provider for '{}': {}", server_name, exc)
        return None
