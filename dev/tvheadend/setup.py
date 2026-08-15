#!/usr/bin/env python3
"""
Point Tvheadend at FastChannels, over Tvheadend's own JSON API.

Creates an IPTV Automatic Network fed by a FastChannels M3U, and enables the
external XMLTV grabber that push_epg.py feeds. Idempotent: re-running updates
the existing network rather than creating a second one.

Why an *automatic* network: Tvheadend parses the M3U itself and maps the
attributes FastChannels emits — tvg-chno to the channel number, tvg-logo to
the icon, group-title to a tag, and crucially tvg-id to `iptv_epgid`, which is
the key XMLTV matching uses. So channel numbers and EPG line up with no
translation layer. `bouquet` then auto-creates channels for every service, and
`refetch_period` re-reads the playlist so scrapes propagate on their own.

Usage:
    python3 dev/tvheadend/setup.py                       # first run
    python3 dev/tvheadend/setup.py --feed news-only      # a FastChannels feed
    python3 dev/tvheadend/setup.py --dry-run

Credentials come from dev/tvheadend/config/superuser, which this script writes
before Tvheadend's first start if it is missing (see --help for the flow).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_DIR = HERE / "config"
SUPERUSER = CONFIG_DIR / "superuser"

TVH = "http://127.0.0.1:9981"
FASTCHANNELS = "http://127.0.0.1:5523"
NETWORK_NAME = "FastChannels"
NETWORK_CLASS = "iptv_auto_network"
# Container path. Bind-mounted from RECORDINGS_HOST by docker-compose.yml;
# the container side deliberately never changes, so the DVR config's storage
# setting and the filenames already in the database stay valid if the host
# directory moves.
RECORDINGS_PATH = "/recordings"
# Host side of that mount. Must match docker-compose.yml.
RECORDINGS_HOST = pathlib.Path.home() / "news" / "tvheadend" / "recordings"


class Tvheadend:
    """Minimal client for Tvheadend's API.

    Endpoints take form-encoded parameters whose values are JSON documents —
    `class` + `conf` for network creation, `node` for idnode saves. Auth is
    digest by default, with basic as a fallback, so both handlers are wired up.
    """

    def __init__(self, base: str, user: str, password: str):
        self.base = base.rstrip("/")
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, self.base, user, password)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(mgr),
            urllib.request.HTTPBasicAuthHandler(mgr),
        )

    def call(self, path: str, **params) -> dict:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"{self.base}/api/{path}", data=data)
        with self.opener.open(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else {}

    def wait(self, timeout: int = 90) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.call("serverinfo")
                return True
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    print(f"  auth rejected ({exc.code}) — check {SUPERUSER}", file=sys.stderr)
                    return False
            except Exception:
                pass
            time.sleep(2)
        return False


def ensure_superuser(user: str, password: str) -> bool:
    """Write the superuser file so the API is scriptable without the wizard.

    Tvheadend reads <config>/superuser at startup and accepts those credentials
    regardless of access-control entries. It must exist *before* the first
    start, so this is a no-op once Tvheadend is running with it.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Pre-create the recordings bind-mount target too. Docker creates a missing
    # bind source as root, which the container — running as your uid — then
    # cannot write to, and the failure surfaces later as a recording error
    # rather than a startup one.
    RECORDINGS_HOST.mkdir(parents=True, exist_ok=True)
    if SUPERUSER.exists():
        return False
    SUPERUSER.write_text(json.dumps({"username": user, "password": password}, indent=2))
    SUPERUSER.chmod(0o600)
    return True


def playlist_url(feed: str | None, gracenote: bool) -> str:
    """Which FastChannels M3U to feed Tvheadend.

    Not the Gracenote playlist by default: Tvheadend has no
    tvc-guide-stationid support, so those entries would arrive with no EPG
    hook at all. Plain /m3u plus /epg.xml is the pairing that works here.
    """
    path = f"/feeds/{feed}/m3u" if feed else "/m3u"
    if gracenote:
        path += "/gracenote"
    return FASTCHANNELS + path


def find_network(tvh: Tvheadend) -> dict | None:
    grid = tvh.call("mpegts/network/grid", limit="999")
    for entry in grid.get("entries", []):
        if entry.get("networkname") == NETWORK_NAME:
            return entry
    return None


def network_conf(url: str, args) -> dict:
    return {
        "networkname": NETWORK_NAME,
        "url": url,
        # Re-read the playlist so channels added or removed by a FastChannels
        # scrape propagate without touching Tvheadend.
        "refetch_period": args.refetch,
        # Create a bouquet mapping every service to a channel — this is what
        # turns muxes into usable channels automatically.
        "bouquet": True,
        "max_streams": args.max_streams,
        "priority": 1,
        "spriority": 1,
        # Do NOT probe every stream. With scan_create on, Tvheadend opens each
        # mux to discover its services — 2,000+ live /play requests, which
        # hammers every provider, trips Roku's source-wide cooldown, and lets
        # FastChannels' health check auto-disable channels en masse.
        #
        # service_sid instead makes iptv_mux.c create one service per mux at
        # creation time (iptv_mux.c:416) and fire the bouquet trigger, so
        # channels appear straight from the playlist with no stream opened at
        # all. Streams are then only touched when something actually watches
        # or records. --scan restores the probing behaviour.
        "scan_create": args.scan,
        "service_sid": 0 if args.scan else 1,
        "tsid_zero": True,
        "ssl_peer_verify": False,
        # 0 = native HTTP/HLS demux. Set 1 (--libav) to route every mux through
        # ffmpeg instead; slower, but it copes with streams the native HLS
        # reader chokes on.
        "use_libav": 1 if args.libav else 0,
    }


def enable_xmltv(tvh: Tvheadend, dry_run: bool) -> str | None:
    """Turn on the external XMLTV grabber and report its socket path.

    Module titles are prefixed by kind — "External: XMLTV" against
    "Internal: XMLTV: Hungary" and a dozen other tv_grab_* scripts. Matching
    loosely on "xmltv" picks whichever internal grabber sorts first, which is
    both wrong and invisible until no EPG appears, so match the prefix exactly.
    """
    modules = tvh.call("epggrab/module/list").get("entries", [])
    target = next((m for m in modules
                   if (m.get("title") or "").strip().lower() == "external: xmltv"), None)
    if target is None:
        available = ", ".join(sorted(m.get("title", "?") for m in modules
                                     if "xmltv" in (m.get("title") or "").lower()))
        print(f"  ! no 'External: XMLTV' module found. Saw: {available}", file=sys.stderr)
        return None

    uuid = target["uuid"]
    if not dry_run:
        tvh.call("idnode/save", node=json.dumps({"uuid": uuid, "enabled": True}))
        disable_ota_epg(tvh)
    return uuid


def disable_ota_epg(tvh: Tvheadend) -> None:
    """Stop Tvheadend scraping EPG out of the transport stream.

    This is the same hazard as `scan_create`, arriving by a different route.
    The over-the-air grabber opens a subscription per mux to read EIT tables —
    meaningful for DVB, useless here because the guide arrives as XMLTV over a
    socket, and actively harmful: it opens real provider streams, and it holds
    the IPTV inputs while doing so.

    The defaults are `ota_initial=True` (scan on every startup) and an
    `ota_cron` firing at 02:04 and 14:04, each mux held up to `ota_timeout`
    600s. Observed here as ten "epggrab" subscriptions in state Bad pinning
    both IPTV inputs, during which every recording failed with "No input
    source available" and streaming returned no data. It is a nasty failure
    because it clears itself before you look, and reappears after each
    restart.

    epgauto_default stops newly created muxes opting back in.
    """
    tvh.call("epggrab/config/save",
             node=json.dumps({"ota_initial": False, "ota_cron": ""}))
    for net in tvh.call("mpegts/network/grid", limit="999").get("entries", []):
        if net.get("networkname") == NETWORK_NAME:
            tvh.call("idnode/save", node=json.dumps(
                {"uuid": net["uuid"], "epgauto_default": False}))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "First run:\n"
            "  1. python3 dev/tvheadend/setup.py --write-superuser-only\n"
            "  2. cd dev/tvheadend && sudo docker compose up -d\n"
            "  3. python3 dev/tvheadend/setup.py\n"
            "  4. python3 dev/tvheadend/push_epg.py\n"
        ),
    )
    ap.add_argument("--tvh", default=TVH, help=f"Tvheadend base URL (default {TVH})")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="fastchannels")
    ap.add_argument("--feed", help="FastChannels feed slug; omit for the master /m3u")
    ap.add_argument("--gracenote", action="store_true",
                    help="use the Gracenote playlist (Tvheadend cannot use its guide IDs)")
    ap.add_argument("--refetch", type=int, default=60, metavar="MINS",
                    help="playlist re-fetch period in minutes (default 60)")
    ap.add_argument("--max-streams", type=int, default=10,
                    help="concurrent streams allowed from this network (default 10)")
    ap.add_argument("--scan", action="store_true",
                    help="probe every stream after creating the network. Opens one "
                         "connection per channel — hammers providers, trips rate "
                         "limits, and can cause FastChannels to auto-disable "
                         "channels. Off by default; services are created from the "
                         "playlist instead.")
    ap.add_argument("--libav", action="store_true",
                    help="demux every mux through ffmpeg instead of the native HLS reader")
    ap.add_argument("--write-superuser-only", action="store_true",
                    help="write config/superuser and exit, before Tvheadend's first start")
    ap.add_argument("--dry-run", action="store_true", help="show what would change")
    args = ap.parse_args()

    if ensure_superuser(args.user, args.password):
        print(f"wrote {SUPERUSER} (user={args.user})")
        if not args.write_superuser_only:
            print("  Tvheadend reads this at startup — start or restart it before continuing.")
    elif args.write_superuser_only:
        print(f"{SUPERUSER} already exists — leaving it alone")
    if args.write_superuser_only:
        return 0

    # Check the playlist first: it is the likelier failure, and a --dry-run
    # should be useful before Tvheadend is even started.
    url = playlist_url(args.feed, args.gracenote)
    print(f"playlist: {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            channels = sum(1 for line in r.read().decode("utf-8", "replace").splitlines()
                           if line.startswith("#EXTINF"))
        print(f"  {channels} channels in the playlist")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not read the playlist: {exc}", file=sys.stderr)
        print("    Tvheadend would fail the same way — fix this first.", file=sys.stderr)
        return 1

    tvh = Tvheadend(args.tvh, args.user, args.password)
    print(f"waiting for Tvheadend at {args.tvh} …")
    reachable = tvh.wait(timeout=10 if args.dry_run else 90)
    if not reachable:
        if not args.dry_run:
            print("error: Tvheadend did not answer. Is the container running?", file=sys.stderr)
            return 1
        print("  not reachable — continuing anyway because this is a dry run")

    conf = network_conf(url, args)
    existing = find_network(tvh) if reachable else None

    if args.dry_run:
        print("\n--dry-run, nothing written. Network config would be:")
        print(json.dumps(conf, indent=2))
        print("\nexisting network: "
              + (existing["uuid"] if existing else ("none" if reachable else "unknown (Tvheadend down)")))
        return 0

    if existing:
        net_uuid = existing["uuid"]
        conf["uuid"] = net_uuid
        tvh.call("idnode/save", node=json.dumps(conf))
        print(f"updated existing network {net_uuid}")
    else:
        resp = tvh.call("mpegts/network/create",
                        **{"class": NETWORK_CLASS, "conf": json.dumps(conf)})
        net_uuid = resp.get("uuid")
        print(f"created network {net_uuid or '?'}")
        # Muxes are created asynchronously from the playlist; the bouquet only
        # appears once they exist.
        print("  waiting for the playlist to be parsed …")
        time.sleep(15)

    # The bouquet the network creates is disabled by default, so services are
    # never mapped to channels until it is switched on — this is the step that
    # actually produces a lineup.
    if not args.dry_run:
        for b in tvh.call("bouquet/grid", limit="999").get("entries", []):
            if (b.get("source") or "").endswith(net_uuid or "") and not b.get("enabled"):
                tvh.call("idnode/save", node=json.dumps({"uuid": b["uuid"], "enabled": True}))
                print(f"enabled bouquet {b.get('name')} ({b.get('services_seen', 0)} services)")

    # Recordings default to /var/lib/tvheadend, which is NOT the bind-mounted
    # path — only /var/lib/tvheadend/config is. Left alone, recordings would
    # land in the container's writable layer and disappear on the next rebuild.
    if not args.dry_run:
        for c in tvh.call("dvr/config/grid", limit="99").get("entries", []):
            if c.get("storage") != RECORDINGS_PATH:
                tvh.call("idnode/save", node=json.dumps(
                    {"uuid": c["uuid"], "storage": RECORDINGS_PATH}))
                print(f"recordings -> {RECORDINGS_PATH} (was {c.get('storage')})")

    uuid = enable_xmltv(tvh, args.dry_run)
    if uuid:
        sock = CONFIG_DIR / "epggrab" / "xmltv.sock"
        print(f"enabled the external XMLTV grabber ({uuid})")
        print(f"  socket: {sock}")
        print(f"  feed it: python3 {HERE / 'push_epg.py'}")

    n_chn = tvh.call("channel/grid", limit="1").get("total", 0)
    print(f"\nTvheadend now has {n_chn} channels.")
    print(f"  UI: {args.tvh}  (user {args.user})")
    print("  Channels appear under Configuration > Channel/EPG > Channels.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
