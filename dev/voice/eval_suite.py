#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pydantic-ai-slim[openai,anthropic,mcp]==2.31.1",
#   "aiohttp",
#   "anthropic<1",   # 1.x needs httpx2; pydantic-ai 2.31.1 passes an httpx client
# ]
# ///
"""Voice-agent eval suite: brain models compared on the household's real requests.

Runs the scenarios in eval/suite.json through the REAL agent (prompts, tools,
lineup scoping, VOICE_TV=ytvn) once per model, and compares the models turn
by turn. It builds on model_bench.py — same dry-run TV (no command reaches
the screen; the guide, title search and web lookup are real), same model
wiring (OpenAI, Anthropic, or a `qwen…` name served by the laptop's
llama.cpp at LOCAL_LLM_BASE_URL), same judge — and adds what a comparison
needs that a single-turn benchmark lacks:

- multi-turn scenarios: each turn runs with the scenario's earlier turns as
  history, exactly as the worker carries it (VoiceBrain.commit_turn);
- a fake screen that follows the dry-run tunes, so "what am I watching"
  after "turn that channel on" has a consistent on-screen line;
- memory notes per scenario (the profile and recent exchanges the worker
  would inject), so memory USE is tested without the memory graph;
- deterministic checks per turn (tool routing, tool arguments, answer
  patterns), then a judge that sees the earlier turns and the scenario's
  pass criteria and grades `meets` plus faithful/helpful/voice;
- a report per model and per category, and the list of scenarios where the
  models disagree.

  ./eval_suite.py --check                         # validate the suite, no model calls
  ./eval_suite.py --list                          # the scenarios
  ./eval_suite.py                                 # VOICE_MODEL vs LOCAL_LLM_MODEL
  ./eval_suite.py --models gpt-5.6-terra,qwen3.5-9b --runs 2
  ./eval_suite.py --category "schedule,tuning" --only B2,B7,B8
  ./eval_suite.py --report ~/news/model-bench/suite-<stamp>.json   # re-print

Results: ~/news/model-bench/suite-YYYYMMDD-HHMM.json (local, never committed).
Volatile scenarios (live schedules) are judged against the tool results of
the moment, and the models run interleaved per scenario, so both see the
same data. See EVAL-SUITE.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics as st
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

# model_bench loads .env, pins VOICE_TV=ytvn and installs the dry-run TV
# (MCPToolset patched so every tv command but get_state returns a fake
# success). Imported for those side effects and for its model/judge code.
import model_bench as mb  # noqa: E402
from pydantic import Field  # noqa: E402
from pydantic_ai import Agent, PromptedOutput  # noqa: E402
from pydantic_ai.mcp import MCPToolset  # noqa: E402
from pydantic_ai.messages import (PartDeltaEvent, PartStartEvent,  # noqa: E402
                                  TextPart, TextPartDelta)

from pai_agent import TurnDeps, VoiceBrain  # noqa: E402
from rollouts import TraceCapability, to_items  # noqa: E402

SUITE = HERE / "eval" / "suite.json"
OUT = Path.home() / "news" / "model-bench"
TURN_TIMEOUT_S = 120

# ---- a fake screen that follows the dry-run tunes ------------------------------------
# model_bench's fake only tracks the player (state, muted, volume); its
# on-screen line always says KTVU. Multi-turn scenarios need the tuned
# channel to change with the tunes and the player to go idle on stop.
SCREEN: dict = {"tuned": None, "playing": None}
LINEUP: dict[str, tuple[str, int | None]] = {}      # lineup channel_id -> (name, number)
_fake_player = mb._fake


def _fake(name: str, args: dict) -> dict:
    p = mb._PLAYER
    if name == "play" and p.get("state") == "idle":
        return {"ok": False, "error": "not_ready: nothing is playing"}
    r = _fake_player(name, args)
    if name in ("tune_channel", "tune_number"):
        cid = str(args.get("channel_id") or args.get("number") or "")
        nm, num = LINEUP.get(cid, (cid, args.get("number")))
        SCREEN["tuned"] = {"channel_id": cid, "number": num, "name": nm, "airing": None}
        SCREEN["playing"] = {"kind": "live", "title": f"{num} {nm} · live"}
    elif name == "stop":
        SCREEN["tuned"], SCREEN["playing"] = None, None
    elif name == "play_asset":
        SCREEN["playing"] = {"kind": "vod", "title": f"on-demand asset {args.get('asset_id')}"}
    elif name == "play_airing":
        kind = "restart" if args.get("mode") == "restart" else "catch-up"
        SCREEN["playing"] = {"kind": kind, "title": str(args.get("airing_id"))}
    return r


async def _read(self, uri):
    if getattr(self, "id", None) == "tv" and str(uri) == "viewer://state":
        return json.dumps({"tuned": SCREEN["tuned"], "player": dict(mb._PLAYER),
                           "playing": SCREEN["playing"]})
    return await mb._real_read(self, uri)


mb._fake = _fake                      # model_bench's _direct/_call look it up by name
MCPToolset.read_resource = _read


def reset_tv(defaults: dict, sc: dict) -> None:
    mb._PLAYER.clear()
    mb._PLAYER.update(mb._PLAYER0)
    mb._PLAYER.update(defaults.get("player") or {})
    mb._PLAYER.update(sc.get("player") or {})
    screen = {**(defaults.get("screen") or {}), **(sc.get("screen") or {})}
    SCREEN["tuned"] = dict(screen["tuned"]) if screen.get("tuned") else None
    SCREEN["playing"] = dict(screen["playing"]) if screen.get("playing") else None


# ---- the suite ------------------------------------------------------------------------
def load_suite(path: Path) -> dict:
    suite = json.loads(path.read_text())
    problems = validate(suite)
    if problems:
        raise SystemExit("suite invalid:\n  " + "\n  ".join(problems))
    return suite


def validate(suite: dict) -> list[str]:
    out, ids = [], set()
    for sc in suite.get("scenarios", []):
        sid = sc.get("id")
        if not sid or sid in ids:
            out.append(f"missing or duplicate id: {sc}")
        ids.add(sid)
        for k in ("category", "source", "turns"):
            if k not in sc:
                out.append(f"{sid}: no {k}")
        for i, t in enumerate(sc.get("turns", []), 1):
            if not t.get("say"):
                out.append(f"{sid} turn {i}: no 'say'")
            if "criteria" not in t:
                out.append(f"{sid} turn {i}: no criteria")
            if "tools_any_of" in t and not isinstance(t["tools_any_of"], list):
                out.append(f"{sid} turn {i}: tools_any_of must be a list of lists")
            for key in ("answer_match", "answer_no_match"):
                pats = t.get(key)
                for p in ([pats] if isinstance(pats, str) else pats or []):
                    try:
                        re.compile(p)
                    except re.error as e:
                        out.append(f"{sid} turn {i}: bad {key} {p!r}: {e}")
            for ta in t.get("tool_args", []):
                if "tool" not in ta or not ("arg" in ta and "regex" in ta or "args" in ta):
                    out.append(f"{sid} turn {i}: tool_args needs tool + (arg, regex) or args")
    return out


def select(suite: dict, only: str | None, category: str | None) -> list[dict]:
    scs = suite["scenarios"]
    if only:
        keep = {x.strip() for x in only.split(",")}
        scs = [s for s in scs if s["id"] in keep]
    if category:
        cats = {x.strip().lower() for x in category.split(",")}
        scs = [s for s in scs if s["category"].lower() in cats]
    return scs


# ---- one turn through the real agent -----------------------------------------------
async def run_turn(brain: VoiceBrain, model, settings, say: str, deps: TurnDeps) -> dict:
    trace = TraceCapability()
    t0, first, text = time.monotonic(), None, []
    try:
        async with brain.agent.run_stream_events(
                await brain.user_prompt(say, deps), model=model, model_settings=settings, deps=deps,
                message_history=list(brain.history), capabilities=[trace]) as ev:
            async for e in ev:
                d = None
                if isinstance(e, PartStartEvent) and isinstance(e.part, TextPart):
                    d = e.part.content
                elif isinstance(e, PartDeltaEvent) and isinstance(e.delta, TextPartDelta):
                    d = e.delta.content_delta
                if d:
                    first = first or (time.monotonic() - t0)
                    text.append(d)
            msgs = list(ev.new_messages())
        err = None
        brain.commit_turn(msgs)              # the next turn sees this one, as in the worker
    except Exception as e:  # noqa: BLE001
        msgs, err = [], f"{type(e).__name__}: {e}"[:300]
    items = to_items(msgs)
    instr = str(getattr((trace.first_messages or [None])[0], "instructions", "") or "")
    calls = [i for i in items if i.get("type") == "function_call"]
    outs = {i.get("call_id"): i.get("output") for i in items if i.get("type") == "function_call_output"}
    return {
        "say": say,
        "ttft_ms": round(first * 1000) if first else None,
        "total_ms": round((time.monotonic() - t0) * 1000),
        "requests": len(trace.requests),
        "tools": [c["name"] for c in calls],
        "calls": [{"name": c["name"], "args": c.get("arguments"),
                   "output": mb._compact(outs.get(c.get("call_id")))} for c in calls],
        "answer": "".join(text).strip(),
        "error": err,
        "on_screen": next((ln for ln in instr.splitlines() if ln.startswith("On screen now")), ""),
        "clock": next((ln for ln in instr.splitlines() if ln.startswith("Clock:")), ""),
    }


# ---- deterministic checks -------------------------------------------------------------
def _args(call: dict) -> dict:
    try:
        return json.loads(call.get("args") or "{}")
    except ValueError:
        return {}


def check_turn(spec: dict, rec: dict, defaults: dict) -> list[str]:
    """The names of the checks this turn fails (empty = all pass)."""
    fails: list[str] = []
    if rec["error"]:
        return ["error"]
    if "tools_any_of" in spec and not mb.routed(rec["tools"], spec["tools_any_of"]):
        fails.append("routing")
    if spec.get("no_tools") and rec["tools"]:
        fails.append("no_tools")
    for ta in spec.get("tool_args", []):
        hits = [_args(c) for c in rec["calls"] if c["name"] == ta["tool"]]
        if "regex" in ta:
            ok = any(re.search(ta["regex"], str(a.get(ta["arg"], ""))) for a in hits)
        else:
            def match(a: dict) -> bool:
                for k, v in ta["args"].items():
                    got = a.get(k)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        try:
                            if abs(float(got) - float(v)) > 0.051:
                                return False
                        except (TypeError, ValueError):
                            return False
                    elif str(got).lower() != str(v).lower():
                        return False
                return True
            ok = any(match(a) for a in hits)
        if not ok:
            fails.append(f"tool_args:{ta['tool']}")
    ans = rec["answer"]
    pats = spec.get("answer_match")
    for p in ([pats] if isinstance(pats, str) else pats or []):
        if not re.search(p, ans):
            fails.append(f"answer_match:{p}")
    pats = spec.get("answer_no_match")
    nos = ([pats] if isinstance(pats, str) else pats or []) + list(defaults.get("answer_no_match") or [])
    for p in nos:
        if re.search(p, ans):
            fails.append(f"answer_no_match:{p}")
    if spec.get("max_total_ms") and rec["total_ms"] > spec["max_total_ms"]:
        fails.append("max_total_ms")
    return fails


# ---- judge ----------------------------------------------------------------------------
class Grade(mb.Grade):
    meets: bool = Field(description="the turn satisfies the stated pass criteria, judged "
                        "against the tool results, the on-screen line, the memory notes and "
                        "the earlier turns — not against any programme names you expect")


JUDGE_PROMPT = mb.JUDGE_PROMPT + """

This turn may be part of a multi-turn scenario: `history` holds the earlier
turns (what the viewer said and what the assistant answered), `memory` the
notes the assistant was given about the household, and `criteria` what the
suite expects of this turn. Grade `meets` strictly against the criteria.
Live schedules change by the hour, so never fault an answer for naming a
programme you did not expect; fault it only when it contradicts or invents
beyond the tool results, the on-screen line or the memory notes."""


async def judge(agent: Agent, sc: dict, turn: dict, rec: dict, history: list[dict]) -> dict | None:
    if rec["error"]:
        return None
    mem = sc.get("memory") or {}
    body = {"request": rec["say"], "criteria": turn.get("criteria", ""),
            "history": history,
            "memory": {"profile": mem.get("profile", []), "recent": mem.get("recent", [])},
            "clock": rec.get("clock", ""), "on_screen": rec.get("on_screen", ""),
            "tool_calls": rec["calls"], "spoken_answer": rec["answer"]}
    try:
        res = await agent.run(json.dumps(body, ensure_ascii=False))
        return res.output.model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


# ---- report ---------------------------------------------------------------------------
def _p(xs, q):
    return sorted(xs)[max(0, int(len(xs) * q) - 1)] if xs else 0


def report(data: dict) -> None:
    models = list(data["results"])
    rows = {m: [t for s in data["results"][m] for t in s["turns"]] for m in models}
    print(f"\n{'model':22} {'turns':>5} {'err':>3} {'routed':>6} {'checks':>6} {'meets':>6} "
          f"{'faith':>5} {'help':>5} {'voice':>5} {'ttft p50':>8} {'p90':>6} {'total p50':>9}")
    for m in models:
        ts = rows[m]
        ok = [t for t in ts if not t["error"]]
        g = [t["grade"] for t in ok if t.get("grade") and "meets" in t["grade"]]
        ttft = [t["ttft_ms"] for t in ok if t["ttft_ms"]]
        print(f"{m:22} {len(ts):5} {len(ts) - len(ok):3} "
              f"{100 * sum('routing' not in t['fails'] for t in ok) / max(1, len(ok)):5.0f}% "
              f"{100 * sum(not t['fails'] for t in ok) / max(1, len(ok)):5.0f}% "
              f"{100 * sum(x['meets'] for x in g) / max(1, len(g)):5.0f}% "
              f"{st.mean(x['faithful'] for x in g) if g else 0:5.2f} "
              f"{st.mean(x['helpful'] for x in g) if g else 0:5.2f} "
              f"{st.mean(x['voice'] for x in g) if g else 0:5.2f} "
              f"{st.median(ttft) if ttft else 0:8.0f} {_p(ttft, 0.9):6.0f} "
              f"{st.median(t['total_ms'] for t in ok) if ok else 0:9.0f}")
    print(f"\n{'category':20} " + " ".join(f"{m[:14]:>14}" for m in models) + "   (checks% / meets%)")
    cats = sorted({s["category"] for m in models for s in data["results"][m]})
    for c in cats:
        cells = []
        for m in models:
            ts = [t for s in data["results"][m] if s["category"] == c for t in s["turns"] if not t["error"]]
            g = [t["grade"] for t in ts if t.get("grade") and "meets" in t["grade"]]
            cells.append(f"{100 * sum(not t['fails'] for t in ts) / max(1, len(ts)):3.0f}/"
                         f"{100 * sum(x['meets'] for x in g) / max(1, len(g)):3.0f}")
        print(f"{c:20} " + " ".join(f"{x:>14}" for x in cells))
    if len(models) >= 2:
        print("\nDisagreements (a turn one model meets and another does not):")
        by = {m: {(s["id"], s["run"], i): t for s in data["results"][m] for i, t in enumerate(s["turns"])} for m in models}
        keys = sorted(set.intersection(*(set(b) for b in by.values())))
        n = 0
        for k in keys:
            verdicts = {m: (by[m][k].get("grade") or {}).get("meets") for m in models}
            if len({v for v in verdicts.values() if v is not None}) > 1:
                n += 1
                sid, run, i = k
                print(f"  {sid} t{i + 1} ({by[models[0]][k]['say'][:50]!r})")
                for m in models:
                    t = by[m][k]
                    print(f"     {m[:14]:14} meets={verdicts[m]!s:5} tools={t['tools']} fails={t['fails']}"
                          f"\n     {'':14} {t['answer'][:150]!r}")
        if not n:
            print("  none")
    print(f"\njudge: {data['judge']}   scenarios: {data['n_scenarios']}   runs: {data['runs']}   "
          "times in ms (ttft = first text; total = whole turn)")


# ---- main -----------------------------------------------------------------------------
async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--suite", type=Path, default=SUITE)
    ap.add_argument("--models", help="comma-separated; default VOICE_MODEL,LOCAL_LLM_MODEL")
    ap.add_argument("--judge", default="claude-opus-5-5")
    ap.add_argument("--no-judge", action="store_true", help="deterministic checks only")
    ap.add_argument("--runs", type=int, default=1, help="repeat each scenario (variance)")
    ap.add_argument("--only", help="scenario ids, comma-separated")
    ap.add_argument("--category", help="categories, comma-separated")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true", help="validate the suite and exit")
    ap.add_argument("--report", help="re-print a saved results file")
    args = ap.parse_args()

    if args.report:
        report(json.loads(Path(args.report).expanduser().read_text()))
        return
    suite = load_suite(args.suite)
    scenarios = select(suite, args.only, args.category)
    if args.check or args.list:
        cats: dict[str, int] = {}
        for s in scenarios:
            cats[s["category"]] = cats.get(s["category"], 0) + len(s["turns"])
            if args.list:
                print(f"{s['id']:5} {s['category']:18} {len(s['turns'])} turn(s)"
                      f"{'  volatile' if s.get('volatile') else ''}  {s['turns'][0]['say'][:60]!r}")
        print(f"\nsuite {suite.get('version')}: {len(scenarios)} scenarios, "
              f"{sum(len(s['turns']) for s in scenarios)} turns, "
              f"{sum(1 for s in scenarios if s.get('volatile'))} volatile; turns per category: {cats}")
        return

    import os
    models = [m.strip() for m in (args.models or "").split(",") if m.strip()] or [
        os.environ.get("VOICE_MODEL", "gpt-5.6-terra"),
        os.environ.get("LOCAL_LLM_MODEL", "qwen3.5-9b")]
    defaults = suite.get("defaults") or {}
    brain = VoiceBrain(None)
    judge_agent = None if args.no_judge else Agent(
        mb.make_model(args.judge)[0], output_type=PromptedOutput(Grade), instructions=JUDGE_PROMPT)
    data = {"started": datetime.now().isoformat(timespec="seconds"), "suite": suite.get("version"),
            "judge": None if args.no_judge else args.judge, "runs": args.runs,
            "n_scenarios": len(scenarios), "results": {m: [] for m in models}}

    async with brain.agent:
        await brain.prime()
        import player_resolve as pr
        try:
            for ch in await pr.lineup():          # the real lineup, read-only, for the fake screen
                LINEUP[ch.channel_id] = (ch.name, ch.number)
                LINEUP[str(ch.number)] = (ch.name, ch.number)
        except Exception as e:  # noqa: BLE001
            print(f"lineup unavailable for the fake screen ({e}); tunes will show raw ids")
        live = []
        for m in models:                           # drop models this box can't reach
            model, settings = mb.make_model(m)
            reset_tv(defaults, {})
            brain.history = []
            r = await run_turn(brain, model, settings, "Say OK.", TurnDeps())
            if r["error"]:
                print(f"skip {m}: {r['error'][:140]}")
            else:
                live.append((m, model, settings))
        for i, sc in enumerate(scenarios, 1):
            for run in range(args.runs):
                order = live if (i + run) % 2 else live[::-1]
                for m, model, settings in order:
                    reset_tv(defaults, sc)
                    brain.history = []
                    mem = sc.get("memory") or {}
                    deps = TurnDeps(facts_profile=list(mem.get("profile", [])),
                                    facts_recent=list(mem.get("recent", [])))
                    turns = []
                    for t in sc["turns"]:
                        try:
                            rec = await asyncio.wait_for(run_turn(brain, model, settings, t["say"], deps),
                                                         TURN_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            rec = {"say": t["say"], "ttft_ms": None, "total_ms": TURN_TIMEOUT_S * 1000,
                                   "requests": 0, "tools": [], "calls": [], "answer": "",
                                   "error": "timeout", "on_screen": "", "clock": ""}
                        rec["fails"] = check_turn(t, rec, defaults)
                        rec["criteria"] = t.get("criteria", "")
                        turns.append(rec)
                        if rec["error"]:
                            break                    # the rest of the scenario has no history
                    data["results"][m].append({"id": sc["id"], "category": sc["category"], "run": run,
                                               "volatile": bool(sc.get("volatile")), "turns": turns})
            print(f"[{i}/{len(scenarios)}] {sc['id']}", flush=True)
        if judge_agent is not None:
            print("judging…", flush=True)
            sem = asyncio.Semaphore(6)
            jobs = []
            for m in live:
                for s in data["results"][m[0]]:
                    sc = next(x for x in scenarios if x["id"] == s["id"])
                    hist: list[dict] = []
                    for t_spec, rec in zip(sc["turns"], s["turns"]):
                        async def grade(rec=rec, t_spec=t_spec, hist=list(hist), sc=sc):
                            async with sem:
                                rec["grade"] = await judge(judge_agent, sc, t_spec, rec, hist)
                        jobs.append(grade())
                        hist.append({"user": rec["say"], "assistant": rec["answer"]})
            await asyncio.gather(*jobs)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"suite-{datetime.now():%Y%m%d-%H%M}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    report(data)
    print(f"saved {path}")


if __name__ == "__main__":
    asyncio.run(main())
