#!/usr/bin/env python3
"""Held-out evaluation of a served brain model on the SFT dev examples, the
teacher's step as the reference. Cheap and deterministic: no guide, no TV,
no judge needed — every tool result is already in the prompt.

For each example of <run>/train/dev.jsonl (prompt = the conversation
exactly as llama.cpp feeds it, completion = what the teacher produced at
that step), the served model gets the prompt as raw text through
llama-server's /completion at temperature 0, and its continuation is
compared with the teacher's:

  kind        a tool call or spoken text — the first decision of every step
  tools       the function names, in order
  args        parameter keys and values, literals normalised (True/true,
              0.5/0.50, case and spaces in strings)
  text steps  the suite's forbidden patterns (markdown, UTC, URLs, ids),
              length against the teacher's, truncation at the token cap,
              a leaked <think> block; --judge asks claude whether the
              answer conveys the same facts as the teacher's, no inventions
  hygiene     Python-style literals in parameters (BUGLIST T8), malformed
              tool-call XML
  speed       prompt and generation tokens/s from the server's timings

Models are `alias[@url]`, several comma-separated; the default url is
--server, so the base on :8001 and the fine-tune on :8002 compare side by
side in one run. Results go to ~/news/model-bench/sft-eval-<stamp>.json;
--report FILE reprints one.

The end-to-end gates stay model_bench.py and eval_suite.py (the agent,
the live guide, the judge); this one says whether training did what the
data asked, example by example, and what it broke.

Concurrency: llama-server's context (32k on the laptop) is shared by its
slots, so --workers is 2 — four of these ~8k prompts at once overflow it
and every request in flight fails with "Context size has been exceeded".
Decoding also slows while another slot processes a prompt, so the
latencies here are "under two concurrent requests", not the agent's.

  ./sft_eval.py --models qwen3.5-9b                                   # the base, as a baseline
  ./sft_eval.py --models qwen3.5-9b@http://192.168.50.71:8001,qwen3.5-9b-sft1@http://192.168.50.71:8002
  ./sft_eval.py --models qwen3.5-9b-sft1 --judge --show 20
  ./sft_eval.py --report ~/news/model-bench/sft-eval-20261008-1200.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_RUN = Path.home() / "news" / "voice-sft" / "run1"
DEFAULT_OUT = Path.home() / "news" / "model-bench"
DEFAULT_SERVER = "http://192.168.50.71:8001"
SUITE = HERE / "eval" / "suite.json"
END = "<|im_end|>"
JUDGE_MODEL = "claude-opus-5-5"

XML_CALL = re.compile(r"<tool_call>\n<function=(?P<name>[^>\n]+)>\n(?P<params>.*?)</function>\n</tool_call>", re.S)
XML_PARAM = re.compile(r"<parameter=(?P<key>[^>\n]+)>\n(?P<value>.*?)\n</parameter>\n", re.S)
PY_LITERAL = re.compile(r"<parameter=[^>\n]+>\n(True|False|None)\n</parameter>")
LAST_USER = re.compile(r"<\|im_start\|>user\n(?!<tool_response>)(.*?)<\|im_end\|>", re.S)


# ---- setup -----------------------------------------------------------------------------------
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().removeprefix("export ")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def parse_models(spec: str, default_url: str) -> list[dict]:
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        alias, _, url = item.partition("@")
        out.append({"alias": alias, "url": (url or default_url).rstrip("/")})
    return out


def load_examples(path: Path, limit: int | None, seed: int) -> list[dict]:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    if limit and limit < len(rows):
        rows = random.Random(seed).sample(rows, k=limit)
    return rows


def forbidden_patterns() -> list[str]:
    try:
        return list(json.loads(SUITE.read_text(encoding="utf-8")).get("defaults", {}).get("answer_no_match") or [])
    except (OSError, ValueError):
        return []


# ---- the server ------------------------------------------------------------------------------
def _post(url: str, key: str | None, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:            # keep the server's own words, not just the code
        detail = e.read().decode("utf-8", "replace")[:300]
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except (ValueError, AttributeError):
            pass
        raise urllib.error.HTTPError(e.url, e.code, f"{e.code}: {detail}", e.headers, None) from None


def probe(model: dict, key: str | None, timeout: float) -> str | None:
    """The server must turn "<|im_start|>" into one token, or the prompts
    would be fed as text; returns a problem or None."""
    try:
        t = _post(model["url"] + "/tokenize", key, {"content": "<|im_start|>user", "add_special": False}, timeout)
        toks = t.get("tokens") or []
        if len(toks) != 2:
            return f"/tokenize split '<|im_start|>user' into {len(toks)} tokens; special tokens are not parsed"
        r = complete(model, key, "<|im_start|>user\nSay OK.<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n", 8, timeout)
        if r.get("error"):
            return r["error"]
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
        return f"{type(e).__name__}: {e}"
    return None


def complete(model: dict, key: str | None, prompt: str, n_predict: int, timeout: float) -> dict:
    """One greedy continuation of the raw prompt; the text, how it stopped
    and the server's timings."""
    t0 = time.monotonic()
    try:
        r = _post(model["url"] + "/completion", key, {
            "model": model["alias"], "prompt": prompt, "n_predict": n_predict, "temperature": 0,
            "stop": [END], "cache_prompt": True}, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}"[:200], "total_ms": round((time.monotonic() - t0) * 1000)}
    stop = r.get("stop_type") or ("eos" if r.get("stopped_eos") else "word" if r.get("stopped_word")
                                  else "limit" if r.get("stopped_limit") else "?")
    tm = r.get("timings") or {}
    return {"text": r.get("content", ""), "stop": stop, "total_ms": round((time.monotonic() - t0) * 1000),
            "prompt_n": tm.get("prompt_n"), "predicted_n": tm.get("predicted_n"),
            "prompt_tps": round(tm["prompt_per_second"], 1) if tm.get("prompt_per_second") else None,
            "gen_tps": round(tm["predicted_per_second"], 1) if tm.get("predicted_per_second") else None}


# ---- comparing a step --------------------------------------------------------------------------
def parse_calls(text: str) -> list[tuple[str, dict]]:
    return [(m.group("name"), dict(XML_PARAM.findall(m.group("params")))) for m in XML_CALL.finditer(text)]


def norm_value(v: str) -> str:
    s = v.strip()
    low = {"true": "true", "false": "false", "none": "null", "null": "null"}
    if s.lower() in low:
        return low[s.lower()]
    try:
        f = float(s)
        return repr(int(f)) if f.is_integer() else repr(round(f, 6))
    except ValueError:
        return re.sub(r"\s+", " ", s).casefold()


def spoken(text: str) -> str:
    """The step's spoken part: tool-call blocks, the end token and a think
    block stripped."""
    t = XML_CALL.sub("", text)
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S)
    return t.replace(END, "").strip()


def score(expected: str, got: str, stop: str, patterns: list[str]) -> dict:
    e_calls, g_calls = parse_calls(expected), parse_calls(got)
    e_text, g_text = spoken(expected), spoken(got)
    e_kind = "tool_call" if e_calls else "text"
    g_kind = "tool_call" if g_calls else "text"
    s = {"kind_expected": e_kind, "kind_got": g_kind, "kind_match": e_kind == g_kind,
         "tools_expected": [n for n, _ in e_calls], "tools_got": [n for n, _ in g_calls],
         "tool_match": [n for n, _ in e_calls] == [n for n, _ in g_calls],
         "malformed": "<tool_call>" in got and not g_calls,
         "py_literals": len(PY_LITERAL.findall(got)),
         "think_leak": "<think>" in got,
         "truncated": stop == "limit",
         "exact": re.sub(r"\s+", " ", expected.replace(END, "")).strip() == re.sub(r"\s+", " ", got.replace(END, "")).strip()}
    if e_calls and s["tool_match"]:
        s["keys_match"] = all(set(ea) == set(ga) for (_, ea), (_, ga) in zip(e_calls, g_calls))
        s["args_match"] = s["keys_match"] and all(
            all(norm_value(ea[k]) == norm_value(ga[k]) for k in ea) for (_, ea), (_, ga) in zip(e_calls, g_calls))
        s["args_diff"] = [{"call": name, "key": k, "expected": ea.get(k), "got": ga.get(k)}
                          for (name, ea), (_, ga) in zip(e_calls, g_calls) for k in set(ea) | set(ga)
                          if k not in ga or k not in ea or norm_value(ea[k]) != norm_value(ga[k])]
    if e_kind == "text":
        s["forbidden"] = [p for p in patterns if re.search(p, g_text)]
        s["len_ratio"] = round(len(g_text) / max(len(e_text), 1), 2)
        s["empty"] = not g_text
    return s


def last_user(prompt: str) -> str:
    m = LAST_USER.findall(prompt)
    return m[-1].strip() if m else ""


# ---- the judge, for text steps -------------------------------------------------------------------
JUDGE_PROMPT = """You compare two answers a TV voice assistant could speak in reply to the viewer's
request. The REFERENCE was produced by the frontier model with the same tool results;
the CANDIDATE by a smaller model. Say whether the candidate conveys the same facts
(programmes, channels, times, what was done or could not be done) with nothing invented
and nothing important missing. Wording may differ; a shorter answer with the same
substance is fine; a different recommendation drawn from the same tool results is fine
when it is grounded. Reply with JSON only: {"same": true|false, "note": "<one line>"}.

REQUEST: %s

REFERENCE: %s

CANDIDATE: %s"""


def judge_text(client, request: str, reference: str, candidate: str) -> dict:
    try:
        msg = client.messages.create(model=JUDGE_MODEL, max_tokens=600,
                                     messages=[{"role": "user", "content": JUDGE_PROMPT % (request, reference, candidate)}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
        verdict = re.search(r'"same"\s*:\s*(true|false)', raw)        # a truncated reply still carries the verdict
        return {"same": verdict.group(1) == "true" if verdict else None,
                "note": raw[:300] or f"empty reply (stop_reason {getattr(msg, 'stop_reason', '?')})"}
    except Exception as e:  # noqa: BLE001  the judge is optional
        return {"same": None, "note": f"{type(e).__name__}: {e}"[:200]}


# ---- running -------------------------------------------------------------------------------------
def run_model(model: dict, key: str | None, examples: list[dict], patterns: list[str], args, judge_client) -> list[dict]:
    def one(ex: dict) -> dict:
        r = complete(model, key, ex["prompt"], args.n_predict, args.timeout)
        if r.get("error"):
            return {"error": r["error"], "total_ms": r["total_ms"]}
        try:
            s = score(ex["completion"], r["text"], r["stop"], patterns)
        except Exception as e:  # noqa: BLE001  keep the text, report the fault
            return {**r, "error": f"scoring: {type(e).__name__}: {e}"[:200]}
        out = {**r, "score": s}
        if judge_client and s["kind_expected"] == "text" and s["kind_got"] == "text":
            out["judge"] = judge_text(judge_client, last_user(ex["prompt"]), spoken(ex["completion"]), spoken(r["text"]))
        return out
    results = [None] * len(examples)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(one, ex): i for i, ex in enumerate(examples)}
        done = 0
        for fut in futures:
            results[futures[fut]] = fut.result()
            done += 1
            if done % 25 == 0:
                print(f"  {model['alias']}: {done}/{len(examples)}", file=sys.stderr)
    # A server error is a hole, not a miss: "Context size has been exceeded"
    # comes from the slots' shared KV being full of earlier prompts, and
    # passes once they are evicted. One more try each, one at a time.
    failed = [i for i, r in enumerate(results) if r.get("error") and not r["error"].startswith("scoring")]
    if failed:
        print(f"  {model['alias']}: retrying {len(failed)} errored steps one at a time", file=sys.stderr)
        for i in failed:
            results[i] = one(examples[i])
        still = sum(1 for i in failed if results[i].get("error"))
        if still:
            print(f"  {model['alias']}: {still} still failing", file=sys.stderr)
    return results


def judge_file(path: Path, client, show: int) -> None:
    """Add the judge's verdicts to a results file's text steps (those not
    judged yet), recompute the summaries, save and reprint."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for m in data["models"]:
        res = data["results"][m["alias"]]
        todo = [(ex, r) for ex, r in zip(data["examples"], res)
                if not r.get("error") and (not r.get("judge") or r["judge"].get("same") is None)
                and r["score"]["kind_expected"] == "text" and r["score"]["kind_got"] == "text"]
        print(f"{m['alias']}: judging {len(todo)} text steps", file=sys.stderr)
        for n, (ex, r) in enumerate(todo, 1):
            r["judge"] = judge_text(client, last_user(ex["prompt"]), spoken(ex["completion"]), spoken(r["text"]))
            if n % 25 == 0:
                print(f"  {n}/{len(todo)}", file=sys.stderr)
        data["summary"][m["alias"]] = summarise(data["examples"], res)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    report(data, show)


def _p(xs, q):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else None


def summarise(examples: list[dict], results: list[dict]) -> dict:
    ok = [(e, r) for e, r in zip(examples, results) if not r.get("error")]
    tool = [(e, r) for e, r in ok if r["score"]["kind_expected"] == "tool_call"]
    text = [(e, r) for e, r in ok if r["score"]["kind_expected"] == "text"]
    def rate(pairs, key, where=None):
        pairs = [(e, r) for e, r in pairs if where is None or where(r["score"])]
        return round(sum(bool(r["score"].get(key)) for _, r in pairs) / len(pairs), 3) if pairs else None
    judged = [r["judge"]["same"] for _, r in text if r.get("judge") and r["judge"].get("same") is not None]
    return {
        "n": len(examples), "errors": len(examples) - len(ok),
        "kind_accuracy": rate(ok, "kind_match"),
        "exact": rate(ok, "exact"),
        "tool_steps": {"n": len(tool), "tool_match": rate(tool, "tool_match"),
                       "args_match": rate(tool, "args_match"), "keys_match": rate(tool, "keys_match"),
                       "malformed": rate(tool, "malformed"),
                       "py_literals": round(sum(r["score"]["py_literals"] > 0 for _, r in tool) / len(tool), 3) if tool else None},
        "text_steps": {"n": len(text), "answered_as_text": rate(text, "kind_match"),
                       "forbidden": round(sum(bool(r["score"].get("forbidden")) for _, r in text) / len(text), 3) if text else None,
                       "empty": rate(text, "empty"), "len_ratio_p50": _p([r["score"].get("len_ratio") for _, r in text], .5),
                       "judge_same": round(sum(judged) / len(judged), 3) if judged else None, "judged": len(judged)},
        "hygiene": {"truncated": rate(ok, "truncated"), "think_leak": rate(ok, "think_leak")},
        "speed": {"total_ms_p50": _p([r["total_ms"] for _, r in ok], .5), "total_ms_p90": _p([r["total_ms"] for _, r in ok], .9),
                  "prompt_tps_p50": _p([r.get("prompt_tps") for _, r in ok], .5),
                  "gen_tps_p50": _p([r.get("gen_tps") for _, r in ok], .5)},
    }


def report(data: dict, show: int) -> None:
    aliases = [m["alias"] for m in data["models"]]
    print(f"\n{data['stamp']}  {data['split']} of {data['run']}: {len(data['examples'])} examples, models {', '.join(aliases)}")
    rows = [("examples / errors", lambda s: f"{s['n']} / {s['errors']}"),
            ("kind accuracy (tool vs text)", lambda s: s["kind_accuracy"]),
            ("exact match", lambda s: s["exact"]),
            ("tool steps: names", lambda s: s["tool_steps"]["tool_match"]),
            ("tool steps: args", lambda s: s["tool_steps"]["args_match"]),
            ("tool steps: malformed XML", lambda s: s["tool_steps"]["malformed"]),
            ("tool steps: python literals (T8)", lambda s: s["tool_steps"]["py_literals"]),
            ("text steps: answered as text", lambda s: s["text_steps"]["answered_as_text"]),
            ("text steps: forbidden patterns", lambda s: s["text_steps"]["forbidden"]),
            ("text steps: length vs teacher p50", lambda s: s["text_steps"]["len_ratio_p50"]),
            ("text steps: judge same facts", lambda s: f"{s['text_steps']['judge_same']} ({s['text_steps']['judged']})" if s["text_steps"]["judged"] else "-"),
            ("truncated / think leak", lambda s: f"{s['hygiene']['truncated']} / {s['hygiene']['think_leak']}"),
            ("latency ms p50 / p90", lambda s: f"{s['speed']['total_ms_p50']} / {s['speed']['total_ms_p90']}"),
            ("prompt tok/s, gen tok/s p50", lambda s: f"{s['speed']['prompt_tps_p50']}, {s['speed']['gen_tps_p50']}")]
    w = max(len(a) for a in aliases) + 2
    print(" " * 36 + "".join(a.rjust(w) for a in aliases))
    for label, f in rows:
        print(label.ljust(36) + "".join(str(f(data["summary"][a])).rjust(w) for a in aliases))
    if show:
        a = aliases[-1]
        print(f"\nmisses of {a} (first {show}):")
        n = 0
        for ex, r in zip(data["examples"], data["results"][a]):
            s = r.get("score") or {}
            miss = r.get("error") or (not s.get("kind_match")) or (s.get("tools_expected") and not s.get("args_match")) \
                or s.get("forbidden") or s.get("malformed") or s.get("truncated") or (r.get("judge", {}).get("same") is False)
            if not miss:
                continue
            n += 1
            print(f"- [{ex['metadata'].get('template')}] {last_user(ex['prompt'])[:90]}")
            print(f"    expected: {ex['completion'].replace(END, '')[:160]!r}")
            print(f"    got:      {(r.get('text') or r.get('error') or '').replace(END, '')[:160]!r}"
                  + (f"  ← {r['judge'].get('note')}" if r.get("judge") and r["judge"].get("same") is False else ""))
            if n >= show:
                break


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", default=os.environ.get("LOCAL_LLM_MODEL", "qwen3.5-9b"),
                    help="alias[@url],... (default url --server)")
    ap.add_argument("--server", default=os.environ.get("LOCAL_LLM_BASE_URL", DEFAULT_SERVER + "/v1").removesuffix("/v1"))
    ap.add_argument("--key", default=None, help="server API key (default $LOCAL_LLM_API_KEY)")
    ap.add_argument("--run", type=Path, default=DEFAULT_RUN)
    ap.add_argument("--split", default="dev", choices=["dev", "train"])
    ap.add_argument("--limit", type=int, help="a random subset of this size")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n-predict", type=int, default=400, help="generation cap in tokens (the teacher's longest step is 257)")
    ap.add_argument("--workers", type=int, default=2,
                    help="parallel requests per model; the laptop's 32k context is shared by its slots, and four "
                         "8k prompts overflow it (\"Context size has been exceeded\")")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--judge", action="store_true", help=f"{JUDGE_MODEL} on the text steps ($ANTHROPIC_API_KEY)")
    ap.add_argument("--show", type=int, default=10, help="misses to print for the last model")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--report", type=Path, help="reprint this results file")
    ap.add_argument("--judge-file", type=Path, metavar="RESULTS", help="run the judge over an existing results file's text steps")
    args = ap.parse_args()
    for env in (HERE / ".env", Path.home() / ".env"):      # as model_bench: the judge's key may live in ~/.env
        load_env(env)
    key = args.key or os.environ.get("LOCAL_LLM_API_KEY")

    if args.report:
        report(json.loads(args.report.read_text(encoding="utf-8")), args.show)
        return
    if args.judge_file:
        args.judge = True

    models = parse_models(args.models, args.server)
    examples = load_examples(args.run / "train" / f"{args.split}.jsonl", args.limit, args.seed)
    patterns = forbidden_patterns()
    judge_client = None
    if args.judge:
        try:
            import anthropic
        except ImportError:
            raise SystemExit("--judge needs the anthropic package: run as  uv run --no-project --with anthropic python sft_eval.py …")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("--judge needs ANTHROPIC_API_KEY (dev/voice/.env or ~/.env)")
        judge_client = anthropic.Anthropic()
    if args.judge_file:
        judge_file(args.judge_file, judge_client, args.show)
        return
    live = []
    for m in models:
        problem = probe(m, key, args.timeout)
        if problem:
            print(f"{m['alias']} @ {m['url']}: skipped — {problem}", file=sys.stderr)
        else:
            live.append(m)
    if not live:
        raise SystemExit("no model reachable")

    stamp = time.strftime("%Y%m%d-%H%M")
    data = {"stamp": stamp, "run": str(args.run), "split": args.split, "models": live,
            "examples": [{"metadata": e["metadata"], "prompt": e["prompt"], "completion": e["completion"]} for e in examples],
            "results": {}, "summary": {}}
    for m in live:
        print(f"{m['alias']} @ {m['url']}: {len(examples)} examples", file=sys.stderr)
        data["results"][m["alias"]] = run_model(m, key, examples, patterns, args, judge_client)
        data["summary"][m["alias"]] = summarise(examples, data["results"][m["alias"]])
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"sft-eval-{stamp}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    report(data, args.show)
    print(f"\n→ {path}")


if __name__ == "__main__":
    main()
