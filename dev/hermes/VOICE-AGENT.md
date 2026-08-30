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
              silero VAD → OpenAI STT → PydanticLLM → OpenAI TTS
                                      │ in-process
                                      ▼
    pydantic-ai Agent (gpt-5-mini, reasoning effort minimal, priority tier)
      ├─ epg_* tools    MCP 127.0.0.1:8001 (read-only allowlist, 8 s cap)
      ├─ tv_* tools     MCP 127.0.0.1:8790 (TVShell remote, 5 s cap)
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
| Config | `dev/voice/.env`: VOICE_MODEL, VOICE_REASONING_EFFORT, EPG_MCP_URL, TV_MCP_URL, VOICE_FALKORDB_* , MEMORY_LLM_MODEL/SMALL |
| Telemetry | `~/news/voice-metrics.jsonl` (LiveKit eou/llm/tts metrics + per-turn rows: ttfd_ms, filler_used, tool_spans) |
| Transcript | `~/news/voice-conversations.jsonl` (unchanged format) |
| TTFT bench | `dev/voice/bench_ttft.py -n 10 --models a,b` (also dumps the wire reasoning config) |

## Model notes

- Reasoning effort is per model FAMILY: gpt-5/gpt-5-mini take `minimal`;
  gpt-5.5/5.6 take `none`. Set VOICE_REASONING_EFFORT to match VOICE_MODEL
  or the API 400s.
- Benchmarked TTFT (prompt-cached, priority tier): gpt-5-mini p50 779 ms,
  gpt-5.5+none p50 740 ms. gpt-5.6-luna is the intended target ($0.20/M,
  effort none) but the OpenAI project needs model access granted
  (grants propagate slowly; failures appear MID-STREAM as
  `model_not_found` after a 200 — check with a bare curl before blaming
  the code).
- Memory extraction models are separate (MEMORY_LLM_MODEL=gpt-5-mini,
  MEMORY_LLM_SMALL=gpt-4.1-mini) — graphiti's defaults assume access to
  gpt-4.1-nano, which this project lacks.

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
- **EPG latency is query-shaped**: ranked `search_memory_facts` on
  frequent terms measures 5.5-6.5 s server-side (same root cause as
  Hermes' old 15 s timeouts); `list_memory_facts` is ~8 ms. The prompt
  steers channel questions to find_channel → list; search keeps an 8 s
  client cap. Fixing the server-side search cost is an open follow-up.
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
