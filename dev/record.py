#!/usr/bin/env python3
"""
Record a FastChannels channel with ffmpeg, scheduled by systemd.

This is the DVR, replacing Tvheadend. It does one job — capture a stream
between two times — with two pieces that are already on the machine:

    ffmpeg      pulls the stream and copies it to disk, no re-encode
    systemd     holds the timer, so a recording at 02:00 does not need
                anything running in the meantime

Why not Tvheadend: it reaches only 2,014 of the 2,220 channels (it ignores
tvc-guide-stationid, so the whole Gracenote playlist is invisible to it),
needs an XMLTV socket shim, serves digest-only auth that VLC cannot use, and
carries a set of defaults that quietly break recording — `scan_create` opening
every stream, and an OTA EPG grabber that pins the IPTV inputs so that *every*
recording fails with "No input source available".

Usage:
    ./record.py now --channel "Al Jazeera" --duration 30m
    ./record.py at --channel "Al Jazeera" --start "18:00" --duration 1h
    ./record.py programme --channel "Al Jazeera" --match "Newshour"
    ./record.py epg --channel "Al Jazeera"
    ./record.py list
    ./record.py cancel fcrec-al-jazeera-1786823520

`--json` on epg/list/programme makes this comfortable to drive from an agent.

Scheduling model
----------------
One transient systemd timer per recording, and no state file — `systemctl
--user list-timers` *is* the database:

    systemd-run --user --unit=fcrec-<slug>-<epoch> \\
      --on-calendar="2026-08-15 18:00:00" \\
      --timer-property=RemainAfterElapse=no \\
      <this script> now --channel ... --duration ...

`RemainAfterElapse=no` is not optional: without it a fired timer lingers and
`list` fills with corpses.

The channel URL is resolved when the recording *starts*, never when it is
scheduled. FastChannels re-resolves `/play/...` per request and upstream
session tokens expire, so a URL captured hours earlier would be stale.

Two nuances worth knowing
-------------------------
*Guides drift*, so `programme` pads by default — 30s before, 120s after.

*HLS gives you back-buffer.* ffmpeg starts from segments the CDN has already
published, so it runs ahead of real time before settling: a 30s capture
completed in 22s of wall clock during testing. `-t` counts **stream** seconds,
not wall-clock, so a recording can finish before the programme really ends.
The 120s end-pad absorbs a typical 30-60s buffer.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
SELF = pathlib.Path(__file__).resolve()

FASTCHANNELS = "http://127.0.0.1:5523"
OUT_DIR = pathlib.Path.home() / "news" / "recordings"
UNIT_PREFIX = "fcrec-"


def _load_vlc():
    """Reuse vlc.py's channel resolution rather than reimplementing it.

    It already handles the two FastChannels edges that matter here: /m3u and
    /m3u/gracenote are disjoint sets and both must be fetched, and the EXTINF
    parsing has to find the separator comma rather than the first one. Loaded
    by explicit path — vlc.py has no import-time side effects.
    """
    spec = importlib.util.spec_from_file_location("_fcvlc", HERE / "vlc.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------

DURATION_RE = re.compile(r"(\d+)\s*([smhd])", re.I)


def parse_duration(text: str) -> int:
    """'30m', '1h30m', '45s', '2h' or a bare number of seconds -> seconds."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    total = 0
    matched = False
    for value, unit in DURATION_RE.findall(text):
        matched = True
        total += int(value) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit.lower()]
    if not matched or total <= 0:
        raise argparse.ArgumentTypeError(f"cannot read duration {text!r} (try 30m, 1h30m, 45s)")
    return total


def parse_when(text: str) -> datetime.datetime:
    """'+20min', '18:00', '2026-08-15 18:00' -> an aware local datetime.

    A bare time that has already passed today is read as tomorrow, which is
    what you mean when you type it.
    """
    text = text.strip()
    now = datetime.datetime.now().astimezone()
    if text.startswith("+"):
        return now + datetime.timedelta(seconds=parse_duration(text[1:]))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
            parsed = parsed.astimezone()
            if parsed <= now:
                parsed += datetime.timedelta(days=1)
            return parsed
        return parsed.astimezone()
    raise argparse.ArgumentTypeError(
        f"cannot read time {text!r} (try '18:00', '2026-08-15 18:00' or '+20min')")


def slug(text: str) -> str:
    """Filename- and unit-safe. Mirrors the DVR naming already in use."""
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"[^A-Za-z0-9._-]", "", text)
    return re.sub(r"-{2,}", "-", text).strip("-.") or "unnamed"


# --------------------------------------------------------------------------
# FastChannels
# --------------------------------------------------------------------------

def local_url(url: str) -> str:
    """Point a playlist URL back at loopback for capture.

    public_base_url is the LAN hostname so that other devices get usable
    playlists, which means M3U entries now read
    http://nucbox.local:5523/play/... . The recorder runs on this host, so
    resolving that over mDNS would put a name lookup in the path of every
    capture — including unattended overnight ones, which is exactly where you
    do not want an extra dependency. Loopback always works and is the same
    server.
    """
    parts = urllib.parse.urlsplit(url)
    base = urllib.parse.urlsplit(FASTCHANNELS)
    return urllib.parse.urlunsplit(
        (base.scheme, base.netloc, parts.path, parts.query, parts.fragment))


def resolve_channel(wanted: str) -> dict:
    """Find a channel by name (substring) or by number, from both playlists."""
    vlc = _load_vlc()
    body = vlc.fc_playlist(None, None, None)
    lines = body.splitlines()
    by_number = wanted.isdigit()
    needle = wanted.strip().lower()

    hits = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF") or i + 1 >= len(lines):
            continue
        name = vlc.extinf_name(line)
        attrs = dict(vlc.EXTINF_ATTR.findall(line))
        if by_number:
            hit = attrs.get("tvg-chno") == wanted
        else:
            hit = needle in name.lower()
        if hit:
            hits.append({"name": name, "url": local_url(lines[i + 1]),
                         "tvg_id": attrs.get("tvg-id", ""),
                         "number": attrs.get("tvg-chno", "")})
    if not hits:
        sys.exit(f"no channel matched {wanted!r}")
    exact = [h for h in hits if h["name"].lower() == needle]
    if exact:
        return exact[0]
    if len(hits) > 1:
        names = ", ".join(h["name"] for h in hits[:8])
        print(f"note: {wanted!r} matched {len(hits)} channels, using the first. "
              f"({names})", file=sys.stderr)
    return hits[0]


def epg_for(channel: dict) -> list[dict]:
    """Programmes for one channel, from /epg.xml.

    /epg.xml filters by channel *name*, not id, so the search narrows the
    document and the tvg-id then selects exactly the right channel — XMLTV's
    `channel=` attribute is the same string as the M3U's tvg-id, which is what
    makes this join trivial.
    """
    query = urllib.parse.urlencode({"search": channel["name"]})
    with urllib.request.urlopen(f"{FASTCHANNELS}/epg.xml?{query}", timeout=120) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    out = []
    for prog in root.findall("programme"):
        if channel["tvg_id"] and prog.get("channel") != channel["tvg_id"]:
            continue
        try:
            start = datetime.datetime.strptime(prog.get("start"), "%Y%m%d%H%M%S %z")
            stop = datetime.datetime.strptime(prog.get("stop"), "%Y%m%d%H%M%S %z")
        except (TypeError, ValueError):
            continue
        out.append({
            "title": (prog.findtext("title") or "").strip(),
            "desc": (prog.findtext("desc") or "").strip(),
            "start": start.astimezone(),
            "stop": stop.astimezone(),
        })
    return sorted(out, key=lambda p: p["start"])


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def out_name(channel_name: str, title: str, when: datetime.datetime) -> str:
    """Channel_Title_date_time — the convention already in use for recordings.

    When no title was given it defaults to the channel name, and repeating it
    ("Al-Jazeera_Al-Jazeera_...") reads like a bug, so collapse that case.
    """
    parts = [slug(channel_name)]
    if slug(title).lower() != slug(channel_name).lower():
        parts.append(slug(title))
    parts.append(f"{when:%Y-%m-%d_%H%M}")
    return "_".join(parts)


def capture(channel: dict, seconds: int, title: str, out_dir: pathlib.Path,
            mp4: bool) -> int:
    """Run ffmpeg and record what happened beside the file."""
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_dir = out_dir / ".meta"
    meta_dir.mkdir(exist_ok=True)

    started = datetime.datetime.now().astimezone()
    stem = out_name(channel["name"], title, started)
    target = out_dir / f"{stem}.ts"
    log_path = meta_dir / f"{stem}.log"

    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
        # A live stream can drop; without these ffmpeg simply stops.
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30",
        # …but reconnecting forever is its own failure. Give up on a read that
        # stalls for 30s rather than retrying a segment that will never arrive.
        "-rw_timeout", "30000000",
        "-i", channel["url"],
        "-t", str(seconds),
        "-c", "copy", "-f", "mpegts", str(target),
    ]

    # A wall-clock bound on top, because -rw_timeout is not enough on its own.
    # Observed on an AES-encrypted Pluto stream: ffmpeg captured 18s, hit
    # "Unable to open resource" on a segment plus TLS errors, and then sat
    # retrying indefinitely. Unbounded, a scheduled recording would hang for
    # ever. Slack is generous because HLS back-buffer lets ffmpeg run *ahead*
    # of real time early on, so wall-clock and stream time are not the same.
    deadline = seconds + max(60, seconds // 4)
    timed_out = False
    with log_path.open("w") as log:
        log.write(f"# {' '.join(cmd)}\n\n")
        log.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log)
        try:
            rc = proc.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            timed_out = True
            # SIGTERM first: ffmpeg finalises the file, so a partial capture
            # stays playable instead of being truncated mid-packet.
            proc.terminate()
            try:
                rc = proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
            log.write(f"\n# wall-clock deadline of {deadline}s exceeded; terminated\n")

    size = target.stat().st_size if target.exists() else 0
    final = target
    # A cut-short capture is still worth keeping — the stream failing halfway
    # through is exactly when you want the first half. Judge on bytes, not on
    # ffmpeg's exit status, which is non-zero after a SIGTERM.
    usable = size > 0
    if usable and mp4:
        # Remux only after a clean capture: MP4 written directly would be
        # unplayable if the recording were cut short, since the index lands at
        # the end of the file.
        mp4_path = out_dir / f"{stem}.mp4"
        remux = subprocess.call(
            ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
             "-i", str(target), "-c", "copy", "-movflags", "+faststart", str(mp4_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if remux == 0 and mp4_path.exists():
            target.unlink()
            final = mp4_path
            size = mp4_path.stat().st_size

    captured = 0.0
    if usable:
        probe = subprocess.run(
            ["ffprobe", "-loglevel", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(final)],
            capture_output=True, text=True)
        try:
            captured = round(float(probe.stdout.strip()), 1)
        except ValueError:
            captured = 0.0

    (meta_dir / f"{stem}.json").write_text(json.dumps({
        "channel": channel["name"],
        "channel_number": channel["number"],
        "tvg_id": channel["tvg_id"],
        "url": channel["url"],
        "title": title,
        "started": started.isoformat(),
        "requested_seconds": seconds,
        "captured_seconds": captured,
        "exit_status": rc,
        "timed_out": timed_out,
        "complete": bool(usable and not timed_out and captured >= seconds * 0.95),
        "file": str(final) if usable else None,
        "bytes": size,
        "log": str(log_path),
    }, indent=2) + "\n")

    if not usable:
        print(f"FAILED rc={rc} — nothing captured, see {log_path}", file=sys.stderr)
        return 1
    note = ""
    if timed_out:
        note = "  [PARTIAL: stream stalled, terminated at the deadline]"
    elif captured and captured < seconds * 0.95:
        note = f"  [PARTIAL: {captured}s of {seconds}s]"
    print(f"{final}  ({size / 1e6:.1f} MB, {captured}s){note}")
    if note:
        print(f"  see {log_path}", file=sys.stderr)
    return 0


def schedule(channel_arg: str, start: datetime.datetime, seconds: int,
             title: str, out_dir: pathlib.Path, mp4: bool) -> int:
    """Hand the recording to systemd and forget about it."""
    if not shutil.which("systemd-run"):
        sys.exit("systemd-run not found — cannot schedule")
    unit = f"{UNIT_PREFIX}{slug(title).lower()}-{int(start.timestamp())}"
    cmd = [
        "systemd-run", "--user", f"--unit={unit}",
        f"--on-calendar={start:%Y-%m-%d %H:%M:%S}",
        # Otherwise the timer survives firing and clutters `list` forever.
        "--timer-property=RemainAfterElapse=no",
        f"--description=FastChannels recording: {title}",
        str(SELF), "now",
        "--channel", channel_arg,
        "--duration", str(seconds),
        "--title", title,
        "--out", str(out_dir),
    ]
    if mp4:
        cmd.append("--mp4")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(proc.stderr.strip() or "systemd-run failed")
    print(f"scheduled {unit}")
    print(f"  {start:%Y-%m-%d %H:%M %Z} for {seconds}s -> {out_dir}")
    return 0


def list_scheduled(as_json: bool) -> int:
    proc = subprocess.run(
        ["systemctl", "--user", "list-timers", "--all", "--no-pager", "--output=json"],
        capture_output=True, text=True)
    rows = []
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            rows = [t for t in json.loads(proc.stdout)
                    if (t.get("unit") or "").startswith(UNIT_PREFIX)]
        except json.JSONDecodeError:
            rows = []
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("nothing scheduled")
        return 0
    now = datetime.datetime.now().astimezone()
    for t in rows:
        # `next` and `left` are both absolute microsecond epochs in this
        # systemd's JSON output, despite the name — so derive the remaining
        # time rather than printing `left` as if it were a duration.
        nxt = t.get("next") or 0
        if not nxt:
            print(f"  {t['unit']:<44} (no next elapse)")
            continue
        when = datetime.datetime.fromtimestamp(nxt / 1e6).astimezone()
        remaining = int((when - now).total_seconds())
        if remaining < 0:
            left = "due"
        elif remaining < 3600:
            left = f"in {remaining // 60}m {remaining % 60}s"
        elif remaining < 86400:
            left = f"in {remaining // 3600}h {(remaining % 3600) // 60}m"
        else:
            left = f"in {remaining // 86400}d {(remaining % 86400) // 3600}h"
        print(f"  {t['unit']:<44} {when:%Y-%m-%d %H:%M}  {left}")
    return 0


def cancel(unit: str) -> int:
    if not unit.startswith(UNIT_PREFIX):
        unit = UNIT_PREFIX + unit
    unit = unit.removesuffix(".timer").removesuffix(".service")
    rc = subprocess.call(["systemctl", "--user", "stop", f"{unit}.timer"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call(["systemctl", "--user", "reset-failed", f"{unit}.timer", f"{unit}.service"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("cancelled" if rc == 0 else f"no such timer: {unit}.timer")
    return 0 if rc == 0 else 1


# --------------------------------------------------------------------------

def cmd_epg(args) -> int:
    ch = resolve_channel(args.channel)
    progs = epg_for(ch)
    now = datetime.datetime.now().astimezone()
    upcoming = [p for p in progs if p["stop"] > now][:args.limit]
    if args.json:
        print(json.dumps([{**p, "start": p["start"].isoformat(),
                           "stop": p["stop"].isoformat()} for p in upcoming], indent=2))
        return 0
    print(f"{ch['name']}  (ch {ch['number']}, {ch['tvg_id']}) — {len(progs)} programmes stored")
    for p in upcoming:
        mins = int((p["stop"] - p["start"]).total_seconds() // 60)
        print(f"  {p['start']:%a %H:%M}-{p['stop']:%H:%M} ({mins:>3}m)  {p['title']}")
    return 0


def cmd_now(args) -> int:
    ch = resolve_channel(args.channel)
    return capture(ch, parse_duration(args.duration), args.title or ch["name"],
                   pathlib.Path(args.out).expanduser(), args.mp4)


def cmd_at(args) -> int:
    start = parse_when(args.start)
    ch = resolve_channel(args.channel)
    return schedule(args.channel, start, parse_duration(args.duration),
                    args.title or ch["name"], pathlib.Path(args.out).expanduser(),
                    args.mp4)


def cmd_programme(args) -> int:
    ch = resolve_channel(args.channel)
    now = datetime.datetime.now().astimezone()
    needle = args.match.strip().lower()
    hits = [p for p in epg_for(ch) if needle in p["title"].lower() and p["stop"] > now]
    if not hits:
        sys.exit(f"no upcoming programme on {ch['name']} matching {args.match!r} "
                 f"(try: {SELF.name} epg --channel {ch['name']!r})")
    prog = hits[0]
    start = prog["start"] - datetime.timedelta(seconds=args.pad_start)
    seconds = int((prog["stop"] - prog["start"]).total_seconds()) + args.pad_start + args.pad_end
    out_dir = pathlib.Path(args.out).expanduser()

    if args.json or args.dry_run:
        info = {
            "channel": ch["name"], "title": prog["title"],
            "airs": prog["start"].isoformat(), "ends": prog["stop"].isoformat(),
            "record_from": start.isoformat(), "seconds": seconds,
            "output_dir": str(out_dir),
            "file": out_name(ch["name"], prog["title"], start) + (".mp4" if args.mp4 else ".ts"),
            "upcoming_matches": len(hits),
        }
        print(json.dumps(info, indent=2))
        if args.dry_run:
            return 0
    if start <= now:
        # Already started; record what is left rather than refusing.
        seconds = int((prog["stop"] - now).total_seconds()) + args.pad_end
        return capture(ch, seconds, prog["title"], out_dir, args.mp4)
    return schedule(args.channel, start, seconds, prog["title"], out_dir, args.mp4)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Record FastChannels channels with ffmpeg, scheduled by systemd.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Usage:" + __doc__.split("Usage:")[1].split("Scheduling model")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, writes_files=True):
        p.add_argument("--channel", required=True, metavar="NAME_OR_NUMBER",
                       help="channel name (substring match) or channel number")
        if writes_files:
            p.add_argument("--out", default=str(OUT_DIR), metavar="DIR",
                           help=f"output directory (default: {OUT_DIR})")
            p.add_argument("--mp4", action="store_true",
                           help="remux to MP4 after a clean capture")

    p = sub.add_parser("now", help="record immediately")
    common(p)
    p.add_argument("--duration", required=True, metavar="30m")
    p.add_argument("--title", help="name for the file (default: channel name)")
    p.set_defaults(func=cmd_now)

    p = sub.add_parser("at", help="schedule an absolute window")
    common(p)
    p.add_argument("--start", required=True, metavar="'18:00'")
    p.add_argument("--duration", required=True, metavar="1h")
    p.add_argument("--title")
    p.set_defaults(func=cmd_at)

    p = sub.add_parser("programme", help="schedule the next airing from the guide")
    common(p)
    p.add_argument("--match", required=True, metavar="TITLE")
    p.add_argument("--pad-start", type=int, default=30, metavar="SEC")
    p.add_argument("--pad-end", type=int, default=120, metavar="SEC")
    p.add_argument("--dry-run", action="store_true", help="resolve and print, schedule nothing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_programme)

    p = sub.add_parser("epg", help="what is on this channel")
    common(p, writes_files=False)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_epg)

    p = sub.add_parser("list", help="scheduled recordings")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda a: list_scheduled(a.json))

    p = sub.add_parser("cancel", help="drop a scheduled recording")
    p.add_argument("unit")
    p.set_defaults(func=lambda a: cancel(a.unit))

    argv = sys.argv[1:]
    # Accept `--list` as well as `list`. vlc.py, the sibling tool, is
    # flag-driven while this one is verb-driven, and having to remember which
    # is which is a pointless thing to get wrong.
    verbs = set(sub.choices)
    if argv and argv[0].startswith("--") and argv[0][2:] in verbs:
        argv[0] = argv[0][2:]
    if not argv:
        ap.print_help()
        return 0

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
