# Introducing the `fastchannels` skill to Hermes

Paste everything below the rule to onboard the agent. It is deliberately short
— the skill file carries the detail, and the point of this prompt is to make
the agent load it, understand its boundaries, and prove it works before acting.

---

You now have a skill called `fastchannels`, at
`.claude/skills/fastchannels/SKILL.md`. Read it before doing anything with it.

It covers two servers on this machine that work together:

**FastChannels** (`http://127.0.0.1:5523`) is a FAST-channel aggregator. It
scrapes ~37 free streaming services and republishes them as M3U playlists and
XMLTV guide data — roughly 4,500 channels with live programme data. It does not
record anything.

**Tvheadend** (`http://127.0.0.1:9981`) is the DVR, fed from FastChannels. It
mirrors the full lineup — 2,014 channels, ~91,000 EPG events refreshed
six-hourly — and it is what actually records. Recordings land in
`dev/tvheadend/recordings`.

Between them you can list and filter channels, diagnose why a channel will not
play, build feeds, map Gracenote station IDs, export channel data, schedule
recordings and set up series rules.

**Neither server's API is documented by its authors.** Both specs in this repo
were reverse-engineered from source and verified against the running servers:

- `docs/openapi.yaml` — FastChannels, 184 paths, with `docs/API.md` as prose
- `dev/tvheadend/openapi.yaml` — Tvheadend, 185 endpoints

Consult those when the skill does not cover what you need. Tvheadend also
describes itself: `*/class` and `mpegts/network/builders` return every valid
property with its type and description, which beats guessing.

Five things to internalise before you touch any of it:

1. **FastChannels has no authentication.** Every request you make will succeed,
   including destructive ones. The skill classifies endpoints as read-only,
   mutating, side-effecting and destructive — stay in the first two unless the
   task calls for more, and say what you changed when you do. Tvheadend does
   have auth (digest, `admin` / `fastchannels`).

2. **Some reads are not reads.** Fetching a `/play/...` URL or calling
   `POST /api/channels/{id}/inspect` exercises the real playback path, and
   FastChannels' health check can auto-disable channels as a result. That has
   already happened here — 29 Roku channels in one sweep.

3. **Never make Tvheadend scan its network.** That opens all 2,014 streams to
   probe them, which trips Roku's source-wide cooldown and mass-disables
   channels. It is configured to create channels from the playlist instead,
   with no stream opened.

4. **The APIs have sharp edges that have caused real bugs.** Category filtering
   is substring on `/api/channels` but exact on `/m3u`. Gracenote-carrying
   channels live in a separate playlist and are absent from the main one.
   `/api/channels` omits `source_channel_id`, so it cannot build a playback URL
   alone. Two different `503` responses mean different things. The skill lists
   these — read that section rather than rediscovering them.

5. **Check `dev/` before writing new code.** `dev/export_channels.py`,
   `dev/news_origins.py` and the `dev/tvheadend/` tooling already solve the
   common tasks and demonstrate correct usage.

To confirm the skill loaded and both servers are reachable, run these and
report what you get:

```bash
curl -s http://127.0.0.1:5523/api/system-stats | jq '{uptime_seconds, db}'
curl -s "http://127.0.0.1:5523/api/channels?feed_eligible=1&per_page=1" | jq .total
curl -s --digest -u admin:fastchannels http://127.0.0.1:9981/api/channel/grid?limit=1 | jq .total
curl -s --digest -u admin:fastchannels http://127.0.0.1:9981/api/dvr/entry/grid | jq .total
```

Then tell me, in your own words, what these two servers do together, and one
thing you could do with them that I have not asked for yet.
