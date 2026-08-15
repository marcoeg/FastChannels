#!/usr/bin/env python3
"""
Export every enabled channel in a category, with both playback endpoints.

Pulls together three sources the server never joins for you:

  /api/channels          the channel record — 42 fields, but no source_channel_id
                         and therefore no way to build a playback URL
  /m3u + /m3u/gracenote  the playback URL, feed channel number, proxied logo,
                         stream codecs and Gracenote station ID
  /play/<src>/<id>.m3u8  followed one hop to the provider's own stream URL

Two endpoints are reported per channel:

  local_url   the FastChannels URL — resolves tokens and expiring URLs at play
              time. This is what you give a player.
  remote_url  the provider's own stream URL. Useful for analysis; it usually
              carries a session token and is sometimes bound to the IP that
              resolved it, so it is a snapshot, not a durable link.

Usage:
    python3 dev/export_channels.py --list-categories
    python3 dev/export_channels.py News
    python3 dev/export_channels.py "Local News" --format csv -o local-news.csv
    python3 dev/export_channels.py Sports --no-resolve
    python3 dev/export_channels.py News --public-base http://192.168.50.17:5523

WARNING: resolving remote URLs is not a read-only operation. It exercises the
real /play path, so the server runs its normal background health check and may
auto-disable channels it finds to be DRM-protected or dead. Use --no-resolve
for a strictly read-only export.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit

DEFAULT_BASE = "http://127.0.0.1:5523"
M3U_PATHS = (("/m3u", "standard"), ("/m3u/gracenote", "gracenote"))
_EXTINF_ATTR = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
_MANIFEST_SUFFIXES = (".m3u8", ".mpd")

# Attributes carried by the playlist that /api/channels does not expose.
M3U_FIELDS = {
    "tvg-chno": "feed_channel_number",
    "tvg-logo": "logo_proxy_url",
    "tvc-stream-vcodec": "video_codec",
    "tvc-stream-acodec": "audio_codec",
    "tvc-guide-stationid": "gracenote_station_id",
    "tvg-description": "guide_description",
}

# Column order for CSV. Anything not listed is appended alphabetically, so a
# future server field still lands in the output instead of being dropped.
LEAD_COLUMNS = [
    "id", "name", "source_name", "category", "country", "language",
    "feed_channel_number", "number", "playlist",
    "local_url", "remote_url", "remote_host", "delivery",
    "source_channel_id", "stream_type", "video_codec", "audio_codec",
    "gracenote_id", "gracenote_station_id", "gracenote_mode",
    "is_enabled", "is_active", "disable_reason", "requires_drm_bridge",
    "epg_count", "epg_first", "epg_last", "epg_programs", "epg_hours",
]


def get_json(url: str, timeout: int = 60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def get_text(url: str, timeout: int = 60) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def list_categories(base: str) -> list[tuple[str, int]]:
    stats = get_json(f"{base}/api/stats")
    return [(c["name"], c["count"]) for c in stats.get("categories", [])]


def fetch_channels(base: str, category: str, contains: bool) -> list[dict]:
    """Fetch channels for a category.

    /api/channels matches category as a substring, so "News" also returns
    "Local News". Narrow to an exact match client-side unless asked otherwise.
    """
    url = f"{base}/api/channels?" + urlencode({"category": category, "per_page": 10000})
    channels = get_json(url).get("channels", [])
    if contains:
        return channels
    wanted = category.strip().lower()
    return [c for c in channels if (c.get("category") or "").strip().lower() == wanted]


def fetch_playlists(base: str, categories: list[str]) -> dict[tuple[str, str], dict]:
    """Map (source, lowercased name) -> playlist-derived fields.

    Both playlists are required: channels holding a Gracenote ID are
    partitioned into /m3u/gracenote and are absent from /m3u. The category
    filter here is an exact match, unlike /api/channels, so every category
    value has to be passed explicitly.
    """
    query = urlencode([("category", c) for c in categories])
    out: dict[tuple[str, str], dict] = {}

    for path, kind in M3U_PATHS:
        try:
            body = get_text(f"{base}{path}?{query}")
        except Exception as exc:  # noqa: BLE001
            print(f"warning: could not fetch {path}: {exc}", file=sys.stderr)
            continue

        pending: dict | None = None
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("#EXTINF"):
                attrs = dict(_EXTINF_ATTR.findall(line))
                display = line.rsplit(",", 1)[-1].strip()
                chan_id = attrs.get("channel-id") or ""
                source, _, scid = chan_id.partition(".")
                pending = {
                    "_key": (source, (attrs.get("tvg-name") or display).lower()),
                    "source_channel_id": scid or None,
                    # The XMLTV <channel id> and <programme channel> use this
                    # verbatim, so it is the join key for full-schedule EPG.
                    "xmltv_id": chan_id or None,
                    "playlist": kind,
                    **{dest: attrs.get(src) for src, dest in M3U_FIELDS.items()},
                }
            elif line and not line.startswith("#") and pending:
                key = pending.pop("_key")
                pending["local_url"] = line
                out[key] = pending
                pending = None
    return out


def rebase(url: str | None, public_base: str | None) -> str | None:
    """Point a URL at a different host, leaving the path and query alone."""
    if not url or not public_base:
        return url
    target, current = urlsplit(public_base.rstrip("/")), urlsplit(url)
    return urlunsplit((target.scheme or current.scheme,
                       target.netloc or current.netloc,
                       current.path, current.query, current.fragment))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def resolve_remote(play_url: str, timeout: int = 20) -> tuple[str | None, str | None, str]:
    """Resolve a /play URL to the provider's stream URL.

    Returns (remote_url, remote_host, delivery):

      redirect  provider URL a client can fetch directly
      proxied   streamed through FastChannels by design — some sources issue
                IP-bound tokens or relative playlists and must be fetched
                server-side, so there is no single remote URL. remote_host
                still names the upstream when the manifest body reveals it.
      error     nothing resolvable; the reason is in remote_host
    """
    own = urlsplit(play_url).netloc
    opener = urllib.request.build_opener(_NoRedirect)
    target = None
    try:
        with opener.open(play_url, timeout=timeout) as r:
            target = r.geturl()
            if urlsplit(target).path == urlsplit(play_url).path:
                target = None
    except HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            target = exc.headers.get("Location")
        elif exc.code == 503:
            return None, "(503 unavailable — rate-limited or upstream down)", "error"
        else:
            return None, f"(HTTP {exc.code})", "error"
    except URLError as exc:
        return None, f"({exc.reason})", "error"
    except Exception as exc:  # noqa: BLE001
        return None, f"({type(exc).__name__})", "error"

    if not target:
        return None, "(no redirect)", "error"

    from urllib.parse import urljoin
    target = urljoin(play_url, target)
    netloc = urlsplit(target).netloc
    if netloc == own:
        return None, _upstream_from_manifest(target, own, timeout) or "(proxied)", "proxied"
    return target, netloc, "redirect"


def _upstream_from_manifest(url: str, own_netloc: str, timeout: int) -> str | None:
    """Read the upstream host out of a proxied manifest body."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(65536).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    for m in re.finditer(r"https?://[^\s\"'<>]+", body):
        netloc = urlsplit(m.group(0)).netloc
        if netloc and netloc != own_netloc:
            return netloc
    return None


# ── EPG ──────────────────────────────────────────────────────────────────────
# Three server routes expose programme data, none of them interchangeable:
#
#   /api/channels/{id}/preview      now/next + how much guide data exists.
#                                   One request per channel.
#   /admin/guide/programs           the only route that selects by channel ID,
#                                   but a fixed five-hour window and times as
#                                   display strings. Batches many IDs per call.
#   /epg.xml?category=…             the full stored schedule with real
#                                   timestamps. One request for the whole
#                                   export — cheapest and most complete.
#
# `full` is the best default when you want programmes; `now` stays available
# because epg_hours is the quickest way to spot thin or stale guide data.
GUIDE_BATCH = 100


def fetch_epg_now(base: str, channel_id: int) -> dict:
    """Now/next for one channel. One request per channel."""
    try:
        data = get_json(f"{base}/api/channels/{channel_id}/preview?detect=0", timeout=30)
    except Exception:  # noqa: BLE001
        return {}
    out = {"epg_programs": data.get("epg_programs"), "epg_hours": data.get("epg_hours")}
    for slot in ("current_program", "next_program"):
        prog = data.get(slot) or {}
        prefix = "now" if slot == "current_program" else "next"
        out[f"{prefix}_title"] = prog.get("title")
        out[f"{prefix}_start"] = prog.get("start_time")
        out[f"{prefix}_end"] = prog.get("end_time")
    return out


def fetch_epg_window(base: str, channel_ids: list[int], offset: int = 0) -> dict[int, list]:
    """Five-hour window per channel, batched. Keyed by channel ID.

    Times come back as display strings in the server's timezone, alongside
    left/width percentages used by the admin guide grid — they are kept as-is
    rather than reinterpreted, since parsing "8:00 AM" back into an instant
    needs the server's timezone and would silently guess the date.
    """
    out: dict[int, list] = {}
    for i in range(0, len(channel_ids), GUIDE_BATCH):
        batch = channel_ids[i:i + GUIDE_BATCH]
        ids = ",".join(str(c) for c in batch)
        url = f"{base}/admin/guide/programs?{urlencode({'channel_ids': ids, 'offset': offset})}"
        try:
            data = get_json(url, timeout=60)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: guide window batch failed: {exc}", file=sys.stderr)
            continue
        for cid, progs in data.items():
            out[int(cid)] = progs
    return out


def _xmltv_time(value: str | None) -> str | None:
    """'20260815130000 +0000' -> '2026-08-15T13:00:00+00:00'."""
    if not value:
        return None
    from datetime import datetime
    raw = value.strip()
    try:
        dt = datetime.strptime(raw, "%Y%m%d%H%M%S %z")
    except ValueError:
        try:
            dt = datetime.strptime(raw.split()[0], "%Y%m%d%H%M%S")
        except ValueError:
            return raw
    return dt.isoformat()


def _programme_dict(p) -> dict:
    icon = p.find("icon")
    rating = p.find("rating")
    return {
        "title": p.findtext("title"),
        "sub_title": p.findtext("sub-title"),
        "desc": p.findtext("desc"),
        "category": p.findtext("category"),
        "episode_num": p.findtext("episode-num"),
        "rating": rating.findtext("value") if rating is not None else None,
        "icon": icon.get("src") if icon is not None else None,
        "start": _xmltv_time(p.get("start")),
        "stop": _xmltv_time(p.get("stop")),
    }


def fetch_epg_native(base: str, wanted: set[str], feed: str = "default") -> dict[str, list]:
    """Full schedule for Gracenote-partitioned channels, via the native feed.

    `/epg.xml` filters Gracenote channels out — see xmltv.py, where the
    generator passes `gracenote=False`. The per-feed *native* variant passes
    `None` instead, i.e. no filter, so it is the only route that returns a
    complete schedule for them.

    It cannot be narrowed by category and covers the whole feed, so the
    document is large (~68 MB for 2,218 channels here). Parsed with iterparse
    and filtered to `wanted` as it streams, so memory stays flat and only the
    requested channels are retained.
    """
    import xml.etree.ElementTree as ET

    url = f"{base}/feeds/{feed}/native/epg.xml"
    out: dict[str, list] = {}
    try:
        with urllib.request.urlopen(url, timeout=300) as r:
            for _, elem in ET.iterparse(r, events=("end",)):
                if elem.tag != "programme":
                    continue
                chan = elem.get("channel")
                if chan in wanted:
                    out.setdefault(chan, []).append(_programme_dict(elem))
                elem.clear()
    except Exception as exc:  # noqa: BLE001
        print(f"warning: native feed EPG unavailable ({exc}); "
              f"Gracenote channels will have no programmes", file=sys.stderr)
        return {}
    for progs in out.values():
        progs.sort(key=lambda x: x["start"] or "")
    return out


def fetch_epg_full(base: str, categories: list[str]) -> dict[str, list]:
    """Complete stored schedule, keyed by XMLTV channel id. One request."""
    import xml.etree.ElementTree as ET

    query = urlencode([("category", c) for c in categories])
    try:
        body = get_text(f"{base}/epg.xml?{query}", timeout=180)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not fetch /epg.xml: {exc}", file=sys.stderr)
        return {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        print(f"warning: /epg.xml did not parse: {exc}", file=sys.stderr)
        return {}

    out: dict[str, list] = {}
    for p in root.findall("programme"):
        out.setdefault(p.get("channel") or "", []).append(_programme_dict(p))
    for progs in out.values():
        progs.sort(key=lambda x: x["start"] or "")
    return out


def build_rows(args) -> list[dict]:
    base = args.base.rstrip("/")

    channels = fetch_channels(base, args.category, args.contains)
    if not args.include_disabled:
        channels = [c for c in channels if c.get("is_enabled")]
    if not channels:
        return []

    categories = sorted({c.get("category") for c in channels if c.get("category")})
    playlist = fetch_playlists(base, categories)

    rows = []
    for ch in channels:
        row = dict(ch)
        extra = playlist.get((ch.get("source_name") or "", (ch.get("name") or "").lower()))
        if extra:
            row.update(extra)
            row["local_url"] = rebase(row.get("local_url"), args.public_base)
        row.setdefault("local_url", None)
        row.setdefault("source_channel_id", None)
        row.setdefault("playlist", None)
        row.update({"remote_url": None, "remote_host": None, "delivery": None})
        rows.append(row)

    matched = sum(1 for r in rows if r["local_url"])
    print(f"{len(rows)} enabled channel(s); {matched} with a playable URL "
          f"({len(rows) - matched} not in either playlist)", file=sys.stderr)

    if not args.no_resolve:
        targets = [r for r in rows if r["local_url"]]
        eta = len(targets) * args.resolve_delay / 60
        print(f"resolving {len(targets)} remote URLs ({args.resolve_delay}s apart, ~{eta:.0f} min)…",
              file=sys.stderr)
        print("  note: this exercises the real /play path; the server may "
              "auto-disable channels it finds DRM-protected or dead", file=sys.stderr)
        for i, row in enumerate(targets, 1):
            local = rebase(row["local_url"], base)  # always probe the server we were given
            row["remote_url"], row["remote_host"], row["delivery"] = resolve_remote(local)
            if i % 25 == 0:
                print(f"  {i}/{len(targets)}", file=sys.stderr)
            time.sleep(args.resolve_delay)

    if args.epg == "now":
        print(f"fetching now/next for {len(rows)} channels "
              f"(one request each)…", file=sys.stderr)
        for row in rows:
            row.update(fetch_epg_now(base, row["id"]))

    elif args.epg == "window":
        ids = [r["id"] for r in rows]
        print(f"fetching a 5h guide window for {len(ids)} channels "
              f"in batches of {GUIDE_BATCH}…", file=sys.stderr)
        window = fetch_epg_window(base, ids, args.epg_offset)
        for row in rows:
            progs = window.get(row["id"], [])
            row["programs"] = progs
            row["epg_count"] = len(progs)

    elif args.epg == "full":
        print("fetching the full schedule from /epg.xml (one request)…", file=sys.stderr)
        schedule = fetch_epg_full(base, categories)
        for row in rows:
            progs = schedule.get(row.get("xmltv_id") or "", [])
            row["programs"] = progs
            row["epg_count"] = len(progs)
            row["epg_first"] = progs[0]["start"] if progs else None
            row["epg_last"] = progs[-1]["stop"] if progs else None
        # /epg.xml omits Gracenote-partitioned channels for the same reason
        # /m3u does: their guide is meant to come from Gracenote rather than
        # from this server's XMLTV. The per-feed native variant applies no such
        # filter, so fall back to it for exactly those channels — a second
        # request, made only when the export actually contains them.
        gap = {r["xmltv_id"] for r in rows
               if not r["epg_count"] and r.get("playlist") == "gracenote" and r.get("xmltv_id")}
        if gap and not args.no_native_epg:
            print(f"  {len(gap)} Gracenote-partitioned channel(s) are absent from "
                  f"/epg.xml by design; fetching the native feed…", file=sys.stderr)
            extra = fetch_epg_native(base, gap, args.native_feed)
            for row in rows:
                progs = extra.get(row.get("xmltv_id") or "")
                if progs:
                    row["programs"] = progs
                    row["epg_count"] = len(progs)
                    row["epg_first"] = progs[0]["start"]
                    row["epg_last"] = progs[-1]["stop"]

        matched = sum(1 for r in rows if r["epg_count"])
        print(f"  {matched}/{len(rows)} channels have programme data", file=sys.stderr)
        still = [r for r in rows if not r["epg_count"]]
        if still:
            print(f"  {len(still)} with none: "
                  + ", ".join(r["name"] for r in still[:6])
                  + (" …" if len(still) > 6 else ""), file=sys.stderr)

    return rows


def order_columns(rows: list[dict]) -> list[str]:
    present = set().union(*(r.keys() for r in rows))
    lead = [c for c in LEAD_COLUMNS if c in present]
    return lead + sorted(present - set(lead))


def write_output(rows: list[dict], args) -> None:
    if args.format == "json":
        text = json.dumps(rows, indent=2, ensure_ascii=False, default=str)
    else:
        # A channel can carry hundreds of programmes; JSON-encoding that array
        # into one cell produces a file that is valid CSV but unreadable in
        # practice — Python's own reader rejects fields over 131072 bytes, and
        # spreadsheets fare worse. Drop the array and keep the summary columns;
        # use --format json when the programmes themselves are wanted.
        if any(r.get("programs") for r in rows):
            rows = [{k: v for k, v in r.items() if k != "programs"} for r in rows]
            print("note: 'programs' omitted from CSV (nested array) — "
                  "epg_count/epg_first/epg_last are retained; "
                  "use --format json for the programmes", file=sys.stderr)
        columns = order_columns(rows)
        import io
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (json.dumps(v, ensure_ascii=False)
                                 if isinstance(v, (dict, list)) else v)
                             for k, v in row.items()})
        text = buf.getvalue()

    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as fp:
            fp.write(text)
        print(f"wrote {args.output} ({len(rows)} rows)", file=sys.stderr)
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="export_channels.py",
        description="Export enabled channels in a category, with local and remote endpoints.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Categories are matched exactly. Run --list-categories to see them;\n"
            "note that \"News\" and \"Local News\" are distinct.\n\n"
            "Resolving remote URLs is NOT read-only: it exercises the real /play\n"
            "path, so the server may auto-disable channels it finds to be DRM-\n"
            "protected or dead. Pass --no-resolve for a strictly read-only export."
        ),
    )
    ap.add_argument("category", nargs="?", help='category to export, e.g. "Local News"')
    ap.add_argument("--list-categories", action="store_true",
                    help="list every category with its channel count, then exit")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"server URL (default {DEFAULT_BASE})")
    ap.add_argument("--public-base", metavar="URL",
                    help="rewrite local_url to this host (the server emits localhost "
                         "when Public Base URL is unset)")
    ap.add_argument("--format", choices=("json", "csv"), default="json")
    ap.add_argument("-o", "--output", metavar="PATH", help="write to a file instead of stdout")
    ap.add_argument("--contains", action="store_true",
                    help='substring match — "News" also matches "Local News"')
    ap.add_argument("--include-disabled", action="store_true",
                    help="include channels with is_enabled = false")
    ap.add_argument("--no-resolve", action="store_true",
                    help="skip the remote lookup; read-only and much faster")
    ap.add_argument("--resolve-delay", type=float, default=0.4, metavar="SEC",
                    help="pause between remote lookups (default 0.4)")
    ap.add_argument("--epg", nargs="?", const="now", default=None,
                    choices=("now", "window", "full"), metavar="MODE",
                    help="attach programme data. now = now/next + guide depth "
                         "(one request per channel); window = five-hour schedule "
                         "by channel id, batched; full = complete stored schedule "
                         "from /epg.xml in a single request. Bare --epg means now.")
    ap.add_argument("--no-native-epg", action="store_true",
                    help="in --epg full, skip the native-feed fallback that recovers "
                         "Gracenote-partitioned channels (avoids a large second fetch)")
    ap.add_argument("--native-feed", default="default", metavar="SLUG",
                    help="feed slug used for the native-feed fallback (default: default)")
    ap.add_argument("--epg-offset", type=int, default=0, metavar="HOURS",
                    help="shift the --epg window, clamped by the server to +/-48 (window mode only)")
    args = ap.parse_args()
    args.base = args.base.rstrip("/")

    try:
        if args.list_categories:
            cats = list_categories(args.base)
            width = max((len(n) for n, _ in cats), default=10)
            print(f"{'CATEGORY'.ljust(width)}  CHANNELS")
            for name, count in cats:
                print(f"{name.ljust(width)}  {count:>8}")
            print(f"\n{len(cats)} categories. Counts are active channels, "
                  f"before the enabled filter this tool applies.", file=sys.stderr)
            return 0

        if not args.category:
            ap.error("a category is required (or use --list-categories)")

        rows = build_rows(args)
    except (URLError, HTTPError) as exc:
        print(f"error: could not reach {args.base}: {exc}", file=sys.stderr)
        return 1

    if not rows:
        known = ", ".join(n for n, _ in list_categories(args.base))
        print(f"no enabled channels in category {args.category!r}.\n"
              f"known categories: {known}", file=sys.stderr)
        return 1

    write_output(rows, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
