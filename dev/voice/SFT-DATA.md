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

## Training: `sft_train.py` (for the laptop; not yet run)

The step-by-step on the laptop — environment, data transfer, dry run,
training, merge, GGUF, serving on `:8002`, the three gates, promotion —
is `SFT-RUNBOOK.md`.

QLoRA by default — the 4-bit base is ~5 GB, which leaves a laptop GPU
room for the 16k-token guide-result examples; `--bf16-lora` for a 24 GB+
card. Unsloth (`FastModel`) + TRL `SFTTrainer`, or `--no-unsloth` for
transformers + peft + bitsandbytes. Prompt-completion format with
`completion_only_loss`; no chat template is applied (the prep rendered it).

```bash
./sft_train.py --check                       # data + tokenizer agreement, the step plan; no model
./sft_train.py --dry-run                     # loads the model, two steps, prints peak GPU memory
./sft_train.py --out ~/minny-sft/sft1        # 2 epochs, lr 2e-4 cosine, r 32, effective batch 8
./sft_train.py --merge-only ~/minny-sft/sft1/adapter --out ~/minny-sft/sft1
```

- **LoRA targets** (`TARGET_MODULES`, a regex over module names): Qwen3.5-9B
  is `Qwen3_5ForConditionalGeneration` — a vision tower and a 32-layer text
  model of 24 gated-delta *linear attention* layers (`in_proj_qkv`,
  `in_proj_z`, `out_proj`) and 8 full-attention layers (`q/k/v/o_proj`),
  MLPs in all. The adapter goes on those projections inside
  `model.language_model` only; vision tower and MTP head stay frozen. The
  stock "q,k,v,o,gate,up,down" list would miss three quarters of the
  attention.
- **Hybrid kernels:** install `flash-linear-attention` and `causal-conv1d`
  on the laptop or the linear-attention layers train on slow fallbacks.
- A checkpoint every `--eval-steps` (200), `--keep` of them kept; **no
  dev loss during the run** — an in-loop eval materialises the
  `[1, seq, 248k]` logit tensor in fp32 (~8 GB) and OOMs a 16 GB card.
  `--pick-best` scores the checkpoints afterwards on 60 dev examples with
  the LM head run on the completion span only (`logits_to_keep`) and
  picks the lowest loss (`pick-best.json`); `--pick-best-only OUT` reruns
  that without training. `adapter/` and `train-summary.json` (args, data
  stats, trainable parameters, peak memory, step times, log history) go
  to `--out`. `--resume DIR` continues from a checkpoint.
- **Merge** applies the adapter to a fresh bf16 base (never to the
  dequantised 4-bit weights it was trained against) and writes `merged/`
  with the tokenizer, chat template and preprocessor files, on the CPU by
  default (~20 GB RAM). Then llama.cpp: `convert_hf_to_gguf.py merged
  --outtype q8_0`, `llama-server --alias qwen3.5-9b-sft1 --jinja -c 32768
  -np 4`, and the eval suite with all three models in one run.
- On a 16 GB card that runs out of memory: `--max-tokens 12288` (drops 230
  of the 4,579 examples, the longest guide-result answers).
- `--check` was run here on run1 (2026-10-07; a tokenizer, no model): all
  4,349 + 230 examples usable, 200 of 200 sampled tokenise exactly as the
  prep counted, eos is `<|im_end|>`; 38.0 M training tokens an epoch, 543
  optimiser steps an epoch, 1,087 for the two, five evaluations.

## Evaluation: `sft_eval.py` (not yet run)

The cheap, deterministic gate between training and the agent-level
suites: the served model gets each dev example's prompt as raw text
through llama-server's `/completion` (temperature 0, the same prompt the
teacher answered, every tool result already inside), and its continuation
is compared with the teacher's step. No guide, no TV, no judge needed.

```bash
./sft_eval.py --models qwen3.5-9b                                  # the base, as the baseline
./sft_eval.py --models qwen3.5-9b@http://192.168.50.71:8001,qwen3.5-9b-sft1@http://192.168.50.71:8002
./sft_eval.py --models qwen3.5-9b-sft1 --judge --show 20           # + claude on the text steps
./sft_eval.py --report ~/news/model-bench/sft-eval-<stamp>.json
```

Per step: **kind** (tool call vs spoken text), **tool names** in order,
**args** (keys and values, literals normalised so `False`/`false`,
`0.5`/`0.50` and case don't count), text steps against the suite's
forbidden patterns, length relative to the teacher's, truncation, a leaked
`<think>` block; hygiene counters for Python-style literals (T8) and
malformed tool-call XML; prompt and generation tokens/s from the server's
timings. `--judge` asks `claude-opus-5-5` whether a text answer conveys
the same facts as the teacher's with nothing invented. The report is one
table per model side by side, then the last model's misses with the
request, the teacher's step and the model's. Results in
`~/news/model-bench/sft-eval-<stamp>.json`. It first checks that the
server parses `<|im_start|>` as one token (special tokens on), and skips
a model it cannot reach.

Two requests at a time (`--workers 2`): the laptop's llama-server has a
32k context shared by its four slots, and four of these ~8k prompts in
flight overflow it — every request then fails with "Context size has been
exceeded" (the first baseline attempt lost 228 of 230 steps that way).
The production agent sends one request at a time and never meets this.
Decoding also slows while another slot is processing a prompt, so the
latencies in this report are under two concurrent requests.

**Baseline, the base model (2026-10-07 13:00, `sft-eval-20261007-1300.json`,
230 steps, 0 errors):** kind accuracy **83.5 %**, exact match 10.9 %;
tool steps (118): names **58.5 %**, args **29.7 %**, malformed 0, Python
literals 0.8 %; text steps (112): answered as text 95.5 %, forbidden
patterns 3.6 %, length ×1.61 the teacher's at the median; truncated 0,
think leak 0; latency p50 3.2 s / p90 6.6 s under two concurrent
requests (prompt 500 tok/s, generation 16 tok/s). What the misses show,
and what SFT has to fix: window values the tool does not have ("tonight",
"today" for a range with start/end), `group_by` dropped, `channel: 12`
for "channel 12", a title searched on the tuned channel with
`whats_on_now` instead of the guide, a channel id from a tool result
reused as a channel name, "KTVU channel 2 is currently showing a
programme" with no call at all, markdown asterisks around titles, and
answers half again as long as the teacher's.

The order of the gates: `sft_eval.py` on the dev split (did training do
what the data asked, and what did it break), `model_bench.py` as the
smoke test on the served GGUF, then `eval_suite.py` with the frontier,
the base and the fine-tune in one run (the live guide, the agent, the
judge). Regenerate rather than reuse across lineup changes, since every
system prompt carries the lineup and the clock of the day it was made.
The training settings are in the fine-tuning plan doc (Stage 1, "The SFT
run on the laptop").

## sft1 — the first fine-tune and its gates (2026-10-09)

One epoch of QLoRA on run1 (4,133 examples at 12,288 tokens, 517 steps,
~8.5 h on the laptop's RTX 5000 Ada 16 GB; the last checkpoint won the
post-hoc dev-loss ranking at 0.134; details in `SFT-RUNBOOK.md`), merged
and served as `qwen3.5-9b-sft1` (Q8_0, 8.9 GB) on `:8002`. All three
gates run the same morning; the base's figures are its baselines (gate 1
and 2 from their own runs, gate 3 from 2026-10-07 06:17 — a different
day, so its volatile scenarios are not strictly comparable).

| Gate | metric | base | **sft1** | frontier |
| --- | --- | --- | --- | --- |
| 1 `sft_eval` (230 dev steps, `sft-eval-20261009-0630.json`) | kind accuracy | 83.5 % | **93.5 %** | — |
| | tool names / args | 58.5 / 29.7 % | **78.0 / 55.9 %** | — |
| | exact match | 10.9 % | **33.5 %** | — |
| | text: forbidden / length vs teacher | 3.6 % / ×1.61 | **0 / ×0.99** | — |
| | judge same facts (110 text steps) | — | **79 %** | — |
| | malformed XML / Python literals / truncated / think leak | 0 / 0.8 % / 0 / 0 | **0 / 0 / 0 / 0** | — |
| | latency p50 / p90 (two concurrent) | 3.2 / 6.6 s | **2.6 / 6.5 s** | — |
| 2 `model_bench` (27 questions, `20261009-0639.json`) | routed | 88 % (2026-10-02) | **100 %** | 100 % |
| | faithful / helpful / voice | 4.08 / 3.96 / 4.42 | **4.52 / 4.41 / 4.78** | ~4.9 / 4.6 / 4.95 |
| | first text p50 | 2.7 s | 3.1 s | 2.3 s |
| 3 `eval_suite` (92 turns, `suite-20261009-0651.json`) | routed / checks / meets | 91 / 82 / 64 % | **97 / 93 / 78 %** | 97 / 96 / 86 % |
| | faithful / helpful / voice | 4.14 / 3.84 / 4.27 | **4.62 / 4.26 / 4.73** | 4.77 / 4.53 / 4.89 |
| | first text p50 / p90 | 2.5 s / — | 2.7 / 5.9 s | 2.3 / 2.8 s |

Reading it: routing now equals the frontier's; the tool arguments and
the spoken form (no markdown, the teacher's length, JSON literals, no
"None" channels) are what one epoch bought. Where the fine-tune still
reads as a smaller model is the long guide result: suite categories
catch-up (meets 43 % against the frontier's 71 %) and schedule (58 %
against 92 %) — a follow-on programme misread off a multi-airing result,
dish names paraphrased where the titles were asked for, an on-demand
check skipped — and gate 1's 23 judge disagreements are the same thing
on the dev set (dropped newscasts and channels, a swapped channel name).
Tuning (100 %), memory (100 %) and robustness (80 %) are at or above the
frontier's in this run. Two misses of a different kind: "Hey Wanda" was
answered without the name (the identity rule), and a volume step landed
at the wrong level.

What to do with it: as the **local** brain it replaces the base
outright (`LOCAL_LLM_MODEL=qwen3.5-9b-sft1`); the household brain stays
the frontier. The next gain is not a second epoch over the same data —
the dev loss was flat over the last hundred steps — but more examples
where it is weak: schedule follow-ups over long results and catch-up
turns, generated with the same pipeline, then a run2.
