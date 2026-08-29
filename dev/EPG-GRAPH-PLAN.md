# A separate Graphiti graph for the EPG — refined plan

**Status: audited against the deployed code and rewritten 2026-08-16; revised
2026-08-18 — the EPG now gets its own MCP server from day one, and the
FalkorDB layout facts are corrected. Nothing has been built.**

Hermes runs Graphiti + FalkorDB. The idea stands: a **separate graph holding
the next day of guide data, refreshed hourly**, so EPG search goes through the
same retrieval surface as memory instead of a second mechanism.

This revision replaces the first draft after a read-only audit of the actual
stack: the deployed graphiti-core 0.29.3 (byte-identical between the
`/home/marco/opt/graphiti` checkout and the running container, so every
citation below is production code), the MCP server source, the live compose
deployment, and a fresh `/epg.xml` snapshot. Two decisions came out of that
audit and are baked in:

1. **Writes go through a new direct-write path added by the local patch**, not
   through stock `add_triplet` behaviour.
2. **The EPG runs behind its own patched MCP server on `127.0.0.1:8001`,
   writing into the existing FalkorDB, where `group_id="epg"` is
   automatically its own physical graph. The chat stack — the MCP server on
   8000 and the `hermes` graph — is never rebuilt, restarted, or touched.**
   (Revised 2026-08-18 from "logical now, physical later" at the user's
   direction, after the FalkorDB per-group finding below.)

---

## What the audit changed

The first draft rested on two premises. Both are false in the deployed code.

**Premise 1 — "`add_triplet` bypasses extraction, so no gpt-5.5 call at all,
~$0.02/day." Wrong.** It bypasses *episode* extraction, but core's
`add_triplet` (`graphiti_core/graphiti.py:1645`) then runs full edge
resolution: two hybrid searches per write (`graphiti.py:1717-1738`) followed by
`resolve_extracted_edge`, which makes a gpt-5.5 dedupe/contradiction call
whenever either search finds anything similar
(`utils/maintenance/edge_operations.py:726-731`) — i.e. on virtually every
write once the group has content. A second gpt-5.5 call tries to parse
timestamps out of the fact text for any edge lacking them
(`edge_operations.py:597-603`; skipped only when `valid_at`/`invalid_at` are
already set, `:587-588`). And this deployment pins the "small model" slot to
the main model (`mcp_server/src/services/factories.py:148`), so all of it runs
on gpt-5.5 itself. Unpatched full-scale cost: **~34k–68k gpt-5.5 calls/day**,
plus ~9 embedding calls and ~14 FalkorDB round-trips per write. The original
cost objection partially survives after all.

**Premise 2 — "each airing is its own edge with its own validity window; that
is what keeps 14:00 and 15:00 distinct." Not a schema property.** Edge
distinctness is decided per write:

- Identical fact text (lowercased, whitespace-collapsed) collapses
  deterministically — the new edge, supplied timestamps included, is silently
  discarded and the existing edge reused (`edge_operations.py:684-695`).
- Near-identical facts (ours differ only in the embedded date/time) are
  adjudicated by gpt-5.5 on every write. The prompt leans toward keeping
  date-distinct facts, but a duplicate verdict drops the airing wholesale
  (`:746-749`), and a contradiction verdict can rewrite `invalid_at` on an
  earlier overlapping airing (`resolve_edge_contradictions`, `:538-573`).
- Every surviving edge that carries `invalid_at` is stamped `expired_at=now`
  at birth on this path (`:820-823`).

So under stock behaviour, silent schedule corruption is a standing per-write
risk adjudicated by a nondeterministic LLM — not a hypothetical to test.

**The consequence is the patch's real scope.** Adding two timestamp parameters
(the first draft's "about a dozen lines") is necessary but not sufficient. The
patch must also add a **direct-write path** that skips resolution entirely —
see "The write path" below. With it, writes are LLM-free *by construction*,
~10x cheaper, and immune to collapse, and the draft's cost model ($0.02/day of
embeddings) becomes actually true.

One piece of good news the draft missed: the read side already does everything
we need, including `invalid_at` filtering (see "Querying").

A follow-up check (2026-08-18) corrected the audit itself on one point: on
FalkorDB, `group_id` is not a mere property filter — **every group is its own
physical graph** (see the deployment table and "Isolation"). The live memory
sits in the FalkorDB graph named `hermes`; `hermes_graphiti`, which the audit
called the memory database, is empty — a bootstrap artifact.

---

## What the guide actually contains

Re-measured 2026-08-16 from the live server:

| | |
| --- | --- |
| `/epg.xml` snapshot | 64.3 MB, 104,895 programmes, 2,014 channels (1,861 with programmes) |
| Horizon | ~5 days forward; ~1.1 days of past airings retained in the artifact |
| Airings starting in the next 24 h | **34,075** |
| New airings per hour | ~1,376 median, 2,259 peak |
| Unique titles / (title, desc) pairs, 24 h | 9,715 / 23,139 |

Two findings shape the schema; the first draft's counterpoint to the second
has weakened.

**An airing is the atomic unit, and airings must not be deduplicated.**
"Newshour" at 14:00 is a different broadcast from "Newshour" at 15:00, and
*The Dick Van Dyke Show* airs 464 times carrying 230 distinct episode
descriptions.

**The guide is more boilerplate than the draft claimed.** Only **34%** of
repeated titles carry ≥2 distinct descriptions across the full guide (44% in
the 24 h window) — the draft said 59%, and its "27% non-distinguishing" figure
does not reproduce under any definition (current analogues run 32–70%). The
likely driver is composition: Plex-sourced listings are now 46% of all
programmes. The blunt consequence stands and is now stronger: this graph
answers **when Newshour airs**, not **what tonight's Newshour is about** — that
is not in the EPG, so no index can produce it.

Al Jazeera, the step-2 test channel, is unchanged and re-verified: 158 airings
across the guide, 38 in the next 24 h, 19 titles, 18 descriptions; "News Live"
airs 58 times sharing one description.

---

## The gap this closes

No FastChannels endpoint searches programme titles. `/epg.xml?search=` filters
*channel names* (`search=Newshour` → 0 programmes, `search=Al Jazeera` → 158);
`/admin/guide/programs` takes only channel ids over a fixed five-hour window.
Today, "what is on about X this week" means downloading and parsing 64 MB.

Fetch facts that matter for the ingester (verified in `docs/openapi.yaml` and
`app/routes/output.py`): `/epg.xml` takes `source`, `category`, `language`,
`search` — **no time/days parameter**. There is no gzip, and conditional GET is
deliberately disabled (`_send_feed_artifact` strips ETag/Last-Modified and
sends `Cache-Control: no-store`), so an unfiltered poll is always the full
artifact. Filtered fetches bypass the prebuilt artifact and are generated live
per request — small (`?category=News` = 2.3 MB / 4,727 programmes,
`?search=Al Jazeera` = 66 KB) but they cost the server a fresh generation each
time.

---

## Design

### Schema — one triplet per airing, in `group_id="epg"`

```
(Channel: Al Jazeera) -[AIRS]-> (Newshour)
   fact:       "Al Jazeera (News) airs Newshour, Mon 17 Aug 14:00-15:00 PDT.
                Latest news and in-depth analysis from around the world."
   valid_at:   2026-08-17T21:00:00Z     start, UTC on the wire
   invalid_at: 2026-08-17T22:00:00Z     stop, UTC on the wire
```

- **Channel node names carry a `Channel: ` prefix.** Nodes dedupe by
  (case-insensitively normalized) name, and **528 channel names are exactly
  equal to a programme title** — TRT World airs a programme called "TRT
  World", likewise 20/20, Rick and Morty, ALF. Bare names would merge Channel
  and Programme into one self-looping node. The fact string keeps the natural
  phrasing. Channel-vs-channel collisions are zero today (all 2,014 guide
  display-names distinct; all 2,220 enabled-lineup names distinct).
- Timestamps travel as UTC ISO-8601 (the server parses naive strings as UTC);
  the fact text speaks local time for readability.
- One edge per airing with its own validity window — **guaranteed by the
  direct-write path below, not by the schema itself.**
- 153 of the 2,014 guide channels carry no programmes and never gain a node.
- **Categories are channel metadata, and ride in two places** (settled
  2026-08-19). Measured: in the Sports category, 198 of 209 channels carry
  one identical category-set on every programme — the XMLTV `<category>`
  tags are the channel's category stamped per programme (plus an occasional
  per-programme extra from some sources); `/api/channels` holds the single
  authoritative `category` string, the XMLTV tags are richer (sub-genres
  like Basketball). (a) **In the fact, attached to the channel** — `"Al
  Jazeera (News) airs …"` — because the fact string is the only surface
  that is fulltext-indexed, embedded, *and* time-boundable, which is what
  makes "any basketball on tonight?" a plain time-bounded search. The tags
  are cleaned through a **brand denylist** derived from source display
  names (Plex, STIRR, TCL TV+, Whale TV+, Xumo Play, Samsung TV Plus,
  FreeLiveSports, LG Channels, …). (b) **On the channel node at creation**:
  `summary` = "TV channel. Category: News." — Entity summaries are
  fulltext-indexed (verified in the live `epg` graph), so
  `search_nodes("news channels")` works — with the raw list in
  `attributes` as display metadata.
- **Rejected**: category as a bare node attribute (attributes are
  unindexed — invisible to every search path), and Category *nodes*
  (`Channel -[IN_CATEGORY]-> Category` edges are timeless; the time filters
  exclude `valid_at`-less edges by construction and the tool surface has no
  traversal, so they answer nothing Hermes can currently ask). Channel-level
  category nodes (~2 edges/channel) stay a cheap later addition if a
  traversal surface ever exists.

### The write path — the patch

Stock `add_triplet` already accepts `group_id` per call
(`mcp_server/src/graphiti_mcp_server.py:907`, fallback at `:941`) but not
timestamps — it builds the `EntityEdge` with only `created_at` (`:958-965`) —
and then hands the edge to core's resolution machinery with all the failure
modes above. The patch, in the local `mcp_server/src/` (which the Docker image
`COPY`s), ships **only in the EPG instance's image** — built under a distinct
tag and run as the `127.0.0.1:8001` server; the chat MCP server on 8000 is
never rebuilt or restarted:

- `add_triplet` gains `valid_at`, `invalid_at` (ISO-8601, naive treated as
  UTC — matching how `search_memory_facts` documents its bounds) and
  `direct: bool = False`.
- **`direct=true` bypasses resolution**: construct the two `EntityNode`s and
  the `EntityEdge` with the supplied uuids and timestamps and persist via
  `add_nodes_and_edges_bulk` — no `resolve_extracted_edge`, no candidate
  searches, no LLM call ever. The bulk writer already persists
  `valid_at`/`invalid_at` verbatim (`utils/bulk_utils.py:205-206`), and
  `EntityEdge` declares both fields (`graphiti_core/edges.py:274-279`). The
  fact is embedded once; node names only when a node is first created.
- The ingester **caches node uuids** (returned in the `TripletResponse`) in
  its state file and passes `source_node_uuid`/`target_node_uuid` on every
  subsequent call, so node resolution is skipped entirely.
- All flags omitted = today's behaviour, so the change is a clean opt-in —
  offerable upstream to getzep/graphiti, though that needs a fork remote the
  checkout does not have yet (its only remote is upstream itself, detached
  HEAD at v0.29.3).
- **Writes are sequential.** Node dedup in the stock path is read-then-write
  with no lock, and even direct writes should not create the same node
  concurrently. Note `SEMAPHORE_LIMIT` would not save us: it never queues
  `add_triplet` (see deployment table).
- Saved as `dev/graphiti-valid-at.patch`, same convention as
  `dev/ytdlp-gate.patch`.

**One footgun outranks all others: the ingester must talk only to
`127.0.0.1:8001`.** Pointed at 8000 it would write through the chat server's
*unpatched* `add_triplet` — gpt-5.5 calls and LLM-mediated edges, straight
into the memory server. The URL is a hard-coded constant, and the ingester
verifies at startup that the server's default group is `epg` (via
`get_status`), refusing to run if it cannot tell.

Even patched, one write is ~4 DB statements plus 1–2 embedding HTTP calls, so
the peak hour (~2,300 airings) should land in roughly ten minutes sequential —
**measured at step 3, not assumed.**

### Facts about the running deployment (verified live)

| | |
| --- | --- |
| Memory graph | the FalkorDB graph **`hermes`** (= group `hermes`; compose overrides the config default `main`). 486 nodes measured 2026-08-18: 91 Episodic chat turns plus typed entities — the agent's tools, skills, and chat knowledge |
| Graph layout | **per-`group_id` graphs.** graphiti-core on FalkorDB redirects every group-scoped read and write to the graph named after the group — *"FalkorDB: one group_id still needs the graph named after that id"* (`graphiti_core/decorators.py:58`, mechanism `:60-99`, clone `falkordb_driver.py:331-344`). `FALKORDB_DATABASE=hermes_graphiti` names only the default for group-less bootstrap ops, and that graph is **empty** — an artifact, optional cleanup |
| MCP endpoint | streamable HTTP on `127.0.0.1:8000` (chat; the EPG server adds `127.0.0.1:8001` — see "The EPG stack"); `GET /health` → 200 is the liveness probe (`GET /mcp` → 406 is normal) |
| FalkorDB port | 6379 **not** published to the host — all access goes over MCP |
| `SEMAPHORE_LIMIT=2` | caps only fan-out *inside* one core operation (a fresh semaphore per gather, `helpers.py:123-133`); `add_triplet` runs synchronously in the HTTP request, unqueued (only `add_memory` queues). It is **not** a write throttle — the first draft mischaracterized it. Throughput is set by per-write latency |
| Models | `MODEL_NAME=gpt-5.5`, and the "small model" slot is pinned to the same (`factories.py:148`); embedder `text-embedding-3-small`, 1536-dim |
| MCP client on host | absent (`import mcp` fails); `python3.12-venv` and `uv` are both available — `uv venv && uv pip install mcp` |
| FalkorDB memory today | 18 MB used, no `maxmemory` |

### Isolation — a dedicated EPG server on a shared FalkorDB

Isolation at the graph layer is automatic. On FalkorDB, graphiti-core routes
every group-scoped read and write to the graph named after the `group_id`
(citations in the table above). Writing with `group_id="epg"` therefore
creates and uses a FalkorDB graph named `epg` with its own graph key, its own
fulltext/vector indexes, and its own per-graph write lock. The chat graph
`hermes` is never scanned, locked, or grown by EPG operations, and the graph
name `epg` is unclaimed (`GRAPH.LIST`: only `hermes` and the empty
`hermes_graphiti`).

What stays shared is the **redis process**: RAM (no `maxmemory` set — 18 MB
held today, while a 24 h EPG window projects to roughly **0.5–1 GB** of
edges, embeddings and index overhead), AOF persistence absorbing ~34k daily
add+delete cycles, and crash blast radius. That is the accepted trade-off of
this topology; the escape hatch is the step-4 gate below.

The trade-off materialized once already (2026-08-19, during step 3): a
FalkorDB write on the `epg` graph **deadlocked inside the database** —
`GRAPH.INFO` showed the query still "running" 90 minutes later with its
client long gone; epg writes hung while epg reads and every `hermes`
operation stayed normal (the per-graph locks held). FalkorDB has no
query-kill, so recovery was a FalkorDB container restart: ~30 s of
chat-memory downtime, zero data loss (AOF replay — the `hermes` graph had
even grown during the incident). Worth reporting upstream to FalkorDB; a
recurrence is a strong argument for taking the step-4 physical split.

**Step-4 gate:** during step 3, measure (a) redis memory growth extrapolated
to the full lineup and (b) p95 latency of a fixed set of hermes memory
searches before vs during ingestion — the graphs are separate, so any
degradation means redis-process contention. If the projection exceeds ~2 GB
or hermes p95 drifts ≥2×, move the EPG to its **own FalkorDB container**
before enabling the timer — cheap, because EPG data is regenerable: a compose
change plus a full re-ingest.

Deletion isolation: `clear_graph(group_ids="epg")` on the EPG server deletes
only the `epg` graph. The empty-string/omitted-`group_ids` footgun
(`utils/type_config.py:62-64` coerces `''` to `None` → the server default)
softens there — the EPG server's default group *is* `epg`, so the worst case
is clearing the EPG itself — but scripts still hard-code the literal `"epg"`,
never an interpolated variable. Likewise `delete_entity_edge` (uuid-only, no
group guard) can only reach the `epg` graph when called on the EPG server,
whose group-less operations run against its `FALKORDB_DATABASE=epg` default.
What makes all of this safe is topological, and bears repeating: **EPG
tooling talks only to the EPG server.**

### The EPG stack

`compose.epg.yml`, next to `compose.hermes.yml` in
`/home/marco/opt/graphiti/mcp_server/docker/` (untracked like it — on the
step-0 backup list).

**Revised 2026-08-27: the stack now carries its own FalkorDB** (service
`falkordb-epg`, volume `falkordb_epg_data`, same pinned engine digest, no
AOF — EPG data is regenerable — `maxmemory 2gb`, timeouts baked into env,
nothing published to the host) and no longer joins the hermes network at
all. The physical split was taken after the shared database deadlocked
**three times** under EPG write bursts (2026-08-19, twice 2026-08-27): a
wedged query holds the graph lock, is unreapable by TIMEOUT_MAX (timeouts
only interrupt at cooperative points), and each recovery restarted the
database holding Hermes' memory. With the split, a wedge costs an EPG-stack
restart and an hourly self-heal; the chat stack is untouchable from here.
The abandoned `epg` graph was deleted from the hermes FalkorDB (back to
~39 MB). Worth reporting upstream to FalkorDB with the GRAPH.INFO evidence.

Original single-service description (superseded):

| | |
| --- | --- |
| Image | the **patched** build under a distinct tag (e.g. `knowledge-graph-mcp:epg`), built with the existing `Dockerfile.hermes`, `GRAPHITI_CORE_VERSION=0.29.3` |
| Network | joins the existing `hermes-graphiti_default` (external), reaching `redis://falkordb:6379` with the existing password |
| Env | `GRAPHITI_GROUP_ID=epg`; `FALKORDB_DATABASE=epg` so even group-less ops touch only the `epg` graph; same embedder config (`text-embedding-3-small`); `MODEL_NAME` is irrelevant to direct writes but set for completeness |
| Port | `127.0.0.1:8001:8000` — 8001 verified free 2026-08-18 |
| Project | its own compose project name, so no compose command for this stack can ever recreate the hermes containers |

The hermes container keeps running its current image — **the patch never
reaches the chat stack**, which is not rebuilt, restarted, or reconfigured at
any step of this plan. On the Hermes side, the entire integration is one
`mcp_servers.epg_graph` block in `~/.hermes/config.yaml` plus the skill at
`~/.hermes/skills/epg-graph/` — see "Querying".

### `dev/epg_graph.py` — the ingester

- One `GET` per full run — since 2026-08-28 of
  `/feeds/default/native/epg.xml`, the default feed's native guide, which
  carries the COMPLETE lineup including the ~258 Gracenote-partition
  channels that the global `/epg.xml` excludes (this closed the "no title
  search for Gracenote channels" gap and makes the graph match what LAN
  viewers consume 1:1). Parsed with
  `ElementTree.iterparse` + `el.clear()`. **Never per-channel `epg_for()`** —
  that function (`dev/record.py:231`) issues one live-generated request per
  channel, which at lineup scale is ~2,014 requests/hour against the
  production server. From it, only the `%Y%m%d%H%M%S %z` strptime lines and
  the tvg-id ↔ `channel` attribute join carry over; it is channel-scoped and
  coupled to `resolve_channel`/`vlc.py`. Handle the 503 artifact-warming
  response via its `Retry-After`. The hourly fetch is always the full ~64 MB
  (no smaller option exists — see "The gap"); over loopback that is fine.
  Steps 2–3 use the filtered fetches instead.
- Window: airings **starting** in the next 24 h, filtered client-side (the
  artifact retains ~1.1 days of past airings). A consequence observed in the
  pilot: an airing already in progress at ingest time is never captured — in
  steady hourly operation every airing is written before it starts, so this
  only affects the very first run.
- Writes sequential, `direct=true`, node uuids cached and passed back — all
  against the EPG server at `127.0.0.1:8001`, never 8000.
- Categories: the programme's XMLTV tags minus the brand denylist go into
  the fact (channel-side phrasing), and at a channel node's first creation
  into its `summary`/`attributes` — see the Schema section.
- Every MCP call runs under a hard per-call watchdog, with one
  reconnect-and-retry on transport failure and a run abort after three
  consecutive transport failures. Added 2026-08-19 after an ingest stalled
  87 minutes on a single `call_tool` await whose connection died silently —
  transport-level timeouts alone did not fire.
- Ledger keys are `channel_id|title|start`; programme titles can themselves
  contain `|` (seen live), so parsing takes `channel_id` from the left and
  the timestamp from the right, never `split("|", 2)`.
- `--limit N` to bound a run, `--dry-run` to print without sending, `--prune`
  as below.
- Hourly systemd user timer (the same systemd-user pattern as `record.py`'s
  `fcrec-` timers) — but not enabled until step 4's preconditions are met.

**Hourly must mean incremental, never a rebuild.** A full 24 h rebuild every
hour is 34,075 × 24 ≈ 818k writes/day; incremental is ~1,376/hour.

### State file — the source of truth

`~/news/epg-graph-state.json`. Per `(tvg_id, title, start)` key:
`{edge_uuid, stop, desc_hash, sent_at}`, plus a `{node name → uuid}` map.

- Written atomically (temp file + `os.replace`) after every batch of ~100
  writes — a crash loses at most one batch of uuids. Those airings' keys are
  gone from state too, so the next run re-sends them, leaving up to one batch
  of duplicate, unpruneable edges — reclaimed only by the weekly reset below.
- Exclusive `flock`; a second run exits 0 immediately (timer overlap at peak
  is expected, not an error).
- Missing or corrupt state = **refuse to ingest**; recovery is the documented
  reset: `clear_graph(group_ids="epg")`, empty state, full re-ingest. This is
  the only recovery, because an edge whose uuid is lost is unpruneable — **no
  MCP tool enumerates a group's edges**, and core `add_triplet` writes no
  episode (`graphiti.py:1762` passes empty episodic lists), so the episode
  tools see nothing either. `search_memory_facts` is ranked top-k retrieval,
  not enumeration — pruning by search would be sampling, and sampling leaks
  edges forever.
- After downtime, the normal hourly run self-heals: prune drops expired
  entries, reconciliation drops stale future keys, ingest fills the window.
  No special backfill mode.

### Reconciliation — the guide mutates

The first draft could only add. A moved programme creates a new key while the
ghost edge stays valid until it expires — the graph would then assert both
airings; a changed stop time or description would never be re-sent at all.

Each run therefore diffs the freshly fetched window against state for all
**future** airings: keys present in state but absent from the guide, or whose
`stop`/`desc_hash` changed, are deleted via `delete_entity_edge(uuid)` and
evicted from state *before* new airings are written. (`desc_hash` covers the
description **and** the cleaned category list, so a category change re-sends
the airing.) Past airings are never
reconciled, only pruned.

### Pruning and retention — two tiers

- **Hourly `--prune` iterates the state file, not the graph:** entries whose
  `invalid_at` is more than a one-hour grace period in the past are deleted by
  their recorded uuid, then evicted. (The graph cannot be enumerated over MCP
  — see above.)
- **`delete_entity_edge` removes edges only.** Programme nodes accumulate —
  ~10k after the first day, then by the smaller (unmeasured) stream of
  never-before-seen titles — each carrying a 1536-dim name embedding, and no
  MCP tool deletes a node. Second tier: weekly, in a quiet window,
  `clear_graph(group_ids="epg")` + immediate full re-ingest + state rebuild
  (the reset invalidates every cached uuid). If that proves annoying, extend
  the local patch with a node-prune tool instead.

### Querying — `dev/hermes/EPG-GRAPH-SKILL.md`, drafted at step 2

The read side needs **zero changes** and is better than the first draft knew:
`search_memory_facts` accepts `valid_at_after`/`valid_at_before` **and**
`invalid_at_after`/`invalid_at_before` (`graphiti_mcp_server.py:574-577`),
compiled to hard Cypher bounds on the edge's `valid_at`/`invalid_at`.
Canonical patterns for the skill:

- **NOW:** `valid_at_before=<now UTC>` **and** `invalid_at_after=<now UTC>` —
  both bounds required; `valid_at` alone cannot find a programme that started
  two hours ago and is still on.
- **Window ("tonight 8–11pm"):** `valid_at_after`/`valid_at_before`
  bracketing the window.
- Timestamps in UTC or with an explicit offset — naive strings parse as UTC
  while the fact text speaks PDT.
- EPG questions go to the **EPG server**, wired into Hermes with a config
  block — **not a plugin, and no patch to the graphiti plugin**. Hermes'
  generic MCP client reads `mcp_servers:` from `~/.hermes/config.yaml` (an
  open config dict; changes live-reload in running sessions):

  ```yaml
  mcp_servers:
    epg_graph:
      url: http://127.0.0.1:8001/mcp
      timeout: 180
      tools:
        include: [search_memory_facts, search_nodes, get_episodes, get_status]
  ```

  Tools surface as `mcp__epg_graph__search_memory_facts` etc., automatically
  visible to CLI, Telegram, and Hermes-cron sessions, with no collision with
  the memory plugin's `graphiti_memory`/`recall`. The `include` list keeps
  the write and destructive tools (`add_memory`, `add_triplet`,
  `clear_graph`, `delete_entity_edge`) off Hermes' surface — ingestion and
  pruning belong to `dev/epg_graph.py`, not the LLM. **Never name the
  server `graphiti`** — the memory plugin falls back to reading
  `mcp_servers.graphiti` and would start interacting with it. For the
  record: a second graphiti-plugin instance is structurally impossible
  (Hermes memory providers are singletons with hard-coded tool names), and
  forking the third-party plugin is unnecessary.
- Because raw MCP tools lack the plugin's conveniences, the skill must carry
  them: pass `group_ids: ["epg"]` explicitly on every call (there is no
  default-group injection; on this server an omitted group defaults to
  `epg`, harmless), and there is no automatic prefetch for this graph. The
  chat server holds no EPG data; asking `graphiti_memory` about TV listings
  returns nothing.
- `max_facts`: default 10, no upper cap — use 25–50 for windows.
- Never filter or reason on `expired_at`.
- **Lookup vs listing:** this is ranked top-k hybrid retrieval (BM25 + cosine,
  RRF-fused, no recency term). A blank query returns nothing, and absence from
  results is never evidence a programme is not airing. The graph answers
  *targeted* questions; for an exhaustive "everything on tonight" listing,
  iterate per-channel/per-category queries or fall back to `/epg.xml`, and say
  which source was used.
- **Freshness probe before trusting results:** `query="airs"`,
  `group_ids=["epg"]`, `valid_at_after=<now+6h>`, `max_facts=1`. The query
  term appears in every fact string, which is what makes an empty result
  meaningful despite the ranked-retrieval caveat above: empty means the graph
  has less than six hours of future coverage — declare it stale and fall back.

The skill is drafted at **step 2** — not last, as the first draft had it — so
steps 2–3 are judged through Hermes' real interface rather than hand-tuned
queries. (The first draft's own query sketch omitted the `invalid_at` bounds,
which is exactly the mistake the skill exists to prevent.)

### Observability

The first draft had an hourly timer and no health signal; ingestion silently
broken for three days would be indistinguishable from a quiet guide — worse,
the graph *empties itself* via `invalid_at` while looking healthy.

- Every run appends one JSON line to `~/news/epg-graph-runs.log`:
  `{ts, fetched, added, reconciled, pruned, failed, wall_s}`.
- The systemd service gets an `OnFailure=` unit so a failed run is surfaced,
  not just logged.
- Hermes' side of the story is the freshness probe above.

---

## Staging

### Step 0 — safety prep (before anything touches production)

- **The revert bundle exists (created 2026-08-18):**
  `~/backups/hermes-2026-08-18/`, 2.9 GB, per-item restore commands in its
  `RESTORE.md`. Contents — Hermes side: `config.yaml`, `.env`, `plugins/`,
  `skills/`, `cron/jobs.json`, the gateway systemd unit, git state plus the
  6,235-line local-changes patch. Memory/server side: the FalkorDB volume
  archive; a memory fingerprint (graph `hermes` 486 nodes /
  `hermes_graphiti` 0 / newest Episodic recorded); the running MCP image
  both as dated tag `standalone-backup-2026-08-18` and a saved tarball
  (guards against an accidental rebuild of the hermes tag);
  `compose.hermes.yml`, `Dockerfile.hermes`, both `.env`s, the effective
  config yaml, full `docker inspect` of both containers (the docker files
  are untracked/gitignored in a detached-HEAD checkout — git cannot restore
  them, which is why they are in the bundle); both checkouts' git state.
- Blast surface the bundle guards: the whole EPG plan touches exactly one
  Hermes file (`~/.hermes/config.yaml`, one added block) plus one new skill
  directory, and creates only new server-side objects (the EPG container,
  its image tag, the graph `epg`). EPG additions are removable without the
  bundle; it covers the failure modes where something touched what it
  should not have.
- Done 2026-08-19: the `falkordb` image is pinned by digest
  (`sha256:ad09d505…`) in `compose.hermes.yml`, so a compose `up` can never
  pull a new database engine as a side effect.
- Done 2026-08-19: `compose.epg.yml` exists and is copied into the bundle.
- Graph inventory is settled (2026-08-18): `GRAPH.LIST` shows `hermes` — the
  live memory, 486 nodes — and `hermes_graphiti`, an empty bootstrap artifact
  (leave it; optional cleanup). The name `epg` is unclaimed, and port 8001 is
  free.

### Step 1 — patch, build the EPG image, start the EPG server

`valid_at`/`invalid_at` + the `direct` flag on `add_triplet`, saved as
`dev/graphiti-valid-at.patch`. Build the patched image under its own tag with
the existing `Dockerfile.hermes`, then bring up **only the EPG stack** from
`compose.epg.yml` under its own compose project. The hermes stack is not a
compose target of this plan at all — and should it ever need a manual
`docker compose` touch, remember its file resolves to the wrong project
(`docker`) without `--project-directory .` (verified via
`docker compose config`).

Verify `GET /health` on `127.0.0.1:8001` → 200. **Zero hermes downtime** —
the chat server is neither rebuilt nor restarted. Revert = stop and remove
the EPG container; `git checkout` of the one tracked src file undoes the
patch.

After the first ingested triplet, confirm the fulltext/vector indexes exist
in the `epg` graph — per-group graphs mean per-graph indexes, and the
`hermes` graph acquired its own at first use; verify the same happens for
`epg` (the step-2 query criteria would also catch their absence).

**Executed 2026-08-19.** Image `zepai/knowledge-graph-mcp:epg` built from the
patched checkout; server healthy on 8001; hermes containers' uptime
preserved throughout. The `epg` graph received its full index set on first
write (RANGE on `valid_at`/`invalid_at`, FULLTEXT on `fact`).

### Step 2 — Al Jazeera, 38 airings

Payload re-verified fresh: 158 airings across the guide, 38 in the next 24 h
(channel id `tcl.OM017937`), via the filtered fetch. Also ingest one
same-name channel — TRT World airs a programme titled "TRT World". Cost: ~38
fact embeddings plus TRT World's handful, and **zero LLM calls**.

**Pass iff all of:**

1. For each repeated programme ("News Live"), edge count equals the guide's
   airing count in the window — any shortfall is a collapse: **fail**.
2. Every airing (38/38) is returned by a title query bracketed ±1 min around
   its start.
3. A NOW query (`valid_at_before=now` + `invalid_at_after=now`) issued
   mid-airing returns the in-progress programme.
4. The same bracket shifted 24 h returns zero.
5. Isolation: the `hermes` graph's node count (486 at baseline) is unchanged
   after the ingest and again after the rollback; the chat MCP container's
   uptime and restart count are untouched; a search on the chat server
   returns no EPG fact, and Hermes' existing memories answer as before.
6. The MCP container log shows **zero LLM calls** during the ingest run.
7. The TRT World case yields two distinct nodes (`Channel: TRT World` vs
   `TRT World`).
8. Rollback: `clear_graph(group_ids="epg")` removes everything ingested;
   hermes still answers.

Draft `EPG-GRAPH-SKILL.md` now and install it at
`~/.hermes/skills/epg-graph/SKILL.md` (the same convention as the two
FastChannels skills already there). Add the `mcp_servers.epg_graph` block to
`~/.hermes/config.yaml` at this point — 8001 answers `/health` after step 1,
and the config live-reloads. Also verified here, unnumbered: Hermes lists
the `mcp__epg_graph__*` tools, answers a test query through the skill, and
`graphiti_memory`/`recall` behave unchanged.

**Executed 2026-08-19, all eight criteria passed.** Run over a 48 h window
instead of 24 — the guide had a coverage hole for Al Jazeera's next day
(8 airings in 24 h at run time vs the plan's 38). 94 airings ingested
(Al Jazeera 46, TRT World 48) at ~4 writes/s: edge counts matched the ledger
exactly (News Live 17, Newshour 12, TRT World 48 — no collapse), 94/94
retrievable through ±1 min `valid_at` brackets, the NOW query returned TRT
World's 05:00 UTC airing while it was live, shifted brackets returned zero,
the `hermes` graph sat at exactly 486 nodes before, during, and after —
including after the `clear_graph` rollback drill — zero LLM calls appeared
in the server log, and the same-name case produced two distinct nodes with
zero self-loops. Re-ingested after the drill: 95 airings in the fresh
window. Hermes lists the `mcp__epg_graph__*` tools; the interactive
skill-driven query check remains with the operator.

What step 2 deliberately does not prove:
search quality at volume, and whether semantic search over boilerplate is
useful at all — Al Jazeera is the worst case (158 airings, 18 descriptions).

### Step 3 — the Sports category

Filtered fetch (`?category=Sports`: 6.1 MB, 12,375 programmes across 221
channels, measured 2026-08-18) — about 2.6× the News payload, a better scale
rehearsal for step 4, and sports programmes carry distinctive titles and
descriptions, so search quality is judged on content the graph can actually
distinguish (news boilerplate, the worst case, was already covered by
step 2). The search-quality evaluation is executed **by Hermes through the
skill** — natural questions, scored against `/epg.xml` ground truth.

Step 3 also lands the category schema (see Schema): the brand denylist,
channel-side genres in the fact, channel-node summaries, and `desc_hash`
covering categories — followed by a pilot re-ingest (`--clear` + re-run),
since the fact format changes. The EPG skill's fact-shape example is updated
at the same time.

**Executed 2026-08-19, all four gates passed.** Final state: 6,771 ledger
airings (221 Sports channels + the pilot pair, 48 h window), graph/ledger
parity exact. Gates: (9) 50/50 random probes returned their exact edge
through title queries with instant time bounds; (10) sustained 2.5–3.5
writes/s — a peak-hour batch lands in ~15 min, and the true hourly
incremental pass (prune 21 + ghost 1 + changed 1 + write 71 over all 221
channels) took **34.9 s**; (11) hermes memory-search p95 during ingestion was
698–719 ms vs a 734 ms baseline — unchanged; (12) an injected ghost and a
tampered airing were both repaired in one run. Memory: 23 → 107 MB at ~6.8k
edges; full-lineup projection ~0.3–0.5 GB — inside the 2 GB gate, so the
shared FalkorDB stands.

Step 3 surfaced and fixed **two graphiti-core FalkorDB search defects**, both
now in `dev/graphiti-valid-at.patch` (`src/utils/falkor_fast_search.py`) and
both upstream-PR candidates: (a) similarity search re-converts
`vecf32($search_vector)` on every row — hoisting it into a leading `WITH`
took the identical 6,771-edge scan from >15 s (query timeout) to 12 ms;
(b) fulltext search re-joins every yielded relationship via
`MATCH ()-[e {uuid: rel.uuid}]->()`, an O(matches × edges) scan — using
`startNode()`/`endNode()` on the yielded edge took a common-term query from
timeout to 3.4 ms. Neither shows at chat-memory scale (~200 edges); both are
walls at EPG scale. Deployment note: FalkorDB now runs
`TIMEOUT_DEFAULT=15000` / `TIMEOUT_MAX=60000` (set at runtime via
`GRAPH.CONFIG SET`; TIMEOUT_MAX also kills wedged writes, which the legacy
TIMEOUT never did). **Persistence is not optional:** this host restarts its
containers daily around 10:30Z (observed on the 15th, 18th and 19th — some
scheduled maintenance), which wipes runtime GRAPH.CONFIG and restores the
legacy `TIMEOUT=1000`, killing every search over one second. That is exactly
how the Hermes freshness probe (and every one-sided `valid_at_after` query,
which runs 2–4 s at this scale) broke on 2026-08-19 hours after the gates
had passed. Until the operator adds
`FALKORDB_ARGS: "TIMEOUT_DEFAULT 15000 TIMEOUT_MAX 60000"` to the falkordb
service env in `compose.hermes.yml` (the assistant's attempt at that edit
was permission-blocked), the settings must be re-applied after each daily
restart.

**Pass iff all of:**

9. 50 random (channel, timestamp) probes answered correctly ≥ 47/50.
10. A peak-hour-sized batch (~2,300 writes) completes well inside the hour —
    target ≤ 30 min sustained.
11. p95 of a fixed set of hermes memory searches during ingestion stays
    within 2× baseline — expected *unchanged*, since the graphs are separate;
    any drift means redis-process contention and trips the step-4 FalkorDB
    migration.
12. A simulated schedule change (drop one future airing from the diff input)
    removes the ghost edge on the next run.

Also measured here: FalkorDB memory growth, extrapolated for the step-4 gate.

### Step 4 — full lineup, hourly

Gated on: the step-3 criteria; the FalkorDB decision (memory projection
< ~2 GB **and** criterion 11 clean, else move the EPG to its own FalkorDB
container first — a compose change plus re-ingest, cheap because EPG data is
regenerable); the freshness probe and the `OnFailure` path each demonstrated
once; the weekly reset scheduled.

**Every failed criterion has the same immediate rollback:
`clear_graph(group_ids="epg")` on the EPG server. All of them mean stop —
except criterion 11, whose remedy is the own-FalkorDB migration followed by a
re-run of step 3.**

**Executed 2026-08-27.** The day's shape: the first two full-lineup ingests
each deadlocked the *shared* FalkorDB mid-burst (wedges #2 and #3, with
TIMEOUT_MAX active and unable to reap them), triggering the physical split
per plan; the very first ingest into the *dedicated* DB then wedged at 8,297
of 8,474 writes (#4) — proving the deadlock is a FalkorDB engine bug under
sustained write bursts, not shared-instance contention, and worth an
upstream report. With the split, that recovery cost one EPG-container
restart (an RDB snapshot first — this DB runs without AOF — preserved all
8,297 writes) and the finishing run completed clean: **9,909 airings, 3,591
nodes, exact graph/ledger parity, uniform valid_at typing, 127 MB** — with
real reconciliation en route (129 ghosts, 68 changed as the guide shifted).
Freshness probe, ranked search, and `list_memory_facts` all verified on the
full corpus. `epg-graph.timer` (hourly :20) and `epg-graph-reset.timer`
(Sun 12:00 UTC) are enabled; the OnFailure notify path was drilled earlier.
The FalkorDB Browser for the EPG database is at `127.0.0.1:3001`.

Known operational caveat, revised 2026-08-28 after wedge #5: the EPG
FalkorDB can wedge under **ordinary hourly load too**, not just bursts
(#5 struck during a routine increment; earlier at 1.2k/2.0k/3.1k/8.3k-write
bursts). The system now absorbs this autonomously, three layers deep:

1. The ingester fails FAST when the DB is wedged — three consecutive
   transport failures abort the run in any phase (the delete path
   originally lacked this and kept "evicting anyway": eight consecutive
   hourly runs each ground ~3 min/key into the 55-minute systemd kill,
   and evictions during transport failures orphaned edges — both fixed:
   ledger entries are KEPT on transport failure, evicted only on
   server-side errors).
2. `OnFailure=` fires both the notify line and
   **`epg-graph-recover.service`**, which restarts the two EPG containers
   (`sg docker`; live-drilled). The chat stack is never touched.
3. The next hourly run self-heals: backlog prune, reconcile, ingest.

Manual intervention is now only needed if the run-log shows repeated
`run-failed`/`stack-restarted` pairs — meaning restarts stopped curing the
wedge.

---

## Cost (patched, direct path)

Per airing: one fact embedding (~25 tokens, computed server-side — every fact
string embeds its own timestamps, so no client-side cache would ever hit) plus
a node-name embedding on first creation only. A full day of 34,075 airings is
~$0.02 of `text-embedding-3-small` and **zero gpt-5.5** — the figure the first
draft claimed, now true because the direct path makes it true. For the record,
the unpatched path costs 1–2 gpt-5.5 calls and ~9 embedding calls per write.

---

## Resolved and open questions

- **DRM channels: moot.** All 69 (all roku, marked `disable_reason="DRM"`;
  `requires_drm_bridge` is true for zero rows) are disabled and inactive, so
  they never appear in `/epg.xml` or `/m3u` — the ingester cannot pick them up
  and needs no exclusion logic.
- **Categories: resolved 2026-08-19 — channel-side, in the fact and the
  channel node's summary** (design in the Schema section). The `<category>`
  tags are channel metadata stamped onto every programme, mixing genres
  with storefront brands ("Plex" alone tags 46% of programmes; 235 distinct
  values) — brands are stripped via the source-name denylist. Category
  *nodes* stay off the table until a traversal surface exists: timeless
  edges are excluded from every time-bounded search by construction.
- **Window beyond 24 h: open.** The guide runs ~5 days; cost scales linearly
  in edges, pruning, and reconciliation surface. Decide after step 3's
  throughput and memory numbers.

---

## A note on placement

This file lives under `dev/`, which `.gitignore:43` excludes, so it is
untracked by default and cannot reach `fork`/upstream through the PR flow —
the same protection the rest of the local tooling has. Committing it to the
private mirror needs `git add -f` on first add, as with the other `dev/`
files.
