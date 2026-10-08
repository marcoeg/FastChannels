#!/usr/bin/env python3
"""Train the local brain's LoRA on the prepared examples — QLoRA by default.

Input is what sft_train_prep.py wrote (<run>/train/): train.jsonl and
dev.jsonl of {"prompt", "completion", "n_tokens", …}, already rendered
with the served chat template. No template is applied here; the prompt is
fed as text and the loss is on the completion only (TRL's
prompt-completion format with completion_only_loss).

The model, Qwen/Qwen3.5-9B, is Qwen3_5ForConditionalGeneration: a vision
tower plus a 32-layer text model of which 24 layers are gated-delta linear
attention (in_proj_qkv, in_proj_z, out_proj) and 8 full attention
(q/k/v/o_proj), MLPs everywhere. The LoRA goes on those projections inside
model.language_model only (TARGET_MODULES); the vision tower and the MTP
head stay frozen. The usual "q,k,v,o" list would miss three quarters of
the attention layers.

Modes:
  --check      load the data and the tokenizer, validate, print the stats; no model
  --dry-run    load the model, run two optimiser steps, print peak memory
  (default)    train; evaluates dev loss and saves every --eval-steps, keeps the best
  --merge      after training (or --merge-only ADAPTER): apply the adapter to a
               fresh bf16 base and save merged/ for llama.cpp's converter

Stack: Unsloth (FastModel) + TRL SFTTrainer; --no-unsloth uses transformers +
peft (+ bitsandbytes for 4-bit). The hybrid architecture wants the
flash-linear-attention and causal-conv1d kernels installed, else the linear
attention layers run on slow fallbacks. CUDA only. Not run on this box.

Memory, rough: QLoRA keeps the base at ~5 GB; at 16k tokens with Unsloth's
checkpointing expect ~12–16 GB in all — on a 16 GB card use --max-tokens
12288 (the examples carry n_tokens). bf16 LoRA (--bf16-lora) is 18 GB of
weights before activations: a 24 GB card at least.

  ./sft_train.py --check
  ./sft_train.py --dry-run
  ./sft_train.py --out ~/minny-sft/sft1                   # ~2 epochs, hours
  ./sft_train.py --merge-only ~/minny-sft/sft1/adapter --out ~/minny-sft/sft1
  python llama.cpp/convert_hf_to_gguf.py ~/minny-sft/sft1/merged --outtype q8_0 \\
      --outfile ~/minny-sft/sft1/qwen3.5-9b-sft1-Q8_0.gguf
  llama-server -m … --alias qwen3.5-9b-sft1 --jinja -c 32768 -np 4 --port 8002
  ./eval_suite.py --models gpt-5.6-terra,qwen3.5-9b,qwen3.5-9b-sft1       # the gate
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import random
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

DEFAULT_RUN = Path.home() / "news" / "voice-sft" / "run1"
DEFAULT_BASE = "Qwen/Qwen3.5-9B"
END = "<|im_end|>"
# Module names from the Hub's model.safetensors.index.json (2026-10-07): the
# language model's big projections, full-attention and linear-attention
# layers alike, and the MLPs. in_proj_a / in_proj_b are tiny gate projections
# and are left alone. A regex, matched in full against module names by peft.
TARGET_MODULES = (r"model\.language_model\.layers\.\d+\."
                  r"(self_attn\.(q_proj|k_proj|v_proj|o_proj)"
                  r"|linear_attn\.(in_proj_qkv|in_proj_z|out_proj)"
                  r"|mlp\.(gate_proj|up_proj|down_proj))")
BASE_FILES = ("chat_template.jinja", "preprocessor_config.json", "video_preprocessor_config.json")


# ---- data ----------------------------------------------------------------------------------
def load_examples(path: Path, max_tokens: int) -> tuple[list[dict], dict]:
    rows, stats = [], {"read": 0, "over_max_tokens": 0, "bad_end": 0}
    for line in path.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        stats["read"] += 1
        if not e["completion"].endswith(END):
            stats["bad_end"] += 1
            continue
        if e["n_tokens"] > max_tokens:
            stats["over_max_tokens"] += 1
            continue
        rows.append({"prompt": e["prompt"], "completion": e["completion"], "n_tokens": e["n_tokens"]})
    stats["kept"] = len(rows)
    stats["tokens"] = sum(r["n_tokens"] for r in rows)
    return rows, stats


def check_tokenizer(tok, rows: list[dict], sample: int, seed: int) -> dict:
    """The loaded tokenizer must see what the prep saw: the same length, no
    token added at the front, and the prompt a clean prefix of the whole."""
    out = {"checked": 0, "length_mismatch": 0, "boundary_mismatch": 0}
    for r in random.Random(seed).sample(rows, k=min(sample, len(rows))):
        p = tok(r["prompt"], add_special_tokens=False).input_ids
        both = tok(r["prompt"] + r["completion"], add_special_tokens=False).input_ids
        out["checked"] += 1
        out["length_mismatch"] += len(both) != r["n_tokens"]
        out["boundary_mismatch"] += both[:len(p)] != p
    if tok("x").input_ids != tok("x", add_special_tokens=False).input_ids:
        out["adds_special_tokens"] = True            # TRL tokenises with the default; the prep did not
    out["eos_token"] = tok.eos_token
    out["eos_is_end"] = tok.eos_token == END
    return out


# ---- model ---------------------------------------------------------------------------------
def load_model(args):
    """(model with LoRA attached, tokenizer)."""
    if not args.no_unsloth:
        try:
            from unsloth import FastModel            # before transformers: it patches them
            model, tok = FastModel.from_pretrained(
                model_name=args.base, max_seq_length=args.max_tokens,
                load_in_4bit=not args.bf16_lora, load_in_8bit=False, dtype=None, full_finetuning=False)
            try:
                model = FastModel.get_peft_model(
                    model, r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=0.0, bias="none",
                    target_modules=TARGET_MODULES, use_gradient_checkpointing="unsloth",
                    random_state=args.seed, finetune_vision_layers=False, finetune_language_layers=True)
            except (TypeError, ValueError) as e:     # a version that wants its own target list
                print(f"unsloth get_peft_model refused the target regex ({e}); using peft directly", file=sys.stderr)
                model = _peft(model, args)
            return model, tok
        except ImportError as e:
            print(f"unsloth not usable ({e}); falling back to transformers + peft", file=sys.stderr)
    import torch
    from transformers import AutoModelForImageTextToText, AutoTokenizer
    kw = {"torch_dtype": torch.bfloat16, "device_map": {"": 0}}
    if not args.bf16_lora:
        from transformers import BitsAndBytesConfig
        kw["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16)
    model = AutoModelForImageTextToText.from_pretrained(args.base, **kw)
    tok = AutoTokenizer.from_pretrained(args.base)
    if not args.bf16_lora:
        from peft import prepare_model_for_kbit_training
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    else:
        model.gradient_checkpointing_enable()
    return _peft(model, args), tok


def _peft(model, args):
    from peft import LoraConfig, get_peft_model
    cfg = LoraConfig(r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=0.0, bias="none",
                     target_modules=TARGET_MODULES, task_type="CAUSAL_LM")
    model = get_peft_model(model, cfg)
    return model


def trainable_report(model) -> dict:
    total = trainable = 0
    for p in model.parameters():
        total += p.numel()
        trainable += p.numel() if p.requires_grad else 0
    return {"trainable": trainable, "total": total, "share": round(trainable / max(total, 1), 5)}


# ---- config ------------------------------------------------------------------------------------
RENAMES = {"max_length": ["max_seq_length"], "eval_strategy": ["evaluation_strategy"]}


def sft_config(SFTConfig, **wanted):
    """SFTConfig across TRL versions: known renames are tried, anything else
    unknown is an error rather than a silently dropped setting."""
    names = {f.name for f in dataclasses.fields(SFTConfig)}
    kw = {}
    for k, v in wanted.items():
        for cand in [k] + RENAMES.get(k, []):
            if cand in names:
                kw[cand] = v
                break
        else:
            raise SystemExit(f"this TRL's SFTConfig has no {k!r} (nor {RENAMES.get(k, [])}); pin TRL or adapt sft_config()")
    return SFTConfig(**kw)


# ---- merge -------------------------------------------------------------------------------------
def merge(args, adapter: Path, out: Path) -> Path:
    """The adapter applied to a fresh bf16 base — never to the dequantised
    4-bit weights the adapter was trained against — saved as merged/ with
    the tokenizer and the base's template and preprocessor files, ready for
    convert_hf_to_gguf.py."""
    import torch
    from huggingface_hub import hf_hub_download
    from peft import PeftModel
    from transformers import AutoModelForImageTextToText, AutoTokenizer
    dest = out / "merged"
    print(f"merging {adapter} into {args.base} on {args.merge_device} → {dest}", file=sys.stderr)
    base = AutoModelForImageTextToText.from_pretrained(args.base, torch_dtype=torch.bfloat16, device_map=args.merge_device)
    model = PeftModel.from_pretrained(base, str(adapter))
    model = model.merge_and_unload()
    dest.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(dest, safe_serialization=True, max_shard_size="4GB")
    AutoTokenizer.from_pretrained(args.base).save_pretrained(dest)
    for name in BASE_FILES:
        try:
            shutil.copy(hf_hub_download(args.base, name), dest / name)
        except Exception as e:  # noqa: BLE001  a base without that file
            print(f"  {name}: not copied ({e})", file=sys.stderr)
    return dest


# ---- main ----------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", type=Path, default=DEFAULT_RUN, help="an sft_gen run; reads <run>/train/")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--out", type=Path, default=Path.home() / "minny-sft" / time.strftime("sft-%Y%m%d-%H%M"))
    ap.add_argument("--max-tokens", type=int, default=16384, help="drop longer examples; also the sequence cap")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, help="default 2e-4 for QLoRA, 1e-4 for bf16 LoRA")
    ap.add_argument("--grad-accum", type=int, default=8, help="effective batch (micro-batch is 1)")
    ap.add_argument("--lora-r", type=int, default=32)
    ap.add_argument("--lora-alpha", type=int, help="default 2 × r")
    ap.add_argument("--eval-steps", type=int, default=200, help="dev loss and a checkpoint every N steps")
    ap.add_argument("--eval-examples", type=int, default=60,
                    help="dev examples for the periodic eval (a full dev pass is ~2 M tokens); the final eval uses all of dev")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--bf16-lora", action="store_true", help="LoRA on the bf16 base instead of QLoRA (24 GB+)")
    ap.add_argument("--no-unsloth", action="store_true", help="transformers + peft (+ bitsandbytes) instead of Unsloth")
    ap.add_argument("--resume", type=Path, help="a checkpoint directory to resume from")
    ap.add_argument("--check", action="store_true", help="data + tokenizer only")
    ap.add_argument("--dry-run", action="store_true", help="two steps, report memory")
    ap.add_argument("--merge", action="store_true", help="after training, write merged/ (bf16)")
    ap.add_argument("--merge-only", type=Path, metavar="ADAPTER", help="only merge this adapter into --out/merged")
    ap.add_argument("--merge-device", default="cpu", help="cpu (needs ~20 GB RAM) or cuda")
    args = ap.parse_args()
    try:
        import transformers  # noqa: F401  the shebang takes whatever python3 the shell has
    except ImportError as e:
        raise SystemExit(f"transformers is not importable from {sys.executable} ({e});\n"
                         "activate the training venv first: source ~/minny-sft/.venv/bin/activate")
    args.lora_alpha = args.lora_alpha or 2 * args.lora_r
    args.lr = args.lr or (1e-4 if args.bf16_lora else 2e-4)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    if args.merge_only:
        dest = merge(args, args.merge_only, out)
        print(f"merged → {dest}\nnext: python convert_hf_to_gguf.py {dest} --outtype q8_0 --outfile {out}/qwen3.5-9b-sft.Q8_0.gguf")
        return

    data = args.run / "train"
    train_rows, train_stats = load_examples(data / "train.jsonl", args.max_tokens)
    dev_rows, dev_stats = load_examples(data / "dev.jsonl", args.max_tokens)
    summary = {"args": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
               "data": {"train": train_stats, "dev": dev_stats}, "target_modules": TARGET_MODULES}
    print(json.dumps(summary["data"]), file=sys.stderr)
    if not train_rows:
        raise SystemExit(f"no training examples under {data}")

    if args.check:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(args.base)
        summary["tokenizer"] = check_tokenizer(tok, train_rows + dev_rows, 200, args.seed)
        steps = int(len(train_rows) / args.grad_accum * args.epochs)
        summary["plan"] = {"optimiser_steps": steps, "steps_per_epoch": len(train_rows) // args.grad_accum,
                           "evals": steps // args.eval_steps, "train_tokens_per_epoch": train_stats["tokens"]}
        print(json.dumps(summary, indent=1))
        bad = summary["tokenizer"]
        if bad["length_mismatch"] or bad["boundary_mismatch"] or bad.get("adds_special_tokens") or not bad["eos_is_end"]:
            raise SystemExit("tokenizer check failed — the prep and this tokenizer disagree; do not train on this")
        return

    import torch
    from datasets import Dataset
    model, tok = load_model(args)
    summary["tokenizer"] = check_tokenizer(tok, train_rows + dev_rows, 50, args.seed)
    summary["parameters"] = trainable_report(model)
    print(json.dumps({"tokenizer": summary["tokenizer"], "parameters": summary["parameters"]}), file=sys.stderr)
    if summary["tokenizer"]["boundary_mismatch"] or not summary["tokenizer"]["eos_is_end"]:
        raise SystemExit("tokenizer check failed; see --check")

    from trl import SFTConfig, SFTTrainer
    train_ds = Dataset.from_list([{"prompt": r["prompt"], "completion": r["completion"]} for r in train_rows])
    dev_ds = Dataset.from_list([{"prompt": r["prompt"], "completion": r["completion"]} for r in dev_rows]) if dev_rows else None
    dry = args.dry_run
    cfg = sft_config(
        SFTConfig,
        output_dir=str(out / "checkpoints"), seed=args.seed, report_to="none",
        per_device_train_batch_size=1, per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.grad_accum,
        num_train_epochs=args.epochs, max_steps=2 if dry else -1,
        learning_rate=args.lr, lr_scheduler_type="cosine", warmup_ratio=0.03,
        weight_decay=0.0, max_grad_norm=1.0, bf16=True,
        optim="adamw_8bit" if not args.bf16_lora else "adamw_torch",
        logging_steps=1 if dry else 10,
        eval_strategy="no" if dry or dev_ds is None else "steps", eval_steps=args.eval_steps,
        save_strategy="no" if dry else "steps", save_steps=args.eval_steps, save_total_limit=3,
        load_best_model_at_end=not dry and dev_ds is not None, metric_for_best_model="eval_loss", greater_is_better=False,
        max_length=args.max_tokens, packing=False, completion_only_loss=True,
        dataset_num_proc=2, gradient_checkpointing=args.no_unsloth,
    )
    dev_sub = (dev_ds.shuffle(seed=args.seed).select(range(min(args.eval_examples, len(dev_ds))))
               if dev_ds is not None else None)
    trainer = SFTTrainer(model=model, processing_class=tok, args=cfg, train_dataset=train_ds, eval_dataset=dev_sub)
    if not getattr(trainer, "completion_only_loss", True):
        raise SystemExit("the trainer did not take completion_only_loss; the prompt would be trained on")

    torch.cuda.reset_peak_memory_stats()
    t0 = time.monotonic()
    result = trainer.train(resume_from_checkpoint=str(args.resume) if args.resume else None)
    summary["train"] = {"seconds": round(time.monotonic() - t0), "metrics": result.metrics,
                        "peak_gpu_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2),
                        "log_history": trainer.state.log_history[-200:]}
    if dry:
        # the projection is pessimistic: two steps carry the warm-up and kernel compilation
        mean_tokens = train_stats["tokens"] / len(train_rows)
        runtime = result.metrics.get("train_runtime") or 1.0
        tps = 2 * args.grad_accum * mean_tokens / runtime
        print(json.dumps({k: v for k, v in summary["train"].items() if k != "log_history"}, indent=1))
        print(f"dry run: {summary['train']['peak_gpu_gb']} GB peak for {args.max_tokens}-token sequences; "
              f"~{tps:.0f} training tokens/s → ~{train_stats['tokens'] / tps / 3600:.1f} h per epoch "
              f"of {train_stats['tokens'] / 1e6:.1f} M tokens (an upper bound: the first steps include warm-up)")
        return
    if dev_ds is not None:
        summary["train"]["final_eval"] = trainer.evaluate(dev_ds)
    adapter = out / "adapter"
    model.save_pretrained(adapter)
    tok.save_pretrained(adapter)
    (out / "train-summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(f"adapter → {adapter}; summary → {out}/train-summary.json")
    if args.merge:
        del trainer, model
        torch.cuda.empty_cache()
        dest = merge(args, adapter, out)
        print(f"merged → {dest}\nnext: python convert_hf_to_gguf.py {dest} --outtype q8_0 --outfile {out}/qwen3.5-9b-sft.Q8_0.gguf")


if __name__ == "__main__":
    main()
