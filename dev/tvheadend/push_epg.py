#!/usr/bin/env python3
"""
Feed FastChannels' XMLTV into Tvheadend's external EPG grabber.

Tvheadend's XMLTV module cannot fetch a URL. It offers two paths: an
*internal* grabber that shells out to `tv_grab_*` scripts inside the container,
and an *external* grabber that reads a complete XMLTV document from a Unix
socket. The socket is the simpler of the two — no image changes, no
tv_grab protocol to implement — so this streams /epg.xml straight into it.

    FastChannels /epg.xml  ──>  <config>/epggrab/xmltv.sock  ──>  Tvheadend

Matching is automatic: FastChannels writes `tvg-id="{source}.{id}"` into the
M3U, Tvheadend stores that as `iptv_epgid`, and the XMLTV `<channel id>` uses
the identical string. Nothing has to be mapped by hand.

Usage:
    python3 dev/tvheadend/push_epg.py                 # once
    python3 dev/tvheadend/push_epg.py --loop 6h       # keep it fresh
    python3 dev/tvheadend/push_epg.py --feed news-only

Run it after every FastChannels scrape, or on a timer. Written in stdlib only
so it needs no socat.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import socket
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_SOCKET = HERE / "config" / "epggrab" / "xmltv.sock"
FASTCHANNELS = "http://127.0.0.1:5523"
CHUNK = 64 * 1024


def parse_interval(text: str) -> int:
    """'6h' / '90m' / '3600' -> seconds."""
    m = re.fullmatch(r"(\d+)\s*([smhd]?)", text.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"bad interval {text!r}; try 6h, 90m or 3600")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def epg_url(base: str, feed: str | None) -> str:
    return f"{base.rstrip('/')}" + (f"/feeds/{feed}/epg.xml" if feed else "/epg.xml")


def push(url: str, sock_path: pathlib.Path, timeout: int = 300) -> tuple[bool, str]:
    """Stream the XMLTV document into Tvheadend's socket.

    Streamed rather than buffered: the full guide for a large lineup runs to
    tens of megabytes, and there is no reason to hold it in memory. A short
    read from the server is passed through as-is — Tvheadend validates the XML
    itself and ignores a document it cannot parse.
    """
    if not sock_path.exists():
        return False, (f"socket not found: {sock_path}\n"
                       "  Tvheadend creates it when the external XMLTV grabber is "
                       "enabled — run setup.py, then make sure Tvheadend is running.")

    try:
        response = urllib.request.urlopen(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not fetch {url}: {exc}"

    sent = 0
    try:
        with response, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                s.sendall(chunk)
                sent += len(chunk)
            # Tvheadend parses on EOF, so the write side must be closed before
            # the socket is torn down or the document is never processed.
            s.shutdown(socket.SHUT_WR)
    except FileNotFoundError:
        return False, f"socket not found: {sock_path}"
    except ConnectionRefusedError:
        return False, (f"nothing listening on {sock_path}\n"
                       "  the external XMLTV grabber is not enabled, or Tvheadend is down")
    except Exception as exc:  # noqa: BLE001
        return False, f"push failed after {sent} bytes: {exc}"

    if sent == 0:
        return False, "the server returned an empty document"
    return True, f"pushed {sent / 1_048_576:.1f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--base", default=FASTCHANNELS, help=f"FastChannels URL (default {FASTCHANNELS})")
    ap.add_argument("--feed", help="FastChannels feed slug; omit for the master /epg.xml")
    ap.add_argument("--socket", type=pathlib.Path, default=DEFAULT_SOCKET,
                    help=f"Tvheadend XMLTV socket (default {DEFAULT_SOCKET})")
    ap.add_argument("--loop", type=parse_interval, metavar="INTERVAL",
                    help="keep running, pushing every INTERVAL (e.g. 6h)")
    ap.add_argument("--once", action="store_true",
                    help="push a single time, skipping the first-run double push")
    args = ap.parse_args()

    url = epg_url(args.base, args.feed)

    # On a lineup Tvheadend has not seen before, one push is not enough. The
    # first parse creates the grabber channels from the <channel> elements and
    # links them to real channels — but the <programme> elements in that same
    # document are processed against links that did not exist when the parse
    # began, and are dropped. A second push, with the links now in place,
    # stores them. Observed here as 2,014 channels linked but 0 events, then
    # 91,796 events after an immediate re-push.
    #
    # Cheap to just do it twice: pushing an already-known lineup is idempotent.
    passes = 1 if (args.once or args.loop) else 2

    while True:
      for attempt in range(passes):
        if attempt:
            print("  first-run double push: sending again so programmes attach "
                  "to the channels the first pass linked", file=sys.stderr)
            time.sleep(5)
        ok, message = push(url, args.socket)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp}  {'ok  ' if ok else 'FAIL'}  {url} -> {args.socket.name}: {message}",
              file=sys.stdout if ok else sys.stderr, flush=True)
        if not ok:
            break
      if not args.loop:
          return 0 if ok else 1
      time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
