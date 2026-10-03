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
| 1.1 | "Hey Minny." | A short greeting, invites a request | J3 | |
| 1.2 | "What's on TV right now?" | 3–5 programmes, only this TV's channels, Pacific times | A1 | |
| 1.3 | "Turn that channel on." | Tunes the first channel it just named (or asks which) | F1 | |
| 1.4 | "What am I watching?" | That programme — answered at once, no "let me check" loop | A4 | |
| 1.5 | "Great." | Instant "Enjoy." | J1 | |
| 1.6 | "Switch channel to Fox, channel 2." | KTVU, channel two | C2 | |
| 1.7 | "Put on CBS." | KPIX, channel five | C3 | |
| 1.8 | "Play TMZ on KTBU." | Tunes KTVU; doesn't claim TMZ is on unless it checked | C5 | |
| 1.9 | "Put on Trace." | Asks which: Urban, Sportstar or Latina | C7 | |
| 1.10 | "Trace Latina." | Tunes it | C7 | |
| 1.11 | "Put on ESPN." | Not a channel on this TV | C8 | |
| 1.12 | "What's on channel nine?" | KQED's current programme | A3 | |
| 1.13 | "Thanks!" | Instant "You're welcome." | J1 | |

Disconnect.

## Session 2 — later today and follow-ups

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 2.1 | "Is there any Columbo tonight?" | Channel and times (Pacific) | B1 | |
| 2.2 | "And what is after that?" | The airing after the one just named | F2 | |
| 2.3 | "How long does it last?" | Duration of that airing | F2 | |
| 2.4 | "What's on next on KQED?" | Next programme, start time | B3 | |
| 2.5 | "Any movie I can watch at around five o'clock today?" | Only films airing around five | B4 | |
| 2.6 | "Any cooking shows on tonight?" | Titles and times only, no invented episode details | B5 | |
| 2.7 | "What's on tomorrow morning on KPIX?" | Tomorrow's programmes, not today's | B7 | |
| 2.8 | "What's on TV right now?" then "No, I meant what is playing on my TV right now." | Second answer is what's on screen | F7 | |

Disconnect.

## Session 3 — catch-up and restart

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 3.1 | "Play the BBC World News America catch-up on Channel 9." | The newest edition plays from its start | D1 | |
| 3.2 | "Tune to COZI." | Live COZI | C1 | |
| 3.3 | "Can you restart this episode from the beginning?" | Restarts at once (one action) | D3 | |
| 3.4 | "What is this episode about?" | Episode summary, no spoilers | F3 | |
| 3.5 | "Start PBS NewsHour over from the beginning." | If not on air: says so, offers the latest catch-up | D4 | |
| 3.6 | "Play yesterday's NewsHour." | Plays if within 24 h, otherwise "the catch-up window has expired" | D5 | |

Disconnect.

## Session 4 — on demand

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 4.1 | "What is on demand that I can watch later?" | A few new releases from this TV's catalogue | E1 | |
| 4.2 | "Any comedies on demand?" | Titles from the Comedy category | E2 | |
| 4.3 | "The second one." | Plays the title it named second | E7 | |
| 4.4 | "Anything for the kids on demand?" | Family titles (or the category list) | E3 | |
| 4.5 | "Put on Dune." | Asks Part One or Part Two | E6 | |
| 4.6 | "Part Two." | Dune: Part Two plays | E4 | |
| 4.7 | "Play Inside Out 2." | Plays, or "not in your package" | E5 | |
| 4.8 | "Can you buy it?" | Explains purchases are done in the Web Client | E8 | |

Disconnect.

## Session 5 — volume and playback

Have something playing first ("Tune to KQED"). Volume starts at 50%.

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 5.1 | "Turn it up." | 60% | G1 | |
| 5.2 | "Quieter please." | 50% | G3 | |
| 5.3 | "Mute it." | Muted | G8 | |
| 5.4 | "Turn it up." | Unmuted, 60% | G2 | |
| 5.5 | "Set the volume to twenty percent." | 20% | G5 | |
| 5.6 | "Turn it up a lot." | About 50% | G6 | |
| 5.7 | "Set the volume to max." | 100% | G4 | |
| 5.8 | "Pause it." / "Resume." | Pauses / plays | G9 | |
| 5.9 | "Stop." | Player closes, grid shows | G9 | |
| 5.10 | "Resume." | Says nothing is playing — no error loop | G9 | |

Check the level after each step: `python3 ~/Development/ytvn-epg/remote.py state`.

## Session 6 — teaching memory

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 6.1 | "Remember that my favorite channel is KQED." | Confirms | H1 | |
| 6.2 | "I don't like reality shows." | Acknowledges | H3 | |
| 6.3 | "My normal volume is 0.4." | Confirms 40% | H5 | |
| 6.4 | "What's on TV right now?" | Normal answer — and this turn is NOT stored | H6 | |

Disconnect, wait a minute (memory is written in the background), then check:
```bash
./memory_cli.py --test profile     # KQED favourite, no reality shows, normal volume 0.4
./memory_cli.py --test stats       # 3 episodes — not 4
```

## Session 7 — memory in a new session

Reconnect (new session).

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 7.1 | "What's my favorite channel?" | KQED | H2 | |
| 7.2 | "What should I watch tonight?" | Suggestions, no reality shows | H3 | |
| 7.3 | "Put the volume back to normal." | 40% | G7 | |
| 7.4 | "What did I tell you last time?" | Recalls session 6 (KQED, reality shows, volume) — nothing invented | H8 | |
| 7.5 | "Forget that KQED is my favorite." | Confirms | H4 | |

Disconnect, wait a minute, then `./memory_cli.py --test profile` — KQED no
longer a favourite.

## Session 8 — out of scope and edge cases

| # | Say | Expect | ID | ✓ |
|---|---|---|---|---|
| 8.1 | "What can you do?" | Live TV, guide, catch-up/restart, on demand, volume — no captions | J4 | |
| 8.2 | "Turn on captions." | This TV can't do captions | K1 | |
| 8.3 | "Record Columbo tonight." | Can't record; mentions catch-up | K3 | |
| 8.4 | "Can you set a timer to turn the TV on that channel when it starts?" | Can't schedule; gives the start time | K2 | |
| 8.5 | "Go back to the last channel." | Asks which channel (no last-channel) | K5 | |
| 8.6 | "Is there any…" (stop mid-sentence) | Waits or asks you to finish | L3 | |
| 8.7 | Ask "What's on TV right now?" and talk over the answer with "Stop — put on KQED." | Stops, tunes KQED | L4 | |
| 8.8 | "Hey Wanda." | Answers as Minny | J5 | |

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
