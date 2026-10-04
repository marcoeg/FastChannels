"""VoiceBrain — the pydantic-ai agent behind the voice pipeline.

One lean agent replaces the whole Hermes gateway path: static instructions
from prompts/*.md (~1.2k tokens vs Hermes' ~24k fixed overhead), exactly
three tool surfaces (EPG MCP read-only, TV-control MCP, in-process memory),
and a rolling history bounded to what a voice session actually needs.

History policy: the stored history is trimmed after every turn (single
mechanism — no ProcessHistory capability doing the same thing twice):
keep the last ≤12 exchanges AND ≤~8k estimated tokens, cut only at user
prompt boundaries, and blank tool-return payloads older than the two most
recent exchanges (EPG results are bulky and stale within a session).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPToolset, load_mcp_toolsets
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.openai import (
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)

from prompts import (
    format_memory_context,
    format_time_context,
    load_static_instructions,
)

if TYPE_CHECKING:
    from memory import RecallResult, VoiceMemory

logger = logging.getLogger("voice.brain")

EPG_ALLOW = {"search_memory_facts", "list_memory_facts", "search_nodes",
             "get_episodes", "get_status"}


@dataclass
class ResilientToolset(WrapperToolset):
    """A dead MCP server yields zero tools instead of a dead assistant.

    Agent.__aenter__ normally raises if any MCP server refuses connections,
    killing the whole voice worker. This wrapper absorbs enter failures,
    serves an empty tool dict while the server is down, and retries the
    connection once per run so the tools come back on their own.
    """

    svc: str = ""
    _up: bool = field(default=False, init=False, repr=False)

    async def __aenter__(self):
        try:
            await self.wrapped.__aenter__()
            self._up = True
        except Exception as e:  # noqa: BLE001
            logger.warning("%s MCP unavailable, continuing without it: %s",
                           self.svc, e)
        return self

    async def __aexit__(self, *args):
        if not self._up:
            return None
        try:
            return await self.wrapped.__aexit__(*args)
        except Exception:  # noqa: BLE001
            return None

    async def get_tools(self, ctx):
        if not self._up:
            try:
                await self.wrapped.__aenter__()
                self._up = True
                logger.info("%s MCP reconnected", self.svc)
            except Exception:  # noqa: BLE001
                return {}
        try:
            return await self.wrapped.get_tools(ctx)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s MCP lost (%s); dropping its tools", self.svc, e)
            self._up = False
            try:
                await self.wrapped.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            return {}

MAX_TURN_PAIRS = 12
MAX_HISTORY_TOKENS = 8_000          # chars//4 estimate
KEEP_TOOL_RESULTS_FOR = 2           # most recent exchanges keep raw payloads


@dataclass
class TurnDeps:
    facts_profile: list[str] = field(default_factory=list)
    facts_recent: list[str] = field(default_factory=list)
    memory: "VoiceMemory | None" = None


def _brain_model():
    """The brain's model + settings, switchable by VOICE_LLM.

    openai (default) — the original path, unchanged: Responses API on
        VOICE_MODEL with priority tier and reasoning effort.
    local — an OpenAI-compatible server (llama.cpp) via Chat Completions,
        using its OWN LOCAL_LLM_* variables passed explicitly. Never set
        OPENAI_BASE_URL for this: the OpenAI SDK reads it from the
        environment, which would also redirect STT, memory extraction and
        embeddings — those stay on OpenAI.
    """
    if os.environ.get("VOICE_LLM", "openai") == "local":
        from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
        from pydantic_ai.profiles.openai import OpenAIModelProfile
        from pydantic_ai.providers.openai import OpenAIProvider
        provider = OpenAIProvider(base_url=os.environ["LOCAL_LLM_BASE_URL"],
                                  api_key=os.environ["LOCAL_LLM_API_KEY"])
        # Qwen chat templates reject a second system message ("System
        # message must be at the beginning"); our static instructions and
        # the per-turn clock/memory blocks arrive as separate ones, so have
        # pydantic-ai merge them into a single leading system message.
        model = OpenAIChatModel(
            os.environ.get("LOCAL_LLM_MODEL", "qwen3.5-9b"), provider=provider,
            profile=OpenAIModelProfile(openai_chat_supports_multiple_system_messages=False))
        settings = OpenAIChatModelSettings(
            parallel_tool_calls=True,
            max_tokens=700,
            # Qwen-style chat templates: skip the thinking block, which a
            # voice turn would otherwise wait on (llama.cpp honours this).
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        logger.info("brain: local LLM %s at %s",
                    model.model_name, os.environ["LOCAL_LLM_BASE_URL"])
        return model, settings

    settings = OpenAIResponsesModelSettings(
        openai_reasoning_effort=os.environ.get("VOICE_REASONING_EFFORT", "none"),
        openai_service_tier="priority",
        openai_reasoning_context="current_turn",
        parallel_tool_calls=True,
        max_tokens=700,                            # spoken answers are short
    )
    return OpenAIResponsesModel(os.environ.get("VOICE_MODEL", "gpt-5.6-luna")), settings


def build_agent(primers: list | None = None) -> Agent:
    """primers collects async callables to run once at session start, off
    the voice path (VoiceBrain.prime), so no turn pays for them."""
    primers = primers if primers is not None else []
    # Two TV-search backends, selected at startup and never mixed (alternatives,
    # not summative): "minny" (default) = the Minny MCP over the operator's
    # OpenSearch catalogue; "falkordb" = the local Graphiti guide graph via the
    # EPG MCP. Flip with VOICE_EPG_BACKEND.
    backend = os.environ.get("VOICE_EPG_BACKEND", "minny")
    # Speak and build time windows in the VIEWER's local zone — the Minny
    # satellite sits in the Bay Area (Pacific), so times are Pacific no matter
    # which catalogue answers. The salesd catalogue's operator zone is Eastern,
    # but that is a property of the tenant's data, not of where the person in the
    # room is: converting to PT is what makes "three this afternoon" mean 3 pm to
    # them. Override with VOICE_LOCAL_TZ if the device ever moves.
    tz = ZoneInfo(os.environ.get("VOICE_LOCAL_TZ", "America/Los_Angeles"))

    # The player the tv_ tools drive, by VOICE_TV (alternatives — one only):
    #   tvshell (default): the TVShell kiosk via its MCP bridge at TV_MCP_URL
    #   ytvn: the ytvn-epg web viewer via its control-only MCP server, spawned
    #     over stdio from mcp-tv-ytvn.json (server name "tv" keeps the tv_
    #     prefix). It needs `python3 serve.py` running in ytvn-epg and the page
    #     open in a browser; otherwise its tools report the TV as not connected.
    player = os.environ.get("VOICE_TV", "tvshell")
    ytvn = None
    scopes: list = []                        # LineupScopes (ytvn + Minny guide)
    if player == "ytvn":
        # The model never sees the raw-id tune (guide ids are the catalogue's,
        # not the player's) nor play_url: it watches by name through the
        # resolver tools below, which call the server directly. play_asset
        # stays for picking one of several titles the resolver listed.
        prefixed = load_mcp_toolsets(
            str(Path(__file__).parent / "mcp-tv-ytvn.json"))[0]
        ytvn = prefixed.wrapped
        tv = ResilientToolset(
            prefixed.filtered(lambda ctx, td: td.name not in
                              {"tv_tune_channel", "tv_tune_number", "tv_play_url"}),
            svc="tv")
    else:
        tv = ResilientToolset(
            MCPToolset(os.environ["TV_MCP_URL"],
                       cache_tools=True,
                       init_timeout=5.0,
                       read_timeout=5.0,
                       tool_error_behavior="failed")
            .prefixed("tv"),
            svc="tv")

    if backend == "minny":
        # Minny MCP (stdio) from mcp.json — one toolset per server, already
        # prefixed "minny_". Wrap for the same dead-server resilience as the
        # others. The server needs the Minerva VPN up (see VPN-CONNECT.md).
        minny_cfg = str(Path(__file__).parent / "mcp.json")
        tv_search = load_mcp_toolsets(minny_cfg)
        for ts in tv_search:
            # Starts the server and its catalogue client, and checks the
            # catalogue answers — what the model used to spend a round trip on.
            primers.append(lambda raw=ts.wrapped:
                           raw.direct_call_tool("cluster_status", {}))
        if player == "ytvn":
            # The guide covers the operator's whole catalogue; the ytvn TV
            # carries one lineup. Keep guide answers to the TV's channels.
            from lineup_scope import LineupScope, LineupScopedToolset
            scopes = [LineupScope(ts.wrapped) for ts in tv_search]
            primers.extend(sc.channels for sc in scopes)
            # On-demand titles come from the TV's own catalogue (tv_on_demand,
            # tv_watch_title): the guide's VOD index is the operator's, with
            # ids and titles this TV may not have.
            tv_search = [LineupScopedToolset(ts, scope=sc).filtered(
                             lambda ctx, td: td.name != "minny_search_vod")
                         for ts, sc in zip(tv_search, scopes)]
        tv_search = [ResilientToolset(ts, svc="minny") for ts in tv_search]
    else:
        tv_search = [ResilientToolset(
            MCPToolset(os.environ["EPG_MCP_URL"],
                       cache_tools=True,
                       init_timeout=5.0,
                       # Ranked search on frequent terms measures 5.5-6.5 s on
                       # this server (query-shape cost, pre-existing; Hermes hit
                       # 15 s timeouts on the same thing). 8 s admits real
                       # queries while still guillotining true wedges; the
                       # prompt steers channel listings to the ~8 ms list tool.
                       read_timeout=8.0,
                       tool_error_behavior="failed")
            .filtered(lambda ctx, td: td.name in EPG_ALLOW)
            .prefixed("epg"),
            svc="epg")]

    model, settings = _brain_model()

    agent = Agent(
        model,
        instructions=load_static_instructions(backend, player=player),
        toolsets=[*tv_search, tv],
        model_settings=settings,
        deps_type=TurnDeps,
    )

    @agent.instructions
    def time_context(ctx: RunContext[TurnDeps]) -> str:
        return format_time_context(tz=tz)

    @agent.instructions
    def memory_context(ctx: RunContext[TurnDeps]) -> str:
        return format_memory_context(ctx.deps.facts_profile,
                                     ctx.deps.facts_recent)

    if backend == "falkordb":
        @agent.tool_plain
        async def whats_on(at_time: str | None = None,
                           channel: str | None = None,
                           query: str | None = None) -> str:
            """AUTHORITATIVE listing of what is actually ON AIR at a moment.
            Times are computed in code, so rows are never stale. at_time: "now"
            (default) or a clock time like "9pm"/"21:30". channel: exact
            channel name to restrict to one channel. query: case-insensitive
            keyword to match against channel, genres, title and description
            (e.g. "Movie", "News", "Columbo"). Use this for every "what's on /
            is X on now / what's on at 9" question."""
            from epg_now import whats_on as _whats_on
            return await _whats_on(at_time=at_time, channel=channel, query=query)

    if ytvn is not None:
        import player_resolve as pr
        pr.use_mcp(ytvn.read_resource)

        def _now_showing(state: str) -> str:
            try:
                a = (json.loads(state).get("tuned") or {}).get("airing") or {}
            except (ValueError, AttributeError):
                return ""
            if not a.get("title"):
                return ""
            from lineup_scope import local_time
            end = local_time(str(a.get("end_utc", "")).replace("Z", ":00Z")) if a.get("end_utc") else None
            return f" Now showing: {a['title']}" + (f" until {end}." if end else ".")

        async def _control(name: str, args: dict) -> str:
            try:
                return _compact(await ytvn.direct_call_tool(name, args))
            except Exception as e:  # noqa: BLE001 — no_shell, unknown_channel, …
                return f"{name} failed: {e}"

        async def _control_ok(name: str, args: dict) -> tuple[bool, str]:
            """(ok, text): callers that prefix a success sentence must not do
            so on failure ("Tuned KQED … tune_channel failed: no_shell")."""
            text = await _control(name, args)
            return not text.startswith(f"{name} failed:"), text

        @agent.tool_plain
        async def tv_watch_channel(channel: str) -> str:
            """Put a live channel on the TV. channel: what the viewer said —
            a name, call sign, network or number ("KTVU", "Fox", "Gusto TV",
            "channel 9"). Resolves it against this TV's own lineup and tunes
            in one call; returns the resulting viewer state, or the closest
            lineup channels when it is ambiguous or not found."""
            hit, cands = await pr.find_channel(channel)
            if hit:
                ok, state = await _control_ok("tune_channel", {"channel_id": hit.channel_id})
                if not ok:
                    return f"Could not tune {hit.name} (channel {hit.number}): {state}"
                # Say what was tuned: from "KTBU" the model read the state
                # and told the viewer "KTBU isn't in this TV's lineup".
                # No mention of what the viewer said: "matched from 'KTBU'"
                # made the model repeat the misheard name back. What the
                # channel is showing comes first, so "play TMZ on KTVU" is
                # answered against it ("TMZ isn't on; Fox NFL Sunday is").
                return (f"Tuned {hit.name}, channel {hit.number}.{_now_showing(state)} "
                        f"Resulting state: {state}")
            if cands:
                return ("Not tuned — several channels match; ask which: "
                        + "; ".join(f"{c.name} ({c.number})" for c in cands))
            if not await pr.lineup():
                return "The TV's lineup is unavailable — the TV is not connected."
            # "Put on College Football": a programme on air, not a channel.
            live = await _airing_now(channel)
            if len(live) == 1:
                r = live[0]
                return (f"{channel!r} is not a channel, but {r['title']!r} is on now on "
                        f"{r['channel_name']} until {r.get('end_local', '?')} — "
                        + await tv_watch_channel(r["channel_name"]))
            if live:
                return (f"{channel!r} is on now on several channels; ask which: "
                        + "; ".join(f"{r['channel_name']} ({r.get('channel_number')}) "
                                    f"until {r.get('end_local', '?')}" for r in live))
            # "Play Love Nature": the viewer may have named a title.
            titles = await pr.search_titles(channel)
            if titles:
                return (f"{channel!r} is not a channel on this TV, but it is an "
                        "on-demand title — " + await tv_watch_title(channel))
            return f"{channel!r} is not a channel on this TV."

        @agent.tool_plain
        async def tv_watch_title(title: str, year: int | None = None,
                                 channel: str | None = None) -> str:
            """Play, watch or catch up on a show, film or series BY NAME — the
            one call for any named programme, even when a channel is named
            too ("catch up on Face the Nation on channel 5" → title="Face the
            Nation", channel="channel 5"). title: its essential words; year:
            release year if known; channel: the channel the viewer named, if
            any (name or number). In one call it plays the on-demand title,
            or tunes the channel airing it now, or plays it as catch-up if it
            aired on this TV (on that channel, if given) in the last 24
            hours. When several titles match it lists them with asset ids —
            ask the viewer, then call tv_play_asset(asset_id)."""
            return await _watch_title(title, year, channel, retry=True)

        async def _watch_title(title: str, year: int | None, channel: str | None,
                               retry: bool) -> str:
            only = None
            if channel:
                hit, _ = await pr.find_channel(channel)
                only = hit.name if hit else None
            # The three lookups are independent: run them together (they
            # took ~4 s in sequence, and viewers spoke again before an answer).
            titles, live, recent = await asyncio.gather(
                pr.search_titles(title, year),
                _airing_now(title, only) if not year else _none(),
                _aired_recently(title, only) if not year else _none())
            pick = pr.pick_title(title, titles)
            if pick:
                return await _control("play_asset", {"asset_id": pick.asset_id})
            if titles:
                return ("Not playing — several titles match; ask which: "
                        + "; ".join(f"{t.title} ({t.year or 'year ?'}) "
                                    f"asset_id={t.asset_id}" for t in titles))
            if not year:
                # "Put on Columbo": a show airing now on one of this TV's channels.
                if len(live) == 1:
                    r = live[0]
                    return (f"Not on demand, but {r['title']!r} is on live now on "
                            f"{r['channel_name']} until {r.get('end_local', '?')} — "
                            + await tv_watch_channel(r["channel_name"]))
                if live:
                    return ("Not on demand, but it is on live now on several channels; "
                            "ask which: " + "; ".join(
                                f"{r['channel_name']} ({r.get('channel_number')}) until "
                                f"{r.get('end_local', '?')}" for r in live))
                # "Put on Trace": the viewer may have named a channel.
                hit, cands = await pr.find_channel(title)
                if hit or cands:
                    return (f"No on-demand title {title!r}, but it is a live "
                            "channel — " + await tv_watch_channel(title))
                # Aired earlier on one of this TV's channels → catch-up, from
                # the schedule, not the catch-up index: the index lags ~30 min
                # behind an airing's end, while the player plays any airing in
                # the channel's 24 h window by its airing_id.
                if len(recent) == 1:
                    r = recent[0]
                    ok, played = await _control_ok("play_airing",
                                                   {"airing_id": r["airing_id"], "mode": "catchup"})
                    what = (f"Not on demand or live now, but {r['title']!r} aired on "
                            f"{r['channel_name']} at {r.get('start_local', '?')}")
                    if not ok:
                        return f"{what}, and catch-up did not start: {played}"
                    return f"{what} — playing it as catch-up: {played}"
                if recent:
                    return ("Not on demand or live now, but it aired earlier on this TV; "
                            "ask which to play as catch-up: " + "; ".join(
                                f"{r['title']} on {r['channel_name']} at {r.get('start_local', '?')} "
                                f"(airing_id {r['airing_id']})" for r in recent))
                # Speech often prepends a misheard verb ("Clay, the Farmer and
                # the Foodie" for "Play The Farmer…"): retry without it once.
                words = title.replace(",", " ").split()
                if retry and len(words) >= 3:
                    rest = " ".join(words[1:])
                    return (f"(nothing matched {title!r}; tried {rest!r}) "
                            + await _watch_title(rest, year, channel, retry=False))
            where = f" on {only}" if only else ""
            return (f"No on-demand title matching {title!r} on this TV, and it "
                    f"isn't on live right now or in the last 24 hours{where}. "
                    "Say so; don't guess when it is on.")

        async def _none() -> list:
            return []

        def _title_matches(title: str, row: dict) -> bool:
            """Every spoken word is in the row's title/series — compared
            without spaces, so 'PBS NewsHour' matches 'PBS News Hour'."""
            words = [w for w in pr._norm(pr.split_caps(title)).split() if len(w) > 1]
            hay = pr.squash(f"{row.get('title', '')} {row.get('series_title') or ''}")
            spoken = pr.squash(title)
            if not words:
                return False
            names = pr._norm(f"{row.get('title', '')} {row.get('series_title') or ''}").split()
            return all(w in names for w in words) or (len(spoken) >= 6 and spoken in hay)

        async def _aired_recently(title: str, only: str | None = None) -> list[dict]:
            """Airings of `title` on the TV's channels that ENDED in the last
            24 hours (the catch-up window), newest first, one per channel."""
            if not scopes:
                return []
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            try:
                raw = await scopes[0].minny.direct_call_tool("search_live_schedule", {
                    "query": pr.split_caps(title), "window": "range",
                    "start": (now - timedelta(hours=24)).isoformat(timespec="seconds"),
                    "end": now.isoformat(timespec="seconds")})
                rows = await scopes[0].filter("minny_search_live_schedule", raw)
            except Exception:  # noqa: BLE001
                return []
            if not isinstance(rows, list):
                return []
            out, seen = [], set()
            for r in sorted(rows, key=lambda r: r.get("start") or "", reverse=True):
                if not (_title_matches(title, r) and r.get("airing_id")):
                    continue
                try:
                    end = datetime.fromisoformat(str(r.get("end")))
                except ValueError:
                    continue
                if end > now or r.get("channel_name") in seen:
                    continue           # still on air (handled as live) / duplicate feed
                if only and r.get("channel_name") != only:
                    continue
                seen.add(r.get("channel_name"))
                out.append(r)
            return out

        async def _airing_now(title: str, only: str | None = None) -> list[dict]:
            """Rows on air now on the TV's channels whose title or series
            matches every word of `title`; one per channel."""
            if not scopes:
                return []
            try:
                rows = await scopes[0].whats_on_now()
            except Exception:  # noqa: BLE001
                return []
            if not isinstance(rows, list):
                return []
            out, seen = [], set()
            for r in rows:
                if only and r.get("channel_name") != only:
                    continue
                if _title_matches(title, r) and r.get("channel_name") not in seen:
                    seen.add(r.get("channel_name"))
                    out.append(r)
            return out

    if ytvn is not None:
        @agent.tool_plain
        async def tv_on_demand(category: str | None = None) -> str:
            """What this TV offers on demand. category: a genre or collection
            the viewer named ("comedy", "action", "family", "popular series");
            leave it out for the newest releases. Returns titles with asset
            ids (play one with tv_play_asset) and the category names."""
            titles, cats = await pr.browse(category)
            if not titles and not cats:
                return "The on-demand catalogue is unavailable right now."
            if not titles:
                return (f"No on-demand category like {category!r}. Categories: "
                        + ", ".join(cats))
            head = f"On demand — {category or 'New Releases'}: "
            return (head + "; ".join(f"{t.title} ({t.year or 'year ?'}) asset_id={t.asset_id}"
                                     for t in titles)
                    + ". Other categories: " + ", ".join(c for c in cats[:20]))

        @agent.instructions
        async def screen_context(ctx: RunContext[TurnDeps]) -> str:
            """What the TV shows, fresh per model request from the MCP
            server's cached viewer state (no round trip to the page). It
            carries the airing_id of the programme on screen, so "restart
            this" or "what am I watching" need no lookup — older tool results
            are blanked from the history after two exchanges."""
            try:
                raw = await asyncio.wait_for(ytvn.read_resource("viewer://state"), 0.3)
                st = json.loads(raw if isinstance(raw, str) else str(raw))
            except Exception:  # noqa: BLE001 — no page / slow: just omit the line
                return ""
            return format_screen(st)

    @agent.tool_plain
    async def web_lookup(query: str) -> str:
        """Look up background about a programme, film, person, or event on
        the web: plot, cast, year, series context, reviews, real-world
        facts. Use when guide descriptions are thin or the viewer asks
        about content rather than scheduling. NOT for what is on TV, when
        something airs, or the viewer's own preferences."""
        from websearch import search
        return await search(query)

    @agent.tool
    async def search_memory(ctx: RunContext[TurnDeps], query: str) -> str:
        """Search long-term memory of past conversations and household
        preferences. Not for TV schedules."""
        if ctx.deps.memory is None:
            return "No stored memories match that."
        facts = await ctx.deps.memory.deep_search(query, limit=8)
        return "\n".join(f"- {f}" for f in facts) or "No stored memories match that."

    return agent


class VoiceBrain:
    """Agent + rolling history + memory handle, shared across turns."""

    def __init__(self, memory: "VoiceMemory | None" = None) -> None:
        self._primers: list = []
        self.agent = build_agent(self._primers)
        self.memory = memory
        self.history: list[ModelMessage] = []

    async def prime(self) -> None:
        """Session-start work (catalogue check, lineup map) in parallel.
        Never raises: a failed primer just leaves that work to the first turn."""
        results = await asyncio.gather(*(p() for p in self._primers),
                                       return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("primer failed: %s", r)

    def make_deps(self, recall: "RecallResult | None") -> TurnDeps:
        if recall is None:
            return TurnDeps(memory=self.memory)
        return TurnDeps(facts_profile=recall.profile,
                        facts_recent=recall.recent,
                        memory=self.memory)

    def commit_turn(self, new_messages: list[ModelMessage]) -> None:
        self.history.extend(new_messages)
        self.history = _trim(self.history)

    def commit_exchange(self, user_text: str, reply: str) -> None:
        """An exchange answered without the model (closing acknowledgements),
        recorded so the next turn still sees it."""
        self.history.append(ModelRequest(parts=[UserPromptPart(content=user_text)]))
        self.history.append(ModelResponse(parts=[TextPart(content=reply)]))
        self.history = _trim(self.history)

    def last_reply(self) -> str:
        for msg in reversed(self.history):
            if isinstance(msg, ModelResponse):
                return " ".join(p.content for p in msg.parts if isinstance(p, TextPart))
        return ""

    def commit_interrupted(self, user_text: str, partial_text: str) -> None:
        """Barge-in: the run was cancelled, new_messages() is unavailable.
        Record what we actually emitted so the next turn has context."""
        self.history.append(ModelRequest(parts=[UserPromptPart(content=user_text)]))
        spoken = (partial_text.strip() + " …") if partial_text.strip() else "…"
        self.history.append(ModelResponse(parts=[TextPart(content=spoken)]))
        self.history = _trim(self.history)


# A closing acknowledgement ends an exchange; it asks for nothing. The model
# answered these with an EMPTY first response (gpt-5.6-terra, 18 times in two
# days), which pydantic-ai rejects and re-requests — 1-3 s for "Great." So
# they are answered here. Not "yes"/"no"/"sure": those can answer a question,
# and only when the last reply wasn't a question.
_CLOSING_ACK = re.compile(
    r"^(oh |ah |well )?(ok(ay)?|great|perfect|cool|nice|awesome|excellent|wonderful|"
    r"good|very good|that'?s (great|good|perfect)|got it|ok(ay)?,? (great|thanks|thank you|cool|perfect)|"
    r"thanks?( a lot| so much| very much)?|thank you( very much| so much)?)"
    r"( minny)?[.!]*$", re.I)


# A bare greeting heard right after Minny spoke is her own echo (a phantom
# "Hi Minny." was transcribed 42 times within 4 s of a reply). Shared by the
# LiveKit hook (which stops the reply) and the model adapter (which must not
# run, commit or trace the preemptive turn LiveKit already started for it).
BARE_GREETING = re.compile(
    r"^(hi|hey|hello|hei|ok|okay)?[, ]*(minny|minnie|mini|minni|winnie|wanda)[.!?]*$", re.I)
GREETING_ECHO_S = 4.0


def is_echo_greeting(text: str, last_reply_at: float, now: float) -> bool:
    return bool(BARE_GREETING.match(text.strip())) and now - last_reply_at < GREETING_ECHO_S


def closing_ack_reply(user_text: str, last_reply: str) -> str | None:
    """The reply to a closing acknowledgement, or None to let the model answer."""
    t = " ".join(user_text.replace("\u2019", "'").split())
    if not _CLOSING_ACK.match(t) or last_reply.rstrip().endswith("?"):
        return None
    if re.search(r"\bthank", t, re.I):
        return "You're welcome."
    if re.search(r"\b(loading|playing|restarting|on now|is on|tuned|starting|watching)\b", last_reply, re.I):
        return "Enjoy."
    return "Okay."


def _is_user_request(msg: ModelMessage) -> bool:
    return isinstance(msg, ModelRequest) and any(
        isinstance(p, UserPromptPart) for p in msg.parts)


def _trim(history: list[ModelMessage]) -> list[ModelMessage]:
    # Exchange boundaries = requests that carry an actual user prompt
    # (mid-run tool-return requests are not boundaries).
    starts = [i for i, m in enumerate(history) if _is_user_request(m)]
    if not starts:
        return history

    kept_start = 0
    for idx in range(max(0, len(starts) - MAX_TURN_PAIRS), len(starts)):
        candidate = starts[idx]
        est = sum(_est_tokens(m) for m in history[candidate:])
        if est <= MAX_HISTORY_TOKENS or idx == len(starts) - 1:
            kept_start = candidate
            break

    kept = history[kept_start:]

    # Blank bulky tool payloads outside the most recent exchanges.
    kept_starts = [i for i, m in enumerate(kept) if _is_user_request(m)]
    blank_before = kept_starts[-KEEP_TOOL_RESULTS_FOR] if len(
        kept_starts) > KEEP_TOOL_RESULTS_FOR else 0
    out: list[ModelMessage] = []
    for i, msg in enumerate(kept):
        if i < blank_before and isinstance(msg, ModelRequest) and any(
                isinstance(p, ToolReturnPart) for p in msg.parts):
            parts = [_replace(p, content="[tool result omitted]")
                     if isinstance(p, ToolReturnPart) else p
                     for p in msg.parts]
            out.append(_replace(msg, parts=parts))
        else:
            out.append(msg)
    return out


def format_screen(st: dict) -> str:
    """One line on what the TV shows, for the instructions."""
    from lineup_scope import local_time
    pl, player, tuned = st.get("playing") or {}, st.get("player") or {}, st.get("tuned") or {}
    if not pl or player.get("state") == "idle":
        return "On screen now: the programme grid (nothing playing)."
    what = {"live": "live", "catch-up": "catch-up", "restart": "restarted",
            "vod": "on demand", "url": "a stream"}.get(pl.get("kind"), pl.get("kind") or "")
    parts = [f"On screen now ({what}):"]
    if tuned:
        parts.append(f"{tuned.get('name')} channel {tuned.get('number')}")
        a = tuned.get("airing") or {}
        if a:
            when = ""
            if a.get("start_utc") and a.get("end_utc"):
                when = (f", {local_time(a['start_utc'].replace('Z', ':00Z'))}"
                        f" to {local_time(a['end_utc'].replace('Z', ':00Z'))}")
            parts.append(f"— {a.get('title')}{when} (airing_id {a.get('airing_id')})")
    elif pl.get("title"):
        parts.append(str(pl["title"]))
    vol = player.get("volume")
    parts.append(f"| player {player.get('state')}"
                 + (f", volume {round(vol * 100)}%" if isinstance(vol, (int, float)) else "")
                 + (", MUTED" if player.get("muted") else ""))
    return " ".join(parts)


def _compact(result) -> str:
    """A control tool's result as one JSON line without empty fields — the
    model reads it, and the viewer state is otherwise mostly nulls."""
    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items()
                    if x not in (None, "", [], {})}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    if isinstance(result, str):
        return result
    return json.dumps(strip(result), ensure_ascii=False, default=str)


def _replace(obj, **changes):
    """Copy-with-changes for message types that are dataclasses in some
    pydantic-ai versions and pydantic models in others."""
    import dataclasses
    if dataclasses.is_dataclass(obj):
        return dataclasses.replace(obj, **changes)
    return obj.model_copy(update=changes)


def _est_tokens(msg: ModelMessage) -> int:
    total = 0
    for p in getattr(msg, "parts", []):
        content = getattr(p, "content", "")
        try:
            total += len(str(content))
        except Exception:  # noqa: BLE001
            total += 100
    return total // 4
