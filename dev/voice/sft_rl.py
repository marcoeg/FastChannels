#!/usr/bin/env python3
"""RLVR on the local brain: GRPO over the prepared prompts with the
verifiable rewards of rlvr_rewards.py. For rented 80 GB cards; not run here.

A trajectory is one step: a prompt exactly as llama.cpp serves it (the
turn's tool results already inside) and one sampled completion. No
environment server: the trainer samples --num-generations completions per
prompt with vLLM, rlvr_rewards scores each (format, routing and arguments
from the templates' checks, grounding, coverage, fabrication, length, the
teacher's decision as a weak prior), and GRPO moves the policy toward the
better ones inside each group. The policy starts from the merged SFT model
(--base, e.g. ~/minny-sft/sft2/merged) with a fresh LoRA on the same
projections sft_train.py tunes; --beta > 0 keeps a KL to that start.

Data: rl/prompts.jsonl from `rlvr_rewards.py build` (prompt,
teacher_completion, checks, listing, kind_expected, split, n_tokens);
the train split is trained on, the dev split is scored with the rewards
every --eval-steps for the curve. The eval suite stays the test.

Modes:
  --check     rewards over the teacher's completions of the prompt set and
              the GRPOConfig built for this TRL; no model
  --dry-run   two optimiser steps with small generation; peak memory
  (default)   train; adapter/ and train-summary.json in --out

Stack: TRL ≥ 1.13 GRPOTrainer with vLLM ("colocate" shares the training
GPU: ~18 GB policy + the vLLM copy + KV for 8k-token prompts × 8
generations — one 80 GB card, tight; "server" puts vLLM on a second card),
peft for the LoRA. transformers 5 (warmup_steps takes the ratio).

  ./sft_rl.py --check
  ./sft_rl.py --dry-run --base ~/minny-sft/sft2/merged --out ~/minny-rl/rl1
  ./sft_rl.py --base ~/minny-sft/sft2/merged --out ~/minny-rl/rl1 --max-steps 300
  ./sft_train.py --merge-only ~/minny-rl/rl1/adapter --out ~/minny-rl/rl1 --base ~/minny-sft/sft2/merged
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rlvr_rewards as rr  # noqa: E402

DEFAULT_PROMPTS = Path.home() / "news" / "voice-sft" / "rl"
TARGET_MODULES = (r"model\.language_model\.layers\.\d+\."
                  r"(self_attn\.(q_proj|k_proj|v_proj|o_proj)"
                  r"|linear_attn\.(in_proj_qkv|in_proj_z|out_proj)"
                  r"|mlp\.(gate_proj|up_proj|down_proj))")
RENAMES = {"warmup_ratio": ["warmup_steps"], "eval_strategy": ["evaluation_strategy"]}


# ---- data -------------------------------------------------------------------------------------------
def load_prompts(path: Path, max_tokens: int, split: str) -> list[dict]:
    rows = []
    for line in (path / "prompts.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e.get("split") != split or e["n_tokens"] > max_tokens:
            continue
        rows.append({"prompt": e["prompt"], "teacher_completion": e["teacher_completion"],
                     "checks_json": json.dumps(e.get("checks") or {}), "listing": bool(e.get("listing")),
                     "kind_expected": e.get("kind_expected") or "text", "template": e.get("template") or "",
                     "n_tokens": e["n_tokens"]})
    return rows


def make_reward_fn(vocab: rr.Vocab | None):
    """The one reward function GRPO calls: the weighted total per completion,
    the components logged as metrics. Dataset columns arrive as list kwargs."""
    def reward_fn(prompts, completions, checks_json, teacher_completion, listing, kind_expected, **kwargs):
        totals, comps = [], defaultdict(list)
        for prompt, completion, cj, teacher, lst, kind in zip(prompts, completions, checks_json, teacher_completion, listing, kind_expected):
            text = completion if isinstance(completion, str) else completion[0]["content"]
            total, c = rr.reward(prompt, text, {"checks": json.loads(cj), "teacher_completion": teacher,
                                                "listing": lst, "kind_expected": kind}, vocab)
            totals.append(total)
            for k in ("format", "routing", "args", "grounding", "coverage", "fabrication", "length", "kind"):
                if c.get(k) is not None:
                    comps[k].append(c[k])
        log_metric = kwargs.get("log_metric")
        if log_metric:
            for k, v in comps.items():
                log_metric(f"reward/{k}", statistics.mean(v))
        return totals
    reward_fn.__name__ = "rlvr_reward"
    return reward_fn


def score_split(rows: list[dict], vocab, completions: list[str] | None = None) -> dict:
    """Mean components over a split, for the teacher's completions (the
    ceiling the rewards see) or a model's."""
    acc = defaultdict(list)
    for i, r in enumerate(rows):
        text = completions[i] if completions else r["teacher_completion"]
        total, c = rr.reward(r["prompt"], text, {"checks": json.loads(r["checks_json"]), "teacher_completion": r["teacher_completion"],
                                                 "listing": r["listing"], "kind_expected": r["kind_expected"]}, vocab)
        acc["total"].append(total)
        for k in ("format", "routing", "args", "grounding", "coverage", "fabrication", "length", "kind"):
            if c.get(k) is not None:
                acc[k].append(c[k])
    return {k: (round(statistics.mean(v), 3), len(v)) for k, v in acc.items()}


# ---- config ------------------------------------------------------------------------------------------
def grpo_config(GRPOConfig, **wanted):
    names = {f.name for f in dataclasses.fields(GRPOConfig)}
    kw = {}
    for k, v in wanted.items():
        for cand in [k] + RENAMES.get(k, []):
            if cand in names:
                kw[cand] = v
                break
        else:
            raise SystemExit(f"this TRL's GRPOConfig has no {k!r} (nor {RENAMES.get(k, [])}); pin TRL or adapt grpo_config()")
    return GRPOConfig(**kw)


def training_kwargs(args, out: Path, dry: bool) -> dict:
    return dict(
        output_dir=str(out / "checkpoints"), seed=args.seed, report_to="none", disable_tqdm=True,
        per_device_train_batch_size=args.batch, gradient_accumulation_steps=args.grad_accum,
        num_generations=4 if dry else args.num_generations,
        max_completion_length=64 if dry else args.max_completion,
        temperature=args.temperature, beta=args.beta, epsilon=0.2, loss_type=args.loss_type,
        num_iterations=1, scale_rewards="group", mask_truncated_completions=True,
        learning_rate=args.lr, lr_scheduler_type="constant_with_warmup", warmup_ratio=0.05,
        max_grad_norm=1.0, bf16=True, gradient_checkpointing=True, optim="adamw_torch",
        max_steps=2 if dry else args.max_steps, num_train_epochs=1,
        logging_steps=1 if dry else 5, log_completions=not dry,
        save_strategy="no" if dry else "steps", save_steps=args.eval_steps,
        eval_strategy="no" if dry else "steps", eval_steps=args.eval_steps,
        per_device_eval_batch_size=args.num_generations,        # TRL: the eval batch must hold whole groups
        use_vllm=True, vllm_mode=args.vllm_mode, vllm_gpu_memory_utilization=args.vllm_gpu_mem,
        vllm_max_model_length=args.max_tokens + args.max_completion,
    )


# ---- main ----------------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS, help="rl/ from rlvr_rewards.py build")
    ap.add_argument("--base", help="the merged SFT model to start from (HF directory); required to train")
    ap.add_argument("--out", type=Path, default=Path.home() / "minny-rl" / time.strftime("rl-%Y%m%d-%H%M"))
    ap.add_argument("--max-tokens", type=int, default=10240, help="drop prompts longer than this")
    ap.add_argument("--max-completion", type=int, default=300, help="the teacher's longest step is 257 tokens")
    ap.add_argument("--num-generations", type=int, default=8)
    ap.add_argument("--batch", type=int, default=4, help="per-device micro-batch (completions)")
    ap.add_argument("--grad-accum", type=int, default=2, help="batch × accum must be a multiple of --num-generations")
    ap.add_argument("--max-steps", type=int, default=300)
    ap.add_argument("--eval-steps", type=int, default=100, help="checkpoint and dev-reward curve every N steps")
    ap.add_argument("--eval-examples", type=int, default=48,
                    help="dev prompts for the in-loop reward curve; each costs --num-generations samples, so 48 every "
                         "100 steps is ~10 min per evaluation; the full dev set is scored offline afterwards")
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--beta", type=float, default=0.02, help="KL to the start model; TRL's default is 0")
    ap.add_argument("--loss-type", default="dapo", help="dapo (TRL default), grpo or dr_grpo")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--lora-r", type=int, default=32)
    ap.add_argument("--vllm-mode", default="colocate", choices=["colocate", "server"])
    ap.add_argument("--vllm-gpu-mem", type=float, default=0.3, help="colocate: share of the card for vLLM")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if (args.batch * args.grad_accum) % (4 if args.dry_run else args.num_generations):
        raise SystemExit("--batch × --grad-accum must be a multiple of --num-generations (whole groups per step)")
    out = args.out
    if not args.check:
        out.mkdir(parents=True, exist_ok=True)       # --check writes nothing

    vocab = rr.Vocab.load(args.prompts / "vocab.json") if (args.prompts / "vocab.json").exists() else None
    train_rows = load_prompts(args.prompts, args.max_tokens, "train")
    dev_rows = load_prompts(args.prompts, args.max_tokens, "dev")
    summary = {"args": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
               "prompts": {"train": len(train_rows), "dev": len(dev_rows),
                           "train_tokens": sum(r["n_tokens"] for r in train_rows)}}
    if not train_rows:
        raise SystemExit(f"no prompts under {args.prompts}")

    if args.check:
        summary["teacher_rewards"] = {"train": score_split(train_rows[:400], vocab), "dev": score_split(dev_rows, vocab)}
        try:
            from trl import GRPOConfig
            cfg = grpo_config(GRPOConfig, **training_kwargs(args, out, False), use_cpu=True)
            summary["grpo_config_ok"] = {k: getattr(cfg, k) for k in ("num_generations", "beta", "loss_type", "max_completion_length", "warmup_steps", "vllm_mode")}
        except ImportError as e:
            summary["grpo_config_ok"] = f"trl not importable here ({e})"
        print(json.dumps(summary, indent=1, default=str))
        return

    if not args.base:
        raise SystemExit("--base: the merged SFT model directory to start from")
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoTokenizer
    from trl import GRPOConfig, GRPOTrainer
    tok = AutoTokenizer.from_pretrained(args.base)
    tok = getattr(tok, "tokenizer", tok)
    cfg = grpo_config(GRPOConfig, **training_kwargs(args, out, args.dry_run))
    peft_cfg = LoraConfig(r=args.lora_r, lora_alpha=2 * args.lora_r, lora_dropout=0.0, bias="none",
                          target_modules=TARGET_MODULES, task_type="CAUSAL_LM")
    random.Random(args.seed).shuffle(train_rows)
    train_ds = Dataset.from_list(train_rows[:64] if args.dry_run else train_rows)
    dev_ds = Dataset.from_list(random.Random(args.seed).sample(dev_rows, k=min(args.eval_examples, len(dev_rows))))
    trainer = GRPOTrainer(model=args.base, reward_funcs=[make_reward_fn(vocab)], args=cfg,
                          train_dataset=train_ds, eval_dataset=dev_ds, processing_class=tok, peft_config=peft_cfg)
    torch.cuda.reset_peak_memory_stats()
    t0 = time.monotonic()
    result = trainer.train()
    summary["train"] = {"seconds": round(time.monotonic() - t0), "metrics": result.metrics,
                        "peak_gpu_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2),
                        "log_history": trainer.state.log_history[-300:]}
    if args.dry_run:
        print(json.dumps({k: v for k, v in summary["train"].items() if k != "log_history"}, indent=1, default=str))
        print(f"dry run: {summary['train']['peak_gpu_gb']} GB peak")
        return
    adapter = out / "adapter"
    trainer.model.save_pretrained(adapter)
    tok.save_pretrained(adapter)
    (out / "train-summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(f"adapter → {adapter}; summary → {out}/train-summary.json\n"
          f"next: ./sft_train.py --merge-only {adapter} --out {out} --base {args.base}")


if __name__ == "__main__":
    main()
