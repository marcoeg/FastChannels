#!/usr/bin/env python3
"""Build NeMo Gym datasets from the voice agent's rollout traces.

Reads ~/news/voice-rollouts/YYYY-MM-DD.jsonl (written by rollouts.py) and
writes, into --out:

  eval.jsonl  one line per usable turn: `responses_create_params` (the exact
              prompt + tools the model saw) plus `expected_answer` (the
              teacher's final spoken answer, when the turn came from the
              OpenAI brain) and `metadata` — NeMo Gym eval / RL prompts.
  sft.jsonl   teacher (OpenAI brain) turns as {"messages": prompt +
              trajectory, "tools": [...]} — distillation data for the
              local model.

A turn is "usable" unless it was cancelled before any speech (preemptive
generation), failed, or (by default) interrupted. Each record also gets the
implicit signal `viewer_corrected_next`: the viewer's NEXT utterance in the
same session reads like a correction ("no", "that's wrong", "I said …").

Everything is validated: lines without a non-empty
responses_create_params.input and tools are reported and dropped (NeMo Gym
would skip them silently).

  ./rollouts_export.py --out /tmp/minny-gym              # all days
  ./rollouts_export.py --since 2026-10-01 --room speaker --llm openai
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

SRC = Path.home() / "news" / "voice-rollouts"
CORRECTION = re.compile(
    r"^\s*(no\b|nope|that'?s (not|wrong)|wrong|i said|not that|i meant|"
    r"you (misunderstood|got it wrong)|actually\b)", re.I)


def load(since: str | None, until: str | None) -> list[dict]:
    recs = []
    for f in sorted(SRC.glob("*.jsonl")):
        day = f.stem
        if (since and day < since) or (until and day > until):
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"skip {f.name}:{n}: bad JSON", file=sys.stderr)
    return recs


def add_correction_signal(recs: list[dict]) -> None:
    by_session = defaultdict(list)
    for r in recs:
        by_session[r.get("session_id")].append(r)
    for turns in by_session.values():
        turns.sort(key=lambda r: r.get("turn_index", 0))
        for cur, nxt in zip(turns, turns[1:]):
            cur.setdefault("signals", {})["viewer_corrected_next"] = bool(
                CORRECTION.match(nxt.get("user_text", "")))


def final_answer(rec: dict) -> str | None:
    msgs = [i for i in rec["response"]["output"] if i.get("role") == "assistant"]
    return msgs[-1]["content"] if msgs else None


def valid(rcp: dict) -> bool:
    return bool(rcp.get("input")) and isinstance(rcp.get("tools"), list) \
        and len(rcp["tools"]) > 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="minny-gym", type=Path)
    ap.add_argument("--since"), ap.add_argument("--until")
    ap.add_argument("--room", help="voice | speaker | …")
    ap.add_argument("--llm", help="openai | local")
    ap.add_argument("--include-interrupted", action="store_true")
    args = ap.parse_args()

    recs = load(args.since, args.until)
    add_correction_signal(recs)
    stats = defaultdict(int)
    eval_lines, sft_lines = [], []
    for r in recs:
        stats["read"] += 1
        sig = r.get("signals", {})
        if args.room and r.get("room") != args.room:
            stats["filtered: room"] += 1; continue
        if args.llm and r.get("brain", {}).get("llm") != args.llm:
            stats["filtered: brain"] += 1; continue
        if sig.get("cancelled_before_speech"):
            stats["dropped: preemptive cancel"] += 1; continue
        if r.get("error"):
            stats["dropped: error"] += 1; continue
        if r.get("interrupted") and not args.include_interrupted:
            stats["dropped: interrupted"] += 1; continue
        rcp = r.get("responses_create_params", {})
        if not valid(rcp):
            stats["dropped: invalid"] += 1; continue

        teacher = r.get("brain", {}).get("llm") == "openai"
        answer = final_answer(r)
        meta = {k: r.get(k) for k in ("session_id", "turn_index", "turn_id",
                                      "room", "brain", "ts_start")}
        meta["signals"] = sig
        line = {"responses_create_params": rcp, "metadata": meta}
        if teacher and answer:
            line["expected_answer"] = answer
        eval_lines.append(line)
        if teacher and answer and not sig.get("viewer_corrected_next"):
            sft_lines.append({"messages": rcp["input"] + r["response"]["output"],
                              "tools": rcp["tools"], "metadata": meta})

    args.out.mkdir(parents=True, exist_ok=True)
    for name, lines in (("eval.jsonl", eval_lines), ("sft.jsonl", sft_lines)):
        with open(args.out / name, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"{stats['read']} turns read → eval {len(eval_lines)}, "
          f"sft {len(sft_lines)} in {args.out}/")
    for k, v in sorted(stats.items()):
        if k != "read":
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
