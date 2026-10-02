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
- **"What's on" questions go to `whats_on` — always.** It is the only
  tool that decides what is CURRENTLY AIRING; it compares times in code,
  so its rows are never stale. `whats_on()` for now, `whats_on(at_time=
  "9pm")` for later tonight, `channel=` for one channel, `query=` to
  filter by keyword ("Movie", "News", a title). NEVER assemble an
  on-air answer yourself from raw guide rows and NEVER read a start or
  end time off a row you filtered by hand — that is how already-finished
  programmes get announced as if they were on.
- The raw tools below remain for schedule questions the above can't
  answer (a channel's full evening line-up, "when is X on next",
  topical discovery).
- Speed rule: epg_list_memory_facts answers in milliseconds and is
  COMPLETE within its window — prefer it whenever completeness matters.
  epg_search_memory_facts (with or without time filters) takes well under
  a second and is RANKED — good for topics and titles, but its ranking is
  arbitrary: NEVER answer "when/next/now" questions from search results
  alone. Having found a programme's channel via search, get authoritative
  times from epg_list_memory_facts on that channel.
- Topical time questions ("anything about volcanoes on tonight") may use
  search WITH the window bounds (valid_at_after/before), max_facts 25 —
  then verify specific claims against a list if precision matters.
- "Is X on RIGHT NOW": `whats_on(query="X")` — one call, done.
  "When is the next X": search X unfiltered to find its channel, then
  epg_list_memory_facts on that channel from now forward and read the
  first row that starts after now.
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
- To act on a finding, switch to the TV tools as the TV-control section
  says.
