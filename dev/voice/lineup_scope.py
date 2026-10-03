"""Scope the guide to the TV's own lineup (VOICE_TV=ytvn).

The guide (Minny) searches the operator's whole catalogue; the ytvn player
carries one subscriber's lineup (53 channels, from the ytvn MCP server's
viewer://lineup — the edge derives it from the account's lineup and
packages). Answering "what's on" from the catalogue names channels this TV
can't play. This module keeps the guide to the lineup, using nothing but the
two MCP servers:

- map: each lineup channel → its guide channel ids. The primary one comes
  from minny find_channels on the lineup title (exact title match first,
  "Muxley KTVU-DT", then the call sign). The catalogue carries several feeds
  of the same station ("[MN] KTVU FOX2", "[MNSSL] KTVU HDTV", …) and its
  recordings (catch-up) can sit on any of them, so every guide channel that
  answers to the call sign AND is airing the identical programme right now
  (title, episode, start, end) as the primary is the same feed. A sister
  subchannel ("[MN] KTVU Movies!") airs something else and stays out.
  Cached for an hour.
- a `channel` argument the guide can't know — the TV's number ("channel 9")
  or call sign — is rewritten to a name the guide matches for that station.
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
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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


_TZ = ZoneInfo(os.environ.get("VOICE_LOCAL_TZ", "America/Los_Angeles"))


def local_time(iso: str, now: datetime | None = None) -> str | None:
    """'2026-10-02T23:00:00Z' → '4:00 PM today' in the viewer's zone. The
    guide speaks UTC; models converting it themselves got the offset wrong
    (UTC-8 vs -7 across DST) or left it in UTC, so the code does it."""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_TZ)
    except (TypeError, ValueError):
        return None
    now = (now or datetime.now(_TZ)).astimezone(_TZ)
    days = (t.date() - now.date()).days
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days) or t.strftime("%A %B %-d")
    return f"{t.strftime('%-I:%M %p')} {day}"


def _same_airing(a: dict, b: dict) -> bool:
    return all(a.get(k) == b.get(k)
               for k in ("title", "episode_title", "start", "end"))


class LineupScope:
    def __init__(self, minny) -> None:
        self.minny = minny          # the raw (unprefixed) minny MCPToolset
        self._map: dict[str, pr.Channel] = {}   # guide channel id → lineup row
        self._names: dict[str, str] = {}        # primary guide id → guide name
        self._query: dict[str, str] = {}        # lineup channel_id → guide name
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def _call(self, tool: str, args: dict) -> list:
        try:
            return _rows(await self.minny.direct_call_tool(tool, args)) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("lineup map: %s %s: %s", tool, args, e)
            return []

    async def _match(self, ch: pr.Channel) -> tuple[str, str, set[str], str] | None:
        """(primary guide id, its name, all same-feed guide ids, the name to
        pass as `channel` so the guide matches every feed)."""
        hits = await self._call("find_channels", {"query": ch.title or ch.name})
        primary = None
        for test in (lambda h: h.get("name") == ch.title,
                     lambda h: (h.get("call_sign") or "").lower() == ch.name.lower(),
                     lambda h: len(hits) == 1):
            found = [h for h in hits if test(h)]
            if found:
                primary = found[0]
                break
        if primary is None:
            return None
        pid, pname = primary["id"], primary.get("name") or ch.title
        feeds, query = {pid}, pname
        now = await self._call("whats_on_now", {"channel": ch.name})
        mine = [r for r in now if r.get("channel_id") == pid]
        if mine:
            query = ch.name
            feeds |= {r["channel_id"] for r in now if _same_airing(r, mine[0])}
        return pid, pname, feeds, query

    async def channels(self) -> dict[str, pr.Channel]:
        async with self._lock:
            if self._map and time.monotonic() - self._at < MAP_TTL_S:
                return self._map
            lineup = await pr.lineup()
            if not lineup:
                return self._map            # stale beats nothing
            matches = await asyncio.gather(*(self._match(c) for c in lineup))
            m, names, query = {}, {}, {}
            for ch, hit in zip(lineup, matches):
                if hit:
                    pid, pname, feeds, q = hit
                    for f in feeds:
                        m.setdefault(f, ch)
                    names[pid] = pname
                    query[ch.channel_id] = q
            missing = [c.name for c, h in zip(lineup, matches) if not h]
            logger.info("lineup map: %d/%d channels in the guide, %d feeds%s",
                        len(names), len(lineup), len(m),
                        f"; missing {missing}" if missing else "")
            if m:
                self._map, self._names, self._query = m, names, query
                self._at = time.monotonic()
            return self._map

    async def guide_channel(self, said: str) -> str | None:
        """The TV's number or channel name → the name the guide matches for
        all of that station's feeds; None if it isn't a lineup channel."""
        hit, _ = await pr.find_channel(said)
        if hit is None:
            return None
        await self.channels()
        return self._query.get(hit.channel_id)

    def _label(self, row: dict, ch: pr.Channel) -> dict:
        """Speak the TV's channel name and number, and give the airing the
        player's own id — <lineup channel_id>@<start UTC minute> — so the
        model can hand it straight to tv_play_airing (guide and player share
        the station's schedule, so the start minute is the same)."""
        row = dict(row)
        row["channel_name"] = ch.name
        row["channel_number"] = ch.number
        start = row.get("start")
        if isinstance(start, str) and len(start) >= 16:
            row["airing_id"] = f"{ch.channel_id}@{start[:16]}Z"
        for k in ("start", "end", "expiration"):
            if isinstance(row.get(k), str) and (lt := local_time(row[k])):
                row[f"{k}_local"] = lt
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

        per = await asyncio.gather(*(one(g) for g in self._names))
        rows = [r for rs in per for r in rs]
        rows.sort(key=lambda r: r.get("channel_number") or 0)
        return rows

    async def filter(self, name: str, result: Any) -> Any:
        rows = _rows(result)
        chans = await self.channels()
        if rows is None or not chans:
            return result
        key = SCOPED[name]
        kept, seen = [], set()
        for r in rows:
            if not isinstance(r, dict) or r.get(key) not in chans:
                continue
            ch = chans[r[key]]
            # One station, several feeds: keep each airing / channel once.
            dup = ((ch.channel_id, r.get("title"), r.get("start"))
                   if key == "channel_id" else ch.channel_id)
            if dup in seen:
                continue
            seen.add(dup)
            kept.append(self._label(r, ch) if key == "channel_id" else r)
        if name == "minny_search_catchup":
            # "Play the X catch-up" means the latest edition; the guide ranks by
            # relevance, so put the newest first (a stable sort keeps relevance
            # order between titles that started together).
            kept.sort(key=lambda r: r.get("start") or "", reverse=True)
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
        if name in SCOPED and tool_args.get("channel"):
            guide = await self.scope.guide_channel(str(tool_args["channel"]))
            if guide:
                tool_args = {**tool_args, "channel": guide}
        result = await super().call_tool(name, tool_args, ctx, tool)
        if name in SCOPED:
            return await self.scope.filter(name, result)
        return result
