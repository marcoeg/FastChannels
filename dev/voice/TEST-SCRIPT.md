# Minny — spoken test script

A script to read aloud to the agent in **test mode**, session by session.
Each step: what to say, then what you should hear and see. IDs refer to
`TEST-PLAN.md` (expected tools and pass criteria in full).

Test mode = the phone client opened with `?test` (room `test-plan`): same
agent, but its traces are marked `"test": true` (left out of the NeMo
export), its memory is a separate graph (`voice_memory_test`), and its
exchanges never appear in a real session's "earlier sessions" — nor real
ones in it.

## Before you start

1. On the phone, open **https://192.168.50.17:8443/?test** (the IP: Android
   Chrome doesn't resolve `nucbox.local`). An orange **TEST MODE** bar must
   show at the top. Tap **Connect**. (No bar → plain `/`: that's your real
   household session.)
2. The player page is open and `serve.py` runs on :8787; the VPN is up.
3. Start from a clean test memory and a known volume:
   ```bash
   cd ~/Development/FastChannels/dev/voice
   ./memory_cli.py --test wipe --yes
   python3 ~/Development/ytvn-epg/remote.py volume 0.5
   ```
4. Keep the phone away from the TV speakers.
5. Programmes change: where a step names Columbo / COZI / BBC World News
   America, use whatever the guide has now — the shape of the test is what
   matters.

Mark each step ✓ / ✗ and note what was said when it fails.

---

## Session 1 — what's on and tuning

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 1.1 | "Hey Minny." | A short greeting, invites a request | J3 | ✓ (said ~1 s after connect: barged into the welcome, heard as "Minny.") |
| 1.2 | "What's on TV right now?" | 3–5 programmes, only this TV's channels, Pacific times | A1 | ✓ |
| 1.3 | "Turn that channel on." | Tunes the first channel it just named (or asks which) | F1 | ✓ asked which of the 4 |
| 1.4 | "What am I watching?" | That programme — answered at once, no "let me check" loop | A4 | ✓ |
| 1.5 | "Great." | Instant "Enjoy." | J1 | ✓ |
| 1.6 | "Switch channel to Fox, channel 2." | KTVU, channel two | C2 | ✓ |
| 1.7 | "Put on CBS." | KPIX, channel five | C3 | ✓ |
| 1.8 | "Play TMZ on KTBU." | Tunes KTVU; doesn't claim TMZ is on unless it checked | C5 | ✓ (STT heard "KTVU", so the call-sign fix was not exercised) |
| 1.9 | "Put on Trace." | Asks which: Urban, Sportstar or Latina | C7 | ✓ |
| 1.10 | "Trace Latina." | Tunes it | C7 | ✓ ("Tr3s Latina" → Trace Latina) |
| 1.11 | "Put on ESPN." | Not a channel on this TV | C8 | ✓ |
| 1.12 | "What's on channel nine?" | KQED's current programme | A3 | ✓ |
| 1.13 | "Thanks!" | Instant "You're welcome." | J1 | ✓ (instant reply: in voice-conversations.jsonl, not in rollouts) |

Disconnect.

*Run 2026-10-05 09:27, Pixel phone, room `test-plan`, gpt-5.6-terra:
13/13 pass. No echo: the only interruption was the welcome greeting.*

## Session 2 — later today and follow-ups

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 2.1 | "Is there any Columbo tonight?" | Channel and times (Pacific) | B1 | ✓ asked Two and a Half Men instead: COZI ch 12, 9–11 PM |
| 2.2 | "And what is after that?" | The airing after the one just named | F2 | ✓ George Lopez at 11 (treated the 9–11 block as one) |
| 2.3 | "How long does it last?" | Duration of that airing | F2 | ✓ 11 PM–1 AM, correct |
| 2.4 | "What's on next on KQED?" | Next programme, start time | B3 | ✓ |
| 2.5 | "Any movie I can watch at around five o'clock today?" | Only films airing around five | B4 | ~ LoudQUIETloud 5–7 ok; Monday M.A.S.S. not a film; missed real films spanning 5 (movie filter lets FAST filler through; the 25-row cut kept the earliest-starting rows, BUGLIST T5) |
| 2.6 | "Any cooking shows on tonight?" | Titles and times only, no invented episode details | B5 | ✓ (skipped Top Chef VIP on KSTS) |
| 2.7 | "What's on tomorrow morning on KPIX?" | Tomorrow's programmes, not today's | B7 | ✓ |
| 2.8 | "What's on TV right now?" then "No, I meant what is playing on my TV right now." | Second answer is what's on screen | F7 | ✓ |

Disconnect.

*Run 2026-10-05 09:37, Pixel phone, room `test-plan`, gpt-5.6-terra:
7 pass, 2.5 partial (search data, not reasoning). Time to first speech
0.9–4.2 s; the movie search was slowest.*

## Session 3 — catch-up and restart

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 3.1 | "Play the BBC World News America catch-up on Channel 9." | The newest edition plays from its start | D1 | ✓ negative only: BBC WNA is weekday-only, last aired Fri, not in catch-up. Positive path untested |
| 3.2 | "Tune to COZI." | Live COZI | C1 | ✓ |
| 3.3 | "Can you restart this episode from the beginning?" | Restarts at once (one action) | D3 | ✓ one `tv_play_airing(restart)`, begins 1 min early |
| 3.4 | "What is this episode about?" | Episode summary, no spoilers | F3 | ✗ "can't identify the episode" + web_lookup; the guide row had it (S9E1 "Shoeway to Heaven", with description). Prompt conflict: 30-routing vs 35-web |
| 3.5 | "Start PBS NewsHour over from the beginning." | If not on air: says so, offers the latest catch-up | D4 | ✓ split into two utterances, answered as one. Didn't offer the 3 PM airing today |
| 3.6 | "Play yesterday's NewsHour." | Plays if within 24 h, otherwise "the catch-up window has expired" | D5 | ✓ STT "Please yesterday's…"; nothing aired Sun |

Disconnect.

*Run 2026-10-05 09:41 (a Monday), Pixel phone, room `test-plan`, gpt-5.6-terra:
5 pass, 3.4 fail. 3.1/3.5/3.6 could only test the "not available" path —
BBC World News America and PBS NewsHour don't air on weekends. Re-run them
after 3 PM on a weekday (BBC WNA 2:30, NewsHour 3:00 on KQED).*

*Re-run 09:47 after the 3.4 prompt fix (prompt 9320ad4c1966): 3.4 ✓, one
`minny_whats_on_now("COZI")` call, answered from the guide row, no web search.
3.1 positive path ✓ with Daniel Tiger's Neighborhood (aired 9:00 on KQED):
catch-up played from the start. Reply took 11.6 s, all of it in two slow
OpenAI requests (6.2 s + 4.1 s for 19–31 output tokens; the play tool took
≤1.4 s). That's 2 of the only 4 requests over 4 s in 419 since Oct 2, so
it's provider latency, not the player. Two misses: "Play Daniel Tiger on
Channel 9" tuned it but didn't offer the 9:00 catch-up (ytvn.md:13-14), and
STT "BBS NewsHour" was searched as heard.*

## Session 4 — on demand

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 4.1 | "What is on demand that I can watch later?" | A few new releases from this TV's catalogue | E1 | ✓ Moonshot, In the Heights, Cruella, Fast X, Free Guy |
| 4.2 | "Any comedies on demand?" | Titles from the Comedy category | E2 | ✓ named 5 of the Comedy list, skipping its 2nd (Ant-Man) |
| 4.3 | "The second one." | Plays the title it named second | E7 | ✗ played Ant-Man (2nd in the tool list); she had said We're the Millers second. BUGLIST T3 |
| 4.4 | "Anything for the kids on demand?" | Family titles (or the category list) | E3 | ✓ Family category |
| 4.5 | "Put on Dune." | Asks Part One or Part Two | E6 | ✓ |
| 4.6 | "Part Two." | Dune: Part Two plays | E4 | ✓ |
| 4.7 | "Play Inside Out 2." | Plays, or "not in your package" | E5 | ✓ played (entitled, so E8's premise didn't occur) |
| 4.8 | "Can you buy it?" | Explains purchases are done in the Web Client | E8 | ~ "can't make purchases", true, but no Web Client pointer; no prompt rule covers it |

Disconnect.

*Run 2026-10-05 09:59, Pixel phone, room `test-plan`, gpt-5.6-terra:
5 pass, 4.3 fail, 4.8 partial. At connect, 0.74 s of audio came back from
STT as its own vocabulary prompt ("English conversation with Minny, a
TV-viewing assistant, …"); Minny answered it and the welcome never played.
BUGLIST T2. T3 fixed at 10:21 (prompt 8c8b6c10a103); 4.2–4.3 re-test
pending.*

## Session 5 — volume and playback

Have something playing first ("Tune to KQED"). Volume starts at 50%.

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 5.1 | "Turn it up." | 60% | G1 | ✓ 60% |
| 5.2 | "Quieter please." | 50% | G3 | ✓ 50% |
| 5.3 | "Mute it." | Muted | G8 | ✓ muted |
| 5.4 | "Turn it up." | Unmuted, 60% | G2 | ✓ unmuted, 60% |
| 5.5 | "Set the volume to twenty percent." | 20% | G5 | ✓ 20% |
| 5.6 | "Turn it up a lot." | About 50% | G6 | ✓ set 0.5 |
| 5.7 | "Set the volume to max." | 100% | G4 | ✓ 100% |
| 5.8 | "Pause it." / "Resume." | Pauses / plays | G9 | ✓ paused / playing |
| 5.9 | "Stop." | Player closes, grid shows | G9 | ✓ idle |
| 5.10 | "Resume." | Says nothing is playing — no error loop | G9 | ✓ one `tv_play` → not_ready; "Nothing is playing to resume." No retry loop |

Check the level after each step: `python3 ~/Development/ytvn-epg/remote.py state`.

*Run 2026-10-05 10:24, Pixel phone, room `test-plan`, gpt-5.6-terra:
10/10 pass. Every level checked against the player state each command
returned. One tool call per step; 2.5–4.6 s per turn.*

## Session 6 — teaching memory

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 6.1 | "Remember that my favorite channel is KQED." | Confirms | H1 | ✓ stored: "favourite channel is KQED" |
| 6.2 | "I don't like reality shows." | Acknowledges | H3 | ✗ first run: episode stored, no fact (BUGLIST T4). ✓ re-run after the fix: "User does not like reality shows." on entity "Reality shows" |
| 6.3 | "My normal volume is 0.4." | Confirms 40% | H5 | ✓ stored: "normal volume level is 0.4 (40%)" |
| 6.4 | "What's on TV right now?" | Normal answer — and this turn is NOT stored | H6 | ✓ answered (led with KQED, the new favourite); not stored: 3 episodes |

Disconnect, wait a minute (memory is written in the background), then check:
```bash
./memory_cli.py --test profile     # KQED favourite, no reality shows, normal volume 0.4
./memory_cli.py --test stats       # 3 episodes — not 4
```

*Run 2026-10-05 10:28, Pixel phone, room `test-plan`, gpt-5.6-terra:
3 pass, 6.2 fail (T4). Stats: 4 entities, 2 edges, 3 episodes. The profile's
"Earlier sessions" lists the 6.4 exchange too; that list is read from the
transcript log, not the graph.*

*Re-run 10:58 after the T4 fix (test memory wiped first): 4/4 pass. Profile:
KQED favourite, does not like reality shows, normal volume 0.4. Stats: 4
entities (user, KQED, Reality shows, Volume), 3 edges, 3 episodes.*

*Re-run 11:24 (after the T5 restart, test memory wiped first): 4/4 again.
Profile: all three facts. Stats: 5 entities (an "assistant" node this
time), 3 edges, 3 episodes.*

## Session 7 — memory in a new session

Reconnect (new session).

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 7.1 | "What's my favorite channel?" | KQED | H2 | ✓ "Your favorite channel is KQED.", from the profile, no tool |
| 7.2 | "What should I watch tonight?" | Suggestions, no reality shows | H3 | ~ no reality shows ("I skipped the reality fare"), but "tonight" got The Last Man on the Moon at 4:15 PM: every row the search returned started before 6 PM. BUGLIST T5 |
| 7.3 | "Put the volume back to normal." | 40% | G7 | ✓ `tv_volume(set 0.4)` → 40% |
| 7.4 | "What did I tell you last time?" | Recalls session 6 (KQED, reality shows, volume) — nothing invented | H8 | ✓ KQED, no reality shows, 40%, from the notes; nothing invented |
| 7.5 | "Forget that KQED is my favorite." | Confirms | H4 | ✓ KQED favourite ended in the graph; the forget request is a fact the profile filter keeps out |

Disconnect, wait a minute, then `./memory_cli.py --test profile` — KQED no
longer a favourite.

*Run 2026-10-05 11:03, Pixel phone, room `test-plan`, gpt-5.6-terra:
4 pass, 7.2 partial. Profile afterwards: reality-shows dislike and volume
0.4, KQED gone. Graph: 4 episodes, the KQED favourite ended (not deleted).
T5 fixed 11:21 (`lineup_scope.window_rank`).*

*Re-run 11:28 after the T5 fix: 7.2 ✓ — one KQED search plus one broad
movie search (158 matches, the new cut starting at 6 PM): "America's Test
Kitchen at seven, Check, Please! at seven thirty, Independent Lens at
ten", no reality shows. 7.1, 7.3, 7.5 ✓. 7.4 ~: called search_memory,
got all three facts back, and answered only "your usual volume is forty
percent" — read "last time" as the last thing said (the volume was the
last preference stated in session 6); nothing invented. The first run
answered with all three from the notes. Graph afterwards: the KQED
favourite ended; two new facts from the forget, "KQED is not the user's
favorite channel anymore" (shown in the profile; "no longer" facts are
kept by design, as for Al Jazeera) and "User asked assistant to forget…"
(a request, kept out).*

## Session 8 — out of scope and edge cases

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 8.1 | "What can you do?" | Live TV, guide, catch-up/restart, on demand, volume — no captions | J4 | ✓ content right (no captions claimed); the VOICE sounded odd — see run note |
| 8.2 | "Turn on captions." | This TV can't do captions | K1 | ✓ |
| 8.3 | "Record Columbo tonight." | Can't record; mentions catch-up | K3 | ✓ "can't schedule recordings", offered to check tonight / later |
| 8.4 | "Can you set a timer to turn the TV on that channel when it starts?" | Can't schedule; gives the start time | K2 | ✓ "can't set timers", offered the start time |
| 8.5 | "Go back to the last channel." | Asks which channel (no last-channel) | K5 | ✓ asked for the channel; "KQED" then tuned |
| 8.6 | "Is there any…" (stop mid-sentence) | Waits or asks you to finish | L3 | ✓ "Go ahead — what are you looking for?" |
| 8.7 | Ask "What's on TV right now?" and talk over the answer with "Stop — put on KQED." | Stops, tunes KQED | L4 | ✓ answer cut mid-word, KQED tuned |
| 8.8 | "Hey Wanda." | Answers as Minny | J5 | ✓ "Hi — I'm Minny." |

*Run 2026-10-05 11:37, Pixel phone, room `test-plan`, gpt-5.6-terra:
8/8 on content; no tool calls except the two KQED tunes. 8.1's reply
sounded odd to the tester (the voice, not the words). Pipeline: ElevenLabs
eleven_flash_v2_5, plugin default voice, no fixed language, no voice
settings; metrics and logs normal for that turn (TTS ttfb 0.18 s); the
generation is not in ElevenLabs history, so no audio to analyse. Cause
open.*

---

## After the run

All test turns of the day, with tools and what was said:
```bash
jq -c 'select(.test) | {t: .ts_start[11:19], user: .user_text,
       tools: [.response.output[] | select(.type=="function_call") | .name],
       said: .spoken_text[:100], ms: (.latency.ttfd_ms | floor)}' \
   ~/news/voice-rollouts/$(date +%F).jsonl
```
Latency targets: no tool ≤ 1.5 s, one tool ≤ 3 s, two tools ≤ 5 s;
acknowledgements ("Great.", "Thanks!") have no trace line at all — they
never reach the model (`grep closing_ack ~/news/voice-metrics.jsonl`).

Clean up when done: `./memory_cli.py --test wipe --yes`. Test traces stay
in the daily files (marked); `rollouts_export.py` skips them unless
`--include-test`.
