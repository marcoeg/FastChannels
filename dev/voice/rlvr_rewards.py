#!/usr/bin/env python3
"""Verifiable rewards for RL on the local brain — code, no model.

A trajectory here is one step: the prompt as llama.cpp feeds it (system
prompt, the conversation, this turn's tool calls and their results inside
`<tool_response>` blocks, the generation prompt) and one completion — a
tool call block or the spoken answer. Everything a reward needs is in the
prompt, so there is no environment to run: GRPO samples completions for
a prompt and this module scores them.

Components (each 0–1, or None when it does not apply to the step):

  format       parseable tool-call XML with nothing trailing, no <think>, no
               Python literals in parameters, a spoken part under 400 chars;
               a failed format floors the total
  routing      the step's calls (with the turn's earlier calls, read from the
               prompt) fit one of the template's `tools_any_of` sets — a text
               answer needs a set already satisfied; `[]` means "no tool"
  args         the template's `tool_args` on the step's calls
  grounding    every title, channel and time the answer speaks occurs in the
               turn's tool results, the on-screen line, the request or the
               earlier conversation — the reward against inventions and swaps
  coverage     a listing answer (schedule, catch-up, on now) names two of
               the first three rows of the result, in any order — the reward
               against dropping what was asked for
  fabrication  a spoken answer that asserts titles, times or an outcome with
               no tool result behind it (1 = fabricated → penalty)
  length       near the teacher's length; long answers decay to 0
  kind         the step takes the teacher's decision (tool call vs text) — a
               weak prior, small weight

`reward()` returns the weighted total and the components. The prompt set
and the channel vocabulary come from `build` (a prep's train/ and the run's
rollouts); `score-eval` runs the components over an sft_eval results file
so the teacher's completions and a model's can be compared — the check
that the rewards reward the right thing before any GPU is rented.

  ./rlvr_rewards.py build --runs ~/news/voice-sft/run1 ~/news/voice-sft/run2 --out ~/news/voice-sft/rl
  ./rlvr_rewards.py score-eval ~/news/model-bench/sft-eval-20261009-0630.json --vocab ~/news/voice-sft/rl/vocab.json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

END = "<|im_end|>"
XML_CALL = re.compile(r"<tool_call>\n<function=(?P<name>[^>\n]+)>\n(?P<params>.*?)</function>\n</tool_call>", re.S)
XML_PARAM = re.compile(r"<parameter=(?P<key>[^>\n]+)>\n(?P<value>.*?)\n</parameter>\n", re.S)
PY_LITERAL = re.compile(r"<parameter=[^>\n]+>\n(True|False|None)\n</parameter>")
TOOL_RESPONSE = re.compile(r"<tool_response>\n(.*?)\n</tool_response>", re.S)
USER_TURN = re.compile(r"<\|im_start\|>user\n(?!<tool_response>)(.*?)<\|im_end\|>", re.S)
ASSISTANT_TURN = re.compile(r"<\|im_start\|>assistant\n(.*?)<\|im_end\|>", re.S)
NARRATED = re.compile(r"\b(let me (check|look|see)|i'?m (checking|looking)|checking (the|what)|i'?ll (check|look))\b", re.I)
CLAIMED = re.compile(r"\b(tuned|switched (to|over)|turned on|now playing|playing from the beginning|restart(ed|ing)|"
                     r"muted|unmuted|paused|resumed|stopped|volume is (now )?(at )?\d|set (it |the volume )?to \d|"
                     r"turned (it |the volume )?(up|down))\b", re.I)
RESULT_CLAIM = re.compile(r"\b(no listings|nothing (is )?(scheduled|listed|on)|isn'?t (on|scheduled|listed|available)|"
                          r"not (on|scheduled|listed|available)|found nothing|couldn'?t find|can'?t find|no catch-?up)\b", re.I)
NO_MATCH = re.compile(r"found nothing|Nothing on this TV's channels matches|no (recordings|results|matches)|is not in this TV|not carried", re.I)
MAX_SPOKEN = 400
WEIGHTS = {"format": 1.5, "routing": 1.0, "args": 1.0, "grounding": 1.0, "coverage": 0.5,
           "fabrication": -1.5, "length": 0.5, "kind": 0.25}
LISTING_TEMPLATES = {"on_now", "on_now_channel", "genre_now", "genre_schedule", "channel_schedule", "channel_lineup",
                     "whats_next", "whats_after_title", "movies", "recommend", "catchup_browse", "vod_browse",
                     "m_on_now_tune", "m_channel_evening_followups", "m_genre_tonight_pick", "m_now_then_next",
                     "m_time_slot_channel", "m_catchup_pick", "m_channel_schedule_tune", "m_recommend_pick",
                     "m_schedule_followups", "m_title_then_after"}

# ---- text normalisation --------------------------------------------------------------------------
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "noon": 12, "midnight": 12, "midday": 12}
MIN_WORDS = {"o'clock": 0, "oclock": 0, "fifteen": 15, "thirty": 30, "forty-five": 45, "forty five": 45,
             "oh one": 1, "oh two": 2, "oh three": 3, "oh four": 4, "oh five": 5, "oh six": 6, "oh seven": 7,
             "oh eight": 8, "oh nine": 9, "ten": 10, "twenty": 20, "twenty-five": 25, "twenty five": 25,
             "thirty-five": 35, "thirty five": 35, "forty": 40, "fifty": 50, "fifty-five": 55, "fifty five": 55}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
TIME_CUE = r"(?:at|until|till|from|to|around|by|before|after|starting|starts|ends|ending|through|past|and)"
_words = "|".join(sorted(NUM_WORDS, key=len, reverse=True))
_mins = "|".join(sorted(MIN_WORDS, key=len, reverse=True)).replace("'", "['’]")
TIME_WORDS = re.compile(rf"\b{TIME_CUE}\s+(?:half\s+past\s+)?(?P<h>{_words})(?:\s+(?P<m>{_mins}))?\b", re.I)
TIME_DIGITS = re.compile(r"(?<![\w:])(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)?(?=\W|$)", re.I)
CHANNEL_NUM = re.compile(rf"\bchannel\s+(?P<n>\d{{1,4}}|(?:{'|'.join(TENS)})(?:[- ](?:{_words}))?|(?:{_words}))\b", re.I)
PERCENT = re.compile(r"\b(\d{1,3})\s*(%|percent)\b", re.I)
STOP_CAPS = {"Yes", "No", "Sorry", "Right", "Next", "Tonight", "Tomorrow", "Today", "Volume", "The", "It", "That",
             "This", "There", "Here", "Playing", "Done", "Okay", "OK", "Sure", "I", "You", "Your", "We", "Want",
             "Would", "Shall", "Let", "After", "Before", "At", "On", "From", "For", "Then", "Later", "Now",
             "Nothing", "None", "Not", "Both", "Each", "Also", "And", "But", "So", "If", "When", "What", "Which",
             "Who", "How", "Catch", "Live", "Earlier", "Yesterday", "Channel", "Mute", "Muted", "Paused", "Stopped",
             "Tuned", "Switched", "Set", "Turned", "Welcome", "Good", "Morning", "Evening", "Afternoon", "Night",
             "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February",
             "March", "April", "May", "June", "July", "August", "September", "October", "November", "December",
             "TV", "PM", "AM", "HD", "UTC", "Minny", "Bay", "Area", "Spanish", "English", "French", "Italian",
             "German", "Local", "National", "Pacific", "News", "Sports", "Movie", "Movies", "Film", "Films",
             "Episode", "Series", "Season", "Part", "Hour", "Minutes", "Everything", "Anything", "Something",
             "Still", "Otherwise", "Instead", "Try", "Or", "Else", "Just", "Only", "Other", "Another", "New",
             "Older", "Newer", "First", "Second", "Third", "Last", "One", "Two", "Three", "Four", "Five", "Six",
             "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve", "Half", "Quarter", "Fox", "Yours", "Mine",
             "Great", "Thanks", "Thank", "Cool", "Fine", "All", "Any", "Some", "Most", "Much", "More", "Less",
             "Please", "Want", "Need", "Like", "Love", "Because", "Since", "While", "Though", "Although",
             "Of", "In", "With", "By", "To", "A", "An", "Yet", "Already", "Again", "Too", "Very", "Really"}
TITLE_CONNECT = {"of", "the", "and", "&", "de", "la", "le", "el", "los", "las", "a", "to", "in", "on", "with", "for",
                 "at", "y", "en", "del", "da", "do", "di", "vs", "vs.", "-", "–", "—", ":", "'s", "’s", "n", "'n'"}
CAP_TOKEN = re.compile(r"[A-ZÀ-ÝÁÉÍÓÚÑ][\w’'&.-]*|\d{1,4}")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().replace("’", "'")
    s = re.sub(r"[^\w\s']", " ", s)
    s = re.sub(r"\b(the|a|an)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> set:
    return {t for t in norm(s).split() if len(t) > 1}


def parse_clock(s: str):
    """'9:30 AM today' / '10 PM' → (hour12, minute) or None."""
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s or "", re.I)
    if not m:
        return None
    h = int(m.group(1)) % 12 or 12
    return (h, int(m.group(2) or 0))


def number_word(s: str):
    s = s.lower().replace("-", " ")
    if s.isdigit():
        return int(s)
    parts = s.split()
    if parts[0] in TENS:
        return TENS[parts[0]] + (NUM_WORDS.get(parts[1], 0) if len(parts) > 1 else 0)
    return NUM_WORDS.get(s)


# ---- what the prompt holds ---------------------------------------------------------------------------
def rows_of(result_text: str) -> list[dict]:
    t = result_text.strip()
    if not t.startswith(("[", "{")):
        return []
    try:
        d = json.loads(t)
    except ValueError:
        return []
    if isinstance(d, dict):
        d = d.get("rows") or d.get("results") or d.get("items") or []
    return [r for r in d if isinstance(r, dict)] if isinstance(d, list) else []


class Sources:
    """Everything an answer may legitimately speak about, read from the prompt."""

    def __init__(self, prompt: str):
        self.prompt = prompt
        users = USER_TURN.findall(prompt)
        self.request = users[-1].strip() if users else ""
        i = self.request.find("\n\n[context for this turn — not spoken by the viewer]")   # the block is a source, not the request
        if i >= 0:
            self.request = self.request[:i].strip()
        self.earlier = " ".join(users[:-1]) + " " + " ".join(ASSISTANT_TURN.findall(prompt))
        sys_end = prompt.find(END)
        system = prompt[:sys_end] if sys_end > 0 else ""
        screen = next((ln for ln in system.splitlines() if ln.startswith("On screen now")), "")
        self.screen = screen
        # the last turn: after the last real user message
        last_user_pos = max((m.start() for m in USER_TURN.finditer(prompt)), default=0)
        turn = prompt[last_user_pos:]
        self.called_so_far = re.findall(r"<function=([^>\n]+)>", turn)
        results = TOOL_RESPONSE.findall(turn)
        self.turn_results = results
        self.turn_rows = [rows_of(r) for r in results]
        self.last_rows = next((rs for rs in reversed(self.turn_rows) if rs), [])
        all_rows = [r for res in TOOL_RESPONSE.findall(prompt) for r in rows_of(res)]
        self.titles = {norm(r.get("title") or "") for r in all_rows} - {""}
        self.channels = {norm(r.get("channel_name") or "") for r in all_rows} - {""}
        self.numbers = {str(r.get("channel_number")) for r in all_rows if r.get("channel_number") is not None}
        self.times = set()
        for r in all_rows:
            for k in ("start_local", "end_local"):
                c = parse_clock(r.get(k) or "")
                if c:
                    self.times.add(c)
        m = re.search(r"On screen now \((?:[^)]*)\):\s*(.*?)\s+channel\s+(\d+)", screen)
        if m:
            self.channels.add(norm(m.group(1)))
            self.numbers.add(m.group(2))
        raw_results = " ".join(TOOL_RESPONSE.findall(prompt))
        self.free_text = norm(self.request + " " + self.earlier + " " + screen + " " + raw_results)
        self.free_tokens = tokens(self.request + " " + self.earlier + " " + screen + " " + raw_results)
        self.has_results = bool(results)
        # "empty" only when every result of the turn is one of the guide's nothing-found sentences
        self.results_empty = bool(results) and all(NO_MATCH.search(r) and not rows_of(r) for r in results)


class Vocab:
    """Channel names and numbers of the lineup, for spotting a channel named
    that the turn's results do not carry (a swap)."""

    def __init__(self, channels: list[str], numbers: list[str], aliases: list[str] = ()):
        names = sorted({n for n in channels + list(aliases) if n and len(n) > 2}, key=len, reverse=True)
        self.names = names
        self.numbers = set(numbers)
        self.pattern = re.compile(r"(?<!\w)(" + "|".join(re.escape(n) for n in names) + r")(?!\w)", re.I) if names else None

    @classmethod
    def load(cls, path: Path) -> "Vocab":
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(d.get("channels", []), d.get("numbers", []), d.get("aliases", []))


# ---- the completion -------------------------------------------------------------------------------
def split_completion(completion: str) -> tuple[list[tuple[str, dict]], str, str | None]:
    """(calls, spoken text, format problem or None)."""
    text = completion.replace(END, "")
    problem = None
    if "<think>" in text:
        problem = "think block"
    calls = [(m.group("name"), dict(XML_PARAM.findall(m.group("params")))) for m in XML_CALL.finditer(text)]
    if "<tool_call>" in text:
        if not calls or text.count("<tool_call>") != len(calls):
            problem = problem or "malformed tool call"
        tail = text[text.rfind("</tool_call>") + len("</tool_call>"):]
        if tail.strip():
            problem = problem or "text after the tool call"
        if PY_LITERAL.search(text):
            problem = problem or "python literal"
    spoken = XML_CALL.sub("", text)
    spoken = re.sub(r"<think>.*?</think>", "", spoken, flags=re.S).strip()
    if not calls and not spoken:
        problem = problem or "empty"
    if len(spoken) > MAX_SPOKEN:
        problem = problem or "too long"
    return calls, spoken, problem


def mentions(text: str, vocab: Vocab | None):
    """Title-like spans, channel names, channel numbers and clock times in an answer."""
    titles = []
    # capitalised runs, connectors allowed inside; sentence starters dropped
    toks = re.findall(r"[\w’'&-]+|[:,.;!?—–-]", text)
    run, run_at_start, prev_end = [], False, True
    contraction = re.compile(r"^[A-Za-zÀ-ÿ]+[’'](m|ll|s|re|ve|d|t)$")

    def flush():
        if run and not (run_at_start and len(run) == 1):      # a lone sentence-initial word is not a title
            titles.append(" ".join(run).strip("-–—: "))
    for i, t in enumerate(toks):
        is_cap = (bool(re.match(r"[A-ZÀ-ÝÁÉÍÓÚÑ]", t)) and t not in STOP_CAPS and not contraction.match(t)
                  and not re.match(r"^[A-Z]{1,4}-[a-z]", t))              # "TV-viewing"
        if is_cap:
            if not run:
                run_at_start = prev_end
            run.append(t)
        elif run and (t.lower() in TITLE_CONNECT) and i + 1 < len(toks) and re.match(r"[A-ZÀ-ÝÁÉÍÓÚÑ]", toks[i + 1]):
            run.append(t)
        else:
            flush()
            run = []
        prev_end = t in {".", "!", "?", ";", ":", "—", "–"}
    flush()
    titles = [t for t in titles if len(t) > 1 and not t.isdigit()]
    channels = [m.group(1) for m in vocab.pattern.finditer(text)] if vocab and vocab.pattern else []
    numbers = []
    for m in CHANNEL_NUM.finditer(text):
        n = number_word(m.group("n"))
        if n is not None:
            numbers.append(str(n))
    times = []
    masked = PERCENT.sub(" ", CHANNEL_NUM.sub(" ", text))
    for m in TIME_WORDS.finditer(masked):
        if m.group("h").lower() in ("noon", "midnight", "midday"):
            continue
        h = NUM_WORDS[m.group("h").lower()]
        mm = MIN_WORDS.get((m.group("m") or "").lower().replace("’", "'"), 0) if m.group("m") else 0
        if m.group("m") and m.group("m").lower() == "ten":
            mm = 10
        times.append((h, mm))
    for m in TIME_DIGITS.finditer(masked):
        h = int(m.group("h"))
        before = masked[max(0, m.start() - 12):m.start()].lower()
        cued = m.group("m") is not None or m.group("ap") or re.search(rf"\b{TIME_CUE}\s*$", before)
        if cued and 0 < h <= 12:
            times.append((h % 12 or 12, int(m.group("m") or 0)))
        elif cued and 12 < h <= 23:
            times.append((h - 12, int(m.group("m") or 0)))
    return titles, channels, numbers, times


def grounded_title(span: str, src: Sources) -> bool:
    n = norm(span)
    if not n or n in src.free_text or any(n in t or t in n for t in src.titles) or any(n in c or c in n for c in src.channels):
        return True
    st = tokens(span) - TITLE_CONNECT
    if st and st <= src.free_tokens:
        return True
    for t in src.titles | src.channels:
        tt = tokens(t)
        if st and tt and len(st & tt) / len(st | tt) >= 0.5:
            return True
    return False


def grounding(spoken: str, src: Sources, vocab: Vocab | None) -> tuple[float | None, dict]:
    if not spoken:
        return None, {}
    titles, channels, numbers, times = mentions(spoken, vocab)
    bad = {"titles": [t for t in titles if not grounded_title(t, src)],
           "channels": [c for c in channels if norm(c) not in src.channels and norm(c) not in src.free_text
                        and not any(norm(c) in t or t in norm(c) for t in src.channels)],
           "numbers": [n for n in numbers if n not in src.numbers and n not in src.free_text.split()],
           "times": [t for t in times if t not in src.times and (t[0], 0) not in src.times
                     and f"{t[0]}" not in src.free_text.split()]}
    n_mentions = len(titles) + len(channels) + len(numbers) + len(times)
    n_bad = sum(len(v) for v in bad.values())
    score = 1.0 if n_mentions == 0 else max(0.0, 1 - n_bad / n_mentions)
    return score, {"mentions": n_mentions, "ungrounded": bad}


def coverage(spoken: str, src: Sources, k: int = 3) -> tuple[float | None, dict]:
    rows = src.last_rows[:k]
    if not rows or not spoken:
        return None, {}
    sp = norm(spoken)
    _, channels, numbers, times = mentions(spoken, None)
    covered = []
    for r in rows:
        title = norm(r.get("title") or "")
        t_ok = bool(title) and (title in sp or len(tokens(title) & tokens(spoken)) >= max(1, len(tokens(title)) - 1))
        ch = norm(r.get("channel_name") or "")
        c_ok = bool(ch) and (ch in sp or str(r.get("channel_number")) in numbers)
        start = parse_clock(r.get("start_local") or "")
        s_ok = start in times if start else False
        covered.append(t_ok or (c_ok and s_ok))
    return min(1.0, sum(covered) / min(2, len(rows))), {"rows": len(rows), "covered": covered}   # two of the first three is the bar


HELPERS = {"search_memory", "tv_get_state", "minny_find_channels"}


def routing(calls: list, checks: dict, src: Sources) -> float | None:
    """As the suite's `routed`: a set is met when all its tools were called,
    extra calls allowed, `[]` meaning no tool at all. A call step counts
    when its tool belongs to some set (a helper half); a text step when a
    set is already met by the turn's calls."""
    sets = checks.get("tools_any_of")
    if sets is None:
        return None
    if calls:
        wanted = {t for s in sets for t in s}
        return min(1.0 if n in wanted else 0.5 if n in HELPERS else 0.0 for n, _ in calls)
    done = set(src.called_so_far)
    return 1.0 if any((set(s) <= done) if s else not done for s in sets) else 0.0


def args_ok(calls: list, checks: dict) -> float | None:
    specs = checks.get("tool_args") or []
    hits = []
    for spec in specs:
        for name, params in calls:
            if name != spec.get("tool"):
                continue
            if "regex" in spec:
                hits.append(bool(re.search(spec["regex"], str(params.get(spec.get("arg"), "")))))
            elif "args" in spec:
                ok = True
                for k, v in spec["args"].items():
                    got = params.get(k)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        try:
                            ok &= abs(float(got) - float(v)) <= 0.05
                        except (TypeError, ValueError):
                            ok = False
                    else:
                        ok &= str(got).strip().casefold() == str(v).strip().casefold()
                hits.append(ok)
    return None if not hits else (1.0 if all(hits) else 0.0)


def fabrication(spoken: str, calls: list, src: Sources, vocab: Vocab | None) -> float | None:
    if calls or not spoken:
        return None
    titles, channels, numbers, times = mentions(spoken, vocab)
    novel_titles = [t for t in titles if not grounded_title(t, src)]
    novel_times = [t for t in times if t not in src.times and str(t[0]) not in src.free_text.split()
                   and not any(w in src.free_text for w, n in NUM_WORDS.items() if n == t[0])]
    if not src.has_results:
        # nothing was looked up: a result claimed, a TV action claimed, a lookup narrated, or specifics invented
        claims = RESULT_CLAIM.search(spoken) or CLAIMED.search(spoken) or NARRATED.search(spoken)
        return 1.0 if (claims or novel_titles or novel_times) else 0.0
    if src.results_empty and (novel_titles or novel_times):
        return 1.0
    return 0.0


def length(spoken: str, teacher_spoken: str, calls: list) -> float | None:
    if calls or not spoken:
        return None
    if len(spoken) > MAX_SPOKEN:
        return 0.0
    ratio = len(spoken) / max(len(teacher_spoken), 40)
    return 1.0 if ratio <= 1.3 else max(0.0, 1 - (ratio - 1.3) / 1.2)


def reward(prompt: str, completion: str, extras: dict, vocab: Vocab | None = None,
           weights: dict = WEIGHTS) -> tuple[float, dict]:
    """extras: checks (dict), teacher_completion (str), listing (bool), kind_expected ('tool_call'|'text')."""
    src = Sources(prompt)
    calls, spoken, problem = split_completion(completion)
    comp: dict = {"format": 0.0 if problem else 1.0, "format_problem": problem}
    checks = extras.get("checks") or {}
    comp["routing"] = routing(calls, checks, src)
    comp["args"] = args_ok(calls, checks) if calls else None
    comp["grounding"], comp["grounding_detail"] = grounding(spoken, src, vocab) if not calls else (None, {})
    comp["coverage"], comp["coverage_detail"] = coverage(spoken, src) if (extras.get("listing") and not calls) else (None, {})
    comp["fabrication"] = fabrication(spoken, calls, src, vocab)
    t_calls, t_spoken, _ = split_completion(extras.get("teacher_completion") or "")
    comp["length"] = length(spoken, t_spoken, calls)
    kind_expected = extras.get("kind_expected") or ("tool_call" if t_calls else "text")
    comp["kind"] = 1.0 if (("tool_call" if calls else "text") == kind_expected) else 0.0
    if problem:
        total = -1.0
    else:
        total = sum(weights[k] * comp[k] for k in weights if comp.get(k) is not None)
    return round(total, 4), comp


# ---- the prompt set --------------------------------------------------------------------------------
def build(runs: list[Path], out: Path) -> None:
    """rl/prompts.jsonl from the runs' train/ examples joined with the
    rollouts' checks, and rl/vocab.json from every channel the results name."""
    out.mkdir(parents=True, exist_ok=True)
    channels, numbers = set(), set()
    n = 0
    with open(out / "prompts.jsonl", "w", encoding="utf-8") as fh:
        for run in runs:
            checks = {}
            for p in sorted((run / "rollouts").glob("*.jsonl")):
                for line in p.read_text(encoding="utf-8").splitlines():
                    r = json.loads(line)
                    checks[(r["session_id"], r["turn_index"])] = r.get("gen", {}).get("checks") or {}
            for split in ("train", "dev"):
                path = run / "train" / f"{split}.jsonl"
                if not path.exists():
                    continue
                for line in path.read_text(encoding="utf-8").splitlines():
                    e = json.loads(line)
                    m = e["metadata"]
                    src = Sources(e["prompt"])
                    for res in TOOL_RESPONSE.findall(e["prompt"]):
                        for row in rows_of(res):
                            if row.get("channel_name"):
                                channels.add(row["channel_name"])
                            if row.get("channel_number") is not None:
                                numbers.add(str(row["channel_number"]))
                    fh.write(json.dumps({
                        "prompt": e["prompt"], "teacher_completion": e["completion"], "n_tokens": e["n_tokens"],
                        "checks": checks.get((m["session_id"], m["turn_index"]), {}),
                        "kind_expected": "tool_call" if m.get("kind", "").startswith("tool") else "text",
                        "listing": m.get("template") in LISTING_TEMPLATES and m.get("kind") == "text" and bool(src.last_rows),
                        "split": split, "run": run.name, "template": m.get("template"), "source": m.get("source"),
                        "session_id": m["session_id"], "turn_index": m["turn_index"], "step": m.get("step"),
                    }, ensure_ascii=False) + "\n")
                    n += 1
    aliases = []
    tpl = Path(__file__).resolve().parent / "sft" / "templates.json"
    if tpl.exists():
        aliases = sorted({a for v in json.loads(tpl.read_text(encoding="utf-8")).get("aliases", {}).values() for a in v})
    (out / "vocab.json").write_text(json.dumps({"channels": sorted(channels), "numbers": sorted(numbers), "aliases": aliases},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{n} prompts → {out}/prompts.jsonl; vocab {len(channels)} channels, {len(numbers)} numbers, {len(aliases)} aliases")


# ---- validation against an sft_eval results file --------------------------------------------------------
def score_eval(path: Path, vocab: Vocab | None, show: int) -> None:
    """Components for the teacher's completions and each model's, side by side."""
    d = json.loads(path.read_text(encoding="utf-8"))
    examples = d["examples"]
    series = {"teacher": [ex["completion"] for ex in examples]}
    for m in d["models"]:
        series[m["alias"]] = [r.get("text") or "" for r in d["results"][m["alias"]]]
    extras_for = []
    for ex in examples:
        meta = ex.get("metadata", {})
        src = Sources(ex["prompt"])
        extras_for.append({"checks": {}, "teacher_completion": ex["completion"],
                           "kind_expected": "tool_call" if meta.get("kind", "").startswith("tool") else "text",
                           "listing": meta.get("template") in LISTING_TEMPLATES and meta.get("kind") == "text" and bool(src.last_rows)})
    table = {}
    worst = defaultdict(list)
    for name, comps in series.items():
        acc = defaultdict(list)
        for ex, extra, comp_text in zip(examples, extras_for, comps):
            total, c = reward(ex["prompt"], comp_text, extra, vocab)
            acc["total"].append(total)
            for k in ("format", "grounding", "coverage", "fabrication", "length", "kind"):
                if c.get(k) is not None:
                    acc[k].append(c[k])
            if c.get("grounding") is not None and c["grounding"] < 1:
                worst[name].append((c["grounding"], USER_TURN.findall(ex["prompt"])[-1].strip()[:60],
                                    split_completion(comp_text)[1][:110], c["grounding_detail"]["ungrounded"]))
        table[name] = {k: (round(statistics.mean(v), 3), len(v)) for k, v in acc.items()}
    keys = ["total", "format", "grounding", "coverage", "fabrication", "length", "kind"]
    print("component".ljust(14) + "".join(n.rjust(22) for n in table))
    for k in keys:
        print(k.ljust(14) + "".join(f"{table[n][k][0]} ({table[n][k][1]})".rjust(22) if k in table[n] else "-".rjust(22) for n in table))
    for name in table:
        if not worst[name]:
            continue
        print(f"\nlowest grounding, {name} (first {show}):")
        for g, req, text, bad in sorted(worst[name])[:show]:
            print(f"  {g:.2f}  {req!r}\n        {text!r}\n        ungrounded: { {k: v for k, v in bad.items() if v} }")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="prompts.jsonl + vocab.json from prepared runs")
    b.add_argument("--runs", type=Path, nargs="+", required=True)
    b.add_argument("--out", type=Path, default=Path.home() / "news" / "voice-sft" / "rl")
    s = sub.add_parser("score-eval", help="components over an sft_eval results file: teacher vs models")
    s.add_argument("results", type=Path)
    s.add_argument("--vocab", type=Path)
    s.add_argument("--show", type=int, default=8)
    args = ap.parse_args()
    if args.cmd == "build":
        build(args.runs, args.out)
    else:
        score_eval(args.results, Vocab.load(args.vocab) if args.vocab else None, args.show)


if __name__ == "__main__":
    main()
