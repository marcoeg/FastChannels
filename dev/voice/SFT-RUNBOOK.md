# Fine-tuning the local brain on the laptop — runbook

The laptop is `192.168.50.71` (llama-server b11340 on `:8001`, alias
`qwen3.5-9b`, four slots sharing a 32k context, API key
`LOCAL_LLM_API_KEY`). Everything below runs there unless marked **nucbox**.
The data and the scripts are ready; nothing in this list has been run yet
except the data preparation and the base model's baseline (both from
nucbox). Written 2026-10-07.

What you end up with: `~/minny-sft/sft1/qwen3.5-9b-sft1-Q8_0.gguf`, served
as `qwen3.5-9b-sft1`, with three gates passed or failed in writing.

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
- **Upgrade the driver before step 1.** 535 (CUDA 12.2) is older than
  current PyTorch wheels (cu126/cu128) expect; a 550+ series driver from
  Ubuntu's repository (`ubuntu-drivers list`, `sudo apt install
  nvidia-driver-5xx`, reboot, `nvidia-smi` showing CUDA 12.4 or later)
  removes the "CUDA driver version is insufficient" class of failures.
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
is served on. The household's brain stays the frontier (`VOICE_MODEL`)
until the local path is switched on deliberately; nothing restarts by
itself. Copy `train-summary.json` and the `sft-eval-*.json` results back
to nucbox and put the numbers in `SFT-DATA.md` next to the baseline, pass
or fail.

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
card. The gates (step 7) have not been run yet.

## Time, roughly

Driver upgrade and reboot 20 min; environment 1–2 h (the kernels may
compile); data and checks 10 min; dry run 10 min; training as the dry run
projects — on this card hours to a day an epoch; merge and GGUF 30 min;
the three gates an hour. If `eval_loss` is flat from the start or the fine-tune scores
below the base on names and args, the first suspects are the loss mask
(`--check`'s boundary test), the template (`chat_template.jinja` in
`train/` against the served one) and a learning rate too high for the
adapter — in that order.
