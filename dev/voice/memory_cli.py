#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "livekit-agents[openai,silero]==1.7.1",
#   "pydantic-ai-slim[openai,mcp]==2.31.1",
#   "graphiti-core[falkordb]==0.29.3",
# ]
# ///
# (Same dep block as voice_agent.py on purpose: graphiti-core needs httpx/
# openai transitively, and matching blocks share one cached uv environment.)
"""Inspect (or reset) the voice agent's memory graph.

  ./memory_cli.py stats                  node/edge/episode counts
  ./memory_cli.py facts "query" [-n 10]  deep_search results
  ./memory_cli.py episodes [-n 10]       recent raw episodes
  ./memory_cli.py profile                EXACTLY the block injected per turn
  ./memory_cli.py wipe --yes             dev reset: drop graph, rebuild indices
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

for line in (Path(__file__).parent / ".env").read_text().splitlines():
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent))


async def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    p_facts = sub.add_parser("facts")
    p_facts.add_argument("query")
    p_facts.add_argument("-n", type=int, default=10)
    p_eps = sub.add_parser("episodes")
    p_eps.add_argument("-n", type=int, default=10)
    sub.add_parser("profile")
    p_wipe = sub.add_parser("wipe")
    p_wipe.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    from memory import VOICE_MEMORY_GROUP, VoiceMemory  # noqa: E402
    mem = await VoiceMemory.create(
        port=int(os.environ.get("VOICE_FALKORDB_PORT", "6390")),
        password=os.environ.get("VOICE_FALKORDB_PASSWORD"))
    driver = mem._driver
    g = mem._graphiti

    try:
        if args.cmd == "stats":
            for label, q in [
                    ("entities", "MATCH (n:Entity) RETURN count(n)"),
                    ("edges", "MATCH ()-[e:RELATES_TO]->() RETURN count(e)"),
                    ("episodes", "MATCH (n:Episodic) RETURN count(n)")]:
                rows, _, _ = await driver.execute_query(q)
                print(f"{label}: {rows[0][list(rows[0].keys())[0]] if rows else 0}")
        elif args.cmd == "facts":
            for f in await mem.deep_search(args.query, limit=args.n):
                print(f"- {f}")
        elif args.cmd == "episodes":
            from datetime import datetime, timezone
            eps = await g.retrieve_episodes(
                reference_time=datetime.now(timezone.utc),
                last_n=args.n, group_ids=[VOICE_MEMORY_GROUP])
            for ep in eps:
                print(f"[{ep.valid_at}] {ep.name}\n  "
                      + str(ep.content).replace("\n", "\n  "))
        elif args.cmd == "profile":
            from prompts import format_memory_context
            r = await mem.recall("")
            block = format_memory_context(r.profile, r.recent)
            print(block or "(empty — nothing would be injected)")
        elif args.cmd == "wipe":
            if not args.yes:
                sys.exit("refusing without --yes")
            await driver.execute_query("MATCH (n) DETACH DELETE n")
            await driver.build_indices_and_constraints()
            print("voice_memory wiped, indices rebuilt")
    finally:
        await mem.aclose(flush_timeout=1)


if __name__ == "__main__":
    asyncio.run(main())
