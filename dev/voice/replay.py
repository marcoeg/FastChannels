#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "openai",
# ]
# ///
"""Replay recorded voice turns against the brain model with an edited prompt.

Each case is one turn from the rollouts (~/news/voice-rollouts): the exact
instructions, history (earlier tool calls and their results included),
utterance and tool definitions the model saw. Each variant applies its own
find-and-replace edits to the instructions, and the request goes back to the
same model with production settings: Responses API, instructions as the
top-level parameter (as pydantic-ai 2.31.1 sends them), reasoning effort
VOICE_REASONING_EFFORT, priority tier. Only the model's FIRST response is
kept: its tool calls, or the text it would speak. No tool runs, so nothing
reaches the TV, the guide or memory.

Use it to reproduce a turn-level bug, to compare candidate prompt edits, and
for the old-vs-new diff on real inputs that every behaviour change needs
(BUGLIST.md, "How to work this list"):

  ./replay.py SPEC.json                    # every case x variant x runs
  ./replay.py SPEC.json --runs 1           # quick screen across many turns
  ./replay.py SPEC.json --runs 5 --only iM655z#4,nsoDzT#1   # recheck what differed

SPEC.json:

  {"runs": 5,
   "variants": {"old": [],
                "new": [["<text in the recorded instructions>", "<replacement>"]]},
   "cases": [{"session": "iM655z", "turn": 4},
             {"session": "iM655z", "turn": 4, "user": "The last one.", "id": "last"}]}

- `session` is any suffix of the rollout's session_id, `turn` its turn_index.
- `user` replaces the turn's utterance: same history, different request.
- `"cases": {"dates": ["2026-10-04", "2026-10-05"]}` replays every recorded
  turn of those days instead.
- An edit's first string must occur exactly once in a case's instructions,
  or that case reports an error: a stale anchor can't silently replay the
  unedited prompt. To insert text, replace a nearby line with itself plus
  the new text.

Output: a tally per case and variant (`10x tv_play_asset({"asset_id": "50000"})`),
then, with two or more variants, the cases whose most frequent response
differs from the first variant's. The comparison looks at tool names plus the
arguments of tv_ commands, which decide what the TV does; guide-query and
spoken wording vary from run to run and are ignored. A difference in a single
run is a lead, not a result: recheck it with --runs 5. Results are saved to
~/news/replays/YYYYMMDD-HHMM.json, outside the repo, since rollouts hold
household conversations.

Limits: the first decision only, not the answer after its tools run; the
clock, on-screen line and memory notes are as recorded; OpenAI-brain turns
only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
for _f in (HERE / ".env", Path.home() / ".env"):
    if _f.exists():
        for line in _f.read_text().splitlines():
            line = line.removeprefix("export ").strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

from openai import AsyncOpenAI  # noqa: E402

ROLLOUT_DIR = Path(os.environ.get(
    "VOICE_ROLLOUT_DIR", str(Path.home() / "news" / "voice-rollouts")))
OUT = Path.home() / "news" / "replays"
EFFORT = os.environ.get("VOICE_REASONING_EFFORT", "none")
CONCURRENCY = 8
# Arguments that decide what a tv_ command does; the rest (a title's wording)
# varies between runs without changing the action.
TV_KEYS = ("channel", "asset_id", "airing_id", "mode", "change", "level", "catchup")


def _rid(rec: dict) -> str:
    return f"{rec['session_id'].split(':')[-1][-6:]}#{rec['turn_index']}"


def _records(dates: list[str] | None = None, needles: set[str] | None = None):
    for f in sorted(ROLLOUT_DIR.glob("*.jsonl")):
        if dates is not None and f.stem not in dates:
            continue
        with f.open() as fh:
            for line in fh:
                if needles and not any(n in line for n in needles):
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def resolve(cases) -> list[tuple[str, dict, str | None]]:
    """The spec's cases as (id, rollout record, replacement utterance)."""
    if isinstance(cases, dict):
        recs = [r for r in _records(dates=cases["dates"])
                if r.get("brain", {}).get("llm") == "openai"]
        return [(_rid(r), r, None) for r in recs]
    recs = list(_records(needles={c["session"] for c in cases}))
    out = []
    for c in cases:
        hits = [r for r in recs if r["session_id"].endswith(c["session"])
                and r["turn_index"] == c["turn"]]
        if len(hits) != 1:
            raise SystemExit(f"case {c}: {len(hits)} matching recorded turns")
        user = c.get("user")
        cid = c.get("id") or (_rid(hits[0]) if user is None else f"{_rid(hits[0])} {user!r}")
        out.append((cid, hits[0], user))
    return out


def _edit(text: str, edits: list) -> str:
    for old, new in edits:
        n = text.count(old)
        if n != 1:
            raise ValueError(f"edit anchor found {n}x: {old[:60]!r}")
        text = text.replace(old, new)
    return text


async def first_response(client: AsyncOpenAI, rec: dict, user: str | None,
                         edits: list) -> dict:
    if rec.get("brain", {}).get("llm") != "openai":
        raise ValueError("not an OpenAI-brain turn")
    p = rec["responses_create_params"]
    items = json.loads(json.dumps(p["input"]))
    if items[0].get("role") != "developer":
        raise ValueError("record has no instructions item")
    instructions = _edit(items[0]["content"], edits)
    items = items[1:]
    for it in items:
        it.pop("error", None)               # a rollout annotation, not an API field
    if user is not None:
        if items[-1].get("role") != "user":
            raise ValueError("the last item is not the viewer's utterance")
        items[-1]["content"] = user
    r = await client.responses.create(
        model=rec["brain"]["model"], instructions=instructions, input=items,
        tools=p["tools"], parallel_tool_calls=p.get("parallel_tool_calls", True),
        max_output_tokens=p.get("max_output_tokens", 700),
        reasoning={"effort": EFFORT}, service_tier="priority")
    calls = []
    for o in r.output:
        if o.type == "function_call":
            try:
                calls.append((o.name, json.loads(o.arguments)))
            except ValueError:
                calls.append((o.name, {"_raw": o.arguments}))
    text = " ".join(c.text for o in r.output if o.type == "message"
                    for c in o.content if getattr(c, "text", None))
    return {"calls": calls, "text": text}


def label(r: dict) -> str:
    """A response as the tally prints it: every call with its arguments, or the text."""
    if "error" in r:
        return "ERROR " + r["error"][:120]
    if r["calls"]:
        return "; ".join(f"{n}({json.dumps(a, ensure_ascii=False)})" for n, a in r["calls"])
    return "TEXT " + r["text"][:100]


def signature(r: dict) -> str:
    """A response as the old-vs-new comparison sees it."""
    if "error" in r:
        return "ERROR"
    if not r["calls"]:
        return "TEXT"
    return " + ".join(sorted(
        n + (json.dumps({k: a[k] for k in TV_KEYS if a.get(k) not in (None, "")},
                        ensure_ascii=False) if n.startswith("tv_") else "")
        for n, a in r["calls"]))


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("spec", type=Path)
    ap.add_argument("--runs", type=int, help="override the spec's runs")
    ap.add_argument("--only", help="comma-separated case ids to replay")
    ap.add_argument("--out", type=Path, help="results file (default ~/news/replays/)")
    args = ap.parse_args()

    spec = json.loads(args.spec.read_text())
    runs = args.runs or spec.get("runs", 1)
    variants: dict[str, list] = spec["variants"]
    cases = resolve(spec["cases"])
    if args.only:
        keep = set(args.only.split(","))
        if unknown := keep - {c[0] for c in cases}:
            raise SystemExit(f"--only: no such case: {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c[0] in keep]
    print(f"{len(cases)} cases x {len(variants)} variants x {runs} runs, "
          f"effort={EFFORT}", flush=True)

    client, sem = AsyncOpenAI(), asyncio.Semaphore(CONCURRENCY)

    async def job(rec, user, edits):
        async with sem:
            try:
                return await first_response(client, rec, user, edits)
            except Exception as e:  # noqa: BLE001 — one bad case mustn't sink the run
                return {"error": f"{type(e).__name__}: {e}"[:300]}

    keys, jobs = [], []
    for cid, rec, user in cases:
        for v, edits in variants.items():
            for _ in range(runs):
                keys.append((cid, v))
                jobs.append(job(rec, user, edits))
    results: dict[str, dict[str, list]] = {}
    for (cid, v), r in zip(keys, await asyncio.gather(*jobs)):
        results.setdefault(cid, {}).setdefault(v, []).append(r)

    width = max((len(c) for c in results), default=8)
    for cid, vs in results.items():
        for v, rs in vs.items():
            for lab, n in Counter(label(r) for r in rs).most_common():
                print(f"{cid:{width}}  {v:8} {n:2}x  {lab}")
    errors = sum("error" in r for vs in results.values() for rs in vs.values() for r in rs)
    if errors:
        print(f"\n{errors} responses are errors (see the tally above)")

    names = list(variants)
    for v in names[1:]:
        diff, failed = [], 0
        for cid, vs in results.items():
            a, b = (Counter(map(signature, vs[n])).most_common(1)[0][0] for n in (names[0], v))
            if "ERROR" in (a, b):
                failed += 1
            elif a != b:
                diff.append(cid)
        print(f"\n{v} vs {names[0]}: {len(diff)} of {len(results) - failed} cases differ"
              + (f" ({failed} not compared: errors)" if failed else ""))
        if diff:
            print(f"  recheck: --runs 5 --only {','.join(diff)}")

    path = args.out or OUT / f"{datetime.now():%Y%m%d-%H%M}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"spec": spec, "runs": runs, "effort": EFFORT,
                                "results": results}, ensure_ascii=False, indent=1))
    print(f"saved {path}")


if __name__ == "__main__":
    asyncio.run(main())
