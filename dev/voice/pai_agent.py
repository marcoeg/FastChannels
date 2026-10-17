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
import contextvars
import json
import logging
import os
import time
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


# A stdio MCP server that dies mid-job is respawned on the next call; two
# attempts are at least this far apart, so a server that dies on start is
# not hammered by every call that follows.
RESPAWN_MIN_S = 15.0
_OUTAGE = {"minny": "The TV guide is unavailable right now (it could not be "
                    "reached) — say so; don't say nothing is on.",
           "tv": "The TV isn't reachable right now — say so; don't retry."}


class GuideDown(Exception):
    """The guide could not be checked; the message says so. A lookup that
    failed must never read as "not found": on 2026-10-17 a guide server that
    was respawning turned "Turn on Premier League Soccer" into "isn't in this
    TV's lineup" while channel 48 was airing it."""


# minny-tv-mcp's tools the model never sees with the minny player: raw-id tunes and
# play_url (as with ytvn), and its extras — pairing is a setup step, one viewer
# needs no TV list, details are opened by the resolvers (partner-app titles),
# search and home are UI the voice does not drive yet.
MINNY_TV_HIDDEN = {"tv_tune_channel", "tv_tune_number", "tv_play_url", "tv_pair_tv",
                   "tv_list_tvs", "tv_search", "tv_open_details", "tv_go_home"}

# How a partner app is named to the viewer (the viewer's button: "Abrir en <name>").
PARTNER_APPS = {"netflix": "Netflix", "disney_plus": "Disney+", "amazon_prime": "Prime Video"}


# Spoken words and how the listings spell them (accents are folded first):
# "Premier League Soccer" is "Fútbol: Premier League" on Telemundo. "football"
# stays as it is: in a US lineup it is American football.
_SAME_WORD = {"soccer": "futbol"}


def _dead_connection(e: BaseException) -> bool:
    """The transport to a stdio server is gone: its process exited or its
    pipes closed. Anything else (a tool error, a slow backend, a request
    timeout) is not a reason to respawn."""
    seen: set[int] = set()
    stack: list[BaseException] = [e]
    while stack:
        x = stack.pop()
        if id(x) in seen:
            continue
        seen.add(id(x))
        name = type(x).__name__
        if name in ("ClosedResourceError", "BrokenResourceError", "EndOfStream",
                    "BrokenPipeError", "ConnectionResetError"):
            return True
        # The MCP client's own word for a transport that went away while a
        # request was in flight (McpError in SDK 1.x, MCPError in 2.x).
        if "connection closed" in str(x).lower():
            return True
        stack.extend(getattr(x, "exceptions", None) or [])
        stack.extend(y for y in (x.__cause__, x.__context__) if y is not None)
    return False


@dataclass
class MCPGuard:
    """Respawns a stdio MCP server in place once its process has died.

    MCPToolset enters once per job and never reconnects: when the guide
    server crashed mid-job (2026-10-06 16:52, after 3½ idle hours), every
    guide call for the rest of the day raised ClosedResourceError — "I
    couldn't reach the guide", then "something went wrong" — until the 04:30
    restart (BUGLIST #1). Every path to the raw toolset goes through a guard:
    the model's tool calls (ResilientToolset), the lineup scope's direct
    calls, the player's resource reads. On a dead connection one respawn
    task exits and re-enters the toolset (a fresh subprocess); callers that
    failed meanwhile wait for it and retry once. The task is shielded from a
    caller's cancellation (barge-in), so a respawn is never left half done.
    """

    raw: MCPToolset
    svc: str
    gen: int = field(default=0, init=False)          # bumps on every respawn
    _task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _last: float = field(default=0.0, init=False, repr=False)

    def kick(self, gen: int, err: BaseException) -> None:
        """Start a respawn for a caller that cannot wait for it (the
        per-request screen line has a 0.3 s budget)."""
        if _dead_connection(err):
            self._start(gen, err)

    def _start(self, gen: int, err: BaseException | None) -> asyncio.Task | None:
        if self.gen != gen:
            return None                       # respawned since that call failed
        if self._task is not None and not self._task.done():
            return self._task
        if time.monotonic() - self._last < RESPAWN_MIN_S:
            return None
        self._last = time.monotonic()
        self._task = asyncio.create_task(self._respawn(err), name=f"mcp-respawn-{self.svc}")
        return self._task

    async def recover(self, gen: int, err: BaseException | None = None) -> bool:
        """True when the toolset has been respawned since `gen`: retry."""
        task = self._start(gen, err)
        if task is not None:
            try:
                await asyncio.shield(task)
            except Exception:  # noqa: BLE001 — logged by _respawn
                pass
        return self.gen != gen

    async def _respawn(self, err: BaseException | None) -> None:
        logger.warning("%s MCP connection is dead (%s); respawning the server",
                       self.svc, type(err).__name__ if err else "?")
        for _ in range(5):                    # once per nested enter; normally one
            if not self.raw.is_running:
                break
            try:
                await asyncio.wait_for(self.raw.__aexit__(None, None, None), 15)
            except Exception:  # noqa: BLE001 — the old process is gone; teardown may complain
                pass
        try:
            await asyncio.wait_for(self.raw.__aenter__(), 30)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s MCP respawn failed: %s", self.svc, e)
            raise
        self.gen += 1
        logger.info("%s MCP respawned", self.svc)

    async def direct_call_tool(self, name: str, args: dict, *a, **kw):
        gen = self.gen
        try:
            return await self.raw.direct_call_tool(name, args, *a, **kw)
        except Exception as e:  # noqa: BLE001
            if _dead_connection(e) and await self.recover(gen, e):
                return await self.raw.direct_call_tool(name, args, *a, **kw)
            raise

    async def read_resource(self, uri: str):
        gen = self.gen
        try:
            return await self.raw.read_resource(uri)
        except Exception as e:  # noqa: BLE001
            if _dead_connection(e) and await self.recover(gen, e):
                return await self.raw.read_resource(uri)
            raise


@dataclass
class ResilientToolset(WrapperToolset):
    """A dead MCP server yields zero tools instead of a dead assistant.

    Agent.__aenter__ normally raises if any MCP server refuses connections,
    killing the whole voice worker. This wrapper absorbs enter failures,
    serves an empty tool dict while the server is down, and retries the
    connection once per run so the tools come back on their own.
    """

    svc: str = ""
    guard: MCPGuard | None = None             # stdio servers: respawn when dead
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
        gen = self.guard.gen if self.guard else 0
        try:
            return await self.wrapped.get_tools(ctx)
        except Exception as e:  # noqa: BLE001
            if self.guard and _dead_connection(e) and await self.guard.recover(gen, e):
                try:
                    return await self.wrapped.get_tools(ctx)
                except Exception as e2:  # noqa: BLE001
                    e = e2
            logger.warning("%s MCP lost (%s); dropping its tools", self.svc, e)
            self._up = False
            try:
                await self.wrapped.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            return {}

    async def call_tool(self, name, tool_args, ctx, tool):
        """A tool call on a dead server respawns it and retries once; if
        that fails too, the model hears an outage, not a crashed turn."""
        gen = self.guard.gen if self.guard else 0
        try:
            return await super().call_tool(name, tool_args, ctx, tool)
        except Exception as e:  # noqa: BLE001
            if self.guard is None or not _dead_connection(e):
                raise
            if await self.guard.recover(gen, e):
                return await super().call_tool(name, tool_args, ctx, tool)
            self._up = False
            return _OUTAGE.get(self.svc, f"The {self.svc} server is unavailable right now — say so.")

MAX_TURN_PAIRS = 12
MAX_HISTORY_TOKENS = 6_000          # estimated: chars//4 for text, chars//3 for tool returns; digests keep it rarely binding
KEEP_TOOL_RESULTS_FOR = 1           # the finished exchange keeps its last raw payload; the rest are digests
DIGEST_ROWS = 3                     # rows an abridged earlier result keeps
DIGEST_ROWS_LAST = 8                # … and the exchange's last result, which follow-ups refer to

# The per-turn context (clock, on-screen line, memory notes) rides at the top
# of the viewer's message, under this header, instead of inside the system
# prompt: the system prompt is then byte-identical for every request of a
# deployment and the server's prefix cache covers it and the whole history —
# llama.cpp re-processed everything after the first changed line (6–11k
# tokens a request on the 9B). Qwen's template allows one system message,
# which is why the block is in the user turn.
CONTEXT_HEADER = "[context for this turn — not spoken by the viewer]"
CONTEXT_END = "[end of context]"


def strip_context(text: str) -> str:
    """The viewer's words without the context block (for logs, memory, judges).
    The words come first and the block after them, so a model that reads the
    opening of the message as the request — as every training example had
    it — is not answering the clock or the screen line."""
    i = text.find("\n\n" + CONTEXT_HEADER)
    return text[:i] if i >= 0 else text


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


# Set by the LiveKit adapter for each run: an async callable that waits until
# the viewer's turn is committed (True) or gives up (False). LiveKit's
# preemptive generation starts the model on a partial transcript; a tune or
# play made then cannot be undone if the transcript changes. Unset (brain
# checks, benchmark, warmup) → commands run at once.
RUN_GATE: contextvars.ContextVar = contextvars.ContextVar("RUN_GATE", default=None)


async def _turn_committed() -> bool:
    gate = RUN_GATE.get()
    return True if gate is None else await gate()


@dataclass
class GatedToolset(WrapperToolset):
    """TV commands wait for the committed turn; lookups (get_state) don't."""

    async def call_tool(self, name, tool_args, ctx, tool):
        if not name.endswith("get_state") and not await _turn_committed():
            return f"{name} not run: the request changed before it was confirmed."
        return await super().call_tool(name, tool_args, ctx, tool)


def _static_instructions(backend: str, player: str) -> str:
    text = load_static_instructions(backend, player=player)
    import hashlib
    import rollouts
    rollouts.PROMPT_HASH = hashlib.sha256(text.encode()).hexdigest()[:12]
    return text


def _stdio_toolsets(cfg_path: str, read_timeout: float) -> list:
    """load_mcp_toolsets() with a request timeout: its default is 5 minutes,
    so a stalled guide or TV server froze a voice turn (and its fan-outs)
    for that long. One prefixed MCPToolset per server, as load_mcp_toolsets."""
    from pydantic_ai.mcp import StdioTransport
    servers = json.loads(Path(cfg_path).read_text())["mcpServers"]
    out = []
    for name, cfg in servers.items():
        env = {k: os.path.expandvars(v) for k, v in (cfg.get("env") or {}).items()}
        transport = StdioTransport(command=cfg["command"], args=list(cfg.get("args") or []),
                                   env=env or None, cwd=cfg.get("cwd"))
        out.append(MCPToolset(transport, id=name, init_timeout=10.0,
                              read_timeout=read_timeout).prefixed(name))
    return out


def build_agent(primers: list | None = None, hooks: dict | None = None) -> Agent:
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
    #   minny: the minny-client reference viewer on Miro TV, through
    #     minny-tv-mcp, one long-lived MCP-over-HTTP service at TV_MCP_URL
    #     (minny-client docs/voice-agent-switch.md). It keeps ytvn's contract —
    #     same tools and viewer:// resources — so it gets the same resolvers and
    #     lineup scoping below (the `ytvn` guard stands for either), plus a second
    #     guide server for partner apps (mcp-minny.json).
    player = os.environ.get("VOICE_TV", "tvshell")
    ytvn = None
    ott = None                               # minny: the partner-app guide server's guard
    scopes: list = []                        # LineupScopes (ytvn/minny + Minny guide)
    if player == "ytvn":
        # The model never sees the raw-id tune (guide ids are the catalogue's,
        # not the player's) nor play_url: it watches by name through the
        # resolver tools below, which call the server directly. play_asset
        # stays for picking one of several titles the resolver listed.
        # 20 s: play_airing waits on the page (relay pre-flight, one retry).
        prefixed = _stdio_toolsets(
            str(Path(__file__).parent / "mcp-tv-ytvn.json"), read_timeout=20.0)[0]
        ytvn = MCPGuard(prefixed.wrapped, "tv")
        tv = ResilientToolset(
            GatedToolset(prefixed.filtered(lambda ctx, td: td.name not in
                                           {"tv_tune_channel", "tv_tune_number", "tv_play_url"})),
            svc="tv", guard=ytvn)
    elif player == "minny":
        # 25 s: play_airing resolves through Miro TV's policy manager; the remote
        # relay allows a command 20 s and minny-tv-mcp waits 22 s.
        raw_tv = MCPToolset(os.environ["TV_MCP_URL"],
                            cache_tools=True,
                            init_timeout=10.0,
                            read_timeout=25.0,
                            tool_error_behavior="failed")
        ytvn = MCPGuard(raw_tv, "tv")
        tv = ResilientToolset(
            GatedToolset(raw_tv.prefixed("tv").filtered(lambda ctx, td: td.name not in MINNY_TV_HIDDEN)),
            svc="tv", guard=ytvn)
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
        # The minny player has two guide servers (mcp-minny.json): "minny" on Miro
        # TV's operator, "minny_ott" on the partner-app catalogue (Ecuador's
        # providers only).
        minny_cfg = str(Path(__file__).parent / ("mcp-minny.json" if player == "minny" else "mcp.json"))
        # 10 s: ranked guide searches measure up to ~6 s.
        tv_search = _stdio_toolsets(minny_cfg, read_timeout=10.0)
        guards = [MCPGuard(ts.wrapped, "minny") for ts in tv_search]
        for g in guards:
            # Starts the server and its catalogue client, and checks the
            # catalogue answers — what the model used to spend a round trip on.
            primers.append(lambda raw=g:
                           raw.direct_call_tool("cluster_status", {}))
        if player in ("ytvn", "minny"):
            # The guide covers the operator's whole catalogue; the TV carries one
            # lineup. Keep guide answers to the TV's channels.
            from lineup_scope import LineupScope, LineupScopedToolset
            scoped = []
            for ts, g in zip(tv_search, guards):
                if getattr(ts, "prefix", "") == "minny_ott":
                    # Partner apps: no channels to scope, only its on-demand search.
                    ott = g
                    scoped.append(ts.filtered(lambda ctx, td: td.name == "minny_ott_search_vod"))
                    continue
                sc = LineupScope(g)
                scopes.append(sc)
                # On-demand titles come from the TV's own catalogue (tv_on_demand,
                # tv_watch_title): the guide's VOD index is the operator's, with
                # ids and titles this TV may not have.
                scoped.append(LineupScopedToolset(ts, scope=sc).filtered(
                    lambda ctx, td: td.name != "minny_search_vod"))
            primers.extend(sc.channels for sc in scopes)
            tv_search = scoped
        tv_search = [ResilientToolset(ts, svc="minny", guard=g)
                     for ts, g in zip(tv_search, guards)]
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
        instructions=_static_instructions(backend, player),
        toolsets=[*tv_search, tv],
        model_settings=settings,
        deps_type=TurnDeps,
    )

    hooks = hooks if hooks is not None else {}
    hooks["time"] = lambda: format_time_context(tz=tz, backend=backend)   # VoiceBrain.user_prompt reads these

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
            if not await _turn_committed():
                return f"{name} failed: the request changed before it was confirmed"
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
            try:
                live, down = await _airing_now(channel), None
            except GuideDown as e:
                live, down = [], e
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
            if down:
                return (f"{channel!r} is not a channel name in this TV's lineup, and the "
                        f"guide could not be checked for a programme by that name: {down}")
            return f"{channel!r} is not a channel on this TV."

        @agent.tool_plain
        async def tv_watch_title(title: str, year: int | None = None,
                                 channel: str | None = None,
                                 catchup: bool = False) -> str:
            """Play, watch or catch up on a show, film or series BY NAME — the
            one call for a named programme to catch up on even when a channel
            is named too ("catch up on Face the Nation on channel 5" →
            title="Face the Nation", channel="channel 5", catchup=True). A
            plain "play X on <channel>" with no catch-up wording is a live
            request: tv_watch_channel(channel). title: its essential words; year:
            release year if known; channel: the channel the viewer named, if
            any (name or number). In one call it plays the on-demand title,
            or tunes the channel airing it now, or plays it as catch-up if it
            aired on this TV (on that channel, if given) in the last 24
            hours. When several titles match it lists them with asset ids —
            ask the viewer, then call tv_play_asset(asset_id). catchup=True
            when the viewer asked for catch-up / an earlier or "yesterday's"
            edition / from the beginning: skips on demand, and a programme
            on now is restarted instead of tuned mid-programme."""
            return await _watch_title(title, year, channel, retry=True, catchup=catchup)

        async def _watch_title(title: str, year: int | None, channel: str | None,
                               retry: bool, catchup: bool = False) -> str:
            only = None
            if channel:
                hit, _ = await pr.find_channel(channel)
                only = hit.name if hit else None
            # The three lookups are independent: run them together (they
            # took ~4 s in sequence, and viewers spoke again before an answer).
            got = await asyncio.gather(
                pr.search_titles(title, year) if not catchup else _none(),
                _airing_now(title, only) if not year else _none(),
                _aired_recently(title, only) if not year else _none(),
                return_exceptions=True)
            down = next((g for g in got if isinstance(g, GuideDown)), None)
            for g in got:
                if isinstance(g, BaseException) and not isinstance(g, GuideDown):
                    raise g
            titles, live, recent = ([] if isinstance(g, GuideDown) else g for g in got)
            pick = pr.pick_title(title, titles)
            if pick:
                return await _control("play_asset", {"asset_id": pick.asset_id})
            if titles:
                return ("Not playing — several titles match; ask which: "
                        + "; ".join(f"{t.title} ({t.year or 'year ?'}) "
                                    f"asset_id={t.asset_id}" for t in titles))
            if not year and catchup and len(live) == 1 and live[0].get("airing_id"):
                # "Catch up on X" while X is on: from its beginning, not live
                # mid-programme.
                r = live[0]
                ok, played = await _control_ok("play_airing",
                                               {"airing_id": r["airing_id"], "mode": "restart"})
                if ok:
                    return (f"{r['title']!r} is on now on {r['channel_name']} — restarted "
                            f"from its beginning: {played}")
            if not year:
                # "Put on Trace" is a channel name first: "SOUNDS OF TRACE" on
                # TRACE URBAN matched the word and tuned it instead of asking
                # which Trace. A programme still wins when its title is exactly
                # what was said.
                hit, cands = await pr.find_channel(title)
                said = pr.squash(title)
                if (hit or cands) and not any(
                        pr.squash(r.get("title", "")) == said for r in live):
                    return (f"No on-demand title {title!r}, but it is a live "
                            "channel — " + await tv_watch_channel(title))
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
                # Only when the first word sounds like a misheard "play" — a
                # looser retry could otherwise play an unrelated programme.
                words = title.replace(",", " ").split()
                if retry and len(words) >= 3 and _MISHEARD_PLAY.match(words[0]):
                    rest = " ".join(words[1:])
                    return (f"(nothing matched {title!r}; tried {rest!r}) "
                            + await _watch_title(rest, year, channel, retry=False,
                                                 catchup=catchup))
            where = f" on {only}" if only else ""
            # Not on Miro TV at all: a partner app may carry it (minny player).
            partner = await _partner_title(title, year)
            if partner:
                return partner
            if year:
                return (f"No on-demand title matching {title!r} ({year}) on this TV "
                        "(live and catch-up were not checked for a dated film).")
            if down:
                return (f"No on-demand title matching {title!r} on this TV; whether it is "
                        f"on live or was in the last 24 hours could not be checked: {down}")
            return (f"No on-demand title matching {title!r} on this TV, and it "
                    f"isn't on live right now or in the last 24 hours{where}. "
                    "Say so; don't guess when it is on.")

        async def _none() -> list:
            return []

        async def _partner_title(title: str, year: int | None) -> str | None:
            """A title on a partner app (Netflix, Disney+, Prime Video) offered
            here: its details screen opened on the TV, and where it streams.
            None when there is no partner catalogue or no matching title."""
            if ott is None:
                return None
            from lineup_scope import _rows
            try:
                rows = _rows(await ott.direct_call_tool(
                    "search_vod", {"query": title, **({"year": year} if year else {})})) or []
            except Exception:  # noqa: BLE001 — no partner answer: say what Miro TV has
                return None
            pick = next((r for r in rows if r.get("details_uri") and _title_matches(title, r)), None)
            if pick is None:
                return None
            apps = [PARTNER_APPS.get(sv, sv) for sv in pick.get("services") or []] or ["a partner app"]
            name = pick.get("series_title") or pick.get("title") or title
            ok, state = await _control_ok("open_details", {"uri": pick["details_uri"]})
            if not ok:
                return (f"{name!r} is not on Miro TV; it streams on {apps[0]}, but its details "
                        f"could not be opened on the TV: {state}")
            return (f"{name!r} is not on Miro TV; it streams on {' and '.join(apps)}. Its details "
                    f"are now open on the TV: the viewer presses \"Abrir en {apps[0]}\" to watch "
                    "it in that app. Say so in one or two sentences.")

        def _title_matches(title: str, row: dict) -> bool:
            """Every spoken word is in the row's title/series — compared
            without spaces, so 'PBS NewsHour' matches 'PBS News Hour'."""
            words = [_SAME_WORD.get(w, w) for w in pr._norm(pr.split_caps(title)).split() if len(w) > 1]
            hay = pr.squash(f"{row.get('title', '')} {row.get('series_title') or ''}")
            spoken = pr.squash(title)
            if not words:
                return False
            names = [_SAME_WORD.get(w, w)
                     for w in pr._norm(f"{row.get('title', '')} {row.get('series_title') or ''}").split()]
            return all(w in names for w in words) or (len(spoken) >= 6 and spoken in hay)

        async def _aired_recently(title: str, only: str | None = None) -> list[dict]:
            """Airings of `title` on the TV's channels that ENDED in the last
            24 hours (the catch-up window), newest first, one per channel."""
            if not scopes:
                return []
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            # The guide's query needs every word: "PBS NewsHour" is stored as
            # "PBS News Hour" but "SportsCenter" as one word — ask both ways.
            queries = list(dict.fromkeys([title, pr.split_caps(title)]))
            window = {"window": "range",
                      "start": (now - timedelta(hours=24)).isoformat(timespec="seconds"),
                      "end": now.isoformat(timespec="seconds")}

            failed: list[BaseException] = []

            async def one(q: str) -> list:
                try:
                    raw = await scopes[0].minny.direct_call_tool(
                        "search_live_schedule", {"query": q, **window})
                    got = await scopes[0].filter("minny_search_live_schedule", raw)
                except Exception as e:  # noqa: BLE001
                    failed.append(e)
                    return []
                return got if isinstance(got, list) else []

            rows = [r for rs in await asyncio.gather(*(one(q) for q in queries)) for r in rs]
            if len(failed) == len(queries):
                raise GuideDown(_OUTAGE["minny"]) from failed[0]
            out, seen = [], set()
            from lineup_scope import start_key
            for r in sorted(rows, key=start_key, reverse=True):
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
            except Exception as e:  # noqa: BLE001
                raise GuideDown(_OUTAGE["minny"]) from e
            if isinstance(rows, str):
                # The scope answers in words when it has no rows: "lists nothing
                # on air … the guide is reachable" is an answer; anything else
                # ("could not be checked", "unavailable") is an outage.
                if "the guide is reachable" in rows:
                    return []
                raise GuideDown(rows)
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
            if titles is None or (not titles and not cats):
                return "The on-demand catalogue is unavailable right now."
            if not titles:
                return (f"No on-demand category like {category!r}. Categories: "
                        + ", ".join(cats))
            head = f"On demand — {category or 'New Releases'}: "
            return (head + "; ".join(f"{t.title} ({t.year or 'year ?'}) asset_id={t.asset_id}"
                                     for t in titles)
                    + ". Other categories: " + ", ".join(c for c in cats[:20]))

        async def read_screen() -> str:
            """What the TV shows, fresh per turn from the MCP server's cached
            viewer state (no round trip to the page). It carries the
            airing_id of the programme on screen, so "restart this" or "what
            am I watching" need no lookup — older tool results are abridged
            in the history after two exchanges."""
            gen = ytvn.gen
            try:
                raw = await asyncio.wait_for(ytvn.raw.read_resource("viewer://state"), 0.3)
                st = json.loads(raw if isinstance(raw, str) else str(raw))
            except Exception as e:  # noqa: BLE001 — no page / slow: just omit the line
                ytvn.kick(gen, e)             # a dead server respawns in the background
                return ""
            return format_screen(st)
        hooks["screen"] = read_screen

    @agent.tool_plain
    async def web_lookup(query: str) -> str:
        """Look up background about a programme, film, person, or event on
        the web: plot, cast, year, series context, reviews, real-world
        facts. For a specific airing, read its guide row first and use
        this only for what the row lacks (or when its description is
        thin). NOT for what is on TV, when
        something airs, or the viewer's own preferences."""
        from websearch import search
        return await search(query)

    @agent.tool
    async def search_memory(ctx: RunContext[TurnDeps], query: str) -> str:
        """Search long-term memory of past conversations and household
        preferences. Not for TV schedules."""
        if ctx.deps.memory is None:
            return "No stored memories match that."
        # Embedding + graph round trips with no bound of their own (the
        # OpenAI SDK waits up to 600 s): the profile is already in the prompt.
        try:
            facts = await asyncio.wait_for(ctx.deps.memory.deep_search(query, limit=8), 3.0)
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            logger.warning("search_memory: %s", e or "timeout")
            return ("Memory search is unavailable right now — answer from the "
                    "household profile above, or say you can't recall.")
        return "\n".join(f"- {f}" for f in facts) or "No stored memories match that."

    return agent


class VoiceBrain:
    """Agent + rolling history + memory handle, shared across turns."""

    def __init__(self, memory: "VoiceMemory | None" = None) -> None:
        self._primers: list = []
        self._hooks: dict = {}
        self.agent = build_agent(self._primers, self._hooks)
        self.memory = memory
        self.history: list[ModelMessage] = []

    async def user_prompt(self, say: str, deps: "TurnDeps") -> str:
        """The viewer's words with the turn's context block on top: clock,
        on-screen line, memory notes. Every run goes through here, so the
        system prompt stays static (see CONTEXT_HEADER)."""
        lines = [CONTEXT_HEADER]
        time_hook = self._hooks.get("time")
        if time_hook:
            lines.append(time_hook())
        screen_hook = self._hooks.get("screen")
        if screen_hook:
            try:
                line = await screen_hook()
            except Exception:  # noqa: BLE001  the hook already logs and respawns
                line = ""
            if line:
                lines.append(line)
        mem = format_memory_context(deps.facts_profile, deps.facts_recent)
        if mem:
            lines.append(mem)
        block = "\n".join(ln.strip("\n") for ln in lines if ln)
        block = re.sub(r"\n{2,}", "\n", block)       # one blank line separates the words from the block
        return f"{say}\n\n{block}\n{CONTEXT_END}"

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
    r"(great|perfect|cool|nice|awesome|excellent|wonderful|good),? (thanks|thank you)|"
    r"thanks?( a lot| so much| very much)?|thank you( very much| so much)?)"
    r"([, ]+(minny|minnie))?[.!]*$", re.I)


# A bare greeting heard right after Minny spoke is her own echo (a phantom
# "Hi Minny." was transcribed 42 times within 4 s of a reply). Shared by the
# LiveKit hook (which stops the reply) and the model adapter (which must not
# run, commit or trace the preemptive turn LiveKit already started for it).
BARE_GREETING = re.compile(
    r"^(hi|hey|hello|hei|ok|okay)?[, ]*(minny|minnie|mini|minni|winnie|wanda)[.!?]*$", re.I)
GREETING_ECHO_S = 4.0


def is_echo_greeting(text: str, last_reply_at: float, now: float,
                     last_reply: str = "") -> bool:
    t = text.strip()
    if not BARE_GREETING.match(t):
        return False
    # "Okay Minny." right after "…want me to put it on?" answers the
    # question; the echo filter swallowed it and the offer was never acted on.
    if re.match(r"(ok|okay)\b", t, re.I) and last_reply.rstrip().endswith("?"):
        return False
    return now - last_reply_at < GREETING_ECHO_S


# Words the recognizer has produced for a leading "play" ("Clay, the
# Farmer and the Foodie").
_MISHEARD_PLAY = re.compile(r"^(play|clay|plate?|played|lay|pay|pray|blay|place|put|please)[,.]?$", re.I)


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

    # Abridge an exchange's tool payloads once, at its own commit (the tail
    # of the history): its last result to DIGEST_ROWS_LAST rows with airing
    # ids — what "the second one" or "what's after that" refer to — the
    # others to DIGEST_ROWS. Older exchanges are never touched again, so the
    # server's prefix cache keeps the whole history up to the finished
    # exchange; a rewrite in the middle (or a dropped head) costs it all.
    kept_starts = [i for i, m in enumerate(kept) if _is_user_request(m)]
    last_start = kept_starts[-1] if kept_starts else 0
    last_result = max((i for i, m in enumerate(kept) if i >= last_start and isinstance(m, ModelRequest)
                       and any(isinstance(p, ToolReturnPart) for p in m.parts)), default=None)
    out: list[ModelMessage] = []
    for i, msg in enumerate(kept):
        if isinstance(msg, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in msg.parts):
            keep = DIGEST_ROWS_LAST if i == last_result else DIGEST_ROWS
            parts = [_replace(p, content=_digest(p.content, keep))
                     if isinstance(p, ToolReturnPart) and not str(p.content).startswith("(earlier result") else p
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
            n = len(str(content))
        except Exception:  # noqa: BLE001
            n = 400
        # JSON rows (digits, quotes, braces) tokenise at ~3 chars a token, prose at ~4
        total += n // 3 if isinstance(p, ToolReturnPart) else n // 4
    return total


def _digest(content, keep: int = DIGEST_ROWS) -> str:
    """An earlier tool result, abridged for the history: a listing keeps its
    first `keep` rows as title · channel · start · airing id, in order, so
    "the second one" or "what's after that" still resolve and a pick can
    still be played; anything else keeps its first 200 chars."""
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    rows = None
    try:
        d = json.loads(text)
        rows = d if isinstance(d, list) else d.get("rows") if isinstance(d, dict) else None
    except (ValueError, AttributeError):
        pass
    if isinstance(rows, list) and rows and all(isinstance(r, dict) for r in rows):
        head = "; ".join(" · ".join(str(r.get(k)) for k in ("title", "channel_name", "start_local", "airing_id") if r.get(k))
                         for r in rows[:keep])
        more = f"; … and {len(rows) - keep} more" if len(rows) > keep else ""
        return f"(earlier result, abridged) {head}{more}"
    return "(earlier result, abridged) " + text[:200] + ("…" if len(text) > 200 else "")
