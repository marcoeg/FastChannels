"""PydanticLLM — livekit-agents LLM node running the pydantic-ai VoiceBrain
in-process (no gateway hop).

Same seam as the frozen HermesLLM (hermes_bridge.py), chosen over an
llm_node override to keep LiveKit's LLMMetrics/TTFT accounting and the
proven aclose()->CancelledError barge-in contract.

Behaviors:
- speech sanitation: markdown stripped per-delta before TTS sees it
- barge-in: cancellation aborts the pydantic-ai run; the partial spoken
  text is appended to history manually
- rollout tracing (rollouts.py): one NeMo Gym-shaped record per turn,
  including barge-ins and failures, written off the voice path
- serialization: one turn at a time (process-level lock, Hermes-era rule)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from pydantic_ai import (
    AgentRunResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
)
from pydantic_ai.exceptions import RunCancelled
from pydantic_ai.messages import TextPart

from pai_agent import VoiceBrain, closing_ack_reply, is_echo_greeting
from rollouts import RolloutWriter, TraceCapability, build_record
from telemetry import log_turn
from textnorm import sanitize

logger = logging.getLogger("voice.llm")


class PydanticLLM(llm.LLM):
    def __init__(self, brain: VoiceBrain, *, session_id: str = "",
                 room: str = "", is_satellite: bool = False,
                 tracer: RolloutWriter | None = None,
                 is_test: bool = False) -> None:
        super().__init__()
        self.is_test = is_test              # test room: traces get "test": true
        self.last_reply_at = 0.0            # monotonic, when Minny last finished a reply
        self.brain = brain
        self.turn_lock = asyncio.Lock()
        self.pending_recall = None          # set per turn by on_user_turn_completed
        # rollout tracing: one session per LiveKit job (= one history)
        self.session_id = session_id or uuid.uuid4().hex
        self.room = room
        self.is_satellite = is_satellite
        self.tracer = tracer
        self.turn_index = 0

    def chat(self, *, chat_ctx: llm.ChatContext, tools=None,
             conn_options=DEFAULT_API_CONNECT_OPTIONS,
             parallel_tool_calls=None, tool_choice=None,
             extra_kwargs=None) -> "PydanticLLMStream":
        return PydanticLLMStream(self, chat_ctx=chat_ctx, tools=tools or [],
                                 conn_options=conn_options)


class PydanticLLMStream(llm.LLMStream):
    def __init__(self, pai: PydanticLLM, *, chat_ctx, tools, conn_options) -> None:
        super().__init__(pai, chat_ctx=chat_ctx, tools=tools,
                         conn_options=conn_options)
        self._pai = pai
        # Chunk id = turn id, so LiveKit's LLMMetrics.request_id joins the
        # metrics log to the rollout record (it used to be a constant).
        self._turn_id = uuid.uuid4().hex

    def _last_user_text(self) -> str:
        for item in reversed(self._chat_ctx.items):
            if getattr(item, "role", None) == "user":
                content = item.content
                if isinstance(content, list):
                    content = " ".join(c for c in content if isinstance(c, str))
                return str(content or "").strip()
        return ""

    def _emit(self, text: str) -> None:
        if not text:
            return
        self._event_ch.send_nowait(
            llm.ChatChunk(id=self._turn_id,
                          delta=llm.ChoiceDelta(role="assistant", content=text)))

    async def _run(self) -> None:
        user_text = self._last_user_text()
        if not user_text:
            return
        brain = self._pai.brain
        if is_echo_greeting(user_text, self._pai.last_reply_at, time.monotonic()):
            # LiveKit started this (preemptive) run for an echo; its hook will
            # drop the turn — don't call the model, commit or trace it.
            logger.info("echo greeting, no model run: %r", user_text)
            return
        ack = closing_ack_reply(user_text, brain.last_reply())
        if ack:
            self._pai.pending_recall = None
            self._emit(ack)
            brain.commit_exchange(user_text, ack)
            log_turn({"type": "turn_timing", "session_id": self._pai.session_id,
                      "turn_id": self._turn_id, "closing_ack": True,
                      "test": self._pai.is_test,
                      "user_chars": len(user_text), "ttfd_ms": 0, "total_ms": 0})
            return
        recall = self._pai.pending_recall
        self._pai.pending_recall = None
        if recall is None and brain.memory is not None:
            # Text-console turns bypass on_user_turn_completed (it only
            # fires on audio turns); recall is ~1 ms, so do it inline.
            try:
                recall = await asyncio.wait_for(
                    brain.memory.recall(user_text), 0.25)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                recall = None

        spoken: list[str] = []
        t0 = time.monotonic()
        wall0 = time.time()
        ttfd_ms: float | None = None
        tool_started: dict[str, tuple[str, float, str]] = {}
        tool_spans: list[dict] = []
        interrupted = False
        trace = TraceCapability() if self._pai.tracer else None
        turn_messages: list | None = None
        error: str | None = None
        self._pai.turn_index += 1
        turn_index = self._pai.turn_index

        async with self._pai.turn_lock:
            try:
                async with brain.agent.run_stream_events(
                        user_text,
                        message_history=list(brain.history),
                        deps=brain.make_deps(recall),
                        conversation_id=self._pai.session_id,
                        capabilities=[trace] if trace else None) as events:
                    async for ev in events:
                        d = None
                        if isinstance(ev, PartStartEvent) and isinstance(
                                ev.part, TextPart):
                            # First chunk of a text part rides on the start
                            # event; only the rest arrives as deltas.
                            d = sanitize(ev.part.content)
                        elif isinstance(ev, PartDeltaEvent) and isinstance(
                                ev.delta, TextPartDelta):
                            d = sanitize(ev.delta.content_delta)
                        if d is not None:
                            if d.strip() or spoken:
                                if ttfd_ms is None:
                                    ttfd_ms = (time.monotonic() - t0) * 1000
                                spoken.append(d)
                                self._emit(d)
                            continue
                        elif isinstance(ev, FunctionToolCallEvent):
                            part = ev.part
                            args = part.args if isinstance(part.args, str) \
                                else str(part.args)
                            tool_started[part.tool_call_id] = (
                                part.tool_name, time.monotonic(), args[:300])
                        elif isinstance(ev, FunctionToolResultEvent):
                            hit = tool_started.pop(ev.tool_call_id, None)
                            if hit:
                                name, ts, args = hit
                                tool_spans.append(
                                    {"name": name, "args": args,
                                     "ms": round((time.monotonic() - ts) * 1000)})
                        elif isinstance(ev, AgentRunResultEvent):
                            pass            # loop ends after this event
                    # serialize-before-commit: commit_turn trims history and
                    # blanks old tool results; the trace needs them intact
                    turn_messages = list(events.new_messages())
                    brain.commit_turn(turn_messages)
            except asyncio.CancelledError as exc:
                interrupted = True
                rc = RunCancelled.from_cancellation(exc)
                if rc is not None:      # the real partial run, for the trace
                    try:
                        turn_messages = list(rc.new_messages())
                    except Exception:  # noqa: BLE001
                        pass
                brain.commit_interrupted(user_text, "".join(spoken))
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                logger.exception("voice turn failed")
                if not spoken:
                    self._emit("Sorry, something went wrong on my side.")
            finally:
                if trace is not None:
                    try:
                        rec = build_record(
                            session_id=self._pai.session_id,
                            turn_index=turn_index, turn_id=self._turn_id,
                            room=self._pai.room,
                            is_satellite=self._pai.is_satellite,
                            user_text=user_text, trace=trace,
                            new_messages=turn_messages,
                            spoken="".join(spoken), interrupted=interrupted,
                            error=error, ts_start=wall0, ttfd_ms=ttfd_ms,
                            total_ms=(time.monotonic() - t0) * 1000,
                            recall_used=recall is not None)
                        if rec is not None and self._pai.is_test:
                            rec["test"] = True
                        self._pai.tracer.enqueue(rec)
                    except Exception:  # noqa: BLE001 — never break a turn
                        logger.debug("rollout build failed", exc_info=True)
                log_turn({
                    "type": "turn_timing",
                    "session_id": self._pai.session_id,
                    "turn_id": self._turn_id,
                    "user_chars": len(user_text),
                    "recall_used": recall is not None,
                    "ttfd_ms": round(ttfd_ms) if ttfd_ms is not None else None,
                    "tool_spans": tool_spans,
                    "total_ms": round((time.monotonic() - t0) * 1000),
                    "interrupted": interrupted,
                })
