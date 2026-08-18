"""Local MCP server wrapping the QuickCommerce REST API.

The hosted QuickCommerce MCP endpoint (api.quickcommerceapi.com/mcp) doesn't
interoperate with langchain-mcp-adapters' streamable-http client (session
handshake fails), but the plain REST API works fine -- so we expose the same 7
capabilities as a local stdio MCP server instead, the same pattern as
kb_search_server.py / rag_search_server.py.

Auth: QUICKCOMMERCE_API_KEY (sent as the X-API-Key header). Most tools need a
lat/lon; some quick-commerce platforms also need a pincode.
"""
import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = "https://api.quickcommerceapi.com"
API_KEY = os.environ.get("QUICKCOMMERCE_API_KEY", "")

mcp = FastMCP("quickcommerce")


def _get(path: str, params: dict) -> str:
    """GET a QuickCommerce endpoint and return the JSON body (or an error string)."""
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    try:
        r = httpx.get(
            f"{BASE_URL}{path}",
            params=clean,
            headers={"X-API-Key": API_KEY},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return f"QuickCommerce request failed: {exc}"

    if r.status_code == 200:
        try:
            return json.dumps(r.json(), ensure_ascii=False)
        except ValueError:
            return r.text
    if r.status_code == 401:
        return "QuickCommerce error 401: invalid or missing API key."
    if r.status_code == 402:
        return "QuickCommerce error 402: no credits remaining."
    if r.status_code == 422:
        return f"QuickCommerce error 422: invalid parameters -- {r.text[:200]}"
    return f"QuickCommerce error {r.status_code}: {r.text[:200]}"


@mcp.tool()
def search_products(q: str, lat: float, lon: float, platform: str, pincode: str = "") -> str:
    """Search products by keyword on one platform at a location. Returns names,
    prices, ratings, inventory, images and deeplinks. platform is one of
    BlinkIt, Zepto, Swiggy, BigBasket, DMart, JioMart, Minutes, Amazon, Nykaa,
    Myntra, Flipkart. pincode is required for DMart/JioMart/Minutes."""
    return _get("/v1/search", {"q": q, "lat": lat, "lon": lon, "platform": platform, "pincode": pincode})


@mcp.tool()
def get_item_details(item_id: str, lat: float, lon: float, platform: str, pincode: str = "") -> str:
    """Get real-time price, stock and availability for a specific item id (from
    search results) on a platform at a location."""
    return _get("/v1/item", {"item_id": item_id, "lat": lat, "lon": lon, "platform": platform, "pincode": pincode})


@mcp.tool()
def check_delivery_eta(lat: float, lon: float, platform: str, pincode: str = "") -> str:
    """Get estimated delivery time and store availability for a quick-commerce
    platform (BlinkIt, Zepto, Swiggy, BigBasket, DMart, JioMart, Minutes) at a
    location. ETA is not available for Amazon/Nykaa/Myntra/Flipkart."""
    return _get("/v1/eta", {"lat": lat, "lon": lon, "platform": platform, "pincode": pincode})


@mcp.tool()
def group_search(q: str, lat: float, lon: float, platforms: str, pincode: str = "") -> str:
    """Search a product across multiple platforms in one call for a side-by-side
    price/availability compare. platforms is comma-separated (e.g.
    'BlinkIt,Zepto,Swiggy'). Costs 1 credit per platform."""
    return _get("/v1/groupsearch", {"q": q, "lat": lat, "lon": lon, "platforms": platforms, "pincode": pincode})


@mcp.tool()
def group_eta(lat: float, lon: float, platforms: str, pincode: str = "") -> str:
    """Get delivery ETAs from multiple quick-commerce platforms at once (find who
    delivers fastest). platforms is comma-separated. Costs 1 credit per platform."""
    return _get("/v1/groupeta", {"lat": lat, "lon": lon, "platforms": platforms, "pincode": pincode})


@mcp.tool()
def check_credits() -> str:
    """Check remaining QuickCommerce API credits and usage. Free -- no credits consumed."""
    return _get("/v1/credits", {})


@mcp.tool()
def list_platforms() -> str:
    """List supported platforms for search/item and ETA. Free -- no key needed."""
    return _get("/v1/supported-platforms", {})


if __name__ == "__main__":
    mcp.run()
