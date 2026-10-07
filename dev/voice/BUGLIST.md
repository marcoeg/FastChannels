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

**T2 [MEDIUM] STT returns its own prompt as a user turn** (`voice_agent.py:274-288`, `_stt_prompt`)
- *Found:* session 4, 2026-10-05 09:59:37, 3 s after connect. 0.74 s of
  audio (STT metrics; no speech from the tester yet) was transcribed as
  the full prompt text: "English conversation with Minny, a TV-viewing
  assistant, about TV channels such as KTVU, KPIX, KQED, KNTV and COZI, and
  the names of programmes and films." Minny answered it, and the welcome
  greeting was never spoken or logged.
- *History:* the first such turn in `voice-conversations.jsonl` since the
  prompt took this form (64ed26b, 2026-10-04). That commit removed the
  example sentence because "Hi Minny." was echoed 42 times; the remaining
  vocabulary text can be echoed too.
- *Fix (to decide):* drop a final user transcript that matches the STT
  prompt (or a long run of it) before it becomes a turn, logging it like
  the echo-greeting filter does; check why the welcome was skipped.

**T3 [MEDIUM] "The second one" counts the tool's list, not the spoken one** (`prompts/tv/ytvn.md:44-46`) — **FIXED 2026-10-05, spoken re-test pending**
- *Found:* session 4 step 4.3. `tv_on_demand("Comedy")` returned Inside
  Out 2, Ant-Man and the Wasp: Quantumania, We're the Millers, …; Minny
  said "Inside Out 2, We're the Millers, Moonshot, …" (skipping Ant-Man).
  "The second one" → `tv_play_asset("59005")`, Ant-Man.
- *Wider than T3:* replaying the recorded request (production model and
  settings, instructions as pydantic-ai 2.31.1 sends them, no tool run),
  "the last one" played The Alternate Side, the tool's last title, which
  Minny never named (10/10), and after a spoken channel list "the third
  one" tuned KQED, the tool's third, instead of channel 45 (10/10).
- *Fix:* one bullet in ytvn.md's "To put something on" list: a pick by
  position counts only what Minny said, in her order. Chosen from five
  placements by side effects: all five fixed every ordinal case 10/10,
  but in 00-identity it added memory searches and clarifying questions; in
  the on-demand bullet it answered "Two and a Half Men tonight?" from
  memory without the guide (4/5); in 30-routing it answered "what am I
  watching" from the live guide row during a catch-up (3/5).
- *Verified:* ordinal cases fixed 10/10; "the first one", "Part 2" and
  "turn that channel on" (still asks which, 10/10) unchanged. All 129
  recorded turns from Oct 4–5 replayed with and without the fix, differing
  turns 5× more: no harmful shift. A garbled "Oh, they are saying." no
  longer changes the volume; "No, I meant what's on my TV" now calls
  `tv_get_state` first (as production already did). `model_bench.py`
  (gpt-5.6-terra): routing identical on 27/27, judge 4.89/4.63/5.00 →
  4.96/4.70/5.00. Agent restarted 10:21, `voice_probe.py` PASS on prompt
  8c8b6c10a103.
- *Left:* the spoken re-test of 4.2–4.3.

**T4 [MEDIUM] Genre preferences mostly yield no fact: graphiti won't make a genre an entity** (graphiti-core 0.29.3 `prompts/extract_nodes.py:83-160`; `memory.py`) — **FIXED 2026-10-05, spoken re-test passed**
- *Found:* session 6 step 6.2, 2026-10-05 10:28. "I don't like reality
  shows." was acknowledged and stored as an episode but produced no fact.
- *Cause:* a graphiti fact joins two entities, and graphiti's message
  entity prompt forbids generic nouns, keeps only things that "could have
  their own Wikipedia article" and ends "When in doubt, do NOT extract".
  "Reality shows" fails that test; our genre instruction, appended after
  those rules, mostly lost. Genre facts that did get through hung off
  user→assistant, so "I like them now" couldn't replace them. Not a
  regression: graphiti pinned since 2026-08-30, sentence first spoken today.
- *Fix (memory.py):*
  1. EXTRACTION_INSTRUCTIONS ends with: a genre or kind of programme IS an
     entity here, named by the genre ("Reality shows", "Horror").
  2. The profile filter accepts "User asked the assistant to set/make/
     remember … as their favourite" (_FAVOURITE_ASK): fix 1 shifted
     extraction of the household's real "Can you set... Colombo as my
     favorite." toward that wording (15/20 runs vs 8/20), which the
     one-off-request rule dropped. Asks to remove or forget a favourite
     stay excluded.
  3. _INSTRUCTION recognises "steer clear of suggesting …".
- *Verified:* the real VoiceMemory path (wants_memory → add_episode →
  supersede → profile) on scratch graphs, current vs new, 24 scenarios
  (test-plan cases, genres, supersede/forget sequences, all 12 exchanges
  that ever qualified for memory). No scenario worse; better: reality
  dislike 2/3→3/3, crime 2/3→3/3, horror 2/3→3/3, dislike then "I like
  them now" 2/3→3/3 with the old fact ended (current text once kept both),
  KQED then forget 2/3→3/3, volume 7/9→9/9, volume 0.6→0.5 3/9→7/9, the
  household's Colombo favourite 12/20→20/20. Filter: 13/13 unit cases;
  no change on any fact in voice_memory or voice_memory_test; of 139
  scratch facts only 12 changed, all asks to set a favourite. Installed
  file identical to the tested one; agent restarted 10:54, voice_probe
  PASS, test profile unchanged.
- *Not fixed here (separate):* "We don't watch sports." and "When I say
  turn it up, set the volume to twenty." never reach extraction
  (wants_memory says no); "We watch the news at six" never reaches the
  profile (0/3 either way); the user's name reaches it 1/3; volume
  corrections still fail 2/9 when the second statement yields no fact.
- *Re-tested:* session 6 re-run 10:58 on a wiped test graph: 4/4, profile
  has all three facts, the dislike on its own "Reality shows" entity.

**T5 [MEDIUM] Broad schedule searches showed only the programmes that started earliest** (`lineup_scope.py`, `search_lineup` / `window_rank`) — **FIXED 2026-10-05, spoken re-test passed**
- *Found:* session 7 step 7.2, 2026-10-05 11:03. "What should I watch
  tonight?" → `minny_search_live_schedule(range 6 PM–5 AM)` with no channel
  or query, so the wrapper searched all 53 lineup channels: 695 matches,
  sorted by start time, cut to 25. A programme already on when the window
  opens is a match, so all 25 rows had started between 4 AM and 5:57 PM and
  Minny suggested The Last Man on the Moon "at four fifteen" for tonight.
- *A regression?* Not from a working state. Every version ranked by start
  time and cut: the catalogue-wide search before Oct 4 returned ~a dozen
  earliest-starting rows (Oct 2 and Oct 4 "movie around five": 5 and 4
  rows, none starting in the window → "not seeing a film beginning around
  five"); 186766f (Oct 4 09:28) made the search per station and cut at 40;
  7482102 (Oct 4 17:18) cut at 25 with a note. The per-station search made
  it bite harder (53 channels' running programmes fill the cut), and the
  no-genre "tonight" question had never gone through this path: it was
  asked on Sep 1, 14, 22 and 26, all on the earlier backends. It did show
  as a symptom in 2.5 today ("25-row cap" in the note, undiagnosed).
- *Fix:* `window_rank`: rows are ordered from the window's start (or now,
  if later): what starts at that moment first, then what is already on
  with at least 30 minutes left (most time left first), then what starts
  next, and so on; what has ended or is about to goes last. The "now"
  window keeps its uncapped start order (dcfc902). The cap note says what
  the 25 are.
- *Verified:* unit checks of the ordering rules. Real row sets captured
  uncapped (tonight 695, around five 46, cooking 32, next two hours 181,
  today 1083, tomorrow evening 204, upcoming 1061), old cut vs new:
  tonight 0 → 19 rows starting at 6 PM (locals first) plus the 6 longest
  running blocks; around five drops the 6 films ending 2–28 min after 4:30
  for 5:00 starts; today asked at 11 AM 25 ended rows → 19 on now + 6
  next; cooking unchanged. The recorded 7.2 and 2.5 answers re-asked with
  the new rows (5× each): 7.2 → PBS NewsHour, Chicago Fire, M*A*S*H at six
  5/5 (old rows: "four seventeen" 4/4); 2.5 → LoudQUIETloud 5–7 with The
  Last Man on the Moon "already underway, ends 6:16" or Future From Above
  5/5; 2.6 cooking unchanged. model_bench (gpt-5.6-terra): routing 27/27,
  judge 4.96/4.67/4.96 vs 4.96/4.70/5.00 before; "around five" helpful
  4 → 5; the three one-point drops are on untouched paths (a catch-up
  wording, "50%" spoken as digits, news-now at a later hour). Agent
  restarted 11:21, voice_probe PASS with the new note in the rollout.
- *Left:* 25 rows cannot represent an 11-hour window: "tonight" asked at
  11 AM shows the 6 PM slot (asked at 7:30 PM it shows what is on then and
  the 8 PM starts, the realistic case). A sample spread across the window
  is a possible follow-up.
- *Re-tested:* session 7 re-run 11:28: "What should I watch tonight?" →
  America's Test Kitchen at seven, Check, Please! at seven thirty,
  Independent Lens at ten; the broad search's cut began at 6 PM.

**T6 [MEDIUM] A channel's evening was truncated: the catalogue caps a `channel` search at 25 rows shared by every feed the name matches** (`lineup_scope.py`, `guide_channel` / `LineupScopedToolset.call_tool`) — **FIXED 2026-10-05**
- *Found while fixing T5.* The catalogue resolves `channel` by name to up
  to 5 feeds and returns at most 25 rows across them, by start time. For
  the channel route ("what's on KQED tonight") the wrapper passed the
  station's short name, which matches every feed: "KPIX" → 5 feeds, 25
  rows, 6–8 PM; "KQED" → 4 feeds, 6–10 PM; "KTVU" → to 11 PM; "COZI" → to
  midnight; KQED "upcoming" → 2½ hours. The brain, asked what's on KPIX
  tonight, stopped at "FBI at eight".
- *Correction to the first filing:* the broad per-station search was never
  affected — it already passes each station's exact primary feed name
  (`_names[pid]`); measured on the T5 capture, 43 of 47 stations with rows
  tonight reach past 2 AM and the other four are long overnight blocks.
  Only the channel route used the all-feeds name.
- *Fix:* `guide_channel(said, exact=True)` returns the primary feed's own
  name (kept per lineup channel in `_exact` at map build); `call_tool`
  uses it for `minny_search_live_schedule` only. `minny_whats_on_now`
  (≤5 rows, no cap issue) and `minny_search_catchup` (recordings can sit
  on any feed) keep the all-feeds name, as does `minny_find_channels`.
- *Verified:* each exact name resolves to exactly one feed. Through the
  wrapper's own lookup and filter, tonight: KQED 6 → 13 rows (to 4:30 AM),
  KPIX 5 → 18 (to 4 AM), KTVU 8 → 16, COZI 13 → 22, KQED upcoming 6 → 25
  (18½ h); KNTV, Gusto TV, ION HD (one feed) unchanged. Brain check
  (questions only): KPIX tonight now ends "CIA at nine and Harlan Coben's
  Final Twist at ten"; "on KQED right now" and the catch-up question
  unchanged. model_bench: routing 27/27, judge 4.93/4.59/5.00 vs
  4.96/4.67/4.96 (the lower-scored questions are on the broad and on-now
  paths, untouched; "what's next on KQED" and "what is on KTVU" 5/5/5
  both runs). Restarted 11:59, voice_probe "What's on KQED tonight?" PASS
  with 13 rows, 6 PM–4:30 AM, in the rollout.
- *Residual (catalogue side):* 25 rows per feed still bound a long window
  (a 24-hour "upcoming" on a 30-minute-slot channel reaches ~12 h). The
  T5 ordering keeps those 25 from the window's start.

**T7 [HIGH] A title the guide spells as two words is "not scheduled" when STT writes it as one** (`lineup_scope.py`, `LineupScopedToolset.call_tool`) — **FIXED 2026-10-06**
- *Found:* satellite, 2026-10-06 18:39. "Do we have PBS NewsHour at some
  point today or tomorrow?" → "No — I checked today and tomorrow, and PBS
  NewsHour isn't scheduled in this lineup", while PBS News Hour was on
  KQED at that moment (the next turn, "what is it playing right now?",
  said so). At 18:38 the same query also denied a catch-up; two recordings
  existed (yesterday's 6 PM, today's 3 PM).
- *Cause:* the guide's text search needs every query word to match a word
  of the title. The catalogue spells it "PBS News Hour"; STT writes
  "NewsHour". Raw: "PBS NewsHour", "NewsHour", "newshour" → 0 rows;
  "PBS News Hour", "News Hour", "PBS News" → the KQED airings. The model
  retried with "NewsHour" alone, which cannot work. `pai_agent.py` already
  asks both ways for the TV's catch-up lookup (`_aired_recently`:
  `[title, pr.split_caps(title)]`); the model's direct guide searches had
  no such retry.
- *Fix:* in `call_tool`, for `minny_search_live_schedule` and
  `minny_search_catchup` with a `query`: when the filtered result is one of
  the wrapper's "nothing matches" results and `pr.split_caps(query)`
  differs, run the same call once more with the split query and return it
  if it has rows. Only after an empty result ("SportsCenter" IS one word
  in the catalogue and splits to a different programme); outage texts do
  not trigger it.
- *Verified:* `_no_match` recognises the two no-match texts and `[]`, not
  the outage texts or row lists. Brain checks (questions only): "search
  the guide for the title NewsHour" → the KQED airings; the catch-up
  question twice, with the model's own "PBS NewsHour" + KQED query → both
  recordings; "PBS NewsHour today or tomorrow" with channel → on now and
  tomorrow at three and six.
  model_bench: routing 27/27; no benchmark question has a run-together
  title, so the retry never ran there (judge 4.89/4.48/4.93 vs 4.93/4.59/
  5.00 the day before: time of day and variance). Restarted 18:47; the
  probe asked the exact question, the model queried "PBS NewsHour", the
  retry fired (logged) and the answer was right.
- *Also seen in that session (not fixed):* STT heard "Trace" as "trues",
  "truths", "Truce" three times (Minny recovered on "How about Trace?");
  "Let's get you back out" was read as stop → the player closed, Minny
  apologised when told.

