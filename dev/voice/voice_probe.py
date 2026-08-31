#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "livekit-agents[openai,silero,elevenlabs]==1.7.1",
#   "pydantic-ai-slim[openai,mcp]==2.31.1",
#   "graphiti-core[falkordb]==0.29.3",
#   "livekit-api",
# ]
# ///
# (Worker's dep block + livekit-api, so the big env is reused.)
"""End-to-end AUDIO probe for the voice agent — no phone required.

Joins a throwaway LiveKit room (the worker serves any room), publishes a
synthesized spoken question as a real mic track, then measures the agent's
audio reply: frames received, seconds of audio, peak RMS. This exercises
the full path the browser uses — VAD, streaming STT, LLM, TTS, WebRTC —
and prints hard numbers instead of "I didn't hear anything".

  ./voice_probe.py ["question to speak"] [--room voice-probe]
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

for line in (Path(__file__).parent / ".env").read_text().splitlines():
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import numpy as np  # noqa: E402  (via livekit-agents)
from livekit import api, rtc  # noqa: E402

SAMPLE_RATE = 24000


async def synthesize(text: str) -> bytes:
    """Spoken question as 24 kHz mono PCM16 via OpenAI TTS."""
    from openai import AsyncOpenAI
    client = AsyncOpenAI()
    resp = await client.audio.speech.create(
        model="gpt-4o-mini-tts", voice="alloy", input=text,
        response_format="pcm")  # 24 kHz mono s16le
    return resp.content


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?", default="What is playing on the TV right now?")
    ap.add_argument("--room", default="voice-probe")
    ap.add_argument("--listen", type=float, default=25.0)
    args = ap.parse_args()

    token = (api.AccessToken(os.environ["LIVEKIT_API_KEY"],
                             os.environ["LIVEKIT_API_SECRET"])
             .with_identity("probe")
             .with_grants(api.VideoGrants(room_join=True, room=args.room,
                                          can_publish=True, can_subscribe=True))
             .to_jwt())

    room = rtc.Room()
    stats = {"frames": 0, "samples": 0, "peak_rms": 0.0, "first_audio_ts": None}
    agent_seen = asyncio.Event()
    spoke_done: float | None = None

    @room.on("track_subscribed")
    def on_track(track, pub, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO and participant.identity != "probe":
            agent_seen.set()
            print(f"subscribed to agent audio track from {participant.identity}")

            async def drain():
                stream = rtc.AudioStream(track)
                async for ev in stream:
                    frame = ev.frame
                    data = np.frombuffer(frame.data, dtype=np.int16)
                    rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
                    if rms > 100 and stats["first_audio_ts"] is None:
                        stats["first_audio_ts"] = time.monotonic()
                        if spoke_done:
                            print(f"FIRST AUDIBLE AUDIO: "
                                  f"{stats['first_audio_ts'] - spoke_done:.2f}s "
                                  f"after question ended")
                    stats["frames"] += 1
                    stats["samples"] += len(data)
                    stats["peak_rms"] = max(stats["peak_rms"], rms)
            asyncio.create_task(drain())

    url = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    await room.connect(url, token)
    print(f"connected to room {args.room}; waiting for agent…")

    source = rtc.AudioSource(SAMPLE_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("probe-mic", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    try:
        await asyncio.wait_for(agent_seen.wait(), 20)
    except asyncio.TimeoutError:
        print("FAIL: no agent audio track appeared within 20 s")
        await room.disconnect()
        sys.exit(1)

    await asyncio.sleep(2)  # let the agent's pipeline settle

    print(f"speaking: {args.question!r}")
    pcm = await synthesize(args.question)
    samples = np.frombuffer(pcm, dtype=np.int16)
    frame_len = SAMPLE_RATE // 100  # 10 ms frames
    for i in range(0, len(samples) - frame_len, frame_len):
        frame = rtc.AudioFrame(
            data=samples[i:i + frame_len].tobytes(), sample_rate=SAMPLE_RATE,
            num_channels=1, samples_per_channel=frame_len)
        await source.capture_frame(frame)
    silence = np.zeros(frame_len, dtype=np.int16).tobytes()
    for _ in range(150):  # 1.5 s of silence so VAD closes the turn
        await source.capture_frame(rtc.AudioFrame(
            data=silence, sample_rate=SAMPLE_RATE, num_channels=1,
            samples_per_channel=frame_len))
    spoke_done = time.monotonic()
    print("question sent; listening…")

    await asyncio.sleep(args.listen)
    await room.disconnect()

    secs = stats["samples"] / 48000  # agent publishes 48 kHz
    print(f"\nRESULT: {stats['frames']} frames, ~{secs:.1f}s of audio, "
          f"peak RMS {stats['peak_rms']:.0f}")
    if stats["peak_rms"] > 500:
        print("PASS: audible agent speech came back")
    elif stats["frames"] > 0:
        print("FAIL: track delivered frames but they are (near-)SILENT")
    else:
        print("FAIL: no audio frames at all")


if __name__ == "__main__":
    asyncio.run(main())
