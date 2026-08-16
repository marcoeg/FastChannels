#!/usr/bin/env python3
"""
Hand a channel to VLC, straight from FastChannels.

FastChannels has no authentication, so nothing here needs a token: a playlist
URL is a playlist URL. It also publishes 2,220 channels — the 2,014 in `/m3u`
plus the 206 in `/m3u/gracenote`, which are a *separate* set, not a subset.

Usage:
    ./vlc.py --list-categories               # categories with channel counts
    ./vlc.py --channel "Al Jazeera" --play    # watch one channel
    ./vlc.py --category News --play           # the whole category
    ./vlc.py --category News --write ~/news.m3u
    ./vlc.py --source pluto --play            # one provider
    ./vlc.py --recordings                     # what record.py has captured

    # each --play opens its own window, so these stack rather than replace
    ./vlc.py --channel "Al Jazeera" --play
    ./vlc.py --channel "Euronews" --play

Three edges this handles for you
--------------------------------
*Both playlists, always.* Channels carrying a Gracenote ID live in
`/m3u/gracenote` and are **absent** from `/m3u`. `--source pluto` is 176
channels from the first and 169 from the second, so asking for one loses
nearly half. Both are fetched and merged, which is why `--write`/`--play`
produce a file rather than handing VLC a single URL.

*Categories match exactly.* `/m3u?category=News` returns `News` only — not
`Local News`, which is a separate category. (`/api/channels` would substring
match; the playlist endpoints do not.) `--list-categories` shows the real
names with counts.

*Channel names survive.* FastChannels puts full descriptions in the M3U as
`tvg-description`/`tvc-guide-description`, and those contain commas. VLC splits
the `#EXTINF` title on a comma inside the quoted attributes rather than on the
separator, so its playlist shows sentence fragments where channel names should
be — but only for channels whose description happens to contain a comma, which
makes the breakage look random. Every `#EXTINF` is rewritten down to the
attributes a player actually uses. Also halves the file.

One window per channel
----------------------
VLC has no "open in a new window" action: picking an item in its playlist
replaces whatever that instance is playing, so watching two channels at once
means two VLC *processes*. `--play` passes `--no-one-instance` and each
invocation gets its own viewer. `--reuse` asks for the old behaviour, which
only does anything if the `one-instance` preference is enabled.

DRM channels will not play here — anything needing the PrismCast/Widevine
bridge is out, as it is in any non-browser client.
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
RECORDINGS = pathlib.Path.home() / "news" / "recordings"

FASTCHANNELS = "http://127.0.0.1:5523"

# Output geometry for a normalising relay. Anything spliced in at another
# resolution is scaled and padded to this, so the track stays consistent.
RELAY_SIZE = "1280:720"

EXTINF_HEAD = re.compile(r"^#EXTINF:(-?[\d.]+)")
EXTINF_ATTR = re.compile(r'([A-Za-z0-9_.-]+)="([^"]*)"')

# Attributes a player can use. Everything else is dropped — in particular
# tvg-description and tvc-guide-description, which are why clean_extinf()
# exists at all.
KEEP_ATTRS = ("tvg-id", "tvg-name", "tvg-chno", "tvg-logo", "group-title")


# --------------------------------------------------------------------------
# EXTINF rewriting — see the module docstring for why
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

    # Prefer an exact name match. "90s Throwback" also matches "XITE 90s
    # Throwback", and handing VLC two items instead of one changes its
    # behaviour badly — see launch_single() for why that matters.
    if not by_number:
        exact = ["#EXTM3U"]
        # out is ["#EXTM3U", EXTINF, url, EXTINF, url, …] — EXTINF at odd
        # indices, its url immediately after.
        for i in range(1, len(out) - 1, 2):
            if extinf_name(out[i]).strip().lower() == needle:
                exact.extend([out[i], out[i + 1]])
        if len(exact) > 1:
            out = exact
    if len(out) > 3:
        names = ", ".join(extinf_name(out[i]) for i in range(1, len(out), 2))
        print(f"note: {wanted!r} matched {(len(out) - 1) // 2} channels: {names}",
              file=sys.stderr)
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------

def best_variant(play_url: str) -> str | None:
    """The highest-bandwidth rendition of an HLS master, as an absolute URL.

    A FastChannels /play URL redirects to a provider master playlist listing
    several renditions. Handing VLC the *master* makes it adapt, and on
    ad-stitched channels that goes wrong twice over: it opens the lowest
    rendition and stays there (426x240), and the rendition set changes at an
    advert splice. Handing it one rendition removes adaptation entirely —
    there is nothing to get stuck on and nothing to re-select.

    The rendition is muxed: despite the master's AUDIO="audio" group, which
    only offers *alternative* audio such as the audio-description track, the
    variant itself carries both streams. Verified: aac 48000 + h264 1216x684.

    Resolution has to be against the URL curl actually ended on, not the /play
    URL, because the child paths are relative to the provider's master and the
    session query string must not be inherited. Returns None when this is not
    an HLS master, leaving the caller to use the original URL.
    """
    try:
        with urllib.request.urlopen(play_url, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            final = r.geturl()
    except Exception:
        return None
    if "#EXT-X-STREAM-INF" not in body:
        return None  # a media playlist already, or not HLS at all

    best_bw, best_path = -1, None
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        m = re.search(r"BANDWIDTH=(\d+)", line)
        if not m:
            continue
        for nxt in lines[i + 1:]:
            if nxt.strip() and not nxt.startswith("#"):
                if int(m.group(1)) > best_bw:
                    best_bw, best_path = int(m.group(1)), nxt.strip()
                break
    if not best_path:
        return None
    return urllib.parse.urljoin(final, best_path)


def list_recordings() -> int:
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
    """
    vlc = shutil.which("vlc")
    if not vlc:
        sys.exit("vlc not found on PATH")
    mode = ["--one-instance", "--playlist-enqueue"] if reuse else ["--no-one-instance"]
    # No adaptive tuning. Four settings were tried to stop channels opening at
    # 426x240 — highest, fixedrate, nearoptimal and adaptive-bw. Headless runs
    # showed them fetching only the top rendition; in real playback none
    # changed what was displayed, and highest/fixedrate additionally broke
    # playback at the first advert. VLC mis-parses this master (see below), so
    # what it fetches and what it decodes are not the same thing, and no launch
    # flag reached the decoder. Left bare: this is what plays through adverts.
    subprocess.Popen(
        # --no-video-title-show suppresses the overlay VLC draws over the first
        # seconds of playback; with a pinned rendition the "title" is a long
        # provider URL, so it is noise rather than information.
        [vlc, *mode, "--network-caching=3000", "--no-video-title-show", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print("launched VLC" + (" (reusing a running instance if any)" if reuse else ""))


def relay(url: str, name: str, copy_only: bool = False) -> int:
    """Play through ffmpeg instead of letting VLC parse the HLS itself.

    Server-side ad insertion breaks players in two distinct ways, and they need
    different answers.

    **Origin switching.** Content segments come from the content CDN, ad
    segments from another host, and the playlist changes origin mid-stream. Put
    a SUBTITLES rendition on top and VLC's adaptive demuxer spins instead of
    playing. NHRA on freelivesports does this —

        Cannot reuse HTTP connection for different host:
            d35p4ep9ync6qx.cloudfront.net != stream.ads.ottera.tv

    — while ffmpeg handles it silently. A passthrough relay (`--relay-copy`) is
    enough here, and costs nothing.

    **Encoding changes.** Pluto's stitcher splices ads encoded at a different
    resolution from the content. With `-c copy` the MPEG-TS output is fixed at
    whatever the first segment was, so the ad's frames land in a track that
    declares the wrong geometry. Measured on a constructed splice:

        -c copy   -> one video track containing BOTH 1216x684 and 640x360
        transcode -> 1216x684 throughout

    ffmpeg tolerates the mixed track; VLC's decoder does not. It freezes on the
    last good frame and never re-initialises — while the audio, which comes
    from a *separate* rendition playlist whose parameters never change, keeps
    playing. That is the "frozen video, audio continues, only audio comes back"
    symptom, and no amount of copying fixes it.

    So the default re-encodes to a fixed geometry, which is the only thing that
    makes the output stable regardless of what the input does. Measured at
    1.62x realtime at 720p on this machine, so live viewing has headroom.

    Either way VLC gets a stream rather than a playlist, so seeking and the
    channel name in the title bar are gone. Use it when a channel misbehaves,
    not by default.
    """
    ffmpeg = shutil.which("ffmpeg")
    vlc = shutil.which("vlc")
    if not ffmpeg:
        sys.exit("ffmpeg not found on PATH — needed for --relay")
    if not vlc:
        sys.exit("vlc not found on PATH")

    # Loopback rather than the published base URL: this is a local pipe, and
    # there is no reason to put mDNS resolution in front of it.
    parts = urllib.parse.urlsplit(url)
    base = urllib.parse.urlsplit(FASTCHANNELS)
    local = urllib.parse.urlunsplit(
        (base.scheme, base.netloc, parts.path, parts.query, parts.fragment))

    if copy_only:
        codec = ["-c", "copy"]
        how = "passthrough"
    else:
        codec = [
            # Fixed geometry is the whole point: an ad spliced in at a
            # different resolution gets scaled and padded to match rather than
            # landing in a track that declares the wrong size.
            "-vf", (f"scale={RELAY_SIZE}:force_original_aspect_ratio=decrease,"
                    f"pad={RELAY_SIZE}:(ow-iw)/2:(oh-ih)/2"),
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            # 48 kHz because some channels carry 44.1 kHz AAC, which is
            # off-spec for MPEG-TS and silently loses audio in some players.
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
        ]
        how = f"normalising to {RELAY_SIZE}"

    print(f"relaying {name} through ffmpeg ({how}) — close VLC to stop")
    ff = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
         "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30",
         "-rw_timeout", "30000000", "-i", local, *codec, "-f", "mpegts", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    player = subprocess.Popen([vlc, "--no-one-instance", "-"], stdin=ff.stdout,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Let ffmpeg see VLC's exit: the parent must not hold the read end open.
    ff.stdout.close()
    player.wait()
    ff.terminate()
    return 0


def save(body: str, dest: pathlib.Path) -> int:
    count = body.count("#EXTINF")
    if not count:
        sys.exit("playlist is empty — check the name with --list-categories")
    dest.write_text(clean_playlist(body))
    return count


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build VLC-playable playlists from FastChannels.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Usage:" + __doc__.split("Usage:")[1].split("Three edges")[0],
    )
    sel = ap.add_mutually_exclusive_group()
    sel.add_argument("--category", "--tag", metavar="NAME", dest="category",
                     help='category, matched exactly, e.g. "Local News"')
    sel.add_argument("--channel", metavar="NUM_OR_NAME", help="one channel, by number or name")
    ap.add_argument("--source", metavar="NAME",
                    help="restrict to one provider, e.g. pluto")
    ap.add_argument("--list-categories", "--list-tags", action="store_true",
                    dest="list_categories", help="list categories and exit")
    ap.add_argument("--recordings", action="store_true",
                    help="list captured recordings and exit")
    ap.add_argument("--write", metavar="PATH", help="save the playlist as a local .m3u")
    ap.add_argument("--play", action="store_true",
                    help="launch VLC on the result, in its own window")
    ap.add_argument("--no-pin", action="store_true",
                    help="hand VLC the master playlist and let it adapt, instead of "
                         "pinning the highest rendition")
    ap.add_argument("--reuse", action="store_true",
                    help="with --play, hand off to a running VLC instead of a new window")
    ap.add_argument("--relay", action="store_true",
                    help="pipe one channel through ffmpeg into VLC, re-encoded to a fixed "
                         "size — use when a channel spins or freezes on adverts")
    ap.add_argument("--relay-copy", action="store_true", dest="relay_copy",
                    help="relay without re-encoding: fixes origin-switching, but not "
                         "adverts encoded at a different resolution")
    args = ap.parse_args()

    if args.recordings:
        return list_recordings()

    if args.list_categories:
        for name, count in sorted(fc_categories(), key=lambda c: -c[1]):
            print(f"  {count:5}  {name}")
        return 0

    # A name search is pushed to the server; a number has to be filtered here.
    search = args.channel if (args.channel and not args.channel.isdigit()) else None
    body = fc_playlist(args.category, args.source, search)
    if args.channel:
        body = fc_filter_channel(body, args.channel)

    if args.relay or args.relay_copy:
        if not args.channel:
            sys.exit("--relay needs --channel: it plays one stream, not a playlist")
        lines = body.splitlines()
        url = next((l for l in lines if l.startswith("http")), None)
        name = next((extinf_name(l) for l in lines if l.startswith("#EXTINF")), args.channel)
        return relay(url, name, copy_only=args.relay_copy)

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

    # A single channel goes to VLC as a bare URL, never wrapped in a playlist.
    # This is not cosmetic. Ad-stitched channels change encoding mid-stream;
    # given a direct MRL, VLC re-initialises and builds a fresh pair of
    # elementary streams for each break, carrying on happily. Given a
    # *playlist*, the same error is treated as "this item failed" and VLC
    # advances or stops. That is what killed playback at the first advert.
    urls = [ln for ln in body.splitlines() if ln.startswith("http")]
    if args.play and not args.write and len(urls) == 1:
        target = urls[0]
        if not args.no_pin:
            variant = best_variant(target.replace("nucbox.local", "127.0.0.1"))
            if variant:
                target = variant
                print("pinned to the highest rendition")
        launch(target, args.reuse)
        return 0

    if dest is None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".m3u", delete=False)
        dest = pathlib.Path(tmp.name)
        tmp.close()
    n = save(body, dest)
    if args.write:
        print(f"wrote {n} channel(s) to {dest}")
    if args.play:
        launch(str(dest), args.reuse)
    return 0


if __name__ == "__main__":
    sys.exit(main())
