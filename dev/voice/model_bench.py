#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pydantic-ai-slim[openai,anthropic,mcp]==2.31.1",
#   "aiohttp",
#   "anthropic<1",   # 1.x needs httpx2; pydantic-ai 2.31.1 passes an httpx client
# ]
# ///
"""Brain model benchmark: the real voice agent (prompts, tools, lineup
scoping, VOICE_TV=ytvn) on a fixed set of voice requests, once per model.

TV control is a DRY RUN: every ytvn command except get_state returns a fake
success, so nothing changes on screen. Guide, title search and web lookup are
real. Each question runs single-turn with fresh history, models interleaved
per question so network drift hits all of them alike.

Per turn: time to first text, total time, model requests, tools called (with
arguments), the answer. Then a judge model (not a contestant) scores routing,
faithfulness to the tool results, helpfulness and voice style.

  ./model_bench.py                                   # default model set
  ./model_bench.py --models gpt-5.6-terra,claude-haiku-4-5 --judge claude-opus-5-5
  ./model_bench.py --report ~/news/model-bench/<file>.json   # re-print a run

Results: ~/news/model-bench/YYYYMMDD-HHMM.json (local, never committed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics as st
import sys
import time
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
os.environ["VOICE_TV"] = "ytvn"
os.environ.setdefault("VOICE_EPG_BACKEND", "minny")
sys.path.insert(0, str(HERE))

from pydantic import BaseModel, Field  # noqa: E402
from pydantic_ai import Agent, PromptedOutput  # noqa: E402
from pydantic_ai.mcp import MCPToolset  # noqa: E402
from pydantic_ai.messages import (PartDeltaEvent, PartStartEvent,  # noqa: E402
                                  TextPart, TextPartDelta)

from pai_agent import VoiceBrain  # noqa: E402
from rollouts import TraceCapability, to_items  # noqa: E402

OUT = Path.home() / "news" / "model-bench"
DEFAULT_MODELS = ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol",
                  "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol",
                  "claude-haiku-4-5", "claude-sonnet-5-5", "claude-fable-5-1"]

# (request, acceptable tool sets — any one fully called counts as routed right;
#  [] = should answer without tools)
QUESTIONS: list[tuple[str, list[list[str]]]] = [
    ("What's on TV right now?", [["minny_whats_on_now"]]),
    ("Is there any Columbo tonight?", [["minny_search_live_schedule"]]),
    ("Switch channel to Fox, channel 2.", [["tv_watch_channel"]]),
    ("Tune on KNTV.", [["tv_watch_channel"]]),
    ("Switch to channel 1001.", [["tv_watch_channel"]]),
    ("What is on KTVU?", [["minny_whats_on_now"]]),
    ("What's on next on KQED?", [["minny_search_live_schedule"]]),
    ("Any movie I can watch at around 5 o'clock today?", [["minny_search_live_schedule"]]),
    ("Play TMZ on KTBU.", [["tv_watch_channel"]]),
    ("Play the BBC World News America catch-up on Channel 9.",
     [["minny_search_catchup", "tv_play_airing"]]),
    ("I want to watch a catch-up of BBC News.", [["minny_search_catchup"]]),
    ("Start PBS NewsHour over from the beginning.",       # may not be on air: any guide check
     [["minny_whats_on_now"], ["minny_search_live_schedule"], ["minny_search_catchup"]]),
    ("Put on Dune Part 2.", [["tv_watch_title"]]),
    ("Play Inside Out 2.", [["tv_watch_title"]]),
    ("Put on Trace.", [["tv_watch_channel"], ["tv_watch_title"]]),
    ("Turn the volume down a bit.", [["tv_volume"]]),
    ("Turn it up.", [["tv_volume"]]),
    ("Set the volume to fifty percent.", [["tv_volume"]]),
    ("Mute the TV.", [["tv_mute"]]),
    ("Pause it.", [["tv_pause"]]),
    ("Who directed Dune Part Two?", [["web_lookup"], []]),
    ("Is there any news on right now?", [["minny_whats_on_now"], ["minny_search_live_schedule"]]),
    ("What's on ESPN?", [["minny_whats_on_now"], ["tv_watch_channel"], []]),
    ("What am I watching right now?", [["tv_get_state"], ["minny_whats_on_now"], []]),   # the on-screen line answers it
    ("Any cooking shows on tonight?", [["minny_search_live_schedule"]]),
    ("Put on CBS.", [["tv_watch_channel"]]),
]

# ---- TV dry run ---------------------------------------------------------------------
_real_direct, _real_call = MCPToolset.direct_call_tool, MCPToolset.call_tool
DRY = {"tune_channel", "tune_number", "play_asset", "play_airing", "play_url",
       "stop", "play", "pause", "mute", "unmute", "volume"}


_PLAYER = {"state": "playing", "muted": False, "volume": 0.6}


def _fake(name: str, args: dict) -> dict:
    """A plausible resulting state: models read it to confirm the action."""
    p = _PLAYER
    if name == "mute":
        p["muted"] = True
    elif name == "unmute":
        p["muted"] = False
    elif name == "pause":
        p["state"] = "paused"
    elif name in ("play", "tune_channel", "tune_number", "play_asset", "play_airing", "play_url"):
        p["state"] = "playing"
    elif name == "stop":
        p["state"] = "idle"
    elif name == "volume":                     # as the page does: step 0.1, up unmutes
        ch = args.get("change", "set")
        if ch == "up":
            p["volume"], p["muted"] = min(1.0, round(p["volume"] + 0.1, 3)), False
        elif ch == "down":
            p["volume"] = max(0.0, round(p["volume"] - 0.1, 3))
        else:
            p["volume"] = args.get("level", p["volume"])
    return {"ok": True, "dry_run": True, "command": name, "args": args, "player": dict(p)}


async def _direct(self, name, args, **kw):
    if getattr(self, "id", None) == "tv" and name in DRY:
        return _fake(name, args)
    return await _real_direct(self, name, args, **kw)


async def _call(self, name, tool_args, ctx, tool):
    if getattr(self, "id", None) == "tv" and name in DRY:
        return _fake(name, tool_args)
    return await _real_call(self, name, tool_args, ctx, tool)


_real_read = MCPToolset.read_resource


async def _read(self, uri):
    # The on-screen line must describe the DRY-RUN player, not the real TV
    # (the judge faulted "turned it up" against the real TV's 100%).
    if getattr(self, "id", None) == "tv" and str(uri) == "viewer://state":
        return json.dumps({"tuned": {"channel_id": "2581", "number": 2, "name": "KTVU", "airing": None},
                           "player": dict(_PLAYER),
                           "playing": {"kind": "live", "title": "2 KTVU · live"}})
    return await _real_read(self, uri)


MCPToolset.direct_call_tool, MCPToolset.call_tool = _direct, _call
MCPToolset.read_resource = _read


# ---- models -------------------------------------------------------------------------
# Models that refuse reasoning effort "none" run at their lowest supported level.
MIN_EFFORT = {"gpt-6.1-sol": "low"}


def make_model(name: str):
    if name.startswith("qwen"):                # the local llama.cpp server, as VOICE_LLM=local
        from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
        from pydantic_ai.profiles.openai import OpenAIModelProfile
        from pydantic_ai.providers.openai import OpenAIProvider
        provider = OpenAIProvider(base_url=os.environ["LOCAL_LLM_BASE_URL"],
                                  api_key=os.environ["LOCAL_LLM_API_KEY"])
        return OpenAIChatModel(name, provider=provider, profile=OpenAIModelProfile(
            openai_chat_supports_multiple_system_messages=False)), OpenAIChatModelSettings(
            parallel_tool_calls=True, max_tokens=700,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    if name.startswith("claude"):
        from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
        return AnthropicModel(name), AnthropicModelSettings(
            max_tokens=700, parallel_tool_calls=True,
            anthropic_cache_instructions=True, anthropic_cache_tool_definitions=True)
    from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
    return OpenAIResponsesModel(name), OpenAIResponsesModelSettings(
        openai_reasoning_effort=MIN_EFFORT.get(name, "none"), openai_service_tier="priority",
        openai_reasoning_context="current_turn", parallel_tool_calls=True,
        max_tokens=700)


async def run_turn(brain: VoiceBrain, model, settings, q: str) -> dict:
    trace = TraceCapability()
    t0 = time.monotonic()
    first = None
    text: list[str] = []
    try:
        async with brain.agent.run_stream_events(
                q, model=model, model_settings=settings,
                deps=brain.make_deps(None), capabilities=[trace]) as ev:
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
    except Exception as e:  # noqa: BLE001
        msgs, err = [], f"{type(e).__name__}: {e}"[:300]
    items = to_items(msgs)
    # What the model was told about the screen and the clock (its instructions):
    # the judge can't grade "what am I watching" or "what's next" without them.
    instr = str(getattr((trace.first_messages or [None])[0], "instructions", "") or "")
    screen = next((ln for ln in instr.splitlines() if ln.startswith("On screen now")), "")
    clock = next((ln for ln in instr.splitlines() if ln.startswith("Clock:")), "")
    calls = [i for i in items if i.get("type") == "function_call"]
    outs = {i.get("call_id"): i.get("output") for i in items
            if i.get("type") == "function_call_output"}
    return {
        "ttft_ms": round(first * 1000) if first else None,
        "total_ms": round((time.monotonic() - t0) * 1000),
        "requests": len(trace.requests),
        "tools": [c["name"] for c in calls],
        "calls": [{"name": c["name"], "args": c.get("arguments"),
                   "output": _compact(outs.get(c.get("call_id")))} for c in calls],
        "answer": "".join(text).strip(),
        "error": err,
        "on_screen": screen,
        "clock": clock,
    }


KEEP = ("title", "episode_title", "channel_name", "channel_number", "start", "end",
        "start_local", "end_local", "on_now", "description",
        "airing_id", "kind", "name", "number", "asset_id", "year", "program_type")


def _compact(out) -> str:
    """Tool output for the judge: every row, only the fields an answer can
    cite — full guide rows are ~600 chars each and a 52-channel listing
    would otherwise be cut off and read as 'not in the results'."""
    data = out
    if isinstance(out, str):
        try:
            data = json.loads(out)
        except ValueError:
            return out[:6000]
    if isinstance(data, dict) and isinstance(data.get("result"), list):
        data = data["result"]
    if isinstance(data, list):
        data = [{k: (str(r[k])[:240] if k == "description" else r[k])
                 for k in KEEP if isinstance(r, dict) and r.get(k) not in (None, "")}
                if isinstance(r, dict) else r for r in data]
    return json.dumps(data, ensure_ascii=False)[:30000]


def routed(tools: list[str], ok: list[list[str]]) -> bool:
    return any(set(want) <= set(tools) if want else not tools for want in ok)


# ---- judge --------------------------------------------------------------------------
class Grade(BaseModel):
    faithful: int = Field(ge=1, le=5, description="5 = every claim is supported by the tool "
                          "results (or is general knowledge for a web/no-tool answer); 1 = invents "
                          "listings, channels, times or actions")
    helpful: int = Field(ge=1, le=5, description="5 = does what the viewer asked, or says "
                         "clearly and briefly why it can't; 1 = misses the request")
    voice: int = Field(ge=1, le=5, description="5 = short, natural to hear aloud, Pacific times "
                       "in words, no lists/markup/ids; 1 = unusable as speech")
    note: str = Field(description="one short sentence on the main problem, or 'ok'")


JUDGE_PROMPT = """You grade a TV voice assistant's single turn. The viewer is in the San
Francisco Bay Area (Pacific time); the TV is a 53-channel lineup (local stations KTVU=Fox 2,
KPIX=CBS 5, KQED=PBS 9, KNTV=NBC 11 and FAST channels numbered 1001+). TV control commands
ran in DRY RUN and returned a fake success — treat them as having worked. Catch-up and
restart are supported via tv_play_airing. `clock` is the time the question was asked and
`on_screen` is what the assistant was told about the TV (channel, programme, player state)
— both count as evidence; a row marked on_now is on air at that time, so "next" is the
row after it. Grade only what is in the record."""


async def judge(agent: Agent, q: str, r: dict) -> dict | None:
    if r["error"]:
        return None
    rec = {"request": q, "clock": r.get("clock", ""), "on_screen": r.get("on_screen", ""),
           "tool_calls": r["calls"], "spoken_answer": r["answer"]}
    try:
        res = await agent.run(json.dumps(rec, ensure_ascii=False))
        return res.output.model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


# ---- report -------------------------------------------------------------------------
def report(data: dict) -> None:
    rows = []
    for m, turns in data["results"].items():
        ok = [t for t in turns if not t["error"]]
        if not ok:
            rows.append((m, None))
            continue
        ttft = [t["ttft_ms"] for t in ok if t["ttft_ms"]]
        tot = [t["total_ms"] for t in ok]
        g = [t["grade"] for t in ok if t.get("grade") and "faithful" in t["grade"]]
        p90 = lambda xs: sorted(xs)[max(0, int(len(xs) * 0.9) - 1)] if xs else 0  # noqa: E731
        rows.append((m, {
            "n": len(ok), "err": len(turns) - len(ok),
            "ttft": st.median(ttft) if ttft else 0, "ttft90": p90(ttft),
            "total": st.median(tot), "req": st.mean(t["requests"] for t in ok),
            "routed": sum(t["routed"] for t in ok) / len(ok) * 100,
            "faith": st.mean(x["faithful"] for x in g) if g else 0,
            "help": st.mean(x["helpful"] for x in g) if g else 0,
            "voice": st.mean(x["voice"] for x in g) if g else 0,
        }))
    print(f"\n{'model':20} {'ok':>3} {'err':>3} {'ttft p50':>8} {'p90':>6} {'total':>6} "
          f"{'reqs':>5} {'routed':>6} {'faith':>5} {'help':>5} {'voice':>5}")
    for m, s in sorted(rows, key=lambda r: (r[1] is None, r[1] and r[1]["total"])):
        if s is None:
            print(f"{m:20} all turns failed: {data['results'][m][0]['error'][:70]}")
            continue
        print(f"{m:20} {s['n']:3} {s['err']:3} {s['ttft']:8.0f} {s['ttft90']:6.0f} {s['total']:6.0f} "
              f"{s['req']:5.2f} {s['routed']:5.0f}% {s['faith']:5.2f} {s['help']:5.2f} {s['voice']:5.2f}")
    print(f"\njudge: {data['judge']}   questions: {len(data['questions'])}   "
          "times in ms (ttft = first text; total = whole turn)")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--judge", default="claude-opus-5-5")
    ap.add_argument("--report")
    ap.add_argument("--limit", type=int, default=0, help="first N questions only (smoke test)")
    args = ap.parse_args()
    if args.report:
        report(json.loads(Path(args.report).expanduser().read_text()))
        return

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    brain = VoiceBrain(None)
    judge_agent = Agent(make_model(args.judge)[0], output_type=PromptedOutput(Grade),
                        instructions=JUDGE_PROMPT)
    data = {"started": datetime.now().isoformat(timespec="seconds"),
            "judge": args.judge, "questions": [q for q, _ in QUESTIONS],
            "results": {m: [] for m in models}}
    async with brain.agent:
        await brain.prime()
        live = []
        for m in models:                       # drop models this key can't call
            model, settings = make_model(m)
            r = await run_turn(brain, model, settings, "Say OK.")
            if r["error"]:
                print(f"skip {m}: {r['error'][:120]}")
                data["results"][m].append(r)
            else:
                live.append((m, model, settings))
        qs = QUESTIONS[:args.limit] if args.limit else QUESTIONS
        data["questions"] = [q for q, _ in qs]
        for i, (q, ok) in enumerate(qs, 1):
            order = live if i % 2 else live[::-1]
            for m, model, settings in order:
                r = await run_turn(brain, model, settings, q)
                r["question"] = q
                r["routed"] = routed(r["tools"], ok)
                data["results"][m].append(r)
            print(f"[{i}/{len(qs)}] {q}", flush=True)
        print("judging…", flush=True)
        sem = asyncio.Semaphore(6)

        async def grade(r):
            async with sem:
                r["grade"] = await judge(judge_agent, r["question"], r)
        await asyncio.gather(*(grade(r) for m, *_ in live
                               for r in data["results"][m] if "question" in r))
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{datetime.now():%Y%m%d-%H%M}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    report(data)
    print(f"saved {path}")


if __name__ == "__main__":
    asyncio.run(main())
