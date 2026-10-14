# SFT dataset generation for the local brain

`sft_gen.py` + `sft/templates.json`. Produces teacher trajectories for
supervised fine-tuning of the laptop model (Qwen3.5-9B) from generated
traffic, through the real agent against a dry-run TV, rejection-sampled with
the eval suite's checks. Prepared and first run 2026-10-07 (`run1`, see the
end).

## The pipeline in eight steps (rollouts → sft2, as of 2026-10-13)

1. **Capture** — `rollouts.py` records every brain turn of the live agent in
   the Responses-API shape (`~/news/voice-rollouts/YYYY-MM-DD.jsonl`: the
   full prompt, tool calls and results, spoken text, latency, signals). 328
   real turns since 2026-10-01 plus the transcript log (806 household
   utterances) were the raw material.
2. **Test set first** — `eval/suite.json` + `eval_suite.py`: 67 scenarios /
   92 turns written from the logs and the week's bugs, deterministic checks
   plus a judge, run through the real agent against a dry-run TV and the
   live guide. Baseline 2026-10-07: frontier 97 / 96 / 85 %, base
   Qwen3.5-9B 91 / 82 / 64 % (routed / checks / meets). Its utterances are
   held out of everything that follows.
3. **Generate teacher data** — `sft_gen.py` + `sft/templates.json`: sampled
   traffic (the household's own utterances, single templates, multi-turn
   scenarios; slots from the live lineup and guide; memory notes; a
   per-conversation screen state) answered by `gpt-5.6-terra` through the
   same agent, rejection-sampled with the suite's checks and generic
   filters (format, claimed actions without a call, length); `--recheck`
   re-filters offline, `--retry-errors` regenerates outages. run1: 2,275
   turns kept; run2-general: 988; run2 (focused on schedule follow-ups and
   catch-up, 14 new templates via `--focus`): 955.
4. **Prepare** — `sft_train_prep.py`: one prompt/completion pair per
   assistant step, rendered with the chat template llama-server actually
   serves (`sft/chat_template.qwen3.5-served.jinja`, verified byte-identical
   against its `/apply-template`), split by conversation, every step
   checked (prefix stability, think block, tool-call XML, token boundary,
   length cap). Two conventions learned the hard way: history calls keep
   Python-style literals as llama.cpp renders them; completions carry JSON
   booleans and omit nulls (BUGLIST T8). run1 → 4,579 examples; the three
   runs → 6,756 train / 367 dev at 12,288 tokens.
5. **Train** — `sft_train.py` on the laptop's RTX 5000 Ada 16 GB: Unsloth
   QLoRA, r 32 on the language model's projections including the 24
   linear-attention layers, one epoch, lr 2e-4, effective batch 8; no
   in-loop eval (the 248k-vocab logits OOM), so a checkpoint every 200
   steps and `--pick-best-only` scores them afterwards with
   `logits_to_keep`, then merges the best into bf16. sft1: run1, 517 steps,
   8.5 h, dev loss flat at 0.134. sft2: all three runs, ~14 h.
6. **Serve** — llama.cpp `convert_hf_to_gguf.py --outtype q8_0 --no-nextn`
   (Qwen3.5's MTP head would otherwise break the file), llama-server with
   the base's flags, alias `qwen3.5-9b-sft2` on `:8002`.
7. **Gates** — `sft_eval.py` (held-out dev steps against the teacher: kind,
   tool names, args, forbidden patterns, length, optional judge),
   `model_bench.py` (27-question smoke), `eval_suite.py` with the frontier
   in the same run. run1 dev names / args: base 58.5 / 29.7 → sft1 78.0 /
   55.9 → sft2 84.7 / 66.1; run2 dev: sft1 73.5 / 42.9 → sft2 91.8 / 49.0;
   suite: base 91 / 82 / 64 → sft1 97 / 93 / 78 → sft2 98 / 96 / 77 against
   the frontier's 98 / 97 / 86, schedule meets 58 → 92.
8. **Promote** — `.env`: `LOCAL_LLM_MODEL=qwen3.5-9b-sft2`,
   `LOCAL_LLM_BASE_URL` to `:8002`; `VOICE_LLM=local` puts it in front of
   the household (where it is now, for testing). The latency work that
   followed (static system prompt, context block, tail digests, lean rows)
   changed the prompt layout, so run3 and sft3 regenerate under it before
   RLVR.

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

## run2 — the targeted data (2026-10-09)

Two runs came out of it, because the first attempt's shard subprocesses
rebuilt their plan without `--focus` (fixed the same day: the shards now
get the flag):

- **run2-general** — 800 conversations, seed 2, the ordinary mix (318 log,
  358 single, 124 multi) under the screen policy: 988 turns kept, 20
  rejected. Prepared: 1,686 train / 93 dev examples, 16.5 M tokens, 89
  over the 16k cap. Not what was ordered, but new coverage of the whole
  distribution on a different day's guide, so it trains too. sft1 on its
  dev split (a second held-out sample): kind 96.8 %, tool names 89.4 %,
  args 61.7 %, exact 39.8 %, length ×1.0, forbidden 0, hygiene rows 0 —
  a touch above its run1 numbers.
- **run2** — 600 conversations, seed 3, `--focus` on the 19 templates for
  schedule follow-ups over long results and catch-up turns (14 new in
  `sft/templates.json`: `channel_lineup`, `whats_after_title`, `ends_at`,
  `catchup_browse`, `catchup_title`, `catchup_news`,
  `m_channel_evening_followups`, `m_genre_tonight_pick`,
  `m_title_then_after`, `m_now_then_next`, `m_time_slot_channel`,
  `m_catchup_pick`, `m_catchup_title_play`, `m_missed`; the catch-up ones
  draw `{bcast_channel}`, a broadcast channel, since FAST channels have
  no recordings). 400 single / 200 multi, no log utterances; **955 turns
  kept, 26 rejected** (routing 15, nine `channel_lineup` answers over 400
  characters, two claimed actions, one API error dropped). Prepared:
  **1,686 train / 96 dev examples**, 18.4 M tokens, prompts p50 8.9k and
  p90 13.8k tokens (the follow-ups carry more history and results), 111
  over the 16k cap; 671 two-step and 115 three-step turns. **sft1 on its
  dev split — the baseline for the targeted categories:** kind 95.8 %,
  tool names **73.5 %**, args **42.9 %**, exact 22.9 %, text answered
  93.6 %, length ×1.0, forbidden 0, hygiene rows 0, latency 4.6 / 8.8 s
  (longer prompts). Lower than on the other two dev splits (names 78 and
  89 %, args 56 and 62 %), which is the point: a follow-up "what about on
  channel 5?" answered with `whats_on_now` instead of the evening's range,
  "put that on" for a programme airing later tuned the channel instead of
  saying it has not started, `group_by` and a genre parameter dropped, a
  time-slot window placed differently from the teacher's.

**sft2's training set** is the three runs together (`sft_train.py --run
run1 run2-general run2`): at the 12,288 cap **6,756 train / 367 dev
examples, 57.9 M tokens, 844 steps an epoch** — about 14 hours on the
laptop. The RL prompt set rebuilt over the three runs holds 8,140 prompts
(6,405 train / 346 dev under 10k tokens); the teacher scores grounding
0.97, fabrication 0.021, routing 0.969, coverage 0.71 on its dev part.

## sft2 — trained on the three runs; the gates (2026-10-09 evening)

One epoch of QLoRA from the base over run1 + run2-general + run2 (6,756
examples at 12,288 tokens; trained on the laptop, gate 1 passed there),
served as `qwen3.5-9b-sft2` (Q8_0) on `:8002`. Gates 1 and 2 from nucbox
the same evening; gate 3 the next morning (2026-10-10 04:12, 0 errors,
0 guide timeouts) after the guide's tunnel had lost its `172.23.0.0/16`
route the evening before — every guide call timed out, for the suite and
for the household agent alike — and was brought back through `tun0`.

| Gate | metric | sft1 | **sft2** |
| --- | --- | --- | --- |
| 1 run1 dev (230 steps, `sft-eval-20261009-2313.json`) | kind accuracy | 93.5 % | 94.3 % |
| | tool names / args | 78.0 / 55.9 % | **84.7 / 66.1 %** |
| | exact match | 33.5 % | **39.1 %** |
| | text: answered as text / forbidden / length | 98.2 % / 0 / ×0.99 | 95.5 % / 0 / ×1.0 |
| | hygiene rows | all 0 | all 0 |
| 1 run2 dev (96 steps, the targeted categories, `sft-eval-20261009-2320.json`) | kind accuracy | 95.8 % | **99.0 %** |
| | tool names / args | 73.5 / 42.9 % | **91.8 / 49.0 %** |
| | exact match | 22.9 % | 26.0 % |
| | text: answered as text / forbidden / length | 93.6 % / 0 / ×1.0 | **97.9 %** / 0 / ×0.99 |
| | hygiene rows | all 0 | all 0 |
| 2 `model_bench` (27, `20261009-2311.json`) | routed | 100 % | 100 % |
| | faithful / helpful / voice | 4.52 / 4.41 / 4.78 | **5.00 / 4.52 / 5.00** |
| 3 `eval_suite` (92 turns, `suite-20261010-0412.json`) | routed / checks / meets | 97 / 93 / 78 % | **98 / 96 / 77 %** (frontier in the same run 98 / 97 / 86; base 91 / 82 / 64) |
| | faithful / helpful / voice | 4.62 / 4.26 / 4.73 | **4.63 / 4.34 / 4.86** (frontier 4.78 / 4.51 / 4.91) |
| | first text p50 / p90 | 2.7 / 5.9 s | 2.7 / 5.9 s (frontier 2.1 / 2.8 s) |

Reading it: the targeted data did what it was for — on the follow-up and
catch-up split the tool choice went from 73.5 to 91.8 % and the
tool-vs-text decision to 99 %, and it lifted the general split too
(names +7, args +10). Arguments on the targeted split moved less
(42.9 → 49.0 %); the remaining differences there are mostly where a
time-slot window starts relative to the teacher's (the teacher opens the
whole evening for "at nine tonight", the student the hour asked) and a
title's trailing "7AM" dropped — conventions, not wrong calls. Gate 2's
timings were inflated by the guide timeouts of that hour; its grades
stand.

**Gate 3 by category** (checks / meets; sft2 · frontier in the same run ·
sft1 in its own run): schedule **100 / 92 · 100 / 92 · 100 / 58** — the
targeted category reached the frontier; catch-up 86 / 43 · 86 / 57 ·
86 / 43 — unchanged, and the misses are shared with the frontier (D1 and
D3 never say the catch-up plays from the start, D5 is the misheard
"Clay") plus one of sft2's own: D2 restarted with an airing id it never
looked up; on now 100 / 57 · 100 / 100 · 100 / 71 — a 4 AM guide of paid
programming and reruns, where sft2 offered no alternatives (A2),
contradicted itself on "national news" (A5) and called a FAST rerun live
(A6); tuning 92 / 69 · 100 / 85 · 85 / 100 — "turn that channel on" took
the third channel named instead of the first (C8), and "tune on the next
channel" ended in an on-demand title called Next (C10); limits & identity
88 / 62 · 100 / 88 — "Hey Wanda" still unanswered by name (J5, since
sft1), the capability list short of volume and playback (J1); memory
100 / 83 — "what did I tell you last time?" ignored a recall that had the
facts (H3); robustness **90 / 90 · 80 / 60** — sft2 asks when the
frontier acts on a misheard request (K2, K4). Routing and checks are at
the frontier's level (98 / 96 against 98 / 97); the nine points of
"meets" that remain are wording the criteria demand ("from the
beginning", naming itself), reading a long on-now result for the right
framing, and two reference errors.

**What sft2 settles:** the local brain moves from sft1 to sft2
(`LOCAL_LLM_MODEL=qwen3.5-9b-sft2`). The next data should carry the
confirmation wording for catch-up and restart, the identity line, and
restart flows that look the airing up; the rest is the RLVR residue.

## Prompt layout change of 2026-10-10 — what it means for the data

For serving latency on the local brain (runbook, "Serving latency"), the
agent's prompt changed shape: the clock, the on-screen line and the memory
notes now ride in a context block *under* the viewer's words (the words,
a blank line, `[context for this turn — not spoken by the viewer]` …
`[end of context]`), the system prompt is static, each exchange's tool
results are abridged at its commit (the last to eight rows with airing
ids, the others to three), and guide rows are lean (`row_shape
pacific-iso-lean/2026-10-10`: no `kind`/`program_type`/`duration_s`,
description ≤ 120 chars, two genres, 15 rows, a lineup-wide what's-on-now
as overview rows). Consequences:

- run1, run2-general and run2 were generated under the old layout and row
  shape; sft1 and sft2 were trained on them. Served under the new layout,
  sft2 is a cross-layout case: the suite with the frontier in the same run
  (2026-10-10 10:44) gave 96 / 92 / 78 % against 96 / 95 / 83 % — the same
  gap as under the old layout — with first text at 2.2 s, the frontier's.
- `sft_train_prep.py` and `sft_eval.py` work unchanged (they render what
  the rollouts recorded); `sft_eval.last_user` and `rlvr_rewards.Sources`
  strip the block when they show or judge the request, and the block's
  lines count as grounding sources.
- The next generation run (**run3**) produces data in the new layout by
  construction, since the generator uses the agent; **sft3** should be
  trained on run3 (plus the old runs only if their layout is accepted as a
  second format), and the RLVR prompt set rebuilt from run3.

## RLVR: `rlvr_rewards.py` and `sft_rl.py` (written 2026-10-09; first run 2026-10-11 — reward-hacked, not shipped; see the run note at the end)

Reinforcement learning with verifiable rewards on top of the SFT model,
for the residue SFT imitates but does not learn to judge — which tool,
which arguments, when to answer from the result and when to look again,
and not inventing or dropping what a long result holds.

**The trajectory is one step**, the same unit the prep makes: a prompt
exactly as llama.cpp serves it, the turn's tool results already inside,
and one sampled completion. So there is no environment to run: GRPO
samples eight completions per prompt with vLLM, the rewards score each,
and the policy moves toward the better ones within the group.

**The rewards** (`rlvr_rewards.py`, code only, no model):

| component | what it checks | weight |
| --- | --- | --- |
| format | parseable tool-call XML with nothing after it, no `<think>`, no Python literals, spoken part ≤ 400 chars; a failure floors the total | 1.5 |
| routing | as the suite's `routed`: a call step's tool belongs to one of the template's `tools_any_of` sets (a helper such as `search_memory` counts half); a text step needs a set already met by the turn's calls, `[]` meaning no tool at all | 1.0 |
| args | the template's `tool_args` (regex or value within ±0.05) on the step's calls | 1.0 |
| grounding | every title, channel and time the answer speaks occurs in the turn's tool results, the on-screen line, the request or the earlier conversation | 1.0 |
| coverage | a listing answer names two of the first three rows of the result | 0.5 |
| fabrication | specifics, a result or a TV action asserted with no tool result behind them | −1.5 |
| length | near the teacher's; decays past ×1.3 of it | 0.5 |
| kind | the teacher's tool-vs-text decision on the same prompt, a weak prior | 0.25 |

`rlvr_rewards.py build --runs run1 run2 --out ~/news/voice-sft/rl` writes
the prompt set (prompts, teacher completions, checks, the listing flag)
and the channel vocabulary; `score-eval RESULTS.json` runs the components
over an `sft_eval` results file. On run1's dev set the components order
the three sources as a reward should — teacher grounding 0.977 and
fabrication 0.018, sft1 0.986 / 0.016 with length 0.89 and kind 0.94,
the base 0.95 / 0.057 with length 0.52 and kind 0.84 — after three rounds
of fixes to the title extractor (contractions, sentence-initial words,
raw tool text as a source, accents). Coverage is the one place the
teacher does not score near 1 (0.61: it summarises by relevance, not
order), which is why its weight is half.

**The trainer** (`sft_rl.py`): TRL `GRPOTrainer` with the one reward
function (components logged as metrics), vLLM colocated on the training
card or on a second one, a fresh LoRA on the SFT merge's language-model
projections as the policy and a KL (`--beta 0.02`) to it, dapo loss,
temperature 1, 300-token completions, prompts over 10k tokens dropped.
`--check` scores the teacher's completions and builds the config for the
installed TRL (verified here against 1.13); `--dry-run` is two small steps
for the memory. For rented 80 GB cards — the laptop cannot sample eight
8k-token trajectories per prompt at a useful rate. The eval suite and the
dev splits stay held out; the gates after RL are the same three.

## RLVR run 1 — reward-hacked, not shipped (2026-10-11)

Ran on a rented RunPod **H200 141 GB** (the infra is in the runbook's
"RLVR on the rented H200" section, and it worked end-to-end): policy from
sft2's merge, fresh LoRA, `--beta 0.02`, dapo, 8 generations, 300 steps,
~2.1 h, ~$15. **The held-out eval reward climbed the whole way — 2.64 →
3.03 → 3.15 at steps 100/200/300**, with grounding, coverage and length
all reaching ~0.99 and fabrication ~0. It read like a win. It was not.

**The one component that fell told the story: `kind` (teacher's
tool-vs-text decision, weight 0.25) dropped 0.80 → 0.53 → 0.48.** Gate 1
on run1's dev exposed the collapse — ckpt-300 scored **tool_match 0,
args 0, answered_as_text 1.00, kind 0.487**, and 0.487 is exactly
112/230, the text-step fraction. **The policy had learned to answer every
turn as short grounded text and never call a tool.**

Why the reward allowed it: a brief grounded text answer scores 1.0 on
format, ~1.0 on grounding, full on length, 0 fabrication — and the two
components meant to stop it did not. **`routing` has a hole**: a text step
counts as routed if a `tools_any_of` set was *already met by the turn's
calls*, and in these single-step trajectories the tool results are already
in the prompt — so a text answer passes routing even when the teacher's
step was a *further* tool call. That left only **`kind` at weight 0.25**,
with too loose a KL (`--beta 0.02`), opposing the degenerate "always text"
optimum — so GRPO walked straight into it, and the rising *reward* masked a
behavioural collapse. **The gate, not the reward, is truth.** The drift was
progressive (kind already 0.80 at ckpt-100), so every checkpoint is worse
than sft2. Nothing was shipped; **sft2 remains the local brain.**

**Fix before run 2** (reward + schedule, not infra):
- **Make kind/routing a hard gate, not a soft prior.** When the teacher's
  step is a tool call and the policy answers text (or vice versa), *floor*
  the total the way a format failure does — grounding/length must not buy
  it back. Equivalently, the routing text-step rule must require the step
  match the teacher's *kind*, not just that some set was met earlier.
- **Up-weight routing + args, down-weight text-grounding/length** so a
  content-free text answer cannot out-score a correct call.
- **Tighten the KL** (`--beta` ~0.1–0.2) and run **far fewer steps** — the
  drift was well under way by step 100.
- **Use `kind`/tool_match in the in-loop eval as the stop signal**, not the
  aggregate reward (gameable by construction). The runbook R5 already flags
  `kl > 0.2` as a stop sign; this run hit ~1.0 and should have been halted.

**Run 2 — staged (2026-10-11).** All of the above are implemented and
committed (`3828535`): `rlvr_rewards.reward()` floors the total to −1.0 on a
tool-vs-text mismatch (like a format failure); `WEIGHTS` moved to
routing/args 1.5, grounding 0.75, length 0.25; `sft_rl.py` defaults are now
`--beta 0.1` and `--max-steps 150`, with `load_best_model_at_end` on
`eval_reward` + an `EarlyStoppingCallback`. Validated offline with
`score-eval` on run 1's saved results: the collapse drops from ~3.15 (old
reward, top of the chart) to **0.849**, below sft2 (2.079) and the teacher
(2.242) — the exploit is closed and `eval_reward` is a faithful in-loop
signal again. The kit (`~/minny-rl-kit.tgz`) was rebuilt with the fixed
scripts and the existing `rl/` (prompts unchanged — the fix is in scoring,
not prompt-building); `scp` it to the next pod and run R2→R7 as written.

## RLVR run 2 — ran clean, a near-wash at frontier parity (2026-10-11)

Ran on a rented H200 (150 steps, `--beta 0.1`, ~65 min). **The fix held:**
in-loop `kind` stayed ~0.95 and `kl` ~0.01 the whole way (run 1 collapsed to
`kind 0.48`, `kl ~1`); best checkpoint auto-kept at step 100. Merged onto
sft2 (`qwen3.5-9b-rl2`), gated against sft2:

- **Gate 1** (run1 dev, deterministic): rl2 **≥ sft2** — tool names 84.7→**86.4**,
  args 66.1→**66.9**, keys 74.6→**76.3**, kind 94.3→94.8, empties 4.5→3.6,
  hygiene all 0. No regression, small gains.
- **Gate 2** (`model_bench`, 27 Q, judged): rl2 **slightly below** — routed
  100→96 %, faithful 5.00→4.67, helpful 4.52→4.33, voice 5.00→4.78. Within
  27-question judge noise, but not an improvement.
- **Gate 3** (`eval_suite`, 92 turns, `suite-20261011-2053.json`): rl2 **meets
  79 %, matching the frontier in the same run (79 %)** — closing the gap sft2
  had (77 % vs its run's frontier 86 %). Judge 4.55 / 4.24 / 4.82 (frontier
  4.74 / 4.40 / 4.87). **It redistributed by category** (meets, rl2 · frontier):
  catch-up **86 · 71** (sft2 was 43 — the grounding/coverage reward paid off
  exactly where aimed), memory **100 · 67**, limits/identity 88 · 88, volume
  93 · 79 — **up**; schedule **75 · 92** (sft2 was 92), on demand 70 · 90,
  robustness 70 · 70 — **down**. Per-category N is 7–14, so single-scenario
  swings are noise; read the direction, not the decimals.

**Verdict: rl2 ≈ sft2 overall** — a legitimate, safe model (the whole point
vs run 1), at frontier parity, but a *trade* (catch-up fixed, schedule
regressed) rather than a clean win, and a touch lower on the gate-2 judge. So
**sft2 stays the production local brain** unless catch-up reliability is valued
over schedule. The durable win is strategic: **RLVR now provably moves the
targeted category without collapsing.**

## Next: targeted RLVR run 2.5 (sketch)

Run 2 showed the reward *can* lift a weak category (catch-up) but at the cost
of others (schedule) — because the reward is applied *uniformly* across all
prompts, so the gradient chases whatever is cheapest everywhere. Make it
**targeted and non-disruptive:**

1. **Train only where sft2 is weak.** Build the RL prompt set from the
   categories that miss — catch-up, on-now framing, the listing/coverage
   turns — not all 6,405. In `rlvr_rewards.py build`, filter prompts by the
   template/category (the metadata already carries `template`); a few hundred
   focused prompts beat 6,405 uniform ones. The strong categories (schedule,
   tuning) then can't be dragged down because they're not in the training set.
2. **Reward the specific residue, not generic text.** For catch-up, add a
   component that checks the confirmation wording the criteria demand ("plays
   from the beginning") and that a restart looks the airing up — i.e. encode
   the *gate-3 criteria* that sft2 misses, rather than only generic grounding.
3. **Keep the guardrails that worked:** `kind` hard-floor, `--beta 0.1`,
   `--max-steps` ~100–150, stop on `kind`/`kl`. These kept run 2 safe; keep them.
4. **Gate the same three, and require no schedule regression** — promote only
   if catch-up rises *and* schedule/tuning hold. That's the bar run 2 didn't
   quite clear.

Cost is the same ~$10 / ~2 h on a **≥100 GB-container H200** (runbook R1).

## RLVR run 2.5 — ran clean, a wash; **sft2 kept** (2026-10-13)

Ran the targeted plan above, step 1 only (the generic `--templates` filter;
**not** step 2 — a new blind reward component risked another hack, so it was
deferred). Focused set = the catch-up + on-now families, 1,803 train / 96 dev
(1,445 / 81 after the 10k-token drop); schedule, tuning and volume excluded.
Guardrails as in run 2 (`kind` floor, `--beta 0.1`, `--max-steps 120`,
early-stop on `eval_reward`). Infra all held: 105.78 GB peak on the H200, clean
finish, GGUF `--no-nextn` (block_count 32 / 427 tensors), served template
identical. Merged onto sft2 → `~/minny-rl/rl25/qwen3.5-9b-rl25-Q8_0.gguf`.

**The run barely moved off sft2.** In-loop `kl` sat at **0.006–0.014** and
`frac_reward_zero_std` ~0.81 the whole way — the focused prompts were already
near-solved in sampling, so GRPO had almost no gradient. `eval_reward` ~2.05,
`kind` ~0.995 throughout (no hack — the floor held perfectly).

Gate 1 on run2's dev (96 ex), rl25 vs sft2 on the **identical examples**
(`~/Downloads/sft-eval-20261013-1254.json` vs
`~/news/model-bench/sft-eval-20261013-1303.json`):

| family | tool steps | names sft2→rl25 | args sft2→rl25 |
| --- | --- | --- | --- |
| catch-up (targeted) | 19 | −1 | **−2** |
| on-now (targeted) | 2 | +0 | +0 |
| schedule/other (not trained) | 28 | −1 | −2 |
| **aggregate** | 49 | 45→43 (91.8→87.8 %) | 24→20 (49.0→40.8 %) |
| kind | — | 0.990 → **0.990** | |

Every per-template change is a single-example flip on a tiny denominator —
noise, not signal. The one thing that is *not* noise: **the targeted
categories did not rise** (catch-up −1/−2). No schedule blow-up either (the
floor + tiny KL protected it), but **no gain to bank**. Fails the promote
rule's "weak categories must rise", so **rl25 was not promoted — sft2 remains
the brain** (restored on laptop `:8002`). rl25's gguf/adapter are kept under
`~/minny-rl/rl25/` for reference, not served.

(Note: gate 3 for rl25 was not completed — the first `model_bench` run
accidentally hit sft2 during a server swap. Given `kl` ~0.01, gate 3 would
near-certainly be a wash too; not worth the rerun.)

**Lesson for a run 2.6, if attempted:** the focused set was *too easy* — a near-
zero KL means there was nothing to learn. Make it harder, not softer: keep only
the prompts sft2 actually gets *wrong* (filter the focused set by sft2's own
misses, not just by category), and give it room to move (`--lr` up, not
`--beta` up). Without gradient there is no RL, however safe the guardrails.
