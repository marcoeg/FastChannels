# Tvheadend + FastChannels

Feeding [Tvheadend](https://github.com/tvheadend/tvheadend) (GPLv3, actively
maintained) from FastChannels, as a free alternative to Channels DVR — which is
closed-source and $8/month.

Tvheadend turns out to be a close fit. Checked against its source rather than
its docs:

| What FastChannels emits | What Tvheadend does with it | Where |
| --- | --- | --- |
| `tvg-id="{source}.{id}"` | stores as `iptv_epgid`, the key XMLTV matches on | `iptv_auto.c` |
| `tvg-chno` | becomes the channel number | `iptv_auto.c:296` |
| `tvg-logo`, `group-title` | channel icon and tag | `iptv_auto.c` |
| a `302` from `/play/...` | followed — `hc_handle_location = 1` | `iptv_http.c:595` |
| HLS, including AES-128 | native reader with `hls_key_url` handling | `iptv_http.c` |
| `/epg.xml` | parsed by the external XMLTV grabber | `xmltv.c` |

Because `tvg-id` and the XMLTV `<channel id>` are the same string, **EPG
matching needs no manual mapping**.

## Setup

```bash
# 1. credentials, written before Tvheadend's first start
python3 dev/tvheadend/setup.py --write-superuser-only

# 2. start it
cd dev/tvheadend && sudo docker compose up -d && cd -

# 3. create the network and enable the XMLTV grabber, over Tvheadend's API
python3 dev/tvheadend/setup.py

# 4. push the guide
python3 dev/tvheadend/push_epg.py
```

Then open http://127.0.0.1:9981 (user `admin`, password `fastchannels` unless
you passed `--user`/`--password`). Channels appear under
**Configuration > Channel/EPG > Channels**.

Verified end to end on this install: **2,014 channels, 91,796 EPG events**, and
a channel streaming 17 MB of valid MPEG-TS out of Tvheadend, sourced from
FastChannels.

`--dry-run` works before Tvheadend is running — it checks the playlist and
prints the network config it would write.

## Why `network_mode: host`

This is the part that decides whether anything works at all.

FastChannels emits **literal `http://127.0.0.1:5523/play/...` URLs** inside its
M3U, because Public Base URL is unset. On the host network those resolve
correctly from inside the Tvheadend container. On a Docker bridge network they
would point at Tvheadend itself and every channel would fail.

The alternative is to join FastChannels' Docker network — container-to-container
traffic ignores the `127.0.0.1:5523` host binding entirely — and set
`public_base_url` to `http://fastchannels:5523`. That works, but then every link
in the FastChannels admin UI points at a hostname your browser cannot resolve.
Host networking avoids the trade-off, at the cost of Tvheadend binding
9981/9982 on all interfaces. It has its own authentication.

## The XMLTV shim

Tvheadend's XMLTV module cannot fetch a URL. It offers an *internal* grabber
that shells out to `tv_grab_*` scripts inside the container, or an *external*
grabber that reads a complete document from a Unix socket. `push_epg.py` uses
the socket — no image changes, no `tv_grab` protocol to implement:

```
FastChannels /epg.xml  ──>  config/epggrab/xmltv.sock  ──>  Tvheadend
```

Run it after each FastChannels scrape, or on a timer:

```bash
python3 dev/tvheadend/push_epg.py --loop 6h
```

It streams rather than buffering (the full guide is tens of megabytes) and uses
only the standard library, so `socat` is not needed — which matters, because
this host does not have it.

## Two behaviours worth knowing

**Nothing is probed.** `scan_create` is off and `service_sid` is on, so
`iptv_mux.c:416` creates one service per mux at creation time and channels
appear straight from the playlist — **zero streams opened**. With
`scan_create` on, Tvheadend would connect to all 2,014 streams to discover
services: that hammers every provider, trips Roku's source-wide cooldown, and
lets FastChannels' health check auto-disable channels wholesale. `--scan`
restores it if you ever want it.

**The first EPG push needs to happen twice.** On a lineup Tvheadend has not
seen before, the first parse creates the grabber channels from the `<channel>`
elements and links them — but the `<programme>` elements in the *same*
document are processed against links that did not exist when the parse began,
and are dropped. Observed here as 2,014 channels linked and **0 events**, then
91,796 events after an immediate re-push. `push_epg.py` now sends twice on a
one-shot run; `--loop` and `--once` send once, since by then the links exist.

## Known limits

**DRM channels will not work.** Tvheadend has no Widevine CDM, so anything
needing the PrismCast bridge is out. Same limitation as any non-browser client.

**Skip `/m3u/gracenote`.** Tvheadend does not understand
`tvc-guide-stationid`, so those entries would arrive with no EPG hook at all.
Plain `/m3u` + `/epg.xml` is the pairing that works — which is also simpler
than the two-playlist split Channels DVR needs.

**Proxied sources are fine.** Samsung and STIRR (21 of 79 News channels) have
no direct upstream URL, but Tvheadend fetches through `local_url` and
FastChannels does the proxying server-side.

**If a stream fails to demux**, re-run `setup.py --libav` to route muxes
through ffmpeg instead of the native HLS reader. Slower, but it copes with
containers the native path chokes on.

## Files

| File | |
| --- | --- |
| `docker-compose.yml` | Tvheadend on the host network, config bind-mounted so the XMLTV socket is reachable |
| `setup.py` | Creates the IPTV auto network and enables the XMLTV grabber, over the API. Idempotent. |
| `push_epg.py` | Streams `/epg.xml` into the grabber socket |
| `config/` | Tvheadend's configuration, including `superuser` and `epggrab/xmltv.sock` |

`config/superuser` holds a plaintext password and is mode `600`. It is covered
by `dev/` in `.gitignore`, so it never reaches a repository.
