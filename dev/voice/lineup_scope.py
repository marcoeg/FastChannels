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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from pydantic_ai.toolsets import WrapperToolset

import player_resolve as pr

logger = logging.getLogger("voice.lineup")

MAP_TTL_S = 3600
# After a failed map build, don't retry on every voice turn: an outage made
# each turn wait for 53 find_channels timeouts before saying anything.
MAP_RETRY_S = 60
# Rows handed to the model. 40 full rows were ~9.4k tokens, more than the
# whole history budget (MAX_HISTORY_TOKENS 8000), and the turn was trimmed.
MAX_ROWS = 25
# Row fields the model never uses: guide ids (it plays by airing_id and
# searches by channel name), ranking internals, empty series stats.
_DROP = {"id", "series_id", "season_id", "score", "matched", "external",
         "services", "series_episodes", "series_first_year",
         "series_last_year", "series_seasons", "channel_id"}
_DESC_CHARS = 160
# A genre word said as the search query ("cooking shows tonight"): routed to
# the per-station genre search, since the catalogue-wide text search ranks
# across 544 channels and the lineup filter keeps a few.
GENRE_WORDS = {"cooking", "news", "comedy", "comedies", "drama", "dramas",
               "sports", "documentary", "documentaries", "reality", "kids",
               "children", "cartoons", "animation", "game show", "game shows",
               "talk show", "talk shows", "western", "westerns", "music",
               "travel", "nature", "science", "history", "crime", "mystery",
               "horror", "romance", "action", "sitcom", "sitcoms", "soap",
               "soaps", "food", "religious", "faith", "weather", "business",
               "family", "anime", "thriller", "thrillers"}
_PLURAL = {"comedies": "comedy", "documentaries": "documentary",
           "cartoons": "cartoon", "dramas": "drama", "westerns": "western",
           "sitcoms": "sitcom", "soaps": "soap", "thrillers": "thriller",
           "game shows": "game show", "talk shows": "talk show",
           "children": "children", "kids": "children"}


# Genre words whose programmes are tagged by the specific kind: Sunday Night
# Football is ["American Football", "Pro"] — no "Sports" tag.
_GENRE_ALSO = {"sports": ("football", "basketball", "baseball", "hockey", "soccer",
                          "golf", "tennis", "boxing", "wrestling", "racing",
                          "rodeo", "volleyball", "rugby", "cricket", "lacrosse",
                          "softball", "mma", "nascar", "athletics", "olympics")}


def genre_hit(genre: str, tags) -> bool:
    """`genre` (lower case) is a word inside one of a row's genre tags."""
    tags = [str(t).lower() for t in (tags or [])]
    return any(w in t for w in (genre, *_GENRE_ALSO.get(genre, ())) for t in tags)


def genre_query(query: str) -> str | None:
    """'cooking shows' / 'Comedies' → the genre word to match in the rows'
    genre tags; None when the query is title words."""
    q = " ".join(str(query).lower().replace("programmes", "").replace(
        "programs", "").split())
    for cand in (q, q.removesuffix(" shows").removesuffix(" show")):
        if cand in GENRE_WORDS:
            return _PLURAL.get(cand, cand)
    return None


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
    # On the night the clocks go back, 1:00-1:59 AM happens twice: say which
    # (a 1:30 PDT - 1:00 PST programme no longer "ends before it starts").
    ambiguous = t.replace(fold=1 - t.fold).utcoffset() != t.utcoffset()
    zone = f" {t.tzname()}" if ambiguous else ""
    return f"{t.strftime('%-I:%M %p')}{zone} {day}"


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def start_key(row: dict) -> datetime:
    """A row's start as an instant. Labelled rows carry Pacific-offset ISO
    text, whose text order is not time order across the fall-back hour
    (01:30-07:00 sorts after 01:00-08:00, which is later)."""
    try:
        t = datetime.fromisoformat(str(row.get("start")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _EPOCH
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _pacific_iso(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_TZ)
        return t.isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return iso


_NOT_FILM = {"sport", "sports", "music", "reality", "competition", "rodeo",
             "talk", "news", "newscast", "game show", "shopping", "infomercial",
             "cooking", "cooking show", "food", "travel", "magazine", "series"}


_TBA = {"to be announced", "tba", "programming", "off air", "sign off", "paid programming"}


def _is_placeholder(row: dict, ch) -> bool:
    """Channel-feed filler the catalogue-wide search used to hide: a title
    that is the channel's own name ("TeenVee"), "To Be Announced", "… programming"."""
    t = pr._norm(row.get("title") or "")
    return (not t or t in _TBA or t.endswith(" programming")
            or pr._key(t) in (pr._key(ch.name), pr._key(ch.title)))


def _looks_like_film(row: dict) -> bool:
    """FAST channels tag nearly every block programType=Movie, so kind=movie
    let 4 h music blocks, slap-fight cards and rodeo through as films. A
    film here has a release year or a film genre, and not only
    sport/music/reality-type tags."""
    genres = {str(g).lower() for g in (row.get("genres") or [])}
    if genres and genres <= _NOT_FILM:
        return False
    return bool(row.get("year")) or bool(genres - _NOT_FILM - {"movie"})


def _far_ahead(start: Any, days: float = 3) -> bool:
    """A window starting more than `days` ahead — past where the guide is
    reliably ingested."""
    try:
        st = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        return st - datetime.now(st.tzinfo or _TZ) > timedelta(days=days)
    except (TypeError, ValueError):
        return False


def _searchable(name: str) -> str:
    """The guide's index matches word prefixes of at most 15 letters, so
    "TrailersFromHell" (16) finds nothing — as a find_channels query or a
    `channel` filter — while "TrailersFromHel" finds it. Shorter words pass
    unchanged."""
    return " ".join(w[:15] for w in str(name).split())


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
        self._failed_at = 0.0
        self._lock = asyncio.Lock()
        self._refresh: asyncio.Task | None = None
        self.unavailable = "lineup"     # why the map is empty: lineup | guide

    async def _call(self, tool: str, args: dict) -> list:
        try:
            return _rows(await self.minny.direct_call_tool(tool, args)) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("lineup map: %s %s: %s", tool, args, e)
            return []

    async def _match(self, ch: pr.Channel) -> tuple[str, str, set[str], str] | None:
        """(primary guide id, its name, all same-feed guide ids, the name to
        pass as `channel` so the guide matches every feed)."""
        said = ch.title or ch.name
        hits = await self._call("find_channels", {"query": said})
        if not hits and _searchable(said) != said:
            hits = await self._call("find_channels", {"query": _searchable(said)})
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
        pid = primary["id"]
        # Every name kept here is passed back to the guide as `channel`.
        pname = _searchable(primary.get("name") or ch.title)
        feeds, query = {pid}, pname
        now = await self._call("whats_on_now", {"channel": _searchable(ch.name)})
        mine = [r for r in now if r.get("channel_id") == pid]
        if mine:
            query = _searchable(ch.name)
            # Same programme AND answering to this station's name: a different
            # station simulcasting the same network show is not a feed of it.
            key = pr.squash(ch.name)
            feeds |= {r["channel_id"] for r in now if _same_airing(r, mine[0])
                      and key in pr.squash(r.get("channel_name") or "")}
        return pid, pname, feeds, query

    async def channels(self) -> dict[str, pr.Channel]:
        """The map. A stale one is served at once and rebuilt in the
        background — the hourly rebuild (53 find_channels + 53 whats_on_now)
        used to sit inside whichever voice turn crossed the hour. Only the
        first build is waited for, and not again within MAP_RETRY_S of a
        failure."""
        now = time.monotonic()
        if self._map:
            if now - self._at >= MAP_TTL_S and not (self._refresh and not self._refresh.done()) \
                    and now - self._failed_at >= MAP_RETRY_S:
                self._refresh = asyncio.create_task(self._rebuild())
            return self._map
        if now - self._failed_at < MAP_RETRY_S:
            return self._map
        await self._rebuild()
        return self._map

    async def _rebuild(self) -> None:
        async with self._lock:
            if self._map and time.monotonic() - self._at < MAP_TTL_S:
                return
            try:
                await self._build()
            except Exception as e:  # noqa: BLE001
                logger.warning("lineup map rebuild failed: %s", e)
            if not self._map or time.monotonic() - self._at >= MAP_TTL_S:
                self._failed_at = time.monotonic()

    async def _build(self) -> None:
            lineup = await pr.lineup()
            if not lineup:
                self.unavailable = "lineup"
                return                      # stale beats nothing
            matches = await asyncio.gather(*(self._match(c) for c in lineup))
            m, names, query = {}, {}, {}
            # Primaries first: a sister feed of one station must never take
            # the id another lineup channel answers to.
            for ch, hit in zip(lineup, matches):
                if hit:
                    m[hit[0]] = ch
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
            else:
                self.unavailable = "guide"

    def unavailable_text(self) -> str:
        if self.unavailable == "guide":
            return ("The TV guide is unavailable right now (it could not be "
                    "reached) — say so; don't say nothing is on.")
        return ("This TV's channel list is unavailable right now (the TV "
                "player isn't answering) — say so; don't say nothing is on.")

    async def guide_channel(self, said: str) -> tuple[str, str] | None:
        """The TV's number or channel name → (the name the guide matches for
        all of that station's feeds, the lineup channel_id); None if it isn't
        a lineup channel."""
        hit, _ = await pr.find_channel(said)
        if hit is None:
            return None
        await self.channels()
        q = self._query.get(hit.channel_id)
        return (q, hit.channel_id) if q else None

    def _label(self, row: dict, ch: pr.Channel) -> dict:
        """Speak the TV's channel name and number, and give the airing the
        player's own id — <lineup channel_id>@<start UTC minute> — so the
        model can hand it straight to tv_play_airing (guide and player share
        the station's schedule, so the start minute is the same)."""
        row = {k: v for k, v in row.items()
               if k not in _DROP and v not in (None, [], "", False)}
        if isinstance(row.get("description"), str) and len(row["description"]) > _DESC_CHARS:
            row["description"] = row["description"][:_DESC_CHARS].rsplit(" ", 1)[0] + "…"
        if isinstance(row.get("genres"), list):
            row["genres"] = row["genres"][:3]
        if row.get("series_title") == row.get("title"):
            row.pop("series_title", None)
        row["channel_name"] = ch.name
        row["channel_number"] = ch.number
        start = row.get("start")
        if isinstance(start, str) and len(start) >= 16:
            row["airing_id"] = f"{ch.channel_id}@{start[:16]}Z"
        # One time convention for the model: start/end/expiration become
        # Pacific-offset ISO ("2026-10-05T00:00:00-07:00"), the same form the
        # prompt tells it to build windows in — so "after that" can pass a
        # row's `end` straight back as a window `start`. With UTC "…Z" rows
        # the model re-labelled 07:00Z as 07:00-07:00 and searched 7 am.
        for k in ("start", "end", "expiration"):
            if isinstance(row.get(k), str) and (lt := local_time(row[k])):
                row[f"{k}_local"] = lt
                row[k] = _pacific_iso(row[k])
        # "What's next on KQED?" took a what's-on lookup before the schedule
        # search just to know which row was current: mark it.
        try:
            now = datetime.now(_TZ)
            st = datetime.fromisoformat(str(row.get("start")).replace("Z", "+00:00"))
            en = datetime.fromisoformat(str(row.get("end")).replace("Z", "+00:00"))
            if st <= now < en:
                row["on_now"] = True
        except (TypeError, ValueError):
            pass
        return row

    async def whats_on_now(self, genre: str | None = None) -> list | str:
        """Every lineup station's current programme; `genre`: only rows
        with that word in a genre tag (matched before the tags are cut to 3)."""
        chans = await self.channels()
        if not chans:
            return self.unavailable_text()

        failures: list[str] = []

        async def one(gid: str) -> list:
            try:
                rows = _rows(await self.minny.direct_call_tool(
                    "whats_on_now", {"channel": self._names[gid]})) or []
            except Exception as e:  # noqa: BLE001
                logger.warning("whats_on_now %s: %s", self._names[gid], e)
                failures.append(str(e)[:200])
                return []
            return [self._label(r, chans[gid]) for r in rows
                    if r.get("channel_id") == gid
                    and (not genre or genre_hit(genre, r.get("genres")))]

        per = await asyncio.gather(*(one(g) for g in self._names))
        rows = [r for rs in per for r in rs]
        # A bare [] (every call failed in a VPN outage) was read as either
        # "nothing is on" or "couldn't check", at random.
        if failures and len(failures) * 2 >= len(self._names):
            return (f"The guide could not be checked ({len(failures)} of "
                    f"{len(self._names)} channels failed: {failures[0]}). Say the TV "
                    "guide is unavailable right now — don't say nothing is on.")
        if not rows:
            what = f"no {genre} programme" if genre else "nothing"
            return (f"The guide was checked on every channel of this TV and lists "
                    f"{what} on air right now — the guide is reachable.")
        rows.sort(key=lambda r: r.get("channel_number") or 0)
        return rows

    async def search_lineup(self, args: dict) -> list | str:
        """A broad schedule search (no channel, no title words) per lineup
        station, in parallel. The catalogue-wide search returns ~a dozen rows
        ranked across 544 channels and the lineup filter keeps a few:
        "cooking shows tonight" came back as 3 Gusto Español rows while Gusto
        TV had cooking shows all evening. The guide's genre filter is also
        exact and tags vary ("Cooking" vs "Cooking Show"), so the genre is
        matched here as a word in any genre tag."""
        chans = await self.channels()
        if not chans:
            return self.unavailable_text()
        genre = (args.get("genre") or "").strip().lower()
        base = {k: v for k, v in args.items() if k not in ("genre", "channel")}

        failures: list[str] = []

        async def one(gid: str) -> list:
            try:
                rows = _rows(await self.minny.direct_call_tool(
                    "search_live_schedule", {**base, "channel": self._names[gid]})) or []
            except Exception as e:  # noqa: BLE001
                logger.warning("search_live_schedule %s: %s", self._names[gid], e)
                failures.append(str(e)[:200])
                return []
            out = []
            for r in rows:
                if r.get("channel_id") != gid:
                    continue
                if genre and not genre_hit(genre, r.get("genres")):
                    continue
                if base.get("kind") == "movie" and not _looks_like_film(r):
                    continue
                if _is_placeholder(r, chans[gid]):
                    continue
                out.append(self._label(r, chans[gid]))
            return out

        per = await asyncio.gather(*(one(g) for g in self._names))
        rows = [r for rs in per for r in rs]
        # A VPN outage made all 53 calls fail and the empty result used to be
        # announced as "the guide is reachable; this is a real 'none'".
        if failures and len(failures) * 2 >= len(self._names):
            return (f"The guide could not be checked ({len(failures)} of "
                    f"{len(self._names)} channels failed: {failures[0]}). Say the TV "
                    "guide is unavailable right now — don't say nothing is on.")
        if not rows:
            if _far_ahead(args.get("start")):
                return ("The guide was searched on every channel of this TV and found "
                        "nothing for that time — it may not reach that far ahead "
                        "yet; say so rather than 'nothing is on'.")
            return ("The guide was searched on every channel of this TV and found "
                    "nothing matching — the guide is reachable; this is a real 'none'.")
        rows.sort(key=lambda r: (start_key(r), r.get("channel_number") or 0))
        if len(rows) > MAX_ROWS:
            logger.info("search_lineup: %d rows, %d sent", len(rows), MAX_ROWS)
            return {"rows": rows[:MAX_ROWS],
                    "note": f"{len(rows)} matches; the first {MAX_ROWS} by start "
                            "time are shown — the list goes on later."}
        return rows

    async def filter(self, name: str, result: Any, only: str | None = None) -> Any:
        """`only`: the lineup channel_id the viewer asked about — the guide's
        name match for "KQED" also returns KQED2's feeds, and "what's next on
        KQED" answered with channel 10's schedule."""
        rows = _rows(result)
        if rows == []:
            # A bare [] was read by the model as "couldn't check the guide"
            # (it said so on an empty Columbo search with the guide up).
            return ("The guide was searched and found nothing matching — the "
                    "guide is reachable; this is a real 'none'.")
        chans = await self.channels()
        if rows is None or not chans:
            return result
        key = SCOPED[name]
        kept, seen = [], set()
        for r in rows:
            if not isinstance(r, dict) or r.get(key) not in chans:
                continue
            ch = chans[r[key]]
            if only and ch.channel_id != only:
                continue
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
            kept.sort(key=start_key, reverse=True)
        if rows and not kept:
            return ("Nothing on this TV's channels matches. (Channels this TV "
                    "doesn't carry may have it — don't offer those.)")
        return kept[:MAX_ROWS]


@dataclass
class LineupScopedToolset(WrapperToolset):
    """The minny guide toolset, answering only for the TV's lineup."""

    scope: LineupScope | None = field(default=None)

    async def call_tool(self, name, tool_args, ctx, tool):
        if name == "minny_whats_on_now" and not tool_args.get("channel"):
            return await self.scope.whats_on_now()
        if name == "minny_search_live_schedule" and not tool_args.get("channel"):
            if not tool_args.get("query"):
                return await self.scope.search_lineup(tool_args)
            # "cooking shows tonight" passed as query="cooking": the catalogue
            # text search ranks 544 channels and the lineup filter kept 3 rows.
            g = genre_query(tool_args["query"])
            if g and not tool_args.get("genre"):
                args = {k: v for k, v in tool_args.items() if k != "query"}
                return await self.scope.search_lineup({**args, "genre": g})
        only = None
        if name in SCOPED and tool_args.get("channel"):
            guide = await self.scope.guide_channel(str(tool_args["channel"]))
            if guide:
                tool_args = {**tool_args, "channel": guide[0]}
                only = guide[1]
            elif g := genre_query(str(tool_args["channel"])):
                # "Any news on now?" sent as channel="news": not a channel of
                # this TV, and the guide's name match found only off-lineup
                # "news" channels → "nothing matches" → "no news on".
                if name == "minny_whats_on_now":
                    return await self.scope.whats_on_now(genre=g)
                if name == "minny_search_live_schedule" and not tool_args.get("genre"):
                    args = {k: v for k, v in tool_args.items() if k != "channel"}
                    return await self.scope.search_lineup({**args, "genre": g})
        result = await super().call_tool(name, tool_args, ctx, tool)
        if name in SCOPED:
            return await self.scope.filter(name, result, only=only)
        return result
