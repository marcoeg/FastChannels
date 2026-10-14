# Prime Intellect `verifiers` + `prime-rl` as the RLVR platform — evaluation

*2026-10-14. Companion to `SFT-DATA.md` (what the three TRL runs taught) and
`SFT-RUNBOOK.md` ("RLVR on the rented H200"). Nothing here has run yet; every
API claim below was checked against the installed `verifiers 0.4.0` and the
upstream sources listed in the appendix, and every budget number is an estimate
to be calibrated by the dry run.*

## Verdict in six lines

- **Adopt `verifiers` for the task/eval layer now, at zero GPU cost.** It gives
  the one thing our TRL loop structurally lacked: the policy plays the *whole
  turn* — picks a tool, gets the real result, speaks — so routing is judged on
  the policy's own calls (run 1's hole closes by construction), and the same
  taskset serves eval and training.
- **Use `prime-rl` for the next metered run instead of `sft_rl.py`,** but only
  after a free "is there a gradient?" gate (run 2.5's lesson, made a number).
- Its renderers solve, by design, the template-drift class of bugs we fought by
  hand in SFT (`False`/`false` round-trip = BUGLIST T8, think-block stripping at
  turn boundaries, served-vs-training template parity).
- Real batch sizes (16–64 prompts × 8 rollouts per step, versus TRL's **one
  prompt per optimiser step** in runs 1–2.5) are the cure for the noise and the
  reason a run costs 3–5× more. Same money still buys a cleaner TRL-scale run.
- What it does not change: rewards are still code, and code still cannot grade
  the wording a gate-3 criterion wants or the name "Minny" — those remain sft3's
  job (`SFT-RUNBOOK.md`, "What RL can and cannot fix here").
- Keep `sft_rl.py` + the H200 runbook as the fallback until a prime-rl run has
  produced a gated checkpoint.

## 1. The two pieces, in our words

**`verifiers` (0.4.0, the `verifiers.v1` API)** is the environment layer: a
*taskset* yields *tasks*; each task has data (`prompt` as a string or full
chat `Messages`, an optional `system_prompt`, plus any fields we add), reward
methods (`@vf.reward(weight=…)`, summed into `trace.reward`), metrics
(`@vf.metric`, recorded not summed), and stop predicates (`@vf.stop`). Tools
are *toolsets*: Python classes whose `@vf.tool` methods are served to the model
as an MCP server — launched per rollout (`Task.toolsets`) or shared per worker
(`Taskset.toolsets`), or **attached to an already-running MCP server by URL**
(`ToolsetConfig.url`; confirmed in `mcp/launch.py`: "already running remotely;
nothing launched"). The default env (`SingleAgentEnv`) runs one agent through
a *harness*; the `null` harness is a plain OpenAI chat-completions tool loop,
which is what our agent is. An env can script the user: `alphabet-sort` replays
pre-generated user turns through `agents.agent.interaction(task)` /
`interaction.turn(text)`, which is exactly a recorded viewer conversation.
`vf-eval <taskset> -m MODEL -r N --client.base-url … --client.api-key-var …
--no-push` runs the taskset against any OpenAI-compatible server (our laptop
`llama-server` on `:8002`, untouched) and writes `outputs/…/traces.jsonl`.

**`prime-rl`** is the trainer: asynchronous RL with three processes — a vLLM
inference server (`uv run inference`, with `--vllm.enable-lora`), an
*orchestrator* that drives verifiers envs and builds training batches, and an
FSDP2 trainer — launched together by `uv run rl @ config.toml`. Two GPUs by
default (inference on 0, trainer on 1); single-GPU runs are supported (the
`alphabet-sort` LoRA example runs on one H100). LoRA is a TOML block
(`[trainer.model.lora] rank/alpha`); with LoRA on, weights move to vLLM through
the filesystem and the run directory holds a plain `adapter_model.safetensors`
+ `adapter_config.json` — i.e. **our R6/R7 home steps (merge with
`sft_train.py --merge-only`, GGUF `--no-nextn`, serve, gates) are unchanged.**
Qwen3.5 dense is a supported trainer architecture (custom implementation; VLM
class supported, vision encoder frozen by default, bf16 mandatory). Rollouts
are tokenised by a *renderer* (hand-coded `qwen3.5` renderer exists) that never
re-renders prior turns, so the trainer sees exactly the sampler's token ids.
In-loop eval is a config block (`[orchestrator.eval]` sources with an
`interval`), batches can skip zero-advantage groups
(`orchestrator.constant_trainer_batch_size = false`), and `uv run dashboard`
shows rewards, metrics, traces and `mismatch_kl` live on `:7788`.

### Our pieces → theirs

| Ours today | In verifiers / prime-rl |
| --- | --- |
| `rl/prompts.jsonl` — 8,140 **rendered-text** step rows (`prompt`, `teacher_completion`, `checks`, `kind_expected`, `listing`, `template`, `session_id`/`turn_index`/`step`) | `TaskData` rows. **Text prompts cannot be used as-is**: the `null` harness speaks chat messages + tools, not raw completions (no completions mode in 0.4.0). The chat form exists: `~/news/voice-sft/run2/sft_chat/shard-*.jsonl` (~1,000 records of full OpenAI messages with `tool_calls`/`tool` roles **and the 19 tool schemas**), joinable to the prompt rows' extras by `session_id`/`turn_index`/`step`. Rollouts (`~/news/voice-rollouts/*.jsonl`) carry the same in Responses-API shape. |
| `rlvr_rewards.reward(prompt, completion, extras)` and its components | One `@vf.reward` calling it (parity with runs 1–2.5), components as `@vf.metric`s. `Sources` parses rendered text; Phase A feeds it the served-template rendering of the task's messages, Phase B adds a `from_messages` constructor. |
| `kind` hard floor, `WEIGHTS` | Unchanged inside the reward; the floor becomes less load-bearing once the turn is played end to end (see §2). |
| The 19 tools: 5 `minny_*` (Minny MCP, stdio via `mcp.json`), 12 `tv_*` (ytvn MCP / agent functions), `web_lookup`, `search_memory` | Toolsets. Guide: external MCP by `url` if `minny-mcp` can serve HTTP, else a 5-method proxy; TV: a dry-run toolset mirroring `model_bench.py`'s (`{ok, dry_run, command, args, player}`); the two local tools: a 2-method toolset. |
| `eval_suite.py`'s fake screen that follows dry-run tunes | Per-rollout toolset instance state (`Task.toolsets` launches one per rollout). |
| `sft_eval.py` (gate 1), `score-eval` | `vf-eval` on the same dev rows, `-r 1` deterministic or `-r 8` for group statistics. |
| `sft_rl.py` (TRL GRPO, 1 prompt × 8 generations per step) | `rl.toml`: `batch_size` tasks × `group_size` rollouts per step, dapo-style loss, LoRA, in-loop eval sources. |
| R0–R7 on the rented H200 | R2 becomes `uv sync` of prime-rl + our taskset package; R4 is `uv run rl … --dry-run`; R6/R7 identical. |

## 2. The three failures, and what actually changes

**Run 1 — always-text hack.** The prompt rows are *single steps with the
teacher's earlier calls already in the prompt*, so a text answer "passed
routing" because the set was met by calls the policy never made; only `kind`
(0.25) opposed the collapse. In a verifiers turn the policy starts from the
viewer's words, makes (or fails to make) its own calls, and the trace holds
exactly those. `routing(calls, checks, src)` with `calls` = the policy's own
calls for the turn has no hole to exploit: a text-only turn where the checks
want `minny_search_catchup` scores routing 0 and grounding over *no results*.
The `kind` floor stays as a belt, but the braces are structural.

**Run 2 — a trade, not a win (catch-up up, schedule down).** The reward was
applied uniformly to 6,405 prompts and the gradient chased whatever was
cheapest. prime-rl trains from **several sources with their own `select`,
`group_size` and sampling** (`[[orchestrator.train.source]]`), so families can
be mixed in chosen proportions, and `[orchestrator.eval]` runs the dev families
every *N* steps with per-family metrics on the dashboard — gate 1 inside the
loop, which is the "use kind/tool_match as the stop signal" fix from
`SFT-DATA.md` without writing a callback.

**Run 2.5 — no gradient (`frac_reward_zero_std` 0.81, `kl` 0.006–0.014).**
Two changes. (a) The taskset is the same object for eval and training, so the
lesson "keep only the prompts sft2 actually gets wrong" is a filter over
`vf-eval -r 8` traces: tasks whose group has *non-zero reward std* (there is
something to learn) and *mean below the ceiling* (it is not solved). That
number is computed **before renting** — the go/no-go gate in §4. (b)
`constant_trainer_batch_size = false` makes the trainer skip zero-advantage
groups instead of padding the batch with them, so the 81 % that carry no
signal stop costing trainer time (they still cost inference).

**The template-drift family (not a run, but real cost in every run).**
`sft_train_prep.py` tracks `boundary_ok` and `think_block_ok`, `clean_params`
rewrites Python literals to JSON, and gate 0 of every candidate is "served
template byte-identical to the training one". The `renderers` package names
the same bugs — "Engine emits `false`; client parses to `bool(False)`;
`apply_chat_template` re-renders `"False"` … reproducible on Qwen3.5" — and
avoids them with `bridge_to_next_turn`, which extends the previous turn's token
ids verbatim instead of re-rendering history. `mismatch_kl` on the dashboard
is the live check that trainer and sampler agree.

**Batch size (the quiet one).** `sft_rl.py` runs `--batch 4 --grad-accum 2`
with 8 generations: **one prompt per optimiser step**, 150 prompts per run.
That alone explains noisy `eval_reward` curves and why a focused set looked
"solved". prime-rl's practical floor is 16 prompts × 8 rollouts per step; the
`alphabet-sort` example uses 256 × 16. Budgets in §5 follow from this.

## 3. Phase A — step-level parity with gate 1 (free; 1–2 days)

Goal: reproduce `sft_eval.py` through verifiers on the identical dev rows,
against the laptop's sft2 on `:8002`, and confirm the model sees the production
tools. Nothing is trained; the laptop keeps serving the household (`-c 2`,
same ceiling `sft_eval` uses).

**Data.** For each dev step row in `rl/prompts.jsonl`, take the matching
`sft_chat` record (same `session_id`/`turn_index`), cut its messages just
before the assistant message of that `step`, and carry the row's extras.
Because `TaskData.prompt` accepts full `Messages` including prior assistant
`tool_calls` and `tool` results, mid-turn states replay exactly.

**Taskset sketch** (patterns from `reverse_text` and `wiki_search`, API
confirmed against 0.4.0):

```python
# dev/voice/minny_tv/minny_tv/taskset.py
import json
import verifiers.v1 as vf
from rlvr_rewards import reward as score, Vocab

class StepData(vf.TaskData):
    extras: dict        # checks, teacher_completion, listing, kind_expected
    rendered: str       # served-template rendering of `prompt` — what Sources parses today
    family: str         # template family, for per-family metrics

class MinnyConfig(vf.TasksetConfig):
    split: str = "dev"              # --env.taskset.split dev|train|sft2-misses
    guide: vf.ToolsetConfig = vf.ToolsetConfig()
    tv: vf.ToolsetConfig = vf.ToolsetConfig()

class StepTask(vf.Task[StepData]):
    @vf.stop
    async def one_reply(self, trace: vf.Trace) -> bool:
        return trace.num_turns >= 1          # judge the first action only, as sft_eval does

    @vf.reward(weight=1.0)
    async def minny(self, trace: vf.Trace) -> float:
        total, _ = score(self.data.rendered, as_text(trace.assistant_messages[-1]),
                         self.data.extras, VOCAB)
        return total                          # the kind floor and WEIGHTS apply unchanged

    @vf.metric
    async def kind(self, trace: vf.Trace) -> float:
        return score(self.data.rendered, as_text(trace.assistant_messages[-1]),
                     self.data.extras, VOCAB)[1]["kind"]
    # …one @vf.metric per component (format, routing, args, grounding, coverage, fabrication, length)

class MinnyTaskset(vf.Taskset[StepTask, MinnyConfig]):
    @classmethod
    def toolsets(cls, config):               # shared per worker; never called in Phase A (the stop fires first)
        return [MinnyGuide(config.guide), TvDryRun(config.tv), LocalTools(vf.ToolsetConfig())]

    def load(self):
        for row in load_steps(self.config.split):   # prompts.jsonl ⋈ sft_chat
            yield StepTask(StepData(prompt=row["messages"], extras=row["extras"],
                                    rendered=row["rendered"], family=row["template"]),
                           self.config.task)
```

`as_text()` rebuilds the `<tool_call>\n<function=NAME>\n<parameter=K>\nV…`
text from `AssistantMessage.tool_calls` (JSON `arguments` → parameters) or
returns `content`, so `split_completion` scores the same shape it scored in
runs 1–2.5. A malformed call never arrives as `tool_calls`; it arrives as text
containing `<tool_call>`, which `format` flags as before.

**Tools.** `@vf.tool` builds each schema from the method signature and
docstring, so the three toolsets must mirror production's 19 schemas (names,
parameter names, enums, descriptions). Cheapest route for the guide: run
`minny-mcp` over HTTP and attach it with `guide.url = …` — same server as
production, schema identical by construction. If it is stdio-only, a 5-method
proxy toolset with the descriptions copied verbatim. TV and local tools are
hand-mirrored (small signatures).

**Commands.**

```bash
# once: a package next to the scripts (hatchling, like upstream's environments/*)
uv pip install -e dev/voice/minny_tv            # into the vf env
LOCAL_LLM_API_KEY=… vf-eval minny-tv -m qwen3.5-9b-sft2 \
  --client.base-url http://192.168.50.71:8002/v1 --client.api-key-var LOCAL_LLM_API_KEY \
  --env.taskset.split dev -r 1 -c 2 --sampling.temperature 0 --sampling.max-tokens 300 \
  --no-push -o ~/news/model-bench/vf
```

**Acceptance (all free).**
1. The `tools` array the harness sends equals a rollout's
   `responses_create_params.tools` field for field (capture with `-v` or a
   logging proxy); otherwise the eval measures a different prompt.
2. Per-example agreement with `sft_eval` on `kind`, tool name and args
   ≥ 95 % on run2's 96 dev steps; disagreements explained (temperature,
   server-side parser vs raw `/completion`).
3. Mean reward within noise of `score-eval` on the same rows.

Known difference, acceptable: `sft_eval` samples raw `/completion` text;
`vf-eval` goes through the chat API and `llama-server`'s tool parser — **the
production path**, which is a point in its favour.

## 4. Phase B — whole-turn env and the gradient gate (free; 2–3 days)

Goal: play complete turns (and conversations), execute the policy's own
calls, score the turn, and measure whether sft2 leaves anything to learn.

**Env.** `prompt=None`, `system_prompt` = the static production prompt, and
the recorded viewer turns replayed by a scripted user (the `alphabet-sort`
pattern). Each replayed user message is the viewer's words **plus the context
block** exactly as the rollout recorded them (clock, on-screen line, memory),
so the policy's time arguments stay consistent with the recorded guide.

```python
class TurnData(vf.TaskData):
    user_turns: list[str]       # words + "[context for this turn …]" block, per turn
    expect: list[dict]          # per-turn checks / listing / kind_expected / teacher_completion
    family: str

class MinnyEnv(vf.SingleAgentEnv):
    async def run(self, task, agents):
        async with agents.agent.interaction(task) as it:
            for words in task.data.user_turns:
                if (await it.turn(words)).terminated:
                    break
```

**Rewards per turn** from `trace.messages`: the turn's `calls` are the
assistant `tool_calls` between two user messages, its results the `tool`
messages, its spoken text the last assistant `content`. `Sources.from_messages`
(≈40 lines beside the text constructor) feeds the existing `routing`, `args_ok`,
`grounding`, `coverage`, `fabrication`, `length`. Per-turn scores average into
the task reward; components and families stay as metrics. `kind` reduces to
two facts the turn now exposes directly: it ended in spoken text, and the calls
it made satisfy the checks.

**Guide backend during play.** Live Minny gives time-varying answers, so
training would chase a moving target. Build a *recorded-response store* from
every `tool` message in `sft_chat` and the rollouts, keyed by tool name +
normalised arguments; serve hits from the store, fall back to live Minny for
misses (logged, so the miss rate is known), and freeze the clock through the
replayed context block. The TV toolset is dry-run with per-rollout state
(`Task.toolsets`), the screen following tunes as `eval_suite.py`'s fake screen
does.

**The gradient gate** (the number run 2.5 did not have). Run the turn taskset
on sft2 with `-r 8` and compute, per family:

- `std>0` — share of tasks whose 8 rollouts do not all score the same;
- `headroom` — 1 − mean reward / max attainable.

Go to Phase C only if the targeted families show roughly **std>0 ≥ 0.3 and
headroom ≥ 0.15**; otherwise the set is solved for this reward and renting
buys nothing (run 2.5 again). The tasks that pass become the
`--env.taskset.split sft2-misses` training split; the full dev split stays the
in-loop eval. The same run gives the sft2 baseline every later checkpoint is
compared with.

## 5. Phase C — the prime-rl run (metered; 1 day setup + one run)

**Config sketch** (`rl.toml`; keys marked † are from the docs/examples, keys
marked ? must be confirmed at dry run):

```toml
max_steps = 100                               # †
seq_len = 12288                               # † prompt p90 ≈ 9k under the old layout; ~half under the lean one

[model]
name = "/data/sft2-merged"                    # † the merged bf16 staged in R3

[trainer.model.ac]                            # † activation checkpointing (alphabet-sort uses it)
[trainer.model.lora]                          # † rank 32 / alpha 64 as the example; our sft recipe is r32
rank = 32
alpha = 64
# target_modules must include the gated-delta projections (in_proj_qkv, in_proj_z, out_proj)
# as sft1/sft2 did — `--trainer.model.lora.target-modules …` exists †; default list ?

[trainer.optim]
lr = 1e-5                                     # † run 2.5's lesson: room to move via lr, not beta

[orchestrator]
batch_size = 16                               # † tasks per step (16–64; 1 was TRL's)
constant_trainer_batch_size = false           # † skip zero-advantage groups

[orchestrator.renderer]
name = "qwen3.5"                              # † hand-coded renderer; auto-detect is exact-match and will not fire on a local path
# enable_thinking = false                     # ? field name on the qwen3.5 renderer config

[orchestrator.train]
group_size = 8                                # † 8 is the documented floor; 16 if the budget allows
[orchestrator.train.sampling]
max_completion_tokens = 300                   # † the teacher's longest step is 257 tokens
temperature = 1.0

[[orchestrator.train.source]]
name = "minny-misses"
env.taskset.id = "minny-tv"                   # †
env.taskset.split = "sft2-misses"             # our TasksetConfig field, like alphabet-sort's env.taskset.min_turns †
env.agent.harness.id = "null"                 # †
env.agent.runtime.type = "subprocess"         # †

[orchestrator.eval]
interval = 10                                 # †
[[orchestrator.eval.source]]
name = "minny-dev"
env.taskset.id = "minny-tv"
env.taskset.split = "dev"
select.limit = 96                             # †
env.agent.harness.id = "null"
env.agent.runtime.type = "subprocess"

[ckpt]                                        # † checkpoint at the end (and on interval if set)
[inference]
[inference.vllm]
max_model_len = 12288                         # † (`--inference.vllm.max-model-len`)
# LoRA serving: the README passes `--vllm.enable-lora` to `uv run inference` †; the rl launcher's equivalent ?
```

**Hardware.** Two cards (inference + trainer) is the documented default and
the safe choice; one H200 141 GB colocated is plausible (TRL colocate peaked
105.78 GB with the same model and lengths) and is what the dry run decides.
Expect the `rl` launcher to pick GPUs itself (`CUDA_VISIBLE_DEVICES` is set by
it and cannot be overridden).

**Budget (estimates — calibrate with the dry run's step time and
`seq_len/mean`).** Per step at 16 × 8 rollouts and ~9k tokens each: ≈1.2 M
trainer tokens; a 9B LoRA forward+backward with checkpointing on an H100/H200
class card at 30–40 % utilisation is roughly **3–6 min/step**, inference
overlapping asynchronously (prefix cache: the 8 rollouts of a task share its
prompt, and every prompt shares the static system + tools prefix).

| run shape | rollouts total | est. wall | est. cost (H200 $5.48/h, 2 cards) |
| --- | --- | --- | --- |
| TRL-scale: 8 × 8, 60 steps | 3,840 | ~1.5–3 h | $15–35 |
| recommended: 16 × 8, 100 steps | 12,800 | ~5–10 h | $55–110 |
| with zero-signal skipping at run 2.5's 81 % | same | trainer time ÷ ~4; inference unchanged | ~$30–60 |
| same, lean-layout prompts (sft3 data, ~4–5k tokens) | same | ÷ ~2 again | ~$20–40 |

For comparison the three TRL runs cost ~$10–15 each and saw 1,200 rollouts.
The first prime-rl run should be the TRL-scale shape: it exercises the stack
for the same money and its dashboard already answers whether the bigger run is
worth it.

**Stop and promote signals** (unchanged in substance, now visible live):
`reward/*` per source, per-family metrics from the eval source every 10 steps,
`kind` (or its turn-level successors) ≥ 0.95, `mismatch_kl` small and flat,
`is_truncated/*` ≈ 0, `seq_len/*` steady. Home gates 1–3 and the promote rule
("weak categories rise, strong ones hold") stay exactly as in `SFT-DATA.md`.

## 6. Risks and open items

Verified (installed package or upstream source read):

- `verifiers.v1` has no raw-completions mode; chat messages + toolsets only.
- `ToolsetConfig.url` attaches an external MCP server without launching one.
- `AssistantMessage.tool_calls[*].arguments` is a JSON **string**; `ToolMessage`
  carries `tool_call_id` and `name`.
- `Interaction.turn()` returns a `Segment` with `terminated`; a prompted task
  speaks first, an unprompted one (`prompt=None`) waits for the scripted user.
- `vf-eval`: `-r`, `-c`, `-n`, `--select.include.ids`, `--client.base-url`,
  `--client.api-key-var`, `--sampling.temperature/max-tokens`, `--no-push`,
  `--resume`, `--dry-run`. The train-side client (`--client.type train`) is
  renderer-backed (`--client.renderer`, `--client.renderer-model-name`).
- prime-rl: LoRA via TOML, filesystem weight broadcast with LoRA, adapter files
  in the broadcast dir, `constant_trainer_batch_size`, `[orchestrator.eval]`
  with `interval` and `select.limit`, `[inference.vllm] max_model_len`,
  `[orchestrator.renderer] name`, the `qwen3.5` hand-coded renderer,
  `tools/convert_dcp_to_bf16.py` **rejects LoRA checkpoints** (irrelevant: we
  merge the adapter at home as today).

To verify, in order of how much they could change the plan:

1. **Tool-schema parity.** Production builds the 19 OpenAI tools through
   pydantic-ai from the MCP `inputSchema`s plus its own functions; verifiers
   builds them from `@vf.tool` signatures/docstrings or from the attached MCP
   server. Any drift in descriptions changes routing behaviour. Phase A
   acceptance 1 is the check; the `url` route makes the guide's 5 identical.
2. **`minny-mcp` over HTTP.** `mcp.json` launches it over stdio
   (`uv --directory ~/Development/MinnyAgent run minny-mcp`). If it cannot
   serve streamable HTTP, the proxy toolset is ~60 lines.
3. **Renderer parity with the served template.** Before renting, render 20
   `sft_chat` records with the `qwen3.5` renderer and with `llama-server`'s
   `/apply-template`, diff the token ids; the runbook's `boundary_ok` /
   `think_block_ok` checks become this one diff. A fine-tune path never
   auto-detects, so the renderer must be named explicitly.
4. **The VLM class.** sft2's merge is `Qwen3_5ForConditionalGeneration` with
   the vision tower and MTP head. prime-rl supports Qwen3.5 dense and its VLM
   path (vision frozen, bf16 mandatory); whether the merge loads as text-only
   or needs `[model.vlm]`, and that the MTP weights are ignored, is for the
   dry run — same place the TRL path found `--no-nextn`.
5. **LoRA target modules.** prime-rl's default target list may stop at the
   attention/MLP names; sft1/sft2 also adapted the 24 gated-delta layers'
   projections. Set them explicitly or accept a different recipe knowingly.
6. **API churn.** `verifiers` is at 0.4.0 and the help text itself notes renamed
   keys; prime-rl is a moving `main`. Pin the two commits in the appendix and
   run `vf-eval --dry-run` / the rl dry run before every rental.
7. **Reward hacking is not solved by the platform.** What improves is the
   exploit surface (whole turns, own calls) and the time to notice (per-family
   eval every 10 steps on a dashboard). Keep the floor and the gates.
8. **Data stays home.** Traces contain household utterances and provider
   results: always `--no-push`; no `PRIME_API_KEY` is needed for local or
   rented-box use; the rented box holds the same artefacts R3 already ships.
9. **Laptop concurrency.** `:8002` shares 32k of context over 4 slots; `-c 2`
   as `sft_eval` learned, more and requests fail with context overflow.

## 7. Decision and next steps

1. **Phase A** this week on nucbox + the laptop: `dev/voice/minny_tv/`
   package, the three toolsets, the step taskset, `vf-eval` on run2's dev,
   the three acceptance checks. Deliverable: a parity table against
   `sft-eval-20261013-1303.json`.
2. **Phase B**: the turn env, recorded-response store, `Sources.from_messages`,
   the `-r 8` gradient numbers per family for sft2. Deliverable: the go/no-go
   table and the `sft2-misses` split.
3. **Phase C** only on a go: TRL-scale shape first, on the current H200
   recipe (R1 unchanged: ≥100 GB container), then the 16 × 8 run if the
   dashboard shows the targeted families moving with `kind`/`mismatch_kl` flat.
4. Fold the outcome into `SFT-DATA.md` as run 3 and retire `sft_rl.py` only
   when a prime-rl checkpoint has passed the three gates.

Independent of all three: sft3 (regeneration under the lean layout) halves
every token count above and is the only route to the wording-and-identity
misses — it should not wait for the RL platform decision.

## Appendix — sources checked (2026-10-14)

- Pins to use until a run has gated: `verifiers` v0.4.0 (released 2026-10-12;
  `main` at `1dc8a14b3b44`, 2026-10-14), `prime-rl` `main` at `32b502bf0cb5`
  (2026-10-14), `renderers` `main` at `d476cc4a3012` (2026-10-12).
- Installed: `verifiers==0.4.0` in the scratch `vfenv` (`verifiers/v1`:
  `task.py`, `agent.py`, `mcp/toolset.py`, `mcp/launch.py`,
  `envs/single_agent/env.py`, `vf-eval --help`).
- `PrimeIntellect-ai/verifiers` `main`: `environments/reverse_text/…/taskset.py`
  (single-turn, `@vf.stop`, `@vf.reward`), `environments/wiki_search/…/taskset.py`
  + `servers/wiki.py` (`Taskset.toolsets`, `TOOL_PREFIX`, `@vf.tool`, judges),
  `environments/alphabet_sort/…/taskset.py` (scripted multi-turn env,
  `prompt=None`, `INFINITE`).
- `PrimeIntellect-ai/prime-rl` `main`: `README.md`, `docs/configuration.md`,
  `docs/training.md`, `docs/advanced.md` (LoRA, VLM), `docs/algorithms.md`
  (renderers), `docs/overview.md`, `examples/basic/{alphabet-sort,reverse-text}/rl.toml`,
  `examples/basic/alphabet-sort/{README.md,eval.toml}`.
- `PrimeIntellect-ai/renderers` `README.md` (hand-coded `qwen3.5`, exact-match
  auto-detect, `bridge_to_next_turn`, the `False`/`false` failure mode).
- Local data: `~/news/voice-sft/rl/prompts.jsonl` (8,140 rows),
  `~/news/voice-sft/run2/sft_chat/shard-*.jsonl` (messages + 19 tool schemas),
  `~/news/voice-rollouts/*.jsonl`, `dev/voice/rlvr_rewards.py`, `dev/voice/sft_rl.py`.
