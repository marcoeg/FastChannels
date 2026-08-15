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

### Keeping it fresh

A systemd **user** service runs the loop. User units were the right fit here:
lingering is already enabled for this account, so it starts at boot and
survives logout without any `sudo`, and it matches the existing
`hermes-gateway.service` pattern.

```bash
cp dev/tvheadend/fastchannels-epg.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fastchannels-epg.service

systemctl --user status fastchannels-epg.service
journalctl --user -u fastchannels-epg.service -f
```

It pushes on start and every six hours. `Restart=always` with a 60s delay
covers the ordinary failures — Tvheadend restarting, the container being
rebuilt — since the socket only exists while Tvheadend is running.

**No filesystem sandboxing in the unit, deliberately.** Connecting to a Unix
socket needs write access to the socket file, which lives under `/home`. Every
`ProtectSystem`/`ProtectHome` combination tried — including `full` with an
explicit `ReadWritePaths` carve-out — failed with `[Errno 13] Permission
denied` after 0 bytes, while the identical push worked by hand. Confirmed by
stripping them, at which point it succeeded immediately. `NoNewPrivileges`
creates no mount namespace and is kept.

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

## Playing streams in VLC

VLC cannot open any Tvheadend URL as shipped, and the cause is authentication.
`digest` is 1 — **Digest only** — and VLC leads with Basic:

```
no auth      -> 401
basic auth   -> 401     <- VLC
digest auth  -> 200
```

`http://user:pass@host/…` is also sent as Basic, so it 401s too.

`dev/vlc.py` (symlinked at the repo root) uses Tvheadend's **persistent
authentication code** instead: a per-user token in the password table, passed
as `?auth=CODE`. No configuration change, no password on the wire, and the
token is revocable by itself.

```bash
./vlc.py --via tvheadend --list-categories
./vlc.py --via tvheadend --category "Local News" --play
```

It defaults to `--via fastchannels`, which needs no token at all and reaches
the 206 Gracenote channels this network deliberately skips. Use
`--via tvheadend` when a stream is flaky and you want Tvheadend to own the
subscription and re-mux it, or when you are checking what the DVR itself sees.

Enable the code under Configuration > Users > Passwords if it is ever reset:
set *Persistent authentication* to Enable.

**Use `/playlist/auth/…`, not `/playlist/…`.** The segment controls how entries
*inside* the playlist are authenticated (`webui.c:92-97`). Plain `/playlist/…`
returns bare entry URLs, so VLC opens the list and then 401s on every channel.

**Recordings are out of scope for the code**, deliberately — `access.c:885`
does `a->aa_rights &= ACCESS_ADVANCED_STREAMING|ACCESS_STREAMING`, and
`/dvrfile/…` needs `ACCESS_RECORDER`. Play the `.ts` files in `recordings/`
directly; `./vlc.py --recordings` lists them.

## Recording files

`.ts` is deliberate, not a fallback: the DVR uses the `pass` profile
(MPEG-TS Pass-thru), which copies the stream with no transcode and tolerates a
truncated write. **MP4 is not available** — this build has no
`profile-transcode` (no libav), leaving `profile-mpegts`, `profile-matroska`,
`profile-htsp` and `profile-audio`. Matroska is the only other container and is
also a straight remux; it is the fix to reach for if the 44.1 kHz AAC channels
lose their audio track, since that rate is off-spec for MPEG-TS but fine in
Matroska.

The extension is never hardcoded — `pathname` ends in `$x`, filled from the
active muxer, so changing the profile changes the extension by itself.

```
pathname = $c - $t - %F %H-%M$n.$x
           -> Al Jazeera - Newshour - 2026-08-15 14-12.ts
```

`%H-%M` rather than `%R`, because `%R` emits `14:12` and colons in filenames
are tiresome to quote. `docs/property/pathname.md` lists every specifier;
`$q` gives a Plex/Jellyfin layout but needs episode and genre data that
FAST-channel guides rarely carry.

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

## API documentation

Tvheadend has none. Its 134 markdown files cover the web UI, configuration and
DVB tuning; not one describes the ~185 JSON endpoints the UI is built on.
`docs/markdown/url.md` documents the streaming URLs and stops there.

`gen_openapi.py` generates one, reading the endpoint table straight from the
Tvheadend source — the authoritative list, since every endpoint is registered
there with its access level and handler:

```bash
python3 dev/tvheadend/gen_openapi.py --verify
#   wrote openapi.yaml — 185 endpoints
#   13/13 sampled endpoints answered as documented
```

Regenerate it after pulling a newer Tvheadend; nothing is hand-maintained.

Half the API is served by generic idnode handlers — 26 `*/grid`, 26 `*/class`,
plus `idnode/load`/`save`/`delete` — so the spec defines those shapes once
rather than 185 times. Conventions worth knowing: everything is POST with
form-encoded parameters whose *values* are JSON documents (`conf=`, `node=`,
`filter=`), digest auth, and `{"entries": […], "total": N}` list envelopes.

The most useful thing the API offers in place of documentation is
introspection: `*/class` and `mpegts/network/builders` return every valid
property with its type, caption and description. That is live rather than
prose, and it is how `service_sid`, `scan_create` and `refetch_period` were
found.

## Files

| File | |
| --- | --- |
| `docker-compose.yml` | Tvheadend on the host network, config bind-mounted so the XMLTV socket is reachable |
| `setup.py` | Creates the IPTV auto network and enables the XMLTV grabber, over the API. Idempotent. |
| `push_epg.py` | Streams `/epg.xml` into the grabber socket |

| `gen_openapi.py` | Generates `openapi.yaml` from the Tvheadend source |
| `openapi.yaml` | 185 endpoints, 14 tags — documentation that does not otherwise exist |
| `fastchannels-epg.service` | systemd user unit for the six-hourly EPG push |
| `config/` | Tvheadend's configuration, including `superuser` and `epggrab/xmltv.sock` |

`config/superuser` holds a plaintext password and is mode `600`. It is covered
by `dev/` in `.gitignore`, so it never reaches a repository.
