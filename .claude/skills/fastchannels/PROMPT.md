# Introducing the `fastchannels` skill to Hermes

Paste everything below the rule to onboard the agent. It is deliberately short
— the skill file carries the detail, and the point of this prompt is to make
the agent load it, understand its boundaries, and prove it works before acting.

---

You now have a skill called `fastchannels`, at
`.claude/skills/fastchannels/SKILL.md`. Read it before doing anything with it.

It covers one server on this machine, plus the tooling around it:

**FastChannels** (`http://127.0.0.1:5523`) is a FAST-channel aggregator. It
scrapes ~37 free streaming services and republishes them as M3U playlists and
XMLTV guide data — roughly 4,500 channels with live programme data. It does not
record anything.

**`record.py`** (repo root) is the DVR. It captures with ffmpeg straight from
FastChannels and schedules with systemd timers, so there is no second server
and no state file — `systemctl --user list-timers` is the schedule. Recordings
land in `~/news/recordings`.

Between them you can list and filter channels, diagnose why a channel will not
play, build feeds, map Gracenote station IDs, export channel data, schedule
recordings and set up series rules.

**FastChannels' API is not documented by its author.** The spec in this repo
was reverse-engineered from the route handlers and verified against the running
server: `docs/openapi.yaml`, 184 paths, with `docs/API.md` as prose. Consult it
when the skill does not cover what you need.

Four things to internalise before you touch any of it:

1. **FastChannels has no authentication.** Every request you make will succeed,
   including destructive ones. The skill classifies endpoints as read-only,
   mutating, side-effecting and destructive — stay in the first two unless the
   task calls for more, and say what you changed when you do.

2. **Some reads are not reads.** Fetching a `/play/...` URL or calling
   `POST /api/channels/{id}/inspect` exercises the real playback path, and
   FastChannels' health check can auto-disable channels as a result. That has
   already happened here — 29 Roku channels in one sweep.

3. **The APIs have sharp edges that have caused real bugs.** Category filtering
   is substring on `/api/channels` but exact on `/m3u`. Gracenote-carrying
   channels live in a separate playlist and are absent from the main one.
   `/api/channels` omits `source_channel_id`, so it cannot build a playback URL
   alone. Two different `503` responses mean different things. The skill lists
   these — read that section rather than rediscovering them.

4. **Check `dev/` before writing new code.** `record.py`, `vlc.py`,
   `dev/export_channels.py` and `dev/news_origins.py` already solve the common
   tasks and demonstrate correct usage.

To confirm the skill loaded and both servers are reachable, run these and
report what you get:

```bash
curl -s http://127.0.0.1:5523/api/system-stats | jq '{uptime_seconds, db}'
curl -s "http://127.0.0.1:5523/api/channels?feed_eligible=1&per_page=1" | jq .total
./record.py epg --channel "Al Jazeera" --limit 3
./record.py list
```

Then tell me, in your own words, what this server does and how recording
works, and one thing you could do with it that I have not asked for yet.
