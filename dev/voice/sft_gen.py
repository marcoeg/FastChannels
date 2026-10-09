#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pydantic-ai-slim[openai,anthropic,mcp]==2.31.1",
#   "aiohttp",
#   "anthropic<1",   # 1.x needs httpx2; pydantic-ai 2.31.1 passes an httpx client
# ]
# ///
"""SFT dataset generation for the local brain: teacher trajectories on
generated traffic, rejection-sampled with the eval suite's checks.

The frontier model (VOICE_MODEL) answers requests through the REAL agent —
prompts, tools, lineup scoping — against the dry-run TV of model_bench.py
(no command reaches the screen; the guide, title and on-demand lookups are
real reads). Each turn is written in the production rollout shape
(`minny-rollout/1`, via rollouts.build_record), so NeMo Gym and
rollouts_export.py read it unchanged, plus a chat-shaped sft_chat.jsonl for
TRL/Unsloth-style trainers. Nothing is written to the agent's own rollouts,
memory or the TV.

Traffic (sft/templates.json, `mix`):
- log: the household's real utterances from ~/news/voice-conversations.jsonl,
  STT artefacts included, deduplicated, each as a single turn;
- single: slot-filled templates whose phrasings come from the log. Channels,
  numbers and network names come from the live lineup, programme titles
  from what the guide has on now and tonight, on-demand titles from the TV,
  so every name in the traffic is real;
- multi: scenarios of two to five turns with shared slots, run with history
  as the worker carries it (VoiceBrain.commit_turn);
- a share of conversations gets memory notes (profile facts) the way the
  worker injects them, and templates that need a fact declare it.

Kept = passes its template's checks (routing, tool arguments, answer
patterns, via eval_suite.check_turn) and the generic filters: no error, a
non-empty answer within the voice length budget, no markdown/ids/UTC, no
"let me check" without a call, no claimed action (tuned, muted, playing…)
without a tv_ call. Rejected turns are written too, with their failures.

Held out: every utterance of eval/suite.json is excluded from the traffic
(exact match after normalisation) and reported.

  ./sft_gen.py --check                      # templates + held-out overlap, offline
  ./sft_gen.py --plan --n 400               # sample the traffic (reads only), no model
  ./sft_gen.py --n 400 --out ~/news/voice-sft/run1          # generate, one process
  ./sft_gen.py --n 2000 --workers 4 --out ~/news/voice-sft/run2   # 4 shard processes
  ./sft_gen.py --n 2000 --out ~/news/voice-sft/run2         # again: resumes, skips done ids
  ./sft_gen.py --recheck ~/news/voice-sft/run2 --retry-errors   # re-filter a run offline; errored turns regenerate on resume

Then: VOICE_ROLLOUT_DIR=~/news/voice-sft/run2/rollouts ./rollouts_export.py --out …
for the NeMo Gym files, or train from sft_chat/*.jsonl directly. See SFT-DATA.md.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import eval_suite as es           # noqa: E402  dry-run TV, fake screen, checks (imports model_bench)
import model_bench as mb          # noqa: E402
import player_resolve as pr       # noqa: E402
from pai_agent import TurnDeps, VoiceBrain  # noqa: E402
from pydantic_ai.messages import (PartDeltaEvent, PartStartEvent,  # noqa: E402
                                  TextPart, TextPartDelta)
from rollouts import TraceCapability, build_record  # noqa: E402

TEMPLATES = HERE / "sft" / "templates.json"
SUITE = HERE / "eval" / "suite.json"
TRANSCRIPT = Path(os.environ.get("VOICE_TRANSCRIPT_LOG",
                                 Path.home() / "news" / "voice-conversations.jsonl"))
OUT_ROOT = Path.home() / "news" / "voice-sft"
TZ = ZoneInfo("America/Los_Angeles")
TURN_TIMEOUT_S = 120
MAX_ANSWER_CHARS = 400
WORDS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80}
NARRATED = re.compile(r"\b(let me (check|look|see)|i'?m (checking|looking)|checking (the|what)|i'?ll (check|look))\b", re.I)
CLAIMED = re.compile(r"\b(tuned|switched (to|over)|turned on|now playing|playing from the beginning|"
                     r"restart(ed|ing)|muted|unmuted|paused|resumed|stopped|volume is (now )?(at )?\d|"
                     r"set (it |the volume )?to \d|turned (it |the volume )?(up|down))\b", re.I)
NEGATED = re.compile(r"\b(isn.t|aren.t|not|no\b|can.t|cannot|couldn.t|won.t|unable)\b", re.I)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# ---- traffic -------------------------------------------------------------------------
def held_out() -> set[str]:
    suite = json.loads(SUITE.read_text())
    return {norm(t["say"]) for sc in suite["scenarios"] for t in sc["turns"]}


def log_requests() -> list[str]:
    """The household's real utterances, deduplicated; STT echoes of the
    recogniser's own prompt and fragments under three characters dropped."""
    seen, out = set(), []
    if not TRANSCRIPT.exists():
        return out
    for line in TRANSCRIPT.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t = (r.get("text") or "").strip()
        if r.get("role") != "user" or len(t) < 3 or t.startswith("English conversation with Minny"):
            continue
        k = norm(t)
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out


class Slots:
    """Slot values from the live lineup, the guide and the TV, read once."""

    def __init__(self, tpl: dict, rng: random.Random) -> None:
        self.tpl, self.rng = tpl, rng
        self.static = tpl["slots"]
        self.channels: list[dict] = []        # {"name","number","spellings":[...]}
        self.prefix_groups: list[tuple[str, list[dict]]] = []
        self.titles_now: list[str] = []
        self.titles_tonight: list[str] = []
        self.vod_titles: list[str] = []

    async def load(self, scope) -> None:
        for ch in await pr.lineup():
            spell = [ch.name] + [a for a in self.tpl["aliases"].get(ch.name, []) if a != ch.name]
            short = re.sub(r"\s*\(.*?\)\s*$", "", ch.name)       # "ACN Country (American Country Network)"
            if short != ch.name and short not in spell:
                spell.insert(1, short)
            if ch.number:
                spell += [str(ch.number), f"channel {ch.number}"]
            self.channels.append({"name": ch.name, "number": ch.number, "spellings": spell})
        groups: dict[str, list[dict]] = defaultdict(list)
        for ch in self.channels:
            groups[ch["name"].split()[0].lower()].append(ch)
        self.prefix_groups = []
        for members in groups.values():
            if len(members) < 2:
                continue
            words = [m["name"].split() for m in members]
            n = 0
            while all(len(w) > n and w[n].lower() == words[0][n].lower() for w in words):
                n += 1
            while n and words[0][n - 1].lower() in ("en", "de", "the", "of", "y", "and"):
                n -= 1                                               # "World Poker Tour", not "… en"
            prefix = " ".join(words[0][:n])
            if len(prefix) > 2 and all(len(w) > n for w in words):   # a real prefix, not a whole name
                self.prefix_groups.append((prefix, members))
        now = await scope.whats_on_now()
        if isinstance(now, list):
            self.titles_now = sorted({r["title"] for r in now if r.get("title") and len(r["title"]) < 50})
        d = datetime.now(TZ).date()
        start = datetime(d.year, d.month, d.day, 18, tzinfo=TZ)
        tonight = await scope.search_lineup({"window": "range", "start": start.isoformat(timespec="seconds"),
                                             "end": (start + timedelta(hours=11)).isoformat(timespec="seconds")})
        rows = tonight["rows"] if isinstance(tonight, dict) else tonight
        if isinstance(rows, list):
            self.titles_tonight = sorted({r["title"] for r in rows if r.get("title") and len(r["title"]) < 50})
        try:
            titles, _ = await pr.browse(None, limit=40)
            self.vod_titles = [t.title for t in (titles or []) if t.title]
        except Exception:  # noqa: BLE001
            self.vod_titles = []

    def fill(self, text: str, bound: dict) -> str:
        """Replace {slot} tokens; a slot already bound in this conversation
        keeps its value, so follow-ups refer to the same thing."""
        def pick(key: str) -> str:
            if key in bound:
                return bound[key]
            val = self._draw(key, bound)
            bound[key] = val
            return val
        return re.sub(r"\{(\w+)\}", lambda m: pick(m.group(1)), text)

    def _draw(self, key: str, bound: dict) -> str:
        r = self.rng
        if key == "channel":
            ch = r.choice(self.channels)
            bound["_channel"] = ch
            return r.choice([sp for sp in ch["spellings"] if not sp.isdigit()][:3])
        if key == "bcast_channel":                # broadcast channels (numbers under 100): the ones with catch-up
            pool = [c for c in self.channels if c["number"] and c["number"] < 100] or self.channels
            ch = r.choice(pool)
            bound["_channel"] = ch
            return r.choice([sp for sp in ch["spellings"] if not sp.isdigit()][:3])
        if key == "channel_name":                 # the lineup's own name, for memory notes
            ch = bound.get("_channel") or r.choice(self.channels)
            bound["_channel"] = ch
            return ch["name"]
        if key == "number":
            ch = bound.get("_channel") or r.choice([c for c in self.channels if c["number"]])
            bound["_channel"] = ch
            return str(ch["number"])
        if key == "channel_prefix":
            k, group = r.choice(self.prefix_groups) if self.prefix_groups else ("Trace", [])
            full = r.choice(group) if group else {"name": "Trace Latina", "spellings": ["Trace Latina"]}
            bound["_channel"] = full
            bound["channel_full"] = full["name"]
            return k.title() if k.isupper() else k
        if key == "channel_full":
            return bound.get("channel_full") or r.choice(self.channels)["name"]
        if key == "misheard_text":
            text, chan = r.choice(self.static["misheard"])
            bound["misheard_channel"] = chan
            bound["_channel"] = next((c for c in self.channels if c["name"] == chan),
                                     {"name": chan, "spellings": [chan]})
            return text
        if key == "misheard_channel":
            return bound.get("misheard_channel") or "Trace"
        if key == "title_now":
            return r.choice(self.titles_now or self.titles_tonight or ["the news"])
        if key == "title_tonight":
            return r.choice(self.titles_tonight or self.titles_now or ["the news"])
        if key == "vod_title":
            return r.choice(self.vod_titles or ["Inside Out 2"])
        if key == "level":
            lv = r.choice(self.static["level"])
            pct = WORDS.get(lv, int(lv) if lv.isdigit() else 50)
            bound["level_pct"] = str(pct)
            bound["level_frac"] = f"{pct / 100:.2f}".rstrip("0").rstrip(".")
            return lv
        if key == "level_frac":
            if "level" not in bound:
                self._draw("level", bound)
            return bound["level_frac"]
        if key == "level_pct":
            if "level" not in bound:
                self._draw("level", bound)
            return bound["level_pct"]
        if key in self.static:
            v = r.choice(self.static[key])
            return v if isinstance(v, str) else v[0]
        raise KeyError(f"unknown slot {{{key}}}")

    def regex_for(self, key: str, bound: dict) -> str:
        """A regex accepting every spelling of the bound value (tool_args)."""
        if key in ("channel", "bcast_channel", "channel_full", "misheard_channel"):
            ch = bound.get("_channel")
            spell = (ch or {}).get("spellings") or [bound.get(key, "")]
            return "(?i)" + "|".join(re.escape(s) for s in spell if s)
        if key == "number":
            return re.escape(bound.get("number", ""))
        return "(?i)" + re.escape(bound.get(key, ""))


def _weighted(items: list[dict], rng: random.Random) -> dict:
    return rng.choices(items, weights=[i.get("weight", 1) for i in items], k=1)[0]


def plan_traffic(tpl: dict, slots: Slots, n: int, rng: random.Random, exclude: set[str]) -> tuple[list[dict], Counter]:
    """n conversations: id, source, template, memory notes, turns with their
    checks (slot regexes already resolved). Utterances in `exclude` (the eval
    suite) are dropped and counted."""
    mix, stats = tpl["mix"], Counter()
    logs = log_requests()
    rng.shuffle(logs)
    convs: list[dict] = []
    li = 0
    while len(convs) < n:
        kind = rng.choices(["log", "single", "multi"], weights=[mix["log"], mix["single"], mix["multi"]])[0]
        bound: dict = {}
        if kind == "log":
            if li >= len(logs):
                kind = "single"
            else:
                say, li = logs[li], li + 1
                if norm(say) in exclude:
                    stats["held_out"] += 1
                    continue
                turns = [{"say": say, "checks": {}}]
                t_id, memory = "log", []
        if kind == "single":
            t = _weighted(tpl["single"], rng)
            memory = _memory_for(t, tpl, slots, bound, rng, mix)
            say = slots.fill(rng.choice(t["say"]), bound)
            turns = [{"say": say, "checks": _resolve(t.get("checks", {}), slots, bound), "screen": t.get("screen")}]
            t_id = t["id"]
        elif kind == "multi":
            t = _weighted(tpl["multi"], rng)
            memory = _memory_for(t, tpl, slots, bound, rng, mix)
            turns = [{"say": slots.fill(rng.choice(tt["say"]), bound),
                      "checks": _resolve(tt.get("checks", {}), slots, bound)} for tt in t["turns"]]
            t_id = t["id"]
        if any(norm(tt["say"]) in exclude for tt in turns):
            stats["held_out"] += 1
            continue
        cid = hashlib.sha1("|".join(tt["say"] for tt in turns).encode()).hexdigest()[:12]
        if any(c["id"] == cid for c in convs):
            stats["duplicate"] += 1
            continue
        convs.append({"id": cid, "source": kind, "template": t_id, "memory": memory,
                      "slots": {k: v for k, v in bound.items() if not k.startswith("_")}, "turns": turns})
        stats[f"{kind}:{t_id}" if kind != "log" else "log"] += 1
    return convs, stats


def _memory_for(t: dict, tpl: dict, slots: Slots, bound: dict, rng: random.Random, mix: dict) -> list[str]:
    kinds = list(t.get("memory") or [])
    if not kinds and rng.random() < mix.get("memory_share", 0.3):
        kinds = rng.sample(list(tpl["memory"]), k=rng.choice([1, 1, 2]))
    return [slots.fill(tpl["memory"][k], bound) for k in kinds]


def _resolve(checks: dict, slots: Slots, bound: dict) -> dict:
    """Template checks → a spec eval_suite.check_turn understands, with slot
    regexes and numeric levels filled from the bound slots."""
    out = {k: v for k, v in checks.items() if k not in ("tool_args", "answer_match_slot")}
    tas = []
    for ta in checks.get("tool_args", []):
        ta = dict(ta)
        if "slot_regex" in ta:
            ta["regex"] = slots.regex_for(ta.pop("slot_regex"), bound)
        if "args" in ta:
            args = {}
            for k, v in ta["args"].items():
                if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
                    key = v[1:-1]
                    if key not in bound:
                        slots.fill(v, bound)
                    val = bound[key]
                    args[k] = float(val) if key.startswith("level") else val
                else:
                    args[k] = v
            ta["args"] = args
        tas.append(ta)
    if tas:
        out["tool_args"] = tas
    if "answer_match_slot" in checks:
        out["answer_match"] = slots.regex_for(checks["answer_match_slot"], bound)
    return out


# ---- the teacher turn ------------------------------------------------------------------
async def teacher_turn(brain: VoiceBrain, model, settings, say: str, deps: TurnDeps) -> tuple[dict, TraceCapability, list, float]:
    trace = TraceCapability()
    t0 = time.monotonic()
    t0_epoch = time.time()
    first, text = None, []
    try:
        async with brain.agent.run_stream_events(
                say, model=model, model_settings=settings, deps=deps,
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
        brain.commit_turn(msgs)
    except Exception as e:  # noqa: BLE001
        msgs, err = [], f"{type(e).__name__}: {e}"[:300]
    from rollouts import to_items
    items = to_items(msgs)
    calls = [i for i in items if i.get("type") == "function_call"]
    outs = {i.get("call_id"): i.get("output") for i in items if i.get("type") == "function_call_output"}
    rec = {"say": say, "ttft_ms": round(first * 1000) if first else None,
           "total_ms": round((time.monotonic() - t0) * 1000), "requests": len(trace.requests),
           "tools": [c["name"] for c in calls],
           "calls": [{"name": c["name"], "args": c.get("arguments"), "output": mb._compact(outs.get(c.get("call_id")))}
                     for c in calls],
           "answer": "".join(text).strip(), "error": err, "on_screen": "", "clock": ""}
    return rec, trace, msgs, t0_epoch


ALREADY = re.compile(r"\balready\b", re.I)                 # "already tuned to KQED": state, not an action


def generic_fails(rec: dict) -> list[str]:
    fails = []
    if rec["error"]:
        return ["error"]
    ans = rec["answer"]
    if not ans:
        fails.append("empty_answer")
    if len(ans) > MAX_ANSWER_CHARS:
        fails.append("too_long")
    if not rec["tools"] and NARRATED.search(ans):
        fails.append("narrated_without_call")
    if CLAIMED.search(ans) and not NEGATED.search(ans) and not ALREADY.search(ans) \
            and not any(t.startswith("tv_") for t in rec["tools"]):
        fails.append("action_without_call")
    return fails


def to_chat(items: list[dict], tools: list[dict]) -> dict:
    """Responses-API items → chat messages (system/user/assistant with
    tool_calls/tool), the shape chat-template trainers consume."""
    msgs: list[dict] = []
    for it in items:
        role, typ = it.get("role"), it.get("type")
        if role == "developer":
            msgs.append({"role": "system", "content": it["content"]})
        elif role == "user":
            msgs.append({"role": "user", "content": it["content"]})
        elif role == "assistant":
            msgs.append({"role": "assistant", "content": it["content"]})
        elif typ == "function_call":
            call = {"id": it["call_id"], "type": "function",
                    "function": {"name": it["name"], "arguments": it.get("arguments") or "{}"}}
            if msgs and msgs[-1]["role"] == "assistant" and msgs[-1].get("tool_calls") and not msgs[-1].get("content"):
                msgs[-1]["tool_calls"].append(call)
            else:
                msgs.append({"role": "assistant", "content": "", "tool_calls": [call]})
        elif typ == "function_call_output":
            out = it.get("output")
            msgs.append({"role": "tool", "tool_call_id": it["call_id"],
                         "content": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)})
    chat_tools = [{"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters") if k in t}}
                  if t.get("type") == "function" and "function" not in t else t for t in tools]
    return {"messages": msgs, "tools": chat_tools}


# ---- generation -------------------------------------------------------------------------
NO_SCREEN = {"tuned": None, "playing": None, "player": {"state": "idle"}}   # the grid, nothing playing
UNTUNED_SHARE = 0.3


def screen_for(conv: dict, lineup: list) -> dict:
    """The TV at the start of a conversation: tuned to a lineup channel, or
    the programme grid with nothing playing (30 %). Drawn from the
    conversation id, so a resume gives a conversation the same screen and
    the traffic sampling is untouched. A template that starts tuned
    ("screen": "tuned") always is; the channel is never the one the
    conversation names, so "put on X" is not a no-op. (The eval suite's
    default screen — always KTVU — is what every conversation of run1 saw.)"""
    r = random.Random(conv["id"])
    starts_tuned = any(t.get("screen") == "tuned" for t in conv["turns"])
    if not lineup or (not starts_tuned and r.random() < UNTUNED_SHARE):
        return dict(NO_SCREEN)
    said = [str(v).lower() for k, v in conv["slots"].items() if k.startswith("channel") or k == "number"]
    pool = [ch for ch in lineup
            if not any(x in ch.name.lower() or ch.name.lower() in x or x == str(ch.number) for x in said)] or lineup
    ch = r.choice(pool)
    return {"tuned": {"channel_id": ch.channel_id, "number": ch.number, "name": ch.name, "airing": None},
            "playing": {"kind": "live", "title": f"{ch.number} {ch.name} · live"}}


async def generate(convs: list[dict], out: Path, model_name: str, defaults: dict, shard: str) -> Counter:
    stats = Counter()
    done = set()
    for sub in ("rollouts", "rejected", "sft_chat"):
        (out / sub).mkdir(exist_ok=True)
    for p in list((out / "rollouts").glob("*.jsonl")) + list((out / "rejected").glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["gen"]["conversation"])
            except (ValueError, KeyError):
                pass
    todo = [c for c in convs if c["id"] not in done]
    stats["skipped_done"] = len(convs) - len(todo)
    model, settings = mb.make_model(model_name)
    brain = VoiceBrain(None)
    async with brain.agent:
        await brain.prime()
        lineup = [ch for ch in await pr.lineup() if ch.number]
        for ch in lineup:
            es.LINEUP[ch.channel_id] = (ch.name, ch.number)
            es.LINEUP[str(ch.number)] = (ch.name, ch.number)
        tag = f"shard-{shard.replace('/', '-')}"       # one file per shard: no interleaved lines
        with open(out / "rollouts" / f"{tag}.jsonl", "a", encoding="utf-8") as kept, \
             open(out / "rejected" / f"{tag}.jsonl", "a", encoding="utf-8") as rejected, \
             open(out / "sft_chat" / f"{tag}.jsonl", "a", encoding="utf-8") as chat:
            for n, conv in enumerate(todo, 1):
                screen = screen_for(conv, lineup)
                es.reset_tv(defaults, {"screen": screen, "player": screen.get("player")})
                brain.history = []
                deps = TurnDeps(facts_profile=list(conv["memory"]), facts_recent=[])
                session = f"sft-gen:{conv['id']}"
                for i, turn in enumerate(conv["turns"]):
                    try:
                        rec, trace, msgs, t0 = await asyncio.wait_for(
                            teacher_turn(brain, model, settings, turn["say"], deps), TURN_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        rec, trace, msgs, t0 = ({"say": turn["say"], "ttft_ms": None, "total_ms": TURN_TIMEOUT_S * 1000,
                                                 "requests": 0, "tools": [], "calls": [], "answer": "",
                                                 "error": "timeout", "on_screen": "", "clock": ""},
                                                TraceCapability(), [], time.time())
                    fails = generic_fails(rec) + [f for f in es.check_turn(turn["checks"], rec, defaults)
                                                  if f not in ("error",)]
                    if turn["checks"].get("max_answer_chars") and len(rec["answer"]) > turn["checks"]["max_answer_chars"]:
                        fails.append("max_answer_chars")
                    record = build_record(
                        session_id=session, turn_index=i, turn_id=uuid.uuid4().hex, room="sft-gen",
                        is_satellite=False, user_text=turn["say"], trace=trace, new_messages=msgs,
                        spoken=rec["answer"], interrupted=False, error=rec["error"], ts_start=t0,
                        ttfd_ms=rec["ttft_ms"], total_ms=rec["total_ms"], recall_used=bool(conv["memory"]))
                    if record is None:
                        record = {"schema": "minny-rollout/1", "session_id": session, "turn_index": i,
                                  "room": "sft-gen", "user_text": turn["say"], "error": rec["error"] or "no model call",
                                  "responses_create_params": {}, "response": {"output": []}, "spoken_text": ""}
                    record["synthetic"] = True
                    record["gen"] = {"conversation": conv["id"], "source": conv["source"], "template": conv["template"],
                                     "slots": conv["slots"], "memory": conv["memory"], "checks": turn["checks"],
                                     "screen": (screen.get("tuned") or {}).get("name"),
                                     "fails": fails, "shard": shard, "teacher": model_name}
                    line = json.dumps(record, ensure_ascii=False) + "\n"
                    if fails:
                        rejected.write(line)
                        stats["rejected"] += 1
                        for f in fails:
                            stats[f"fail:{f.split(':')[0]}"] += 1
                    else:
                        kept.write(line)
                        rcp = record["responses_create_params"]
                        chat.write(json.dumps({**to_chat(rcp["input"] + record["response"]["output"], rcp["tools"]),
                                               "metadata": {"session_id": session, "turn_index": i,
                                                            "template": conv["template"], "source": conv["source"]}},
                                              ensure_ascii=False) + "\n")
                        stats["kept"] += 1
                    if rec["error"]:
                        break                     # the rest of the conversation has no history
                if n % 10 == 0:
                    print(f"[{shard}] {n}/{len(todo)} conversations, kept {stats['kept']}, rejected {stats['rejected']}",
                          flush=True)
    return stats


def recheck(out: Path, defaults: dict, retry_errors: bool) -> Counter:
    """Re-apply the current checks to every record of a run and rewrite
    rollouts/, rejected/ and sft_chat/. With retry_errors, conversations
    whose turns failed with an error (an API outage, a timeout) are dropped
    altogether, so the next resume regenerates them. No model calls."""
    stats = Counter()
    recs: dict[str, list[dict]] = defaultdict(list)       # shard tag -> records
    for sub in ("rollouts", "rejected"):
        for p in sorted((out / sub).glob("*.jsonl")):
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    recs[p.stem].append(json.loads(line))
                except ValueError:
                    stats["unreadable"] += 1
    errored = {r["gen"]["conversation"] for rs in recs.values() for r in rs if r.get("error")} if retry_errors else set()
    for tag, rs in recs.items():
        kept, rejected, chat = [], [], []
        for r in rs:
            if r["gen"]["conversation"] in errored:
                stats["dropped_for_retry"] += 1
                continue
            output = r["response"]["output"]
            calls = [o for o in output if o.get("type") == "function_call"]
            outs = {o.get("call_id"): o.get("output") for o in output if o.get("type") == "function_call_output"}
            rec = {"say": r["user_text"], "tools": [c["name"] for c in calls],
                   "calls": [{"name": c["name"], "args": c.get("arguments"), "output": mb._compact(outs.get(c.get("call_id")))}
                             for c in calls],
                   "answer": r.get("spoken_text") or "", "error": r.get("error"), "total_ms": r.get("latency", {}).get("total_ms", 0)}
            checks = r["gen"].get("checks") or {}
            fails = generic_fails(rec) + [f for f in es.check_turn(checks, rec, defaults) if f != "error"]
            if checks.get("max_answer_chars") and len(rec["answer"]) > checks["max_answer_chars"]:
                fails.append("max_answer_chars")
            r["gen"]["fails"] = fails
            if fails:
                rejected.append(r)
                for f in fails:
                    stats[f"fail:{f.split(':')[0]}"] += 1
            else:
                kept.append(r)
                rcp = r["responses_create_params"]
                chat.append({**to_chat(rcp["input"] + r["response"]["output"], rcp["tools"]),
                             "metadata": {"session_id": r["session_id"], "turn_index": r["turn_index"],
                                          "template": r["gen"]["template"], "source": r["gen"]["source"]}})
        stats["kept"] += len(kept)
        stats["rejected"] += len(rejected)
        for sub, rows in (("rollouts", kept), ("rejected", rejected), ("sft_chat", chat)):
            (out / sub).mkdir(exist_ok=True)
            (out / sub / f"{tag}.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")
    (out / "summary-recheck.json").write_text(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **stats}, indent=1))
    return stats


def validate(tpl: dict) -> list[str]:
    problems = []
    known = set(tpl["slots"]) | {"channel", "channel_name", "number", "channel_prefix", "channel_full", "misheard_text", "misheard_channel",
                                 "title_now", "title_tonight", "vod_title", "level_frac", "level_pct", "bcast_channel"}
    for kind in ("single", "multi"):
        for t in tpl[kind]:
            turns = t["turns"] if kind == "multi" else [t]
            for tt in turns:
                for s in tt.get("say", []):
                    for key in re.findall(r"\{(\w+)\}", s):
                        if key not in known:
                            problems.append(f"{t['id']}: unknown slot {{{key}}} in {s!r}")
                for ta in tt.get("checks", {}).get("tool_args", []):
                    if "tool" not in ta or not (("arg" in ta and ("regex" in ta or "slot_regex" in ta)) or "args" in ta):
                        problems.append(f"{t['id']}: tool_args needs tool + (arg, regex|slot_regex) or args")
            for key in t.get("memory", []):
                if key not in tpl["memory"]:
                    problems.append(f"{t['id']}: unknown memory kind {key}")
    for k, s in tpl["memory"].items():
        for key in re.findall(r"\{(\w+)\}", s):
            if key not in known:
                problems.append(f"memory {k}: unknown slot {{{key}}}")
    return problems


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=400, help="conversations to sample")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--model", default=os.environ.get("VOICE_MODEL", "gpt-5.6-terra"), help="the teacher")
    ap.add_argument("--out", type=Path, help=f"output dir (default {OUT_ROOT}/<stamp>)")
    ap.add_argument("--workers", type=int, default=1, help="shard processes (the dry-run TV is per process)")
    ap.add_argument("--shard", help="internal: i/k")
    ap.add_argument("--check", action="store_true", help="validate templates and the held-out overlap, offline")
    ap.add_argument("--plan", action="store_true", help="sample and print the traffic; reads only, no model")
    ap.add_argument("--focus", help="comma-separated template ids: sample only these, no log utterances "
                                    "(a targeted run; the single/multi shares follow the ids' weights)")
    ap.add_argument("--recheck", type=Path, metavar="DIR", help="re-apply the current checks to a run; no model calls")
    ap.add_argument("--retry-errors", action="store_true", help="with --recheck: drop errored conversations so a resume regenerates them")
    args = ap.parse_args()

    tpl = json.loads(TEMPLATES.read_text())
    problems = validate(tpl)
    exclude = held_out()
    if args.check:
        static = [s for t in tpl["single"] for s in t["say"]] + [s for t in tpl["multi"] for tt in t["turns"] for s in tt["say"]]
        overlap = sorted({s for s in static if norm(s) in exclude})
        logs = log_requests()
        log_overlap = sum(1 for s in logs if norm(s) in exclude)
        print(f"templates {tpl['version']}: {len(tpl['single'])} single, {len(tpl['multi'])} multi "
              f"({sum(len(t['turns']) for t in tpl['multi'])} turns), {len(tpl['memory'])} memory kinds; "
              f"log requests {len(logs)} ({log_overlap} overlap the suite, excluded); "
              f"suite utterances held out {len(exclude)}")
        print(f"static phrasings that equal a suite utterance (excluded at sampling): {len(overlap)}")
        for s in overlap:
            print("   ", repr(s))
        print("problems:", problems or "none")
        return
    if problems:
        raise SystemExit("templates invalid:\n  " + "\n  ".join(problems))
    if args.focus:
        ids = {x.strip() for x in args.focus.split(",") if x.strip()}
        unknown = ids - {t["id"] for t in tpl["single"] + tpl["multi"]}
        if unknown:
            raise SystemExit(f"--focus: unknown template ids {sorted(unknown)}")
        tpl["single"] = [t for t in tpl["single"] if t["id"] in ids]
        tpl["multi"] = [t for t in tpl["multi"] if t["id"] in ids]
        w_single = sum(t.get("weight", 1) for t in tpl["single"])
        w_multi = sum(t.get("weight", 1) for t in tpl["multi"])
        tpl["mix"] = {**tpl["mix"], "log": 0.0,
                      "single": w_single / (w_single + w_multi), "multi": w_multi / (w_single + w_multi)}
    if args.recheck:
        defaults = json.loads(SUITE.read_text()).get("defaults") or {}
        stats = recheck(args.recheck, defaults, args.retry_errors)
        print(f"recheck {args.recheck}: kept {stats['kept']}, rejected {stats['rejected']}, "
              f"dropped for retry {stats['dropped_for_retry']}")
        for k, v in sorted(stats.items()):
            if k.startswith("fail:"):
                print(f"  {k}: {v}")
        return

    out = args.out or OUT_ROOT / f"{datetime.now():%Y%m%d-%H%M}"
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    brain = VoiceBrain(None)
    async with brain.agent:
        await brain.prime()
        import gc
        import lineup_scope
        scope = next(o for o in gc.get_objects() if isinstance(o, lineup_scope.LineupScope))
        slots = Slots(tpl, rng)
        await slots.load(scope)
    convs, stats = plan_traffic(tpl, slots, args.n, rng, exclude)
    (out / "traffic.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in convs), encoding="utf-8")
    print(f"traffic: {len(convs)} conversations, {sum(len(c['turns']) for c in convs)} turns → {out}/traffic.jsonl")
    print(f"  lineup channels {len(slots.channels)}, ambiguous prefixes {[k for k, _ in slots.prefix_groups]}, "
          f"titles on now {len(slots.titles_now)}, tonight {len(slots.titles_tonight)}, on demand {len(slots.vod_titles)}")
    print(f"  held out (suite utterances dropped): {stats['held_out']}, duplicates: {stats['duplicate']}")
    by_source = Counter(c["source"] for c in convs)
    print(f"  by source: {dict(by_source)}; with memory notes: {sum(1 for c in convs if c['memory'])}")
    if args.plan:
        for c in rng.sample(convs, k=min(20, len(convs))):
            print(f"   {c['source']:6} {c['template']:22} {' → '.join(t['say'] for t in c['turns'])[:110]!r}"
                  + (f"  [memory: {'; '.join(c['memory'])[:60]}]" if c["memory"] else ""))
        return

    defaults = json.loads(SUITE.read_text()).get("defaults") or {}
    if args.shard:
        i, k = (int(x) for x in args.shard.split("/"))
        convs = [c for j, c in enumerate(convs) if j % k == i]
    elif args.workers > 1:
        procs = [await asyncio.create_subprocess_exec(
            sys.executable, __file__, "--n", str(args.n), "--seed", str(args.seed), "--model", args.model,
            "--out", str(out), "--shard", f"{i}/{args.workers}",
            *(["--focus", args.focus] if args.focus else [])) for i in range(args.workers)]   # the shards plan for themselves
        codes = await asyncio.gather(*(p.wait() for p in procs))
        print("shards exited:", codes)
        return
    stats.update(await generate(convs, out, args.model, defaults, args.shard or "0/1"))
    (out / f"summary-{(args.shard or '0-1').replace('/', '-')}.json").write_text(
        json.dumps({"started": datetime.now().isoformat(timespec="seconds"), "model": args.model,
                    "templates": tpl["version"], "stats": dict(stats)}, indent=1))
    print(f"done: kept {stats['kept']}, rejected {stats['rejected']} → {out}/ "
          f"(rollouts/, rejected/, sft_chat/, traffic.jsonl)")
    for k, v in sorted(stats.items()):
        if k.startswith("fail:"):
            print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
