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
- Speed rule: epg_list_memory_facts answers in milliseconds;
  epg_search_memory_facts can take several seconds. For anything about a
  SPECIFIC CHANNEL (what's on it now, its schedule), prefer the list tool.
  Resolve the exact channel name first with tv_find_channel — lineup names
  match the guide's node names — then use
  source_node_name="Channel: <exact name>". Reserve search for topics and
  programme titles.
- On air RIGHT NOW on a channel: epg_list_memory_facts with
  valid_at_after = now minus four hours, valid_at_before = now plus five
  minutes, source_node_name set, then keep rows whose invalid_at is after
  now. With search instead, BOTH bounds are needed: valid_at_before=now
  AND invalid_at_after=now — valid_at alone misses shows that started
  earlier.
- Time window (search): valid_at_after=start, valid_at_before=end,
  max_facts 25-50.
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
- Freshness check when answers look thin: epg_search_memory_facts with
  query "airs", valid_at_after = now plus six hours, max_facts 1. Empty
  means the guide is stale — say so instead of answering from it.
- Coverage is the next 24 hours across the full lineup (about 1,075
  channels). Beyond that, say the guide doesn't reach that far.
- Descriptions are often boilerplate — don't infer episode content from
  them.
- To act on a finding, switch to the TV tools: tv_find_channel, then
  tv_tune_channel.
