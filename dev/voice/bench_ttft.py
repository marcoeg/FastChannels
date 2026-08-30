#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pydantic-ai-slim[openai]==2.31.1",
# ]
# ///
"""TTFT benchmark: gpt-5.6-luna vs gpt-5.6-terra at realistic prompt size.

Streams N no-tool turns per model with the production settings and prints
ms-to-first-text-delta p50/p95. Also dumps the outgoing request's reasoning
config once per model (verifies effort 'none' is actually on the wire).

  ./bench_ttft.py [-n 10] [--models gpt-5.6-luna,gpt-5.6-terra]
"""

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

for line in (Path(__file__).parent / ".env").read_text().splitlines():
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent))

from pydantic_ai import Agent, PartDeltaEvent, PartStartEvent, TextPartDelta  # noqa: E402
from pydantic_ai.messages import TextPart  # noqa: E402
from pydantic_ai.models.openai import (  # noqa: E402
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)

from prompts import format_time_context, load_static_instructions  # noqa: E402

QUESTIONS = [
    "Good evening.",
    "What can you help me with?",
    "Tell me a one sentence fun fact about television.",
    "How should I ask for a channel?",
    "Say a short greeting.",
]

# Synthetic history filler ≈ 8k tokens, mimicking a long session.
_FILLER_TURN = ("We talked about the evening schedule and you suggested a "
                "documentary about the Roman empire on the history channel. " * 8)


async def bench(model_name: str, n: int, verify_wire: bool) -> None:
    settings = OpenAIResponsesModelSettings(
        openai_reasoning_effort=os.environ.get("VOICE_REASONING_EFFORT", "none"),
        openai_service_tier="priority",
        openai_reasoning_context="current_turn",
        max_tokens=700,
    )
    agent = Agent(OpenAIResponsesModel(model_name),
                  instructions=load_static_instructions()
                  + "\n\n" + format_time_context(),
                  model_settings=settings)

    history = None
    pad = "Context recap: " + _FILLER_TURN
    samples: list[float] = []
    for i in range(n):
        q = QUESTIONS[i % len(QUESTIONS)]
        prompt = q if history else pad + "\n\n" + q
        t0 = time.monotonic()
        first = None
        async with agent.run_stream_events(prompt,
                                           message_history=history) as events:
            async for ev in events:
                is_text = (isinstance(ev, PartStartEvent)
                           and isinstance(ev.part, TextPart)) or (
                    isinstance(ev, PartDeltaEvent)
                    and isinstance(ev.delta, TextPartDelta))
                if first is None and is_text:
                    first = (time.monotonic() - t0) * 1000
            history = events.all_messages()
        samples.append(first if first is not None else float("nan"))
        print(f"  {model_name} run {i + 1}: {samples[-1]:.0f} ms")

    ok = [s for s in samples if s == s]
    print(f"{model_name}: p50={statistics.median(ok):.0f} ms  "
          f"p95={sorted(ok)[max(0, int(len(ok) * 0.95) - 1)]:.0f} ms  "
          f"(n={len(ok)})")

    if verify_wire:
        import openai as openai_sdk
        orig = openai_sdk.AsyncClient.post

        async def spy(self, path, **kw):
            body = kw.get("body") or {}
            if "reasoning" in str(body)[:2000] or True:
                print(f"  [wire] {path} reasoning="
                      f"{body.get('reasoning')!r} service_tier="
                      f"{body.get('service_tier')!r}")
            openai_sdk.AsyncClient.post = orig
            return await orig(self, path, **kw)

        openai_sdk.AsyncClient.post = spy
        await agent.run("Say OK.")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--models", default="gpt-5.6-luna,gpt-5.6-terra")
    args = ap.parse_args()
    for m in args.models.split(","):
        await bench(m.strip(), args.n, verify_wire=True)


if __name__ == "__main__":
    asyncio.run(main())
