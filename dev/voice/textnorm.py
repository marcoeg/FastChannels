"""Speech text hygiene for the voice pipeline.

Copied from hermes_bridge.py (not imported) so the frozen Hermes fallback
path and the pydantic-ai path stay fully independent of each other.

Known limitation (accepted, same as the original): patterns are applied per
delta, so a markdown token split across two deltas is not stripped.
"""

from __future__ import annotations

import re

FILLER = "One moment, let me check."

_MD_PATTERNS = [
    (re.compile(r"```.*?```", re.S), " "),      # fenced code
    (re.compile(r"`([^`]*)`"), r"\1"),           # inline code
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),  # links -> text
    (re.compile(r"^#{1,6}\s*", re.M), ""),       # headings
    (re.compile(r"^\s*[-*•]\s+", re.M), ""),     # bullets
    (re.compile(r"[|*_>#]"), " "),               # table pipes, emphasis, quotes
    (re.compile(r"\s{2,}"), " "),
]


def sanitize(text: str) -> str:
    for pat, repl in _MD_PATTERNS:
        text = pat.sub(repl, text)
    return text
