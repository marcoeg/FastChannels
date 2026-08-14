#!/usr/bin/env python3
"""
Derive the originating market and station for FastChannels news channels.

FastChannels stores no origin field. For local stations the origin exists only
as prose in `name` and `description`, with a call sign sometimes reachable via
`guide_key`. This script recovers it with per-source rules and reports honest
per-rule confidence rather than a single blended number.

Two different things get called "origin", so they are handled separately:

  Local News  -> a US market: call sign + city + state
  News        -> a national/international network, which has no market

The app's own `category` makes that split, so it is used as the classifier
rather than re-deriving it.

Usage:
    python3 dev/news_origins.py                     # table + coverage report
    python3 dev/news_origins.py --json out.json
    python3 dev/news_origins.py --csv  out.csv
    python3 dev/news_origins.py --unresolved        # only the ones that failed
    python3 dev/news_origins.py --base http://host:5523

Read-only: issues one GET against /api/channels and never writes to the server.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from markets import (  # noqa: E402
    CALLSIGN_MARKETS, CITY_MARKETS, REGION_MARKETS, STATE_MARKETS, MACRO_REGIONS,
)

DEFAULT_BASE = "http://127.0.0.1:5523"

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}
NAME_TO_ABBR = {v.lower(): k for k, v in STATES.items()}

# A US broadcast call sign: K or W plus 2-3 letters, optionally -TV/-DT.
# Deliberately anchored on word boundaries; network initialisms (ABC, CBS,
# NBC, FOX) cannot match because none begin with K or W.
CALLSIGN = re.compile(r"\b([KW][A-Z]{2,3})(?:-(?:TV|DT|SD|HD))?\b")

# Words that look like a call sign but are not one, seen in real channel names.
CALLSIGN_STOPLIST = {"WNBA", "WWE", "WORLD", "WEST", "WATCH", "WNBC"}  # WNBC is real; kept for review
CALLSIGN_STOPLIST -= {"WNBC"}

# vizio stores "93" + call sign in guide_key, e.g. "93KWCH".
VIZIO_GUIDE_KEY = re.compile(r"^\d+([KW][A-Z]{2,3})$")

# stirr descriptions are templated:
#   "Live Asheville, NC news, weather & sports from WLOS."
STIRR_DESC = re.compile(
    r"Live\s+(?P<city>[^,]+?),?\s+(?P<st>[A-Z]{2})\s+news.*?from\s+(?P<cs>[KW][A-Z]{2,3})",
    re.I | re.S,
)

# stirr names are templated too: "ABC 13 WLOS Asheville NC"
NAME_CS_CITY_ST = re.compile(
    r"\b(?P<cs>[KW][A-Z]{2,3})\s+(?P<city>[A-Z][\w.\'\- ]+?)\s+(?P<st>[A-Z]{2})\s*$"
)

# "…coverage for Wichita, Kansas viewing area."
DESC_CITY_STATE = re.compile(
    r"\b(?:for|in|serving)\s+(?P<city>[A-Z][\w.\'\-]*(?:\s+[A-Z][\w.\'\-]*){0,2}),\s+"
    r"(?P<state>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
)

# tcl/vizio descriptions: "KMTV 3 News Now Omaha gives you…"
DESC_CS_LEADING_CITY = re.compile(
    r"^(?P<cs>[KW][A-Z]{2,3})\b.*?\b(?P<city>[A-Z][\w.\'\-]*(?:\s+[A-Z][\w.\'\-]*)?)\s+"
    r"(?:gives|brings|is your|delivers|provides)"
)

# "Channel 13 Las Vegas News KTNV gives you…"
DESC_CITY_BEFORE_CS = re.compile(
    r"\b(?P<city>[A-Z][\w.\'\-]*(?:\s+[A-Z][\w.\'\-]*)?)\s+News\s+(?P<cs>[KW][A-Z]{2,3})\b"
)

# Words that are never part of a place name. Directionals (north, south,
# central, …) are deliberately absent: they are legitimate city prefixes
# ("South Bend", "West Palm Beach") and trimming them produced the "Bend, IN"
# and "Palm Beach" bugs in the first pass.
NOISE_CITY_TOKENS = {
    "news", "live", "now", "the", "your", "local", "breaking", "weather",
    "channel", "action", "first", "alert", "today", "tonight", "stream",
    "watch", "free", "coverage", "source", "america", "national", "world",
    "global", "daily",
}

# Results that are grammatically city-shaped but meaningless on their own.
# ("city" cannot go in NOISE_CITY_TOKENS above — trailing-token trimming would
# turn "Kansas City" into "Kansas".)
REJECT_CITY = {"city", "county", "area", "region", "state", "valley", "coast"}


@dataclass
class Origin:
    channel_id: int
    name: str
    source: str
    category: str
    scope: str                     # "local" | "national"
    call_sign: str | None = None
    city: str | None = None
    state: str | None = None
    confidence: str = "none"       # high | medium | low | none
    granularity: str = "none"      # market | state | macro-region | none
    rule: str | None = None
    evidence: str | None = None
    source_channel_id: str | None = None
    play_url: str | None = None    # FastChannels proxy URL (--urls)
    cdn_url: str | None = None     # upstream stream URL (--resolve)
    cdn_host: str | None = None    # true upstream host, even when proxied
    delivery: str | None = None    # redirect | proxied | error

    @property
    def market(self) -> str | None:
        if self.city and self.state:
            return f"{self.city}, {self.state}"
        return self.city or None


def fetch(base: str) -> list[dict]:
    url = f"{base.rstrip('/')}/api/channels?category=News&per_page=1000"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)["channels"]


# ── Playable URLs ────────────────────────────────────────────────────────────
# The control API cannot supply these: Channel.to_dict() omits
# source_channel_id, so /api/channels alone gives no way to build a /play URL.
# The M3U outputs carry both, so they are the source of truth here.
#
# Two playlists are required. Channels holding a Gracenote ID are partitioned
# into /m3u/gracenote and are absent from /m3u — fetching only the latter
# silently loses them. The category filter on these endpoints is an exact
# match (unlike /api/channels, which does a substring LIKE), so both category
# values must be passed explicitly.
M3U_PATHS = ("/m3u", "/m3u/gracenote")
NEWS_CATEGORIES = ("News", "Local News")

_EXTINF_ATTR = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')


def fetch_playlist_urls(base: str) -> dict[tuple[str, str], tuple[str, str]]:
    """Return {(source, lowercased name): (source_channel_id, play_url)}."""
    from urllib.parse import urlencode

    out: dict[tuple[str, str], tuple[str, str]] = {}
    query = urlencode([("category", c) for c in NEWS_CATEGORIES])
    for path in M3U_PATHS:
        url = f"{base.rstrip('/')}{path}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception as exc:
            print(f"warning: could not fetch {path}: {exc}", file=sys.stderr)
            continue

        pending: tuple[str, str, str] | None = None
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("#EXTINF"):
                attrs = dict(_EXTINF_ATTR.findall(line))
                display = line.rsplit(",", 1)[-1].strip()
                chan_id = attrs.get("channel-id", "")
                src, _, scid = chan_id.partition(".")
                name = attrs.get("tvg-name") or display
                pending = (src, name, scid)
            elif line and not line.startswith("#") and pending:
                src, name, scid = pending
                out[(src, name.lower())] = (scid, line)
                pending = None
    return out


def rebase(url: str, public_base: str | None) -> str:
    """Swap the scheme/host/port of *url* for *public_base*.

    FastChannels emits localhost URLs whenever Public Base URL is unset and it
    is running in Docker — see detected_base_url() in app/url.py, which
    deliberately declines to guess, because the container's own LAN detection
    returns an unreachable bridge address. Those URLs only work on the host
    itself, so rewrite them for anything off-box.
    """
    if not public_base or not url:
        return url
    from urllib.parse import urlsplit, urlunsplit

    target, current = urlsplit(public_base.rstrip("/")), urlsplit(url)
    return urlunsplit((
        target.scheme or current.scheme,
        target.netloc or current.netloc,
        current.path, current.query, current.fragment,
    ))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Capture the Location header instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _external_host_from_manifest(url: str, own_netloc: str, timeout: int = 20) -> str | None:
    """Pull the first non-local absolute host out of a proxied manifest.

    Proxied sources never redirect a client to the provider, but the manifest
    body they serve usually still names it (Samsung absolutises its playlist
    references to the origin CDN). That host is the real answer to "where does
    this come from", even though it is not a URL a client can use directly.
    """
    from urllib.parse import urlsplit
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(65536).decode("utf-8", "replace")
    except Exception:
        return None
    for m in re.finditer(r"https?://[^\s\"'<>]+", body):
        netloc = urlsplit(m.group(0)).netloc
        if netloc and netloc != own_netloc:
            return netloc
    return None


def resolve_cdn(play_url: str, timeout: int = 20) -> tuple[str | None, str | None, str]:
    """Resolve one /play URL to the stream origin.

    Returns (cdn_url, cdn_host, delivery) where delivery is:

      redirect  the provider's own URL — a client can fetch it directly
      proxied   the stream is served through FastChannels and there is no
                single remote URL. cdn_host still names the upstream when it
                can be read out of the manifest.
      error     nothing resolvable

    Sources land in `proxied` by design, not by failure: STIRR issues IP-bound
    session tokens and Samsung emits relative playlist references, so both must
    be fetched server-side.
    """
    from urllib.parse import urlsplit
    from urllib.error import HTTPError, URLError

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
        upstream = _external_host_from_manifest(target, own, timeout)
        return None, upstream or "(proxied — upstream not exposed)", "proxied"

    return target, netloc, "redirect"


def clean_city(raw: str | None) -> str | None:
    if not raw:
        return None
    city = re.sub(r"\s+", " ", raw).strip(" .,-")
    if not city:
        return None
    tokens = city.split()
    # Trim leading noise words ("Live Asheville" -> "Asheville").
    while tokens and tokens[0].lower() in NOISE_CITY_TOKENS:
        tokens.pop(0)
    while tokens and tokens[-1].lower() in NOISE_CITY_TOKENS:
        tokens.pop()
    if not tokens:
        return None
    city = " ".join(tokens)
    # Reject things that are obviously not place names.
    if len(city) < 3 or city.lower() in NOISE_CITY_TOKENS or city.lower() in REJECT_CITY:
        return None
    return city


def _longest_first(table: dict) -> list[str]:
    return sorted(table, key=len, reverse=True)


_CITY_KEYS = _longest_first(CITY_MARKETS)
_REGION_KEYS = _longest_first(REGION_MARKETS)
_STATE_KEYS = _longest_first(STATE_MARKETS)
_MACRO_KEYS = _longest_first(MACRO_REGIONS)


def lookup_place(text: str) -> tuple[str, str, str] | None:
    """Match a place name in *text*. Returns (city, state, granularity).

    Regions and cities are tried before states so "Kansas City" cannot be
    swallowed by "Kansas", and "South Florida" beats "Florida".
    """
    hay = (text or "").lower()

    for key in _REGION_KEYS:
        if re.search(rf"\b{re.escape(key)}\b", hay):
            city, st = REGION_MARKETS[key]
            return city, st, "market"
    for key in _CITY_KEYS:
        if re.search(rf"\b{re.escape(key)}\b", hay):
            city, st = CITY_MARKETS[key]
            return city, st, "market"
    for key in _MACRO_KEYS:
        if re.search(rf"\b{re.escape(key)}\b", hay):
            return MACRO_REGIONS[key], "", "macro-region"
    for key in _STATE_KEYS:
        if re.search(rf"\b{re.escape(key)}\b", hay):
            return "Statewide", STATE_MARKETS[key], "state"
    return None


def norm_state(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().strip(".,")
    if raw.upper() in STATES:
        return raw.upper()
    return NAME_TO_ABBR.get(raw.lower())


def find_callsign(*texts: str | None) -> str | None:
    for t in texts:
        if not t:
            continue
        for m in CALLSIGN.finditer(t):
            cs = m.group(1)
            if cs not in CALLSIGN_STOPLIST:
                return cs
    # Case-insensitive second pass, but only against known call signs — some
    # providers title-case them ("Wapa+" where another lists "WAPA+"). Safe
    # because it is a lookup in a closed table, not a regex guess: matching
    # loosely on shape would catch ordinary words like "west" and "kids".
    for t in texts:
        if not t:
            continue
        for token in re.findall(r"\b[A-Za-z]{3,4}\b", t):
            up = token.upper()
            if up in CALLSIGN_MARKETS and up not in CALLSIGN_STOPLIST:
                return up
    return None


def extract(ch: dict) -> Origin:
    name = ch.get("name") or ""
    desc = ch.get("description") or ""
    gkey = ch.get("guide_key") or ""
    scope = "local" if (ch.get("category") or "") == "Local News" else "national"

    o = Origin(
        channel_id=ch["id"],
        name=name,
        source=ch.get("source_name") or "",
        category=ch.get("category") or "",
        scope=scope,
    )
    if scope == "national":
        o.confidence = "n/a"
        o.rule = "category=News (no local market)"
        return o

    # Call sign first — independent of the market rules below.
    gk = VIZIO_GUIDE_KEY.match(gkey)
    if gk:
        o.call_sign, cs_rule = gk.group(1), "guide_key"
    else:
        o.call_sign, cs_rule = find_callsign(name, desc), "text"

    # Rule 0 — the call-sign table. Highest precision available, and
    # authoritative over provider metadata, which is sometimes wrong about
    # the market (see markets.py).
    if o.call_sign and o.call_sign in CALLSIGN_MARKETS:
        o.city, o.state = CALLSIGN_MARKETS[o.call_sign]
        o.granularity = "market"
        o.confidence = "high"
        o.rule = f"callsign-table:{cs_rule}"
        o.evidence = o.call_sign
        return o

    # Rule A — stirr's templated description. Highest signal available.
    m = STIRR_DESC.search(desc)
    if m:
        city, st = clean_city(m.group("city")), norm_state(m.group("st"))
        if city and st:
            o.city, o.state = city, st
            o.call_sign = o.call_sign or m.group("cs")
            o.granularity = "market"
            o.confidence, o.rule, o.evidence = "high", "desc:stirr-template", desc[:90]
            return o

    # Rule B — "<CALLSIGN> <City> <ST>" at the end of the name.
    m = NAME_CS_CITY_ST.search(name)
    if m:
        city, st = clean_city(m.group("city")), norm_state(m.group("st"))
        if city and st:
            o.city, o.state = city, st
            o.call_sign = o.call_sign or m.group("cs")
            o.granularity = "market"
            o.confidence, o.rule, o.evidence = "high", "name:callsign-city-state", name
            return o

    # Rule C — "…for Wichita, Kansas viewing area."
    m = DESC_CITY_STATE.search(desc)
    if m:
        city, st = clean_city(m.group("city")), norm_state(m.group("state"))
        if city and st:
            o.city, o.state = city, st
            o.granularity = "market"
            o.confidence, o.rule, o.evidence = "high", "desc:city-state", m.group(0)[:90]
            return o

    # Rule D — "KMTV 3 News Now Omaha gives you…"
    m = DESC_CS_LEADING_CITY.search(desc)
    if m:
        city = clean_city(m.group("city"))
        if city:
            o.city = city
            o.call_sign = o.call_sign or m.group("cs")
            o.granularity = "market"
            o.confidence, o.rule, o.evidence = "medium", "desc:callsign-then-city", m.group(0)[:90]
            return o

    # Rule E — "Channel 13 Las Vegas News KTNV gives you…"
    m = DESC_CITY_BEFORE_CS.search(desc)
    if m:
        city = clean_city(m.group("city"))
        if city:
            o.city = city
            o.call_sign = o.call_sign or m.group("cs")
            o.granularity = "market"
            o.confidence, o.rule, o.evidence = "medium", "desc:city-before-callsign", m.group(0)[:90]
            return o

    # Rule F — gazetteer match on the channel name. This is what resolves the
    # bulk of the lineup: names like "FOX LOCAL Atlanta", "CBS News Baltimore"
    # and "6ABC Philadelphia" carry the market and nothing else does.
    hit = lookup_place(name)
    if hit:
        o.city, o.state, o.granularity = hit
        o.confidence = "medium" if o.granularity == "market" else "low"
        o.rule = f"gazetteer:name:{o.granularity}"
        o.evidence = name
        return o

    # Rule G — same lookup against the description, which is noisier: a
    # national story mentioning a city would false-positive here, so it only
    # runs once the name has produced nothing.
    hit = lookup_place(desc)
    if hit:
        o.city, o.state, o.granularity = hit
        o.confidence = "low"
        o.rule = f"gazetteer:desc:{o.granularity}"
        o.evidence = desc[:90]
        return o

    if o.call_sign:
        o.confidence, o.rule = "low", f"callsign-unknown:{cs_rule}"
    return o


def gazetteer_pass(results: list[Origin]) -> int:
    """Second pass: match unresolved names against cities already recovered
    with high confidence. This is what rescues sources with bare names and
    empty descriptions, e.g. roku's "6ABC Philadelphia" — Philadelphia is
    known because stirr or vizio named it explicitly.
    """
    known: dict[str, tuple[str, str | None]] = {}
    for o in results:
        if o.confidence == "high" and o.city:
            known.setdefault(o.city.lower(), (o.city, o.state))

    if not known:
        return 0

    # Longest first so "New York" wins over "York".
    ordered = sorted(known, key=len, reverse=True)
    rescued = 0
    for o in results:
        if o.scope != "local" or o.city:
            continue
        hay = f"{o.name} {o.name}"  # name only; descriptions are noisier
        for key in ordered:
            if re.search(rf"\b{re.escape(key)}\b", hay, re.I):
                o.city, o.state = known[key]
                o.granularity = "market"
                o.confidence = "medium"
                o.rule = "gazetteer:bootstrap"
                o.evidence = o.name
                rescued += 1
                break
    return rescued


def report(results: list[Origin], rescued: int) -> None:
    local = [o for o in results if o.scope == "local"]
    national = [o for o in results if o.scope == "national"]
    resolved = [o for o in local if o.city]

    print(f"\n{'=' * 78}")
    print(f"{len(results)} news channels: {len(local)} local, {len(national)} national/international")
    print("=" * 78)

    pct = 100 * len(resolved) / len(local) if local else 0
    print(f"\nMarket resolved for {len(resolved)}/{len(local)} local channels ({pct:.0f}%)")
    cs = [o for o in local if o.call_sign]
    print(f"Call sign resolved for {len(cs)}/{len(local)} ({100 * len(cs) / len(local):.0f}%)")
    if rescued:
        print(f"  (gazetteer second pass rescued {rescued})")

    print("\n--- confidence ---")
    for conf, n in Counter(o.confidence for o in local).most_common():
        print(f"  {n:5}  {conf}")

    print("\n--- rule that fired ---")
    for rule, n in Counter(o.rule or "(none)" for o in local).most_common():
        print(f"  {n:5}  {rule}")

    print("\n--- coverage by source (local channels only) ---")
    by_src: dict[str, list[Origin]] = defaultdict(list)
    for o in local:
        by_src[o.source].append(o)
    print(f"  {'source':<16} {'resolved':>9} {'total':>6}   {'%':>4}")
    for src, items in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
        got = sum(1 for o in items if o.city)
        print(f"  {src:<16} {got:>9} {len(items):>6}   {100 * got / len(items):>3.0f}%")

    if any(o.play_url for o in results):
        with_url = [o for o in results if o.play_url]
        print(f"\n--- playable URLs: {len(with_url)}/{len(results)} ---")
        resolved = [o for o in with_url if o.delivery]
        if resolved:
            kinds = Counter(o.delivery for o in resolved)
            print(f"\n--- stream delivery ({len(resolved)} probed) ---")
            print(f"  {kinds.get('redirect', 0):4}  redirect  — direct provider URL")
            print(f"  {kinds.get('proxied', 0):4}  proxied   — served through FastChannels, no direct URL")
            print(f"  {kinds.get('error', 0):4}  error")
            hosts = Counter(o.cdn_host for o in resolved
                            if o.cdn_host and not o.cdn_host.startswith("("))
            print(f"\n--- upstream hosts ({len(hosts)} distinct) ---")
            for host, n in hosts.most_common(12):
                print(f"  {n:4}  {host}")

    print("\n--- top markets ---")
    for market, n in Counter(o.market for o in resolved if o.market).most_common(15):
        print(f"  {n:3}  {market}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"server base URL (default {DEFAULT_BASE})")
    ap.add_argument("--json", metavar="PATH", help="write full results as JSON")
    ap.add_argument("--csv", metavar="PATH", help="write full results as CSV")
    ap.add_argument("--unresolved", action="store_true", help="list local channels with no market")
    ap.add_argument("--table", action="store_true", help="print every resolved row")
    ap.add_argument("--urls", action="store_true",
                    help="attach the playable /play URL for each channel")
    ap.add_argument("--public-base", metavar="URL",
                    help="rewrite emitted URLs to this host, e.g. http://192.168.50.17:5523 "
                         "(FastChannels emits localhost when Public Base URL is unset)")
    ap.add_argument("--resolve", action="store_true",
                    help="also follow each /play URL to its upstream CDN URL. SLOW, makes one "
                         "live request per channel, and can trip provider rate limits")
    ap.add_argument("--resolve-delay", type=float, default=0.4, metavar="SEC",
                    help="pause between --resolve requests (default 0.4)")
    args = ap.parse_args()

    try:
        channels = fetch(args.base)
    except Exception as exc:
        print(f"error: could not reach {args.base}: {exc}", file=sys.stderr)
        return 1

    results = [extract(c) for c in channels]
    rescued = gazetteer_pass(results)

    if args.urls or args.resolve:
        playlist = fetch_playlist_urls(args.base)
        for o in results:
            hit = playlist.get((o.source, o.name.lower()))
            if hit:
                o.source_channel_id, o.play_url = hit[0], rebase(hit[1], args.public_base)
        got = sum(1 for o in results if o.play_url)
        print(f"attached {got}/{len(results)} playable URLs "
              f"({len(results) - got} not in either playlist — disabled, or no stream)")

    if args.resolve:
        targets = [o for o in results if o.play_url]
        print(f"resolving {len(targets)} upstream URLs "
              f"({args.resolve_delay}s apart, ~{len(targets) * args.resolve_delay / 60:.0f} min)...",
              file=sys.stderr)
        from urllib.parse import urlsplit
        for i, o in enumerate(targets, 1):
            # Resolve against the local server regardless of --public-base:
            # the rewritten host may not be reachable from where this runs.
            local = rebase(o.play_url, args.base)
            o.cdn_url, o.cdn_host, o.delivery = resolve_cdn(local)
            if i % 25 == 0:
                print(f"  {i}/{len(targets)}", file=sys.stderr)
            time.sleep(args.resolve_delay)
    results.sort(key=lambda o: (o.scope != "local", o.source, o.name.lower()))

    if args.table:
        print(f"\n{'call':<6} {'market':<28} {'source':<12} {'conf':<7} name")
        print("-" * 100)
        for o in results:
            if o.scope != "local" or not o.city:
                continue
            print(f"{o.call_sign or '-':<6} {(o.market or '-'):<28} {o.source:<12} "
                  f"{o.confidence:<7} {o.name}")

    if args.unresolved:
        miss = [o for o in results if o.scope == "local" and not o.city]
        print(f"\n--- {len(miss)} local channels with no recoverable market ---")
        for o in miss:
            print(f"  [{o.source:<12}] {o.name}  (call sign: {o.call_sign or '-'})")

    report(results, rescued)

    rows = [asdict(o) | {"market": o.market} for o in results]
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fp:
            json.dump(rows, fp, indent=2, ensure_ascii=False)
        print(f"\nwrote {args.json}")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fp:
            w = csv.DictWriter(fp, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
