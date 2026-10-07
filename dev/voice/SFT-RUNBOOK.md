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
ps -o args= -C llama-server     # keep this line: the flags to reuse in step 8
```

- 16 GB: QLoRA at 16k tokens is tight; plan on `--max-tokens 12288`.
- 24 GB+: the defaults (16,384) should fit; the dry run in step 4 says.
- **The served model holds ~13 GB of VRAM.** Stop llama-server for the
  training hours (`systemctl --user stop …` or kill it; the household
  brain is the frontier, only local-model tests lose it) and restart it
  afterwards with the line you kept.

## 1. Code and environment (once)

```bash
cd ~/Development/FastChannels && git pull        # sft_train.py, sft_eval.py, sft/, SFT-DATA.md
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
cd ~/Development/FastChannels/dev/voice && source ~/minny-sft/.venv/bin/activate
./sft_train.py --check
```

Expected (it ran on nucbox with the same data): 4,349 + 230 examples
usable, 200/200 tokenise as the prep counted, eos `<|im_end|>`, 543 steps
an epoch, 1,087 for two.

## 3. Stop the server, dry-run

```bash
# llama-server stopped (step 0)
./sft_train.py --dry-run --out ~/minny-sft/sft1
```

Two optimiser steps, then `dry run: N GB peak for 16384-token sequences`.
Out of memory → `--max-tokens 12288` and dry-run again (that cap drops 230
of the 4,579 examples, the longest guide-result answers — acceptable, not
free). Everything else is already at its memory minimum (micro-batch 1,
8-bit optimiser, Unsloth checkpointing, 4-bit base).

## 4. Train

```bash
nohup ./sft_train.py --out ~/minny-sft/sft1 --merge > ~/minny-sft/sft1.log 2>&1 &
tail -f ~/minny-sft/sft1.log
```

Two epochs, lr 2e-4 cosine, LoRA r 32 on the language model's projections,
effective batch 8; dev loss and a checkpoint every 200 steps (five in all),
the best by dev loss loaded at the end; expect hours. Watch `eval_loss` in
the log — it should fall through the first epoch and flatten in the
second; rising again means stop at the best checkpoint (it is kept). A
crash or a reboot: `--resume ~/minny-sft/sft1/checkpoints/checkpoint-NNN`.

Outputs: `adapter/`, `train-summary.json` (args, data, trainable
parameters, peak memory, log history, final eval), and with `--merge` the
`merged/` bf16 model — the adapter applied to a fresh bf16 base on the
CPU (~20 GB RAM, several minutes). Without `--merge`, or to redo it:

```bash
./sft_train.py --merge-only ~/minny-sft/sft1/adapter --out ~/minny-sft/sft1
```

## 5. GGUF

llama.cpp's converter, at the server's own build so the template and the
tensor layout match what `:8001` serves:

```bash
cd ~/minny-sft && git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp && git checkout b11340
uv pip install -r requirements/requirements-convert_hf_to_gguf.txt
python convert_hf_to_gguf.py ~/minny-sft/sft1/merged --outtype q8_0 \
    --outfile ~/minny-sft/sft1/qwen3.5-9b-sft1-Q8_0.gguf
```

Q8_0 is the level served today (the base is Unsloth's `UD-Q8_K_XL`, a
dynamic Q8; plain Q8_0 is near-lossless and enough). The vision tower is
not needed: no `--mmproj`.

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
  | diff - ~/Development/FastChannels/dev/voice/sft/chat_template.qwen3.5-served.jinja && echo "template identical"
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

## Time, roughly

Environment 1–2 h (the kernels may compile); data and checks 10 min; dry
run 10 min; training an afternoon; merge and GGUF 30 min; the three gates
an hour. If `eval_loss` is flat from the start or the fine-tune scores
below the base on names and args, the first suspects are the loss mask
(`--check`'s boundary test), the template (`chat_template.jinja` in
`train/` against the served one) and a learning rate too high for the
adapter — in that order.
