"""Brave web search for programme background the EPG doesn't carry.

Guide descriptions are thin and often boilerplate; the web has cast, year,
plot context, reviews and real-world background. This is a native
in-process tool (no MCP hop): one HTTPS call, hard-capped, shaped for
speech — compact lines, no URLs, no markup.

Measured: ~0.5 s typical for the Brave web endpoint.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from urllib.parse import quote_plus

import aiohttp

logger = logging.getLogger("voice.websearch")

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
TIMEOUT_S = 4.0          # voice budget: degrade rather than stall a turn
MAX_RESULTS = 4
SNIPPET_CHARS = 220

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    # Brave marks matched terms with <strong> and HTML-escapes the rest;
    # both would be spoken literally by TTS.
    return _WS.sub(" ", html.unescape(_TAGS.sub("", text or ""))).strip()


async def search(query: str, *, count: int = MAX_RESULTS) -> str:
    """Return compact search results as plain lines, or an honest failure
    string. Never raises — a dead search must not break a voice turn."""
    key = os.environ.get("BRAVE_SEARCH_KEY")
    if not key:
        return "Web search is not configured."

    url = (f"{ENDPOINT}?q={quote_plus(query)}&count={count}"
           f"&result_filter=web&safesearch=moderate")
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}

    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(url, headers=headers) as resp:
                if resp.status == 429:
                    return "Web search is rate limited right now."
                if resp.status != 200:
                    logger.warning("brave search HTTP %s", resp.status)
                    return "I couldn't reach the web just now."
                data = await resp.json()
    except asyncio.TimeoutError:
        return "The web search took too long, so I skipped it."
    except Exception as e:  # noqa: BLE001
        logger.warning("brave search failed: %s", e)
        return "I couldn't reach the web just now."

    results = (data.get("web") or {}).get("results") or []
    if not results:
        return "Nothing useful came back from the web for that."

    lines = []
    for r in results[:count]:
        title = _clean(r.get("title", ""))
        desc = _clean(r.get("description", ""))[:SNIPPET_CHARS]
        age = r.get("page_age") or ""
        if not title:
            continue
        # No URLs on purpose: this text is spoken, and the voice rules
        # forbid reading out links.
        lines.append(f"- {title}: {desc}" + (f" ({age[:10]})" if age else ""))
    return "\n".join(lines) or "Nothing useful came back from the web for that."
