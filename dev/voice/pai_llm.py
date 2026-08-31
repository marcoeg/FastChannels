"""PydanticLLM — livekit-agents LLM node running the pydantic-ai VoiceBrain
in-process (no gateway hop).

Same seam as the frozen HermesLLM (hermes_bridge.py), chosen over an
llm_node override to keep LiveKit's LLMMetrics/TTFT accounting and the
proven aclose()->CancelledError barge-in contract.

Behaviors:
- speech sanitation: markdown stripped per-delta before TTS sees it
- progress filler: armed when a tool call starts before any text, but
  DELAYED 0.9 s so fast TV-control calls finish silently
- barge-in: cancellation aborts the pydantic-ai run; the partial spoken
  text is appended to history manually (new_messages() is unavailable
  after a cancelled run)
- serialization: one turn at a time (process-level lock, Hermes-era rule)
"""

from __future__ import annotations

import asyncio
import logging
import time

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
from pydantic_ai.messages import TextPart

from pai_agent import VoiceBrain
from telemetry import log_turn
from textnorm import FILLER, sanitize

logger = logging.getLogger("voice.llm")

FILLER_DELAY_S = 0.9


class PydanticLLM(llm.LLM):
    def __init__(self, brain: VoiceBrain) -> None:
        super().__init__()
        self.brain = brain
        self.turn_lock = asyncio.Lock()
        self.pending_recall = None          # set per turn by on_user_turn_completed

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
            llm.ChatChunk(id="pai",
                          delta=llm.ChoiceDelta(role="assistant", content=text)))

    async def _run(self) -> None:
        user_text = self._last_user_text()
        if not user_text:
            return
        brain = self._pai.brain
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
        filler_task: asyncio.Task | None = None
        filler_used = False
        t0 = time.monotonic()
        ttfd_ms: float | None = None
        tool_started: dict[str, tuple[str, float]] = {}
        tool_spans: list[dict] = []
        interrupted = False

        async def filler_after_delay() -> None:
            nonlocal filler_used
            await asyncio.sleep(FILLER_DELAY_S)
            if not spoken:
                filler_used = True
                self._emit(FILLER + " ")

        async with self._pai.turn_lock:
            try:
                async with brain.agent.run_stream_events(
                        user_text,
                        message_history=list(brain.history),
                        deps=brain.make_deps(recall)) as events:
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
                            if not spoken and filler_task is None:
                                filler_task = asyncio.create_task(
                                    filler_after_delay())
                        elif isinstance(ev, FunctionToolResultEvent):
                            hit = tool_started.pop(ev.tool_call_id, None)
                            if hit:
                                name, ts, args = hit
                                tool_spans.append(
                                    {"name": name, "args": args,
                                     "ms": round((time.monotonic() - ts) * 1000)})
                        elif isinstance(ev, AgentRunResultEvent):
                            pass            # loop ends after this event
                    brain.commit_turn(events.new_messages())
            except asyncio.CancelledError:
                interrupted = True
                brain.commit_interrupted(user_text, "".join(spoken))
                raise
            except Exception:
                logger.exception("voice turn failed")
                if not spoken:
                    self._emit("Sorry, something went wrong on my side.")
            finally:
                if filler_task:
                    filler_task.cancel()
                log_turn({
                    "type": "turn_timing",
                    "user_chars": len(user_text),
                    "recall_used": recall is not None,
                    "ttfd_ms": round(ttfd_ms) if ttfd_ms is not None else None,
                    "filler_used": filler_used,
                    "tool_spans": tool_spans,
                    "total_ms": round((time.monotonic() - t0) * 1000),
                    "interrupted": interrupted,
                })
