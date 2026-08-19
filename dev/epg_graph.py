#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.10,<2"]
# ///
"""Ingest FastChannels EPG airings into the EPG knowledge graph.

One AIRS edge per airing, written through the *patched* Graphiti MCP server's
direct-write path (no LLM calls, no edge resolution). See
dev/EPG-GRAPH-PLAN.md.

  (Channel: Al Jazeera) -[AIRS]-> (Newshour)
     fact:       "Al Jazeera airs Newshour, Mon 18 Aug 14:00-15:00 PDT. ..."
     valid_at:   start (UTC)      invalid_at: stop (UTC)

This script talks ONLY to the EPG server on 127.0.0.1:8001 and refuses to run
against any server whose default group is not "epg" — pointed at the chat
server (8000) it would write LLM-mediated edges into Hermes' memory.

Step-2 scope: per-channel filtered fetch, sequential direct writes, uuid
ledger in ~/news/epg-graph-state.json. Reconciliation and --prune are step 3.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Hard-coded on purpose: 8000 is the CHAT memory server. Never point this
# script there.
EPG_MCP_URL = "http://127.0.0.1:8001/mcp"
FC_BASE = "http://127.0.0.1:5523"
GROUP_ID = "epg"  # literal everywhere; never interpolate a variable
STATE_FILE = Path.home() / "news" / "epg-graph-state.json"
STATE_FLUSH_EVERY = 25
XMLTV_FMT = "%Y%m%d%H%M%S %z"


# ---------------------------------------------------------------- state file

def load_state() -> dict:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        if state.get("version") != 1:
            sys.exit(f"unsupported state version in {STATE_FILE}; refusing to run")
        return state
    return {"version": 1, "nodes": {}, "airings": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, STATE_FILE)


class StateLock:
    """flock so overlapping runs exit instead of double-ingesting."""

    def __init__(self):
        self.path = STATE_FILE.with_suffix(".lock")
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another epg_graph.py run holds the lock; exiting", file=sys.stderr)
            sys.exit(0)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


# ------------------------------------------------------------------ EPG side

def fetch_channel_epg(channel_name: str) -> tuple[str, list[dict]]:
    """Fetch the filtered guide for one channel; return (channel_id, airings).

    /epg.xml?search= is a substring match on channel NAME, so the response may
    contain several channels; we require exactly one exact display-name match.
    """
    query = urllib.parse.urlencode({"search": channel_name})
    with urllib.request.urlopen(f"{FC_BASE}/epg.xml?{query}", timeout=60) as r:
        root = ET.fromstring(r.read())

    wanted = channel_name.casefold()
    ids = []
    for ch in root.iter("channel"):
        names = [dn.text or "" for dn in ch.findall("display-name")]
        if any(n.casefold() == wanted for n in names):
            ids.append(ch.get("id"))
    if len(ids) != 1:
        all_names = sorted(
            {dn.text for ch in root.iter("channel") for dn in ch.findall("display-name") if dn.text}
        )
        sys.exit(
            f"channel {channel_name!r}: expected exactly one exact match, got {len(ids)}."
            f" Channels in response: {all_names}"
        )
    channel_id = ids[0]

    airings = []
    for prog in root.iter("programme"):
        if prog.get("channel") != channel_id:
            continue
        try:
            start = datetime.strptime(prog.get("start"), XMLTV_FMT)
            stop = datetime.strptime(prog.get("stop"), XMLTV_FMT)
        except (TypeError, ValueError):
            continue
        title = (prog.findtext("title") or "").strip()
        desc = (prog.findtext("desc") or "").strip()
        if title:
            airings.append({"start": start, "stop": stop, "title": title, "desc": desc})
    airings.sort(key=lambda a: a["start"])
    return channel_id, airings


def build_fact(channel: str, a: dict) -> str:
    start_l, stop_l = a["start"].astimezone(), a["stop"].astimezone()
    tz = start_l.strftime("%Z")
    when = f"{start_l:%a %d %b %H:%M}-{stop_l:%H:%M} {tz}"
    fact = f"{channel} airs {a['title']}, {when}."
    if a["desc"]:
        fact += f" {a['desc']}"
    return fact


def airing_key(channel_id: str, a: dict) -> str:
    return f"{channel_id}|{a['title']}|{a['start'].astimezone(timezone.utc).isoformat()}"


# ------------------------------------------------------------------ MCP side

def parse_result(res) -> dict:
    if res.isError:
        raise RuntimeError(f"MCP tool error: {res.content}")
    data = res.structuredContent
    if data is None:
        data = json.loads(res.content[0].text)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])
    return data


async def verify_epg_server(session: ClientSession) -> None:
    """Refuse to run unless the server identifies its default group as epg.

    The chat server (8000) runs the unpatched image, whose get_status message
    lacks the group suffix — so it is refused too, not just misconfigured
    servers.
    """
    status = parse_result(await session.call_tool("get_status", {}))
    message = status.get("message", "")
    if f"default group: {GROUP_ID}" not in message:
        sys.exit(
            f"refusing to write: server at {EPG_MCP_URL} did not identify as the EPG "
            f"server (status message: {message!r})"
        )


async def write_airings(channels: list[str], hours: int, limit: int | None,
                        dry_run: bool) -> None:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)

    plan = []  # (channel_name, channel_id, airing)
    for name in channels:
        channel_id, airings = fetch_channel_epg(name)
        in_window = [a for a in airings if now <= a["start"] < horizon]
        print(f"{name}: {len(airings)} airings fetched, {len(in_window)} start in next {hours}h")
        plan.extend((name, channel_id, a) for a in in_window)

    state = load_state()
    new = [(n, cid, a) for n, cid, a in plan if airing_key(cid, a) not in state["airings"]]
    print(f"{len(plan)} in window, {len(new)} not yet in the graph")
    if limit is not None:
        new = new[:limit]
        print(f"--limit: writing at most {limit}")

    if dry_run:
        for name, cid, a in new:
            print(f"DRY {airing_key(cid, a)}\n    {build_fact(name, a)}")
        return

    if not new:
        return

    written = 0
    async with streamablehttp_client(EPG_MCP_URL, timeout=120) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await verify_epg_server(session)

            for name, cid, a in new:
                source = f"Channel: {name}"
                args = {
                    "source_node_name": source,
                    "edge_name": "AIRS",
                    "fact": build_fact(name, a),
                    "target_node_name": a["title"],
                    "group_id": GROUP_ID,
                    "valid_at": a["start"].astimezone(timezone.utc).isoformat(),
                    "invalid_at": a["stop"].astimezone(timezone.utc).isoformat(),
                    "direct": True,
                }
                if source in state["nodes"]:
                    args["source_node_uuid"] = state["nodes"][source]
                if a["title"] in state["nodes"]:
                    args["target_node_uuid"] = state["nodes"][a["title"]]

                data = parse_result(await session.call_tool("add_triplet", args))

                for node in data.get("nodes", []):
                    state["nodes"][node["name"]] = node["uuid"]
                edge = data["edges"][0]
                state["airings"][airing_key(cid, a)] = {
                    "edge_uuid": edge["uuid"],
                    "stop": a["stop"].astimezone(timezone.utc).isoformat(),
                    "desc_hash": hashlib.sha1(a["desc"].encode()).hexdigest(),
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                }
                written += 1
                if written % STATE_FLUSH_EVERY == 0:
                    save_state(state)

    save_state(state)
    print(f"wrote {written} airings to group '{GROUP_ID}' via {EPG_MCP_URL}")


async def clear_epg() -> None:
    """Wipe the EPG graph (group 'epg' only — hard-coded) and reset state."""
    async with streamablehttp_client(EPG_MCP_URL, timeout=120) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await verify_epg_server(session)
            data = parse_result(await session.call_tool("clear_graph", {"group_ids": "epg"}))
            print(data.get("message", data))
    if STATE_FILE.exists():
        backup = STATE_FILE.with_suffix(".json.cleared")
        os.replace(STATE_FILE, backup)
        print(f"state moved to {backup}")


async def show_status() -> None:
    async with streamablehttp_client(EPG_MCP_URL, timeout=60) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            status = parse_result(await session.call_tool("get_status", {}))
            print(status.get("message", status))
    state = load_state()
    print(f"state: {len(state['airings'])} airings, {len(state['nodes'])} node uuids cached")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--channel", action="append", default=[],
                   help="exact channel display name; repeatable")
    p.add_argument("--hours", type=int, default=24, help="window size (default 24)")
    p.add_argument("--limit", type=int, help="write at most N airings")
    p.add_argument("--dry-run", action="store_true", help="print what would be written")
    p.add_argument("--status", action="store_true", help="server + state summary")
    p.add_argument("--clear", action="store_true",
                   help="clear_graph(group 'epg') on the EPG server and reset state")
    args = p.parse_args()

    if args.status:
        asyncio.run(show_status())
    elif args.clear:
        with StateLock():
            asyncio.run(clear_epg())
    elif args.channel:
        with StateLock():
            asyncio.run(write_airings(args.channel, args.hours, args.limit, args.dry_run))
    else:
        p.error("nothing to do: pass --channel (repeatable), --status, or --clear")


if __name__ == "__main__":
    main()
