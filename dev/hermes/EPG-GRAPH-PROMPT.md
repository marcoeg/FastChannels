# Introducing the `epg-graph` skill to Hermes

Paste everything below the rule to onboard the agent. It assumes the
`fastchannels` and `fastchannels-record` skills are already installed.
Deliberately short — the skill file carries the detail; this exists to make
the agent load it, place it correctly among its TV skills, and prove the
tools work.

---

You have a third TV skill, `epg-graph`, alongside `fastchannels` and
`fastchannels-record`. Its skill file is in your skills index — read it
before answering any TV-listings question.

**What it is.** A knowledge graph of TV guide airings, one fact per
broadcast with machine-readable start/stop bounds, served by a dedicated MCP
server that appears to you as the `mcp__epg_graph__*` tools. It is separate
from your memory: `graphiti_memory` and `recall` know nothing about TV.

**How the three TV skills divide — check in this order:**

1. `epg-graph` — targeted schedule questions: "when is X on", "what's on
   <channel> right now / tonight", "is anything about Y airing". Try it
   first for these.
2. `fastchannels` — the HTTP API: channel lineups, stream URLs, exhaustive
   "everything on tonight" listings, and any channel the graph does not
   cover.
3. `fastchannels-record` — keeping a copy of a broadcast.

Five rules that matter:

1. Every `mcp__epg_graph__*` call passes `group_ids: ["epg"]`, and all time
   bounds are UTC (naive strings are treated as UTC; answer people in
   Pacific time).
2. "Right now" needs BOTH bounds: `valid_at_before=<now>` AND
   `invalid_at_after=<now>` — `valid_at` alone misses programmes already in
   progress.
3. It is ranked search, not enumeration. Absence from results is never
   evidence something is not airing — fall back to the `fastchannels` skill
   and say which source you used.
4. Before trusting results, run the freshness probe from the skill file; if
   the graph is stale, say so and fall back.
5. Coverage today is a pilot: Al Jazeera and TRT World, about 48 hours
   ahead. Everything else is `fastchannels` territory for now.

To confirm the skill and tools work, run these and report what you get:

1. `mcp__epg_graph__get_status` — expect the message to end with
   "default group: epg".
2. A right-now query for TRT World (both time bounds, as above).
3. "When is Newshour next on Al Jazeera?" — answered from the graph.
4. Then tell me, in your own words, which of the three skills you would
   reach for: to record Newshour tonight; to get a stream URL for CNN; to
   say what is on TRT World at 9pm.
