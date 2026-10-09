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
from collections import Counter
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
# Qwen3.5's served/trained chat template is the Unsloth one in sft/, NOT the
# base repo's HF chat_template.jinja (they differ in the thinking default and
# tool-call rendering). merge() embeds this so the GGUF matches what :8001
# serves and what the data was rendered with. chat_template.jinja is therefore
# NOT copied from the base below.
SERVED_TEMPLATE = Path(__file__).resolve().parent / "sft" / "chat_template.qwen3.5-served.jinja"
BASE_FILES = ("preprocessor_config.json", "video_preprocessor_config.json")


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
def _tokenizer_of(tok):
    """A multimodal checkpoint comes with a Processor (images, text); its
    text tokenizer is what this text-only training wants — a Processor
    called on a string tries to decode it as an image."""
    return tok.tokenizer if hasattr(tok, "tokenizer") else tok


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
            return model, _tokenizer_of(tok)
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
    return _peft(model, args), _tokenizer_of(tok)


def _peft(model, args):
    from peft import LoraConfig, get_peft_model
    cfg = LoraConfig(r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=0.0, bias="none",
                     target_modules=TARGET_MODULES, task_type="CAUSAL_LM")
    model = get_peft_model(model, cfg)
    return model


class StepTimer:
    """Seconds per optimiser step; the last one is the rate, the first one
    is kernel compilation (534 s against 57 s on the laptop)."""
    def __init__(self):
        self.t0, self.seconds = None, []

    def on_step_begin(self, args, state, control, **kw):
        self.t0 = time.monotonic()

    def on_step_end(self, args, state, control, **kw):
        self.seconds.append(round(time.monotonic() - self.t0, 1))


def trainable_report(model) -> dict:
    total = trainable = 0
    for p in model.parameters():
        total += p.numel()
        trainable += p.numel() if p.requires_grad else 0
    return {"trainable": trainable, "total": total, "share": round(trainable / max(total, 1), 5)}


# ---- config ------------------------------------------------------------------------------------
# transformers 5 dropped warmup_ratio: warmup_steps takes a float ratio instead.
RENAMES = {"max_length": ["max_seq_length"], "eval_strategy": ["evaluation_strategy"],
           "warmup_ratio": ["warmup_steps"]}


def training_kwargs(args, out: Path, dry: bool, has_dev: bool) -> dict:
    """What SFTConfig is asked for; sft_config() maps the names."""
    return dict(
        output_dir=str(out / "checkpoints"), seed=args.seed, report_to="none",
        disable_tqdm=True,                       # a nohup log wants lines, not a bar rewritten in place
        per_device_train_batch_size=1, per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.grad_accum,
        num_train_epochs=args.epochs, max_steps=3 if dry else -1,
        learning_rate=args.lr, lr_scheduler_type="cosine", warmup_ratio=0.03,
        weight_decay=0.0, max_grad_norm=1.0, bf16=True,
        optim="adamw_8bit" if not args.bf16_lora else "adamw_torch",
        logging_steps=1 if dry else 10,
        # No in-loop eval: a dev forward on Qwen3.5's 248k-vocab logit tensor at long
        # context OOMs a 16 GB card (accelerate upcasts [1, seq, 248k] to fp32). Instead
        # save checkpoints through the run and score them afterwards — see --pick-best.
        eval_strategy="no",
        save_strategy="no" if dry else "steps", save_steps=args.eval_steps, save_total_limit=args.keep,
        load_best_model_at_end=False,            # nothing to select on in-loop; --pick-best does it after
        max_length=args.max_tokens, packing=False, completion_only_loss=True,
        dataset_num_proc=2, gradient_checkpointing=args.no_unsloth,
    )


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
def embed_served_template(dest: Path) -> None:
    """Write the served/trained chat template into the merged model. The base's
    HF chat_template.jinja is not what :8001 serves or what the data was rendered
    with; convert_hf_to_gguf reads tokenizer_config.json's chat_template first and
    only then chat_template.jinja (llama.cpp gguf/vocab.py), so set both — else the
    GGUF carries the wrong template and the served fine-tune mismatches training."""
    if not SERVED_TEMPLATE.is_file():
        print(f"  WARNING: served template {SERVED_TEMPLATE} missing; the GGUF will carry the "
              f"base's HF template — re-embed the served one before serving", file=sys.stderr)
        return
    tmpl = SERVED_TEMPLATE.read_text(encoding="utf-8")
    (dest / "chat_template.jinja").write_text(tmpl, encoding="utf-8")
    tc = dest / "tokenizer_config.json"
    if tc.is_file():
        cfg = json.loads(tc.read_text(encoding="utf-8"))
        cfg["chat_template"] = tmpl
        tc.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  chat template: embedded served template ({len(tmpl)} B) from {SERVED_TEMPLATE.name}", file=sys.stderr)


def merge(args, adapter: Path, out: Path) -> Path:
    """The adapter applied to a fresh bf16 base — never to the dequantised
    4-bit weights the adapter was trained against — saved as merged/ with
    the tokenizer, the served chat template and the base's preprocessor files,
    ready for convert_hf_to_gguf.py."""
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
    embed_served_template(dest)
    for name in BASE_FILES:
        try:
            shutil.copy(hf_hub_download(args.base, name), dest / name)
        except Exception as e:  # noqa: BLE001  a base without that file
            print(f"  {name}: not copied ({e})", file=sys.stderr)
    return dest


# ---- checkpoint scoring ------------------------------------------------------------------------
def _dev_loss(model, tok, rows, max_ctx: int) -> float:
    """Mean per-token NLL on the completion only. The LM head runs on the
    completion span alone (logits_to_keep), so the [seq, 248k-vocab] logits that
    OOM the in-loop eval are never built. Sequences longer than max_ctx keep
    their tail — the completion and as much preceding prompt as fits — so this is
    a consistent score for ranking checkpoints, not the exact training loss."""
    import torch
    import torch.nn.functional as F
    model.eval()
    tot_loss, tot_tok = 0.0, 0
    for r in rows:
        p = tok(r["prompt"], add_special_tokens=False).input_ids
        full = tok(r["prompt"] + r["completion"], add_special_tokens=False).input_ids
        ids = full[-max_ctx:] if len(full) > max_ctx else full      # keep the tail → keep the completion
        n_comp = min(len(full) - len(p), len(ids) - 1)              # ≥1 token must precede the completion
        if n_comp <= 0:
            continue
        input_ids = torch.tensor([ids], device=model.device)
        with torch.no_grad():
            try:
                logits = model(input_ids=input_ids, logits_to_keep=n_comp + 1).logits
            except TypeError:                                       # older forward without logits_to_keep
                logits = model(input_ids=input_ids).logits[:, -(n_comp + 1):, :]
        pred = logits[:, :-1, :].reshape(-1, logits.size(-1))       # positions predicting each completion token
        targets = input_ids[:, -n_comp:].reshape(-1)
        for i in range(0, pred.size(0), 256):                       # chunk the fp32 upcast to bound memory
            tot_loss += F.cross_entropy(pred[i:i + 256].float(), targets[i:i + 256], reduction="sum").item()
        tot_tok += n_comp
    return tot_loss / max(tot_tok, 1)


def checkpoints_under(out: Path) -> list[Path]:
    """Saved checkpoints oldest-first, plus out/adapter (the end-of-run state)."""
    ckpts = sorted((out / "checkpoints").glob("checkpoint-*"),
                   key=lambda p: int(p.name.split("-")[1]) if p.name.split("-")[1].isdigit() else 0)
    if (out / "adapter" / "adapter_config.json").exists():
        ckpts.append(out / "adapter")
    return ckpts


def score_checkpoints(args, out: Path) -> Path | None:
    """Load a clean 4-bit base once, score every checkpoint's dev loss by swapping
    its adapter in, and return the best (lowest-loss) adapter directory."""
    import torch
    from peft import PeftModel
    from transformers import AutoModelForImageTextToText, AutoTokenizer, BitsAndBytesConfig
    ckpts = checkpoints_under(out)
    if not ckpts:
        print(f"no checkpoints under {out/'checkpoints'}", file=sys.stderr)
        return None
    dev_rows = [r for run in args.run for r in load_examples(run / "train" / "dev.jsonl", args.max_tokens)[0]]
    rows = random.Random(args.seed).sample(dev_rows, k=min(args.eval_examples, len(dev_rows)))
    max_ctx = args.eval_max_tokens or args.max_tokens
    tok = _tokenizer_of(AutoTokenizer.from_pretrained(args.base))
    base = AutoModelForImageTextToText.from_pretrained(
        args.base, torch_dtype=torch.bfloat16, device_map={"": 0},
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16))
    print(f"scoring {len(ckpts)} checkpoints on {len(rows)} dev examples (context ≤{max_ctx})", file=sys.stderr)
    pm, scores = None, {}
    for i, ckpt in enumerate(ckpts):
        name = f"c{i}"
        if pm is None:
            pm = PeftModel.from_pretrained(base, str(ckpt), adapter_name=name)
        else:
            pm.load_adapter(str(ckpt), adapter_name=name)
        pm.set_adapter(name)
        scores[str(ckpt)] = _dev_loss(pm, tok, rows, max_ctx)
        print(f"  {ckpt.name}: dev_loss {scores[str(ckpt)]:.4f}", file=sys.stderr)
        if name != "c0":
            pm.delete_adapter(name)                                 # keep only one extra adapter resident
    best = min(scores, key=scores.get)
    (out / "pick-best.json").write_text(json.dumps(
        {"eval_max_tokens": max_ctx, "eval_examples": len(rows), "scores": scores, "best": best}, indent=1))
    print(f"best: {Path(best).name} (dev_loss {scores[best]:.4f}) → {out}/pick-best.json")
    del pm, base
    torch.cuda.empty_cache()
    return Path(best)


# ---- main ----------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", type=Path, nargs="+", default=[DEFAULT_RUN],
                    help="one or more sft_gen runs; reads each <run>/train/ (train and dev concatenated)")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--out", type=Path, default=Path.home() / "minny-sft" / time.strftime("sft-%Y%m%d-%H%M"))
    ap.add_argument("--max-tokens", type=int, default=16384, help="drop longer examples; also the sequence cap")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, help="default 2e-4 for QLoRA, 1e-4 for bf16 LoRA")
    ap.add_argument("--grad-accum", type=int, default=8, help="effective batch (micro-batch is 1)")
    ap.add_argument("--lora-r", type=int, default=32)
    ap.add_argument("--lora-alpha", type=int, help="default 2 × r")
    ap.add_argument("--eval-steps", type=int, default=200, help="save a checkpoint every N steps")
    ap.add_argument("--keep", type=int, default=8, help="save_total_limit: how many recent checkpoints to keep for --pick-best")
    ap.add_argument("--eval-examples", type=int, default=60,
                    help="dev examples used to score checkpoints in --pick-best")
    ap.add_argument("--pick-best", action="store_true",
                    help="after training, score every kept checkpoint's dev loss and merge/keep the best")
    ap.add_argument("--pick-best-only", type=Path, metavar="OUT",
                    help="skip training: score the checkpoints already under OUT/checkpoints and report the best")
    ap.add_argument("--eval-max-tokens", type=int, default=0,
                    help="context cap when scoring checkpoints (0 = use --max-tokens); lower it only if scoring OOMs")
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
        if not (args.check or args.merge_only or args.no_unsloth):
            import unsloth  # noqa: F401  before transformers, or its patches are skipped (it warns)
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

    if args.pick_best_only:
        best = score_checkpoints(args, args.pick_best_only)
        if best and args.merge:
            dest = merge(args, best, args.pick_best_only)
            print(f"merged {best.name} → {dest}\nnext: python convert_hf_to_gguf.py {dest} --outtype q8_0 --outfile {args.pick_best_only}/qwen3.5-9b-sft.Q8_0.gguf")
        return

    train_rows, dev_rows = [], []
    train_stats, dev_stats = Counter(), Counter()
    for run in args.run:
        tr, ts = load_examples(run / "train" / "train.jsonl", args.max_tokens)
        dv, ds = load_examples(run / "train" / "dev.jsonl", args.max_tokens)
        train_rows += tr
        dev_rows += dv
        train_stats.update(ts)
        dev_stats.update(ds)
    train_stats, dev_stats = dict(train_stats), dict(dev_stats)
    data = args.run[0] / "train"
    summary = {"args": {k: [str(x) for x in v] if isinstance(v, list) else str(v) if isinstance(v, Path) else v
                        for k, v in vars(args).items()},
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
        print(json.dumps(summary, indent=1, default=str))
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
    cfg = sft_config(SFTConfig, **training_kwargs(args, out, dry, dev_ds is not None))
    from transformers import TrainerCallback
    timer = StepTimer()
    callback = type("StepTimerCallback", (TrainerCallback,), {"on_step_begin": lambda self, *a, **k: timer.on_step_begin(*a, **k),
                                                            "on_step_end": lambda self, *a, **k: timer.on_step_end(*a, **k)})()
    trainer = SFTTrainer(model=model, processing_class=tok, args=cfg, train_dataset=train_ds, eval_dataset=None,
                         callbacks=[callback])
    if not getattr(trainer, "completion_only_loss", True):
        raise SystemExit("the trainer did not take completion_only_loss; the prompt would be trained on")

    torch.cuda.reset_peak_memory_stats()
    t0 = time.monotonic()
    result = trainer.train(resume_from_checkpoint=str(args.resume) if args.resume else None)
    summary["train"] = {"seconds": round(time.monotonic() - t0), "metrics": result.metrics,
                        "peak_gpu_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2),
                        "step_seconds": timer.seconds[:50], "log_history": trainer.state.log_history[-200:]}
    if dry:
        mean_tokens = train_stats["tokens"] / len(train_rows)
        last = timer.seconds[-1] if timer.seconds else (result.metrics.get("train_runtime") or 1.0)
        tps = args.grad_accum * mean_tokens / last
        steps = len(train_rows) // args.grad_accum
        print(json.dumps({k: v for k, v in summary["train"].items() if k != "log_history"}, indent=1))
        print(f"dry run: {summary['train']['peak_gpu_gb']} GB peak for {args.max_tokens}-token sequences; "
              f"steps took {timer.seconds} s (the first is kernel compilation); at the last step's rate "
              f"~{tps:.0f} training tokens/s → ~{steps * last / 3600:.1f} h per epoch of {steps} steps, "
              f"{train_stats['tokens'] / 1e6:.1f} M tokens")
        return
    adapter = out / "adapter"
    model.save_pretrained(adapter)
    tok.save_pretrained(adapter)
    (out / "train-summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(f"adapter → {adapter}; summary → {out}/train-summary.json")
    chosen = adapter
    if args.pick_best or args.merge:
        del trainer, model                       # free the training model before a clean base is loaded
        torch.cuda.empty_cache()
    if args.pick_best:
        chosen = score_checkpoints(args, out) or adapter
    if args.merge:
        dest = merge(args, chosen, out)
        print(f"merged {chosen.name} → {dest}\nnext: python convert_hf_to_gguf.py {dest} --outtype q8_0 --outfile {out}/qwen3.5-9b-sft.Q8_0.gguf")


if __name__ == "__main__":
    main()
