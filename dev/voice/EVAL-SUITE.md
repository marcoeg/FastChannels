# Minny voice agent — eval suite (frontier vs local model)

`eval/suite.json` + `eval_suite.py`. Compares brain models on the requests
this household actually makes, through the real agent: prompts, tools,
lineup scoping and the dry-run TV of `model_bench.py`, plus multi-turn
history, a fake screen that follows the tunes, injected memory notes,
deterministic checks and a judge that sees the scenario.

Prepared 2026-10-07 from the logs; **not yet run.**

## Where the cases come from

| Source | What it contributed |
| --- | --- |
| `~/news/voice-conversations.jsonl` — 806 household utterances since 2026-08-29 (satellite 519, phone 207, console 80) | The weighting (what's-on and schedule questions lead, then wake/identity, tuning, on demand, volume) and verbatim phrasings, including the garbled ones |
| `~/news/voice-rollouts/` — 328 traced turns since 2026-10-01 | Expected tool routing per request shape (tv_watch_channel 74, search_live_schedule 53, whats_on_now 49, tv_watch_title 27 …), the frontier's latency to beat (first speech median 2.7 s, p90 4.5 s), and the misheard utterances (`Tune on trues`, `Clay yesterday's new hours`, `Two non-press`, `Tr3s Latina`) |
| `TEST-PLAN.md` / `TEST-SCRIPT.md` — sessions 1–8 run on 2026-10-05 | The A–L test ids and their pass criteria |
| `BUGLIST.md` — T1–T7, #1 | Regression cases for every bug fixed this week (T3 ordinals, T4 memory, T5 "tonight", T6 KPIX evening, T7 NewsHour spelling) |

67 scenarios, 92 turns, in ten categories (13 multi-turn, 5 with memory notes): on now, schedule, tuning,
catch-up, on demand, volume & playback, memory, programme info, limits &
identity, robustness. Each scenario carries its `source`.

## What a scenario looks like

```json
{"id": "E2", "category": "on demand", "source": "BUGLIST T3 …",
 "memory": {"profile": ["…"], "recent": ["…"]},          // optional notes injected per turn
 "player": {"volume": 0.5}, "screen": {...},             // optional dry-run TV start state
 "volatile": true,                                       // depends on the live guide
 "turns": [
   {"say": "Any comedies on demand?",
    "tools_any_of": [["tv_on_demand"]],                  // any listed set, fully called, passes; [] = no tools
    "tool_args": [{"tool": "tv_volume", "args": {"change": "set", "level": 0.2}}],   // or {"tool","arg","regex"}
    "answer_match": "(?i)which", "answer_no_match": "\\bclay\\b",
    "max_total_ms": 8000,
    "criteria": "Plays the SECOND title the assistant NAMED in its previous answer …"}]}
```

`defaults.answer_no_match` applies to every turn (no UTC, URLs, ids,
asterisks, "as an AI"). Numeric `args` match within ±0.05.

## Scoring, per turn and per model

1. **Deterministic checks** — routing (`tools_any_of`), tool arguments,
   answer patterns, a latency budget. Cheap, exact, and the first thing to
   read for a local model: a model that calls the wrong tool or the right
   tool with wrong arguments fails here regardless of how it sounds.
2. **Judge** (`claude-opus-5-5` by default, as in `model_bench.py`) — gets the
   request, the earlier turns of the scenario, the memory notes, the clock,
   the on-screen line, every tool call with its (compacted) result, the
   spoken answer and the scenario's criteria; returns `meets` (the criteria,
   strictly) and `faithful` / `helpful` / `voice` 1–5. It is told that
   schedules change and to fault only contradictions and inventions.
3. **Latency** — ms to first text and per turn, p50/p90. The production
   reference is the frontier's 2.7 s / 4.5 s first speech.

The report prints per model: turns, errors, routed %, all-checks %,
meets %, the three means, latency; then per category; then every turn where
the models' `meets` verdicts differ, with both answers. The JSON keeps
everything (calls, outputs, grades) for a closer look.

## Running it

Both models must be reachable from this box:

- the frontier through `OPENAI_API_KEY` (`VOICE_MODEL`, today `gpt-5.6-terra`);
- the local model through the laptop's OpenAI-compatible server at
  `LOCAL_LLM_BASE_URL` / `LOCAL_LLM_API_KEY` (`.env`: `http://192.168.50.71:8001/v1`,
  `LOCAL_LLM_MODEL=qwen3.5-9b`). Any name starting with `qwen` is routed there
  with the production local settings (chat completions, thinking off,
  parallel tool calls, 700 output tokens). Use the id the server reports.
- the judge through `ANTHROPIC_API_KEY`;
- the Minerva VPN (the guide) and `ytvn-epg` (`python3 serve.py`, page open)
  — the TV's lineup, on-demand and title lookups are read live; commands are
  faked.

```bash
cd dev/voice
./eval_suite.py --check                       # validates the suite, no model calls
./eval_suite.py --list
./eval_suite.py                               # VOICE_MODEL vs LOCAL_LLM_MODEL, 1 run
./eval_suite.py --models gpt-5.6-terra,qwen3.5-9b --runs 2
./eval_suite.py --category "tuning,volume & playback" --no-judge   # the exact checks only
./eval_suite.py --report ~/news/model-bench/suite-20261007-1200.json
```

Cost and time: 92 turns per model per run. The frontier takes ~3–6 s a
turn; the local model whatever the laptop gives (the production worker's
local path is the same code). One run of both plus judging is of the order
of 20–40 minutes and ~2 M judge tokens. A model that can't be reached is
skipped after a "Say OK." probe.

## Reading the results honestly

- **Volatile scenarios** (marked; 29 of 67) depend on the live schedule and
  can only be compared between models run at the same moment — the runner
  interleaves the models per scenario for that reason. Don't compare their
  `meets` across days; compare the two models within a run.
- **Weekday / weekend**: B8 (PBS News Hour), D1 (Daniel Tiger), D3/D4 state
  what passes on either kind of day; a Sunday run tests only the
  "not available" branch of those.
- **The dry-run TV** fakes success for every command. Tunes, plays and
  volume are checked by their arguments, not by the screen. The fake player
  starts at 50 % (the test plan's convention), steps 10 %, "up" unmutes,
  and `play` after `stop` fails as `not_ready` so G1's last turn can test
  the no-retry behaviour.
- **Memory** writes are off (`VoiceBrain(None)`): H4/H5 judge only the
  spoken confirmation. Memory USE is tested through injected notes
  (`memory.profile` / `memory.recent`), which is what the worker gives the
  model at recall time.
- **Closing acknowledgements** (K6) reach the model here; in the worker a
  code shortcut answers "Great."/"Thanks!" before the model sees them. K6
  still tells you whether a model keeps those short.
- **STT, TTS, barge-in and the satellite's half-duplex timing are out of
  scope**: this suite measures the brain. The misheard phrasings in the
  robustness category are included as text, which is what the brain gets.
- A single run is one sample. `--runs 2` or `3` shows the variance; the
  frontier's own replays this week moved individual turns between runs.

## Adding a case

Take the user text verbatim from a rollout or the transcript log, decide
the acceptable tool sets from how the fixed agent routes it (the rollout
shows the calls), write the criteria as the test plan does (what a correct
answer contains, what it must not contain), add `source`. Keep ids stable;
the disagreements list is keyed by them. Mark `volatile` whenever the
answer depends on the day's guide.
