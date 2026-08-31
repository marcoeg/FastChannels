# The TV guide (epg_ tools) — the schedule, not the TV

One fact per airing, like: "Al Jazeera (News) airs Newshour, Wed 19 Aug
17:00-18:00 PDT. <description>". valid_at = start (UTC), invalid_at = stop
(UTC). The parentheses after a channel name hold its genres, so genre words
match in searches. Channel nodes are named "Channel: <name>".

- Every epg_search_memory_facts / epg_search_nodes call passes
  group_ids=["epg"]; epg_list_memory_facts takes group_id="epg". Never any
  other group.
- Filter timestamps are UTC (build them from the UTC clock below); fact
  text and your answers speak Pacific.
- Speed rule: epg_list_memory_facts answers in milliseconds and is
  complete; epg_search_memory_facts is slower and NEVER takes time
  filters — passing valid_at/invalid_at parameters to search makes it
  time out (server bug). Anything time-scoped goes through the list tool.
- Search = title/topic DISCOVERY only, no filter parameters,
  max_facts 25: it tells you WHICH channels air a programme, but its
  ranking is arbitrary — NEVER answer "when/next/now" questions from
  search results alone. Having found the channel, get the times from
  epg_list_memory_facts on that channel and answer from those rows.
- "Is X on RIGHT NOW" / "when is the next X": search X unfiltered to find
  its channel(s), then epg_list_memory_facts with
  source_node_name="Channel: <name>", valid_at_after = now minus four
  hours, valid_at_before = now plus twenty-four hours. The first row
  whose invalid_at is after now is on air; the first row whose valid_at
  is after now is the next airing.
- For a SPECIFIC CHANNEL's schedule, resolve the exact name first with
  tv_find_channel — lineup names match the guide's node names — then
  epg_list_memory_facts with source_node_name="Channel: <exact name>".
- epg_search_memory_facts is RANKED search — absence proves nothing. When
  completeness matters (a channel's evening schedule, everything on
  tonight, genre listings) use epg_list_memory_facts: edge_name="AIRS",
  optional source_node_name="Channel: <name>", the UTC window,
  max_facts 250, and page by raising offset until fewer rows return. For
  genres, filter the (…) after channel names yourself; add a search pass
  for topic words in descriptions.
- Shows overlapping a window (still on at eight): the tools select by START
  time, so set valid_at_after about four hours before the window start and
  keep rows whose invalid_at falls after it.
- Freshness check when answers look thin: epg_list_memory_facts with
  valid_at_after = now plus six hours, valid_at_before = now plus twelve
  hours, max_facts 1. Empty means the guide is stale — say so instead of
  answering from it.
- Coverage is the next 24 hours across the full lineup (about 1,075
  channels). Beyond that, say the guide doesn't reach that far.
- Descriptions are often boilerplate — don't infer episode content from
  them.
- To act on a finding, switch to the TV tools: tv_find_channel, then
  tv_tune_channel.
