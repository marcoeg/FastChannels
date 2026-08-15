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
                source, _, scid = (attrs.get("channel-id") or "").partition(".")
                pending = {
                    "_key": (source, (attrs.get("tvg-name") or display).lower()),
                    "source_channel_id": scid or None,
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


def fetch_epg(base: str, channel_id: int) -> dict:
    """Now/next programme for one channel. One request per channel."""
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

    if args.epg:
        print(f"fetching EPG for {len(rows)} channels…", file=sys.stderr)
        for row in rows:
            row.update(fetch_epg(base, row["id"]))

    return rows


def order_columns(rows: list[dict]) -> list[str]:
    present = set().union(*(r.keys() for r in rows))
    lead = [c for c in LEAD_COLUMNS if c in present]
    return lead + sorted(present - set(lead))


def write_output(rows: list[dict], args) -> None:
    if args.format == "json":
        text = json.dumps(rows, indent=2, ensure_ascii=False, default=str)
    else:
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
    ap.add_argument("--epg", action="store_true",
                    help="include now/next programme (one extra request per channel)")
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
