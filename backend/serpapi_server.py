"""Local MCP server wrapping SerpAPI (https://serpapi.com) so the agent can do
real Google searches: general web search, local places (doctors, restaurants),
and shopping (products/prices).

Auth: SERPAPI_API_KEY. Results are trimmed to the useful fields and returned as
JSON; the agent formats them into readable markdown for the results window.
"""
import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = "https://serpapi.com/search.json"
API_KEY = os.environ.get("SERPAPI_API_KEY", "")

mcp = FastMCP("serpapi")


def _call(params: dict) -> dict:
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    clean["api_key"] = API_KEY
    try:
        r = httpx.get(BASE_URL, params=clean, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"request failed: {exc}"}
    if r.status_code == 401:
        return {"error": "401 invalid SERPAPI_API_KEY"}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    try:
        return r.json()
    except ValueError:
        return {"error": "non-JSON response from SerpAPI"}


@mcp.tool()
def google_search(query: str, location: str = "", num: int = 8) -> str:
    """General Google web search for any open-web lookup (facts, documentation,
    how-tos, news, companies). Returns the top results (title, link, snippet)
    plus Google's answer box / knowledge panel when present. Optionally bias by
    location (e.g. 'Bangalore, India')."""
    data = _call({"engine": "google", "q": query, "location": location, "num": num})
    if "error" in data:
        return f"Search error: {data['error']}"
    out: dict = {"query": query}
    if data.get("answer_box"):
        ab = data["answer_box"]
        out["answer_box"] = {k: ab.get(k) for k in ("title", "answer", "snippet") if ab.get(k)}
    if data.get("knowledge_graph"):
        kg = data["knowledge_graph"]
        out["knowledge_graph"] = {k: kg.get(k) for k in ("title", "type", "description", "website") if kg.get(k)}
    out["results"] = [
        {k: it.get(k) for k in ("title", "link", "snippet", "source", "date") if it.get(k)}
        for it in (data.get("organic_results") or [])[:num]
    ]
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def google_local(query: str, location: str = "") -> str:
    """Find local businesses / places -- e.g. 'eye doctors near Koramangala',
    'dentists', 'restaurants'. Returns name, address, phone, rating, review
    count, type, hours and website. Pass a location for best results."""
    data = _call({"engine": "google_local", "q": query, "location": location})
    if "error" in data:
        return f"Local search error: {data['error']}"
    places = [
        {k: it.get(k) for k in ("title", "address", "phone", "rating", "reviews", "type", "hours", "website")
         if it.get(k) is not None}
        for it in (data.get("local_results") or [])[:12]
    ]
    return json.dumps({"query": query, "location": location, "places": places}, ensure_ascii=False)


@mcp.tool()
def google_shopping(query: str, location: str = "") -> str:
    """Find products for sale across the web with prices/sellers -- e.g.
    'noise cancelling headphones', 'ergonomic office chair'. Returns title,
    price, seller/source, rating, review count and link."""
    data = _call({"engine": "google_shopping", "q": query, "location": location})
    if "error" in data:
        return f"Shopping search error: {data['error']}"
    products = [
        {k: it.get(k) for k in ("title", "price", "source", "rating", "reviews", "link", "delivery")
         if it.get(k) is not None}
        for it in (data.get("shopping_results") or [])[:12]
    ]
    return json.dumps({"query": query, "products": products}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
