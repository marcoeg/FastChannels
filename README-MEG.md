# FastChannels — local additions

Local tooling and configuration for this FastChannels install. None of it is
part of the upstream project.

| | |
| --- | --- |
| **Tools** | `record.py` — record a channel, now or scheduled<br>`vlc.py` — hand a channel to VLC, from either server<br>`dev/news_origins.py` — where local news channels broadcast from<br>`dev/export_channels.py` — export a category with both playback endpoints |
| **DVR** | `record.py` — ffmpeg + systemd timers, see [Recording](#recording--recordpy) |
| **Requires** | Python 3.9+, standard library only. No pip install. Tested on 3.12.3. |
| **Server** | A running FastChannels instance (default `http://127.0.0.1:5523`) |
| **Reference** | `docs/openapi.yaml` and `docs/API.md` document the HTTP API both scripts read |

Both scripts talk to the server's API and write nothing to it, with one
exception: resolving a channel's **remote** stream URL exercises the real
`/play` path, which can cause the server to auto-disable channels it finds to
be DRM-protected or dead. Each script has a flag to skip that.

Operational notes on this install — the private repo, remotes, and the
`docker-compose.override.yml` deviations — are in
[Repository](#repository) and [Local configuration](#local-configuration).

---

## Origins extractor — `dev/news_origins.py`

Derives the originating market and station for news channels: which city
and state a local channel broadcasts from, and its call sign. Resolves
283 of 293 local channels (97%).

### Why this exists

FastChannels stores no origin field. Grepping the models turns up nothing
geographic beyond `country`, which is `"US"` for all 494 news channels, so
`GET /api/channels?category=News` cannot answer "where does this channel come
from" — it only tells you which *provider* carries it (roku, vizio, xumo …),
which is a different question.

The origin is present in the data, but only as prose:

```json
{
  "name":        "12 News KWCH Wichita",
  "description": "The latest breaking news and severe weather coverage for Wichita, Kansas viewing area.",
  "guide_key":   "93KWCH",
  "source_name": "vizio",
  "country":     "US"
}
```

Wichita and KWCH are right there — nothing parses them. This script does.

---

### Quick start

```bash
# Coverage report
python3 dev/news_origins.py

# Every resolved channel, one per line
python3 dev/news_origins.py --table

# Export
python3 dev/news_origins.py --csv news_origins.csv --json news_origins.json

# Origins plus playable URLs, pointed at a LAN host
python3 dev/news_origins.py --urls --public-base http://192.168.50.17:5523 --csv news.csv
```

The server must be running. If it is bound somewhere other than loopback:

```bash
python3 dev/news_origins.py --base http://192.168.1.50:5523
```

---

### Usage

```
python3 dev/news_origins.py [--base URL] [--table] [--unresolved]
                            [--csv PATH] [--json PATH]
                            [--urls] [--public-base URL]
                            [--resolve] [--resolve-delay SEC]
```

| Flag | Effect |
| --- | --- |
| `--base URL` | Server base URL. Default `http://127.0.0.1:5523`. |
| `--table` | Print every resolved channel: call sign, market, source, confidence, name. |
| `--unresolved` | List only the local channels whose market could not be recovered. Use this when tuning the tables. |
| `--csv PATH` | Write all 494 rows as CSV. |
| `--json PATH` | Write all 494 rows as JSON. |
| `--urls` | Attach the playable `/play/…` URL and `source_channel_id` to every channel. |
| `--public-base URL` | Rewrite emitted URLs to this host instead of localhost. See [URLs](#urls). |
| `--resolve` | Follow each `/play` URL to the **upstream CDN URL**. Slow, live, and **mutates server state** — read the warning first. |
| `--resolve-delay SEC` | Pause between `--resolve` requests. Default `0.4`. |

The coverage report always prints. `--table` and `--unresolved` add sections
above it; the two export flags can be combined with anything.

---

### Examples

#### Coverage report

```console
$ python3 dev/news_origins.py

==============================================================================
494 news channels: 293 local, 201 national/international
==============================================================================

Market resolved for 283/293 local channels (97%)
Call sign resolved for 150/293 (51%)

--- confidence ---
    150  high
    102  medium
     31  low
     10  none

--- rule that fired ---
    102  gazetteer:name:market
     75  callsign-table:text
     75  callsign-table:guide_key
     22  gazetteer:name:state
     10  (none)
      6  gazetteer:name:macro-region
      2  gazetteer:desc:market
      1  gazetteer:desc:state

--- coverage by source (local channels only) ---
  source            resolved  total      %
  vizio                   87     88    99%
  roku                    72     76    95%
  xumo                    47     49    96%
  stirr                   34     34   100%
  tcl                     23     23   100%
  tubi                     9     10    90%
  plex                     9      9   100%
  samsung                  1      3    33%
  lg-channels              1      1   100%

--- top markets ---
   15  New York, NY
   13  San Francisco Bay Area, CA
   10  Chicago, IL
    9  Los Angeles, CA
    8  Atlanta, GA
```

#### Per-channel listing

```console
$ python3 dev/news_origins.py --table

call   market                       source       conf    name
-------------------------------------------------------------------------------
KSBW   Monterey–Salinas, CA         plex         high    Very Central Coast by KSBW
KCRA   Sacramento, CA               plex         high    Very Sacramento by KCRA
WAPA   San Juan, PR                 plex         high    WAPA+
-      Philadelphia, PA             roku         medium  6ABC Philadelphia
-      Statewide, NC                roku         low     ABC11 North Carolina
-      Houston, TX                  roku         medium  ABC13 Houston
-      San Francisco Bay Area, CA   plex         medium  CBS News Bay Area
```

#### What could not be resolved

```console
$ python3 dev/news_origins.py --unresolved

--- 10 local channels with no recoverable market ---
  [roku        ] ABC 20/20  (call sign: -)
  [roku        ] ABC Localish  (call sign: -)
  [roku        ] Channel 3 Eyewitness News  (call sign: -)
  [roku        ] Kino  (call sign: -)
  [samsung     ] Localish  (call sign: -)
  [tubi        ] Localish  (call sign: -)
  [vizio       ] Local Now  (call sign: -)
  [xumo        ] Local Now  (call sign: -)
  [xumo        ] Spectrum News+  (call sign: -)
```

These are national channels that the lineup has miscategorised as *Local News*
— an upstream categorisation issue, not an extraction failure.

#### Useful one-liners

```bash
# How many channels per market, most first
python3 dev/news_origins.py --json /tmp/o.json >/dev/null
jq -r '.[] | select(.market) | .market' /tmp/o.json | sort | uniq -c | sort -rn

# Every channel from one market, across all providers
jq -r '.[] | select(.market=="Chicago, IL") | "\(.source)\t\(.name)"' /tmp/o.json

# Only high-confidence rows (call-sign table hits)
jq -r '.[] | select(.confidence=="high") | "\(.call_sign)\t\(.market)\t\(.name)"' /tmp/o.json

# Which markets are covered by more than one provider
jq -r '.[] | select(.market) | "\(.market)\t\(.source)"' /tmp/o.json \
  | sort -u | cut -f1 | uniq -c | awk '$1>1'
```

---

### URLs

`--urls` adds a playable URL for each channel. It resolves **492 of 494**; the
two misses are not feed-eligible (disabled, or no stream).

```bash
python3 dev/news_origins.py --urls --csv news.csv
```

The URLs cannot come from `/api/channels` — `Channel.to_dict()` omits
`source_channel_id`, so there is nothing to build a `/play` path from. They are
parsed out of the M3U outputs instead, which requires two fetches:

| Playlist | News URLs |
| --- | ---: |
| `/m3u` | 430 |
| `/m3u/gracenote` | 62 |
| **total** | **492** |

Channels carrying a Gracenote ID are partitioned into the second playlist and
are absent from the first. Fetching only `/m3u` silently loses 62 channels. The
`category` filter on these endpoints is an **exact** match, unlike
`/api/channels`, which does a substring `LIKE` — so both `News` and
`Local News` must be passed explicitly. The script handles all of this.

#### localhost vs a reachable host

FastChannels emits `http://127.0.0.1:5523/play/…` whenever **Public Base URL**
is unset. That is deliberate: `detected_base_url()` in `app/url.py` declines to
guess inside Docker, because the container's own LAN detection returns an
unreachable bridge address.

```bash
python3 dev/news_origins.py --urls --public-base http://192.168.50.17:5523
```

```
http://192.168.50.17:5523/play/plex/6596546bfca6056ca69e3e3c.m3u8
```

**This only rewrites text.** If the container port is bound to loopback
(`127.0.0.1:5523:5523` in `docker-compose.override.yml`) the rewritten URL is
still unreachable from anywhere else — it will look correct and fail. To make
LAN URLs actually work, both of these are required:

1. Publish the port on the LAN interface — change the override to
   `192.168.50.17:5523:5523` or plain `5523:5523`. Note the security trade-off:
   the API has no authentication, so this exposes full admin to the network.
2. Set **Public Base URL** in the app, so *every* output is correct — the M3U
   and EPG a DVR consumes, not just this script:

   ```bash
   curl -s -X POST http://127.0.0.1:5523/api/settings \
     -H 'Content-Type: application/json' \
     -d '{"public_base_url":"http://192.168.50.17:5523"}'
   ```

   With that set, `--public-base` becomes unnecessary.

#### Upstream CDN URLs

`--public-base` only changes *which interface of this machine* the URL points
at. To get the provider's own URL — the actual remote origin — use `--resolve`,
which follows the redirect and records `cdn_url`, `cdn_host` and `delivery`.

```console
$ python3 dev/news_origins.py --urls --resolve --csv news_remote.csv

--- stream delivery (478 probed) ---
   402  redirect  — direct provider URL
    75  proxied   — served through FastChannels, no direct URL
     1  error

--- upstream hosts (175 distinct) ---
    94  osm.sr.roku.com
    42  dbrb49pjoymg4.cloudfront.net
    35  epg.provider.plex.tv
    23  live-manifest.production-public.tubi.io
    21  cdn-uw2-prod.tsv2.amagi.tv
    19  d4whmvwm0rdvi.cloudfront.net
    12  dai.google.com
```

```
xumo/99951223.m3u8  ->  https://dbrb49pjoymg4.cloudfront.net/10001/99951223/…
```

**Not every channel has a remote URL.** STIRR (46) and Samsung (29) are always
`proxied`: STIRR issues IP-bound session tokens and Samsung emits relative
playlist references, so both must be fetched server-side and the stream really
does flow through FastChannels. For those, `cdn_url` is empty but `cdn_host`
still names the upstream when it can be read out of the manifest body
(Samsung → `pb-ycseayifkqre8.akamaized.net`).

| `delivery` | Meaning | `cdn_url` |
| --- | --- | --- |
| `redirect` | Provider URL a client can fetch directly | set |
| `proxied` | Streamed through FastChannels by design | empty; `cdn_host` may still name the upstream |
| `error` | Nothing resolvable — reason in `cdn_host` | empty |

> **`--resolve` changes server state. It is not a dry run.**
>
> It exercises the real `/play` path, which is what a player would hit — so the
> server does everything it normally would, including its background health
> check. On one 492-channel sweep that **auto-disabled 29 Roku channels** on
> detecting DRM:
>
> ```
> [play] DRM detected — auto-disabled channel CBS News Sacramento (roku/…)
> ```
>
> This is correct application behaviour, not corruption — those channels are
> genuinely DRM-protected and any real playback attempt would have done the
> same. But your lineup shrinks, and a second sweep sees fewer channels than the
> first. Reverse it per channel with
> `PATCH /api/channels/{id}` `{"is_enabled": true}` (which clears a DRM
> `disable_reason`), or globally by enabling the DRM bridge in Settings, which
> recovers them onto the PrismCast path instead of disabling them.

Other things to know:

- **Slow and live.** One real request per channel — about four minutes for 492
  at the default delay, every one hitting the provider.
- **Rate limits.** Roku returns `403` under Cloudflare rate-limiting, and
  FastChannels then applies a **source-wide five-minute cooldown** — every Roku
  channel returns `503` until it expires. Raise `--resolve-delay` if
  `cdn_host` starts showing `(503 unavailable …)`. Requests are naturally
  interleaved across providers because `/api/channels` returns rows ordered by
  name, so no one provider is hit in a solid block.
- **Perishable URLs.** Most carry session tokens and some are bound to the IP
  that resolved them. A resolved URL is a snapshot for analysis, not something
  to hand to a player — use `play_url` for playback.

---

### How resolution works

Every channel is first split by the app's own `category` field, rather than
re-deriving the distinction:

- **`Local News`** → a US market. 293 channels. Resolution is attempted.
- **`News`** → a national or international network (BBC, Al Jazeera, Bloomberg,
  DW). 201 channels. These have no market, and are reported as `scope: national`
  with `confidence: n/a` rather than being force-fitted to a city.

For local channels the rules run in precedence order, first hit wins:

| # | Rule | Signal | Confidence |
| --- | --- | --- | --- |
| 0 | **Call-sign table** | Call sign from `guide_key` or the title, looked up in `CALLSIGN_MARKETS` | high |
| A | STIRR description template | `"Live Asheville, NC news … from WLOS."` | high |
| B | Name pattern | `"<CALLSIGN> <City> <ST>"` at end of title | high |
| C | Description | `"…coverage for Wichita, Kansas viewing area."` | high |
| D | Description | `"KMTV 3 News Now Omaha gives you…"` | medium |
| E | Description | `"Channel 13 Las Vegas News KTNV…"` | medium |
| F | **Name gazetteer** | A market name in the title — `FOX LOCAL Atlanta`, `CBS News Baltimore` | medium (market) / low (state) |
| G | Description gazetteer | Same lookup against the description | low |

Two rules do nearly all the work: the call-sign table (150) and the name
gazetteer (102). The description rules exist because vizio and STIRR write
templated copy that is more precise than their titles.

**Granularity** is reported separately from confidence, because some feeds have
no city by design:

| `granularity` | Meaning | Example |
| --- | --- | --- |
| `market` | A real DMA | `Wichita, KS` |
| `state` | Statewide feed | `CBS News Texas` → `Statewide, TX` |
| `macro-region` | Multi-state brand | `Telemundo Noreste` → `Northeast` |
| `none` | Unresolved, or national | — |

#### Why the call-sign table wins

It is not just more precise — it is authoritative over the source data.
Sinclair/STIRR describes WPDE as *"Myrtle Beach FL"*. Myrtle Beach is in South
Carolina. The call sign resolves it correctly; the provider's own metadata does
not.

---

### Output schema

CSV and JSON carry the same 13 fields.

| Field | Notes |
| --- | --- |
| `channel_id` | FastChannels internal ID — join key back to `/api/channels` |
| `name` | Channel name as stored |
| `source` | Provider carrying it (`roku`, `vizio`, …) — *not* the origin |
| `category` | `Local News` or `News` |
| `scope` | `local` or `national` |
| `call_sign` | US broadcast call sign, or `null` |
| `city` | Principal city of the DMA, or `Statewide` / a macro-region name |
| `state` | Two-letter code, empty for macro-regions |
| `confidence` | `high` / `medium` / `low` / `none` / `n/a` |
| `granularity` | `market` / `state` / `macro-region` / `none` |
| `rule` | Which rule fired — useful for auditing a specific row |
| `evidence` | The text the match came from |
| `market` | Convenience: `"City, ST"` |
| `source_channel_id` | The source's own ID — the `{id}` in `/play/{source}/{id}.m3u8`. `--urls` only. |
| `play_url` | Playable FastChannels URL. `--urls` only. |
| `cdn_url` | Provider's own stream URL. Empty for `proxied` channels. `--resolve` only. |
| `cdn_host` | Upstream hostname — set even when `proxied`, if readable. `(reason)` on failure. `--resolve` only. |
| `delivery` | `redirect` / `proxied` / `error`. `--resolve` only. |

```json
{
  "channel_id": 142,
  "name": "News 12 New York",
  "source": "lg-channels",
  "category": "Local News",
  "scope": "local",
  "call_sign": null,
  "city": "New York",
  "state": "NY",
  "confidence": "medium",
  "granularity": "market",
  "rule": "gazetteer:name:market",
  "evidence": "News 12 New York",
  "market": "New York, NY"
}
```

Markets use the principal city of the DMA rather than the licence city, so
sister stations group together. Combined markets keep both names
(`Dallas–Fort Worth, TX`, `Minneapolis–St. Paul, MN`,
`Grand Rapids–Kalamazoo, MI`).

---

### Accuracy

Coverage is measured directly. Accuracy is measured by cross-validation: for the
98 channels that carry **both** a call sign in the table **and** a market name in
the title, the two independent signals are compared.

```
cross-checkable (call sign AND place in name): 98
  agree    : 96
  disagree : 2   ->  98% agreement
```

Both disagreements are the same market written two ways — `Grand Rapids–Kalamazoo`
(table) versus `Grand Rapids` (title). No substantive errors.

Reproduce it:

```bash
python3 - <<'PY'
import json, sys, urllib.request
sys.path.insert(0, 'dev')
from markets import CALLSIGN_MARKETS
from news_origins import lookup_place, extract

url = "http://127.0.0.1:5523/api/channels?category=News&per_page=1000"
ch = json.load(urllib.request.urlopen(url))["channels"]

agree = disagree = 0
for c in ch:
    if c.get("category") != "Local News":
        continue
    o = extract(c)
    if not o.call_sign or o.call_sign not in CALLSIGN_MARKETS:
        continue
    hit = lookup_place(c["name"] or "")
    if not hit or hit[2] != "market":
        continue
    if (hit[0], hit[1]) == CALLSIGN_MARKETS[o.call_sign]:
        agree += 1
    else:
        disagree += 1
        print("MISMATCH", o.call_sign, c["name"], CALLSIGN_MARKETS[o.call_sign], hit[:2])
print(f"{agree} agree, {disagree} disagree")
PY
```

---

### Known limits

- **Extraction, not ground truth.** Names and descriptions are provider-written
  marketing copy. The tables correct the common errors, but a channel whose
  title names no place and whose call sign is absent cannot be resolved.
- **31 rows are coarser than a market** — statewide feeds and Telemundo
  macro-regions. These are reported at their true granularity rather than being
  guessed into a city.
- **10 rows are unresolved**, all national channels miscategorised upstream as
  *Local News*.
- **Not incremental.** Re-run after a scrape adds channels; there is no cache.
- **US-only.** All current news channels are `country: US`. Non-US local news
  would need a different gazetteer.
- **Call signs are matched conservatively.** The regex requires uppercase
  `K`/`W` plus 2–3 letters. A case-insensitive second pass runs *only* against
  the known-call-sign table, so title-cased forms like `Wapa+` resolve without
  matching ordinary words such as "west" or "kids".

---

### Maintaining the tables

Both live in `dev/markets.py`. After a scrape adds channels, run
`--unresolved` and extend whichever table is missing.

**New call sign** — the highest-value addition, since it resolves every channel
carrying it regardless of title:

```python
CALLSIGN_MARKETS = {
    ...
    "WXYZ": ("Detroit", "MI"),
}
```

**New market name** in titles:

```python
CITY_MARKETS = {
    ...
    "spokane": ("Spokane", "WA"),
}
```

**Regional brand** (`Bay Area`, `Tampa Bay`, `South Florida`):

```python
REGION_MARKETS = {
    ...
    "inland empire": ("Riverside–San Bernardino", "CA"),
}
```

Lookup keys are lowercase and matched on word boundaries, longest first, with
regions tried before cities before states — so `Kansas City` cannot be swallowed
by `Kansas`, and `South Florida` beats `Florida`. Adding a short key that is a
substring of a longer one is safe.

Verify an addition did what you expected:

```bash
python3 dev/news_origins.py --table | grep -i spokane
```

---

## Category exporter — `dev/export_channels.py`

Exports every **enabled** channel in a category as CSV or JSON, carrying both
playback endpoints and as much per-channel detail as the API exposes.

### Why it needs three sources

No single endpoint can answer this. `/api/channels` returns a 42-field record
but **omits `source_channel_id`**, so it cannot yield a playback URL on its
own. The script joins:

| Source | Contributes |
| --- | --- |
| `/api/channels` | the channel record — name, category, language, country, Gracenote state, flags, timestamps |
| `/m3u` **and** `/m3u/gracenote` | `local_url`, `source_channel_id`, feed channel number, proxied logo, stream codecs, Gracenote station ID |
| `/play/<src>/<id>.m3u8` | one hop to `remote_url` — the provider's own stream URL |

Both playlists are fetched because channels carrying a Gracenote ID are
partitioned into `/m3u/gracenote` and are **absent** from `/m3u`. Fetching one
silently loses them.

Rows are joined on `(source_name, name)`, which is unique across the lineup.

### Quick start

```bash
# what categories exist, with counts
python3 dev/export_channels.py --list-categories

# a category as JSON on stdout
python3 dev/export_channels.py "Local News"

# as CSV, to a file, URLs pointed at a LAN host
python3 dev/export_channels.py "Local News" --format csv -o local-news.csv \
  --public-base http://192.168.50.17:5523

# read-only and fast — skips the remote lookup
python3 dev/export_channels.py Sports --no-resolve
```

### Usage

```
export_channels.py [--list-categories] [--base URL] [--public-base URL]
                   [--format {json,csv}] [-o PATH] [--contains]
                   [--include-disabled] [--no-resolve] [--resolve-delay SEC]
                   [--epg] [category]
```

| Flag | Effect |
| --- | --- |
| `--list-categories` | Print every category with its channel count, then exit. |
| `--base URL` | Server URL. Default `http://127.0.0.1:5523`. |
| `--public-base URL` | Rewrite `local_url` to this host. The server emits localhost whenever Public Base URL is unset — see [localhost vs a reachable host](#localhost-vs-a-reachable-host). |
| `--format {json,csv}` | Default `json`. |
| `-o PATH` | Write to a file instead of stdout. Progress goes to stderr either way, so piping stdout is safe. |
| `--contains` | Substring match — `News` also matches `Local News`. |
| `--include-disabled` | Include channels with `is_enabled = false`. |
| `--no-resolve` | Skip the remote lookup. Strictly read-only and much faster. |
| `--resolve-delay SEC` | Pause between remote lookups. Default `0.4`. |
| `--epg [MODE]` | Attach programme data: `now`, `window` or `full`. Bare `--epg` means `now`. See [EPG modes](#epg-modes). |
| `--epg-offset HOURS` | Shift the `window` mode window. Server clamps to ±48. |
| `--tz ZONE` | IANA timezone for programme times. Defaults to the server's configured timezone. |
| `--utc` | Leave programme times in UTC, as the server emits them. |

### Category matching is exact

`--list-categories` prints the 34 valid names. `News` and `Local News` are
**distinct categories**, and the default is an exact match:

```console
$ python3 dev/export_channels.py News --no-resolve | …
79 enabled channels

$ python3 dev/export_channels.py News --contains --no-resolve | …
272 enabled channels        # Local News 193 + News 79
```

The server's `/api/channels` does a substring `LIKE` and would quietly return
both; `--contains` opts into that. The M3U endpoints, by contrast, match
exactly — the script passes every matched category value explicitly so the two
stay consistent.

Counts from `--list-categories` are *active* channels, while the export filters
to *enabled*, so the export is usually smaller. International lists 12 and
exports 9.

### Output

54 fields per channel, or 62 with `--epg`. CSV leads with identity and
playback columns and appends the rest alphabetically, so a future server field
lands in the output rather than being dropped.

| Field | Notes |
| --- | --- |
| `local_url` | FastChannels playback URL. Resolves tokens and expiring URLs at play time — **give this to a player.** |
| `remote_url` | The provider's own stream URL. Empty when `delivery` is not `redirect`. |
| `remote_host` | Upstream hostname, or `(reason)` when resolution failed. |
| `delivery` | `redirect` / `proxied` / `error` — see below. |
| `source_channel_id` | The `{id}` in `/play/{source}/{id}.m3u8`. Not available from `/api/channels`. |
| `feed_channel_number` | `tvg-chno` — the number as it appears in the playlist. |
| `playlist` | `standard` or `gracenote` — which output carries this channel. |
| `video_codec` / `audio_codec` | From the playlist; present only where the server knows them. |
| `gracenote_station_id` | `tvc-guide-stationid`, on Gracenote-partitioned rows. |
| `logo_proxy_url` | Cached logo served by this instance. |
| `now_title` / `next_title` / `epg_hours` | `--epg now` only. |
| `programs` | Array of programmes. `--epg window` / `full`, **JSON output only**. |
| `epg_count` / `epg_first` / `epg_last` | Programme count and schedule bounds. `--epg window` / `full`. |
| `xmltv_id` | `{source}.{source_channel_id}` — the join key XMLTV uses. |

### EPG modes

The server exposes programme data three different ways and they are not
interchangeable. `--epg` picks one:

| Mode | Route | Cost | Returns |
| --- | --- | --- | --- |
| `now` | `/api/channels/{id}/preview` | one request **per channel** | now/next titles plus `epg_programs` and `epg_hours` |
| `window` | `/admin/guide/programs` | batched, 100 ids per request | a fixed **five-hour** window; `--epg-offset` shifts it |
| `full` | `/epg.xml` | **one request** for the whole export | the complete stored schedule with real timestamps |

`full` is the best value — one request, real ISO timestamps, hundreds of
programmes per channel — and is what you usually want.

**Gracenote channels need a second fetch, which `full` handles for you.**
`/epg.xml` omits Gracenote-partitioned channels for the same reason `/m3u`
does: their guide is meant to come from Gracenote, not from this server's
XMLTV. On a News export that is 19 of 79.

The per-feed **native** variant applies no such filter — in
`app/generators/xmltv.py` the generator passes `gracenote=None` for native
against `gracenote=False` otherwise — so it is the only route returning a
complete schedule for them. `full` falls back to it automatically, and only
when the export actually contains such channels:

```console
$ python3 dev/export_channels.py News --no-resolve --epg full
fetching the full schedule from /epg.xml (one request)…
  19 Gracenote-partitioned channel(s) are absent from /epg.xml by design;
  fetching the native feed…
  76/79 channels have programme data
  3 with none: CGTN Global Biz, DeFiance Media, Weather Nation
```

The native feed cannot be filtered by category and covers the whole feed —
~68 MB here — so it is streamed with `iterparse` and filtered to the wanted
channels as it arrives. Memory stays flat and the whole run still takes about
a second. `--no-native-epg` skips it; `--native-feed SLUG` picks a different
feed than `default`.

Channels still showing zero after that genuinely have no guide data — the
three above return `epg_programs: 0` from `/api/channels/{id}/preview`, so
nothing was scraped for them.

`window` times are **display strings** in the server's timezone (`"8:00 AM"`)
next to `left`/`width` percentages, because that route backs the admin guide
grid. `full` gives real timestamps. Prefer `full` unless you specifically need
a bounded window.

#### Timezone

The server is inconsistent here: XMLTV emits UTC (`+0000`),
`/api/channels/{id}/preview` returns UTC ISO, and `/admin/guide/programs`
formats in the server's configured timezone. Left alone, the three `--epg`
modes would disagree with each other and with the admin UI.

So programme times are converted to the server's own timezone by default,
read from `/api/settings`:

```console
$ python3 dev/export_channels.py News --no-resolve --epg full
times in America/Los_Angeles (the server's configured timezone)
  start: 2026-08-15T05:58:00-07:00      # was …T12:58:00+00:00
```

`--tz Europe/Rome` picks another zone; `--utc` keeps the server's raw UTC. An
unknown zone warns and falls back to UTC rather than failing the export.

Only schedule fields are converted — `epg_first`, `epg_last`, the `now_*` and
`next_*` pairs, and each programme's `start`/`stop`. Channel-record timestamps
such as `first_seen_at` stay in UTC so rows remain directly comparable with
`/api/channels`.

CSV output drops the `programs` array — hundreds of programmes JSON-encoded
into one cell exceeds Python's own 131072-byte field limit and is unreadable in
a spreadsheet. `epg_count`, `epg_first` and `epg_last` are kept. Use
`--format json` when you want the programmes themselves.

Not every channel has a remote URL. Sources whose CDN issues IP-bound tokens or
relative playlists must be fetched server-side, so they report
`delivery: proxied` with an empty `remote_url`; `remote_host` still names the
upstream when the manifest body reveals it.

### `--no-resolve` is the read-only mode

> Without it, the script follows each `local_url` through the real `/play`
> path. That is what a player does, so the server behaves normally — including
> its background health check, which **may auto-disable channels it finds to be
> DRM-protected or dead**. Twenty-nine Roku channels were disabled this way
> during development. It is correct application behaviour, not corruption, and
> is reversible with `PATCH /api/channels/{id}` `{"is_enabled": true}` or by
> enabling the DRM bridge in Settings.

Resolving is also slow — one live request per channel, about four minutes for
500 at the default delay — and can trip provider rate limits. Roku returns
`403` under Cloudflare rate-limiting, after which FastChannels applies a
source-wide five-minute cooldown; raise `--resolve-delay` if `remote_host`
starts showing `(503 unavailable …)`.

Resolved URLs carry session tokens and are sometimes bound to the IP that
resolved them, so treat them as analysis snapshots rather than durable links.

### Example

```console
$ python3 dev/export_channels.py "Local News" --no-resolve --format csv -o ln.csv
193 enabled channel(s); 193 with a playable URL (0 not in either playlist)
wrote ln.csv (193 rows)
```

```json
{
  "id": 3205,
  "name": "12 News KWCH Wichita",
  "source_name": "vizio",
  "category": "Local News",
  "feed_channel_number": "7946",
  "source_channel_id": "93KWCH",
  "playlist": "standard",
  "local_url": "http://127.0.0.1:5523/play/vizio/93KWCH.m3u8",
  "video_codec": "h264",
  "audio_codec": "aac",
  "logo_proxy_url": "http://127.0.0.1:5523/logos/711a3abd…jpg"
}
```

Exit codes: `0` on success, `1` when the category matches nothing (the message
lists the valid names) or the server is unreachable.

---

## Recording — `record.py`

The DVR. Symlinked at the repo root; the file is `dev/record.py`. It records
straight from FastChannels with ffmpeg, and hands scheduling to systemd.

```bash
./record.py now       --channel "Al Jazeera" --duration 30m
./record.py at        --channel "Al Jazeera" --start "18:00" --duration 1h
./record.py programme --channel "Al Jazeera" --match "Newshour"
./record.py epg       --channel "Al Jazeera"        # what is on, to pick from
./record.py list                                    # what is scheduled
./record.py cancel fcrec-newshour-1786824833
```

`--mp4` remuxes after capture, `--out DIR` redirects, and `--json` on
`epg`/`list`/`programme` makes it comfortable to drive from an agent.
Recordings land in `~/news/recordings`.

### Why this replaced Tvheadend (removed 2026-08-15)

The decisive number: **206 channels Tvheadend cannot reach at all.** It ignores
`tvc-guide-stationid`, so the entire `/m3u/gracenote` playlist is invisible to
it — and that playlist holds *ABC News Live*, *60 Minutes* and *48 Hours*.
Verified: Tvheadend's channel grid has no "48 Hours"; `record.py` records it.

The rest is friction removed. No XMLTV socket shim with a double-push quirk,
no digest-only auth that VLC cannot use, no recordings unreachable over HTTP,
and none of the defaults that silently broke recording — `scan_create` opening
every stream, an OTA EPG grabber pinning both IPTV inputs so that *every*
recording failed with "No input source available", and a `TZ` setting that did
nothing because the image ships no tzdata.

### How it works

Capture is a plain copy, no re-encode:

```
ffmpeg -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 30 \
       -i <play-url> -t <seconds> -c copy -f mpegts <out>.ts
```

Scheduling is one transient systemd timer per recording, and there is **no
state file** — `systemctl --user list-timers` is the database:

```
systemd-run --user --unit=fcrec-<slug>-<epoch> \
  --on-calendar="2026-08-15 18:00:00" \
  --timer-property=RemainAfterElapse=no \
  <record.py> now --channel ... --duration ...
```

`RemainAfterElapse=no` matters: without it a fired timer lingers and `list`
fills with corpses. Lingering is already enabled for this account, so timers
survive logout and fire at boot.

The channel URL is resolved when the recording **starts**, never when it is
scheduled — FastChannels re-resolves `/play/...` per request and upstream
session tokens expire, so a URL captured hours earlier would be stale.

Each run leaves `.meta/<name>.json` (channel, URL, window, exit status, size)
and `.meta/<name>.log` (ffmpeg stderr) beside the recording. Enough to answer
"did it record, and if not why" without reintroducing a database.

### Adverts and recording — fixed by pinning

Recording an ad-stitched channel used to lose video silently. Handed the
provider **master**, ffmpeg sees one program per rendition — five of them on
`90s Throwback`, eleven streams — and at an advert splice it loses the mapped
video stream and never re-acquires it. Audio and the container run the full
length, so the file passes every cheap check.

Handed **one rendition** it sees two streams, one audio and one video, so there
is no mapping to lose. Measured on 15-minute captures of the same channel:

```
master   900.0s container, 148.0s video  =  16%   video lost at the first break
pinned   900.0s container, 890.1s video  =  99%   complete, 26,702 frames, one resolution
```

`record.py` now resolves the highest rendition before capturing, the same way
`vlc.py` does, and records `pinned` in the sidecar. Resolution happens at
**record** time, never at schedule time — these URLs carry a session token and
expire. `--no-pin` records the master, which is only useful for reproducing the
old failure.

Things that did **not** fix it, all tested first: `--normalize` (a scale filter
cannot process frames the demuxer stopped delivering), an ffmpeg supervisor
that restarted on stall (restarting re-enters at the live edge, replaying the
advert), and yt-dlp (falls back to ffmpeg for live HLS even with
`--hls-prefer-native` and a single `-f`, inheriting the identical failure).

The tail check below stays as a safety net, since pinning is not available when
a `/play` URL does not resolve to an HLS master.

### Damage detection

After each capture `record.py` checks whether video still exists in the last
ten seconds — 0.07s on a damaged file, against roughly three and a half minutes
to count every frame of an hour:

```
WARNING: this recording has NO VIDEO at the end.
```

The sidecar carries `video_at_end`.

### `--normalize`### `--normalize`

`record.py` captures with `-c copy`, so it is exposed in principle to the same
thing that breaks `vlc.py` on ad-stitched channels: an advert spliced in at a
different encoding lands in a track declaring the first segment's geometry, and
VLC then freezes at the change.

**In practice it held up.** A ten-minute capture of `90s Throwback` (Pluto's
stitcher) spanning real advert breaks produced a clean file:

```
duration      588.8s of 600s requested
decode errors 0
resolutions   1216x684 throughout — no change
```

The capture log carried 46 `Packet corrupt` messages and 5 timestamp
discontinuities — one with a `-94237930689` offset — but those are the *input*
HLS, and ffmpeg absorbed them while re-stamping the output.

`--normalize` re-encodes to a fixed 1280x720 with 48 kHz stereo audio, the same
treatment `vlc.py --relay` applies, for when a channel does splice adverts at
another resolution:

```bash
./record.py now --channel "90s Throwback" --duration 1h --normalize
./record.py at  --channel X --start 20:00 --duration 1h --normalize
```

It works on `now`, `at` and `programme`, and scheduled recordings carry it
through to the systemd timer's command line. Leave it off unless a recording
actually misbehaves — `-c copy` is lossless and free, and re-encoding is
neither.

To check a suspect recording, `resolutions_in()` in `dev/record.py` does a full
frame scan. It is deliberately **not** run automatically: it costs about 13
seconds per four minutes of video, roughly three and a half minutes for an
hour-long recording, to answer a question that came back "no problem" on the
real test above. Sampling with `-read_intervals` was tried and does not work —
advert splices restart timestamps, so time-based seeks never reach the second
encoding.

### A stalled stream cannot hang a recording

`-reconnect` alone is a trap: it makes ffmpeg retry *for ever*. Seen on an
AES-encrypted Pluto stream — 18s captured, then `Unable to open resource` on a
segment plus TLS errors, and ffmpeg sat retrying until killed by hand. A
scheduled recording would have hung indefinitely.

Two bounds, because neither is sufficient alone:

- `-rw_timeout 30000000` — give up on a read that stalls for 30s rather than
  retrying a segment that will never arrive.
- A **wall-clock deadline** in the script, `duration + max(60s, duration/4)`.
  ffmpeg gets SIGTERM first so it finalises the file, then SIGKILL. The slack
  is generous because HLS back-buffer lets ffmpeg run *ahead* of real time, so
  wall-clock and stream time are not the same thing.

A cut-short capture is kept, not discarded — the stream failing halfway is
exactly when you want the first half. Success is judged on bytes written, not
on ffmpeg's exit status, which is non-zero after a SIGTERM. Partial results say
so:

```
Al-Jazeera_Newshour_2026-08-15_1400.ts  (412.7 MB, 1830.0s)  [PARTIAL: stream stalled, terminated at the deadline]
```

and the sidecar carries `captured_seconds`, `timed_out` and `complete` so the
outcome is inspectable afterwards.

### Two nuances

**Guides drift**, so `programme` pads by default: 30s before, 120s after.
`now` and `at` pad nothing, because you stated the window.

**HLS gives you back-buffer.** ffmpeg starts from segments the CDN already
published, so it runs ahead of real time before settling — a 45s capture
completed in 36s of wall clock. `-t` counts *stream* seconds, not wall-clock,
so a recording can finish before the programme really ends. The 120s end-pad
absorbs a typical 30-60s buffer.

### What it deliberately does not do

- **Series rules.** "Record Newshour every day" needs a daemon and dedup state.
  Excluded by choice; schedule each airing instead.
- **HTSP.** The protocol Kodi used to talk to Tvheadend. Unused here.
- **A web UI.** `record.py list`, `ls`, and `vlc.py --recordings` cover it.

---

## Watching in VLC — `vlc.py`

Symlinked at the repo root; the file is `dev/vlc.py`. FastChannels has no
authentication, so nothing here needs a token.

```bash
./vlc.py --list-categories                  # categories with counts
./vlc.py --channel "Al Jazeera" --play      # one channel
./vlc.py --category News --play             # the whole category
./vlc.py --category News --write ~/news.m3u
./vlc.py --source pluto --play              # one provider
./vlc.py --recordings                       # what record.py has captured
```

### Adverts, and why a playlist wrapper broke playback

`./vlc.py --channel "90s Throwback" --play` used to die at the first advert
break. The same URL pasted straight into VLC plays **through** breaks
indefinitely. The difference was never the stream — it was the playlist.

VLC handles ad-stitched channels natively. At each discontinuity it creates a
*new pair* of elementary streams and switches to them; VLC's Codec panel after
a few breaks shows streams `0, 3, 4, 19, 20` — non-contiguous, because it has
built a fresh audio/video pair per break and is back on the movie at 1216x684.
ffmpeg's HLS demuxer does not do this, which is why every ffmpeg-based
workaround failed.

Two bugs conspired:

**`--channel` did not prefer an exact match.** "90s Throwback" also matched
"XITE 90s Throwback", so VLC received a **two-item playlist**.

**A single channel was still wrapped in a playlist.** Given a playlist, VLC
treats a mid-stream error as "this item failed" and advances or stops. Given a
direct MRL, it re-initialises and carries on. So the wrapper turned a
recoverable discontinuity into the end of playback.

Now an exact name match wins, ambiguous names list the alternatives on stderr,
and a single channel is handed to VLC as a bare URL:

```
/usr/bin/vlc --no-one-instance http://nucbox.local:5523/play/pluto/5f4d....m3u8
```

`--write` still produces a playlist file, and multi-channel selections still
produce one — the direct-URL path applies only to `--play` with a single match.

**A single channel is pinned to its highest rendition.** A `/play` URL
redirects to a provider master listing several renditions. Handing VLC the
*master* goes wrong twice on ad-stitched channels: it opens the lowest
rendition and stays there (426x240), and the rendition set changes at an
advert splice. Handing it **one** rendition removes adaptation entirely —
nothing to get stuck on, nothing to re-select:

```
follow the /play redirect        -> provider master.m3u8 (+ the URL curl ended on)
pick the highest BANDWIDTH       -> e.g. 3321280
urljoin against the FINAL url    -> absolute rendition URL
```

Resolution must be against the URL curl *ended on*, not the `/play` URL: the
child paths are relative to the provider's master, and its session query string
must not be inherited.

Measured across providers, all muxed audio+video:

| channel | pinned rendition |
| --- | --- |
| 90s Throwback (pluto) | 1216x684 |
| Al Jazeera (tcl) | 1920x1080 |
| NHRA (freelivesports) | 1920x1080 |

**A rendition carries its own audio**, despite the master's `AUDIO="audio"`
group — that group offers *alternative* audio such as the audio-description
track. An earlier note here claimed variants were video-only and that pinning
would cost the sound. That was wrong.

`--no-pin` hands VLC the master and lets it adapt. `--network-caching=3000`
gives a 3s buffer, which a pinned high rendition wants.

Four VLC launch flags were tried before this and none worked, because they all
tried to steer VLC's adaptive selection rather than removing it:
`--adaptive-logic=highest` and `=fixedrate` fetched the top rendition but
**broke playback at adverts** by pinning a *representation* inside a still-
adaptive session; `--adaptive-bw` and `=nearoptimal` changed what was fetched
but not what was decoded.

Worth knowing if the Codec panel looks strange: VLC mis-parses this master.
The variants carry no `RESOLUTION` attribute and reference `AUDIO="audio"`, so
VLC files every video rendition under an adaptation set called *"audio
Original"* — there is no video adaptation set at all, and the panel shows H264
streams described as audio. That is cosmetic; playback is unaffected.

**Pasting a URL into VLC by hand:** use the **master**, not a variant. Every
variant carries `AUDIO="audio"`, meaning the variant playlists are video-only —
pinning the 3.3 Mbps HD variant gets you picture with no sound. The master has
both and VLC selects the top variant itself.

### When a channel spins — `--relay`

A different failure, and still real: content segments from the content CDN, ad
segments from another host, the playlist changing origin mid-stream, plus a
SUBTITLES rendition. NHRA on `freelivesports` does this and VLC spins without
starting. ffmpeg handles it silently, so relaying through ffmpeg helps:

```bash
./vlc.py --channel NHRA --relay          # re-encodes to a fixed size
./vlc.py --channel NHRA --relay-copy     # passthrough, free
```

**`--relay` cannot fix adverts.** An ffmpeg supervisor that restarted on stall
was tried and reverted: restarting re-enters the playlist at the live edge,
which during a break is still inside the break, so it replayed the same advert
seven times running. Let VLC handle adverts; use `--relay` only for the
spinning case.

### One window per channel

VLC has **no "open in a new window" action** — picking an item in its playlist
replaces whatever that instance is playing. Watching two channels at once means
two VLC *processes*, so `--play` passes `--no-one-instance` and every
invocation gets its own viewer:

```bash
./vlc.py --channel "Al Jazeera" --play
./vlc.py --channel "Euronews" --play      # second window, first keeps playing
```

The flag is passed explicitly rather than relying on VLC's `one-instance`
preference. It defaults to disabled, but a file manager or distro preset can
flip it, and then a second launch would silently hijack the window already
playing something. `--reuse` asks for that hand-off deliberately.

### Three edges it handles for you

**Both playlists, always.** `/m3u` and `/m3u/gracenote` are *disjoint*, not a
superset and subset. `--source pluto` is 176 channels from the first and 169
from the second: taking only `/m3u` loses nearly half. Both are fetched and
merged, which is why `--write`/`--play` produce a file rather than handing VLC
a single URL.

**Categories match exactly.** `/m3u?category=News` returns `News` and *not*
`Local News` — they are separate categories, and the playlist endpoints do not
substring match the way `/api/channels` does.

**Channel names in the player.** FastChannels puts full descriptions in the M3U
as `tvg-description` / `tvc-guide-description`, and those contain commas:

```
#EXTINF:-1 … tvg-description="…challenging outdoor jobs, WeatherSpy is
reinventing the way we watch weather." …,WeatherSpy
```

VLC splits the title on a comma *inside the quoted attributes* instead of on
the separator comma, so its playlist shows a sentence fragment where the
channel name should be. Only channels whose description happens to contain no
comma render correctly, which makes the breakage look random. The script
rewrites every `#EXTINF` down to the attributes a player actually uses
(`tvg-id`, `tvg-name`, `tvg-chno`, `tvg-logo`, `group-title`) with commas and
quotes stripped from the values. Side benefit: the full lineup drops from
1.2 MB to 588 KB.

### Clicking "Open in VLC" in the guide

The guide's channel preview has an **Open in VLC** button
(`app/templates/admin/guide.html:1183`) pointing at
`/play/{source}/{id}.m3u`, which is served with

```
Content-Disposition: attachment; filename="OM017937.m3u"
```

so the browser saves it to `~/Downloads` instead of launching anything. There
is no server-side header that fixes this for both browsers, and it is worth
recording why, because `inline` looks like the obvious answer and is not:

| Header | Firefox | Chrome |
| --- | --- | --- |
| `attachment` | saves to Downloads | saves to Downloads |
| `inline` | offers *Open with* → works | renders it in Chrome's own `<video>` player, which cannot play raw HLS — a black, dead page with no way out |

Handing a file to an external application is a capability browsers removed
deliberately; no MIME type restores it. So the fix is a **one-time browser
setting**:

- **Chrome** — click the button once, then on the download choose
  *Always open files of this type*.
- **Firefox** — click once, then right-click the entry in the Downloads panel
  and choose *Always Open Similar Files*.

After that every click downloads a ~90-byte `.m3u` and immediately launches
VLC. Nothing else on this machine needs changing: `.m3u` already resolves to
`audio/x-mpegurl`, which maps to `vlc.desktop`, and snap Firefox has the
`desktop` and `home` interfaces connected so it reaches host VLC through
`xdg-desktop-portal`.

---

## Repository

This working copy lives in a **private** repo, independent of upstream:

| | |
| --- | --- |
| Repo | `github.com/marcoeg/fastchannels-private` (private) |
| Fork? | **No** — created with `gh repo create` and pushed, so it is outside upstream's fork network and can be private. A GitHub fork cannot. |
| History | 1,323 upstream commits + local additions, tags `v5.0.2`–`v5.0.4` |
| Actions | **Disabled.** Upstream's `publish-image.yml` fires on every push to `main` and does a two-architecture QEMU build (Chromium, Camoufox, Node 24) — enough to consume a private repo's entire monthly Actions allowance in one run. |

Re-enable Actions if you ever want your own image:

```bash
gh api -X PUT repos/marcoeg/fastchannels-private/actions/permissions -F enabled=true
```

### Remotes

```
origin    git@github.com:marcoeg/fastchannels-private.git   your repo — push here
upstream  git@github.com:kineticman/FastChannels.git        read-only reference
```

`main` tracks `origin/main`, so a bare `git push` / `git pull` always goes to
your repo. You have `READ` on upstream, so a stray push there is rejected
anyway; to make that failure immediate and obvious:

```bash
git remote set-url --push upstream DISABLED
```

### Taking upstream changes

```bash
git fetch upstream
git merge upstream/main      # or: git rebase upstream/main
git push
```

Two tracked files diverge from upstream, so expect conflicts there. Each has
a saved patch under `dev/` (gitignored upstream, so it survives a pull):

| File | Change | Patch |
| --- | --- | --- |
| `app/worker.py` | `FC_YTDLP_AUTO_UPGRADE` gate | `dev/ytdlp-gate.patch` |
| `app/scrapers/xumo.py` | Follow one JSON-pointer hop when a channel resolves to a provider API instead of a manifest — fixes Shaka error 4015 on Local Now | `dev/xumo-indirection.patch` |

To take upstream's version wholesale and re-apply on top:

```bash
git checkout --theirs app/worker.py app/scrapers/xumo.py
git apply -3 dev/ytdlp-gate.patch dev/xumo-indirection.patch
```

The xumo fix is a genuine upstream bug rather than a local preference, so it
is a reasonable candidate to contribute back.

### Local commits

| Commit | Contents |
| --- | --- |
| `Add HTTP API documentation` | `docs/openapi.yaml`, `docs/API.md` |
| `Add news-channel origin extractor` | `README-MEG.md`, `dev/news_origins.py`, `dev/markets.py` |
| `Local hardening: …` | `docker-compose.override.yml`, `app/worker.py`, `dev/ytdlp-gate.patch` |

The `app/worker.py` change is isolated in its own commit so it can be reverted
or rebased independently of everything else.

### What stays out of git

`.git/info/exclude` (local-only, never committed, so it cannot conflict on a
pull) covers:

```
news*.csv
news*.json
dev/__pycache__/
```

Those are derived exports — regenerable in about four minutes with
`dev/news_origins.py --urls --resolve`. More to the point, **133 of the
resolved CDN URLs carry provider session tokens**, which have no business in a
repo even a private one.

---

## Local configuration

`docker-compose.override.yml` is auto-loaded by `docker compose up` — no `-f`
needed — and carries three deviations from upstream defaults:

| Setting | Effect |
| --- | --- |
| `ports: !override ["5523:5523"]` | Published on all interfaces so LAN devices can reach it — **no authentication on any route**, so anything that can route here has full admin. `ufw` is not a mitigation: Docker's iptables rules precede ufw's INPUT chain. The `!override` tag is still required, or Compose *appends* and leaves a duplicate mapping. |
| `public_base_url = http://nucbox.local:5523` | Set in the DB, not this file. Makes `/m3u` and `/epg.xml` emit URLs a LAN client can reach; without it every playlist entry points at the client itself. |
| `VERSION_CHECK_ENABLED: "0"` | Stops the 12-hourly GitHub releases poll. |
| `FC_YTDLP_AUTO_UPGRADE: "0"` | Stops the weekly unattended `pip install` — **requires the `app/worker.py` gate**, which upstream does not have. |

### Applying a config or code change

The base `docker-compose.yml` has no bind mount for `app/`, so the container
holds a `COPY` of the source. Editing `app/worker.py` on the host does nothing
until you rebuild:

```bash
sudo docker compose up -d --build
```

`COPY . .` sits near the end of the Dockerfile, so only the last two layers
re-run — the pip, Playwright and Camoufox layers stay cached. This is a fast
rebuild, not a cold one.

Verify the gate took effect:

```bash
sudo docker compose logs fastchannels | grep -i 'auto-upgrade'
# [scheduler] yt-dlp auto-upgrade disabled (FC_YTDLP_AUTO_UPGRADE=0)
```

Absence of that line means the rebuild did not pick up the change.

---

## See also

- `docs/openapi.yaml` and `docs/API.md` — the FastChannels HTTP API these
  scripts read from. 184 paths, verified against the route handlers by
  `dev/check_api_docs.py`.
- `dev/hermes/SKILL.md` — the same API described for the Hermes agent, with
  `dev/hermes/PROMPT.md` as the onboarding message. `dev/hermes/RECORD-SKILL.md`
  is a second skill covering `record.py`, with `dev/hermes/RECORD-PROMPT.md`
  as its onboarding message. Deliberately **not** under `.claude/`,
  which Claude Code loads automatically — these are written for a different
  agent and should not be picked up here.
