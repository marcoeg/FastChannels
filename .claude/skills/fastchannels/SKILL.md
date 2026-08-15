---
name: fastchannels
description: Live TV, EPG, M3U and DVR via FastChannels + Tvheadend.
---

# FastChannels API

FastChannels scrapes free streaming services (Roku, Pluto, Xumo, Tubi, Samsung,
Plex, STIRR, Vizio, TCL and others) and republishes them as M3U playlists and
XMLTV guide data for Channels DVR, Plex, and generic IPTV clients. Everything
its admin UI does, it does over the HTTP API described here.

## When to use this

Reach for this skill whenever a task involves:

- live TV channels, channel lineups, or streaming sources
- EPG or guide data — what is on now, guide gaps, stale programming
- M3U playlists or XMLTV output
- Channels DVR or Plex integration
- recording a show — FastChannels cannot record, but Tvheadend on this machine
  can; see [It does not record](#it-does-not-record--tvheadend-does)
- anything naming FastChannels, or the API at `127.0.0.1:5523`

What it lets you do: list and filter channels across every source, get a
channel's playback URL, diagnose why a channel will not play, build and manage
feeds (saved filters with their own numbering), map Gracenote station IDs,
export channel data, and register outputs with Channels DVR.

## This install

```
Base URL   http://127.0.0.1:5523      (loopback only — not reachable from the LAN)
Version    5.0.4
Runtime    Docker, container `fastchannels`
```

Get live figures rather than assuming — counts change with every scrape:

```bash
curl -s http://127.0.0.1:5523/api/system-stats | jq '{uptime_seconds, db}'
curl -s http://127.0.0.1:5523/api/stats | jq '.categories[:5]'
```

**There is no authentication.** Not a single route checks a credential. The
loopback binding is the only access control, which is why every request below
works with a bare `curl`. It also means *any* call you make succeeds, including
destructive ones — see the safety section.

Local configuration deviates from upstream defaults: the port is bound to
`127.0.0.1` only, `VERSION_CHECK_ENABLED=0`, and `FC_YTDLP_AUTO_UPGRADE=0`
(weekly unattended `pip install` from GitHub master is disabled).

## It does not record — Tvheadend does

FastChannels has **no recording, timer, series-pass or storage model**, and
nowhere to put a recording: `/data` holds the database, its backups, browser
sign-in profiles and image/XML caches. The schema is `Channel` / `Program` /
`Source` / `Feed` — guide metadata, not media. It is a scraper and publisher.

**This install has a DVR attached: Tvheadend, fed from FastChannels.** If asked
to record something, that is where it happens.

```
Tvheadend 4.3   http://127.0.0.1:9981   admin / fastchannels   (digest auth)
  2,014 channels mirroring the FastChannels lineup
  ~91,000 EPG events, refreshed six-hourly by fastchannels-epg.service
  recordings -> dev/tvheadend/recordings
```

Its API is form-encoded like FastChannels' is, but needs digest auth:

```bash
TVH="curl -s --digest -u admin:fastchannels http://127.0.0.1:9981/api"

$TVH/channel/grid?limit=5                    # channels
$TVH/epg/events/grid?limit=5                 # what is on
$TVH/dvr/entry/grid                          # scheduled + completed recordings
$TVH/dvr/config/grid                         # storage path, retention
```

To record, find the event then schedule it — **mutating, so confirm first**:

```bash
# find the event id — `title` is a substring match
$TVH/epg/events/grid --data-urlencode 'title=News' --data-urlencode 'limit=5'
#   eventId=74427  ABC 27   ABC 27+ News

# schedule it
$TVH/dvr/entry/create_by_event \
  --data-urlencode 'event_id=<id>' --data-urlencode 'config_uuid=<dvr config uuid>'
```

`dvr/autorec/create` sets a series rule; `dvr/timerec/create` a recurring
time slot; `dvr/entry/cancel` and `dvr/entry/remove` undo. Channel numbers,
names, icons and EPG all come from FastChannels, so a channel identified there
is the same channel in Tvheadend.

**Do not make Tvheadend scan the network.** `scan_create` means "open every
stream to discover services" — 2,014 live `/play` requests, which trips Roku's
source-wide cooldown and lets FastChannels auto-disable channels wholesale. The
network is deliberately configured with `service_sid` instead, so channels come
from the playlist with no stream opened.

**DRM channels cannot be recorded.** Tvheadend has no Widevine CDM, so anything
needing the PrismCast bridge is out — the same limit as any non-browser client.

### Handing a stream to a player

`vlc.py` (repo root, symlink to `dev/vlc.py`) does this for either server:

```bash
./vlc.py --list-categories                       # categories with counts
./vlc.py --channel "Al Jazeera" --play           # FastChannels direct
./vlc.py --category News --write ~/news.m3u
./vlc.py --via tvheadend --category "Local News" --play
./vlc.py --recordings                            # DVR files on disk
```

**Default to `--via fastchannels`.** No authentication, and it reaches the 206
Gracenote channels Tvheadend does not carry — 2,220 against 2,014. Use
`--via tvheadend` when a stream is flaky and you want Tvheadend to own the
subscription and re-mux to MPEG-TS, or when checking what the DVR sees.

Never hand out a bare `http://127.0.0.1:9981/playlist/…` or `/stream/…` URL.
Tvheadend's `digest` is 1 — **Digest only** — and VLC and most simple players
lead with Basic, which is refused:

```
no auth -> 401     basic -> 401     digest -> 200
```

`http://user:pass@host/…` is sent as Basic too, so it fails the same way. The
way through is the **persistent authentication code**, a per-user token from
`passwd/entry/grid` passed as `?auth=CODE`. Fetch it at run time; never
hard-code it into a file that lands in the repo.

### Tvheadend's API is documented here, and nowhere else

Tvheadend ships no API documentation — 134 markdown files covering the UI and
DVB tuning, none describing the 185 JSON endpoints the UI is built on.
`dev/tvheadend/openapi.yaml` is a generated spec for them, and
`dev/tvheadend/gen_openapi.py` regenerates it from the Tvheadend source.
Consult the spec before guessing.

Conventions: every endpoint is POST with **form-encoded parameters whose
values are JSON documents** — `conf=`, `node=`, `filter=`, not a JSON body.
Most accept GET with a query string too. Lists come back as
`{"entries": [...], "total": N}` and take `limit`, `start`, `sort`, `dir` and
a JSON `filter`.

**It also documents itself.** Half the API is a generic object layer, so
`*/class` and `mpegts/network/builders` return every valid property with its
type, caption and description — live, not prose:

```bash
$TVH/dvr/entry/class                  # every field on a recording
$TVH/mpegts/network/builders          # every network type and its properties
```

That is how `service_sid`, `scan_create` and `refetch_period` were found.
Prefer it to guessing at a property name.

### Recording quality

Verified working: two test recordings, 1080p h264 with AAC, zero errors,
landing in `dev/tvheadend/recordings`.

One real gotcha. Some FAST channels carry **44.1 kHz AAC**, which is off-spec
for MPEG-TS — broadcast is 48 kHz. ffmpeg copes; many players silently drop
the track, so video plays with no sound. Adult Swim does this, Al Jazeera does
not. Diagnose and fix:

```bash
ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate \
        -of csv=p=0 recording.ts                      # 44100 means trouble
ffmpeg -i in.ts -c:v copy -c:a aac -ar 48000 -disposition:a:0 default out.mp4
```

That re-encodes audio only, so it is fast. It is a property of the provider's
stream, not a fault in the recording path.

Setup, rationale and limits: `dev/tvheadend/README.md`.

### The other option, not used here

**Channels DVR** is the alternative, and FastChannels has first-class support
for it — `POST /api/feeds/{id}/push-to-dvr` and friends register playlists
directly. It is closed-source and paid ($8/month or $80/year, 30-day trial,
Fancy Bits LLC), which is easy to miss: it self-hosts, installs from a shell
script and quotes no price on its download page.

It is **not configured on this install**, and that has one consequence beyond
recording: FastChannels holds no Gracenote licence and proxies station lookups
through a configured Channels DVR, so `/api/gracenote-search` and
`/api/channels/{id}/gracenote-suggestions` return `400` here. That is a missing
dependency, not a bug.

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

Full machine-readable reference: `docs/openapi.yaml` in this repo (184 paths,
verified against the route handlers by `dev/check_api_docs.py`). `docs/API.md`
is the prose companion.

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
curl -s "$BASE/epg.xml?search=Al%20Jazeera"          # 157 programmes, XMLTV

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

**Tvheadend refuses Basic auth.** `digest` is 1, so `--basic` and
`http://user:pass@host/…` both return 401; only `--digest` works from the shell.
For players, use the `?auth=CODE` token instead (see `vlc.py`) —
and use `/playlist/auth/…`, because plain `/playlist/…` returns entry URLs with
no credential, so the list opens and every channel inside it then 401s.

**The Tvheadend auth code cannot touch recordings.** `access.c:885` clamps it
to `ACCESS_ADVANCED_STREAMING|ACCESS_STREAMING`, and `/dvrfile/…` requires
`ACCESS_RECORDER`. Granting the account more permissions does not change this.
Read recordings from `dev/tvheadend/recordings/` instead.

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
