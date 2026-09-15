#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "livekit-agents[openai,silero,elevenlabs]==1.7.1",
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
from livekit.agents import (  # noqa: E402
    Agent,
    AgentSession,
    JobProcess,
    RoomInputOptions,
    WorkerOptions,
    cli,
)
# elevenlabs imported at module level on purpose: LiveKit plugins must
# register on the MAIN thread; importing inside the entrypoint (job event
# loop thread) raises RuntimeError.
from livekit.plugins import elevenlabs, openai, silero  # noqa: E402

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

    # TTS is switchable via VOICE_TTS (elevenlabs|openai) — flipping back to
    # openai restores the original voice with one .env line.
    if os.environ.get("VOICE_TTS", "openai") == "elevenlabs":
        tts = elevenlabs.TTS(model="eleven_flash_v2_5",
                             api_key=os.environ["ELEVENLABS_API_KEY"])
    else:
        tts = openai.TTS()

    # Interruption / barge-in is per ROOM, not global. The browser/phone
    # (room "voice") has real client-side echo cancellation, so it keeps full
    # full-duplex async conversation — interrupt Minny any time. Only the
    # ESP32 satellite (room "speaker"), which has NO on-device echo
    # cancellation, runs half-duplex (interruption off + discard mic while
    # speaking) so it doesn't transcribe and loop on its own voice.
    is_satellite = ctx.room.name == "speaker"
    turn_handling = {
        "preemptive_generation": {"enabled": True, "preemptive_tts": False},
    }
    if is_satellite:
        turn_handling["interruption"] = {
            "enabled": False, "discard_audio_if_uninterruptible": True}

    session = AgentSession(
        vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
        # Streaming transcription: transcript finalizes ~0.2 s after speech
        # end instead of ~1 s of post-hoc batch Whisper (measured), and
        # interim transcripts are what lets preemptive generation fire.
        # language + prompt: "en" alone is only a hint — short greetings
        # from the satellite's mic still code-switched ("hi Minny" became
        # Norwegian "Hei, Minni" and Thai "ไอมินนี่", and the session then
        # missed English follow-ups). The prompt anchors the model.
        stt=openai.STT(
            use_realtime=True, language="en",
            prompt="English conversation with Minny, a TV-viewing "
                   "assistant. Expect greetings like 'hi Minny' and TV, "
                   "channel, and programme names."),
        llm=pai,
        tts=tts,
        # Start LLM AND TTS on the live transcript before the endpointer
        # commits the turn — overlaps model TTFT + TTS TTFB with the
        # silence-detection wait. Discarded preemptive synthesis on a
        # mid-sentence pause costs pennies on flash-tier TTS.
        turn_handling=turn_handling,
    )

    wire_session_metrics(session)

    # Mirror agent state (listening/thinking/speaking) onto the
    # "agent_state" data topic for hardware clients: the ESP32 satellite
    # (dev/voice/esp32) cannot read the lk.agent.state participant
    # attribute — its SDK speaks protocol v1, which predates attributes.
    # Echo-tail guard for the ESP32 satellite: it has no on-device echo
    # cancellation, so when Minny stops "speaking" the device's speaker is
    # still draining the last ~0.5 s of her audio into the open mic, which
    # STT transcribes (e.g. the tail of "Hi, what would you like to watch"
    # comes back as "hi Minny") and loops. On the speaking->listening edge,
    # mute the mic input for a cooldown covering that drain, then re-enable.
    # Only for the "speaker" room; the browser/phone room "voice" has real
    # AEC and keeps full barge-in.
    ECHO_COOLDOWN_S = 1.0
    _cooldown_task: dict = {}

    # Wake-gated STT for the satellite. The device streams continuously (so its
    # shared-bus speaker keeps working) and only runs an on-device wake word;
    # it CANNOT stop sending audio without breaking playback. So the agent is
    # what keeps cloud STT from running on the silent asleep stream — the whole
    # point of the wake word: it disables its own audio input while the device
    # sleeps and re-enables it on the device's "wake" signal. Feeding the STT a
    # 24/7 silent stream also timed it out ("resetting Realtime STT session")
    # and dropped the next request. Effective input = awake AND not in the
    # post-speech echo cooldown. Non-satellite rooms are never gated.
    mic = {"awake": not is_satellite, "cooldown": False}

    def _apply_mic() -> None:
        try:
            session.input.set_audio_enabled(mic["awake"] and not mic["cooldown"])
        except Exception:
            logger.debug("set_audio_enabled failed", exc_info=True)

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
        async def _publish() -> None:
            try:
                await ctx.room.local_participant.publish_data(
                    str(ev.new_state).encode(),
                    topic="agent_state", reliable=True)
            except Exception:
                logger.debug("agent_state publish failed", exc_info=True)
        asyncio.create_task(_publish())

        if ctx.room.name == "speaker" and \
                str(ev.old_state) == "speaking" and str(ev.new_state) == "listening":
            async def _cooldown() -> None:
                mic["cooldown"] = True
                _apply_mic()
                try:
                    await asyncio.sleep(ECHO_COOLDOWN_S)
                finally:
                    mic["cooldown"] = False
                    _apply_mic()   # re-enable only if still awake
            old = _cooldown_task.get("t")
            if old and not old.done():
                old.cancel()
            _cooldown_task["t"] = asyncio.create_task(_cooldown())

    # Late joiners (the satellite reconnecting mid-session) missed earlier
    # transitions — push the current state straight to them.
    @ctx.room.on("participant_connected")
    def _on_participant_connected(p) -> None:
        async def _push() -> None:
            try:
                state = getattr(session, "agent_state", None) or "idle"
                await ctx.room.local_participant.publish_data(
                    str(state).encode(), topic="agent_state", reliable=True,
                    destination_identities=[p.identity])
            except Exception:
                logger.debug("agent_state push failed", exc_info=True)
        asyncio.create_task(_push())

    # ...and rebroadcast every 10 s regardless: the satellite's state
    # display rides the data channel, whose one-shot messages are lost
    # whenever its peer connection recycles. The ticker makes the device's
    # status ring self-correct within seconds.
    async def _state_ticker() -> None:
        while True:
            await asyncio.sleep(10)
            try:
                state = getattr(session, "agent_state", None) or "idle"
                await ctx.room.local_participant.publish_data(
                    str(state).encode(), topic="agent_state", reliable=True)
            except Exception:
                logger.debug("state ticker publish failed", exc_info=True)
    asyncio.create_task(_state_ticker())

    # Transcript log (playout truth) + memory writes from the same event.
    last_user = {"text": ""}

    # Idle-sleep for the ESP32 satellite: it runs an on-device wake word and
    # only streams while awake, so it relies on the backend (which has real
    # turn detection) to tell it when to go back to sleep. Track the last user
    # turn; a watchdog sends "sleep" on the "session" topic after a quiet spell.
    SLEEP_AFTER_S = 30.0
    sleep_state = {"last_activity": time.monotonic(), "slept": False}

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
                sleep_state["last_activity"] = time.monotonic()
                sleep_state["slept"] = False
            elif role == "assistant" and memory is not None and last_user["text"]:
                memory.remember(last_user["text"], text, interrupted=interrupted)
        except Exception:  # noqa: BLE001 — logging must never break the pipeline
            logger.debug("conversation_item handler failed", exc_info=True)

    if is_satellite:
        # Device -> agent "wake": the satellite fired its on-device wake word
        # and is about to stream real speech. Resume STT (it was disabled while
        # asleep) with enough lead that the first words aren't dropped.
        @ctx.room.on("data_received")
        def _on_session_data(packet) -> None:
            try:
                if getattr(packet, "topic", None) != "session":
                    return
                if bytes(packet.data or b"") != b"wake":
                    return
                mic["awake"] = True
                mic["cooldown"] = False
                sleep_state["last_activity"] = time.monotonic()
                sleep_state["slept"] = False
                _apply_mic()
                logger.info("satellite wake -> resume STT")
            except Exception:
                logger.debug("wake data handler failed", exc_info=True)

        # The satellite reconnects asleep (it re-arms its wake word on every
        # reconnect). The session persists across the drop, so drop STT here
        # too — otherwise it runs on the device's silent stream until the
        # watchdog self-corrects ~30 s later.
        @ctx.room.on("participant_disconnected")
        def _on_participant_gone(p) -> None:
            mic["awake"] = False
            mic["cooldown"] = False
            sleep_state["slept"] = True
            _apply_mic()

        async def _sleep_watchdog() -> None:
            while True:
                await asyncio.sleep(5)
                idle = time.monotonic() - sleep_state["last_activity"]
                if idle >= SLEEP_AFTER_S and not sleep_state["slept"]:
                    try:
                        await ctx.room.local_participant.publish_data(
                            b"sleep", topic="session", reliable=True)
                        sleep_state["slept"] = True
                        mic["awake"] = False
                        _apply_mic()   # stop STT — no cloud cost while asleep
                        logger.info("satellite idle %.0fs -> sleep", idle)
                    except Exception:
                        logger.debug("sleep publish failed", exc_info=True)
        asyncio.create_task(_sleep_watchdog())

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

    # Phones drop their WebRTC connection constantly (screen lock, network
    # hop). Keep the session alive across disconnects so a reconnect
    # resumes with history intact — the default close_on_disconnect=True
    # closed the session 3 s after a drop while the job kept squatting on
    # the room, which read as "stuck" from the browser.
    @session.on("close")
    def _on_close(ev) -> None:
        # When the session truly closes (room deleted, fatal error), end
        # the job immediately so the room re-dispatches a fresh one.
        ctx.shutdown(reason="session closed")

    await session.start(
        room=ctx.room,
        agent=TvAssistant(pai),
        room_input_options=RoomInputOptions(close_on_disconnect=False),
    )

    # The satellite connects asleep (mic["awake"] starts False): keep STT off
    # until its first on-device "wake", so cloud STT never runs on the idle
    # stream. Non-satellite rooms stay fully enabled.
    if is_satellite:
        _apply_mic()

    # Spoken welcome on fresh sessions: scripted say(), not an LLM turn —
    # instant, deterministic, and immune to model latency. The short sleep
    # lets the phone's audio playback attach so the first words aren't
    # clipped. Reconnects reuse the live session, so no repeat greeting.
    # Room-gated to "voice" (phone/browser) only: the ESP32 satellite (room
    # "speaker") stays asleep until its on-device wake word fires and signals
    # readiness with a local chime, so it must not greet on connect.
    if os.environ.get("VOICE_WELCOME", "1") == "1" and ctx.room.name == "voice":
        from datetime import datetime
        from zoneinfo import ZoneInfo
        hour = datetime.now(ZoneInfo("America/Los_Angeles")).hour
        sal = ("Good morning" if 5 <= hour < 12 else
               "Good afternoon" if 12 <= hour < 18 else "Good evening")
        await asyncio.sleep(1.0)
        session.say(f"{sal} — I'm Minny, your TV-viewing companion. "
                    f"I can check what's on, help you find something, "
                    f"and control the TV when you ask.")


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint,
                              prewarm_fnc=prewarm,
                              load_threshold=0.85))
