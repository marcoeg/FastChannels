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

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPToolset
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


def build_agent() -> Agent:
    epg = ResilientToolset(
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
        svc="epg")
    tv = ResilientToolset(
        MCPToolset(os.environ["TV_MCP_URL"],
                   cache_tools=True,
                   init_timeout=5.0,
                   read_timeout=5.0,
                   tool_error_behavior="failed")
        .prefixed("tv"),
        svc="tv")

    settings = OpenAIResponsesModelSettings(
        openai_reasoning_effort=os.environ.get("VOICE_REASONING_EFFORT", "none"),
        openai_service_tier="priority",
        openai_reasoning_context="current_turn",
        parallel_tool_calls=True,
        max_tokens=700,                            # spoken answers are short
    )

    agent = Agent(
        OpenAIResponsesModel(os.environ.get("VOICE_MODEL", "gpt-5.6-luna")),
        instructions=load_static_instructions(),
        toolsets=[epg, tv],
        model_settings=settings,
        deps_type=TurnDeps,
    )

    @agent.instructions
    def time_context(ctx: RunContext[TurnDeps]) -> str:
        return format_time_context()

    @agent.instructions
    def memory_context(ctx: RunContext[TurnDeps]) -> str:
        return format_memory_context(ctx.deps.facts_profile,
                                     ctx.deps.facts_recent)

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
