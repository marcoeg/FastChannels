#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pydantic-ai-slim[openai,mcp]==2.31.1",
# ]
# ///
"""One-shot brain + MCP smoke test — no LiveKit, no audio.

Builds the real VoiceBrain (default VOICE_EPG_BACKEND=minny), runs a single
query through the agent (starting the MCP subprocess via `async with`), and
prints the tool-call order and the spoken answer. Needs the Minerva VPN up.

Usage:  ./minny_brain_check.py ["question"]
"""
import asyncio
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
for line in (HERE / ".env").read_text().splitlines():
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
os.environ.setdefault("VOICE_EPG_BACKEND", "minny")
sys.path.insert(0, str(HERE))

from pai_agent import VoiceBrain  # noqa: E402


def _tool_calls(messages) -> list[str]:
    names = []
    for m in messages:
        for p in getattr(m, "parts", []) or []:
            if getattr(p, "part_kind", "") == "tool-call":
                names.append(getattr(p, "tool_name", "?"))
    return names


async def main() -> None:
    q = sys.argv[1] if len(sys.argv) > 1 else "what's on now on Fox Sports?"
    print(f"backend={os.environ['VOICE_EPG_BACKEND']}  query={q!r}\n")
    brain = VoiceBrain(memory=None)
    t = time.monotonic()
    try:
        async with brain.agent:
            deps = brain.make_deps(None)
            r = await brain.agent.run(await brain.user_prompt(q, deps), deps=deps)
    except Exception as e:  # noqa: BLE001
        print(f"FAILED after {time.monotonic() - t:.1f}s: {type(e).__name__}: {e}")
        print("If this is a connection error, the Minerva VPN is likely down — "
              "see /home/marco/Development/MinnyAgent/VPN-CONNECT.md")
        raise
    dt = time.monotonic() - t
    print(f"tool calls (in order): {_tool_calls(r.all_messages())}")
    print(f"\nanswer: {r.output}")
    print(f"\n[{dt:.1f}s]")


asyncio.run(main())
