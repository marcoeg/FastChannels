# A separate Graphiti graph for the EPG — refined plan

**Status: audited against the deployed code and rewritten 2026-08-16. Nothing
has been built.**

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
2. **Isolation is logical (`group_id`) through step 3, and physical (own
   FalkorDB database) only if step-3 measurements demand it.**

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
   fact:       "Al Jazeera airs Newshour, Mon 17 Aug 14:00-15:00 PDT.
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

### The write path — the patch

Stock `add_triplet` already accepts `group_id` per call
(`mcp_server/src/graphiti_mcp_server.py:907`, fallback at `:941`) but not
timestamps — it builds the `EntityEdge` with only `created_at` (`:958-965`) —
and then hands the edge to core's resolution machinery with all the failure
modes above. The patch, in the local `mcp_server/src/` (which the Docker image
`COPY`s, so a rebuild picks it up):

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

Even patched, one write is ~4 DB statements plus 1–2 embedding HTTP calls, so
the peak hour (~2,300 airings) should land in roughly ten minutes sequential —
**measured at step 3, not assumed.**

### Facts about the running deployment (verified live)

| | |
| --- | --- |
| Memory graph | `group_id=hermes` — compose overrides the config default `main` |
| FalkorDB database | `hermes_graphiti`. `GRAPH.LIST` also shows a **stray second graph named `hermes`** — identify it (step 0) before any "clean" claim |
| MCP endpoint | streamable HTTP on `127.0.0.1:8000`; `GET /health` → 200 is the liveness probe (`GET /mcp` → 406 is normal) |
| FalkorDB port | 6379 **not** published to the host — all access goes over MCP |
| `SEMAPHORE_LIMIT=2` | caps only fan-out *inside* one core operation (a fresh semaphore per gather, `helpers.py:123-133`); `add_triplet` runs synchronously in the HTTP request, unqueued (only `add_memory` queues). It is **not** a write throttle — the first draft mischaracterized it. Throughput is set by per-write latency |
| Models | `MODEL_NAME=gpt-5.5`, and the "small model" slot is pinned to the same (`factories.py:148`); embedder `text-embedding-3-small`, 1536-dim |
| MCP client on host | absent (`import mcp` fails); `python3.12-venv` and `uv` are both available — `uv venv && uv pip install mcp` |
| FalkorDB memory today | 18 MB used, no `maxmemory` |

### Isolation — logical now, physical later

`group_id` is a property filter, not a database. EPG edges share
`hermes_graphiti`'s graph key, fulltext/vector indexes, and per-graph write
locks with Hermes' production memory. At step-2/3 scale that is negligible; a
full 24 h window projects to roughly **0.5–1 GB** (34k edges × ~7 KB of
embedded fact, plus node-name embeddings and index overhead) on a database
currently at 18 MB.

**Step-4 gate:** during step 3, measure (a) FalkorDB memory growth
extrapolated to the full lineup and (b) p95 latency of a fixed set of hermes
memory searches before vs during ingestion. If the projection exceeds ~2 GB or
hermes p95 degrades ≥2×, deploy a second graphiti-mcp container from the same
patched image with `FALKORDB_DATABASE=epg_graphiti` (own port) — real
index/lock isolation, at the price of a second MCP connection for Hermes.
Otherwise stay logical.

Deletion isolation is real: `clear_graph(group_ids="epg")` deletes only the
epg partition (`DETACH DELETE` by `group_id`). Two footguns, both fatal to
hermes memory: **omitting `group_ids` falls back to the default group (=
hermes), and an empty string coerces to `None` and does the same**
(`utils/type_config.py:62-64`). Every script hard-codes the literal `"epg"`,
never an interpolated variable. `delete_entity_edge` is uuid-only and
**cross-group with no guard** — uuids come only from our own ledger.

### `dev/epg_graph.py` — the ingester

- One unfiltered `GET /epg.xml` per run, parsed with
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
  artifact retains ~1.1 days of past airings).
- Writes sequential, `direct=true`, node uuids cached and passed back.
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
evicted from state *before* new airings are written. Past airings are never
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
- `group_ids=["epg"]` is mandatory; omitted, the search silently targets
  hermes memory.
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

- Archive the FalkorDB volume:
  `docker run --rm -v hermes-graphiti_falkordb_data:/d -v "$PWD":/b alpine tar czf /b/falkordb-$(date +%F).tgz /d`
- Pin the `falkordb` image (currently `:latest`) to a digest, so a compose
  `up` can never pull a new database engine as a side effect.
- The entire deployment definition — `Dockerfile.hermes`,
  `compose.hermes.yml`, and the secrets-bearing `mcp_server/.env` — is
  untracked or gitignored in a detached-HEAD checkout: `git checkout` cannot
  restore any of them, `git clean -fd` would destroy the two docker files, and
  a `git clean -x` would take the `.env` too. Back all three up (the `.env`
  outside any repo).
- Identify the stray `hermes` graph in `GRAPH.LIST` (likely a stale early
  deployment) and note the finding.

### Step 1 — patch and rebuild

`valid_at`/`invalid_at` + the `direct` flag on `add_triplet`, saved as
`dev/graphiti-valid-at.patch`. Rebuild with **corrected commands** — the first
draft's, lacking `--project-directory`, resolve to compose project `docker`
and would try to start a duplicate stack with port collisions and a fresh
empty volume (verified via `docker compose config`):

```bash
cd /home/marco/opt/graphiti/mcp_server
docker compose -f docker/compose.hermes.yml --project-directory . build graphiti-mcp
docker compose -f docker/compose.hermes.yml --project-directory . up -d graphiti-mcp
```

Naming the service means the falkordb container is never recreated. Brief
hermes-memory downtime; graph data lives in the volume and is untouched.
Revert = `git checkout` of the one tracked src file + rebuild.

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
5. `group_ids=["hermes"]` search returns no EPG fact; Hermes' existing
   memories answer as before.
6. The MCP container log shows **zero LLM calls** during the ingest run.
7. The TRT World case yields two distinct nodes (`Channel: TRT World` vs
   `TRT World`).
8. Rollback: `clear_graph(group_ids="epg")` removes everything ingested;
   hermes still answers.

Draft `EPG-GRAPH-SKILL.md` now. What step 2 deliberately does not prove:
search quality at volume, and whether semantic search over boilerplate is
useful at all — Al Jazeera is the worst case (158 airings, 18 descriptions).

### Step 3 — the News category

Filtered fetch (2.3 MB, ~4,700 programmes over the guide). The search-quality
evaluation is executed **by Hermes through the skill** — natural questions,
scored against `/epg.xml` ground truth.

**Pass iff all of:**

9. 50 random (channel, timestamp) probes answered correctly ≥ 47/50.
10. A peak-hour-sized batch (~2,300 writes) completes well inside the hour —
    target ≤ 30 min sustained.
11. p95 of a fixed set of hermes memory searches during ingestion is < 2× the
    before-baseline.
12. A simulated schedule change (drop one future airing from the diff input)
    removes the ghost edge on the next run.

Also measured here: FalkorDB memory growth, extrapolated for the step-4 gate.

### Step 4 — full lineup, hourly

Gated on: the step-3 criteria; the isolation decision (memory projection
< ~2 GB **and** criterion 11 passed, else deploy the physical split first);
the freshness probe and the `OnFailure` path each demonstrated once; the
weekly reset scheduled.

**Every failed criterion has the same immediate rollback:
`clear_graph(group_ids="epg")`. All of them mean stop — except criterion 11,
whose remedy is the physical split followed by a re-run of step 3.**

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
- **GENRE edge: deferred.** 100% of programmes carry `<category>`, but the
  values mix genres with storefront brands — "Plex" alone tags 46% of
  programmes, alongside TCL TV+, STIRR, Whale TV+, Xumo Play; 235 distinct
  values. Meaningful only after value cleanup; nothing in this design blocks
  adding it later.
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
