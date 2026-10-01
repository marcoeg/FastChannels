"""Per-turn rollout tracing for training and evaluation (NVIDIA NeMo Gym).

Every voice turn becomes one JSONL record holding exactly what the model saw
and did, in the OpenAI Responses-API shape NeMo Gym consumes:

  responses_create_params.input  the instructions sent (one developer
                                 message) + the history actually passed to
                                 the run + the user's utterance
  responses_create_params.tools  the function tools offered on that request
  response.output                the turn's trajectory: function_call,
                                 function_call_output, assistant message

plus ids (session/turn/run), the brain that produced it, per-request usage
and latency, what went to TTS, and cheap implicit quality signals. Rewards
are assigned later (rollouts_export.py, NeMo Gym, or a judge).

Capture points (pydantic-ai 2.31.1):
- TraceCapability.before/after_model_request — the real request: messages,
  instructions, tool definitions, and each response with its usage. Attached
  per run, so the warmup run and memory extraction are never traced.
- the turn's new messages — on success from the run handle, on barge-in from
  RunCancelled.from_cancellation(exc): the real partial run, not the
  synthetic history entry commit_interrupted writes.

Writing never blocks or breaks a voice turn: records go to a queue drained
by one background task (file I/O off the event loop). Files land in
VOICE_ROLLOUT_DIR (default ~/news/voice-rollouts/YYYY-MM-DD.jsonl), outside
the repo — they contain household conversations. VOICE_TRACE=0 disables.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

logger = logging.getLogger("voice.rollouts")

SCHEMA = "minny-rollout/1"
ROLLOUT_DIR = Path(os.environ.get(
    "VOICE_ROLLOUT_DIR", str(Path.home() / "news" / "voice-rollouts")))


def tracing_enabled() -> bool:
    return os.environ.get("VOICE_TRACE", "1") != "0"


# ── capture ───────────────────────────────────────────────────────────


class TraceCapability(AbstractCapability):
    """Records every model request of one run (a tool turn makes two or
    more). Holds the first request's messages and tools, which define the
    rollout's prompt."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.first_messages: list | None = None
        self.first_tools: list[dict[str, Any]] | None = None
        self._open: list[float] = []

    async def before_model_request(self, ctx, request_context):
        try:
            tools = [tool_def(t) for t in
                     request_context.model_request_parameters.function_tools]
            if self.first_messages is None:
                self.first_messages = list(request_context.messages)
                self.first_tools = tools
            self._open.append(time.monotonic())
            self.requests.append({
                "n_messages": len(request_context.messages),
                "n_tools": len(tools),
                "tools_hash": _tools_hash(tools),
            })
        except Exception:  # noqa: BLE001 — tracing must never break a turn
            logger.debug("trace before_model_request failed", exc_info=True)
        return request_context

    async def after_model_request(self, ctx, *, request_context, response):
        try:
            t0 = self._open.pop(0) if self._open else None
            rec = self.requests[-1] if self.requests else {}
            rec.update({
                "model": response.model_name,
                "latency_ms": round((time.monotonic() - t0) * 1000) if t0 else None,
                "finish_reason": response.finish_reason,
                "usage": _usage(response.usage),
            })
        except Exception:  # noqa: BLE001
            logger.debug("trace after_model_request failed", exc_info=True)
        return response


def tool_def(t) -> dict[str, Any]:
    return {"type": "function", "name": t.name,
            "description": t.description or "",
            "parameters": t.parameters_json_schema}


def _tools_hash(tools: list[dict]) -> str:
    blob = json.dumps(tools, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def _usage(u) -> dict[str, Any]:
    if u is None:
        return {}
    return {k: v for k, v in vars(u).items()
            if isinstance(v, (int, float)) and v}


# ── pydantic-ai messages → Responses API items ───────────────────────


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return " ".join(c for c in content if isinstance(c, str))
    return str(content)


def to_items(messages: list, *, with_instructions: bool = False) -> list[dict]:
    """Convert pydantic-ai messages to Responses-API input/output items.

    Only the LAST request's instructions are what a model actually receives
    (history requests keep stale copies), so instructions are emitted once,
    as a leading developer message, and only when asked for.
    """
    items: list[dict] = []
    if with_instructions:
        instr = next((m.instructions for m in reversed(messages)
                      if isinstance(m, ModelRequest) and m.instructions), None)
        if instr:
            items.append({"role": "developer", "content": instr})
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, SystemPromptPart):
                    items.append({"role": "developer", "content": p.content})
                elif isinstance(p, UserPromptPart):
                    items.append({"role": "user", "content": _text(p.content)})
                elif isinstance(p, ToolReturnPart):
                    items.append({"type": "function_call_output",
                                  "call_id": p.tool_call_id,
                                  "output": p.model_response_str()})
                elif isinstance(p, RetryPromptPart):
                    if p.tool_name:
                        items.append({"type": "function_call_output",
                                      "call_id": p.tool_call_id,
                                      "output": p.model_response(),
                                      "error": True})
                    else:
                        items.append({"role": "user",
                                      "content": p.model_response()})
        elif isinstance(m, ModelResponse):
            text = "".join(p.content for p in m.parts if isinstance(p, TextPart))
            for p in m.parts:
                if isinstance(p, ToolCallPart):
                    items.append({"type": "function_call",
                                  "call_id": p.tool_call_id,
                                  "name": p.tool_name,
                                  "arguments": p.args_as_json_str()})
            if text:
                items.append({"role": "assistant", "content": text})
    return items


def _signals(output: list[dict], interrupted: bool, spoken: str) -> dict:
    calls = {i["call_id"] for i in output if i.get("type") == "function_call"}
    results = [i for i in output if i.get("type") == "function_call_output"]
    answered = {i["call_id"] for i in results}
    empty = sum(1 for i in results
                if i.get("output", "").strip() in ("", "[]", "{}", '{"result": []}',
                                                   '{"result":[]}'))
    return {
        "interrupted": interrupted,
        # preemptive generation starts and cancels runs on interim
        # transcripts; those were never heard — not a viewer barge-in
        "cancelled_before_speech": interrupted and not spoken.strip(),
        "tool_errors": sum(1 for i in results if i.get("error")),
        "empty_tool_results": empty,
        "unanswered_tool_calls": len(calls - answered),
    }


def build_record(*, session_id: str, turn_index: int, turn_id: str,
                 room: str, is_satellite: bool, user_text: str,
                 trace: TraceCapability, new_messages: list | None,
                 spoken: str, interrupted: bool, error: str | None,
                 ts_start: float, ttfd_ms: float | None, total_ms: float,
                 recall_used: bool) -> dict | None:
    if trace.first_messages is None:
        return None                     # the model was never called
    msgs = new_messages or []
    # new_messages starts with this turn's user request (already in input)
    out_msgs = msgs[1:] if msgs and isinstance(msgs[0], ModelRequest) else msgs
    output = to_items(out_msgs)
    run_id = next((getattr(m, "run_id", None) for m in msgs
                   if getattr(m, "run_id", None)), None)
    models = [r.get("model") for r in trace.requests if r.get("model")]
    return {
        "schema": SCHEMA,
        "session_id": session_id,
        "turn_index": turn_index,
        "turn_id": turn_id,
        "run_id": run_id,
        "ts_start": datetime.fromtimestamp(ts_start, timezone.utc).isoformat(),
        "ts_end": datetime.now(timezone.utc).isoformat(),
        "room": room,
        "is_satellite": is_satellite,
        "epg_backend": os.environ.get("VOICE_EPG_BACKEND", "minny"),
        "brain": {"llm": os.environ.get("VOICE_LLM", "openai"),
                  "model": models[0] if models else None},
        "user_text": user_text,
        "responses_create_params": {
            "input": to_items(trace.first_messages, with_instructions=True),
            "tools": trace.first_tools or [],
            "parallel_tool_calls": True,
            "max_output_tokens": 700,
        },
        "response": {"output": output},
        "requests": trace.requests,
        "spoken_text": spoken,
        "interrupted": interrupted,
        "error": error,
        "signals": _signals(output, interrupted, spoken),
        "latency": {"ttfd_ms": round(ttfd_ms) if ttfd_ms is not None else None,
                    "total_ms": round(total_ms)},
        "recall_used": recall_used,
    }


# ── writer ────────────────────────────────────────────────────────────


class RolloutWriter:
    """Queue + one background task; JSONL appends happen off the event
    loop. enqueue() never raises and never blocks."""

    def __init__(self, directory: Path = ROLLOUT_DIR) -> None:
        self._dir = directory
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    def enqueue(self, record: dict | None) -> None:
        if record is None:
            return
        try:
            if self._queue is None:
                self._queue = asyncio.Queue(maxsize=1000)
                self._task = asyncio.create_task(self._drain(),
                                                 name="rollout-writer")
            self._queue.put_nowait(record)
        except Exception:  # noqa: BLE001
            logger.warning("rollout dropped", exc_info=True)

    async def _drain(self) -> None:
        while True:
            rec = await self._queue.get()
            try:
                await asyncio.to_thread(self._append, rec)
            except Exception:  # noqa: BLE001
                logger.warning("rollout write failed", exc_info=True)
            finally:
                self._queue.task_done()

    def _append(self, rec: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with open(self._dir / f"{day}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    async def aclose(self, timeout: float = 5.0) -> None:
        if self._queue is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
        except asyncio.TimeoutError:
            logger.warning("rollout flush timed out; %d unwritten",
                           self._queue.qsize())
        if self._task:
            self._task.cancel()
