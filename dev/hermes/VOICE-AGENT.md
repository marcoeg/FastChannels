# Standalone voice agent — pydantic-ai brain (runbook)

Replaces Hermes in the voice loop. Built 2026-08-30 from the approved plan;
motivation: Hermes was ~90% of time-to-first-audio (median 16 s per turn,
~24k tokens fixed overhead, up to 7 sequential model calls). This agent
answers no-tool turns in ~1 s and tool turns in 2-5 s.

## Architecture

```
Browser mic ──wss:8443 (caddy)──► livekit-server (host net)
                                      │ room "voice"
                                      ▼
              voice-agent.service (dev/voice/voice_agent.py)
              silero VAD → OpenAI streaming STT → PydanticLLM
                         → ElevenLabs Flash TTS (VOICE_TTS switch)
                                      │ in-process
                                      ▼
    pydantic-ai Agent (gpt-5.6-terra, reasoning effort none, priority tier)
      ├─ epg_* tools    MCP 127.0.0.1:8001 (read-only allowlist, 8 s cap)
      ├─ tv_* tools     MCP 127.0.0.1:8790 (TVShell remote, 5 s cap)
      ├─ whats_on       deterministic on-air listing (code-filtered)
      ├─ web_lookup     in-process Brave web search (~0.5 s)
      ├─ search_memory  in-process graphiti-core
      └─ memory         graph "voice_memory" in falkordb-voice (:6390, AOF)
```

Prompts (`dev/voice/prompts/*.md`, ~1.3k tokens total) replace the Hermes
skills; loaded once at startup — **restart the worker after editing them**.
Dynamic per-turn context: clock in Pacific AND UTC, plus remembered facts.

## Moving parts

| Piece | Control |
| --- | --- |
| Worker | `systemctl --user <status\|restart> voice-agent` |
| Hermes fallback worker | `hermes-voice-agent.service`, disabled; `Conflicts=` makes the two mutually exclusive. Rollback: `systemctl --user disable --now voice-agent && systemctl --user enable --now hermes-voice-agent` |
| Memory DB | `falkordb-voice` service in `dev/voice/compose.livekit.yml` (project hermes-voice); AOF everysec, 127.0.0.1:6390, digest-pinned |
| Memory backup | `voice-memory-backup.timer` Sun 04:00 → `~/backups/voice-memory/` (keep 8) |
| Memory inspection | `dev/voice/memory_cli.py stats\|facts "q"\|episodes\|profile\|wipe --yes` |
| Web search | Brave API, key `BRAVE_SEARCH_KEY` in `dev/voice/.env` (copied from `~/.env`); `dev/voice/websearch.py`, 4 s cap, 4 results, no URLs in output |
| Config | `dev/voice/.env`: VOICE_MODEL, VOICE_REASONING_EFFORT, EPG_MCP_URL, TV_MCP_URL, VOICE_FALKORDB_* , MEMORY_LLM_MODEL/SMALL |
| Telemetry | `~/news/voice-metrics.jsonl` (LiveKit eou/llm/tts metrics + per-turn rows: ttfd_ms, filler_used, tool_spans) |
| Transcript | `~/news/voice-conversations.jsonl` (unchanged format) |
| TTFT bench | `dev/voice/bench_ttft.py -n 10 --models a,b` (also dumps the wire reasoning config) |

## Model notes

- Reasoning effort is per model FAMILY: gpt-5/gpt-5-mini take `minimal`;
  gpt-5.5/5.6 take `none`. Set VOICE_REASONING_EFFORT to match VOICE_MODEL
  or the API 400s.
- **Model chosen on measured merit: gpt-5.6-terra** (live 2026-08-30).
  The deciding test was temporal correctness — "is Columbo on right now"
  against 15 schedule rows: luna FAILED it three different ways at effort
  none AND low (nano tier cannot scan time ranges; dual-notation clock
  injection didn't save it); gpt-5.5, terra, and sol all pass. TTFT p50/
  p95 (prompt-cached, priority): terra 745/767 ms (tightest), 5.5 740 ms,
  sol 625 ms warm but 2.6-3.0 s on HALF its calls (capacity tail), luna
  608 ms. Terra = correctness + newest generation + no tail. Access
  grants propagate slowly and failures appear MID-STREAM as
  `model_not_found` after a 200 — check with a bare curl first.
- Memory extraction models are separate (MEMORY_LLM_MODEL=gpt-5-mini,
  MEMORY_LLM_SMALL=gpt-4.1-mini) — graphiti's defaults assume access to
  gpt-4.1-nano, which this project lacks.

## Latency pipeline (upgraded 2026-08-30 after the first phone sessions)

Measured bottleneck was three sequential OpenAI cloud calls, not anything
local (FalkorDB recall: 1-2 ms). Measured result of the upgrade (medians,
speech-end → first audio = EOU + LLM TTFT + TTS TTFB; before = 36 audio
turns, after = 8):

| | before | after |
| --- | --- | --- |
| plain turns | 3.59 s | **2.10 s** (−41%) |
| tool turns | 4.26 s | **3.37 s** (−21%) |
| TTS TTFB | 0.76 s | 0.16 s |
| EOU p90 (endpointing-cap stalls) | 2.50 s | 1.24 s (outliers gone) |

Residual budget: transcript finalization still ~0.95 s after speech end —
realtime STT pays a server-side commit round trip, so the projected
~0.2 s did not materialize; plus terra's ~0.9 s per model pass (×2 on
tool turns). Next levers if ever needed: endpointing.min_delay 0.5→0.35,
and the realtime-STT commit latency. The upgrades:

- **Streaming STT**: `openai.STT(use_realtime=True)` — transcribes during
  speech; transcript ready ~0.2 s after speech end vs ~1.0 s batch
  Whisper, and interim transcripts enable LLM preemption.
- **LLM preemption**: `turn_handling.preemptive_generation.enabled` — the
  model starts on the live transcript before the endpointer commits.
  **`preemptive_tts` must stay False**: with it True, TTS synthesized
  successfully (metrics showed frames) but playout stayed SILENT — a
  livekit 1.7.1 defect found via voice_probe.py. Re-test before enabling
  after any livekit upgrade.
- **ElevenLabs Flash v2.5 TTS** (`VOICE_TTS=elevenlabs`, key in .env):
  TTFB 0.16-0.19 s measured vs ~0.7 s OpenAI TTS. `VOICE_TTS=openai`
  restores the old voice. NOTE: LiveKit plugins must be imported at
  module top level — importing inside the entrypoint raises "Plugins must
  be registered on the main thread".
- **`dev/voice/voice_probe.py`** — end-to-end AUDIO test without a phone:
  joins a throwaway room, speaks a synthesized question as a mic track,
  measures the reply (frames, seconds, peak RMS, time-to-first-audio).
  The tool that isolated the preemptive_tts silence in one run.

## whats_on — never let a model compare times

`dev/voice/epg_now.py` answers every "what's on / is X on now / what's on
at 9" question: it pages the EPG list tool over an overlap window, then
filters and formats **in Python**, so the model only ever sees programmes
genuinely airing at the requested moment, with Pacific times already
rendered. Measured 13-355 ms.

This exists because the prompt-level overlap rule ("fetch from 4 h back,
keep rows still running") is not reliably executed by any model tested:
luna failed it outright, and terra silently skipped the filter on long
lists and announced films that had already ended (2026-09-12 session,
films "until six oh five" read out at 6:57 pm). Time comparison is
arithmetic, not judgement.

Two traps found building it, both encoded:
- **Paging is mandatory.** The server caps a page at 1000 rows ordered by
  start time; a 6 h window across ~1,075 channels overflows it, so a
  single call silently returns only the OLDEST rows — every one already
  finished. "At 9pm" returned nothing until paging was added.
- **A one-minute grace** (`MIN_REMAINING_S`): rows ending seconds after
  the asked-about moment are technically airing but useless to offer.

`at_time` accepts "now" or a clock time ("9pm", "21:30") and resolves it
in code — bare hours are read as evening, past times roll to tomorrow —
so the model never does date arithmetic either.

## Web lookups

EPG descriptions are thin and often boilerplate, so `web_lookup(query)`
(native in-process tool, not MCP — one Brave HTTPS call, ~0.5 s measured)
supplies plot, cast, year, and background. Prompt rules live in
`prompts/35-web.md`: content questions only (never schedule, tuner, or
preferences — those tools are far faster), search title + a
disambiguating word, guide wins on conflicts, spoiler discipline applies
to snippets too, never read links aloud. Failure modes all degrade to a
spoken sentence rather than an exception: missing key, bad key, HTTP
error, 429, and a 4 s timeout are each their own honest message.

## Testing without a browser

`console --text` mode, driven through a pty (stdin EOF kills it, and it
imports sounddevice even in text mode — no PortAudio on this host):

- sounddevice stub + pty driver live in the session scratchpad pattern;
  see `console_probe.py` from the build session, or run interactively:
  `PYTHONPATH=<dir-with-sounddevice-stub> ./voice_agent.py console --text`
- **Text console bypasses `on_user_turn_completed`** (it goes through
  `session.run()`), so the recall seam has an inline fallback in
  `pai_llm._run` — recall works on both paths; the hook's cost is only
  measured (EOUMetrics) on real audio turns.

## Hard-won gotchas (all bitten during the build)

- **A long-idle worker goes deaf** (2026-09-10): after ~9 days untouched,
  the worker's prewarmed job processes had died; a phone join then failed
  dispatch three times ("failed to launch job on process", BrokenPipe on
  the IPC channel) and no agent entered the room — connected session,
  nobody home. Fix: restart. Prevention: `voice-agent-restart.timer`
  recycles the worker daily at 04:30 via `try-restart` (a scheduled quiet-
  hours restart, deliberately NOT RuntimeMaxSec, which would fire 24 h
  after start and could kill a live conversation).

- **Dependency wall**: livekit-agents 1.7.1 needs `openai>=2,<3`;
  pydantic-ai ≥2.32 needs `openai>=3` → pinned
  `pydantic-ai-slim[openai,mcp]==2.31.1` (newest compatible). Revisit both
  pins together.
- **First word missing**: pydantic-ai streams the first chunk of a text
  part in `PartStartEvent`; only later chunks are `PartDeltaEvent`. Handle
  both or every reply loses its first word.
- **One dead MCP server must not kill the worker**: `agent.__aenter__`
  raises if any server refuses connections. `ResilientToolset`
  (pai_agent.py) absorbs enter failures, serves zero tools while down,
  reconnects per run. Verified: EPG container stopped → honest "couldn't
  check", TV tools unaffected, auto-recovery on restart.
- **The FalkorDB label-join pathology (fixed 2026-09-01)**: with
  `MATCH (n:Entity)-[e:RELATES_TO]->(m:Entity)` and NO selective node
  anchor, FalkorDB's planner joins two label scans instead of scanning
  edges once — 9,840 ms vs 3.6 ms for the IDENTICAL query with unlabeled
  `(n)-[e]->(m)` at 17k edges. This was the true root cause of BOTH the
  "filtered search is slow" mystery (earlier misdiagnosed as a vecf32
  hoist defeat) and unanchored window lists ("movies tonight") timing
  out while channel-anchored ones flew. Fixed by dropping the node
  labels in list_memory_facts' cypher and falkor_fast_search's
  similarity leg (graphiti_mcp_server.py + falkor_fast_search.py, EPG
  MCP image rebuilt; vendored copy in dev/voice synced). Filtered
  search now 0.25-0.8 s (embedding-bound), unanchored lists 31 ms; the
  prompt's time-filter ban on search was lifted. Upstream-PR candidate.
  The 8 s client cap stays as the wedge guillotine. Still on the shelf:
  a deterministic in-worker `whats_on(channel)` tool.
- **Message parts are dataclasses** in pydantic-ai 2.31 (`model_copy`
  absent) — `_replace()` helper handles both shapes.
- Console text turns don't fire the user-turn hook (see above).
- The 0.9 s **delayed filler** means fast TV calls stay silent and only
  genuinely slow tool turns get "One moment, let me check."
- Memory filter: acks, pure device twiddling, and barge-in fragments are
  never written; standing instructions (always/never/favorite/…) always
  are; tune-by-name passes through (repetition is the preference signal).

## Relationship to Hermes

Hermes keeps running for chat/Telegram/etc. with its own memory graph —
only the voice room moved off it. The EPG graph, TVShell MCP, and
FastChannels stack are shared, unchanged.
