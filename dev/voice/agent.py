#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "livekit-agents[openai,silero]~=1.2",
#   "aiohttp",
# ]
# ///
"""Hermes voice worker: LiveKit room audio <-> Hermes agent.

Pipeline: silero VAD -> OpenAI STT -> HermesLLM (SSE bridge to the Hermes
api_server on 127.0.0.1:8642, persistent session "voice-main") -> OpenAI TTS.

Run:  ./agent.py download-files   (once: fetch VAD model)
      ./agent.py dev              (foreground, auto-reload)
      ./agent.py start            (production; used by the systemd unit)

Env (dev/voice/.env): LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
OPENAI_API_KEY, HERMES_API_URL, HERMES_API_KEY.
"""

import os
import sys
from pathlib import Path

# Load dev/voice/.env so the systemd unit and manual runs behave identically.
for line in (Path(__file__).parent / ".env").read_text().splitlines():
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent))

from livekit import agents  # noqa: E402
from livekit.agents import Agent, AgentSession, WorkerOptions, cli  # noqa: E402
from livekit.plugins import openai, silero  # noqa: E402

from hermes_bridge import HermesLLM  # noqa: E402


VOICE_LOG = Path.home() / "news" / "voice-conversations.jsonl"


def _log_turn(role: str, text: str, **extra) -> None:
    import json
    import time

    VOICE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "role": role,
             "text": text, **extra}
    with open(VOICE_LOG, "a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def entrypoint(ctx: agents.JobContext) -> None:
    await ctx.connect()

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=openai.STT(),
        llm=HermesLLM(
            base_url=os.environ["HERMES_API_URL"],
            api_key=os.environ["HERMES_API_KEY"],
            session_id="voice-main",
            session_key="voice:livekit:marco",
        ),
        tts=openai.TTS(),
    )

    # Transcript log: what STT heard and what TTS spoke (incl. interruptions).
    # The full agent-side transcript also lives in Hermes session voice-main.
    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        try:
            item = getattr(ev, "item", ev)
            role = str(getattr(item, "role", "?"))
            text = getattr(item, "text_content", None) or getattr(item, "content", "")
            if isinstance(text, list):
                text = " ".join(str(c) for c in text if isinstance(c, str))
            interrupted = bool(getattr(item, "interrupted", False))
            if str(text).strip():
                _log_turn(role, str(text).strip(), interrupted=interrupted,
                          room=ctx.room.name)
        except Exception:
            pass  # logging must never break the pipeline

    await session.start(
        room=ctx.room,
        # Instructions live server-side in Hermes (rules + the voice-style
        # message in session voice-main); this Agent shell is just plumbing.
        agent=Agent(instructions="You are Hermes. (Handled server-side.)"),
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
