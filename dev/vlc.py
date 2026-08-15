#!/usr/bin/env python3
"""
Hand a channel to VLC — from FastChannels directly, or through Tvheadend.

Two servers can serve the same channel, and which one you want depends on why
you are watching.

**FastChannels direct** (`--via fastchannels`, the default) is the simpler
path. FastChannels has no authentication, so its playlists need no token at
all, and it publishes 206 Gracenote-partitioned channels that Tvheadend does
not carry — 2,220 against 2,014. One less hop, and it works whether or not
Tvheadend is running.

**Through Tvheadend** (`--via tvheadend`) is worth it when a stream is flaky:
Tvheadend owns the subscription, re-muxes everything to MPEG-TS, and is the
only path that can also record. It costs an auth token (see below) and misses
the Gracenote set.

Neither path helps with DRM. Channels needing the PrismCast/Widevine bridge
will not play in VLC from either server.

Why Tvheadend needs a token
---------------------------
Tvheadend's `digest` setting is 1 — Digest only — and VLC leads with Basic::

    no auth -> 401     basic -> 401  <- VLC     digest -> 200

`http://user:pass@host/…` is sent as Basic too, so it fails identically. The
way through is Tvheadend's *persistent authentication code*, a per-user token
in the password table passed as `?auth=CODE`: no password on the wire, no
change to the server's auth posture, and revocable on its own. This script
fetches it from the API at run time rather than storing it.

The URL must be `/playlist/auth/…`, not `/playlist/…`. That path segment
decides how the entry URLs *inside* the playlist are authenticated
(webui.c:92-97). Plain `/playlist/…` yields bare entries, so VLC opens the
list and then 401s on every channel in it.

Two FastChannels sharp edges this handles for you
-------------------------------------------------
*Both playlists, always.* Channels carrying a Gracenote ID live in
`/m3u/gracenote` and are **absent** from `/m3u`. Asking for one silently loses
the other, so `--via fastchannels` fetches both and merges them.

*Categories match exactly.* `/m3u?category=News` returns `News` only — not
`Local News`, which is a separate category. (`/api/channels` would substring
match; the playlist endpoints do not.) `--list-categories` shows the real
names with counts.

One window per channel
----------------------
VLC has no "open in a new window" action: picking an item in its playlist
replaces whatever that instance is playing, so watching two channels at once
means two VLC *processes*. `--play` therefore passes `--no-one-instance` and
each invocation gets its own viewer. Run it again per channel to stack them
up. `--reuse` asks for the old behaviour — hand the URL to a running VLC —
which only does anything if the `one-instance` preference is enabled.

Recordings
----------
Tvheadend's auth code cannot play them, by design — access.c:885 clamps it to
``ACCESS_ADVANCED_STREAMING|ACCESS_STREAMING`` while `/dvrfile/…` requires
``ACCESS_RECORDER``, so it 401s regardless of the account's permissions.
`--recordings` lists the files on disk instead; VLC opens those with no auth.

Usage:
    ./vlc.py --list-categories               # categories with channel counts
    ./vlc.py --channel "Al Jazeera" --play    # watch one channel
    ./vlc.py --category News --play           # the whole category
    ./vlc.py --category News --write ~/news.m3u
    ./vlc.py --source pluto --play            # one provider
    ./vlc.py --via tvheadend --category "Local News" --play
    ./vlc.py --recordings                     # DVR files on disk

    # each --play opens its own window, so these stack rather than replace
    ./vlc.py --channel "Al Jazeera" --play
    ./vlc.py --channel "Euronews" --play
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
TVH_DIR = HERE / "tvheadend"
SUPERUSER = TVH_DIR / "config" / "superuser"
# Host side of the /recordings bind mount. Must match docker-compose.yml.
RECORDINGS = pathlib.Path.home() / "news" / "tvheadend" / "recordings"

FASTCHANNELS = "http://127.0.0.1:5523"
TVH = "http://127.0.0.1:9981"

EXTINF_HEAD = re.compile(r"^#EXTINF:(-?[\d.]+)")
EXTINF_ATTR = re.compile(r'([A-Za-z0-9_.-]+)="([^"]*)"')

# Attributes a player can use. Everything else is dropped — in particular
# tvg-description and tvc-guide-description, which are why clean_extinf()
# exists at all.
KEEP_ATTRS = ("tvg-id", "tvg-name", "tvg-chno", "tvg-logo", "group-title")


# --------------------------------------------------------------------------
# EXTINF rewriting
#
# FastChannels emits full channel descriptions as M3U attributes, and those
# descriptions contain commas:
#
#   #EXTINF:-1 … tvg-description="…challenging outdoor jobs, WeatherSpy is
#   reinventing the way we watch weather." …,WeatherSpy
#
# VLC splits the title on a comma inside the quoted attributes rather than on
# the separator comma, so the playlist shows a sentence fragment instead of the
# channel name. Channels whose description happens to have no comma render
# correctly, which is why the breakage looks arbitrary.
#
# Rather than depend on any player parsing quoted attributes correctly, emit
# EXTINF lines with no comma anywhere before the separator.
# --------------------------------------------------------------------------

def _attr_safe(value: str) -> str:
    """Quotes and commas in an attribute value break EXTINF title parsing."""
    return value.replace('"', "").replace(",", "")


def extinf_name(line: str) -> str:
    """The channel name: everything after the *separator* comma.

    The separator is the comma following the last quoted attribute, not the
    first comma on the line — attribute values carry commas of their own.
    """
    tail = line[line.rindex('"') + 1:] if '"' in line else line
    if "," in tail:
        return tail.split(",", 1)[1].strip()
    # A quote inside the name itself; fall back to the last comma.
    return line.rsplit(",", 1)[-1].strip()


def clean_extinf(line: str) -> str:
    head = EXTINF_HEAD.match(line)
    if not head:
        return line
    attrs = dict(EXTINF_ATTR.findall(line))
    kept = " ".join(
        '{}="{}"'.format(k, _attr_safe(attrs[k])) for k in KEEP_ATTRS if attrs.get(k)
    )
    return "#EXTINF:{}{},{}".format(
        head.group(1), " " + kept if kept else "", extinf_name(line)
    )


def clean_playlist(body: str) -> str:
    return "\n".join(
        clean_extinf(ln) if ln.startswith("#EXTINF") else ln
        for ln in body.splitlines()
    ) + "\n"


# --------------------------------------------------------------------------
# FastChannels — no authentication, so plain urlopen throughout
# --------------------------------------------------------------------------

def fc_get(url: str, timeout: int = 120) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 503 + Retry-After: 15 means the background worker is still building
        # the artifact; that is ordinary right after a restart.
        if exc.code == 503:
            sys.exit(f"503 from {url.split('?')[0]} — artifact still warming, retry shortly")
        sys.exit(f"{exc.code} fetching {url.split('?')[0]}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach FastChannels at {FASTCHANNELS}: {exc.reason}")


def fc_categories() -> list[tuple[str, int]]:
    stats = json.loads(fc_get(f"{FASTCHANNELS}/api/stats", timeout=30))
    return [(c["name"], c["count"]) for c in stats.get("categories", [])]


def fc_query(category: str | None, source: str | None, search: str | None) -> str:
    params: list[tuple[str, str]] = []
    if category:
        params.append(("category", category))
    if source:
        params.append(("source", source))
    if search:
        params.append(("search", search))
    return urllib.parse.urlencode(params)


def fc_playlist(category: str | None, source: str | None, search: str | None) -> str:
    """Merge /m3u and /m3u/gracenote.

    They are disjoint sets, not a superset/subset pair — 2,014 and 206 here —
    so taking only the first silently drops every channel with a Gracenote
    station ID.
    """
    query = fc_query(category, source, search)
    suffix = f"?{query}" if query else ""
    merged = ["#EXTM3U"]
    for path in ("/m3u", "/m3u/gracenote"):
        body = fc_get(f"{FASTCHANNELS}{path}{suffix}")
        for line in body.splitlines():
            if line.startswith("#EXTM3U"):
                continue
            if line.strip():
                merged.append(line)
    return "\n".join(merged) + "\n"


def fc_filter_channel(playlist: str, wanted: str) -> str:
    """Narrow a playlist to one channel, by number or by name.

    Done client-side because the M3U endpoints have no channel selector —
    `search` is a substring match on the name and there is no number filter at
    all. Entries are #EXTINF/URL pairs, so they are taken two lines at a time.
    """
    lines = playlist.splitlines()
    by_number = wanted.isdigit()
    needle = wanted.strip().lower()
    out = ["#EXTM3U"]
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF") or i + 1 >= len(lines):
            continue
        if by_number:
            hit = 'tvg-chno="{}"'.format(wanted) in line
        else:
            hit = needle in extinf_name(line).lower()
        if hit:
            out.extend([line, lines[i + 1]])
    if len(out) == 1:
        sys.exit(f"no channel matched {wanted!r}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Tvheadend — digest for the API, an auth code for the player
# --------------------------------------------------------------------------

class Tvheadend:
    """Minimal digest client — same shape as setup.py's."""

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
        try:
            with self.opener.open(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            sys.exit(f"cannot reach Tvheadend at {self.base}: {exc}")
        return json.loads(body) if body.strip() else {}


def tvh_client() -> Tvheadend:
    if not SUPERUSER.exists():
        sys.exit(f"no credentials: {SUPERUSER} is missing (run tvheadend/setup.py first)")
    doc = json.loads(SUPERUSER.read_text())
    return Tvheadend(TVH, doc["username"], doc["password"])


def tvh_auth_code(tvh: Tvheadend) -> str:
    """The persistent auth code, fetched live rather than stored here.

    It is a bearer credential for streaming: anyone holding it can watch. That
    is why it stays out of the repo, and why --write chmods its output.
    """
    for entry in tvh.call("passwd/entry/grid").get("entries", []):
        if entry.get("enabled") and entry.get("authcode"):
            if "enable" in (entry.get("auth") or []):
                return entry["authcode"]
    sys.exit(
        "no persistent auth code is enabled.\n"
        "  Configuration > Users > Passwords > (user) > "
        "set 'Persistent authentication' to Enable, then re-run."
    )


def tvh_tags(tvh: Tvheadend) -> list[str]:
    grid = tvh.call("channeltag/grid", limit="999")
    return sorted(e["name"] for e in grid.get("entries", []) if e.get("name"))


def tvh_url(code: str, category: str | None, channel: str | None) -> str:
    """A /playlist/auth/… URL, so entries carry the code too.

    A single channel is served as a playlist rather than a bare /stream/ URL:
    it costs nothing and keeps the channel name in VLC's title bar.
    """
    if channel is not None:
        selector = "channelnumber" if channel.isdigit() else "channelname"
        path = f"/playlist/auth/{selector}/{urllib.parse.quote(channel)}"
    elif category is not None:
        path = f"/playlist/auth/tagname/{urllib.parse.quote(category)}"
    else:
        path = "/playlist/auth/channels"
    return f"{TVH}{path}?auth={code}"


def tvh_fetch(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        sys.exit(f"{exc.code} fetching {url.split('?')[0]} — check the name (--list-categories)")


# --------------------------------------------------------------------------

def list_recordings() -> int:
    """Recordings play from disk; see the module docstring for why not HTTP."""
    files = sorted(p for p in RECORDINGS.glob("*") if p.is_file())
    if not files:
        print(f"no recordings in {RECORDINGS}")
        return 0
    print(f"{len(files)} recording(s) in {RECORDINGS} — open these directly in VLC:\n")
    for p in files:
        print(f"  {p.stat().st_size / 1e6:8.1f} MB  {p}")
    return 0


def launch(target: str, reuse: bool = False) -> None:
    """Start VLC on `target`, in its own window by default.

    VLC has no "open in a new window" action: picking an item in its playlist
    replaces whatever that instance is playing. A second viewer means a second
    *process*, so --no-one-instance is passed explicitly rather than trusting
    the `one-instance` preference — it defaults to disabled, but a desktop
    file manager or a distro preset can flip it, and then this would silently
    hijack the window already playing something.

    --reuse asks for the opposite: hand the URL to a running VLC if there is
    one, which only does anything when one-instance is enabled.
    """
    vlc = shutil.which("vlc")
    if not vlc:
        sys.exit("vlc not found on PATH")
    mode = ["--one-instance", "--playlist-enqueue"] if reuse else ["--no-one-instance"]
    subprocess.Popen(
        [vlc, *mode, target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print("launched VLC" + (" (reusing a running instance if any)" if reuse else ""))


def save(body: str, dest: pathlib.Path, secret: bool) -> int:
    count = body.count("#EXTINF")
    if not count:
        sys.exit("playlist is empty — check the name with --list-categories")
    dest.write_text(clean_playlist(body))
    if secret:
        # Every line embeds the Tvheadend auth code.
        dest.chmod(0o600)
    return count


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build VLC-playable URLs and playlists from FastChannels or Tvheadend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Usage:" + __doc__.split("Usage:")[1],
    )
    ap.add_argument("--via", choices=("fastchannels", "tvheadend"), default="fastchannels",
                    help="which server to stream from (default: fastchannels)")
    sel = ap.add_mutually_exclusive_group()
    sel.add_argument("--category", "--tag", metavar="NAME", dest="category",
                     help='category, matched exactly, e.g. "Local News"')
    sel.add_argument("--channel", metavar="NUM_OR_NAME", help="one channel, by number or name")
    ap.add_argument("--source", metavar="NAME",
                    help="restrict to one provider, e.g. pluto (FastChannels only)")
    ap.add_argument("--list-categories", "--list-tags", action="store_true",
                    dest="list_categories", help="list categories and exit")
    ap.add_argument("--recordings", action="store_true", help="list DVR files on disk and exit")
    ap.add_argument("--write", metavar="PATH", help="save the playlist as a local .m3u")
    ap.add_argument("--play", action="store_true",
                    help="launch VLC on the result, in its own window")
    ap.add_argument("--reuse", action="store_true",
                    help="with --play, hand off to a running VLC instead of a new window")
    args = ap.parse_args()

    if args.recordings:
        return list_recordings()

    if args.source and args.via == "tvheadend":
        sys.exit("--source only applies to --via fastchannels")

    # ---- Tvheadend -------------------------------------------------------
    if args.via == "tvheadend":
        tvh = tvh_client()
        if args.list_categories:
            for name in tvh_tags(tvh):
                print(f"  {name}")
            return 0

        url = tvh_url(tvh_auth_code(tvh), args.category, args.channel)
        if args.write:
            dest = pathlib.Path(args.write).expanduser()
            n = save(tvh_fetch(url), dest, secret=True)
            print(f"wrote {n} channel(s) to {dest}")
            if args.play:
                launch(str(dest), args.reuse)
            return 0

        print(url)
        if args.play:
            # VLC can take the URL directly — every entry inside carries the code.
            launch(url, args.reuse)
        return 0

    # ---- FastChannels ----------------------------------------------------
    if args.list_categories:
        for name, count in sorted(fc_categories(), key=lambda c: -c[1]):
            print(f"  {count:5}  {name}")
        return 0

    # A name search is pushed to the server; a number has to be filtered here.
    search = args.channel if (args.channel and not args.channel.isdigit()) else None
    body = fc_playlist(args.category, args.source, search)
    if args.channel:
        body = fc_filter_channel(body, args.channel)

    dest = pathlib.Path(args.write).expanduser() if args.write else None
    if dest is None and not args.play:
        # Nothing to launch and nowhere to save: report what the filter selects
        # and show the URLs, noting the split rather than hiding it.
        query = fc_query(args.category, args.source, search)
        suffix = f"?{query}" if query else ""
        print(f"{body.count('#EXTINF')} channel(s)")
        print(f"  {FASTCHANNELS}/m3u{suffix}")
        print(f"  {FASTCHANNELS}/m3u/gracenote{suffix}")
        print("\nThese are two disjoint playlists; use --write or --play to get them merged.")
        return 0

    if dest is None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".m3u", delete=False)
        dest = pathlib.Path(tmp.name)
        tmp.close()
    n = save(body, dest, secret=False)
    if args.write:
        print(f"wrote {n} channel(s) to {dest}")
    if args.play:
        launch(str(dest), args.reuse)
    return 0


if __name__ == "__main__":
    sys.exit(main())
