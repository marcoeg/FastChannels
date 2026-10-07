# SFT dataset generation for the local brain

`sft_gen.py` + `sft/templates.json`. Produces teacher trajectories for
supervised fine-tuning of the laptop model (Qwen3.5-9B) from generated
traffic, through the real agent against a dry-run TV, rejection-sampled with
the eval suite's checks. Prepared and first run 2026-10-07 (`run1`, see the
end).

## What it writes

In `--out` (default `~/news/voice-sft/<stamp>/`):

| File | Contents |
| --- | --- |
| `traffic.jsonl` | The sampled conversations before any model call: id, source, template, slots, memory notes, turns with their resolved checks |
| `rollouts/shard-*.jsonl` | Kept turns in the production rollout shape (`minny-rollout/1`, built by the agent's own `rollouts.build_record`), one line per turn, with `synthetic: true` and a `gen` block (conversation, template, slots, checks, failures = []) |
| `rejected/shard-*.jsonl` | The turns that failed a check, same shape, `gen.fails` naming the checks |
| `sft_chat/shard-*.jsonl` | Kept turns as chat messages: `system` (the full production prompt of that turn), `user`, `assistant` with `tool_calls`, `tool` results, the final `assistant` text; `tools` in the chat function shape; `metadata` |
| `summary-<shard>.json` | Counts: kept, rejected, failures by kind, skipped (already done) |

Because the kept rollouts are the production shape, `rollouts_export.py`
reads them unchanged for the NeMo Gym files (point it at the `rollouts/`
subdirectory, which holds only kept turns):

```bash
VOICE_ROLLOUT_DIR=~/news/voice-sft/run2/rollouts ./rollouts_export.py --out ~/minny-gym-sft
```

(`rollouts.py` reads `VOICE_ROLLOUT_DIR` at import; the exporter drops a
turn whose next utterance reads like a correction, which the multi-turn
"I meant Trace" scenarios trigger on purpose; use `sft_chat.jsonl` when you
want those kept.) One file per shard: `--workers 4` writes `shard-0-4` … `shard-3-4`.

## Where the traffic comes from

`mix` in the templates sets the proportions (default 35 / 45 / 20 %):

- **log** — the household's real utterances from `~/news/voice-conversations.jsonl`
  (806 since 2026-08-29), deduplicated, STT artefacts included, each run as a
  single turn with no history. Context-dependent ones ("the second one")
  then teach the right question back.
- **single** — 36 templates whose phrasings are taken from the log, with
  slots filled at sampling time from live data: channels, numbers and
  network names from the lineup (`aliases` adds "Fox", "CBS", "PBS"…),
  programme titles from what the guide has on now and tonight, on-demand
  titles from the TV's catalogue. Every name in the traffic exists, so the
  teacher's answers are grounded in real tool results.
- **multi** — 12 scenarios of two to five turns with shared slots
  (what's on → turn that on → what am I watching; comedies on demand → the
  second one; a title tonight → what's after that → how long; an ambiguous
  channel prefix → the full name; a misheard channel → "I meant X"; tune →
  volume steps; an action → a closing ack). History is carried as the worker
  carries it.
- **memory notes** — 30 % of conversations get one or two profile facts
  (favourite channel or show, a dislike, normal volume, news channel),
  injected as the worker injects them; templates that need a fact declare
  it (`memory`), so "put the volume back to normal" always has a level to
  go back to.

Multi-turn scenarios that start from a tuned screen (`"screen": "tuned"`)
get the first lineup channel on the fake screen.

## Rejection sampling

A turn is kept only if it passes:

1. its template's checks through `eval_suite.check_turn`: tool routing
   (`tools_any_of`), tool arguments (`tool_args`, with slot regexes that
   accept every spelling of the bound channel, and numeric levels), answer
   patterns; the suite's defaults (no UTC, URLs, ids, asterisks, "as an AI");
2. the generic filters in `sft_gen.generic_fails`: no error or timeout, a
   non-empty answer of at most 400 characters, no "let me check" without a
   tool call, no claimed action (tuned, switched to, now playing, muted,
   paused, volume set to N…) without a `tv_` call — unless negated or
   "already" (a description of the screen, not an action). Information like
   "Hoy día is on now on KSTS" is not an action; the first run's rule said it
   was and rejected 138 good turns;
3. `max_answer_chars` where a template sets it (closings).

The checks can be re-applied to a finished run without model calls:

```bash
./sft_gen.py --recheck ~/news/voice-sft/run1                 # rewrites rollouts/, rejected/, sft_chat/
./sft_gen.py --recheck ~/news/voice-sft/run1 --retry-errors  # also drops errored conversations
```

`--retry-errors` removes every record of a conversation that hit an error
(an API outage, a timeout), so running the generator again with the same
`--out` regenerates exactly those; without it they stay in `rejected/` and
a resume skips them as done. `summary-recheck.json` keeps the counts.

Log utterances carry no template checks, so only the generic filters apply
to them. The judge is not used here (cost); it stays the eval suite's job.

## Held out

Every utterance of `eval/suite.json` is excluded from the traffic by exact
match after normalisation, and `--check` lists the template phrasings that
would collide. Near-paraphrases are allowed on purpose: the suite tests
generalisation, not memorisation of its strings.

## Running

```bash
cd dev/voice
./sft_gen.py --check                       # templates + held-out overlap; offline
./sft_gen.py --plan --n 400                # sample the traffic and print 20 examples; reads only
./sft_gen.py --n 400 --out ~/news/voice-sft/run1
./sft_gen.py --n 2000 --workers 4 --out ~/news/voice-sft/run2
./sft_gen.py --n 2000 --out ~/news/voice-sft/run2   # resumes: conversations already in the output are skipped
```

Needs what the eval suite needs: `OPENAI_API_KEY` (the teacher,
`VOICE_MODEL`), the Minerva VPN (the guide), `ytvn-epg` serving with the
page open (lineup, on-demand and title reads; commands are faked). Workers
are separate processes because the dry-run TV state is per process; each
writes its own files (`shard-i-k`) under the three subdirectories.

Cost and time, rough: 2000 conversations are about 2,800 turns, each
1–2 teacher requests of ~8k mostly cached input tokens and a short output;
with 4 shards about 45 minutes. Expect 15–25 % rejected on the first run;
read `rejected.jsonl` before trusting the rest, since a check that rejects
good turns (a title the guide spells differently) is as likely as a teacher
slip.

## run1 (2026-10-07)

`./sft_gen.py --n 2000 --workers 4 --out ~/news/voice-sft/run1`, 37 minutes
for the part that ran. The teacher's OpenAI account ran out of credits at
16:40 UTC, after 1,268 of the 2,000 conversations; the remaining 732 all
failed with `APIError: You have no credits remaining` and were dropped with
`--recheck --retry-errors`. Of the turns that did run: **1,340 kept, 31
rejected** (routing 15, tool_args 13, answer_no_match 6, action claimed
without a call 2, narrated without a call 1) after the claimed-action rule
was narrowed. The resume for the other 732 conversations, once credits are
back:

```bash
./sft_gen.py --n 2000 --workers 4 --out ~/news/voice-sft/run1
```

## Before training

- Split by conversation id, not by turn, so a scenario's turns stay together.
- Train on `sft_chat/*.jsonl` with the chat template Qwen serves with on the
  laptop and the `tools` field rendered by that template; loss on assistant
  tokens only (tool calls and the final text), not on system, user or tool
  results.
- Keep the eval suite as the test set; `model_bench.py` as the smoke test
  on the served GGUF.
- Regenerate rather than reuse across lineup changes: the system prompt in
  every record carries the lineup and the clock of the day it was made.
