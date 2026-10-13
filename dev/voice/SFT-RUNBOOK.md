# Fine-tuning the local brain on the laptop — runbook

The laptop is `192.168.50.71` (llama-server b11340 on `:8001`, alias
`qwen3.5-9b`, four slots sharing a 32k context, API key
`LOCAL_LLM_API_KEY`). Everything below runs there unless marked **nucbox**.
Written 2026-10-07; run through once on 2026-10-08/09 (sft1 — the
corrections and the gate results are at the end). The decision after
sft1 (2026-10-09): stay on the 9B; next a targeted run2 of data where
it is short (schedule follow-ups over long results, catch-up turns),
then RLVR with grounding and coverage rewards — "Next: run2 and RLVR"
at the end.

What you end up with: `~/minny-sft/sft1/qwen3.5-9b-sft1-Q8_0.gguf`, served
as `qwen3.5-9b-sft1`, with three gates passed or failed in writing.

## The words: three models, a set of numbers, two data splits

Three models are in play — and two terms below are *not* models:

- **Teacher** — the frontier model whose answers are the training targets
  and the top-line quality bar: `gpt-5.6-terra`, the household brain. SFT
  trains the student to imitate it, so it is both the label source and the
  reference the fine-tune is measured against.
- **Base** — stock `Qwen/Qwen3.5-9B`, before any tuning, served as
  `qwen3.5-9b` on `:8001`.
- **Fine-tune** (the student) — what this runbook produces,
  `qwen3.5-9b-sft1` on `:8002`.

- **Baseline** is not a third model: it is the *base* model's saved gate
  scores (step 7, 2026-10-07: kind 83.5 %, tool names 58.5 %, args 29.7 %,
  text ×1.61 the teacher's length, forbidden 3.6 %) — the numbers the
  fine-tune has to beat.
- **Held-out** is data, not a model: `dev.jsonl` is kept out of training
  (`train.jsonl` is what the model sees), so scoring on it measures
  generalisation rather than memorisation. The `--pick-best` dev loss and
  gate 1 both evaluate the fine-tune on this held-out set against the
  teacher.

## 0. Before anything: the GPU and the server

```bash
nvidia-smi                      # the card and its memory — this decides --max-tokens
ps -o args= -C llama-server     # keep this line: the flags to reuse in step 6
```

Read on 2026-10-07 20:15: **NVIDIA RTX 5000 Ada Generation Laptop GPU,
16 GB** (16,376 MiB), driver **535.171.04 / CUDA 12.2**, 130 MiB in use
(Xorg) — llama-server not loaded. So:

- QLoRA, and start with `--max-tokens 12288`; the dry run in step 3
  decides. The Unsloth path is required on 16 GB: Qwen3.5's 248k
  vocabulary makes a plain transformers run materialise sequence × vocab
  logits (6–12 GB at 12k tokens); Unsloth's chunked loss does not. The
  embeddings and LM head stay 16-bit under QLoRA (2 GB each), so the fixed
  cost is ~8.5 GB of weights plus ~1 GB adapter and optimiser; the rest is
  activations.
- **The driver (535, CUDA 12.2) is fine as it is** — pin the cu126 build
  of torch (step 1 as it was actually done, in the first-run notes);
  the cu130 wheel uv picks by default is the one that fails on it.
- **The served model holds ~13 GB of VRAM.** Keep llama-server stopped for
  the training hours (the household brain is the frontier; only
  local-model tests lose it) and restart it afterwards with the line you
  kept.

## 1. Code and environment (once)

```bash
cd ~/Development/fastchannels-private && git pull     # sft_train.py, sft_eval.py, sft/, SFT-DATA.md
mkdir -p ~/minny-sft && cd ~/minny-sft
uv venv .venv --python 3.12 && source .venv/bin/activate      # or python -m venv
uv pip install unsloth                                        # torch (CUDA), trl, peft, bitsandbytes, transformers
uv pip install flash-linear-attention causal-conv1d           # Qwen3.5's linear-attention layers; causal-conv1d may compile (needs nvcc)
python - <<'EOF'
import torch, unsloth, trl, peft, bitsandbytes, transformers
from transformers import AutoConfig
print(torch.__version__, torch.cuda.get_device_name(0))
print("transformers", transformers.__version__, "trl", trl.__version__, "peft", peft.__version__)
print("model type:", AutoConfig.from_pretrained("Qwen/Qwen3.5-9B").model_type)   # must say qwen3_5
import fla, causal_conv1d; print("hybrid kernels ok")
EOF
export HF_TOKEN=…            # optional; public model, the token only lifts the Hub's rate limits
```

If `model type` does not print `qwen3_5`, transformers is too old for the
architecture: `uv pip install -U transformers` and check again. If
`import fla` fails the training still runs, slowly, on the fallback
kernels — fine for a dry run, not for the real one.

## 2. The data (once per generation run)

From **nucbox** (161 MB, the household's text stays on the LAN):

```bash
rsync -av ~/news/voice-sft/run1/train/ marco@192.168.50.71:news/voice-sft/run1/train/
```

The scripts default to `--run ~/news/voice-sft/run1`, so the path is
mirrored on purpose. Then, on the laptop, the check that needs no GPU:

```bash
source ~/minny-sft/.venv/bin/activate          # in EVERY shell that runs the scripts: the shebang takes the shell's python3
cd ~/Development/fastchannels-private/dev/voice
./sft_train.py --check
```

Expected (it ran on nucbox with the same data): 4,349 + 230 examples
usable, 200/200 tokenise as the prep counted, eos `<|im_end|>`, 543 steps
an epoch, 1,087 for two.

## 3. Stop the server, dry-run

```bash
# llama-server stopped (step 0); the venv active
./sft_train.py --dry-run --out ~/minny-sft/sft1
```

Three optimiser steps, then `dry run: N GB peak …; steps took [a, b, c] s;
at the last step's rate ~T tokens/s → ~H h per epoch`. The first step is
kernel compilation and does not count. Out of memory → a lower
`--max-tokens` and dry-run again. Everything else is already at its memory
minimum (micro-batch 1, 8-bit optimiser, Unsloth checkpointing, 4-bit base).

**Measured 2026-10-08 on this laptop at 12,288:** peak **10.2 GB**;
first step 534 s, second **57 s** → ~1,200 training tokens/s →
**~8.6 h an epoch** of 543 steps (35.0 M tokens; 216 train and 11 dev
examples over the cap). Tokenising the examples adds ~8 min at the start
of every run. 16,384 would likely fit in memory and costs ~10 % more
time for the 216 longest guide-result answers.

**Read H before training.** The levers, in order of cost: `--epochs 1`
(a full cosine schedule in one night; the dev-loss trend at the end and
the gates say whether a second epoch is worth another night); rent an
H100 for an hour or two an epoch (the data is 161 MB and leaves no trace
once the box is destroyed); halve the tokens with one example per turn
instead of per step (the two steps of a turn share the same 8k prompt) —
a pre-tokenised two-span mask, not written yet.

## 4. Train

```bash
nohup ./sft_train.py --epochs 1 --max-tokens 12288 --out ~/minny-sft/sft1 --pick-best --merge > ~/minny-sft/sft1.log 2>&1 &
tail -f ~/minny-sft/sft1.log
```

One epoch first (~9 h here), lr 2e-4 cosine, LoRA r 32 on the language
model's projections, effective batch 8; a checkpoint every 200 steps
(`--keep 8` kept). **No dev loss during the run**: an in-loop eval
builds the `[1, seq, 248k]` logit tensor in fp32 (~8 GB) and OOMs this
card. The log shows the training loss every 10 steps only. After the
epoch, `--pick-best` loads a clean 4-bit base, scores every kept
checkpoint and the final adapter on 60 dev examples with the LM head run
on the completion span alone (`logits_to_keep`, no full logit tensor),
writes `pick-best.json`, and `--merge` merges the best. Scoring can be
rerun or done separately:

```bash
./sft_train.py --pick-best-only ~/minny-sft/sft1 --merge      # scores checkpoints/ + adapter/, merges the best
```

A second epoch is a second night, taken only if `pick-best.json` shows
the loss still falling at the last checkpoint and the gates say the
model is short of the teacher rather than past it. Watch `eval_loss` in
the log — it should fall through the first epoch and flatten in the
second; rising again means stop at the best checkpoint (it is kept). A
crash or a reboot: `--resume ~/minny-sft/sft1/checkpoints/checkpoint-NNN`.

Outputs: `checkpoints/checkpoint-N/` (each a complete adapter),
`adapter/` (the end of the run), `train-summary.json` (args, data,
trainable parameters, peak memory, step times, log history),
`pick-best.json`, and with `--merge` the `merged/` bf16 model — the chosen
adapter applied to a fresh bf16 base on the CPU (~20 GB RAM, several
minutes). Without `--merge`, or to redo it:

```bash
./sft_train.py --merge-only ~/minny-sft/sft1/adapter --out ~/minny-sft/sft1
```

## 5. GGUF

llama.cpp's converter, at the server's own build so the template and the
tensor layout match what `:8001` serves:

```bash
cd ~/minny-sft && git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp && git checkout b11340
uv pip install -r requirements/requirements-convert_hf_to_gguf.txt   # see the venv note below
python convert_hf_to_gguf.py ~/minny-sft/sft1/merged --outtype q8_0 --no-nextn \
    --outfile ~/minny-sft/sft1/qwen3.5-9b-sft1-Q8_0.gguf
```

**`--no-nextn` is required.** Qwen3.5 carries a NextN/MTP speculative head
(`text_config.mtp_num_hidden_layers = 1`). Without the flag the converter
writes MTP metadata — `block_count` bumped to 33, a 33-long
`recurrent_layers`, `blk.*.nextn.*` tensors expected — but the merged model
(from `AutoModelForImageTextToText.merge_and_unload`) carries no MTP
weights, so the GGUF is inconsistent and the server refuses it
(`check_tensor_dims: tensor 'blk.32.attn_norm.weight' not found`, then
`blk.31.nextn.eh_proj.weight not found`). `--no-nextn` drops the head
entirely and yields a clean 32-layer model matching what `:8001` serves.

Q8_0 is the level served today (the base is Unsloth's `UD-Q8_K_XL`, a
dynamic Q8; plain Q8_0 is near-lossless and enough). The vision tower is
not needed: no `--mmproj`.

The convert requirements pin older, CPU builds — installing them into the
training venv downgrades it (`torch …+cu126 → …+cpu`, `transformers 5 → 4`),
so it can no longer train. Either use a throwaway venv for the conversion
(`uv venv ~/minny-sft/.convert && source …/bin/activate`), or reinstall the
cu126 stack (step 1's CUDA-version trap) before the next training run.

## 6. Serve the fine-tune

Restart the base server with its kept line, then the fine-tune on `:8002`
with the same flags, three changed:

```bash
llama-server <the kept flags> -m ~/minny-sft/sft1/qwen3.5-9b-sft1-Q8_0.gguf \
    --alias qwen3.5-9b-sft1 --port 8002
```

Two Q8 9B models need ~26 GB of VRAM together. If they do not fit, serve
the fine-tune alone (any port) and compare against the saved baseline:
`sft_eval.py` is deterministic on a fixed dev set, so runs on different
days compare — unlike the eval suite's volatile scenarios.

Check the template the new server reports is the one the data was
rendered with:

```bash
curl -s -H "Authorization: Bearer $LOCAL_LLM_API_KEY" http://127.0.0.1:8002/props \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["chat_template"], end="")' \
  | diff - ~/Development/fastchannels-private/dev/voice/sft/chat_template.qwen3.5-served.jinja && echo "template identical"
```

## 7. The gates, in order

```bash
# 1. held-out dev steps against the teacher — base and fine-tune side by side (either machine)
./sft_eval.py --models qwen3.5-9b@http://192.168.50.71:8001,qwen3.5-9b-sft1@http://192.168.50.71:8002 --show 25
./sft_eval.py --models qwen3.5-9b-sft1@http://192.168.50.71:8002 --judge     # + claude on the text steps
```

The base's baseline (2026-10-07): kind 83.5 %, tool names 58.5 %, args
29.7 %, text ×1.61 the teacher's length, forbidden 3.6 %. The fine-tune
has to move names and args a long way and bring the length to ~1.0
without new failures in the hygiene rows (malformed XML, Python literals,
truncation, think leak must stay at 0).

```bash
# 2. and 3. — from nucbox, where the agent, the guide (VPN) and the TV page live
cd ~/Development/FastChannels/dev/voice
LOCAL_LLM_BASE_URL=http://192.168.50.71:8002/v1 ./model_bench.py --models qwen3.5-9b-sft1     # smoke, 27 questions
LOCAL_LLM_BASE_URL=http://192.168.50.71:8002/v1 ./eval_suite.py --models gpt-5.6-terra,qwen3.5-9b-sft1
```

The suite routes every `qwen*` name to one `LOCAL_LLM_BASE_URL`, so base
and fine-tune cannot share a suite run from two ports; compare the
fine-tune's run with the frontier in the same run and with the base's
figures of 2026-10-07 06:17 (routed 91 / checks 82 / meets 64 %),
remembering that 29 scenarios are volatile. Read the disagreements list,
not only the percentages.

## 8. Promote, or not

If the gates pass: in `dev/voice/.env` on nucbox set
`LOCAL_LLM_MODEL=qwen3.5-9b-sft2` and `LOCAL_LLM_BASE_URL` to the port it
is served on. That changes the **local brain** only — the model the agent
uses when `VOICE_LLM=local`; the household's brain stays the frontier
(`VOICE_LLM=openai`, `VOICE_MODEL`) until that switch is flipped
deliberately; nothing restarts by itself. Copy `train-summary.json` and
the `sft-eval-*.json` results back to nucbox and put the numbers in
`SFT-DATA.md` next to the baseline, pass or fail. (sft1 recommended
2026-10-09; **sft2 promoted 2026-10-10** after all three gates — schedule
reached the frontier, routing/checks at frontier level, judge scores up
on every axis; the remaining 9-point "meets" gap and the unmoved catch-up
category are in `SFT-DATA.md`.)

## First run — sft1, 2026-10-08/09 (what actually happened)

The plan above mostly held; the corrections, in the order they bit:

- **No driver upgrade was needed.** Step 0 suggests moving to a 550+
  driver; we didn't, and 535 was fine. `uv pip install` first pulled a
  `cu130` PyTorch, which `torch.cuda.is_available()` rejects on driver 535
  ("driver too old, found 12020"). The fix is to pin the **cu126** build
  of the same version — `uv pip install --index-strategy unsafe-best-match
  --index-url https://download.pytorch.org/whl/cu126 --extra-index-url
  https://pypi.org/simple "torch==2.14.1+cu126"` — which runs on 535 under
  CUDA 12.x minor-version compatibility. `causal-conv1d` then compiles
  against the system **nvcc 12.2** only with `--no-build-isolation` (else
  uv's build sandbox reinstalls cu130 torch and the build aborts on the
  12.2-vs-13.0 major mismatch); `uv pip install ninja` first and set
  `CAUSAL_CONV1D_FORCE_BUILD=TRUE`.

- **543 → 517 steps.** `--check` projected 543 an epoch on the full 4,349;
  at `--max-tokens 12288`, 216 long examples drop, leaving 4,133 → 517
  steps. Measured **~59.5 s/step** (the dry run said 57), **~8.5 h** for
  the epoch, peak ~12.6 GB. Reruns start faster — the CUDA kernels are
  cached after the first build. Final training loss ~0.156.

- **pick-best curve** (dev loss, 60 examples, context ≤12288):
  100 → 0.218, 200 → 0.184, 300 → 0.161, 400 → 0.135, 500 → 0.134,
  517 → 0.134. Monotonic, the **last** checkpoint wins, and it is flat
  over the final ~100 steps — a second epoch is into diminishing returns;
  let the gates, not the loss, decide.

- **Run `--pick-best-only` as its own process.** The inline
  `--pick-best --merge` tail OOMs: after training, Unsloth keeps ~8.5 GB
  resident that `del model; torch.cuda.empty_cache()` does not release, so
  loading the scoring base on the same card fails. Training, the
  checkpoints and `adapter/` are all saved before that point, so just
  rerun in a fresh process — `./sft_train.py --pick-best-only
  ~/minny-sft/sft1 --merge` — with the whole card free. (Scoring + CPU
  merge took ~10 min.)

- **GGUF needed `--no-nextn`, and the convert step clobbers the venv** —
  both covered in step 5. Restoring the training venv afterwards is the
  cu126 reinstall above plus `transformers==5.17.0`.

The result: `sft1/qwen3.5-9b-sft1-Q8_0.gguf` (8.9 GB, 32 layers,
`block_count=32`) loads on build b11340 and generates at ~43 tok/s on this
card.

- **The gates, 2026-10-09 (from nucbox, `:8001` down so the fine-tune
  alone against the saved baselines).** Gate 1: kind 93.5 % (base 83.5),
  tool names 78.0 % (58.5), args 55.9 % (29.7), text ×0.99 the teacher's
  length (1.61) with no forbidden patterns, hygiene rows all 0, judge
  same-facts 79 % of 110 text steps. Gate 2: 27/27, routed 100 % (base
  88 %), judge 4.52 / 4.41 / 4.78 (base 4.08 / 3.96 / 4.42). Gate 3:
  routed / checks / meets 97 / 93 / 78 % (base 91 / 82 / 64, frontier in
  the same run 97 / 96 / 86), judge 4.62 / 4.26 / 4.73. Weak where the
  guide result is long (catch-up and schedule categories); tuning, memory
  and robustness at or above the frontier's. The table and the reading
  are in `SFT-DATA.md`. Gate 1 taught the eval two things on the way:
  retry the steps a server error loses (12 of 230 failed on "Context
  size has been exceeded" at the start of the run), and give the judge
  room for its notes (38 verdicts were cut off at 200 tokens).

## Time, roughly

Environment 1–2 h (the kernels may compile; no driver upgrade was needed
with the cu126 wheels); data and checks 10 min; dry run 10 min; training
as the dry run projects — 8.5 h an epoch on this card; scoring, merge and
GGUF 30 min; the three gates an hour and a half. If `pick-best.json` is
flat from the first checkpoint or the fine-tune scores below the base on
names and args, the first suspects are the loss mask (`--check`'s
boundary test), the template (`chat_template.jinja` in `train/` against
the served one) and a learning rate too high for the adapter — in that
order.

## Next: run2 and RLVR (decided 2026-10-09)

Why not a bigger model yet: routing and format are solved at 9B; what is
short is reading a long guide result (catch-up 43 % and schedule 58 %
meets against the frontier's 71 and 92). A 27B would likely close that
but cannot be served on this laptop at voice latency (Q4 is ~17 GB), so
it is a purchase, not an experiment. The 9B path first:

1. **run2 — targeted data** (nucbox, the generator) — **done 2026-10-09.**
   Fourteen new templates in `sft/templates.json` for schedule follow-ups
   over long results and catch-up turns, `sft_gen.py --focus`, a
   `bcast_channel` slot. The first attempt's shards ignored `--focus` and
   produced a general run (kept as `run2-general`, 988 turns); the focused
   `run2` is 600 conversations → 955 turns kept, 26 rejected. Both
   prepared; numbers in `SFT-DATA.md`.
2. **sft2** (laptop): one epoch from the base over the three runs,
   6,756 examples at the 12,288 cap, 844 steps, **~14 h** at the measured
   rate:

   ```bash
   # nucbox → laptop: the two new train/ directories
   rsync -av ~/news/voice-sft/run2-general/train/ marco@192.168.50.71:news/voice-sft/run2-general/train/
   rsync -av ~/news/voice-sft/run2/train/ marco@192.168.50.71:news/voice-sft/run2/train/
   # laptop, venv active, llama-server stopped
   cd ~/Development/fastchannels-private && git pull
   cd dev/voice && ./sft_train.py --check --max-tokens 12288 --run ~/news/voice-sft/run1 ~/news/voice-sft/run2-general ~/news/voice-sft/run2
   nohup ./sft_train.py --run ~/news/voice-sft/run1 ~/news/voice-sft/run2-general ~/news/voice-sft/run2 \
       --epochs 1 --max-tokens 12288 --out ~/minny-sft/sft2 > ~/minny-sft/sft2.log 2>&1 &
   ./sft_train.py --pick-best-only ~/minny-sft/sft2 --merge --run ~/news/voice-sft/run1 ~/news/voice-sft/run2-general ~/news/voice-sft/run2   # afterwards, its own process
   ```

   then the GGUF with `--no-nextn`, serving as `qwen3.5-9b-sft2`, and the
   three gates — gate 1 on run1's dev against sft1 (kind 93.5 / names 78.0
   / args 55.9) and on run2's dev for the targeted categories.
   **Done 2026-10-09/10:** sft2 trained on the laptop and served on
   `:8002`; gates from nucbox — run1 dev names 84.7 / args 66.1 %, run2
   dev 91.8 / 49.0 %, model_bench 100 % routed and 5.00 / 4.52 / 5.00, the
   suite 98 / 96 / 77 % against the frontier's 98 / 97 / 86 in the same
   run, schedule meets 58 → 92. Table and reading in `SFT-DATA.md`. The
   evening's gate 3 attempt found the guide's tunnel without its
   `172.23.0.0/16` route (all guide calls timed out); fixed on the network
   side, rerun clean the next morning.

   **Done 2026-10-09.** 845 steps, ~58 s/step; pick-best picked
   checkpoint-845 (dev loss 0.283 at the ≤16384 default cap — not
   comparable to sft1's 0.134, which used ≤12288). The GGUF needed **no
   hand-patching** this run: `merge()`'s embedded served template (7816 B)
   and `--no-nextn` produced a clean `block_count=32` model that loaded
   first try, and the conversion ran in a throwaway `~/minny-sft/.convert`
   venv so the cu126 training venv stayed intact.

   **Gate 1 (run1's dev, 230 ex, 0 errors):** sft2 beats sft1 on every
   tool metric — kind 94.3 %, tool names **84.7 %** (sft1 78.0), args
   **65.3 %** (55.9), arg keys 74.6 % (67.8), exact 38.7 % (33.0); text
   length ×1.0, forbidden 0, all hygiene rows 0. The gains land on run1's
   dev — which run2 added no data for — so it is generalisation, not
   fitting the new examples. One regression: empty / non-text answers
   1.8 % → **4.5 %** (`answered_as_text` 95.5 %), ~5 of 112 text steps —
   the thing to watch in the `--judge` pass and gates 2–3.
3. **RLVR** (rented 80 GB cards; the laptop cannot sample 8 trajectories
   of 8k tokens at a useful rate): step-wise GRPO with TRL's
   `GRPOTrainer` and vLLM, the policy initialised from the merged sft2,
   KL to it as the reference. Prompts are the training prompts as they
   are (the tool results are already inside them), so no environment
   server is needed; a trajectory is one completion. Rewards, all code,
   none a model: format and hygiene (parseable XML, no Python literals,
   end token, no think block); routing and arguments from the templates'
   checks where the prompt has them; **grounding** — every title, channel
   and time spoken must occur in the turn's tool results; **coverage** —
   a schedule or catch-up answer names the first airings of the result in
   order; no claimed action or narrated result without a call; length
   near the teacher's. Held out as before: the suite and the dev splits.
   **Written and verified 2026-10-09:** `rlvr_rewards.py` (the rewards,
   the prompt set over the three runs — 6,405 train / 346 dev prompts —
   and `score-eval`; on run1's dev the teacher scores grounding 0.977 and
   fabrication 0.018, sft1 0.986 / 0.016, the base 0.95 / 0.057) and
   `sft_rl.py` (GRPO, config built against TRL 1.13, `--check` and
   `--dry-run`). The step-by-step for the rented box is the last section
   of this file, "RLVR on the rented H200".

## RLVR on the rented H200 — runbook

> **Run 1 result (2026-10-11): the reward was hacked.** The policy
> collapsed to answering every turn as short text and never calling a tool
> (gate 1: `tool_match 0`, `answered_as_text 1.0`) while the *reward* rose
> the whole way — `kind` 0.80→0.48 was the only warning. sft2 was not
> beaten and **remains the local brain**. The **infrastructure below is
> correct and worked end-to-end** — keep it. What needs changing before
> run 2 is the **reward and schedule** (make kind/routing a hard gate,
> up-weight routing+args, `--beta` ~0.1–0.2, far fewer steps, stop on
> `kind`/`kl` not reward); the full post-mortem is in `SFT-DATA.md`.

The policy starts from **sft2's merged bf16 model** (`~/minny-sft/sft2/merged`
on the laptop); the box only trains. Merging, GGUF, serving and the gates
happen at home afterwards, so the meter runs for the training alone.
Budget at $5.48/h: ~10–11 GPU-hours for setup, two runs of 300–400 steps
and a buffer — $55–65 (in-loop evaluation on 48 dev prompts every 100
steps, ~10 min each, is what keeps it there).

### R0. Before renting (free, at home)

```bash
# laptop — stage the policy where the box can pull it at datacentre speed
uv pip install -U "huggingface_hub[cli]"             # in any venv; the `hf` CLI
hf auth login                                         # a write token
hf upload <you>/minny-sft2-merged ~/minny-sft/sft2/merged --private      # ~18 GB, once

# nucbox — the prompt set and the scripts the box needs, in one archive
ls -la ~/news/voice-sft/rl/                           # prompts.jsonl (~300 MB), vocab.json; built over run1 + run2-general + run2
cd ~/Development/FastChannels/dev/voice && ./sft_rl.py --check | head -40    # teacher rewards; the config builds
tar czf ~/minny-rl-kit.tgz -C ~/Development/FastChannels/dev/voice sft_rl.py rlvr_rewards.py sft_train.py sft \
    -C ~/news/voice-sft rl
```

Rehearse the install (R2) on the laptop once; every minute of
troubleshooting on the box costs nine cents.

### R1. Rent

One **H200 141 GB** — strongly preferred, and what the commands below
assume. **The colocate run peaks ~105 GB of VRAM** (measured 2026-10-11/12,
both the dry run and the real run), so it needs a card with ≥ ~120 GB
usable — the H200's 141 GB fits with headroom. An **H100 80 GB cannot do
colocate** (105 > 80): `--vllm-gpu-mem 0.25` starves vLLM ("No available
memory for the cache blocks") and 0.3 OOMs the backward. **Not a B200** —
Blackwell (`sm_100`) forces a cu128 stack and reopens the CUDA-build fight;
stay on Hopper (same stack as the laptop). An Ubuntu 22.04/24.04 image with
a **550+ driver** (CUDA 12.4 or later — `nvidia-smi` first).

**Set the Container Disk to ≥ 100 GB** (the deploy screen's "Container
Disk" slider, *separate* from the network volume) — the single most
important pod setting, and the cause of a lost evening (2026-10-11). The
default **30 GB overlay cannot hold the venv + CUDA toolkit + the JIT
caches**, and no other filesystem is writable *and* executable *and* roomy:
**`/dev/shm` is `noexec`** (compiled `.so`s from Triton/TileLang/vLLM fail
to `dlopen` — "failed to map segment"), and **`/workspace` cannot `chmod`**
(wheel builds die with "Operation not permitted"); a locked-down container
won't even let you `mount -o remount,exec /dev/shm`. With a ≥ 100 GB
overlay, *everything* — venv, toolkit, builds, Triton/vLLM caches, the
18 GB model — lives on `/` and none of the filesystem juggling in R2/R3
below is needed. Only override `UV_CACHE_DIR` off its `/workspace` preset
(see R2). `df -h / /dev/shm /workspace` first to confirm the overlay is
roomy. Register your SSH key on the account **before** creating the pod (it
is injected at creation; otherwise paste it into `authorized_keys` by hand).
Note the price and the clock when it starts.

### R2. Environment (~20 min; the kernels may compile)

```bash
nvidia-smi ; df -h / /dev/shm /workspace              # driver/memory; overlay vs tmpfs vs the no-chmod volume
# builds + venv on the LOCAL overlay — the volume cannot chmod .so files; its UV_CACHE_DIR preset must be overridden
export UV_CACHE_DIR=/root/.cache/uv TMPDIR=/root/tmp && mkdir -p /root/tmp
export HF_HOME=/root/.cache/huggingface               # small metadata; the 18 GB model goes to --local-dir on the volume (R3)
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
mkdir -p ~/minny-rl && cd ~/minny-rl
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.local/bin/env
uv venv .venv --python 3.12 && source .venv/bin/activate
uv pip install "vllm==0.28.0"                         # PIN: TRL 1.13 supports vLLM 0.19.1–0.28.0, Qwen3.5 needs ≥0.17. NOT latest — see note.
uv pip install "transformers==5.17.0" "trl==1.13.0" "peft==0.21.2" datasets accelerate requests
uv pip install flash-linear-attention                 # Qwen3.5's linear-attention layers (pure Triton, no compile)
# causal_conv1d compiles CUDA — nvcc's MAJOR must equal torch.version.cuda's major:
python -c "import torch; print('torch', torch.__version__, '-> need nvcc major', torch.version.cuda.split('.')[0])"
ls -d /usr/local/cuda-* 2>/dev/null ; nvcc --version 2>/dev/null   # missing the right major? apt-get install -y 'cuda-toolkit-<major>-<minor>*'
export CUDA_HOME=/usr/local/cuda-<major.minor> && export PATH=$CUDA_HOME/bin:$PATH
uv pip install ninja && CAUSAL_CONV1D_FORCE_BUILD=TRUE uv pip install --no-build-isolation causal-conv1d
python -c 'import torch, vllm, trl, transformers; from transformers import AutoConfig; import fla, causal_conv1d; \
print(torch.__version__, torch.cuda.get_device_name(0), "vllm", vllm.__version__); \
print("model type:", AutoConfig.from_pretrained("Qwen/Qwen3.5-9B").model_type, "| hybrid kernels ok")'
```

`model type` must print `qwen3_5`.

**The version bind — what cost a day (2026-10-10).** `uv pip install vllm`
unpinned took the latest (**0.31.0**); TRL 1.13 only supports vLLM
**0.19.1–0.28.0**, and 0.31 **deadlocks** its colocate init — the trainer
hangs in `futex_wait` with weights loaded and the GPU at 0 %, in both
`colocate` and `server` mode. Qwen3.5's gated-delta layers are supported
from vLLM **0.17**, so **`vllm==0.28.0` satisfies both** (that is the pin
above). Three follow-ons, each of which bit once:
- **`nvcc` major must match torch's CUDA major.** vLLM pulls its own
  torch; `causal_conv1d` failed with `12.8 vs 13.0` and `12.2 vs 13.0`
  until `CUDA_HOME` pointed at a toolkit of torch's major. Install the
  toolkit for torch's major; do **not** downgrade torch to match an old
  toolkit (breaks vLLM).
- **The `/workspace` volume cannot `chmod`.** The pod presets
  `UV_CACHE_DIR` onto it, so every compiled wheel dies with `Operation not
  permitted` until `UV_CACHE_DIR`/`TMPDIR` are moved to `/root` (above).
  Keep *builds* on the overlay, *data* on the volume.
- **The overlay is small** (~30 GB, ~3–6 GB free after the stack). Purge
  the CUDA toolkit once `causal_conv1d` is built (`apt-get remove --purge
  'cuda-toolkit-*'`) if you need room; the 18 GB model never goes on the
  overlay — `--local-dir` to the volume, or `/dev/shm` for fast loads.

Confirmed on the 2026-10-11 H200 run — the exact snags and their fixes:
- `vllm==0.28.0` still pulled **`torch 2.13.0+cu130`**, and the image only
  had the 12.8 toolkit → `apt-get install -y 'cuda-toolkit-13-0*'`,
  `CUDA_HOME=/usr/local/cuda-13.0`, then `causal_conv1d` builds. (The
  nvcc-major rule above, made concrete.)
- **`flash-linear-attention` is its own install line and easy to skip** —
  if you do, the import check dies with `ModuleNotFoundError: No module
  named 'fla'`. Install it, re-run the check.
- **Do not `-U huggingface-hub`** — `transformers 5.17` requires it `<2.0`;
  the pinned installs resolve a compatible one (1.33), and `hf download`
  works off that. (`uv pip install -U "huggingface_hub[cli]"` broke the
  laptop venv this way; on the box just don't upgrade it.)
- The H200's **`/dev/shm` was 117 GB** — staging the model there
  (`hf download … --local-dir /dev/shm/merged`, then `--base /dev/shm/merged`)
  loaded far faster than the volume and dodged the earlier "stuck loading"
  pain. That "stuck at 0 %" on the first attempt was **not** slow IO — it
  was the vLLM-0.31 `futex` deadlock (check `State`/`wchan` in `/proc`,
  not just GPU %, to tell IO-wait from a lock).

### R3. Model, prompts, scripts (~10 min)

```bash
export HF_TOKEN=...                                   # a read token for the private repo
hf download <you>/minny-sft2-merged --local-dir ~/minny-sft/sft2/merged
scp nucbox:minny-rl-kit.tgz . && tar xzf minny-rl-kit.tgz    # sft_rl.py rlvr_rewards.py sft_train.py sft/ rl/
./sft_rl.py --check --prompts ./rl                    # teacher rewards as at home; GRPOConfig builds on this TRL
```

Kit transfer, as it actually went: when the scripts or prompt layout
change, rebuild the kit on whichever machine has the runs (`rlvr_rewards.py
build --runs … --out ~/news/voice-sft/rl`, then `tar czf …`) — it was
rebuilt on the **laptop** after the 2026-10-10 layout change. The rented
pod **cannot reach the LAN**, so **push from the laptop**, capital-`-P`
port: `scp -P <POD_PORT> -i ~/.ssh/id_runpod ~/minny-rl-kit.tgz
root@<POD_IP>:` (use the "SSH over exposed TCP" endpoint — the
`ssh.runpod.io` proxy has no SCP). Register your SSH **public** key on the
RunPod account *before* creating the pod; if you forget, it drops to a
password prompt — paste `id_runpod.pub` into the pod's
`~/.ssh/authorized_keys` by hand (chmod 700 `~/.ssh`, 600 the file) via the
web terminal.

### R4. Dry run (~5 min)

```bash
./sft_rl.py --dry-run --prompts ./rl --base ~/minny-sft/sft2/merged --out ~/minny-rl/rl1
```

Two optimiser steps with four short generations, then `dry run: N GB
peak`. It loads the policy in bf16 (~18 GB), attaches the LoRA, starts
vLLM colocated with 30 % of the card, samples, scores, steps. On the H200
it just runs. Out of memory on a tighter card: keep
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` (above) and cut the
**training** side — `--batch 1`, then lower `--num-generations` /
`--max-completion`. Do **not** drop `--vllm-gpu-mem` below ~0.3: vLLM
needs ~0.3 of the card for the 18 GB weights + KV and errors "No available
memory for the cache blocks" below it. A **hang** with the GPU at 0 % and
the process in `futex_wait` is **not** memory — it is the vLLM-version
bind (R2), so check `vllm.__version__ == 0.28.0` first. vLLM refusing the
architecture: R2's version note.

### R5. Train (~3 h a run)

```bash
nohup ./sft_rl.py --prompts ./rl --base ~/minny-sft/sft2/merged --out ~/minny-rl/rl1 --max-steps 300 > ~/minny-rl/rl1.log 2>&1 &
tail -f ~/minny-rl/rl1.log
```

Defaults: 8 generations of up to 300 tokens per prompt, temperature 1,
LoRA r 32 on the language model's projections, lr 1e-5 constant after a
5 % warm-up, KL to the start model (`--beta 0.02`), dapo loss, a
checkpoint (a complete adapter) and a 48-prompt dev evaluation every 100
steps. Prompts over 10k tokens are dropped; one optimiser step is one
group of eight (`--batch 4 --grad-accum 2`), about 29 s on an H200.

What the log should show, every 5 steps: `reward` rising from the first
tens of steps (the components come as `reward/format`, `reward/routing`,
`reward/args`, `reward/grounding`, `reward/coverage`,
`reward/fabrication`, `reward/length`, `reward/kind`); `reward/format`
pinned at 1.0 throughout; `kl` small and stable (under ~0.2 at this
beta); completion length steady — not collapsing to one line, not
growing to the cap. Signs to stop and read: format below 1.0 (the policy
found a shape the parser rejects); kl climbing (lower the lr or raise
beta); coverage rising while length falls to 0 (it is listing everything
— raise the length weight in `rlvr_rewards.WEIGHTS`); nothing moving
after 100 steps (`--lr 2e-5`, or `--num-generations 16` for a sharper
group signal). A second run with the adjustment is the plan, not a
surprise.

### R6. Bring the adapter home, stop the meter (~5 min)

```bash
# from the laptop
rsync -av box:minny-rl/rl1/adapter/ ~/minny-rl/rl1/adapter/              # ~300 MB
rsync -av box:minny-rl/rl1/train-summary.json box:minny-rl/rl1.log ~/minny-rl/rl1/
rsync -av box:minny-rl/rl1/checkpoints/ ~/minny-rl/rl1/checkpoints/      # optional: every 100-step adapter
```

Then **terminate the instance**. Nothing else lives on it.

### R7. At home: merge, GGUF, serve, gates

```bash
# laptop, training venv
./sft_train.py --merge-only ~/minny-rl/rl1/adapter --out ~/minny-rl/rl1 --base ~/minny-sft/sft2/merged
# the throwaway convert venv, as in step 5
python ~/minny-sft/llama.cpp/convert_hf_to_gguf.py ~/minny-rl/rl1/merged --outtype q8_0 --no-nextn \
    --outfile ~/minny-rl/rl1/qwen3.5-9b-rl1-Q8_0.gguf
llama-server [the kept flags] -m ~/minny-rl/rl1/qwen3.5-9b-rl1-Q8_0.gguf --alias qwen3.5-9b-rl1 --port 8002
```

The gates, from nucbox, against **sft2's** numbers: gate 1
`sft_eval.py` on run1's dev (sft2: names 84.7 / args 66.1 %) and run2's
dev (91.8 / 49.0 %), plus `rlvr_rewards.py score-eval RESULTS.json
--vocab ~/news/voice-sft/rl/vocab.json` for the full-dev reward
comparison against the teacher's; gate 2 `model_bench.py`; gate 3
`eval_suite.py --models gpt-5.6-terra,qwen3.5-9b-rl1` (sft2: 98 / 96 /
77 % against the frontier's 98 / 97 / 86; the misses to watch are the
on-now framing, the two tuning references, the ignored recall, "Hey
Wanda"). Promote through `.env` only if it beats sft2 on gates 1 and 3
without a hygiene regression, and record the result in `SFT-DATA.md`.

### Run 2.5 — targeted on the weak categories (prepared 2026-10-11, ran 2026-10-13)

> **Result (2026-10-13): ran clean, a wash — sft2 kept.** The focused set was
> already near-solved by sft2, so training KL stayed ~0.01 and the policy
> barely moved; gate 1 on run2's dev was noise (kind 0.990 unchanged, the
> targeted catch-up family −1/−2, not up). Not promoted. Full numbers and the
> run-2.6 lesson (filter to sft2's *misses*, raise `--lr`) in `SFT-DATA.md`.
> The procedure below is correct and reusable — only the prompt set needs to
> be harder next time.


Run 2 was a safe near-wash: it held the frontier parity and lifted catch-up
(gate 1 catch-up 43→86) but **redistributed** rather than added — schedule
fell (92→75) by the same hand that raised catch-up, because the reward
trained uniformly over all 6,405 prompts and GRPO moved mass off the turns
sft2 already did well. Run 2.5 fixes that by **training only where sft2 is
weak** and leaving the strong categories untouched so they cannot regress.
Same reward, same guardrails as run 2 (`kind` hard-floor, `--beta 0.1`,
early-stop on `eval_reward`) — the only change is the **prompt set**.

**The focused set is built at home and shipped in the kit** — the pod has no
run data, so it cannot rebuild it. `rlvr_rewards.py build` now takes
`--templates`: it still counts the full vocab from every turn but writes only
prompts whose metadata `template` is in the list. The 14 kept templates are
the catch-up and on-now families that miss; schedule, tuning and volume are
**deliberately excluded** so they stay at sft2's level.

```bash
# laptop — build the focused set (1,803 train / 96 dev), then fold it into the kit
cd ~/Development/FastChannels/dev/voice
./rlvr_rewards.py build --runs ~/news/voice-sft/run1 ~/news/voice-sft/run2-general ~/news/voice-sft/run2 \
    --out ~/news/voice-sft/rl-focus \
    --templates catchup catchup_title catchup_browse catchup_news m_catchup_pick m_catchup_title_play m_missed restart \
               on_now on_now_channel title_now genre_now m_title_now_put_on m_now_then_next
./sft_rl.py --check --prompts ~/news/voice-sft/rl-focus | tail -20          # GRPOConfig builds; beta 0.1
tar czf ~/minny-rl-kit.tgz -C ~/Development/FastChannels/dev/voice sft_rl.py rlvr_rewards.py sft_train.py sft \
    -C ~/news/voice-sft rl rl-focus                                         # kit now carries BOTH rl/ and rl-focus/
```

Then rent and set up exactly as R1–R4 (the kit already has `rl-focus/`). The
only changed commands are the dry run and train — point `--prompts` at
`./rl-focus`, cut the steps (a quarter the prompts, so the pass is shorter),
and write a new output dir:

```bash
./sft_rl.py --dry-run --prompts ./rl-focus --base ~/minny-sft/sft2/merged --out ~/minny-rl/rl25
nohup ./sft_rl.py --prompts ./rl-focus --base ~/minny-sft/sft2/merged --out ~/minny-rl/rl25 \
    --max-steps 120 > ~/minny-rl/rl25.log 2>&1 &
tail -f ~/minny-rl/rl25.log
```

Watch the same signals as R5 (`reward/format` pinned at 1.0, `kl` under ~0.2,
`kind` holding, length not collapsing). Bring it home (R6, `rl25`), merge /
GGUF / serve (R7, `--no-nextn`, alias `qwen3.5-9b-rl25`, port 8002).

**Gate and promote rule — the one thing run 2.5 must satisfy.** Run the same
three gates against sft2's numbers, and **require no schedule regression**:
gate 1 on run2's dev must keep its schedule/args at or above sft2 (the 92→75
drop is exactly what we are undoing), *and* show the catch-up / on-now lift
run 2 found. Promote through `.env` only if it **raises the weak categories
without pulling any strong one down** and holds gate 3's frontier parity
(98/96/77) with no hygiene regression. If schedule still falls, the focused
set is still too broad — drop `on_now`/`genre_now` and keep only the pure
catch-up templates, and rerun. Record the outcome in `SFT-DATA.md`.

## Serving latency: prompt layout and history budget (plan, 2026-10-10)

The first household session on the local brain (29 turns, sft2 on the
laptop): behaviour as the gates said, first text **p50 6.1 s, p90 12.9 s**
against the frontier's 2.7 / 4.5 s. All of it is prompt processing:
llama-server reports `cache_read_tokens = 7,699` on every request — the
static system prompt — and re-processes everything after it (clock line,
on-screen line, memory notes, the history, this turn's tool results),
6–11k tokens by the fourth turn, twice a turn. Three fixes; the first is a
server flag, the other two are agent changes that the next data
generation must share, since training prompts have to match serving.

**Fix 1 — `--cache-reuse 256` on llama-server** (laptop restart). Lets
llama.cpp reuse cached KV chunks past the point where a prompt diverges.
Measure on the next turns: the rollouts' `requests[].usage.cache_read_tokens`
against `input_tokens`; if it stays at 7,699 the feature does not apply to
this hybrid model (KV shifting needs plain attention; 24 of the 32 layers
are linear attention) and fix 2 carries the whole gain.

**Fix 2 — a stable prefix: the volatile lines move into the user turn.**
Today three `@agent.instructions` hooks (`time_context`, `memory_context`,
`screen_context`, `pai_agent.py` ~455–465 and ~751) render the clock, the
memory notes and the on-screen line *into the system prompt*, after the
static text; any change to them — the minute, a tune, a recalled fact —
invalidates the cache for the history behind them. The change: the three
hooks go, and `run_turn` prepends one context block to the viewer's
utterance in the user message —

```
[Minny context — not spoken by the viewer]
Clock: Saturday, October 10, 2026, 9:28 AM PDT …
On screen now (live): KTVU channel 2 | player playing, volume 50%
Memory: the viewer's favourite channel is KQED; usual volume 40 %.

Tune on that channel.
```

— so the system prompt is byte-identical for every request of a
deployment (`prompt_hash` becomes stable), every earlier turn is frozen
history (its block is stale but never rewritten), and a turn's second
request differs from its first only by the appended tool call and
result. Qwen's template forbids a second system message, which is why the
block rides in the user turn. Pieces: a `context_block(deps)` builder and
a `strip_context(text)` used wherever the raw utterance matters (memory
extraction, the transcript log — the rollouts already take `user_text`
separately); the instruction wording that says "these notes, refreshed
every request" points to the block instead; `format_time_context` keeps
minute resolution (the windows need it) since the clock no longer breaks
anything. The frontier path gains too: OpenAI's prompt caching also wants
an exact prefix, and the household brain's instructions currently change
every request for the same reason.

**Fix 3 — the history budget.** `_trim` already keeps at most 12 exchanges
and ~8,000 *estimated* tokens (chars ÷ 4) and blanks tool results older
than `KEEP_TOOL_RESULTS_FOR` exchanges to "[tool result omitted]"; prompts
still reached 18–19k tokens because the estimate undercounts JSON rows
(digits and punctuation tokenise at ~3 chars a token), the current turn's
two full results sit on top, and 25-row results are large. Changes: count
tokens at chars ÷ 3 for tool returns; budget 4,000 history tokens; replace
blanking with **compaction at commit** — the finished turn's tool returns
are rewritten to a short digest (the first three rows as title · channel ·
start, plus "… and N more") except the last listing result, which stays
full for one more turn so "the second one" still resolves; and
`MAX_ROWS` 25 → 15 for broad schedule searches (the window ranking already
puts the relevant rows first). Compaction at commit rewrites only the tail
of the prefix, so the cache loses the newest turn once, not the
conversation.

**Verification, in this order:** the cache ratio from the rollouts after
each change (`cache_read_tokens / input_tokens` on second requests should
approach 1); first text p50 on a scripted multi-turn session (the suite's
multi-turn scenarios and a ten-turn `minny_brain_check`-style run) — the
target is ~2–3 s on the laptop; `eval_suite.py` on the frontier before and
after fix 2 (the household brain must not regress) and on sft2 (a
cross-layout check: the fine-tune was trained with the lines in the
system prompt, so expect a small loss until retrained); then **run3** —
one general + focused generation under the new layout and compaction
(~1,500 conversations) — `sft3` trained on it, and the RLVR prompt set
rebuilt from it. Order of work: fix 2 (half a day with the gates), fix 3
(half a day), run3 and sft3 (a day, mostly the laptop's), RLVR after.

**Status 2026-10-10, fixes 2 and 3 implemented** (`--cache-reuse` turned
out not to be supported for this model, as feared):

- The context block is in (`pai_agent.CONTEXT_HEADER` / `CONTEXT_END`,
  `VoiceBrain.user_prompt`, `strip_context`): the viewer's words first,
  then a blank line, the block, `[end of context]`. The first placement
  (block first) made sft2 answer *about the screen line* in four suite
  turns ("Remember that my favorite channel is KQED" → "KTVU channel two
  is on right now…") — the fine-tune reads the opening of the user message
  as the request, as every training example had it; words first cured
  three of the four, the fourth (C5) was failing before. The three dynamic
  instruction hooks are gone, so the system prompt is byte-identical per
  deployment. Every run site goes through `user_prompt` — the worker
  (`pai_llm`), `eval_suite`, `sft_gen`, `model_bench`, `minny_brain_check`;
  `sft_eval` and `rlvr_rewards` strip the block when they show or judge
  the request.
- History: tool returns counted at chars ÷ 3, a 6,000-token budget, and
  **every exchange abridged once, at its own commit** — its last tool
  result to eight rows with airing ids (what "the second one" or "what's
  after that" refer to, still playable), the others to three — so a
  rewrite only ever lands at the tail. The first version abridged results
  two exchanges later, in the middle of the history, and the server lost
  the whole history's cache at every turn start (the household session
  showed request 1 of a turn at a 75 % hit while request 2 was at 96 %).
  Rows (`row_shape pacific-iso-lean/2026-10-10`): `kind`, `program_type`,
  `duration_s` dropped, description ≤ 120 chars, two genres, `MAX_ROWS`
  15; the lineup-wide what's-on-now is an overview row (title, channel,
  number, end, airing id, one genre, 60 chars of description). Payloads:
  the 52-row on-now 27.8k → ~14k chars, a 13-row evening 7.7k → 6.5k.
- Measured on the local brain, four turns through the real agent and the
  dry-run TV while the gates were also running on the laptop: first text
  **3.5 / 2.7 / 2.2 / 2.3 s**, the first request of a turn re-reading
  400–600 tokens instead of 3–10k (the same turns before any change: 7.6 /
  12.8 / 7.1 / 0.7; the morning's household session 7–14 s on long
  conversations). The four regressed suite scenarios re-run alone answered
  in ~0.5 s. What remains at a turn boundary is Qwen's template rendering
  the previous turn's assistant message with the empty think block only
  while it is the latest — a few hundred tokens now that results are
  digests.
- Gates on the new layout: `model_bench` 27/27, routed 96 %, judge 4.74 /
  4.56 / 5.00 (before the layout 100 %, 5.00 / 4.52 / 5.00 — within the
  bench's run-to-run spread). The suite, words-first, frontier and sft2 in
  one run (10:44, `suite-20261010-1044.json`): **sft2 96 / 92 / 78 %
  against the frontier's 96 / 95 / 83 %**, judge 4.60 / 4.24 / 4.87 against
  4.73 / 4.46 / 4.95 — the same gap as the morning's old-layout run (98 /
  96 / 77 against 98 / 97 / 86), both models lower by the mid-morning
  guide (on-now 43 % meets for both; the frontier lost ten turns to the
  time of day, sft2 lost eight and gained nine). sft2's first text in the
  suite **2.2 s p50, the frontier's 2.2 s**, from 2.7. One earlier suite run
  had skipped the frontier because the benches inherited the production
  switch's local settings (`extra_body chat_template_kwargs`, which OpenAI
  rejects); `model_bench` now pins the agent's default brain to OpenAI.
- Left for run3 (both need retraining, so they travel with the next data):
  a served template that renders the empty think block for every
  assistant turn (stable bytes across turns; a `--chat-template-file` on
  llama-server and the same file for the prep), and a text row format in
  place of JSON rows (the key overhead is now most of a payload).

### What RL can and cannot fix here

The rewards are code, so they move what code can verify: the tool and
its arguments, answering from a result already in hand, not inventing or
dropping titles, channels and times, the spoken form. They cannot teach
the wording a criterion wants ("from the beginning", the name Minny) or
a judgement the judge makes about framing — those are data (the next
templates) or prompt. Read a run's gain in gate 1's args and the
grounding/coverage columns first; the suite's "meets" moves last.
