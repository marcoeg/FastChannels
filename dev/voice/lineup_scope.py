"""Scope the guide to the TV's own lineup (VOICE_TV=ytvn).

The guide (Minny) searches the operator's whole catalogue; the ytvn player
carries one subscriber's lineup (53 channels, from the ytvn MCP server's
viewer://lineup — the edge derives it from the account's lineup and
packages). Answering "what's on" from the catalogue names channels this TV
can't play. This module keeps the guide to the lineup, using nothing but the
two MCP servers:

- map: each lineup channel → its guide channel id, via minny find_channels on
  the lineup title. Exact title match first ("Muxley KTVU-DT"), then the call
  sign — the catalogue carries several feeds of the same station ("[MN] KTVU
  FOX2", "[MNSSL] KTVU HDTV", …), and the one named like the lineup row is
  the channel the player plays. Cached for an hour.
- what's on now with no channel: one whats_on_now per lineup channel, in
  parallel (measured 0.4 s for 53), instead of the catalogue-wide top 25.
- every other guide result naming channels: rows on channels outside the
  lineup are dropped.

Search results are filtered after the fact, so a broad schedule search that
the catalogue ranks mostly off-lineup can come back thin; the what's-on case,
the one that matters most, is complete by construction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.toolsets import WrapperToolset

import player_resolve as pr

logger = logging.getLogger("voice.lineup")

MAP_TTL_S = 3600
# Guide tools whose results carry channels, and the row key holding the id.
SCOPED = {"minny_whats_on_now": "channel_id",
          "minny_search_live_schedule": "channel_id",
          "minny_search_catchup": "channel_id",
          "minny_find_channels": "id"}


def _rows(result: Any) -> list | None:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return None
    return result if isinstance(result, list) else None


class LineupScope:
    def __init__(self, minny) -> None:
        self.minny = minny          # the raw (unprefixed) minny MCPToolset
        self._map: dict[str, pr.Channel] = {}   # guide channel id → lineup row
        self._names: dict[str, str] = {}        # guide channel id → guide name
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def _match(self, ch: pr.Channel) -> tuple[str, str] | None:
        try:
            hits = _rows(await self.minny.direct_call_tool(
                "find_channels", {"query": ch.title or ch.name})) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("lineup map: %s: %s", ch.name, e)
            return None
        for test in (lambda h: h.get("name") == ch.title,
                     lambda h: (h.get("call_sign") or "").lower() == ch.name.lower(),
                     lambda h: len(hits) == 1):
            found = [h for h in hits if test(h)]
            if found:
                return found[0]["id"], found[0].get("name") or ch.title
        return None

    async def channels(self) -> dict[str, pr.Channel]:
        async with self._lock:
            if self._map and time.monotonic() - self._at < MAP_TTL_S:
                return self._map
            lineup = await pr.lineup()
            if not lineup:
                return self._map            # stale beats nothing
            matches = await asyncio.gather(*(self._match(c) for c in lineup))
            m, names = {}, {}
            for ch, hit in zip(lineup, matches):
                if hit:
                    m[hit[0]] = ch
                    names[hit[0]] = hit[1]
            missing = [c.name for c, h in zip(lineup, matches) if not h]
            logger.info("lineup map: %d/%d channels in the guide%s", len(m),
                        len(lineup), f"; missing {missing}" if missing else "")
            if m:
                self._map, self._names, self._at = m, names, time.monotonic()
            return self._map

    def _label(self, row: dict, ch: pr.Channel) -> dict:
        row = dict(row)
        row["channel_name"] = ch.name
        row["channel_number"] = ch.number
        return row

    async def whats_on_now(self) -> list | str:
        chans = await self.channels()
        if not chans:
            return "This TV's lineup is unavailable right now."

        async def one(gid: str) -> list:
            try:
                rows = _rows(await self.minny.direct_call_tool(
                    "whats_on_now", {"channel": self._names[gid]})) or []
            except Exception as e:  # noqa: BLE001
                logger.warning("whats_on_now %s: %s", self._names[gid], e)
                return []
            return [self._label(r, chans[gid]) for r in rows
                    if r.get("channel_id") == gid]

        per = await asyncio.gather(*(one(g) for g in chans))
        rows = [r for rs in per for r in rs]
        rows.sort(key=lambda r: r.get("channel_number") or 0)
        return rows

    async def filter(self, name: str, result: Any) -> Any:
        rows = _rows(result)
        chans = await self.channels()
        if rows is None or not chans:
            return result
        key = SCOPED[name]
        kept = [self._label(r, chans[r[key]]) if key == "channel_id" else r
                for r in rows if isinstance(r, dict) and r.get(key) in chans]
        if rows and not kept:
            return ("Nothing on this TV's channels matches. (Channels this TV "
                    "doesn't carry may have it — don't offer those.)")
        return kept


@dataclass
class LineupScopedToolset(WrapperToolset):
    """The minny guide toolset, answering only for the TV's lineup."""

    scope: LineupScope | None = field(default=None)

    async def call_tool(self, name, tool_args, ctx, tool):
        if name == "minny_whats_on_now" and not tool_args.get("channel"):
            return await self.scope.whats_on_now()
        result = await super().call_tool(name, tool_args, ctx, tool)
        if name in SCOPED:
            return await self.scope.filter(name, result)
        return result
