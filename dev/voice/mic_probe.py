#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["livekit", "pyjwt", "numpy"]
# ///
"""Record what the agent actually hears from the ESP32 satellite's mic.

Joins room "speaker" as a hidden, subscribe-only participant, captures the
esp32 participant's audio track for --seconds (default 60), writes a 16 kHz
mono WAV, and prints level statistics (RMS, peak, clipping, speech windows).

Usage: ./mic_probe.py out.wav [--seconds 60] [--identity esp32]
"""

import argparse
import asyncio
import sys
import time
import wave
from pathlib import Path

import jwt
import numpy as np
from livekit import rtc

HERE = Path(__file__).parent
env = dict(
    line.split("=", 1)
    for line in (HERE / ".env").read_text().splitlines()
    if line and not line.startswith("#") and "=" in line
)

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("--seconds", type=int, default=60)
ap.add_argument("--identity", default="esp32")
ap.add_argument("--room", default="speaker")
args = ap.parse_args()

now = int(time.time())
token = jwt.encode(
    {"iss": env["LIVEKIT_API_KEY"].strip(), "sub": "mic-probe",
     "nbf": now - 60, "exp": now + 600,
     "video": {"room": args.room, "roomJoin": True,
               "canSubscribe": True, "canPublish": False, "hidden": True}},
    env["LIVEKIT_API_SECRET"].strip(), algorithm="HS256")


async def main() -> None:
    room = rtc.Room()
    chunks: list[bytes] = []
    got_track = asyncio.Event()

    @room.on("track_subscribed")
    def on_sub(track, pub, participant):
        if participant.identity == args.identity and \
                track.kind == rtc.TrackKind.KIND_AUDIO:
            print(f"subscribed to {participant.identity}'s audio", flush=True)
            got_track.set()
            asyncio.create_task(reader(track))

    async def reader(track) -> None:
        stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
        async for ev in stream:
            chunks.append(bytes(ev.frame.data))

    await room.connect("ws://127.0.0.1:7880", token)
    print("connected; waiting for the device track...", flush=True)
    try:
        await asyncio.wait_for(got_track.wait(), 15)
    except asyncio.TimeoutError:
        print("ERROR: no esp32 audio track appeared within 15 s", flush=True)
        await room.disconnect()
        sys.exit(1)

    print(f"recording {args.seconds} s...", flush=True)
    await asyncio.sleep(args.seconds)
    await room.disconnect()

    pcm = b"".join(chunks)
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if len(x) == 0:
        print("no audio captured")
        return
    win = 16000 // 4  # 250 ms windows
    n = len(x) // win
    rms = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1))
    rms_db = 20 * np.log10(np.maximum(rms, 1e-7))
    peak = float(np.abs(x).max())
    clip_pct = float((np.abs(x) > 0.985).mean() * 100)
    speech = rms_db > -35  # crude speech-ish threshold
    print(f"duration: {len(x)/16000:.1f} s   peak: {20*np.log10(max(peak,1e-7)):.1f} dBFS"
          f"   clipping: {clip_pct:.3f}%")
    print(f"noise floor (p10 rms): {np.percentile(rms_db,10):.1f} dBFS   "
          f"speech level (p95 rms): {np.percentile(rms_db,95):.1f} dBFS")
    print(f"windows above -35 dBFS: {int(speech.sum())}/{n} "
          f"({100*speech.mean():.0f}%)")
    print(f"wrote {args.out}")


asyncio.run(main())
