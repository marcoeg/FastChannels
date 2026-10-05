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
  ./memory_cli.py prune [--days N] [--dry-run]   (default: the worker's retention)
                                         retention: drop old episodes except those
                                         that created a current household fact
  ./memory_cli.py wipe --yes             dev reset: drop graph, rebuild indices
  ./memory_cli.py --test <command>       the same on the test-room graph (voice_memory_test)
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
    ap.add_argument("--test", action="store_true",
                    help="the test-room graph (voice_memory_test) instead of the household's")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    p_facts = sub.add_parser("facts")
    p_facts.add_argument("query")
    p_facts.add_argument("-n", type=int, default=10)
    p_eps = sub.add_parser("episodes")
    p_eps.add_argument("-n", type=int, default=10)
    sub.add_parser("profile")
    p_prune = sub.add_parser("prune")
    p_prune.add_argument("--days", type=float, default=None,
                         help="default: VOICE_MEMORY_RETENTION_DAYS (as the worker)")
    p_prune.add_argument("--dry-run", action="store_true")
    p_wipe = sub.add_parser("wipe")
    p_wipe.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    from memory import VoiceMemory  # noqa: E402
    mem = await VoiceMemory.create(
        port=int(os.environ.get("VOICE_FALKORDB_PORT", "6390")),
        password=os.environ.get("VOICE_FALKORDB_PASSWORD"),
        test=args.test)
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
                last_n=args.n, group_ids=[mem._group])
            for ep in eps:
                print(f"[{ep.valid_at}] {ep.name}\n  "
                      + str(ep.content).replace("\n", "\n  "))
        elif args.cmd == "profile":
            from prompts import format_memory_context
            # create() warms the profile cache in the background; without
            # waiting, recall() races it and shows an empty profile.
            if mem.profile_ready is not None:
                await mem.profile_ready
            r = await mem.recall("")
            block = format_memory_context(r.profile, r.recent)
            print(block or "(empty — nothing would be injected)")
        elif args.cmd == "prune":
            from memory import RETENTION_DAYS
            days = args.days if args.days is not None else RETENTION_DAYS
            if days <= 0:
                sys.exit("retention is off (VOICE_MEMORY_RETENTION_DAYS=0); "
                         "pass --days to prune anyway")
            print(f"pruning episodes older than {days:g} days"
                  f"{' (dry run)' if args.dry_run else ''}")
            print(await mem.prune(days, dry_run=args.dry_run))
        elif args.cmd == "wipe":
            if not args.yes:
                sys.exit("refusing without --yes")
            await driver.execute_query("MATCH (n) DETACH DELETE n")
            await driver.build_indices_and_constraints()
            print(f"{mem._group} wiped, indices rebuilt")
            if args.test:
                from datetime import datetime, timezone
                from memory import TEST_RESET_MARKER
                TEST_RESET_MARKER.write_text(datetime.now(timezone.utc).isoformat())
                print("test sessions start without earlier-session history from now")
    finally:
        await mem.aclose(flush_timeout=1)


if __name__ == "__main__":
    asyncio.run(main())
