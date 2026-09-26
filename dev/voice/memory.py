"""Long-term memory for the standalone voice agent.

graphiti-core IN-PROCESS against the falkordb-voice container (loopback
:6390), graph/group "voice_memory" — one string used as BOTH the driver
database and the group_id on every call, so graphiti's add_episode
driver-clone mutation can never re-bind us to another graph.

Hot path contract (see the plan):
- recall() is awaited every turn under a 0.25 s cap in the worker: profile
  facts come from an in-process cache (zero I/O), recent episodes from one
  small DB read capped at 0.2 s. No embedding calls.
- remember() only enqueues; a single background consumer runs add_episode
  (the expensive LLM-extraction path) one at a time, per upstream guidance.
  Memory failures degrade to dropped writes — the voice path never blocks.
- deep_search() backs the agent's search_memory tool: RRF recipe (no LLM
  rerank), ~0.7 s, paid only when the model asks for it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Importing installs FalkorDriver.search_interface as a CLASS attribute
# (reaches per-group driver clones too). Without it, similarity search is
# >1000x slower and edge fulltext search times out. Vendored verbatim from
# ~/opt/graphiti/mcp_server/src/utils/falkor_fast_search.py.
import falkor_fast_search  # noqa: F401

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.nodes import EpisodeType
from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
from graphiti_core.search.search_filters import (
    ComparisonOperator,
    DateFilter,
    SearchFilters,
)

logger = logging.getLogger("voice.memory")

VOICE_MEMORY_GROUP = "voice_memory"
PROFILE_QUERY = ("user preferences favorite channels shows genres "
                 "household routine standing instructions")

_SKIP_ACK = re.compile(
    r"^(ok(ay)?|thanks?( a lot)?|thank you|yes|no|yeah|nope|sure|"
    r"stop|cancel|never ?mind|got it)[.!]?$", re.I)
_SKIP_CONTROL = re.compile(
    r"^(turn (it|the tv) (up|down|off)|volume\b|mute\b|unmute\b|"
    r"pause\b|play\b|resume\b|louder|quieter|"
    r"(turn )?captions?\b|open the guide|close the guide)", re.I)
_KEEP = re.compile(
    r"\b(always|never|remember|forget|favou?rite|prefer|"
    r"i (like|love|hate|watch)|my |our |we |usually|"
    r"every (night|morning|day|week(end)?))\b", re.I)


def _worth_remembering(user_text: str, assistant_text: str,
                       interrupted: bool) -> bool:
    t = user_text.strip()
    if len(t) < 8 or _SKIP_ACK.match(t):
        return False
    if not assistant_text.strip():
        return False                       # nothing was spoken back
    if interrupted and len(assistant_text) < 40:
        return False                       # barge-in fragment
    if _KEEP.search(t):
        return True                        # standing instructions always kept
    if _SKIP_CONTROL.match(t):
        return False                       # transient device twiddling
    return True                            # incl. tune-by-name: repetition IS the signal


@dataclass
class RecallResult:
    profile: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)


class VoiceMemory:
    def __init__(self, graphiti: Graphiti, driver: FalkorDriver) -> None:
        self._graphiti = graphiti
        self._driver = driver
        self._profile: list[str] = []
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.profile_ready: asyncio.Task | None = None
        self._consumer: asyncio.Task | None = None
        self._closing = False
        self._last_enqueued_user = ""

    @classmethod
    async def create(cls, *, host: str = "127.0.0.1", port: int = 6390,
                     password: str | None = None) -> "VoiceMemory":
        os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
        driver = FalkorDriver(host=host, port=port, password=password,
                              database=VOICE_MEMORY_GROUP)
        # graphiti's defaults (gpt-5.5 + small gpt-4.1-nano) assume model
        # access this OpenAI project may not have — pin both explicitly.
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_client import OpenAIClient
        llm_config = LLMConfig(
            model=os.environ.get("MEMORY_LLM_MODEL", "gpt-5-mini"),
            small_model=os.environ.get("MEMORY_LLM_SMALL", "gpt-4.1-mini"))
        graphiti = Graphiti(graph_driver=driver,
                            llm_client=OpenAIClient(config=llm_config))
        # The driver only schedules index creation when built inside a
        # running loop, and silently skips it otherwise — be explicit.
        await driver.build_indices_and_constraints()
        self = cls(graphiti, driver)
        self._consumer = asyncio.create_task(self._consume(), name="memory-writer")
        self.profile_ready = asyncio.create_task(
            self._refresh_profile(), name="memory-profile-warmup")
        return self

    # ── hot path ──────────────────────────────────────────────────────

    async def recall(self, user_text: str) -> RecallResult:
        """<50 ms typical. user_text deliberately unused (no embedding on
        the hot path); query-specific lookups are deep_search's job."""
        recent: list[str] = []
        try:
            episodes = await asyncio.wait_for(
                self._graphiti.retrieve_episodes(
                    reference_time=datetime.now(timezone.utc),
                    last_n=4, group_ids=[VOICE_MEMORY_GROUP]),
                timeout=0.2)
            for ep in reversed(episodes):   # most recent first
                body = " / ".join(str(ep.content or "").split("\n"))
                recent.append(body)
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            if not isinstance(e, asyncio.TimeoutError):
                logger.debug("recent-episode fetch failed: %s", e)
        return RecallResult(profile=list(self._profile), recent=recent)

    def remember(self, user_text: str, assistant_text: str, *,
                 interrupted: bool = False,
                 ts: datetime | None = None) -> None:
        if self._closing:
            return
        if not _worth_remembering(user_text, assistant_text, interrupted):
            return
        if user_text.strip() == self._last_enqueued_user:
            return                           # STT repeats
        self._last_enqueued_user = user_text.strip()
        try:
            self._queue.put_nowait(
                (user_text.strip(), assistant_text.strip(),
                 ts or datetime.now(timezone.utc)))
        except asyncio.QueueFull:
            logger.warning("memory queue full — dropping episode")

    # ── deep recall (agent tool) ──────────────────────────────────────

    async def deep_search(self, query: str, limit: int = 8) -> list[str]:
        cfg = EDGE_HYBRID_SEARCH_RRF.model_copy(deep=True)
        cfg.limit = limit
        results = await self._graphiti.search_(
            query=query, config=cfg, group_ids=[VOICE_MEMORY_GROUP],
            search_filter=_current_facts_filter())
        return _format_edges(results.edges)

    # ── background ────────────────────────────────────────────────────

    async def _consume(self) -> None:
        while True:
            user_text, assistant_text, ts = await self._queue.get()
            try:
                await asyncio.wait_for(
                    self._graphiti.add_episode(
                        name=f"voice {ts.isoformat(timespec='seconds')}",
                        episode_body=(f"user: {user_text}\n"
                                      f"assistant: {assistant_text[:1000]}"),
                        source=EpisodeType.message,
                        source_description="TV voice assistant conversation",
                        reference_time=ts,
                        group_id=VOICE_MEMORY_GROUP),
                    timeout=180)
                await self._refresh_profile()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("memory write dropped: %s", e)
                await asyncio.sleep(5)      # no hot loop when the DB is down
            finally:
                self._queue.task_done()

    async def _refresh_profile(self) -> None:
        try:
            cfg = EDGE_HYBRID_SEARCH_RRF.model_copy(deep=True)
            cfg.limit = 12
            results = await self._graphiti.search_(
                query=PROFILE_QUERY, config=cfg,
                group_ids=[VOICE_MEMORY_GROUP],
                search_filter=_current_facts_filter())
            self._profile = _format_edges(results.edges)
        except Exception as e:  # noqa: BLE001
            logger.debug("profile refresh failed (keeping old cache): %s", e)

    async def aclose(self, flush_timeout: float = 30.0) -> None:
        self._closing = True
        try:
            await asyncio.wait_for(self._queue.join(), flush_timeout)
        except asyncio.TimeoutError:
            logger.warning("memory flush timed out; %d episodes unwritten",
                           self._queue.qsize())
        if self._consumer:
            self._consumer.cancel()
        await self._graphiti.close()


def _current_facts_filter() -> SearchFilters:
    """Only facts that are still true.

    Graphiti never deletes: a correction ("Columbo is my favorite now")
    stamps the old edge with invalid_at/expired_at. Searching without this
    filter serves superseded facts back into the prompt — which is how
    Minny kept offering "your usual Al Jazeera" for days after being told
    to drop it. Filtering in the query (not afterwards) keeps the result
    limit filled with live facts.
    """
    now = datetime.now(timezone.utc)
    return SearchFilters(
        # invalid_at unset, or set in the future (fact still in force)
        invalid_at=[
            [DateFilter(comparison_operator=ComparisonOperator.is_null)],
            [DateFilter(date=now,
                        comparison_operator=ComparisonOperator.greater_than)],
        ],
        expired_at=[
            [DateFilter(comparison_operator=ComparisonOperator.is_null)],
        ],
    )


def _is_current(e, now: datetime) -> bool:
    # Belt and braces for the query filter: FalkorDB stores these as ISO
    # strings, and a driver/filter regression must not resurrect dead facts.
    if getattr(e, "expired_at", None) is not None:
        return False
    inv = getattr(e, "invalid_at", None)
    if inv is None:
        return True
    if isinstance(inv, str):
        try:
            inv = datetime.fromisoformat(inv)
        except ValueError:
            return False
    if inv.tzinfo is None:
        inv = inv.replace(tzinfo=timezone.utc)
    return inv > now


def _format_edges(edges) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    now = datetime.now(timezone.utc)
    for e in edges:
        if not _is_current(e, now):
            continue
        fact = " ".join(str(getattr(e, "fact", "") or "").split())
        if not fact or fact.casefold() in seen:
            continue
        seen.add(fact.casefold())
        valid_at = getattr(e, "valid_at", None)
        if valid_at:
            fact = f"{fact} (as of {valid_at:%b %d %Y})"
        out.append(fact[:200])
    return out
