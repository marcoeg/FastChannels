# Introducing the `fastchannels` skill to Hermes

Paste everything below the rule to onboard the agent. It is deliberately short
— the skill file carries the detail, and the point of this prompt is to make
the agent load it, understand its boundaries, and prove it works before acting.

---

You now have a skill called `fastchannels`. It documents a server running on
this machine, and it refers to files in a repository at:

```
/home/marco/Development/FastChannels
```

**Resolve every path in the skill against that root** — it is not your working
directory. The skill file itself is `/home/marco/Development/FastChannels/dev/hermes/SKILL.md`.
Read it before doing anything with it.

**FastChannels** (`http://127.0.0.1:5523`) is a FAST-channel aggregator. It
scrapes free streaming services — Roku, Pluto, Xumo, Tubi, Samsung, Plex,
STIRR, Vizio, TCL and others, 37 configured with 18 enabled here — and
republishes them as M3U playlists and XMLTV guide data. Around 2,220 active
channels and ~95,000 programmes in the published guide, running about five days
ahead. The database holds more (it keeps disabled channels too), so trust
`/epg.xml` over the raw `programs_total` figure when you mean "what is
published".

With it you can list and filter channels across every source, read the full
schedule for a channel, get a playable stream URL, diagnose why a channel will
not play, build feeds, map Gracenote station IDs and export channel data.

**Its API is not documented by its author.** The spec in this repo was
reverse-engineered from the route handlers and verified against the running
server:

```
/home/marco/Development/FastChannels/
  docs/openapi.yaml    184 paths, every parameter and response schema
  docs/API.md          the same thing as prose, with worked examples
```

**Read the spec rather than guessing** — especially for the two things you will
reach for constantly:

- **Guide data.** Several routes return schedules and they differ in what they
  filter on and how far they look. `/epg.xml` takes `search`, `source`,
  `category` and `language` and returns everything stored; the per-channel
  routes are narrower and one of them returns a fixed five-hour window with
  display-formatted times. Picking the wrong one silently gives you a partial
  answer.
- **Stream endpoints.** Building a playable URL needs a field that
  `/api/channels` does not return, so the obvious approach does not work. The
  skill and the spec both show the routes that do.

Four things to internalise before you touch any of it:

1. **There is no authentication.** Every request you make will succeed,
   including destructive ones. The skill classifies endpoints as read-only,
   mutating, side-effecting and destructive — stay in the first two unless the
   task calls for more, and say what you changed when you do.

2. **Some reads are not reads.** Fetching a `/play/...` URL or calling
   `POST /api/channels/{id}/inspect` exercises the real playback path, and
   FastChannels' health check can auto-disable channels as a result. That has
   already happened here — 29 Roku channels in one sweep.

3. **The API has sharp edges that have caused real bugs.** Category filtering
   is substring on `/api/channels` but exact on `/m3u`. Gracenote-carrying
   channels live in a separate playlist and are absent from the main one, so
   fetching one silently loses 206 channels. `/api/channels` omits
   `source_channel_id`, so it cannot build a playback URL alone. Two different
   `503` responses mean different things. The skill lists these — read that
   section rather than rediscovering them.

4. **Roku rate-limits hard.** A `403` triggers a source-wide five-minute
   cooldown during which every Roku channel returns `503`. Retrying makes it
   worse.

To confirm the skill loaded and the server is reachable, run these and report
what you get:

```bash
curl -s http://127.0.0.1:5523/api/system-stats | jq '{uptime_seconds, db}'
curl -s "http://127.0.0.1:5523/api/channels?feed_eligible=1&per_page=1" | jq .total
curl -s "http://127.0.0.1:5523/api/stats" | jq '.categories[:5]'
curl -s "http://127.0.0.1:5523/epg.xml?search=Al%20Jazeera" | grep -c '<programme'
```

Then tell me, in your own words, what this server does, how you would get the
schedule for one channel and a playable URL for it, and one thing you could do
with it that I have not asked for yet.
