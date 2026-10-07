#!/usr/bin/env python3
"""Prepare an SFT generation run (sft_gen.py) for training the local brain.

Turns the chat records of a run (sft_chat/*.jsonl: system, user, assistant
with tool_calls, tool results, final text; the tools in chat shape) into
prompt/completion pairs rendered through the base model's own chat
template, exactly as llama.cpp renders them when the agent serves the
model — so the trained model generates what it will be asked to generate.

One example per assistant STEP of a record's last turn: the prompt is the
conversation up to that step plus the generation prompt (thinking off,
``<|im_start|>assistant\\n<think>\\n\\n</think>\\n\\n`` on Qwen3.5), the completion
is what the teacher produced at that step — a tool call block, or the
spoken text — ending with ``<|im_end|>``. A turn that called a tool and then
answered gives two examples; the earlier turns of a conversation are not
targets here (they are their own records). The loss belongs on the
completion only; the trainer masks the prompt.

What the template does that a trainer must not get wrong (read from the
served Qwen3.5 template): tool calls are rendered as XML
(``<tool_call>\\n<function=NAME>\\n<parameter=KEY>\\nVALUE\\n</parameter>…``), and
only when ``arguments`` is a mapping — the chat records carry it as a JSON
string, as OpenAI does, so it is parsed here first. Scalar values go
through the template's ``string`` filter, so a boolean in a HISTORY call is
``False`` and a null ``None``, in llama.cpp's rendering too (checked
against its /apply-template); the prompts keep that, being what the model
is shown. The COMPLETIONS write JSON literals (``false``, ``null``) instead:
llama.cpp parses those back to typed arguments, while Python-style ones
reach the agent's tools as the strings "False" and "None" — which is what
the base model emits today (eval suite run of 2026-10-07: catchup 'True'/
'False', channel and year 'None', as strings; pydantic takes "False" as a
bool but rejects "None" for ``int | None`` and keeps it as text for
``str | None``). Null-valued parameters are left out of the completions
altogether, which every parser reads the same way. Every assistant message after the last
user query is rendered with the empty think block, which matches the
generation prompt; the script checks that on every example.

Outputs, in --out (default <run>/train/):
  train.jsonl, dev.jsonl   {"prompt", "completion", "n_tokens",
                            "n_prompt_tokens", "n_completion_tokens",
                            "boundary_ok", "metadata": {...}}
  dropped.jsonl            the examples that failed a check, with the reason
  prep-summary.json        counts, token percentiles, drops by reason
  chat_template.jinja      the template the examples were rendered with

Split is by conversation (metadata.session_id), never by turn, so a
scenario's turns land on one side. The eval suite is the test set and is
already held out of the generated traffic.

Needs ``transformers`` (the tokenizer of --base, downloaded on first use);
nothing else beyond the standard library. Made to run on the training
machine; --server checks a sample of prompts against the live llama.cpp
server's /apply-template, which is the one rendering that matters.

  ./sft_train_prep.py --run ~/news/voice-sft/run1 --base Qwen/Qwen3.5-9B
  ./sft_train_prep.py --run ~/news/voice-sft/run1 --limit 50 --show 3      # a look
  ./sft_train_prep.py --run ~/news/voice-sft/run1 --server http://127.0.0.1:8001 --server-sample 40
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_RUN = Path.home() / "news" / "voice-sft" / "run1"
DEFAULT_BASE = "Qwen/Qwen3.5-9B"
ASSISTANT_HEAD = "<|im_start|>assistant\n"
END = "<|im_end|>"

# The XML tool-call block the Qwen3.5 template renders (and llama.cpp parses back).
XML_CALL = re.compile(r"<tool_call>\n<function=(?P<name>[^>\n]+)>\n(?P<params>.*?)</function>\n</tool_call>", re.S)
XML_PARAM = re.compile(r"<parameter=(?P<key>[^>\n]+)>\n(?P<value>.*?)\n</parameter>\n", re.S)


# ---- records ----------------------------------------------------------------------------
def load_records(run: Path) -> list[dict]:
    recs = []
    for p in sorted((run / "sft_chat").glob("*.jsonl")):
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            try:
                r = json.loads(line)
            except ValueError:
                print(f"unreadable line {p.name}:{n}", file=sys.stderr)
                continue
            r.setdefault("metadata", {})
            r["metadata"].setdefault("session_id", f"{p.stem}:{n}")
            r["metadata"].setdefault("turn_index", 0)
            recs.append(r)
    recs.sort(key=lambda r: (r["metadata"]["session_id"], r["metadata"]["turn_index"]))
    return recs


def normalise(messages: list[dict]) -> tuple[list[dict], list[str]]:
    """The record's messages as the template expects them: arguments as
    mappings (values untouched: the template's `string` filter then writes
    True/False/None exactly as llama.cpp does), string content. Returns the
    problems met."""
    out, problems = copy.deepcopy(messages), []
    for m in out:
        if m.get("content") is None:
            m["content"] = ""
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except ValueError:
                    problems.append(f"arguments not JSON: {fn.get('name')}")
                    args = {}
            if not isinstance(args, dict):
                problems.append(f"arguments not an object: {fn.get('name')}")
                args = {}
            fn["arguments"] = args
    return out, problems


def split_sessions(records: list[dict], dev_frac: float, seed: int) -> set[str]:
    """The conversation ids that go to dev."""
    ids = sorted({r["metadata"]["session_id"] for r in records})
    random.Random(seed).shuffle(ids)
    return set(ids[:round(len(ids) * dev_frac)])


# ---- rendering ------------------------------------------------------------------------------
def render(tok, messages: list[dict], tools: list[dict], generation_prompt: bool) -> str:
    return tok.apply_chat_template(messages, tools=tools or None, tokenize=False,
                                   add_generation_prompt=generation_prompt, enable_thinking=False)


def common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def check_calls(completion: str, message: dict) -> str | None:
    """The rendered XML must carry every call of the message, in order,
    with every argument; returns the first problem or None."""
    expected = [c.get("function") or {} for c in message.get("tool_calls") or []]
    found = XML_CALL.findall(completion)
    if len(found) != len(expected):
        return f"tool_call_render: {len(found)} blocks for {len(expected)} calls"
    for (name, params), fn in zip(found, expected):
        if name != fn.get("name"):
            return f"tool_call_render: name {name!r} != {fn.get('name')!r}"
        keys = [k for k, _ in XML_PARAM.findall(params)]
        if keys != list((fn.get("arguments") or {}).keys()):
            return f"tool_call_render: parameters {keys} != {list((fn.get('arguments') or {}).keys())}"
    if expected and not completion.endswith(END):
        return "tool_call_render: no end token"
    return None


def clean_params(completion: str, message: dict) -> str:
    """The target's tool-call parameters as they should be generated:
    booleans as JSON literals, which llama.cpp parses back to typed values
    (Python-style "False" reaches the tools as a string, accepted by luck),
    and null-valued parameters left out — "None" as text is rejected by
    `int | None` and taken literally by `str | None`, a channel named None."""
    calls = [c.get("function") or {} for c in message.get("tool_calls") or []]
    out, pos = [], 0
    for m, fn in zip(XML_CALL.finditer(completion), calls):
        params = m.group("params")
        for k, v in (fn.get("arguments") or {}).items():
            if v is None:
                params = params.replace(f"<parameter={k}>\nNone\n</parameter>\n", "", 1)
            elif isinstance(v, bool):
                params = params.replace(f"<parameter={k}>\n{v}\n</parameter>\n",
                                        f"<parameter={k}>\n{json.dumps(v)}\n</parameter>\n", 1)
        out.append(completion[pos:m.start("params")] + params)
        pos = m.end("params")
    return "".join(out) + completion[pos:]


def examples_for(tok, record: dict, max_tokens: int) -> tuple[list[dict], list[dict]]:
    """Prompt/completion pairs for the assistant steps of the record's last
    turn; the dropped ones carry a reason."""
    meta = record["metadata"]
    msgs, problems = normalise(record["messages"])
    tools = record.get("tools") or []
    kept, dropped = [], []
    base = {"session_id": meta["session_id"], "turn_index": meta["turn_index"],
            "template": meta.get("template"), "source": meta.get("source")}
    if problems:
        dropped.append({**base, "reason": "; ".join(problems)})
        return kept, dropped
    users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
    if not users or msgs[0]["role"] != "system":
        dropped.append({**base, "reason": "no user message" if not users else "system message not first"})
        return kept, dropped
    last_user = users[-1]
    steps = [j for j in range(last_user + 1, len(msgs)) if msgs[j]["role"] == "assistant"]
    for n_step, j in enumerate(steps):
        info = {**base, "step": n_step, "n_steps": len(steps)}
        before = render(tok, msgs[:j], tools, generation_prompt=False)
        prompt = render(tok, msgs[:j], tools, generation_prompt=True)
        full = render(tok, msgs[:j + 1], tools, generation_prompt=False)
        if not full.startswith(before) or not prompt.startswith(before):
            dropped.append({**info, "reason": "template not prefix-stable"})
            continue
        gen_tail, body = prompt[len(before):], full[len(before):]
        cut = common_prefix_len(body, gen_tail)
        if cut < len(ASSISTANT_HEAD) or not body.startswith(ASSISTANT_HEAD):
            dropped.append({**info, "reason": "rendered message does not start with the assistant head"})
            continue
        think_ok = cut == len(gen_tail)          # the message carries the think block the prompt ends with
        completion = body[cut:].rstrip("\n")
        if not completion.endswith(END):
            dropped.append({**info, "reason": "completion does not end with the end token"})
            continue
        if completion == END:
            dropped.append({**info, "reason": "empty completion"})
            continue
        problem = check_calls(completion, msgs[j])
        if problem:
            dropped.append({**info, "reason": problem})
            continue
        completion = clean_params(completion, msgs[j])
        ids_p = tok(prompt, add_special_tokens=False).input_ids
        ids_c = tok(completion, add_special_tokens=False).input_ids
        ids_all = tok(prompt + completion, add_special_tokens=False).input_ids
        boundary_ok = ids_all[:len(ids_p)] == ids_p and ids_all[len(ids_p):] == ids_c
        n = len(ids_all)
        kind = "tool_call" if msgs[j].get("tool_calls") else "text"
        if kind == "tool_call" and (msgs[j].get("content") or "").strip():
            kind = "text+tool_call"
        ex = {"prompt": prompt, "completion": completion, "n_tokens": n,
              "n_prompt_tokens": len(ids_p), "n_completion_tokens": len(ids_c),
              "boundary_ok": boundary_ok, "metadata": {**info, "kind": kind, "think_block_ok": think_ok}}
        if n > max_tokens:
            dropped.append({**info, "reason": f"over max tokens: {n}", "n_tokens": n})
            continue
        kept.append(ex)
    return kept, dropped


# ---- the live server's rendering --------------------------------------------------------------
def server_prompt(server: str, key: str | None, messages: list[dict], tools: list[dict]) -> str:
    """What llama.cpp would feed the model for these messages: its
    /apply-template, with the agent's own request settings."""
    body = json.dumps({"messages": messages, "tools": tools or None,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(server.rstrip("/") + "/apply-template", data=body,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {key}"} if key else {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["prompt"]


def check_server(server: str, key: str | None, records: list[dict], examples: list[dict], sample: int, seed: int) -> dict:
    """Compare a sample of our prompts with the server's rendering of the
    same messages (the records' original chat shape, string arguments and
    all — what the agent sends). Reports mismatches and the first diff."""
    by_key = {(r["metadata"]["session_id"], r["metadata"]["turn_index"]): r for r in records}
    picks = random.Random(seed).sample(examples, k=min(sample, len(examples)))
    out = {"checked": 0, "mismatched": 0, "errors": 0}
    for ex in picks:
        m = ex["metadata"]
        rec = by_key[(m["session_id"], m["turn_index"])]
        msgs = rec["messages"]
        last_user = max(i for i, x in enumerate(msgs) if x["role"] == "user")
        steps = [j for j in range(last_user + 1, len(msgs)) if msgs[j]["role"] == "assistant"]
        j = steps[m["step"]]
        try:
            theirs = server_prompt(server, key, msgs[:j], rec.get("tools") or [])
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as e:
            out["errors"] += 1
            out.setdefault("first_error", f"{type(e).__name__}: {e}"[:200])
            continue
        out["checked"] += 1
        if theirs != ex["prompt"]:
            out["mismatched"] += 1
            if "first_diff" not in out:
                i = common_prefix_len(theirs, ex["prompt"])
                out["first_diff"] = {"session_id": m["session_id"], "turn_index": m["turn_index"], "step": m["step"],
                                     "at": i, "server": theirs[max(0, i - 80):i + 160], "ours": ex["prompt"][max(0, i - 80):i + 160]}
    return out


# ---- report -----------------------------------------------------------------------------------
def pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def summarise(train: list[dict], dev: list[dict], dropped: list[dict], records: int, sessions: int, dev_ids: int) -> dict:
    allx = train + dev
    n_tok = [e["n_tokens"] for e in allx]
    n_c = [e["n_completion_tokens"] for e in allx]
    return {
        "records": records, "conversations": sessions, "dev_conversations": dev_ids,
        "examples": {"train": len(train), "dev": len(dev)},
        "by_kind": dict(Counter(e["metadata"]["kind"] for e in allx)),
        "steps_per_turn": dict(Counter(e["metadata"]["n_steps"] for e in allx if e["metadata"]["step"] == 0)),
        "by_source": dict(Counter(e["metadata"]["source"] for e in allx)),
        "tokens": {"p50": pct(n_tok, .5), "p90": pct(n_tok, .9), "max": max(n_tok, default=0), "total": sum(n_tok),
                   "completion_p50": pct(n_c, .5), "completion_max": max(n_c, default=0), "completion_total": sum(n_c)},
        "boundary_mismatch": sum(not e["boundary_ok"] for e in allx),
        "think_block_mismatch": sum(not e["metadata"]["think_block_ok"] for e in allx),
        "dropped": dict(Counter(d["reason"].split(":")[0] for d in dropped)),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", type=Path, default=DEFAULT_RUN, help="an sft_gen.py output directory (reads sft_chat/)")
    ap.add_argument("--out", type=Path, help="default <run>/train")
    ap.add_argument("--base", default=DEFAULT_BASE, help="the model whose tokenizer and chat template render the examples")
    ap.add_argument("--template", type=Path, help="render with this chat template instead of the tokenizer's "
                                                  "(e.g. the one llama-server reports in /props)")
    ap.add_argument("--dev-frac", type=float, default=0.05, help="share of conversations held for dev loss")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=16384,
                    help="drop examples longer than this (prompt + completion); each example carries n_tokens, "
                         "so a trainer short of memory can filter lower")
    ap.add_argument("--limit", type=int, help="only the first N records (a quick look)")
    ap.add_argument("--show", type=int, default=0, help="print N examples (prompt tail and completion)")
    ap.add_argument("--server", help="llama.cpp server URL: compare a sample of prompts with its /apply-template")
    ap.add_argument("--server-key", default=os.environ.get("LOCAL_LLM_API_KEY"), help="its API key (default $LOCAL_LLM_API_KEY)")
    ap.add_argument("--server-sample", type=int, default=40)
    args = ap.parse_args()
    out = args.out or args.run / "train"

    try:
        from transformers import AutoTokenizer
    except ImportError:
        raise SystemExit("needs transformers (pip install transformers) — the tokenizer of --base renders the examples")
    tok = AutoTokenizer.from_pretrained(args.base)
    if args.template:
        tok.chat_template = args.template.read_text(encoding="utf-8")
    if not tok.chat_template:
        raise SystemExit(f"{args.base} has no chat template")

    records = load_records(args.run)
    if not records:
        raise SystemExit(f"no records under {args.run / 'sft_chat'}")
    if args.limit:
        records = records[:args.limit]
    dev_ids = split_sessions(records, args.dev_frac, args.seed)
    sessions = {r["metadata"]["session_id"] for r in records}

    t0 = time.monotonic()
    train, dev, dropped = [], [], []
    for n, r in enumerate(records, 1):
        kept, bad = examples_for(tok, r, args.max_tokens)
        (dev if r["metadata"]["session_id"] in dev_ids else train).extend(kept)
        dropped.extend(bad)
        if n % 200 == 0:
            print(f"{n}/{len(records)} records, {len(train) + len(dev)} examples, {len(dropped)} dropped, "
                  f"{time.monotonic() - t0:.0f}s", file=sys.stderr)

    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train.jsonl", train), ("dev.jsonl", dev), ("dropped.jsonl", dropped)):
        (out / name).write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")
    (out / "chat_template.jinja").write_text(tok.chat_template, encoding="utf-8")
    summary = summarise(train, dev, dropped, len(records), len(sessions), len(dev_ids & sessions))
    summary["base"] = args.base
    summary["template"] = str(args.template) if args.template else "tokenizer's"
    summary["max_tokens"] = args.max_tokens
    summary["generation_prompt"] = render(tok, [{"role": "user", "content": "x"}], [], True).split("x<|im_end|>\n")[-1]
    if args.server and (train or dev):
        summary["server_check"] = check_server(args.server, args.server_key, records, train + dev, args.server_sample, args.seed)
    (out / "prep-summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")

    for ex in (train + dev)[:args.show]:
        print("=" * 100)
        print("…" + ex["prompt"][-300:])
        print("-" * 40, "completion", ex["metadata"]["kind"], f"({ex['n_completion_tokens']} tokens)")
        print(ex["completion"])
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    print(f"→ {out}/ (train.jsonl, dev.jsonl, dropped.jsonl, prep-summary.json, chat_template.jinja)")
    if summary["boundary_mismatch"] or summary["think_block_mismatch"]:
        print("WARNING: boundary or think-block mismatches — read prep-summary.json before training", file=sys.stderr)
    sc = summary.get("server_check")
    if sc and (sc["mismatched"] or sc["errors"]):
        print(f"WARNING: server check: {sc['mismatched']} prompts differ from the server's rendering, "
              f"{sc['errors']} errors — see prep-summary.json", file=sys.stderr)


if __name__ == "__main__":
    main()
