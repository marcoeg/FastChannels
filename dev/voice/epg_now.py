"""Deterministic "what is on at time T" over the EPG graph.

Why this exists: asking the model to fetch an overlap window and keep the
rows still running is a reliable source of wrong answers — luna failed it
outright, and terra silently skipped the filter on long lists and read out
films that had already ended (2026-09-12, 18:57). Time comparison is
arithmetic, not judgement, so it belongs in Python.

This calls the EPG MCP server's list_memory_facts (the cheap, complete,
~20 ms tool), then filters and formats in code. The model only ever sees
programmes that are genuinely on air at the requested moment, with times
already rendered in Pacific.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

logger = logging.getLogger("voice.epg_now")

PACIFIC = ZoneInfo("America/Los_Angeles")
TIMEOUT_S = 8.0
LOOKBACK_H = 6           # longest plausible programme, for overlap capture
DESC_CHARS = 90
MIN_REMAINING_S = 60     # a programme with seconds left is not "on"
PAGE_SIZE = 1000         # server maximum per call
MAX_PAGES = 12           # ~12k rows: a 6 h window over the full lineup

_HDR = {"Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"}


def _payload(body: str) -> dict:
    """Streamable HTTP returns SSE frames; the payload rides on a data: line."""
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise RuntimeError(f"unexpected MCP reply: {body[:200]}")


async def _call_list_facts(window_start: datetime, window_end: datetime,
                           channel: str | None) -> list[dict]:
    """Page through the whole window.

    The server caps a page at 1000 rows ordered by start time, and a
    multi-hour window across ~1,075 channels exceeds that easily — taking
    only the first page silently returns the OLDEST rows, every one of
    them already finished. Paging is what makes "at 9pm" work at all.
    """
    url = os.environ.get("EPG_MCP_URL", "http://127.0.0.1:8001/mcp")
    base = {
        "group_id": "epg",
        "edge_name": "AIRS",
        "valid_at_after": window_start.strftime("%Y-%m-%dT%H:%M:%S"),
        "valid_at_before": window_end.strftime("%Y-%m-%dT%H:%M:%S"),
        "max_facts": PAGE_SIZE,
    }
    if channel:
        base["source_node_name"] = f"Channel: {channel}"

    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    facts: list[dict] = []
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.post(url, headers=_HDR, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "voice-agent", "version": "1"}}
        }) as resp:
            sid = resp.headers.get("mcp-session-id")
            await resp.read()
        hdr = dict(_HDR)
        if sid:
            hdr["mcp-session-id"] = sid
        async with http.post(url, headers=hdr, json={
                "jsonrpc": "2.0", "method": "notifications/initialized"}) as r:
            await r.read()

        for page in range(MAX_PAGES):
            args = dict(base, offset=page * PAGE_SIZE)
            async with http.post(url, headers=hdr, json={
                    "jsonrpc": "2.0", "id": 2 + page, "method": "tools/call",
                    "params": {"name": "list_memory_facts", "arguments": args}
            }) as resp:
                body = await resp.text()
            payload = _payload(body)
            if "result" not in payload:
                raise RuntimeError(f"unexpected MCP reply: {body[:200]}")
            inner = json.loads(payload["result"]["content"][0]["text"])
            batch = inner.get("facts", [])
            facts.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        else:
            logger.warning("epg window hit the %d-page cap", MAX_PAGES)
    return facts


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve_when(at_time: str | None, now: datetime) -> datetime:
    """'now' | 'HH:MM' | '9pm' -> absolute instant, next occurrence."""
    if not at_time or at_time.strip().lower() in ("now", "right now", ""):
        return now
    s = at_time.strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return now
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif not ampm and hour < 12:
        # Bare hours in a TV conversation mean the evening far more often
        # than the small hours ("put the news on at 6").
        hour += 12 if hour >= 1 else 0
    local = now.astimezone(PACIFIC).replace(hour=hour % 24, minute=minute,
                                            second=0, microsecond=0)
    if local < now.astimezone(PACIFIC) - timedelta(minutes=5):
        local += timedelta(days=1)          # already passed -> tomorrow
    return local.astimezone(timezone.utc)


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone(PACIFIC).strftime("%-I:%M %p").lower()


async def whats_on(at_time: str | None = None, channel: str | None = None,
                   query: str | None = None, max_results: int = 30) -> str:
    now = datetime.now(timezone.utc)
    when = resolve_when(at_time, now)

    try:
        facts = await _call_list_facts(
            when - timedelta(hours=LOOKBACK_H), when + timedelta(minutes=1),
            channel)
    except Exception as e:  # noqa: BLE001
        logger.warning("epg now lookup failed: %s", e)
        return "I couldn't check the guide just now."

    rows = []
    for f in facts:
        start, stop = _parse(f.get("valid_at")), _parse(f.get("invalid_at"))
        if not start or start > when:
            continue
        if stop and (stop - when).total_seconds() <= MIN_REMAINING_S:
            continue                        # finished, or all but finished
        text = f.get("fact", "")
        if query and query.lower() not in text.lower():
            continue
        m = re.match(r"^(.*?) \((.*?)\) airs (.*?), \w{3} \d+ \w{3} "
                     r"[\d:]+-[\d:]+ \w+\.?\s*(.*)$", text)
        if m:
            chan, genres, title, desc = m.groups()
        else:
            chan, genres, title, desc = "", "", text[:60], ""
        rows.append({"chan": chan, "genres": genres, "title": title,
                     "desc": desc[:DESC_CHARS], "stop": stop})

    if not rows:
        scope = f" on {channel}" if channel else ""
        return (f"Nothing{scope} matches that at {_fmt_time(when)}."
                if query or channel else
                f"The guide has nothing listed for {_fmt_time(when)}.")

    rows.sort(key=lambda r: (r["stop"] or when))
    label = ("On air now" if when == now else f"On air at {_fmt_time(when)}")
    out = [f"{label} ({_fmt_time(when)} Pacific) — "
           f"{len(rows)} match(es), showing up to {max_results}:"]
    for r in rows[:max_results]:
        until = f", until {_fmt_time(r['stop'])}" if r["stop"] else ""
        genres = f" [{r['genres']}]" if r["genres"] else ""
        desc = f" — {r['desc']}" if r["desc"] else ""
        out.append(f"- {r['chan']}{genres}: {r['title']}{until}{desc}")
    return "\n".join(out)
