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

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROMPTS_DIR = Path(__file__).parent / "prompts"
_PACIFIC = ZoneInfo("America/Los_Angeles")


def load_static_instructions(prompts_dir: Path = PROMPTS_DIR) -> str:
    parts = [p.read_text(encoding="utf-8").strip()
             for p in sorted(prompts_dir.glob("*.md"))]
    if not parts:
        raise FileNotFoundError(f"no prompt sections in {prompts_dir}")
    return "\n\n".join(parts)


def format_time_context(now_utc: datetime | None = None) -> str:
    """Both zones, computed from zoneinfo — the model builds EPG bounds in
    UTC but speaks Pacific, and DST means the offset can't be hardcoded."""
    now_utc = now_utc or datetime.now(timezone.utc)
    local = now_utc.astimezone(_PACIFIC)
    tzname = local.tzname()  # PDT or PST
    offset_h = int(local.utcoffset().total_seconds() // 3600)
    return (
        f"Clock: {local:%A, %B %-d, %Y, %-I:%M %p} Pacific "
        f"({tzname}, UTC{offset_h:+d}).\n"
        f"UTC now: {now_utc:%Y-%m-%dT%H:%M:%S}Z. "
        f"Guide coverage ends roughly 24 hours from now."
    )


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
