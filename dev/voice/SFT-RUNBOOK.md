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
`LOCAL_LLM_MODEL=qwen3.5-9b-sft1` and `LOCAL_LLM_BASE_URL` to the port it
is served on. That changes the **local brain** only — the model the agent
uses when `VOICE_LLM=local`; the household's brain stays the frontier
(`VOICE_LLM=openai`, `VOICE_MODEL`) until that switch is flipped
deliberately; nothing restarts by itself. Copy `train-summary.json` and
the `sft-eval-*.json` results back to nucbox and put the numbers in
`SFT-DATA.md` next to the baseline, pass or fail. (sft1: recommended as
the local brain on 2026-10-09; the results are in `SFT-DATA.md`.)

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
   `--dry-run`). On the rented box: the training venv's stack plus vLLM
   (`uv pip install vllm` matching the torch build), the merged sft2
   rsynced over, `./sft_rl.py --check`, `--dry-run --base <merged>`, then
   `--base <merged> --out ~/minny-rl/rl1 --max-steps 300`; merge with
   `sft_train.py --merge-only … --base <merged>`, GGUF, the gates against
   sft2.
