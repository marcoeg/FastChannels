"""Which LiveKit rooms are test traffic. Kept free of heavy imports: the
worker decides test mode before memory (graphiti, FalkorDB) loads, and a
broken memory install must leave voice running."""

from __future__ import annotations


def is_test_room(room: str | None) -> bool:
    """Test traffic: rooms named test-… (the phone client opened with ?test),
    voice_probe.py's voice-probe… rooms and the debug console. Their turns are
    traced but marked, their memory is a separate graph, and their exchanges
    never reach a real session's "earlier sessions" (nor the reverse)."""
    room = room or ""
    # `voice_agent.py console` runs in LiveKit's mock room "console": the
    # household graph got two facts typed there that had to be scrubbed.
    return room.startswith(("test-", "voice-probe", "console"))
