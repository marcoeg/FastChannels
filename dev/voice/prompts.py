"""Prompt assembly for the standalone voice agent.

Static sections live in prompts/*.md, concatenated in filename order. They
load ONCE at startup — no hot reload — so the instructions prefix is
byte-identical across turns and OpenAI prompt caching stays effective.
Restart the worker after editing a prompt file.

Dynamic blocks (time context, memory context) are appended per turn by
@agent.instructions functions in pai_agent.py, AFTER the static prefix.

Debug: `./prompts.py --check` prints the assembled static prompt and a
rough token estimate.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROMPTS_DIR = Path(__file__).parent / "prompts"
_PACIFIC = ZoneInfo("America/Los_Angeles")


def load_static_instructions(backend: str = "minny",
                             prompts_dir: Path = PROMPTS_DIR,
                             player: str = "tvshell") -> str:
    """Shared sections (prompts/*.md, sorted), the active player's control
    section (prompts/tv/<player>.md) and the active guide backend's section
    (prompts/epg/<backend>.md). Players and guide backends are each
    alternatives — exactly one of each is ever loaded."""
    parts = [p.read_text(encoding="utf-8").strip()
             for p in sorted(prompts_dir.glob("*.md"))]
    if not parts:
        raise FileNotFoundError(f"no prompt sections in {prompts_dir}")
    tv = prompts_dir / "tv" / f"{player}.md"
    if not tv.exists():
        raise FileNotFoundError(f"no TV prompt for VOICE_TV={player!r}: {tv}")
    parts.append(tv.read_text(encoding="utf-8").strip())
    epg = prompts_dir / "epg" / f"{backend}.md"
    if not epg.exists():
        raise FileNotFoundError(
            f"no guide prompt for VOICE_EPG_BACKEND={backend!r}: {epg}")
    text = epg.read_text(encoding="utf-8").strip()
    if player == "ytvn":
        text = _ytvn_vod(text)
    parts.append(text)
    return "\n\n".join(parts)


# The ytvn TV hides minny_search_vod (its on-demand catalogue is the TV's
# own); the guide section's routes to it cost an unknown-tool retry.
_YTVN_VOD = {
    'To stream instead → `minny_search_vod` `kind="movie"`.':
        "On demand instead → `tv_on_demand`.",
    "Watch a movie / something to stream / a title with no time →\n    `minny_search_vod`.":
        "Watch a movie / something to stream / a title with no time →\n"
        "    `tv_watch_title` (a named title) or `tv_on_demand` (browsing).",
    "for a\n  title the viewer wants to watch, try `minny_search_vod`, and":
        "for a\n  title the viewer wants to watch, try `tv_watch_title`, and",
}


def _ytvn_vod(text: str) -> str:
    for old, new in _YTVN_VOD.items():
        text = text.replace(old, new)
    if "minny_search_vod" in text:
        logging.getLogger("voice.prompts").warning(
            "guide prompt still routes to minny_search_vod in ytvn mode")
    return text


# How far ahead each guide reaches. The local FalkorDB guide holds the next
# 24 h; the Minny catalogue is ingested about a week ahead (only its default
# "upcoming" window is 24 h) — telling the model "24 h" there made it decline
# "tomorrow night" questions a range search would answer.
COVERAGE = {"falkordb": "Guide coverage ends roughly 24 hours from now.",
            "minny": "The guide reaches about a week ahead; an empty result "
                     "more than a few days out may mean the guide doesn't "
                     "reach that far yet — say so rather than 'nothing is on'."}


def format_time_context(now_utc: datetime | None = None,
                        tz: ZoneInfo = _PACIFIC, backend: str = "falkordb") -> str:
    """Current time in the viewer's zone, computed from zoneinfo — the model
    builds EPG bounds and speaks in that zone, and DST means the offset can't be
    hardcoded. `tz` MUST match the active backend's catalogue zone (Eastern for
    the salesd/minny catalogue, Pacific for the local guide); otherwise the model
    miscomputes 'now'/'today'/'tonight' and builds windows in the wrong offset."""
    now_utc = now_utc or datetime.now(timezone.utc)
    local = now_utc.astimezone(tz)
    tzname = local.tzname()  # e.g. EDT / EST / PDT / PST
    offset_h = int(local.utcoffset().total_seconds() // 3600)
    # Both notations on purpose: guide fact text uses 24-hour times
    # ("19:10-20:57 EDT") and a no-reasoning model must be able to compare
    # them to "now" without doing 12h->24h arithmetic itself.
    return (
        f"Clock: {local:%A, %B %-d, %Y, %-I:%M %p} {tzname} — that is "
        f"{local:%H:%M} in the 24-hour times the guide uses "
        f"(UTC{offset_h:+d}). Speak times in this zone; never say UTC.\n"
        f"UTC now: {now_utc:%Y-%m-%dT%H:%M:%S}Z. "
        f"{COVERAGE.get(backend, COVERAGE['falkordb'])}\n"
        f"A programme is on air now exactly when its start time is at or "
        f"before {local:%H:%M} and its end time is after {local:%H:%M} "
        f"(same day: {local:%a %d %b}). Build every time window "
        f"(now/today/tonight/range) in this zone, offset UTC{offset_h:+d}."
        f"{_dst_note(local, tz)}"
    )


def _dst_note(local: datetime, tz: ZoneInfo) -> str:
    """Empty except today and the day before the clocks change: one offset
    for every window put 'tonight' an hour off across the change."""
    for add, when in ((0, "today"), (1, "tomorrow")):
        d = (local + timedelta(days=add)).date()
        a = datetime.combine(d, dtime(0, 0), tz).utcoffset()
        b = datetime.combine(d, dtime(23, 59), tz).utcoffset()
        if a != b and local.utcoffset() != b:      # not already changed
            return (f" Clocks change at 2 AM {when}: from then the offset is "
                    f"UTC{int(b.total_seconds() // 3600):+d} — use it for times "
                    "after the change.")
    return ""


def format_memory_context(profile: list[str], recent: list[str]) -> str:
    """Empty string when there is nothing to say — no 'memory is empty'
    noise in the prompt."""
    profile = profile[:12]
    recent = recent[:4]
    if not profile and not recent:
        return ""
    lines: list[str] = []
    if profile:
        lines.append("Known about this household:")
        lines += [_clip(f) for f in profile]
    if recent:
        lines.append("Earlier sessions (most recent first):")
        lines += [_clip(r) for r in recent]
    return "\n".join(lines)


def _clip(line: str, limit: int = 200) -> str:
    line = " ".join(line.split())
    if not line.startswith("- "):
        line = "- " + line
    return line[:limit]


if __name__ == "__main__":
    import sys

    text = load_static_instructions()
    if "--check" in sys.argv:
        print(text)
        print("\n---")
        print(format_time_context())
        print(f"[static: {len(text)} chars ≈ {len(text) // 4} tokens]")
