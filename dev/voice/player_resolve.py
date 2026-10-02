"""Resolve what the viewer asked for into ytvn player ids (VOICE_TV=ytvn).

The guide (Minny MCP) answers with the catalogue's own ids, which the ytvn
player does not know; the player only takes edge content ids. Rather than
trust a crosswalk, resolve against the player's own catalogue:

  channel   name / call sign / network / number → the player's lineup, the
            ytvn MCP server's viewer://lineup resource (53 channels, cached)
  title     on-demand title (+ year) → the edge VOD search → ASSET contentId.
            TEMPORARY: over HTTP on the ytvn proxy until the MCP server
            offers a viewer://titles resource; then that replaces _get.

The MCP server is meant to be the agent's only path to the player (its
remote-control WebSocket carries the commands; the proxy's /api relay has
no remote-control auth). Matching stays here: it is about understanding
speech, not about the player.

Catch-up and restart (an airing id = channel contentId + start epoch) wait
for a play_airing command in the player.

Never raises: failures come back as None / [] with a logged reason, so the
calling tool can answer honestly instead of breaking the turn.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger("voice.resolve")

PROXY = os.environ.get("YTVN_PROXY", "http://127.0.0.1:8787").rstrip("/")
TIMEOUT_S = 4.0
LINEUP_TTL_S = 300

# Network names viewers say for the Bay Area OTA stations in this lineup
# (the Muxley headend). Only affiliations that are certain.
NETWORKS = {"fox": "2581", "cbs": "2583", "pbs": "2585", "nbc": "2586",
            "telemundo": "2589", "ion": "2591"}

# Words in lineup titles that viewers never say ("Muxley KTVU-DT").
_NOISE = {"muxley", "dt", "dt2", "hd", "hdtv", "tv", "the", "channel"}


@dataclass(frozen=True)
class Channel:
    channel_id: str
    number: int | None
    name: str          # call sign
    title: str

    def row(self) -> dict:
        return {"channel_id": self.channel_id, "number": self.number,
                "name": self.name}


@dataclass(frozen=True)
class Title:
    asset_id: str
    title: str
    year: int | None

    def row(self) -> dict:
        return {"asset_id": self.asset_id, "title": self.title,
                "year": self.year}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _key(text: str) -> str:
    """Compact form for call signs: 'K T V U', 'ktvu-dt' → 'ktvu'."""
    words = [w for w in _norm(text).split() if w not in _NOISE]
    return "".join(words)


async def _get(path: str, params: dict) -> dict | None:
    url = f"{PROXY}{path}?{urlencode(params)}"
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(url, headers={"Accept": "application/json"}) as r:
                if r.status != 200:
                    logger.warning("ytvn proxy %s → HTTP %s", path, r.status)
                    return None
                return await r.json(content_type=None)
    except Exception as e:  # noqa: BLE001 — proxy down / VPN / timeout
        logger.warning("ytvn proxy %s unreachable: %s", path, e)
        return None


_lineup: dict = {"at": 0.0, "channels": []}
# async (uri) -> resource contents; set by the agent to the ytvn MCP
# toolset's read_resource.
_read_resource: Callable[[str], Awaitable[object]] | None = None


def use_mcp(read_resource: Callable[[str], Awaitable[object]]) -> None:
    global _read_resource
    _read_resource = read_resource


async def lineup() -> list[Channel]:
    if time.monotonic() - _lineup["at"] < LINEUP_TTL_S and _lineup["channels"]:
        return _lineup["channels"]
    rows: list = []
    if _read_resource is None:
        logger.warning("ytvn lineup: no MCP server configured")
    else:
        try:
            raw = await _read_resource("viewer://lineup")
            rows = json.loads(raw if isinstance(raw, str) else str(raw))
        except Exception as e:  # noqa: BLE001 — server down / proxy down
            logger.warning("ytvn lineup unavailable: %s", e)
    chans = [Channel(str(r["channel_id"]), r.get("number"),
                     r.get("name") or r.get("title") or "",
                     r.get("title") or "")
             for r in rows if isinstance(r, dict) and r.get("channel_id")]
    if chans:
        _lineup.update(at=time.monotonic(), channels=chans)
    return chans or _lineup["channels"]   # stale beats nothing


async def find_channel(query: str) -> tuple[Channel | None, list[Channel]]:
    """(the channel, []) on a confident match; (None, candidates) otherwise.
    Candidates may be empty: not in this lineup (or the lineup is unreachable).
    """
    chans = await lineup()
    q = query.strip()
    if not chans or not q:
        return None, []

    digits = re.fullmatch(r"(?:channel\s*)?(\d{1,4})", q, re.I)
    if digits:
        n = int(digits.group(1))
        hit = [c for c in chans if c.number == n]
        return (hit[0], []) if hit else (None, [])

    k, nq = _key(q), _norm(q)
    if nq in NETWORKS or k in NETWORKS:
        cid = NETWORKS.get(nq) or NETWORKS[k]
        hit = [c for c in chans if c.channel_id == cid]
        if hit:
            return hit[0], []

    # Exact call sign or title, ignoring spacing and "-DT"/"Muxley" noise.
    exact = [c for c in chans if k and k in (_key(c.name), _key(c.title))]
    if len(exact) == 1:
        return exact[0], []
    if exact:
        return None, exact

    # Every spoken word inside the name ("gusto" → Gusto TV, Gusto Espanol).
    words = [w for w in nq.split() if w not in _NOISE]
    contains = [c for c in chans
                if words and all(w in _norm(f"{c.name} {c.title}").split()
                                 or w in _key(c.name) for w in words)]
    if len(contains) == 1:
        return contains[0], []
    if contains:
        return None, contains[:6]

    # Mis-heard names ("Drone TV" → DroneTV is exact; "Surfing Cinema" → Surf Cinema).
    by_key = {_key(c.name): c for c in chans}
    close = difflib.get_close_matches(k, list(by_key), n=3, cutoff=0.82)
    if len(close) == 1:
        return by_key[close[0]], []
    return None, [by_key[c] for c in close]


async def search_titles(query: str, year: int | None = None,
                        limit: int = 8) -> list[Title]:
    d = await _get("/api/content/search/vod",
                   {"searchText": query, "page": 0, "size": 20})
    rows = ((d or {}).get("contents") or {}).get("content") or []
    titles = [Title(str(r["contentId"]), r.get("title") or "",
                    r.get("releasedYear"))
              for r in rows
              if r.get("contentId") and r.get("contentType", "ASSET") == "ASSET"]
    # The edge search also matches descriptions and credits ("trace" finds
    # Onward); a viewer asking for a title says words of the title.
    words = [w for w in _norm(query).split() if len(w) > 1]
    titles = [t for t in titles
              if all(w in _norm(t.title).split() for w in words)]
    if year:
        titles = [t for t in titles if t.year in (year - 1, year, year + 1)]
    return titles[:limit]


def pick_title(query: str, titles: list[Title]) -> Title | None:
    """One title to play without asking: the only result, or the only one
    whose title is exactly what was asked for (with a single id)."""
    if len(titles) == 1:
        return titles[0]
    exact = [t for t in titles if _norm(t.title) == _norm(query)]
    return exact[0] if len(exact) == 1 else None
