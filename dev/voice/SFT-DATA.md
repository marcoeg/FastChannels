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

**The screen.** Each conversation starts with the TV in a state drawn from
its id (`sft_gen.screen_for`): 70 % tuned to a lineup channel — never the
one the conversation names, so "put on X" is not a no-op — and 30 % the
programme grid with nothing playing, which is what the on-screen line says
when the household's TV is off. Templates whose request acts on what is
playing (`"screen": "tuned"`: watching, about this episode, restart, pause,
resume, stop, mute, volume) always start tuned. (`run1` predates this: the
eval suite's default screen, KTVU channel 2, reached every conversation, so
all of its turns were answered with KTVU on screen. They are consistent
with the prompt the teacher saw; they are just one screen state.)

## Rejection sampling

A turn is kept only if it passes:

1. its template's checks through `eval_suite.check_turn`: tool routing
   (`tools_any_of`), tool arguments (`tool_args`, with slot regexes that
   accept every spelling of the bound channel, and numeric levels), answer
   patterns; the suite's defaults (no UTC, URLs, ids, markdown, "as an AI");
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

A resume re-samples the traffic with the same seed, but the slot values
come from the guide and the log of the moment, so the conversations are
not the same set as the first time: the ones already in the output (same
id = same utterances) are skipped, the rest are generated. It tops the run
up; it does not replay a plan.

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

`./sft_gen.py --n 2000 --workers 4 --out ~/news/voice-sft/run1`, in two
parts. The first ran 37 minutes until the teacher's OpenAI account ran out
of credits at 16:40 UTC, after 1,268 of the 2,000 conversations; the
remaining 732 failed with `APIError: You have no credits remaining` and
were dropped with `--recheck --retry-errors`. The resume (same command,
17:44–17:58 UTC, after credits were added) generated the 870 conversations
of the re-sampled plan that were not in the output yet, with no errors.

**Result: 2,275 turns kept in 2,105 conversations, 37 rejected** (routing
24, tool_args 20, action claimed without a call 3, narrated without a call
1; some turns fail two checks). Sources: 592 log, 1,335 single, 348 multi;
506 turns with memory notes; screen: 1,346 turns with KTVU on screen (the
first part, before the screen policy), 672 tuned to a drawn channel, 257
the grid. Teacher usage for the first part alone was 18.7 M input tokens
(6.2 M cache reads) and 80 k output.

The rejections, read one by one: the FAST channels whose names read like
programmes ("Judge Nosey", "Unleashed by DOGTV", "MODUS SUPER SERIES
DARTS") are tuned with `tv_watch_title` instead of `tv_watch_channel`
(12); "Restart this from the beginning" on a fake screen that carries no
airing is declined instead of looked up (3 — the real screen line carries
the title and airing id, the fake does not); "My normal volume is 0.35"
was executed as a volume command (1); the rest are single cases. Two
checks were wrong and were fixed, with the turns recovered by `--recheck`:
the suite's no-asterisk default rejected every mention of M\*A\*S\*H (16
turns; it now matches markdown only), and the claimed-action rule rejected
information ("X is on now on KSTS", 138 turns).

Export for NeMo Gym, verified on this run: `./rollouts_export.py
--rollouts ~/news/voice-sft/run1/rollouts --out <dir>` (or the
`VOICE_ROLLOUT_DIR` form above).

## Before training: `sft_train_prep.py`

Turns `sft_chat/*.jsonl` into prompt/completion pairs rendered through the
base model's own chat template (`--base Qwen/Qwen3.5-9B`, needs
`transformers` — run here on run1 on 2026-10-07 with
`uv run --no-project --with transformers --with jinja2 python sft_train_prep.py …`,
the laptop's server reachable for the check):

```bash
./sft_train_prep.py --run ~/news/voice-sft/run1                      # → run1/train/
./sft_train_prep.py --run ~/news/voice-sft/run1 --limit 50 --show 3  # a look
./sft_train_prep.py --run ~/news/voice-sft/run1 --server http://127.0.0.1:8001   # + compare with llama.cpp
```

- **One example per assistant step of the record's last turn.** The prompt
  is the conversation up to that step plus the generation prompt with
  thinking off (`<|im_start|>assistant\n<think>\n\n</think>\n\n` on Qwen3.5);
  the completion is what the teacher produced at that step — the XML tool
  call block or the spoken text — ending in `<|im_end|>`. A turn that
  called a tool and answered gives two examples. The loss goes on the
  completion only; the trainer masks the prompt (TRL's prompt-completion
  format does exactly that).
- **Split by conversation** (`metadata.session_id`), 5 % dev by default.
- **What the template needs**, read from the served Qwen3.5 template: tool
  calls are rendered as `<function=…><parameter=…>` XML, and only when
  `arguments` is a mapping — the records carry it as a JSON string, so it
  is parsed first. Scalars go through Jinja's `string` filter, so a boolean
  in a history call reads `False` and a null `None`, in llama.cpp's
  rendering too; the prompts keep that, being what the model is shown.
  The completions differ on purpose: booleans as JSON literals and
  null-valued parameters left out, because Python-style ones reach the
  agent's tools as the strings "False"/"None" (BUGLIST T8) — a channel
  named None — and llama.cpp parses JSON literals back to typed values.
  Render with `--template sft/chat_template.qwen3.5-served.jinja`, the
  template llama-server reports; the tokenizer's differs only in the
  generation-prompt branch.
- **Checks**, per example: the template is prefix-stable; the rendered
  message carries the think block the prompt ends with; the XML block has
  every call with every argument, in order; the completion ends with the
  end token; prompt and completion tokenise the same joined as apart
  (`boundary_ok`); length under `--max-tokens` (12,288 default). Failures
  go to `dropped.jsonl` with the reason. `--server` renders a sample of the
  prompts through the live llama.cpp `/apply-template` and reports any
  difference — the one rendering that matters.
- Outputs in `<run>/train/`: `train.jsonl`, `dev.jsonl`, `dropped.jsonl`,
  `prep-summary.json` (counts, token percentiles and totals, drops by
  reason), `chat_template.jinja`.
- **run1, prepared 2026-10-07:** 2,275 records → **4,579 examples** (4,349
  train / 230 dev from 105 conversations): 2,307 tool-call steps, 2,272
  text steps; turns with 1/2/3/4 steps: 306/1,651/298/20. Tokens per
  example p50 8,353, p90 9,057, max 16,009, 40.0 M in total (prompts;
  completions are 182 k tokens, p50 31). 3 examples dropped over the
  16,384 cap; 0 boundary or think-block mismatches; 150 of 150 sampled
  prompts identical to the laptop's `/apply-template` rendering. 393
  prompts carry Python-style literals in history calls (llama.cpp's
  rendering), the completions none: 285 JSON booleans, nulls omitted.
  `train.jsonl` is 153 MB. The trainer can filter on `n_tokens` if 16 k
  sequences do not fit.

Then: keep the eval suite as the test set, `model_bench.py` as the smoke
test on the served GGUF; regenerate rather than reuse across lineup
changes, since every system prompt carries the lineup and the clock of the
day it was made. The training settings are in the fine-tuning plan doc
(Stage 1, "The SFT run on the laptop").
