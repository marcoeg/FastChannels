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
Tools     mcp__epg_graph__search_memory_facts   (topical & title search, ranked)
          mcp__epg_graph__list_memory_facts     (complete schedule listings)
          mcp__epg_graph__search_nodes          (find channels by category)
          mcp__epg_graph__get_status
Group     every call MUST pass group_ids: ["epg"] (list_memory_facts: group_id="epg")
Fact      "Al Jazeera (News) airs Newshour, Wed 19 Aug 17:00-18:00 PDT. <description>"
          valid_at = start (UTC)   invalid_at = stop (UTC)
          the (…) after the channel holds its categories/genres — so genre
          words like "basketball" match in searches
Channels  channel nodes are named "Channel: <name>" (e.g. "Channel: Al Jazeera")
          and carry a category summary — search_nodes can find "news channels"
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

**A COMPLETE listing for a window** — a channel's schedule, or everything on
tonight. This ENUMERATES every airing ordered by start time (no ranking, no
query text), so use it whenever completeness matters:

```
list_memory_facts(valid_at_after=<window start UTC>,
                  valid_at_before=<window end UTC>,
                  group_id="epg", edge_name="AIRS",
                  source_node_name="Channel: <name>",   # optional filter
                  max_facts=250, offset=0)
```

Page by raising `offset` in steps of `max_facts` until fewer rows return. For
genre listings ("all basketball tonight"), list the window and filter by the
`(…)` genres in each fact yourself, or combine with a `search_memory_facts`
pass for topical matches in descriptions. The tool selects by START time —
for "overlapping a window" questions (films still running at 8 PM), set
`valid_at_after` to ~4 hours BEFORE the window start and keep rows whose
`invalid_at` is after it.

## Limits — read before answering anyone

1. **`search_memory_facts` is ranked search — never treat it as a listing.**
   Its results are the top matches for your query text; absence from them is
   NEVER evidence a programme is not airing. When completeness matters, use
   `list_memory_facts` (complete by construction within its window). For
   genre questions, `list_memory_facts` + your own filtering on the fact's
   `(…)` genres beats repeated searches; add a `search_memory_facts` pass to
   catch topic words hiding in descriptions. Always say which tool produced
   a listing if there is any chance it is partial.
2. **Check freshness before trusting results.** Run:
   `search_memory_facts(query="airs", group_ids=["epg"], valid_at_after=<now+6h UTC>, max_facts=1)`
   — the word "airs" appears in every fact, so an empty result means the
   graph has less than six hours of future coverage: say it is stale and fall
   back to the `fastchannels` skill.
3. **Coverage**: the full FastChannels lineup (~870 guide channels), next
   24 hours, refreshed hourly by a systemd timer (minute :20). Only future
   airings are ingested, so a programme already in progress when its first
   ingest ran may be missing.
4. The graph answers **when things air**, not what a news episode is about —
   guide descriptions are often boilerplate (Al Jazeera's "News Live" airs
   dozens of times with one shared description).
5. You cannot write to this graph, and never pass any group other than
   `"epg"`.
