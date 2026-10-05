"""Long-term memory for the standalone voice agent.

graphiti-core IN-PROCESS against the falkordb-voice container (loopback
:6390), graph/group "voice_memory" — one string used as BOTH the driver
database and the group_id on every call, so graphiti's add_episode
driver-clone mutation can never re-bind us to another graph.

Hot path contract:
- recall() is awaited every turn under a 0.25 s cap in the worker and does
  no I/O: profile facts come from an in-process cache, earlier-session
  exchanges were read from the transcript log once at session start.
- remember() only enqueues, and only statements of preference, habit or
  standing instruction (wants_memory) — questions, tuning and small talk
  never reach graphiti. A single background consumer runs add_episode (the
  expensive LLM-extraction path) one at a time, with extraction
  instructions that keep household facts only. Failures degrade to dropped
  writes — the voice path never blocks.
- deep_search() backs the agent's search_memory tool: RRF recipe (no LLM
  rerank), ~0.7 s, paid only when the model asks for it.
- prune() (retention) runs in the background once per session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

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
TEST_MEMORY_GROUP = "voice_memory_test"
# TV settings with one value at a time (VoiceMemory._supersede_settings).
SINGLE_VALUE_SETTINGS = ("volume", "caption", "subtitle", "language")
# Written by `memory_cli.py --test wipe`: test sessions ignore transcript rows
# older than it, so a reset test run starts with no "earlier sessions" too.
TEST_RESET_MARKER = Path.home() / "news" / "voice-test-reset"


def is_test_room(room: str | None) -> bool:
    """Test traffic: rooms named test-… (the phone client opened with ?test),
    voice_probe.py's voice-probe… rooms and the debug console. Their turns are traced but
    marked, their memory is a separate graph, and their exchanges never
    reach a real session's "earlier sessions" (nor the reverse)."""
    room = room or ""
    # `voice_agent.py console` runs in LiveKit's mock room "console": the
    # household graph got two facts typed there that had to be scrubbed.
    return room.startswith(("test-", "voice-probe", "console"))
# Episodes older than this are pruned unless they hold a lasting household
# fact (VoiceMemory.prune). VOICE_MEMORY_RETENTION_DAYS=0 disables pruning.
TRANSCRIPT_LOG = Path(os.environ.get(
    "VOICE_TRANSCRIPT_LOG", Path.home() / "news" / "voice-conversations.jsonl"))
PROFILE_TTL_S = 90.0
RETENTION_DAYS = float(os.environ.get("VOICE_MEMORY_RETENTION_DAYS", "14"))
PROFILE_QUERY = ("user preferences favorite channels shows genres "
                 "household routine standing instructions")

# Only statements ABOUT the household reach graphiti. Its extraction turns
# whatever it is given into facts — schedule data, suggestions, what the
# assistant did — and custom instructions only partly hold it back (tested:
# gpt-5-mini still filed "Assistant suggested Frasier", "Jojo Rabbit is on
# FX at 4:15"). So the gate is here, before any LLM cost: questions, tuning
# and small talk are not memory; the transcript log keeps them for context.
_PREFERENCE = re.compile(
    r"\b(remember|forget|favou?rite|prefer(red)?|"
    r"i (really |just |also )?(like|love|hate|enjoy|adore|can'?t stand|don'?t like)|"
    r"we (really |all )?(like|love|hate|enjoy|usually|always|never|watch)|"
    r"(i|we) (usually|always|never|normally)|(my|our) (usual|normal|default|regular)|"
    r"usually|every (night|morning|day|evening|weekend|week)|"
    r"(don'?t|do not|never) (ever )?(suggest|show|recommend|play|put on)\b|"
    r"my preference|(my|our) (wife|husband|partner|kids?|son|daughter|family) "
    r"(likes?|loves?|hates?|enjoys?|watch(es)?)|the kids (watch|like|love)|"
    r"(always|never) (set|put|play|suggest|show|turn)|"
    r"when i say)\b", re.I)
_QUESTION = re.compile(
    r"^(what|which|when|where|who|how|is|are|do|does|did|can|could|should|"
    r"will|would)\b.*\?*$", re.I)


_ASKING = re.compile(
    r"^((do )?you (know|remember)|tell me|can you tell|i wonder|any idea)\b", re.I)


def wants_memory(user_text: str) -> bool:
    """True for a statement of preference, habit or standing instruction.
    A question that merely mentions a favourite ("what's my favorite news
    channel?") asks memory; it doesn't add to it."""
    t = re.sub(r"^(hey |hi |ok )?(minny|minnie|wanda)[,.!]?\s*", "", user_text.strip(), flags=re.I)
    if len(t) < 8 or not _PREFERENCE.search(t):
        return False
    # A question is never a statement to store — whether it starts with a
    # question word or not ("You know what my preferred volume is?"), and
    # STT does not always add the question mark ("Do you remember my
    # favorite channel"). Checked before the remember/forget shortcut.
    if t.endswith("?") or _QUESTION.match(t) or _ASKING.match(t):
        return False
    if re.match(r"^(no,? )?(just )?forget (it|that|about it)[.!]?$", t, re.I):
        return False                          # a cancellation, not a request
    return True


# An assistant line that declined or failed. Replaying those as "earlier
# sessions" taught the model to repeat them after the capability arrived
# ("There isn't a BBC News catch-up recording available on this TV").
_REFUSAL = re.compile(
    r"\b(can'?t|cannot|couldn'?t|unable|isn'?t|aren'?t|wasn'?t|"
    r"there'?s no|there (is|are) no|not (available|in|on|supported|possible)|"
    r"no longer|sorry|doesn'?t have|don'?t have)\b", re.I)

# Profile facts must be about the household. Graphiti extracts facts from
# every exchange, so the store also holds facts about the assistant ("can
# tune channels"), about what was once on screen ("UEFA.tv is showing …"),
# about the guide's data, and meta chatter ("user asks what they would like
# to do") — the profile query ranks those in when real preferences are few.
_NOT_HOUSEHOLD = re.compile(
    r"^(the )?(assistant|minny|tv|guide|catalogue|channel|uefa|programme)\b|"
    r"^(the )?assistant \(|"
    r"\b(is|are) (showing|airing|on now)\b|"
    r"^(the )?lineup\b|"
    # one-off requests and actions are history, not who the household is
    r"^user (asks?|asked|requests?|requested|tuned|switched|is on|was on)\b", re.I)


# Passed to graphiti's entity AND fact extraction for every new episode:
# remember the household, not the session. Without it, every exchange
# became facts like "assistant can tune channels" or "UEFA.tv is showing
# the draw" — true for a minute, then noise in search and the profile.
EXTRACTION_INSTRUCTIONS = """\
This is one exchange between a household and its TV voice assistant. The
memory exists to know the HOUSEHOLD over weeks and months. Extract ONLY:
- the household's preferences and dislikes: favourite shows, channels,
  genres, people (actors, hosts, teams), languages;
- habits and routines ("we watch the news at six", "the kids watch cartoons
  after school");
- standing instructions to the assistant ("when I say turn it up, set the
  volume to twenty", "never suggest horror");
- people in the household and facts they state about themselves;
- explicit requests to remember or forget something.
Do NOT extract:
- what the assistant did, can do, or failed to do (tuning, volume, searches,
  "could not find", "is loading");
- what is on air, on screen, or in the guide or lineup right now, or any
  schedule data (programmes, airings, channel numbers);
- one-off requests ("put on KTVU", "what's on now", "pause") — a single
  request is not a preference;
- small talk and greetings.
Entities are the household member (as "User") and what the preference is
about: a show, channel, genre, person, language, or a TV setting such as
"Volume" or "Captions" — so a standing instruction like "when I say turn it
up, set the volume to twenty" is a fact between User and Volume.
Entity names NEVER contain a value or an opinion: the entity is "Volume",
not "preferred volume: 20%"; "KQED", not "favourite channel KQED". The value
belongs in the fact ("User's preferred volume is 20%"). A later statement
about the same thing must land on the same entity so it can replace the
earlier one.
If the exchange contains none of the above, extract nothing."""


_PREF_FACT = re.compile(
    r"\b(favou?rite|prefers?|preferred|preference|likes?|loves?|enjoys?|hates?|"
    r"dislikes?|usual(ly)?|normal(ly)?|default|regular|always|never|routine|habit|every (night|morning|day|"
    r"evening|weekend|week)|instructs?|standing|no longer|wants the assistant|"
    r"when (the )?user says|when they say)\b", re.I)
_OFFERED = re.compile(r"\b(offered|suggest\w*|recommend\w*|scheduled|airing|"
                      r"is on (channel|[A-Z])|called the assistant)\b", re.I)


def is_household_fact(fact: str) -> bool:
    """A lasting fact about the household: says what they like, do or want
    (positive test), and is not an offer, a schedule, or about the
    assistant / screen / guide."""
    text = re.sub(r" \(as of [^)]*\)$", "", fact)
    return bool(_PREF_FACT.search(text)) and not _OFFERED.search(text) \
        and not _NOT_HOUSEHOLD.search(text)


def _profile_facts(facts: list[str]) -> list[str]:
    """Household facts only, near-duplicates collapsed (the first, i.e.
    highest-ranked, phrasing wins)."""
    out: list[str] = []
    seen: list[set[str]] = []
    for f in facts:
        text = re.sub(r" \(as of [^)]*\)$", "", f)
        if not is_household_fact(text):
            continue
        words = {w for w in re.findall(r"[a-z0-9]+", text.lower())
                 if len(w) > 2 and w not in {"the", "user", "their", "and", "for"}}
        if any(len(words & s) / max(1, len(words | s)) >= 0.6 for s in seen):
            continue
        seen.append(words)
        out.append(f)
    return out


@dataclass
class RecallResult:
    profile: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)


class VoiceMemory:
    def __init__(self, graphiti: Graphiti, driver: FalkorDriver,
                 group: str = VOICE_MEMORY_GROUP, test: bool = False) -> None:
        self._group = group
        self.test = test
        self._graphiti = graphiti
        self._driver = driver
        self._profile: list[str] = []
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.profile_ready: asyncio.Task | None = None
        self._consumer: asyncio.Task | None = None
        self._closing = False
        self._maintenance: asyncio.Task | None = None
        self._last_enqueued_user = ""
        # One VoiceMemory per LiveKit job = per session.
        self._profile_at = 0.0
        self._profile_task: asyncio.Task | None = None
        self._session_start = datetime.now(timezone.utc)
        self._earlier = self._load_earlier()

    @classmethod
    async def create(cls, *, host: str = "127.0.0.1", port: int = 6390,
                     password: str | None = None,
                     maintain: bool = False,
                     test: bool = False) -> "VoiceMemory":
        """maintain=True (the voice worker): prune old episodes in the
        background once the profile is warm — at most once per session,
        never on the voice path."""
        os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
        group = TEST_MEMORY_GROUP if test else VOICE_MEMORY_GROUP
        driver = FalkorDriver(host=host, port=port, password=password,
                              database=group)
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
        self = cls(graphiti, driver, group=group, test=test)
        self._consumer = asyncio.create_task(self._consume(), name="memory-writer")
        self.profile_ready = asyncio.create_task(
            self._refresh_profile(), name="memory-profile-warmup")
        if maintain and RETENTION_DAYS > 0:
            async def _maintain() -> None:
                await self.profile_ready
                await self.prune()
            self._maintenance = asyncio.create_task(_maintain(), name="memory-prune")
        return self

    # ── hot path ──────────────────────────────────────────────────────

    def _load_earlier(self, n: int = 4) -> list[str]:
        """The last n exchanges of EARLIER sessions, from the transcript log
        (voice_agent.py writes it), newest first, refusals left out. Read
        once per session — earlier sessions don't change during this one."""
        try:
            with open(TRANSCRIPT_LOG, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 256_000))
                lines = fh.read().decode("utf-8", "replace").splitlines()[1:]
        except OSError:
            return []
        floor = None
        if self.test:
            try:
                floor = datetime.fromisoformat(TEST_RESET_MARKER.read_text().strip())
            except (OSError, ValueError):
                floor = None
        rows = []
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            try:
                ts = datetime.fromisoformat(r["ts"])
            except (KeyError, ValueError):
                continue
            # real sessions never see test exchanges, and the reverse
            if floor is not None and ts < floor:
                continue                    # before the last test reset
            if ts < self._session_start and is_test_room(r.get("room")) == self.test:
                rows.append(r)
        out: list[str] = []
        for user, asst in zip(rows, rows[1:]):
            if user.get("role") != "user" or asst.get("role") != "assistant":
                continue
            said = str(asst.get("text", "")).replace("\u2019", "'")
            if asst.get("interrupted") or _REFUSAL.search(said):
                continue
            out.append(f"user: {user.get('text', '')} / assistant: {said}")
        return out[-n:][::-1]

    async def recall(self, user_text: str) -> RecallResult:
        """Zero I/O: the profile is cached, earlier sessions were read at
        session start. user_text deliberately unused (no embedding on the hot
        path); query-specific lookups are deep_search's job."""
        recent = self._earlier                # zero I/O on the hot path
        # Another job (phone vs the days-long satellite job, memory_cli) may
        # have written since: refresh in the background when the cache is
        # older than PROFILE_TTL_S — this turn uses the cache, the next sees
        # the new facts.
        if (time.monotonic() - self._profile_at > PROFILE_TTL_S
                and (self._profile_task is None or self._profile_task.done())):
            self._profile_task = asyncio.create_task(self._refresh_profile())
        return RecallResult(profile=list(self._profile), recent=recent)

    def remember(self, user_text: str, assistant_text: str, *,
                 interrupted: bool = False,
                 ts: datetime | None = None) -> None:
        if self._closing:
            return
        if not wants_memory(user_text) or not assistant_text.strip():
            return
        if interrupted and len(assistant_text) < 40:
            return                           # barge-in fragment
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
            query=query, config=cfg, group_ids=[self._group],
            search_filter=_current_facts_filter())
        return _format_edges(results.edges)

    # ── background ────────────────────────────────────────────────────

    async def _consume(self) -> None:
        while True:
            user_text, assistant_text, ts = await self._queue.get()
            try:
                result = await asyncio.wait_for(
                    self._graphiti.add_episode(
                        name=f"voice {ts.isoformat(timespec='seconds')}",
                        episode_body=(f"user: {user_text}\n"
                                      f"assistant: {assistant_text[:1000]}"),
                        source=EpisodeType.message,
                        source_description="TV voice assistant conversation",
                        reference_time=ts,
                        group_id=self._group,
                        custom_extraction_instructions=EXTRACTION_INSTRUCTIONS),
                    timeout=180)
                await self._supersede_settings(result)
                await self._refresh_profile()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("memory write dropped: %s", e)
                await asyncio.sleep(5)      # no hot loop when the DB is down
            finally:
                self._queue.task_done()

    async def prune(self, max_age_days: float = RETENTION_DAYS,
                    dry_run: bool = False) -> dict:
        """Retention: remove episodes older than max_age_days unless one
        created a fact that is still current AND about the household.

        Graphiti never expires episodes by itself. remove_episode() deletes
        the episode, the facts it FIRST created and the entities only it
        mentioned — so an old episode that created a lasting preference
        ("Columbo is my favourite", Aug 31) must be kept, or the preference
        goes with it. Everything else old (device control, what's-on chatter,
        test sessions) is removed. Never raises."""
        stats = {"checked": 0, "kept": 0, "removed": 0, "failed": 0}
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max_age_days)).isoformat()
        try:
            rows, _, _ = await self._driver.execute_query(
                "MATCH (e:Episodic) WHERE e.group_id = $g AND e.valid_at < $cutoff "
                "RETURN e.uuid AS uuid, e.entity_edges AS edges",
                g=self._group, cutoff=cutoff)
        except Exception as e:  # noqa: BLE001
            logger.warning("memory prune: listing failed: %s", e)
            return stats
        now = datetime.now(timezone.utc)
        for row in rows:
            stats["checked"] += 1
            ep, edge_ids = row["uuid"], list(row["edges"] or [])
            keep = False
            if edge_ids:
                erows, _, _ = await self._driver.execute_query(
                    "MATCH ()-[r:RELATES_TO]->() WHERE r.uuid IN $ids "
                    "RETURN r.fact AS fact, r.episodes AS episodes, "
                    "r.invalid_at AS invalid_at, r.expired_at AS expired_at",
                    ids=edge_ids)
                for r in erows:
                    created_here = (r["episodes"] or [None])[0] == ep
                    current = _is_current(SimpleNamespace(
                        invalid_at=r["invalid_at"], expired_at=r["expired_at"]), now)
                    # Retention must not depend on wording: is_household_fact
                    # is a profile-ranking heuristic and rejected "never
                    # suggest horror", habits and identity facts, which
                    # remove_episode would then delete for good.
                    if created_here and current:
                        keep = True
                        break
            if keep:
                stats["kept"] += 1
                continue
            if dry_run:
                stats["removed"] += 1
                continue
            try:
                await self._graphiti.remove_episode(ep)
                stats["removed"] += 1
                logger.info("memory prune: removed episode %s (no current fact)", ep)
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                logger.debug("memory prune: %s: %s", ep, e)
        logger.info("memory prune (>%gd%s): %s", max_age_days,
                    ", dry run" if dry_run else "", stats)
        if stats["removed"] and not dry_run:
            await self._refresh_profile()
        return stats

    async def _supersede_settings(self, result) -> None:
        """A TV setting has one value at a time: a new household fact about
        Volume (or Captions, Language …) ends every older current fact from
        the same person about that setting. Graphiti only compares facts on
        the SAME pair of entities and lets its model judge the contradiction;
        "preferred volume is 20%" landed on a new entity "preferred volume:
        20%" and left "normal volume is 0.6" (on "Volume") current beside it.
        The setting is matched by name, so a value glued into an entity name
        still counts. Never raises."""
        try:
            ep = getattr(getattr(result, "episode", None), "uuid", None)
            names = {n.uuid: n.name for n in (getattr(result, "nodes", None) or [])}
            # The result also lists OLD facts this episode just ended — only
            # facts it created, and that are still current, can supersede.
            edges = [e for e in (getattr(result, "edges", None) or [])
                     if (e.episodes or [None])[0] == ep
                     and e.invalid_at is None and e.expired_at is None]
            new_ids = [x.uuid for x in edges]
            for e in edges:
                target = (names.get(e.target_node_uuid) or "").lower()
                # The setting must be the entity, not a word inside it
                # ("language" matched "Spanish language films").
                key = next((k for k in SINGLE_VALUE_SETTINGS
                            if re.search(rf"\b{k}s?\b", target)), None)
                if key is None:
                    continue
                when = (e.valid_at or datetime.now(timezone.utc)).isoformat()
                # Never end a fact this episode created (two facts about one
                # setting ended each other), nor one stated LATER than this
                # one (another job's extraction can finish out of order).
                rows, _, _ = await self._driver.execute_query(
                    "MATCH (a:Entity {uuid: $src})-[r:RELATES_TO]->(b:Entity) "
                    "WHERE toLower(b.name) CONTAINS $key AND NOT r.uuid IN $new_ids "
                    "AND r.expired_at IS NULL AND r.invalid_at IS NULL "
                    "AND (r.valid_at IS NULL OR r.valid_at < $when) "
                    "SET r.invalid_at = $when, r.expired_at = $now RETURN r.fact AS fact",
                    src=e.source_node_uuid, key=key, new_ids=new_ids, when=when,
                    now=datetime.now(timezone.utc).isoformat())
                for r in rows:
                    logger.info("memory: %r superseded by %r", r["fact"], e.fact)
        except Exception as e:  # noqa: BLE001
            logger.warning("memory supersede failed: %s", e)

    async def _refresh_profile(self) -> None:
        try:
            cfg = EDGE_HYBRID_SEARCH_RRF.model_copy(deep=True)
            cfg.limit = 30                 # over-fetch: most get filtered out
            results = await self._graphiti.search_(
                query=PROFILE_QUERY, config=cfg,
                group_ids=[self._group],
                search_filter=_current_facts_filter())
            self._profile = _profile_facts(_format_edges(results.edges))[:12]
            self._profile_at = time.monotonic()
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
        if self._maintenance and not self._maintenance.done():
            self._maintenance.cancel()
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
