# BUGLIST — pending fixes from the 2026-10-04 review

Source: `REVIEW-2026-10-04.md` (72 findings; full analysis, evidence and
verifier notes per finding). Status as of 2026-10-05: **50 fixed or
mitigated, 22 open: 11 MEDIUM, 11 LOW, no HIGH.** The fixing commits are in
the report's "Fix status" section. Each item below was re-checked against
the current code on 2026-10-05.

Related, outside the review:
- `esp32/BUG-0001-satellite-offline-recovery.md`: satellite offline
  diagnosis and recovery.
- `BUG-0002-lineup-specific-code.md`: lineup- and zone-specific code.
- "Found in spoken testing" at the end of this file: bugs from the
  `TEST-SCRIPT.md` runs, numbered T1, T2, … so they don't collide with the
  review's numbers.

## How to work this list

Same rules as batches 1–8:
- Small batches.
- The benchmark gate (`model_bench.py`) before and after.
- An old-vs-new diff on real inputs (traces, transcript log, memory backup,
  live lineup) for every behaviour change.
- One agent restart per batch, then `voice_probe.py` with **questions only**:
  commands act on the real TV.
- Commit only when asked.

Restarts force the satellite to reconnect (see BUG-0001), so keep them to
one per batch.

## Summary

| # | Sev | Area | Problem | Decision needed? |
|---|---|---|---|---|
| 32 | MED | turn history | Discarded preemptive runs write phantom exchanges into history; a preemptive run can tune the TV before the turn is final | — |
| 38 | LOW | turn history | Closing-ack shortcut commits a phantom pair on a partial transcript | — |
| 33 | MED | turn history | (rest) Echo-greeting preemptive handle is not cancelled | — |
| 54 | MED | ytvn-epg | Tune result reports `muted`/`state` from before autoplay settles | — |
| 53 | MED | ytvn-epg | A failed open leaves a black player and an "On screen now" for a programme that never played | — |
| 52 | MED | ytvn-epg | 15 s command timeout < relay pre-flight: "TV isn't connected", then it plays | — |
| 56 | LOW | ytvn-epg | Same timeout via grid reload / ffprobe; timeout text looks like no_shell | — |
| 57 | LOW | ytvn-epg | `canplay` re-kick resumes after the viewer paused during buffering | — |
| 55 | LOW | ytvn-epg | `viewer://titles` cache never evicts; an empty category root is refetched every call | — |
| 34 | MED | satellite | Half-duplex drops a request that finalizes while a reply is in flight | **yes**: keep half-duplex? |
| 35 | MED | satellite | Post-speech mute outlasts the device's playout and clips the first word | — |
| 1 | MED | robustness | A stdio MCP server that dies mid-job is never reconnected | — |
| 42 | LOW | robustness | Shutdown flushes (memory, rollouts, MCP exits) don't run on a systemd restart | — |
| 72 | MED | MinnyAgent | Window-argument errors reach the model as a bare error → "catalogue unavailable" | — |
| 24 | LOW | memory | Satellite job's "Earlier sessions" frozen at job start; pairs straddle sessions | — |
| 27 | LOW | memory | "Earlier sessions" replays misheard text and phantom greeting pairs | — |
| 25 | LOW | memory | Prune vs. consumer race can resurrect a deleted edge | — |
| 29 | LOW | memory | Two jobs pruning at once: false "failed" counts, orphan entities | **yes**: who prunes? |
| 60 | LOW | prompts | "After that" needs the previous row's `end` in context; no fallback | — |
| 63 | LOW | prompts | ~4.1k-token static prompt with duplicated rules | — |
| 65 | MED | deployment/docs | Unmute can't work on an unclicked page; no kiosk launcher; docs and tests assume it works | **yes**: autoplay flag or not? |
| 66 | MED | tests | Test plan lacks cases for recent fixes; J5/H5 contradict the code | — |

## Suggested batches

1. **Turn history: #32, #38, #33-rest.** The riskiest code (`pai_llm.py`
   commit paths), so it gets its own batch. Highest value: corrupted history
   and side effects from runs nobody heard.
2. **Player truthfulness (ytvn-epg): #54, #53, #52 + #56, then #57, #55.**
   What Minny says about the TV must match the TV.
3. **Robustness: #1, #42, #72** (#72 is in the MinnyAgent repo).
4. **Memory: #24 + #27, #25 + #29.**
5. **Satellite: #34, #35.** Needs spoken tests on the satellite, and #34
   needs a decision.
6. **Prompts, docs, tests: #60, #63, #65, #66.** #65 needs a decision.

## Items

### Batch 1 — turn history

**#32 [MEDIUM] Discarded preemptive runs commit phantom exchanges** (`pai_llm.py:187`)
- *Fails when:* "Tune to KTVU" [pause] "…no, KQED". The preemptive run tunes
  KTVU before the turn is final, is cancelled, and history gets
  user:"Tune to KTVU" / assistant:"…"; then the real turn runs.
- *Fix:* don't touch `brain.history` from a run that was never scheduled:
  skip `commit_interrupted` when nothing was spoken, and defer
  `commit_exchange`/`commit_turn` until the speech is scheduled (commit from
  the assistant item for that turn). Consider blocking side-effecting `tv_`
  tools in preemptive runs. Batch 3's command gate already holds `tv_`
  controls until the turn is committed; check how far that covers the tune
  case before adding more.
- *Verify:* replay traces with `cancelled_before_speech`; history after a
  cancel must not contain the fragment.

**#38 [LOW] Closing-ack shortcut commits a phantom pair** (`pai_llm.py:101`)
- *Fails when:* "Okay … put on KQED." The first segment "Okay." is
  committed as an ack pair, and the real turn follows with it in history.
  Also over-counts `closing_ack` in the metrics.
- *Fix:* commit/log the ack only once the stream is confirmed (close path,
  not cancelled), or skip the shortcut while the last user item isn't final.

**#33 [MEDIUM] (rest) Echo-greeting preemptive handle not cancelled** (`voice_agent.py:97`)
- *Already done (batch 1):* the adapter skips the model for an echo greeting
  (no emit, commit or trace).
- *Left:* call `self.session.interrupt()` before `raise StopResponse()`, so
  the stale preemptive handle is cancelled at once rather than on the next
  utterance.

### Batch 2 — player truthfulness (ytvn-epg)

**#54 [MEDIUM] Tune result captured before autoplay settles** (`ytvn-epg/static/app.js:616`)
- *Fails when:* fresh page, "Put on channel 9": the result says
  `muted:false`, Minny says it's on, and the page mutes a second later
  (autoplay fallback). The viewer hears nothing.
- *Fix:* resolve `tune`/`play_*` after the first `play()` settles (race with
  ~3 s); add `player.autoplay_muted`; reset `video.muted` in `closePlayer`
  only when it was the autoplay fallback. Prompt: key the unmute rule on
  `playing.status` "muted — unmute" rather than `player.muted` at result
  time.

**#53 [MEDIUM] Failed open leaves a black player and a false "On screen now"** (`ytvn-epg/static/app.js:529`)
- *Fails when:* watching KQED live, "play yesterday's NewsHour" → relay 404
  twice → live picture gone. "What am I watching?" → "the NewsHour
  catch-up" (stalled).
- *Fix:* on pre-flight/HLS failure re-open the previous `playing`, or
  `closePlayer()` so the state goes idle. Agent: `format_screen` renders
  `stalled` as "playback FAILED — nothing is playing"; define it in the
  prompt.

**#52 [MEDIUM] + #56 [LOW] Command timeout shorter than slow opens** (`ytvn-epg/mcp_server.py:55`, `COMMAND_TIMEOUT = 15.0`)
- *Fails when:* slow packager / 404-then-retry / grid reload / ffprobe pushes
  a play past 15 s. The MCP server returns "timed out … is the web app
  open?", Minny says the TV isn't connected, and the programme starts
  seconds later. A retry then closes and reopens the stream.
- *Fix:* per-command timeouts (≈30 s for tune/play, 5 s for
  mute/volume/state); a timeout message that doesn't claim the page is
  closed ("may still start — don't retry"); `_control` maps it to a distinct
  phrase. Page side: bound the pre-flight (~10 s), and don't tear down the
  current stream until the new one passes pre-flight.

**#57 [LOW] `canplay` re-kick resumes after a pause** (`ytvn-epg/static/app.js:591`)
- *Fix:* drop the `canplay` listener on an explicit pause, or guard it with a
  `userPaused` flag.

**#55 [LOW] Titles cache never evicts** (`ytvn-epg/mcp_server.py`, `_TITLES`)
- *Fix:* evict expired entries on insert (or a small LRU); cache an empty
  category list with the same TTL.

### Batch 3 — robustness

**#1 [MEDIUM] Dead stdio MCP server never reconnected** (`pai_agent.py:88`)
- *Fails when:* the guide server crashes or times out mid-job. Every guide
  question then fails ("Sorry, something went wrong") until the next
  restart, though the guide came back minutes later.
- *Fix:* treat connection-closed / timeouts in `_control`,
  `LineupScope._call` and the fan-outs as "server down": re-enter
  (or recreate) the MCPToolset with backoff; give `ResilientToolset.get_tools`
  a cheap liveness probe instead of relying on cached `list_tools`.

**#42 [LOW] Shutdown flushes don't run on systemd restart** (`voice_agent.py:234`)
- *Fails when:* a preference said shortly before a restart is lost in the
  memory queue; queued rollout records are lost; MCP children are killed.
- *Fix:* unit `KillMode=mixed`, `TimeoutStopSec≈20`; a worker drain timeout
  and shutdown timeout covering the flush; `memory.aclose(flush_timeout)`
  within that budget (10–15 s). Today the unit has neither setting.

**#72 [MEDIUM] Window-argument errors masked** (MinnyAgent `src/minny/agent/tools.py:55`)
- *Fails when:* the model sends an inverted or offset-less window
  ("after that" past midnight). The tool returns a bare "Error executing
  tool", and Minny says the catalogue is unavailable.
- *Fix:* MinnyAgent: re-raise `ValueError` from `resolve_window` as a
  `ToolError` with its text. FastChannels (optional): validate aware/ordered
  `start`/`end` in `LineupScopedToolset.call_tool` and return a plain
  sentence. Prompt: "rejected arguments → fix and retry once" is not
  "catalogue unreachable".

### Batch 4 — memory

**#24 [LOW] "Earlier sessions" frozen for the always-on satellite job** (`memory.py:230`)
- *Fix:* re-read the transcript tail when a turn starts long after the
  previous one (or on an mtime change); pair rows only within one
  `session_id`/room.

**#27 [LOW] "Earlier sessions" replays misheard text and phantom pairs** (`memory.py:302`)
- *Fix (with #24):* pair by `session_id`; drop greeting and ack pairs; drop a
  pair when the next user row in that session is a correction
  (`rollouts_export.CORRECTION`, widened in batch 5).

**#25 [LOW] Prune vs. consumer race** (`memory.py:264`)
- *Fix:* serialize prune with the consumer (one asyncio lock around
  `add_episode` and prune), or prune only when the queue is empty, holding
  the lock.

**#29 [LOW] Concurrent prunes** (`memory.py:421`)
- *Decision:* where prune runs: one job (e.g. the satellite's), a systemd
  timer via `memory_cli.py prune`, or a lock key in FalkorDB.
- *Fix:* prune from one place only; treat "not found" as already removed;
  optional orphan-entity cleanup.

### Batch 5 — satellite (spoken tests needed)

**#34 [MEDIUM] Half-duplex drops a continuation** (`voice_agent.py:263`)
- *Fails when:* "Tune to KTVU [0.7 s] set the volume to twenty percent."
  The second half finalizes while the reply is being prepared and is dropped
  with only a warning (11 in 3 days, 9 real requests).
- *Decision:* recommended: enable interruptions on the satellite and stop
  echo by muting the mic only during audible playout (the cooldown code
  already toggles input). Otherwise: a longer endpointing delay for
  `speaker`, and log the dropped text so tests can see it.

**#35 [MEDIUM] Post-speech mute clips the first word** (`voice_agent.py:326`, `ECHO_COOLDOWN_S = 1.0`)
- *Fix:* shorten to ≈0.6 s, or end the cooldown on a device "drained"
  message (firmware) with 1.0 s as a cap.

### Batch 6 — prompts, docs, tests

**#60 [LOW] "After that" has no fallback when the row left context** (`prompts/epg/minny.md:23`)
- *Fix:* prompt fallback ("if the row is gone, re-run the title search and
  take its end"), and/or keep guide tool results one exchange longer
  (`KEEP_TOOL_RESULTS_FOR = 2` today).

**#63 [LOW] Static prompt ~4.1k tokens with duplicates** (`prompts/tv/ytvn.md`)
- *Already done (batch 7):* the dead VOD lines are rewritten in ytvn mode.
- *Left:* merge the two restart and two `tv_get_state` passages; trim
  repeats.

**#65 [MEDIUM] Unmute can't work on an unclicked page** (`TEST-PLAN.md:104`, ytvn-epg docs)
- *Already done (batch 1):* the page reports "sound needs one click on the
  TV page" instead of silently pausing, and the prompt says to tell the
  viewer and not retry.
- *Decision:* launch the TV browser with
  `--autoplay-policy=no-user-gesture-required` (a user systemd unit), which
  makes the mute fallback disappear, or keep the policy and document the
  one-tap step.
- *Left either way:* README/protocol wording; TEST-PLAN C12/G2/G4
  expectations; TEST-SCRIPT setup step.

**#66 [MEDIUM] Test docs behind the code** (`TEST-PLAN.md`)
- *Add:* an echo case (J6: "Hi Minny" < 4 s after a reply is ignored,
  > 5 s is answered); a two-step H5 (0.6 then 0.4 → profile shows only 0.4;
  today H5 is one step); a stored-vs-question memory case; an empty-result
  case; cases for today's fixes (Liga MX "live sports now", "Dune Part 2"
  without a year, "Okay Minny" after a question, a standing instruction and
  an identity fact in memory). Make 8.8 wait 5 s.

## Found in spoken testing

**T1 [MEDIUM] Catch-up search by title misses recordings the channel listing has** (MinnyAgent `src/minny/agent/tools.py:83`, `search_catchup`)
- *Found:* session 3 re-run, 2026-10-05 09:47. "Play Daniel Tiger on
  Channel 9" tuned KQED and said it wasn't on; it didn't offer the 9:00 AM
  airing as catch-up.
- *Repro (2026-10-05 ~09:55):* `search_catchup(channel="KQED")` with no
  query lists Daniel Tiger's Neighborhood on `[MN] KQED HDTV`, 16:00–16:30Z,
  expiring 2026-10-06T16:30Z. `search_catchup(query="Daniel Tiger",
  channel="KQED")` (with or without `group_by="none"`) returns six other
  recordings (one on `Muxley KQEDDT2`, five on `[MN] PBS Kids`), not that
  one. `LineupScope` then drops all six, as none is channel 9's own feed,
  and returns "Nothing on this TV's channels matches". Through the real
  brain (`minny_brain_check.py`), "is there a catch-up of Daniel Tiger on
  channel 9?" → "No".
- *Not affected:* `tv_watch_title(…, catchup=true)` uses the TV's own data
  and played that recording.
- *Blocks:* the prompt-only fix for "Play X on <channel>" when X isn't on
  (`prompts/tv/ytvn.md:12-16` says to offer catch-up "if it aired earlier"
  but gives no way to check). It was written, then reverted before any
  restart: with this bug it still misses Daniel Tiger and would only change
  test 1.8 (TMZ on KTVU has a catch-up). Re-apply once T1 is fixed.
- *Fix:* MinnyAgent: find why a query plus channel excludes a recording
  that matches both (index field, channel expansion, or ranking cut), and
  add the repro as a test.

