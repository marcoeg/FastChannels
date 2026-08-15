# Introducing the `fastchannels` skill to Hermes

Paste the prompt below to onboard the agent. It is deliberately short — the
skill file carries the detail, and the point of this prompt is to make the
agent load it, understand its boundaries, and prove it works before acting.

---

You now have a skill called `fastchannels`, at
`.claude/skills/fastchannels/SKILL.md`. Read it before doing anything with it.

It gives you control of a FastChannels server running on this machine at
`http://127.0.0.1:5523` — a FAST-channel aggregator that scrapes free streaming
services and republishes them as M3U playlists and XMLTV guide data for
Channels DVR and Plex. Roughly 4,500 channels across 37 sources, with live
programme data.

The skill covers listing and filtering channels, diagnosing why a channel will
not play, building feeds (saved filters with their own channel numbering),
Gracenote station mapping, exporting channel data, and registering outputs with
Channels DVR. The complete machine-readable reference is `docs/openapi.yaml` in
this repo — 182 paths, verified against the server's route handlers — with
`docs/API.md` as the prose companion. Consult those when the skill does not
cover what you need.

Four things to internalise before you touch it:

1. **There is no authentication.** Every request you make will succeed,
   including destructive ones. The skill classifies endpoints as read-only,
   mutating, side-effecting, and destructive. Stay in the first two unless the
   task explicitly calls for more, and tell me what you changed when you do.

2. **Some reads are not reads.** Fetching a `/play/...` URL or calling
   `POST /api/channels/{id}/inspect` exercises the real playback path, which
   can cause the server to auto-disable channels it judges DRM-protected or
   dead. That has already happened here — 29 Roku channels in one sweep.

3. **The API has sharp edges that have caused real bugs.** Category filtering
   is substring on `/api/channels` but exact on `/m3u`. Channels with a
   Gracenote ID live in a *separate* playlist and are absent from the main one.
   `/api/channels` omits `source_channel_id`, so you cannot build a playback
   URL from it alone. Two different `503` responses mean different things. The
   skill lists all of these; read that section rather than rediscovering them.

4. **Check `dev/` before writing new code.** `dev/export_channels.py` and
   `dev/news_origins.py` already solve common tasks and demonstrate correct
   usage patterns.

To confirm the skill loaded and the server is reachable, run these three and
report what you get:

```bash
curl -s http://127.0.0.1:5523/api/system-stats | jq '{uptime_seconds, db}'
curl -s http://127.0.0.1:5523/api/stats | jq '.categories[:5]'
curl -s "http://127.0.0.1:5523/api/channels?feed_eligible=1&per_page=1" | jq .total
```

Then tell me, in your own words, what this server does and one thing you could
do with it that I have not asked for yet.
