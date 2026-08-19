---
name: epg-graph
description: TV listings — what is on now, tonight, or when a programme airs. Fast temporal search over the EPG knowledge graph (mcp__epg_graph__* tools); check here before parsing guide XML.
---

# TV guide search — the EPG knowledge graph

A separate knowledge graph holding FastChannels guide airings, one fact per
broadcast, queried through the `mcp__epg_graph__*` tools. It runs on its own
MCP server — it is **not** your memory: `graphiti_memory` and `recall` know
nothing about TV listings, and this graph knows nothing about your
conversations.

```
Tools     mcp__epg_graph__search_memory_facts   (primary)
          mcp__epg_graph__search_nodes
          mcp__epg_graph__get_status
Group     every call MUST pass group_ids: ["epg"]
Fact      "Al Jazeera airs Newshour, Wed 19 Aug 17:00-18:00 PDT. <description>"
          valid_at = start (UTC)   invalid_at = stop (UTC)
Channels  channel nodes are named "Channel: <name>" (e.g. "Channel: Al Jazeera")
```

## When to use this

- "when is X on", "what time does X air", "is X on tonight"
- "what is on <channel> at <time> / right now"
- "is anything about <topic> airing"

Not this skill: *recording* is `fastchannels-record`; *stream URLs and
channel lineup* are `fastchannels`; anything about past conversations or the
system is your normal memory.

## Query patterns

All timestamps must be **UTC** (or carry an explicit offset) — naive strings
are treated as UTC. The fact text speaks local Pacific time; the filters do
not.

**On air RIGHT NOW** — both bounds are required; `valid_at` alone cannot find
a programme that started an hour ago and is still on:

```
search_memory_facts(query="<channel or topic>", group_ids=["epg"],
                    valid_at_before=<now UTC>, invalid_at_after=<now UTC>,
                    max_facts=25)
```

**A time window** ("tonight 8–11pm"): convert the window to UTC first:

```
search_memory_facts(query="<topic, title or channel>", group_ids=["epg"],
                    valid_at_after=<window start UTC>,
                    valid_at_before=<window end UTC>, max_facts=50)
```

**Next airing of a programme**:

```
search_memory_facts(query="<title>", group_ids=["epg"],
                    valid_at_after=<now UTC>, max_facts=25)
```

`max_facts` defaults to 10 — use 25–50 for windows.

## Limits — read before answering anyone

1. **This is ranked search, not a schedule listing.** Results are the top
   matches for your query text inside the time bounds. Absence from results
   is NEVER evidence a programme is not airing. For an exhaustive "everything
   on tonight" listing, use the `fastchannels` skill (`/epg.xml`) instead,
   and say which source you used.
2. **Check freshness before trusting results.** Run:
   `search_memory_facts(query="airs", group_ids=["epg"], valid_at_after=<now+6h UTC>, max_facts=1)`
   — the word "airs" appears in every fact, so an empty result means the
   graph has less than six hours of future coverage: say it is stale and fall
   back to the `fastchannels` skill.
3. **Coverage is currently a pilot**: Al Jazeera and TRT World, next ~48
   hours, refreshed manually. Only future airings are ingested, so a
   programme already in progress when ingestion ran may be missing.
4. The graph answers **when things air**, not what a news episode is about —
   guide descriptions are often boilerplate (Al Jazeera's "News Live" airs
   dozens of times with one shared description).
5. You cannot write to this graph, and never pass any group other than
   `"epg"`.
