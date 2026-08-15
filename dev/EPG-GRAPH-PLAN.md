# Proposal: a separate Graphiti graph for the EPG

**Status: for evaluation, not agreed. Nothing here has been built.**

Hermes runs Graphiti + FalkorDB. The idea is a **separate graph holding the
next day of guide data, refreshed hourly**, so that EPG search goes through the
same retrieval surface as memory instead of a second mechanism.

---

## The objection I raised, and why it was wrong

I argued this would be too expensive, assuming every airing would be an
`add_memory` episode triggering `gpt-5.5` entity extraction. At ~34,000 airings
a day that would be a serious recurring bill.

The assumption was wrong. **`add_triplet` bypasses extraction**
(`mcp_server/src/graphiti_mcp_server.py:906`):

> Unlike add_memory, this bypasses episode extraction and writes the
> relationship directly. graphiti-core resolves the source/target nodes against
> existing entities (deduplicating by name) and generates the required
> embeddings internally.

So there is no `gpt-5.5` call at all — only `text-embedding-3-small` on each
fact, roughly 25 tokens apiece. A full day of 34,388 airings is on the order of
**$0.02**. The cost objection does not survive contact with the API.

---

## What the guide actually contains

Measured from a live `/epg.xml` snapshot — 57.8 MB, 94,735 programmes,
2,014 channels, about five days deep:

| | |
| --- | --- |
| Airings in a 24-hour window | **34,388** |
| New airings per hour | ~1,255 median, 2,469 peak |
| Unique titles, 24h | 10,073 |
| Unique title+description, 24h | 23,751 |

Two findings shape the schema.

**An airing is the atomic unit, and airings must not be deduplicated.**
"Newshour" at 14:00 is a different broadcast from "Newshour" at 15:00.
Deduplicating by title would also be wrong for series — *The Dick Van Dyke
Show* airs 431 times carrying **223 distinct episode descriptions**.

**For news, the guide is boilerplate.** Al Jazeera's 158 airings carry 18
distinct descriptions; "News Live" airs 58 times sharing one
("Up-to-date news and analysis from around the world"). Across the whole guide,
27% of airings under a repeated title have no distinguishing description.

Worth being blunt about the consequence: this graph will answer **when
Newshour airs**, not **what tonight's Newshour is about**. That is not in the
EPG, so no index — graph, full-text or vector — can produce it. Where the guide
*is* informative (59% of repeated titles carry distinct descriptions) is series
and entertainment.

---

## The gap this closes

No FastChannels endpoint searches programme titles. Checked three ways:

- All 184 paths in `docs/openapi.yaml` swept for `program`/`guide`/`epg` — only
  `/epg.xml`, its two per-feed variants, and `/admin/guide/programs`.
- Every endpoint taking `search`/`q`/`query`/`title` — nine, none matching
  programme titles. `/epg.xml?search=` filters *channel names*:
  `search=Newshour` returns **0** programmes, `search=Al Jazeera` returns 157.
- `/admin/guide/programs` read from source (`app/routes/admin.py:1087`) —
  accepts only `channel_ids`, `offset` and `anchor`, over a fixed five-hour
  window.

Today, "what is on about X this week" means downloading and parsing 57.8 MB.

---

## Design

### Schema — one triplet per airing, in `group_id="epg"`

```
(Channel "Al Jazeera") -[AIRS]-> (Programme "Newshour")
   fact:       "Al Jazeera airs Newshour, Sat 16 Aug 14:00-15:00 PDT.
                Latest news and in-depth analysis from around the world."
   valid_at:   2026-08-16T14:00:00-07:00
   invalid_at: 2026-08-16T15:00:00-07:00
```

Node names deduplicate automatically, so 2,014 channel nodes and one node per
distinct programme title accumulate, while **each airing is its own edge with
its own validity window**. That is what keeps 14:00 and 15:00 distinct.

### One blocker to clear first

`search_memory_facts` already accepts `valid_at_after` / `valid_at_before`
(line 567) — precise temporal filtering, exactly what airings need.

But the `add_triplet` **MCP tool** builds its `EntityEdge` with only
`created_at`. Edges written through it get `valid_at=None` and would never
match those filters. The query side supports what we need; the write side does
not populate it.

`graphiti_core.add_triplet()` (`graphiti_core/graphiti.py:1645`) accepts a
fully-constructed `EntityEdge`, and `EntityEdge` carries both fields
(`graphiti_core/edges.py:274`). The fix is small: have the MCP tool accept
optional `valid_at` / `invalid_at` and pass them through.

That means patching
`/home/marco/opt/graphiti/mcp_server/src/graphiti_mcp_server.py` and rebuilding
with the existing `mcp_server/docker/Dockerfile.hermes`. That checkout already
carries local additions (`Dockerfile.hermes`, `compose.hermes.yml`), so this
fits how it is being used — and it is clean enough to offer upstream to
getzep/graphiti.

FalkorDB's Redis port is **not** published to the host, so ingestion goes over
MCP HTTP at `127.0.0.1:8000`, not directly to the database.

### Facts about the running deployment

Read from `docker/compose.hermes.yml` and the live container, because two of
them contradict what the config file suggests:

| | |
| --- | --- |
| Memory graph | **`group_id=hermes`** — *not* `main`. `config-docker-falkordb.yaml` defaults to `main`, but compose overrides it with `GRAPHITI_GROUP_ID: ${GRAPHITI_GROUP_ID:-hermes}`. Any isolation check must compare against `hermes`. |
| FalkorDB database | `hermes_graphiti` |
| Concurrency | **`SEMAPHORE_LIMIT=2`** |
| MCP client on this host | **absent** — `import mcp` fails, so ingestion needs the SDK in a virtualenv |

`SEMAPHORE_LIMIT=2` is the one with teeth at full scale: 34,388 airings at
roughly 200 ms each, two at a time, is about **three hours of wall clock per
day of guide**. Fine for a single-channel test, but it is what decides whether
hourly ingestion of the whole lineup is achievable at all — worth measuring at
step 3 rather than discovering at step 4.

### `dev/epg_graph.py`

- Fetch `/epg.xml`; keep airings starting in the next 24 hours.
- Skip any `(tvg_id, title, start)` already sent, tracked in
  `~/news/epg-graph-state.json`. **This is what makes hourly incremental.**
- One `add_triplet` per new airing, `group_id="epg"`.
- `--limit N` to bound a run, `--dry-run` to print without sending, `--prune`
  to drop edges past their `invalid_at`.
- Reuse the XMLTV parsing already in `dev/record.py` — `epg_for()` handles the
  `%Y%m%d%H%M%S %z` format and the `tvg-id` ↔ `channel` join.

Hourly systemd user timer, the same pattern as the `fcrec-` timers.

**Hourly must mean incremental, never a rebuild.** A full 24h rebuild every
hour is 34,388 × 24 = **825,312 writes/day**. Incremental is ~1,255/hour.

### Retention

34,388 edges/day accumulate. `--prune` removes edges past `invalid_at` via
`delete_entity_edge`, holding the graph at roughly one day. `clear_graph`
accepts a `group_id`, so `clear_graph(group_id="epg")` is a safe full reset
that **cannot touch the `hermes` memory graph** — the isolation the "separate
graph" requirement was asking for.

### `dev/hermes/EPG-GRAPH-SKILL.md`

How to query it — `search_memory_facts(query=…, group_ids=["epg"],
valid_at_after=…, valid_at_before=…)` — what the graph does and does not know,
and that `group_ids=["epg"]` must be passed or the search falls back to memory.

---

## Suggested staging

So cost and value are visible before committing to the whole thing:

1. Patch and rebuild the MCP server; verify `valid_at` round-trips.
2. Ingest **one channel** for 24h. Does temporal search answer "what is on Al
   Jazeera at 21:00"?
3. Ingest one **category** (News, 79 channels). Judge search quality at real
   volume, and measure throughput against `SEMAPHORE_LIMIT`.
4. Only then all 2,014 channels on an hourly timer.

**Step 2 is where this proves itself**, at negligible cost, and is the point at
which to decide whether to continue.

### Step 1 in detail — patch `add_triplet`

Two optional parameters on `add_triplet`, `valid_at` and `invalid_at`, parsed
as ISO-8601 (naive treated as UTC, matching how `search_memory_facts`
documents its own bounds) and passed into the `EntityEdge(...)` constructor.
About a dozen lines, and a no-op when the arguments are omitted.

`docker/Dockerfile.hermes` installs `graphiti-core` from PyPI (pinned
`0.29.3`) but `COPY`s `main.py` and `src/` from the local checkout — so editing
`src/graphiti_mcp_server.py` does take effect, and only the final layers
rebuild:

```bash
cd /home/marco/opt/graphiti/mcp_server
docker compose -f docker/compose.hermes.yml build graphiti-mcp
docker compose -f docker/compose.hermes.yml up -d
```

**Risk:** this restarts the server Hermes uses for memory, so there is brief
downtime. Graph data lives in the `falkordb_data` volume and is untouched.
Reverting is a `git checkout` of the one file plus a rebuild. Keep the change
as `dev/graphiti-valid-at.patch` so it survives a re-clone, the same convention
as `dev/ytdlp-gate.patch`.

### Step 2 in detail — Al Jazeera

**Payload: 38 airings** in the next 24 hours, of 158 across the full five-day
guide. Small enough to read by hand, real enough to mean something. Cost is 38
embedding calls and no `gpt-5.5` — effectively free.

What it has to demonstrate, ordered by how likely each is to fail:

1. **Repeat airings stay distinct.** "News Live" airs repeatedly and must
   produce **one** Programme node with **many** `AIRS` edges carrying different
   `valid_at`. This is the schema's central claim. If `graphiti-core`
   deduplicates edges by fact similarity, they collapse and the approach needs
   rethinking — this is the cheapest possible place to find that out.
2. `valid_at` survives the round trip at all — the entire point of step 1.
3. Temporal filtering selects: a bracket over 21:00-22:00 returns that airing;
   the same bracket shifted a day returns nothing.
4. Isolation: `search_memory_facts(group_ids=["hermes"])` returns no EPG facts,
   and Hermes' existing memories are unaffected.
5. Rollback: `clear_graph(group_id="epg")` removes everything ingested and
   leaves `hermes` answering as before.

**What step 2 deliberately does not prove:** search quality at 2,014-channel
volume — 38 facts retrieve trivially — and whether semantic search over
boilerplate is useful at all. Al Jazeera is the *worst* case for that second
question: 158 airings sharing 18 descriptions.

---

## Open questions worth deciding before starting

- **Is `AIRS` the only edge worth having?** Category could be a second triplet
  (`Programme -[GENRE]-> Category`), making "what sports is on" a traversal
  rather than a text match. Cheap to add, more nodes.
- **Should the window be longer than 24 hours?** The guide runs five days. Wider
  costs proportionally more edges and more pruning, and buys "what is on
  Thursday".
- **Should DRM channels be ingested at all?** Those 69 channels cannot be
  watched or recorded here, so they are noise in every result.

---

## A note on placement

This file lives under `dev/`, which `.gitignore:43` excludes, so it is
untracked by default and cannot reach `fork`/upstream through the PR flow —
the same protection the rest of the local tooling has. Committing it to the
private mirror needs `git add -f`, as with the other `dev/` files.
