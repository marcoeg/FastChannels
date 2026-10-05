# Minny voice agent — test plan

Voice requests and multi-turn scenarios for testing the standalone voice agent
(`voice_agent.py`, `VOICE_TV=ytvn`, guide = Minny MCP, memory = Graphiti on
`falkordb-voice`). Compiled on 2026-10-03 from:

- **Transcripts** `~/news/voice-conversations.jsonl` — 728 user utterances,
  2026-08-29 → 2026-10-03 (phone room `voice` and satellite room `speaker`).
- **Rollout traces** `~/news/voice-rollouts/*.jsonl` — 76 traced turns since
  2026-10-01 (prompt, tools, results, memory block, latency, signals).
- **Agent log** `journalctl --user -u voice-agent` since 2026-10-01.
- The model benchmark (`model_bench.py`) and the fixes made since.

Every case below is a real utterance (marked **R**) or derived from a real
failure or a feature that must not regress (**D**). Expected tools use the
names the model sees.

## What the logs show

| Area | Share of utterances | Notes |
|---|---|---|
| What's on / playing now | 14% | Most common request; "what's on TV" vs "what is playing on MY TV" is confused often (3 corrections) |
| Later / tonight / at a time | 10% | Time words in Pacific; guide rows now carry `start_local` |
| On demand / movies | 9% | Entitlement failures until 2026-10-03; Minny VOD ≠ the TV's catalogue (fixed) |
| Programme info (plot, cast, episode) | 7% | Guide + `web_lookup` |
| Tune / change channel | 5% | Call signs misheard (KTBU, KMTVU), "Fox, channel 2" (fixed) |
| Volume / mute / transport | 5% | The old "turn it up → twenty" rule lowered the volume (fixed) |
| Catch-up / restart / memory | 2% | New since 2026-10-02 |
| Acknowledgements | 10% | Model returned an empty first reply 18× (fixed: answered without the model) |
| Corrections ("no, I meant…") | 3% (19) | Mostly STT errors and "what's on" ambiguity |
| Other | 46% | Follow-ups ("turn that on", "how long does it last?"), small talk, capability questions, TV audio picked up as speech, misrecognized foreign-language fragments, cut-off fragments |

Failure signals in the traces: 10 turns with no spoken reply (preemptive
cancels, barge-ins, one STT-split turn); tool errors were `not_ready` (play
with nothing playing) and `not entitled` (VOD, before the account fix). Agent
log: OpenAI Realtime STT disconnects (4), "transcript arrives after turn has
been committed" (5), "current speech generation cannot be interrupted" (11 —
mostly TV audio), lineup-map failures when the VPN was down.

## How to run

- **By voice** on the satellite (room `speaker`) or the phone client. Keep the
  satellite away from the TV speakers (TV audio becomes speech — see L1).
- **One turn, no audio:** `VOICE_TV=ytvn uv run --with aiohttp --script
  minny_brain_check.py "<request>"` — real tools (the TV does change), no
  memory, fresh history.
- **Benchmark:** `./model_bench.py --models gpt-5.6-terra` — 26 single-turn
  requests, TV in dry run, judged.
- **Check a turn afterwards:** today's trace in
  `~/news/voice-rollouts/$(date +%F).jsonl` (tools, arguments, results, memory
  block, latency); the player state with `python3 ~/Development/ytvn-epg/remote.py state`.

Preconditions: VPN up (guide), `serve.py` on :8787 with the player page open,
voice-agent active. For each case record: pass/fail, tools called, what was
said, first-audio latency.

**Latency targets** (model time to first text, from the traces): no tool
≤ 1.5 s · one tool ≤ 3 s · two tools ≤ 5 s · closing acknowledgement ≈ 0
(no model call).

---

## A. What's on now

| ID | Say | Expect | Pass when | |
|---|---|---|---|---|
| A1 | "What's on TV right now?" | `minny_whats_on_now` (no channel) | Names 3–5 programmes, all on this TV's channels (KTVU, KQED, COZI …, never Fox Sports 2 / EPIX), times in Pacific words | R |
| A2 | "What is on KTVU?" | `minny_whats_on_now(channel=KTVU)` | The current programme and when it ends | R |
| A3 | "What's on channel nine?" | `minny_whats_on_now` with KQED | Channel number resolved to KQED | D |
| A4 | "What am I watching?" (something playing) | none, or the guide for episode details | Uses the "On screen now" line; no `tv_get_state` | R |
| A5 | "What is playing on my TV right now?" (grid open, nothing playing) | none | Says nothing is playing — doesn't list what's on air | R |
| A6 | "Is there any news on right now?" | `minny_whats_on_now` or live schedule | Only lineup channels (KNTV, KQED, KTVU …) | R |
| A7 | "What's on ESPN?" | guide or `tv_watch_channel` | ESPN isn't on this TV — says so, offers nothing invented | D |
| A8 | "Show me the line-up." | `minny_whats_on_now` | Answers with what's on (no list of 53 channels read aloud) | R |

## B. Later / schedule

| ID | Say | Expect | Pass when | |
|---|---|---|---|---|
| B1 | "Is there any Columbo tonight?" | `minny_search_live_schedule` | Correct times (Pacific), COZI | R |
| B2 | "Is there any Columbo episodes today?" | live schedule, today's range | All airings, times matching `start_local` | R |
| B3 | "What's on next on KQED?" | live schedule on KQED | The next programme and its start time | R |
| B4 | "Any movie I can watch at around 5 o'clock today?" | live schedule, kind movie | Only films airing around 5 (not ones ending at 4:58) | R |
| B5 | "Any cooking shows on tonight?" | live schedule | Titles and times only — no invented episode details | R |
| B6 | "At what time can I watch the movie Speed?" | live schedule, query | Time and channel, or "not on today" | R |
| B7 | "What's on tomorrow morning on KPIX?" | live schedule, range in Pacific | Tomorrow's rows, not today's | D |
| B8 | "What's on at three in the morning?" | live schedule | Answers from the guide (it reaches ~a week ahead); far-future empty → "the guide may not reach that far" | D |

## C. Tuning and channel names

| ID | Say | Expect | Pass when | |
|---|---|---|---|---|
| C1 | "Tune on KNTV." | `tv_watch_channel(KNTV)` | KNTV playing, one call | R |
| C2 | "Switch channel to Fox, channel 2." | `tv_watch_channel` | KTVU 2 | R |
| C3 | "Put on CBS." | `tv_watch_channel` | KPIX 5 | D |
| C4 | "Switch to channel 1001." | `tv_watch_channel` | Gusto TV | R |
| C5 | "Play TMZ on KTBU." | `tv_watch_channel` | Tunes KTVU (one-letter mishearing); doesn't claim TMZ is on without checking | R |
| C6 | "Tune the TV to KMTVU." | `tv_watch_channel` | KTVU or asks which | R |
| C7 | "Put on Trace." | `tv_watch_channel` | Asks which: Trace Urban, Sportstar or Latina | D |
| C8 | "Put on ESPN." | `tv_watch_channel` | "Not a channel on this TV" | D |
| C9 | "I want to view Columbo, now." | `tv_watch_channel(COZI)` (from context or the guide) | COZI playing Columbo | R |
| C10 | "Now tune the TV to Colombo." | `tv_watch_channel(COZI)` | Misspelling resolved | R |
| C11 | "Play Love Nature." | `tv_watch_channel` → falls through to title | Offers the on-demand titles | D |
| C12 | First tune of a session on an unclicked page | tune, then `tv_unmute` | Sound on (autoplay starts muted) | D |

## D. Catch-up and restart

| ID | Say | Expect | Pass when | |
|---|---|---|---|---|
| D1 | "Play the BBC World News America catch-up on Channel 9." | ONE `tv_watch_title(BBC World News America, channel=9, catchup=true)` (or `minny_search_catchup` → `tv_play_airing(…, catchup)`) | Newest edition plays; state `catch-up` | R |
| D2 | "I want to watch a catch-up of BBC News." | `tv_watch_title(BBC News, catchup=true)` or `minny_search_catchup` (+ play) | Plays or asks which edition | R |
| D3 | (Columbo playing) "Can you restart this Columbo episode from the beginning?" | ONE `tv_play_airing(…, restart)` | airing_id from the "On screen now" line; state `restart` | R |
| D4 | "Start PBS NewsHour over from the beginning." (not on air) | `tv_watch_title(PBS NewsHour, catchup=true)` | Says it isn't on now; offers catch-up of the latest edition if there is one | R |
| D5 | Catch-up of yesterday's 5:30 pm edition when older than 24 h | `tv_watch_title(…, catchup=true)` | "Catch-up window expired" — no retry loop | D |
| D6 | Restart on a channel without restart (any FAST channel) | `tv_play_airing` | Says restart isn't offered for that channel | D |

## E. On demand

| ID | Say | Expect | Pass when | |
|---|---|---|---|---|
| E1 | "What is on demand that I can watch later?" | `tv_on_demand()` | New Releases from the TV's catalogue (not `minny_search_vod`) | R |
| E2 | "Any comedies on demand?" | `tv_on_demand(comedy)` | Titles from the Comedy category | D |
| E3 | "Any westerns on demand?" / "Anything for the kids?" | `tv_on_demand(western / family)` | Category resolved; if unknown, lists categories | D |
| E4 | "Can you play Dune part 2?" | `tv_watch_title(Dune Part Two, 2024)` | Plays (`playing.kind` vod) | R |
| E5 | "Play Inside Out 2." | `tv_watch_title` | Plays, or "not in your package" when not entitled | R |
| E6 | "Put on Dune." | `tv_watch_title` | Asks Part One or Part Two | D |
| E7 | E2, then "The second one." | `tv_play_asset(asset_id)` | Plays the title named second | D |
| E8 | "Can you buy it?" (after E5 not entitled) | none | Explains purchase happens in the Web Client, not by voice | R |
| E9 | "So, where is Dune Part 2? Which platform?" | `tv_watch_title` or `web_lookup` | Says it's on demand on this TV | R |

## F. Follow-ups and context (multi-turn)

Run each line in order in one session.

| ID | Turns | Pass when | |
|---|---|---|---|
| F1 | "What's on TV right now?" → "Turn that channel on." | Tunes the channel just named first (asks if several) | R |
| F2 | "Is there any Columbo tonight?" → "And what is after that?" → "How long does it last?" | Uses the previous answer; times correct | R |
| F3 | (Columbo playing) "What is this episode about?" | Guide + `web_lookup`; no spoilers | R |
| F4 | "When did the current Columbo show start?" | From the "On screen now" line or the guide | R |
| F5 | "What else can I watch instead?" (after a suggestion) | New suggestions, not repeats | R |
| F6 | "Go back on Columbo." (after switching away) | Tunes COZI (no "last channel" command) | R |
| F7 | "No, I meant what is playing on my TV right now." (after A1) | Switches to what's on screen | R |
| F8 | "Play TMZ on KTBU." → "I meant KTVU." | Second turn tunes KTVU, no repeated question | R |

## G. Volume, mute and playback

Start with the player at a known level (`remote.py volume 0.5`).

| ID | Say | Expect | Pass when | |
|---|---|---|---|---|
| G1 | "Turn it up." | `tv_volume(up)` | +10%, never lower | D |
| G2 | "Turn it up." while muted | `tv_volume(up)` | Unmuted and +10% | D |
| G3 | "Quieter please." / "Turn the volume down a bit." | `tv_volume(down)` | −10% | R |
| G4 | "Set the volume to max." | `tv_volume(set, 1)` (+ `tv_unmute` if muted) | Volume 100%, audible | R |
| G5 | "Set the volume to twenty percent." | `tv_volume(set, 0.2)` | 20% | R |
| G6 | "Turn it up a lot." | `tv_volume(set, current + 0.3)` | Uses "On screen now"; no extra `tv_get_state` | D |
| G7 | "Put the volume back to normal." | `tv_volume(set, 0.6)` | Uses memory's normal volume (60%) | D |
| G8 | "Mute the volume." / "Unmute." | `tv_mute` / `tv_unmute` | Done, short confirmation | R |
| G9 | "Pause it." / "Resume." / "Stop." | `tv_pause` / `tv_play` / `tv_stop` | Done; "resume" with nothing playing says so (no `not_ready` loop) | R |

## H. Memory

| ID | Turns | Pass when | |
|---|---|---|---|
| H1 | "Remember that my favorite channel is KQED." | Stored (graph gains an episode/fact: `./memory_cli.py stats`); profile shows it next session | D |
| H2 | "What's my favorite show?" | Columbo, from the profile — no tool, or `search_memory` | R |
| H3 | "I don't like reality shows." → next session "What should I watch tonight?" | Suggestions avoid reality shows | D |
| H4 | "Forget that KQED is my favorite." | The fact is invalidated; not in the profile next session | R |
| H5 | "My normal volume is 0.5." | Replaces 0.6 (old fact invalidated); G7 now sets 0.5 | D |
| H6 | Ordinary turns (A1, C1, G1) | Nothing written to the graph | D |
| H7 | New session after a refusal ("isn't available") | The refusal is not in "Earlier sessions" | D |
| H8 | "What did I ask you yesterday?" | `search_memory` or the earlier-sessions notes; no invention | D |

## I. Programme info and web

| ID | Say | Expect | Pass when | |
|---|---|---|---|---|
| I1 | "Who directed Dune Part Two?" | `web_lookup` or general knowledge | Denis Villeneuve | D |
| I2 | "Now tell me more about this AFV, what is it?" | `web_lookup` | Explains America's Funniest Home Videos | R |
| I3 | "Is I Believe on CBS News local?" | guide | Answers from the guide or says it can't tell | R |
| I4 | "Can you access their website and check?" | `web_lookup` | Uses web search, doesn't claim to browse sites | R |

## J. Small talk, acknowledgements, capabilities

| ID | Say | Expect | Pass when | |
|---|---|---|---|---|
| J1 | "Great." / "OK, great." / "Thanks!" (after an action) | no model call | Instant "Enjoy." / "You're welcome."; trace has no turn (metrics `closing_ack`) | R |
| J2 | "Yes." right after "Want me to put one on?" | model | Treated as an answer — does the action | R |
| J3 | "Hey Minny." / "Are you there?" | none | Short greeting, invites a request | R |
| J4 | "What can you do?" / "What kind of TV device are you?" | none | Accurate capabilities: live, guide, catch-up/restart, on demand, volume — not captions | R |
| J5 | "Hey Wanda." | none | Answers as Minny | R |

## K. Unsupported or out of scope

| ID | Say | Pass when | |
|---|---|---|---|
| K1 | "Turn on captions." | Says this TV can't do captions | D |
| K2 | "Can you set a timer to turn the TV on that channel when it starts?" | Says it can't schedule; offers the start time | R |
| K3 | "Record Columbo tonight." | Says recording isn't available; mentions catch-up (24 h) | D |
| K4 | "Buy Dune Part Two." | Explains purchases go through the Web Client | R |
| K5 | "Go back to the last channel." | No surfing / last-channel: asks which channel or tunes the named one | D |

## L. Robustness

| ID | Condition | Pass when | |
|---|---|---|---|
| L1 | TV loud near the satellite; TV dialogue transcribed ("You have the right to retain a lawyer") | No action taken on TV speech; real requests still answered (move the satellite away — known limit) | R |
| L2 | Misrecognized foreign-language fragment ("La Tredens dettò 1100 canali") | Asks to repeat, in English — doesn't act or answer in another language | R |
| L3 | Cut-off fragment ("Is there any…", "I want to know what…") | Waits or asks to finish; no guess | R |
| L4 | Barge-in: speak over a long answer | Stops, answers the new request | R |
| L5 | VPN down | "The TV guide is temporarily unavailable"; channel tuning still works | R |
| L6 | Player page closed (`no_shell`) | "The TV isn't connected" once — no retry loop | D |
| L7 | Proxy (`serve.py`) stopped | Same as L6; lineup lookups degrade without a crash | D |
| L8 | Agent restart mid-session | Satellite answered again within ~15 s (healer) | D |
| L9 | STT disconnect (OpenAI Realtime) | Next utterance still transcribed; session continues | R |

## Bugs and gaps these tests target

Seen in the logs and not yet fully covered by code:
- **F1 / F2 deictic follow-ups** depend on the model carrying context; older
  tool results are blanked after two exchanges.
- **L1 TV audio** — needs physical separation or echo handling; the agent
  can't tell TV speech from the viewer.
- **L2 language** — STT occasionally returns other languages; the prompt has
  no rule for it.
- **C5 / B5 invented details** — the benchmark found the model claiming TMZ
  was on and inventing dish details; watch faithfulness.
- **K2 / K3** — no reminders or recordings; check the refusal is clear.
