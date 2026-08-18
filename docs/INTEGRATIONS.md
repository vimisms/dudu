# Personal-assistant integrations — what actually exists (Aug 2026)

Researched against DuDu's current setup. Sorted by *real, verified availability*
rather than by how useful they'd be if they existed — several obvious candidates
(MakeMyTrip, Practo, Zomato) turn out to have no usable public route.

Already wired in DuDu: QuickCommerce (Blinkit/Zepto/Instamart/BigBasket/…),
SerpAPI (google_search / google_local / google_shopping), Twilio (disabled),
Zomato (unverified), ICM (OAuth, in progress).

---

## Tier 1 — Official, real, worth doing

### Swiggy Builders Club MCP ★ biggest win
India-first MCP platform, **35 tools across 3 servers**:

| Server | Tools | What it covers |
|---|---|---|
| Food | 14 | Restaurant discovery, menus with variants/add-ons, **ordering**, live tracking, 500k+ restaurants |
| Instamart | 13 | Quick-commerce grocery, 50k+ SKUs, 1,000+ cities |
| Dineout | 8 | **Table reservations**, 50+ cities, availability + deals |

- Auth: **OAuth 2.1 + PKCE** — the same flow I just built for ICM, so DuDu can
  already do this.
- Access: free to build against locally; **production credentials require
  submitting a recorded demo video** for approval. Not instant, not a waitlist.
- Why it matters: this is the *official* replacement for the unverified Zomato
  entry in `mcp_config.json`, and it does ordering and reservations, not just
  search. It also overlaps QuickCommerce on groceries.

### Google Calendar
In the Claude connector directory. `create_event`, `list_events`,
`search_events`, `suggest_time`, `respond_to_event`. For a day-to-day assistant
this is probably higher value than any food/travel integration — "what's on
today", "move my 3pm", "book an hour to write the ICM RCA".

### Travel search — Expedia / Booking.com / lastminute.com
All in the connector directory, all **authless** (no account, no key):
- Expedia: `search_flights`, `search_hotels`, `get_hotel_pdp_offers`
- Booking.com: `accommodations_search`
- lastminute.com: `search_flights`

Global inventory rather than India-optimised, and they search rather than book —
but they need nothing from you, which makes them the cheapest thing on this list.

---

## Tier 2 — Exists, but with a catch worth knowing

### Uber — official MCP, but not useful here
`https://mcp.uber.com/claude/rides-3p/mcp`, OAuth sign-in. Two dealbreakers:
**it only estimates fares/ETAs — it cannot book a ride**, and coverage is
**United States only**. Booking is explicitly handed off to the Uber app.
Not worth wiring for an India-based user today.

### Twilio — already configured, currently disabled
Your `.env` disables it with a good reason recorded: the key returns 401 *and*
the server exposes ~197 tools. That second point is the real problem — see
"Tool-count tax" below. If you want calling/SMS, this needs a valid API key
**and** a tool filter.

**Correction on DLT, verified by testing:** a US Twilio long code
(+1 737…) delivered successfully to an Indian mobile (+91…) with no DLT
registration. TRAI's DLT regime governs A2P traffic sent over **domestic Indian
routes / registered sender IDs**; international long-code SMS reaches Indian
handsets without it. Caveats that still apply: international routes cost more,
are subject to carrier filtering (delivery is not guaranteed and can degrade
without notice), and sender ID is not preserved. For personal, low-volume use
this is fine — my earlier "you must register with DLT first" was too strong.

If you later need reliable, high-volume Indian delivery, **Exotel / Plivo /
Ozonetel** are the local options and they do require DLT. None ships an official
MCP server, so that would be a thin custom wrapper like `serpapi_server.py` — a
couple of hours, not a research project.

---

## Tier 3 — No usable public route (don't waste time)

### MakeMyTrip
No public developer API and no MCP server. There's an affiliate programme, not a
booking API. **Use Expedia/Booking.com MCP or the existing SerpAPI for travel
lookups instead.**

### Practo
There *is* a "Practo API Program", but it's a **B2B partner programme** — the
terms are written around an entity that has already signed an agreement, with
roles for "Partner", "Healthcare Provider" and "Customer". No self-serve
individual signup is documented; you'd have to negotiate access as a business.
Not realistic for a personal assistant.

**You already have a working substitute:** SerpAPI's `google_local` finds
doctors/clinics near a location with address, phone and rating — which covers
"find me a dermatologist nearby" without any new integration at all.

### Zomato
No official public MCP. The entry in `mcp_config.json` points at
`https://mcp-server.zomato.com/mcp`, which was never verified and is disabled
without a key anyway. Third-party Zomato "MCP servers" on Apify and LobeHub are
**scrapers** — brittle and against Zomato's terms. Swiggy's official MCP covers
the same need legitimately.

---

## WhatsApp — two paths, very different risk

| | Official (Meta Cloud API) | Unofficial bridges (`whatsapp-mcp` etc.) |
|---|---|---|
| Access to your **personal** chats | **No** | Yes |
| What it does | Business messaging from a business number | Links as a WhatsApp Web device |
| Requirements | Meta Business account, verified number, message templates for outbound, 24h reply window | Scan a QR code |
| Risk | None | **Violates WhatsApp ToS; accounts do get banned** |

The honest summary: if you want DuDu to read and reply to your *own* WhatsApp,
the only technical route is an unofficial bridge, and it puts your personal
number at risk. If a business number sending templated messages is enough, the
Cloud API is safe and free at low volume. I'd want you to pick deliberately
rather than have me choose.

---

## The tool-count tax — read before adding anything

Every MCP tool's name, description and JSON schema is sent to the model on
**every single request**. Your own `.env` already notes Twilio's ~197 tools
"bloats the agent". That cost is real and it compounds with the latency you've
been chasing:

- More tools = a larger prompt on every call = slower and more expensive.
- More tools = worse tool *selection*, because the model is choosing from a
  bigger, noisier menu.

DuDu currently loads ~30-40 tools. Swiggy alone would add 35. Before we go past
roughly 60, it's worth adding a per-server tool allowlist in
`mcp_config.json` — `"only_tools": ["search_restaurants", "place_order"]` —
filtered in `mcp_clients.get_agent_tools()`. Cheap to build, and it makes
Twilio usable (2 tools instead of 197).

---

## Sources

- [Swiggy Builders Club docs](https://mcp.swiggy.com/builders/docs/)
- [Swiggy Builders Club announcement](https://press.aboutamazon.com/aws/2026/4/swiggy-to-launch-builders-club-giving-developers-and-enterprises-access-to-its-ai-commerce-stack)
- [Uber official remote MCP server](https://mcpservers.org/remote-mcp-servers/uber)
- [Practo API programme terms](https://help.practo.com/partner-api/practo-api-program-terms-and-conditions/)
- [Practo developer portal](https://developers.practo.com/)
- [WhatsApp MCP server comparison](https://blueticks.co/blog/best-whatsapp-mcp-servers)
- [Official vs unofficial WhatsApp APIs](https://whapi.cloud/whatsapp-business-api-to-choose)
- [Voice API India buyer's guide](https://frejun.com/voice-api-india/)
- [Plivo vs Exotel vs Twilio India](https://caller.digital/blog/telephony-partner-voice-ai-india-plivo-exotel-ozonetel-knowlarity-twilio-2026)
