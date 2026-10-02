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

import json
import logging
import os
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


def build_agent() -> Agent:
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
        tv_search = [ResilientToolset(ts, svc="minny")
                     for ts in load_mcp_toolsets(minny_cfg)]
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

        async def _control(name: str, args: dict) -> str:
            try:
                return _compact(await ytvn.direct_call_tool(name, args))
            except Exception as e:  # noqa: BLE001 — no_shell, unknown_channel, …
                return f"{name} failed: {e}"

        @agent.tool_plain
        async def tv_watch_channel(channel: str) -> str:
            """Put a live channel on the TV. channel: what the viewer said —
            a name, call sign, network or number ("KTVU", "Fox", "Gusto TV",
            "channel 9"). Resolves it against this TV's own lineup and tunes
            in one call; returns the resulting viewer state, or the closest
            lineup channels when it is ambiguous or not found."""
            hit, cands = await pr.find_channel(channel)
            if hit:
                return await _control("tune_channel",
                                      {"channel_id": hit.channel_id})
            if cands:
                return ("Not tuned — several channels match; ask which: "
                        + "; ".join(f"{c.name} ({c.number})" for c in cands))
            if not await pr.lineup():
                return "The TV's lineup is unavailable — the TV is not connected."
            # "Play Love Nature": the viewer may have named a title.
            titles = await pr.search_titles(channel)
            if titles:
                return (f"{channel!r} is not a channel on this TV, but it is an "
                        "on-demand title — " + await tv_watch_title(channel))
            return f"{channel!r} is not a channel on this TV."

        @agent.tool_plain
        async def tv_watch_title(title: str, year: int | None = None) -> str:
            """Play an on-demand title (film, show) on the TV. title: the
            title's essential words; year: release year if known. Searches
            this TV's on-demand catalogue and plays the match in one call;
            when several titles match it returns them with asset ids —
            ask the viewer, then call tv_play_asset(asset_id)."""
            titles = await pr.search_titles(title, year)
            pick = pr.pick_title(title, titles)
            if pick:
                return await _control("play_asset", {"asset_id": pick.asset_id})
            if titles:
                return ("Not playing — several titles match; ask which: "
                        + "; ".join(f"{t.title} ({t.year or 'year ?'}) "
                                    f"asset_id={t.asset_id}" for t in titles))
            if not year:
                # "Put on Trace": the viewer may have named a channel.
                hit, cands = await pr.find_channel(title)
                if hit or cands:
                    return (f"No on-demand title {title!r}, but it is a live "
                            "channel — " + await tv_watch_channel(title))
            return f"No on-demand title matching {title!r} on this TV."

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
        self.agent = build_agent()
        self.memory = memory
        self.history: list[ModelMessage] = []

    def make_deps(self, recall: "RecallResult | None") -> TurnDeps:
        if recall is None:
            return TurnDeps(memory=self.memory)
        return TurnDeps(facts_profile=recall.profile,
                        facts_recent=recall.recent,
                        memory=self.memory)

    def commit_turn(self, new_messages: list[ModelMessage]) -> None:
        self.history.extend(new_messages)
        self.history = _trim(self.history)

    def commit_interrupted(self, user_text: str, partial_text: str) -> None:
        """Barge-in: the run was cancelled, new_messages() is unavailable.
        Record what we actually emitted so the next turn has context."""
        self.history.append(ModelRequest(parts=[UserPromptPart(content=user_text)]))
        spoken = (partial_text.strip() + " …") if partial_text.strip() else "…"
        self.history.append(ModelResponse(parts=[TextPart(content=spoken)]))
        self.history = _trim(self.history)


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
