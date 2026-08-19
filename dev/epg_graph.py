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
     fact:       "Al Jazeera (News) airs Newshour, Wed 19 Aug 17:00-18:00 PDT. ..."
     valid_at:   start (UTC)      invalid_at: stop (UTC)

Categories are channel metadata: the XMLTV tags minus storefront-brand labels
go into the fact (channel-side phrasing) and, at a channel node's first
creation, into its summary (fulltext-indexed) and attributes.

Every run: PRUNE expired ledger entries, RECONCILE future airings of the
fetched channels against the guide (ghosts and changed stop/desc/categories
are deleted and re-sent), then WRITE new airings in the window. One JSON line
per run is appended to ~/news/epg-graph-runs.log.

This script talks ONLY to the EPG server on 127.0.0.1:8001 and refuses any
server whose default group is not "epg" — pointed at the chat server (8000)
it would write LLM-mediated edges into Hermes' memory.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import sys
import time
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
RUN_LOG = Path.home() / "news" / "epg-graph-runs.log"
STATE_FLUSH_EVERY = 25
PRUNE_GRACE = timedelta(hours=1)
XMLTV_FMT = "%Y%m%d%H%M%S %z"

# Storefront/source brand labels that ride in <category> alongside real
# genres. Derived from the live guide 2026-08-19 (244 distinct values);
# re-check when the full lineup lands (step 4).
BRAND_DENYLIST = frozenset({
    "Bally Sports Live", "FreeLiveSports", "LG Channels", "Plex", "Pluto TV",
    "STIRR", "Samsung TV Plus", "TCL TV+", "The Roku Channel", "Tubi TV",
    "Vizio WatchFree+", "Whale TV+", "Xumo Play",
})


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


def log_run(entry: dict) -> None:
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RUN_LOG, "a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


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

def fetch_guide(params: dict) -> tuple[dict, list[dict]]:
    """Fetch a filtered guide; return (channel_id -> display name, airings).

    Airings carry the FULL guide horizon (~5 days) — windowing happens later;
    reconciliation wants everything the guide knows about the future.
    """
    url = f"{FC_BASE}/epg.xml?{urllib.parse.urlencode(params, doseq=True)}"
    id_to_name: dict[str, str] = {}
    airings: list[dict] = []
    with urllib.request.urlopen(url, timeout=300) as r:
        for _, el in ET.iterparse(r):
            if el.tag == "channel":
                name = el.findtext("display-name")
                if name:
                    id_to_name[el.get("id")] = name
                el.clear()
            elif el.tag == "programme":
                try:
                    start = datetime.strptime(el.get("start"), XMLTV_FMT)
                    stop = datetime.strptime(el.get("stop"), XMLTV_FMT)
                except (TypeError, ValueError):
                    el.clear()
                    continue
                title = (el.findtext("title") or "").strip()
                if title:
                    cats = sorted({c.text for c in el.findall("category")
                                   if c.text and c.text not in BRAND_DENYLIST})
                    airings.append({
                        "channel_id": el.get("channel"),
                        "start": start, "stop": stop, "title": title,
                        "desc": (el.findtext("desc") or "").strip(),
                        "cats": cats,
                    })
                el.clear()
    return id_to_name, [a for a in airings if a["channel_id"] in id_to_name]


def select_channels(selectors: argparse.Namespace) -> tuple[dict, list[dict]]:
    """Resolve --channel/--category selections into one channel map + airings."""
    id_to_name: dict[str, str] = {}
    airings: list[dict] = []
    for name in selectors.channel:
        ids, got = fetch_guide({"search": name})
        exact = {i: n for i, n in ids.items() if n.casefold() == name.casefold()}
        if len(exact) != 1:
            sys.exit(f"channel {name!r}: expected exactly one exact match, got "
                     f"{len(exact)} of {sorted(set(ids.values()))}")
        id_to_name.update(exact)
        airings.extend(a for a in got if a["channel_id"] in exact)
    for cat in selectors.category:
        ids, got = fetch_guide({"category": cat})
        print(f"category {cat}: {len(ids)} channels, {len(got)} airings in guide")
        id_to_name.update(ids)
        airings.extend(got)
    # de-dup airings if selections overlapped
    seen, unique = set(), []
    for a in airings:
        k = (a["channel_id"], a["title"], a["start"])
        if k not in seen:
            seen.add(k)
            unique.append(a)
    return id_to_name, unique


def build_fact(channel: str, a: dict) -> str:
    start_l, stop_l = a["start"].astimezone(), a["stop"].astimezone()
    tz = start_l.strftime("%Z")
    cats = f" ({', '.join(a['cats'])})" if a["cats"] else ""
    fact = f"{channel}{cats} airs {a['title']}, {start_l:%a %d %b %H:%M}-{stop_l:%H:%M} {tz}."
    if a["desc"]:
        fact += f" {a['desc']}"
    return fact


def airing_key(a: dict) -> str:
    return f"{a['channel_id']}|{a['title']}|{a['start'].astimezone(timezone.utc).isoformat()}"


def desc_hash(a: dict) -> str:
    return hashlib.sha1((a["desc"] + "\n" + ",".join(a["cats"])).encode()).hexdigest()


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
    """Refuse to run unless the server identifies its default group as epg."""
    status = parse_result(await asyncio.wait_for(session.call_tool("get_status", {}), 30))
    message = status.get("message", "")
    if f"default group: {GROUP_ID}" not in message:
        sys.exit(
            f"refusing to write: server at {EPG_MCP_URL} did not identify as the EPG "
            f"server (status message: {message!r})"
        )


class EpgClient:
    """MCP client with a per-call watchdog and reconnect-on-hang.

    A tool call whose HTTP connection silently dies would otherwise block the
    sequential loop forever (observed 2026-08-19: an ingest stalled 87 minutes
    on one call_tool await). Every call gets a hard timeout; a transport
    failure tears the session down, reconnects (re-running the identity
    check), and retries once.
    """

    def __init__(self):
        self._transport_cm = None
        self._session_cm = None
        self.session: ClientSession | None = None

    async def connect(self) -> None:
        await self.close()
        self._transport_cm = streamablehttp_client(EPG_MCP_URL, timeout=120)
        read, write, _ = await asyncio.wait_for(self._transport_cm.__aenter__(), 30)
        self._session_cm = ClientSession(read, write)
        self.session = await asyncio.wait_for(self._session_cm.__aenter__(), 30)
        await asyncio.wait_for(self.session.initialize(), 30)
        await verify_epg_server(self.session)

    async def close(self) -> None:
        for cm in (self._session_cm, self._transport_cm):
            if cm is not None:
                try:
                    await asyncio.wait_for(cm.__aexit__(None, None, None), 10)
                except Exception:
                    pass
        self._transport_cm = self._session_cm = None
        self.session = None

    async def call(self, name: str, args: dict, timeout: float = 90) -> dict:
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                if self.session is None:
                    await self.connect()
                res = await asyncio.wait_for(self.session.call_tool(name, args), timeout)
                return parse_result(res)
            except RuntimeError:
                raise  # server-side error response — retrying will not help
            except Exception as e:
                last = e
                print(f"  transport problem on {name} (attempt {attempt}): {e!r}"
                      " — reconnecting", file=sys.stderr, flush=True)
                await self.close()
        raise ConnectionError(f"{name} failed after reconnect+retry: {last!r}")


async def delete_edges(client: EpgClient, state: dict, keys: list[str],
                       what: str) -> int:
    deleted = 0
    for key in keys:
        rec = state["airings"][key]
        try:
            await client.call("delete_entity_edge", {"uuid": rec["edge_uuid"]})
            deleted += 1
        except Exception as e:
            print(f"  {what}: edge {rec['edge_uuid']} ({key}): {e} — evicting anyway",
                  file=sys.stderr, flush=True)
        del state["airings"][key]
    return deleted


# ------------------------------------------------------------------ the run

async def run(selectors: argparse.Namespace) -> None:
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=selectors.hours)
    counters = {"fetched": 0, "in_window": 0, "added": 0, "pruned": 0,
                "ghosts": 0, "changed": 0, "failed": 0}

    id_to_name, airings = select_channels(selectors)
    counters["fetched"] = len(airings)
    guide = {airing_key(a): a for a in airings}
    in_window = [a for a in airings if now <= a["start"] < horizon]
    counters["in_window"] = len(in_window)
    print(f"{len(id_to_name)} channels, {len(airings)} guide airings, "
          f"{len(in_window)} start in next {selectors.hours}h")

    state = load_state()

    # PRUNE: ledger entries whose airing ended more than the grace period ago.
    prune_keys = [k for k, rec in state["airings"].items()
                  if datetime.fromisoformat(rec["stop"]) < now - PRUNE_GRACE]

    # RECONCILE future airings of the channels in scope.
    ghost_keys, changed_keys = [], []
    for key, rec in state["airings"].items():
        if key in prune_keys:
            continue
        # key = channel_id|title|start_iso; the TITLE may itself contain "|"
        # (seen live: "gamescom 2025 | opening night"), so parse from both ends
        channel_id, rest = key.split("|", 1)
        start_iso = rest.rsplit("|", 1)[1]
        if channel_id not in id_to_name or datetime.fromisoformat(start_iso) <= now:
            continue
        a = guide.get(key)
        if a is None:
            ghost_keys.append(key)
        elif (rec["stop"] != a["stop"].astimezone(timezone.utc).isoformat()
              or rec["desc_hash"] != desc_hash(a)):
            changed_keys.append(key)

    new = [a for a in in_window if airing_key(a) not in state["airings"]
           or airing_key(a) in changed_keys]
    # changed airings are deleted below, then re-written
    if selectors.limit is not None:
        new = new[:selectors.limit]

    print(f"plan: prune {len(prune_keys)}, ghosts {len(ghost_keys)}, "
          f"changed {len(changed_keys)}, write {len(new)}")
    if selectors.dry_run:
        for k in ghost_keys:
            print(f"DRY ghost  {k}")
        for k in changed_keys:
            print(f"DRY changed {k}")
        for a in new[:20]:
            print(f"DRY write  {airing_key(a)}\n    {build_fact(id_to_name[a['channel_id']], a)}")
        if len(new) > 20:
            print(f"DRY … and {len(new) - 20} more writes")
        return

    # channel-level categories for node summaries: union over this run's airings
    chan_cats: dict[str, set] = {}
    for a in airings:
        chan_cats.setdefault(a["channel_id"], set()).update(a["cats"])

    client = EpgClient()
    consecutive_transport = 0
    try:
        await client.connect()

        counters["pruned"] = await delete_edges(client, state, prune_keys, "prune")
        counters["ghosts"] = await delete_edges(client, state, ghost_keys, "ghost")
        counters["changed"] = await delete_edges(client, state, changed_keys, "changed")
        save_state(state)

        written = 0
        for a in new:
            name = id_to_name[a["channel_id"]]
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
            else:
                cats = sorted(chan_cats.get(a["channel_id"], set()))
                if cats:
                    args["source_node_summary"] = f"TV channel. Category: {', '.join(cats)}."
                    args["source_node_attributes"] = {"type": "channel", "categories": cats}
                else:
                    args["source_node_summary"] = "TV channel."
                    args["source_node_attributes"] = {"type": "channel"}
            if a["title"] in state["nodes"]:
                args["target_node_uuid"] = state["nodes"][a["title"]]

            try:
                data = await client.call("add_triplet", args)
                consecutive_transport = 0
            except ConnectionError as e:
                counters["failed"] += 1
                consecutive_transport += 1
                print(f"  write failed: {airing_key(a)}: {e}", file=sys.stderr, flush=True)
                if consecutive_transport >= 3:
                    print("aborting run: 3 consecutive transport failures",
                          file=sys.stderr, flush=True)
                    break
                continue
            except Exception as e:
                msg = str(e)
                retryable = False
                if "Failed to load node" in msg:
                    # A cached node uuid went stale (node removed by a sweep or
                    # reset). Forget exactly the offending cache entry and
                    # retry once, recreating that node.
                    if args.get("source_node_uuid") and args["source_node_uuid"] in msg:
                        args.pop("source_node_uuid")
                        state["nodes"].pop(source, None)
                        cats = sorted(chan_cats.get(a["channel_id"], set()))
                        args["source_node_summary"] = (
                            f"TV channel. Category: {', '.join(cats)}." if cats
                            else "TV channel.")
                        args["source_node_attributes"] = (
                            {"type": "channel", "categories": cats} if cats
                            else {"type": "channel"})
                        retryable = True
                    if args.get("target_node_uuid") and args["target_node_uuid"] in msg:
                        args.pop("target_node_uuid")
                        state["nodes"].pop(a["title"], None)
                        retryable = True
                if not retryable:
                    counters["failed"] += 1
                    print(f"  write failed: {airing_key(a)}: {e}", file=sys.stderr, flush=True)
                    continue
                try:
                    data = await client.call("add_triplet", args)
                    consecutive_transport = 0
                except Exception as e2:
                    counters["failed"] += 1
                    print(f"  write failed after stale-uuid retry: {airing_key(a)}: {e2}",
                          file=sys.stderr, flush=True)
                    continue

            for node in data.get("nodes", []):
                state["nodes"][node["name"]] = node["uuid"]
            edge = data["edges"][0]
            state["airings"][airing_key(a)] = {
                "edge_uuid": edge["uuid"],
                "stop": a["stop"].astimezone(timezone.utc).isoformat(),
                "desc_hash": desc_hash(a),
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }
            written += 1
            if written % STATE_FLUSH_EVERY == 0:
                save_state(state)
            if written % 200 == 0:
                print(f"  … {written}/{len(new)} written", flush=True)
        counters["added"] = written
    finally:
        await client.close()

    save_state(state)
    counters["wall_s"] = round(time.monotonic() - t0, 1)
    log_run({"ts": now.isoformat(), "selectors": vars(selectors)["channel"]
             + [f"category:{c}" for c in selectors.category], **counters})
    print(f"added {counters['added']}, pruned {counters['pruned']}, "
          f"ghosts {counters['ghosts']}, changed {counters['changed']}, "
          f"failed {counters['failed']} in {counters['wall_s']}s")
    if counters["failed"]:
        sys.exit(1)


async def prune_only(dry_run: bool) -> None:
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    state = load_state()
    prune_keys = [k for k, rec in state["airings"].items()
                  if datetime.fromisoformat(rec["stop"]) < now - PRUNE_GRACE]
    print(f"plan: prune {len(prune_keys)} of {len(state['airings'])}")
    if dry_run:
        return
    pruned = 0
    if prune_keys:
        client = EpgClient()
        try:
            await client.connect()
            pruned = await delete_edges(client, state, prune_keys, "prune")
        finally:
            await client.close()
        save_state(state)
    log_run({"ts": now.isoformat(), "selectors": ["prune-only"], "fetched": 0,
             "in_window": 0, "added": 0, "pruned": pruned, "ghosts": 0,
             "changed": 0, "failed": 0, "wall_s": round(time.monotonic() - t0, 1)})
    print(f"pruned {pruned}")


async def clear_epg() -> None:
    """Wipe the EPG graph (group 'epg' only — hard-coded) and reset state."""
    client = EpgClient()
    try:
        await client.connect()
        data = await client.call("clear_graph", {"group_ids": "epg"}, timeout=120)
        print(data.get("message", data))
    finally:
        await client.close()
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
    p.add_argument("--category", action="append", default=[],
                   help="FastChannels channel category (exact, e.g. Sports); repeatable")
    p.add_argument("--hours", type=int, default=24, help="window size (default 24)")
    p.add_argument("--limit", type=int, help="write at most N airings")
    p.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    p.add_argument("--prune", action="store_true", help="prune expired entries only")
    p.add_argument("--status", action="store_true", help="server + state summary")
    p.add_argument("--clear", action="store_true",
                   help="clear_graph(group 'epg') on the EPG server and reset state")
    args = p.parse_args()

    if args.status:
        asyncio.run(show_status())
    elif args.clear:
        with StateLock():
            asyncio.run(clear_epg())
    elif args.prune:
        with StateLock():
            asyncio.run(prune_only(args.dry_run))
    elif args.channel or args.category:
        with StateLock():
            asyncio.run(run(args))
    else:
        p.error("nothing to do: pass --channel/--category, --prune, --status, or --clear")


if __name__ == "__main__":
    main()
