---
name: fastchannels
description: Live TV, EPG guide data and stream URLs via FastChannels.
---

# FastChannels API

FastChannels scrapes free streaming services (Roku, Pluto, Xumo, Tubi, Samsung,
Plex, STIRR, Vizio, TCL and others) and republishes them as M3U playlists and
XMLTV guide data for Channels DVR, Plex, and generic IPTV clients. Everything
its admin UI does, it does over the HTTP API described here.

## When to use this

Reach for this skill whenever a task involves:

- live TV channels, channel lineups, or streaming sources
- EPG or guide data — what is on now or later, guide gaps, stale programming
- getting a playable stream URL for a channel
- M3U playlists or XMLTV output
- Channels DVR or Plex integration
- anything naming FastChannels, or the API at `127.0.0.1:5523`

**Schedule questions have a faster path.** The EPG knowledge graph (the
`epg-graph` skill, `mcp__epg_graph__*` tools on its own MCP server at
`127.0.0.1:8001`) covers the full XMLTV lineup for the next ~24 hours,
refreshed hourly: `search_memory_facts` for "when is X on / is anything
about Y airing", `list_memory_facts` for complete window or per-channel
listings. Prefer it over parsing guide XML for those questions. This skill
remains the source of truth for stream URLs, lineup and source management,
anything beyond the graph's 24 h window, and the **Gracenote-partition
channels** (they have no XMLTV guide, so they never appear in the graph —
see "Always fetch both playlists" below).

What it lets you do: list and filter channels across every source, get a
channel's playback URL, read the full schedule for a channel, diagnose why a
channel will not play, build and manage feeds (saved filters with their own
numbering), map Gracenote station IDs, export channel data, and register
outputs with Channels DVR.

## This install

```
Base URL   http://127.0.0.1:5523      (also on the LAN as http://nucbox.local:5523)
Version    5.0.4
Runtime    Docker, container `fastchannels`
Repo root  /home/marco/Development/FastChannels
```

**Every file path in this document is relative to that repo root.** Nothing
here lives in your own working directory, so resolve paths against it — e.g.
the spec is `/home/marco/Development/FastChannels/docs/openapi.yaml`.
If that directory does not exist on the machine you are running on, the API is
still usable over HTTP but none of the local files are.

Get live figures rather than assuming — counts change with every scrape:

```bash
curl -s http://127.0.0.1:5523/api/system-stats | jq '{uptime_seconds, db}'
curl -s http://127.0.0.1:5523/api/stats | jq '.categories[:5]'
```

**There is no authentication.** Not a single route checks a credential, and the
port is published on the LAN, so there is no access control at all — which is
why every request below works with a bare `curl`. It also means *any* call you
make succeeds, including destructive ones. Be correspondingly careful: see the
safety section.

Use `127.0.0.1:5523` for your own calls. `nucbox.local:5523` is the same
server, and is what the M3U and XMLTV outputs hand to clients so that other
devices can reach the streams.

Local configuration deviates from upstream defaults: the port is published on
all interfaces so LAN clients can reach it (a deliberate choice — hence the
caution above), `VERSION_CHECK_ENABLED=0`, and `FC_YTDLP_AUTO_UPGRADE=0`
(weekly unattended `pip install` from GitHub master is disabled).

## The API is documented — read the spec first

FastChannels ships no API documentation. The spec in this repo was
reverse-engineered from the route handlers and verified against the running
server:

```
/home/marco/Development/FastChannels/
  docs/openapi.yaml    184 paths, every parameter and response schema
  docs/API.md          the same thing as prose, with worked examples
```

**Read it before guessing at a route or a parameter.** It is the authority for
the two things you will reach for most:

- **Guide data** — which endpoint returns a schedule, what each one filters on,
  and the window it covers. `/epg.xml` takes `search`, `source`, `category` and
  `language`; the per-channel routes differ in important ways. See
  [Get EPG for a channel](#get-epg-for-a-channel).
- **Stream endpoints** — how to build a playable URL for a channel, which
  requires a field `/api/channels` does not return. See
  [Get a channel's playback URL](#get-a-channels-playback-url).

`dev/check_api_docs.py` (under the repo root) verifies the spec against the
route handlers, so drift is a bug rather than something to work around.

## Safety classification

**Read-only — always fine:**
`GET /api/channels`, `/api/sources`, `/api/stats`, `/api/system-stats`,
`/api/logs`, `/api/feeds`, `/api/channels/{id}/preview`,
`/api/channels/{id}/duplicates`, `/api/gracenote/*` GETs, and every output
endpoint (`/m3u`, `/epg.xml`, `/feeds/{slug}/*`).

**Mutating — fine when the task calls for it, but say what you changed:**
`PATCH /api/channels/{id}`, `POST /api/channels/bulk`, `POST /api/feeds`,
`PATCH/DELETE /api/feeds/{id}`, `POST /api/settings`,
`POST /api/sources/{id}/run`.

**Has non-obvious side effects — check before using:**
- `POST /api/channels/{id}/inspect` and anything that fetches `/play/...`
  exercises the real playback path. The server runs its background health check
  and **may auto-disable channels** it finds DRM-protected or dead.
- `POST /api/custom-channels/detect` points a headless browser at an arbitrary
  URL with no host filtering. Do not feed it untrusted input.

**Destructive — do not call without explicit instruction:**
`POST /api/settings/restore-db` (replaces the database),
`DELETE /api/channels/{id}`, `DELETE /api/sources/{id}/channels`,
`POST /api/channels/delete-inactive`, `POST /api/settings/tve/reset`,
`POST /api/gracenote/submit-contributions` (sends data to a third-party
webhook). `GET /api/settings/backup-db` downloads the whole database including
plaintext provider credentials — do not write it anywhere shared.

## The five surfaces

| Prefix | Purpose |
| --- | --- |
| `/m3u`, `/epg.xml`, `/feeds/{slug}/…` | Playlist and guide outputs — what a DVR consumes |
| `/play/…`, `/watch/…` | Stream resolution, manifest proxies, DRM license proxy |
| `/logos/…`, `/images/proxy/…` | Cached image proxy |
| `/api/…`, `/api/feeds/…` | Control API — ~130 operations |
| `/admin/…` | HTML pages — **except** `/admin/guide/programs` and `/admin/channels/chnum-map`, which return JSON |

Full machine-readable reference: `/home/marco/Development/FastChannels/docs/openapi.yaml`
(184 paths, verified against the route handlers by `dev/check_api_docs.py`).
`docs/API.md` beside it is the prose companion.

## Core recipes

### List channels

```bash
# paginated; default 50 per page
curl -s "$BASE/api/channels?source=roku&search=news&per_page=100" | jq '.total, .channels[0]'

# reduced field set — much faster for large lineups
curl -s "$BASE/api/channels?category=Sports&slim=1&per_page=1000" | jq '.channels[] | .name'

# only channels that can actually appear in a playlist
curl -s "$BASE/api/channels?feed_eligible=1&per_page=1" | jq .total
```

Filters: `source` (exact), `category` (**substring**), `search` (substring on
name), `feed_eligible`, `slim`, `page`, `per_page`.

### Get a channel's playback URL

`/api/channels` does **not** return `source_channel_id`, so you cannot build a
`/play/...` URL from it. Two ways to get one:

```bash
# per channel — also gives now/next programming and EPG depth
curl -s "$BASE/api/channels/1314/preview?detect=0" | jq '{play_url, preview_url, epg_hours, current_program}'

# in bulk — parse the M3U outputs, which carry channel-id and the URL together
curl -s "$BASE/m3u?category=News&category=Local%20News" | grep -A1 '^#EXTINF'
```

### Get EPG for a channel

Three routes, none of them obvious. Pick by what you need:

```bash
# FULL SCHEDULE — everything stored, real timestamps. Best default.
# Filters by name/source/category/language, NOT by channel id.
curl -s "$BASE/epg.xml?search=Al%20Jazeera"          # full stored schedule, XMLTV

# BY CHANNEL ID — the only route that takes ids. Fixed 5-hour window.
curl -s "$BASE/admin/guide/programs?channel_ids=1314,2577"
curl -s "$BASE/admin/guide/programs?channel_ids=2577&offset=5"   # shift window +5h

# NOW/NEXT ONLY — plus how much guide data exists at all.
curl -s "$BASE/api/channels/2577/preview?detect=0" \
  | jq '{epg_programs, epg_hours, now: .current_program.title, next: .next_program.title}'
```

`/admin/guide/programs` returns a five-hour window keyed by channel ID, with
every requested ID present (empty array when nothing airs). `offset` shifts the
window in hours, clamped to ±48, so stepping it by 5 pages through roughly four
days. Its times are **display strings** in the server's timezone (`"8:00 AM"`)
next to `left`/`width` percentages for the guide grid — convenient for a UI,
awkward for data. Use `/epg.xml` when you want real timestamps.

`epg_hours` from `preview` is the quickest way to spot a channel with thin or
stale guide data.

### Diagnose a channel that will not play

```bash
curl -s -X POST "$BASE/api/channels/1314/inspect" | jq
```

Always returns HTTP 200; the verdict is in `status`:

| Status | Meaning |
| --- | --- |
| `live` | Manifest parsed, a segment returned bytes |
| `bridge` | DRM, but DASH+Widevine and a license path exist — plays via PrismCast |
| `drm` | Encrypted, no usable bridge |
| `dead` | Upstream 404/410 |
| `vod` | Finished recording, not a live channel |
| `no_data` | Manifest fine, segments empty |
| `error` | Everything else — read `detail` |

Then correlate with the log. Every response carries
`X-FastChannels-Request-ID`; grep for it:

```bash
curl -s "$BASE/api/logs?n=500" | jq -r '.lines[]' | grep '<request-id>'
```

### Feeds (saved filters with their own numbering)

```bash
curl -s "$BASE/api/feeds" | jq '.[] | {slug, name, m3u_url, epg_url}'

curl -s -X POST "$BASE/api/feeds" -H 'Content-Type: application/json' -d '{
  "name": "News Only",
  "filters": {"categories": ["News", "Local News"], "countries": ["US"]},
  "chnum_start": 8000
}'
```

Each feed exposes `/feeds/{slug}/m3u`, `/feeds/{slug}/epg.xml`,
`/feeds/{slug}/m3u/gracenote` and PrismCast variants.

### Bulk channel edits

```bash
curl -s -X POST "$BASE/api/channels/bulk" -H 'Content-Type: application/json' \
  -d '{"action":"disable","filters":{"source":"roku","drm":"dead"}}'
```

`action` is `enable` / `disable` / `mark_reviewed`. Runs as a background job and
returns the matched count immediately. The `filters` object supports 18
dimensions — see `ChannelFilters` in `docs/openapi.yaml`.

### Register outputs with Channels DVR

```bash
curl -s "$BASE/api/dvr/test-connection" | jq          # check connectivity first
curl -s -X POST "$BASE/api/feeds/1/push-to-dvr" -d '{}' -H 'Content-Type: application/json'
```

Registers up to two DVR sources (Gracenote-guided and standard-with-XMLTV).
Over 750 channels returns `409` with `requires_confirm`; re-send with
`{"force": true}`.

## Gotchas that cause real bugs

**`Channel.to_dict()` omits `source_channel_id` and `stream_info`.** They exist
on the model but not in `/api/channels` responses. Use
`/api/channels/{id}/preview` or the M3U outputs.

**Category filtering differs between surfaces.** `/api/channels?category=News`
does a substring `LIKE` and returns `News` *and* `Local News`. The `/m3u`
endpoints match **exactly**, so both values must be passed:
`?category=News&category=Local%20News`. They are distinct categories.

**No `/api` route returns a channel's full schedule.** `/epg.xml` filters by
name, source, category and language but **not by channel ID**;
`/api/channels/{id}/preview` gives only now/next. The one ID-based route is
`/admin/guide/programs`, and it returns a fixed five-hour window with
display-formatted times. Do not assume `/admin` is all HTML — two of its
eleven routes return JSON.

**`/m3u` descriptions break player playlists.** `tvg-description` and
`tvc-guide-description` carry commas, and VLC splits the `#EXTINF` title on a
comma inside the quoted attributes rather than the separator — so the playlist
shows sentence fragments instead of channel names, but only for channels whose
description has a comma. Strip those attributes before handing an M3U to a
player; `vlc.py` does this.

**Always fetch both playlists.** Channels holding a Gracenote ID are
partitioned into `/m3u/gracenote` and are **absent** from `/m3u`. Fetching only
the latter silently loses them.

**Two different `503`s:**

| Response | Meaning | Action |
| --- | --- | --- |
| `Retry-After: 15`, "artifact is warming" | Background worker still building output | Retry in ~15s. Normal after startup. |
| `Retry-After: 30`, "Stream temporarily unavailable." | Stream resolution failed — timeout, expired token, or an active back-off | Retry after the delay. **Never retry immediately.** |

**Resolving a stream is not a read-only operation.** Following a `/play` URL
runs the server's health check, which can auto-disable channels. Prefer the
`/play/...` URL itself over the upstream URL it redirects to: upstream URLs
carry expiring session tokens, some are IP-bound to the resolving host, and
some sources are proxied by design and have no single upstream URL at all.

**Roku rate-limits hard.** A `403` triggers a *source-wide* five-minute
cooldown during which every Roku channel returns `503`. Retrying makes it
worse. Most likely right after a restart, when all sources scrape at once.

**Channel-number collisions return `409`** with a `warnings` array, and the
change is rolled back. Applies to source edits, feed creation/edits and manual
reordering. Only *newly introduced* overlaps block.

**SQLite is single-writer.** Writes during a running scrape can return `503`
with "Server is busy" or "Database busy". Retry.

**Unmatched routes return Flask's HTML 404**, not JSON. Do not assume a JSON
body on error.
