#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "livekit-agents[openai,silero]==1.7.1",
#   "pydantic-ai-slim[openai,mcp]==2.31.1",
#   "graphiti-core[falkordb]==0.29.3",
# ]
# ///
"""Standalone voice worker: LiveKit room audio <-> pydantic-ai VoiceBrain.

Replaces the Hermes-backed worker (agent.py, kept frozen as fallback).
Pipeline: silero VAD -> OpenAI STT -> PydanticLLM (in-process pydantic-ai
agent: EPG MCP + TV MCP + graphiti memory) -> OpenAI TTS.

Run:  ./voice_agent.py download-files   (once: fetch VAD model)
      ./voice_agent.py console --text   (browser-free text console)
      ./voice_agent.py dev              (foreground, auto-reload)
      ./voice_agent.py start            (production; systemd voice-agent.service)

Env (dev/voice/.env): LIVEKIT_URL/API_KEY/API_SECRET, OPENAI_API_KEY,
VOICE_MODEL, EPG_MCP_URL, TV_MCP_URL, VOICE_FALKORDB_PASSWORD/PORT,
VOICE_METRICS_LOG, SEMAPHORE_LIMIT, GRAPHITI_TELEMETRY_ENABLED.
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

# Load dev/voice/.env so the systemd unit and manual runs behave identically.
for line in (Path(__file__).parent / ".env").read_text().splitlines():
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent))

from livekit import agents  # noqa: E402
from livekit.agents import Agent, AgentSession, JobProcess, WorkerOptions, cli  # noqa: E402
from livekit.plugins import openai, silero  # noqa: E402

from pai_agent import VoiceBrain  # noqa: E402
from pai_llm import PydanticLLM  # noqa: E402
from telemetry import log_turn, wire_session_metrics  # noqa: E402

logger = logging.getLogger("voice.worker")

VOICE_LOG = Path.home() / "news" / "voice-conversations.jsonl"
RECALL_CAP_S = 0.25


def _log_transcript(role: str, text: str, **extra) -> None:
    VOICE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "role": role,
             "text": text, **extra}
    with open(VOICE_LOG, "a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class TvAssistant(Agent):
    """LiveKit-side shell; all real instructions live in the pydantic-ai
    brain. This subclass only hooks the recall seam."""

    def __init__(self, pai: PydanticLLM) -> None:
        super().__init__(instructions="(Handled by the pydantic-ai brain.)")
        self._pai = pai

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        memory = self._pai.brain.memory
        if memory is None:
            return
        text = getattr(new_message, "text_content", None) or ""
        try:
            self._pai.pending_recall = await asyncio.wait_for(
                memory.recall(str(text)), RECALL_CAP_S)
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            self._pai.pending_recall = None
            if not isinstance(e, asyncio.TimeoutError):
                logger.debug("recall failed: %s", e)


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: agents.JobContext) -> None:
    await ctx.connect()

    memory = None
    try:
        from memory import VoiceMemory
        memory = await VoiceMemory.create(
            port=int(os.environ.get("VOICE_FALKORDB_PORT", "6390")),
            password=os.environ.get("VOICE_FALKORDB_PASSWORD"))
        logger.info("voice memory online (graph voice_memory)")
    except Exception as e:  # noqa: BLE001 — memory is optional, voice is not
        logger.warning("voice memory unavailable, running without it: %s", e)

    brain = VoiceBrain(memory)
    pai = PydanticLLM(brain)

    # Keep MCP sessions warm for the whole job (otherwise per-run setup).
    await brain.agent.__aenter__()

    async def _shutdown() -> None:
        try:
            await brain.agent.__aexit__(None, None, None)
        finally:
            if memory is not None:
                await memory.aclose()

    ctx.add_shutdown_callback(_shutdown)

    session = AgentSession(
        vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
        stt=openai.STT(),
        llm=pai,
        tts=openai.TTS(),
    )

    wire_session_metrics(session)

    # Transcript log (playout truth) + memory writes from the same event.
    last_user = {"text": ""}

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        try:
            item = getattr(ev, "item", ev)
            role = str(getattr(item, "role", "?"))
            text = getattr(item, "text_content", None) or getattr(item, "content", "")
            if isinstance(text, list):
                text = " ".join(str(c) for c in text if isinstance(c, str))
            text = str(text).strip()
            if not text:
                return
            interrupted = bool(getattr(item, "interrupted", False))
            _log_transcript(role, text, interrupted=interrupted,
                            room=ctx.room.name)
            if role == "user":
                last_user["text"] = text
            elif role == "assistant" and memory is not None and last_user["text"]:
                memory.remember(last_user["text"], text, interrupted=interrupted)
        except Exception:  # noqa: BLE001 — logging must never break the pipeline
            logger.debug("conversation_item handler failed", exc_info=True)

    # Prime TLS + priority-tier routing so the first real turn isn't cold.
    if os.environ.get("VOICE_WARMUP", "1") == "1":
        async def _warmup() -> None:
            try:
                t0 = time.monotonic()
                await brain.agent.run("Say OK.",
                                      deps=brain.make_deps(None))
                log_turn({"type": "warmup",
                          "ms": round((time.monotonic() - t0) * 1000)})
            except Exception as e:  # noqa: BLE001
                logger.warning("warmup run failed: %s", e)
        asyncio.create_task(_warmup())

    await session.start(room=ctx.room, agent=TvAssistant(pai))


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint,
                              prewarm_fnc=prewarm,
                              load_threshold=0.85))
