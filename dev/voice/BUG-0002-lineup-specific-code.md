# BUG-0002 — Voice agent carries code and prompts specific to one lineup and time zone

| | |
|---|---|
| Component | Voice agent (`dev/voice`): `player_resolve.py`, `voice_agent.py`, `prompts/`, `lineup_scope.py`, `model_bench.py` |
| Severity | Medium: wrong times in any other zone; silent loss of brand/network names on any other lineup |
| Status | Open; audit only, no code changed |
| Found | 2026-10-04 |

## Summary

The agent must serve any subscriber lineup, not just the 53-channel Bay Area
lineup it was developed against. Most of it already does. The lineup is read
live from the TV (`viewer://lineup`), the guide mapping is built at runtime
(`lineup_scope.py`), and channel, number, title, catch-up, on-demand and memory
handling are generic.

What is specific is small: about 25 lines of code plus about a dozen prompt
sentences. It falls into two classes with different failure modes:

- **Time zone.** The guide prompt hard-codes "Pacific". In any other zone the
  model builds "tonight"/"today" windows in the wrong zone, so answers are
  silently wrong by hours. This is the part that actually breaks.
- **This lineup's channels.** Brand and network aliases point at this edge's
  channel ids, and the speech-to-text hint names Bay Area call signs. On
  another lineup these quietly stop helping ("Put on Fox" no longer reaches
  the local affiliate) but don't break anything.

The regression gate (`model_bench.py`) and the test documents are also bound to
this lineup, so a different lineup currently can't be validated at all.

## Findings

### A. Time zone (breaks in another zone)

| Where | What |
|---|---|
| `prompts/epg/minny.md:12, 27, 33-37, 45, 68-72` | "Pacific" written into the rules for building windows and speaking times. The Clock line follows `VOICE_LOCAL_TZ` (`pai_agent.py:227`), but this prompt does not: a TV in Eastern would get an EDT Clock line plus instructions to build Pacific windows. |
| `prompts/epg/minny.md:11, 36` | "the operator's zone (Eastern)": a fact about this Minny tenant, presented as a rule. |
| `prompts/epg/falkordb.md:12` | "your answers speak Pacific" (FalkorDB backend). |
| `voice_agent.py:568` | Welcome greeting's morning/afternoon/evening uses `America/Los_Angeles`, not `VOICE_LOCAL_TZ`. |
| `epg_now.py:29, 201` | FalkorDB backend hard-codes Pacific in its output ("… Pacific"). |
| `prompts.py:23, 85` | `format_time_context` defaults to Pacific (callers in `pai_agent.py` pass the env zone; `bench_ttft.py` relies on the default). |
| `prompts.py:88-89` | Docstring says the Minny catalogue's zone is Eastern and `tz` must match it, which contradicts what the code now does (the viewer's zone, by design). Stale. |
| `lineup_scope.py:156` and comments | `_pacific_iso` is named for Pacific but uses `_TZ` (env); the name only. |

### B. This lineup's channels (degrade silently on another lineup)

| Where | What |
|---|---|
| `player_resolve.py:43` `BRANDS` | 14 on-air brands → this edge's channel ids (`"fox2": "2581"` = KTVU, `"nbcbayarea": "2586"`, `"telemundo48": "2589"`, `"metv": "2588"` …). On another lineup the ids are absent, `find_channel` falls through to name matching, and "Fox 2" / "NBC Bay Area" / "CBS 5" stop resolving. |
| `player_resolve.py:47` `NETWORKS` | "fox"/"cbs"/"pbs"/"nbc"/"telemundo"/"ion" → this lineup's affiliates. Elsewhere, "Put on Fox" matches whatever has "fox" in its name (or asks), not the local affiliate. |
| `voice_agent.py:284-286` | Speech-to-text hint names KTVU, KPIX, KQED, KNTV and COZI. It biases recognition toward Bay Area call signs and does nothing for another market's. |
| `player_resolve.py:51` `_NOISE` | "muxley": this headend's naming ("Muxley KTVU-DT"). Harmless elsewhere. |

### C. Generic logic tuned on this guide's naming

| Where | Assumption |
|---|---|
| `lineup_scope.py:241-243` `_match` | The lineup title equals the guide's channel name ("Muxley KTVU-DT"), else the call sign equals the guide `call_sign`, else a single hit. A lineup whose titles differ from the guide's names gets unmapped channels, which are then missing from what's-on and searches (as TrailersFromHell was until `b6b0d83`). Unmapped channels are only logged. |
| `lineup_scope.py:260` | Same-station feeds are found by "same programme and the call sign appears in the feed name" ("[MN] KTVU FOX2"). Holds where feed names carry the call sign. |
| `lineup_scope.py:65, 82` `GENRE_WORDS`, `_GENRE_ALSO` | English genre words and English sport tags. Spanish-tagged rows ("Deportes", "Noticias") would not match "sports"/"news". KSTS happens to tag in English. |

### D. Examples only (no functional effect)

Docstrings, comments and prompt examples name this lineup's channels and
programmes ("KTBU" → KTVU in `prompts/tv/ytvn.md:10`, "channel 9",
"Columbo", "fresh news on CBS" in `prompts/00-identity.md:19`, KQED in
`prompts/epg/minny.md:19`). They illustrate patterns; they don't constrain
behaviour, though they tilt the model slightly toward these names.

### E. Tests bound to this lineup

- `model_bench.py`: at least 13 of 27 questions name this lineup's channels
  or programmes (KTVU, KQED, COZI, Trace, Columbo, PBS NewsHour, Dune…), and
  the dry-run screen state is fixed on KTVU (`model_bench.py:161`).
- `TEST-PLAN.md`, `TEST-SCRIPT.md`: written for this lineup.

So the gate that has caught regressions all day can't validate another
lineup as it stands.

## Proposed fixes (in priority order)

### 1. One zone, everywhere

- Make the viewer's zone a single value, from `VOICE_LOCAL_TZ` or from the
  guide (the Minny MCP reports the tenant's zone in `cluster_status`; check
  whether that's the viewer's zone or only the operator's). Fill it into the
  prompts instead of the literal "Pacific", e.g. a `{zone}` placeholder
  expanded by `load_static_instructions`.
- Move "the operator's zone is Eastern" out of the static prompt: derive it
  from `cluster_status`, or drop the rule and always build explicit ranges in
  the viewer's zone (the prompt already prefers that).
- Use the same zone for the welcome greeting and in `epg_now.py`; fix the
  stale `prompts.py` docstring.

### 2. Brand and network aliases from data, not literals

Two options:

- **Derive at map build.** The guide's feed names already carry the brands
  ("[MN] KTVU FOX2", "Muxley KSTS Telemundo 48", "Muxley KTLN MeTV", as seen
  in the 2026-10-04 map). `LineupScope._match` sees
  them, so it can emit alias → lineup channel_id pairs: tokens and joined
  tokens of each mapped feed's name, minus noise. `find_channel` would
  consult those instead of `BRANDS`/`NETWORKS`. This works for any lineup the
  guide covers.
- **Per-lineup config.** A small JSON next to the agent (or served by the
  edge), keyed by lineup, holding aliases. Simpler, but someone has to write
  it for each lineup.

Either way, keep a network alias pointing at a channel only if that channel
is in the current lineup, and ask when two candidates fit.

### 3. Speech-to-text hint from the lineup

Build the hint from the current lineup's call signs (or the 5–8 most-tuned
channels from the rollout traces), keeping the conversational phrasing that
avoids hint echo.

### 4. Guide mapping fallback and visibility

- When `_match` finds nothing, try the call sign and `_searchable(title)`
  before giving up, as the TrailersFromHell fix did.
- Report unmapped lineup channels in a startup summary (already logged as
  "missing [...]"), and count them in the trace metadata so a new lineup's gaps
  are visible on day one.

### 5. Genre words beyond English

Add the common Spanish genre words and tags ("noticias", "deportes", "fútbol",
"cocina", "películas"…) to `GENRE_WORDS` / `_GENRE_ALSO`, or match against
the tag vocabulary the guide actually returns for the lineup.

### 6. A gate that works for any lineup

Split `model_bench.py` questions into lineup-independent ones ("What's on TV
right now?", "Turn it up", "Any live sports on right now?") and lineup-bound
ones generated at run time from the live lineup and guide: "What's on
channel {number}?", "Put on {call sign}", "Is there any {title on now}
tonight?". Make the dry-run screen state come from the lineup's first
channel instead of KTVU.

## Acceptance checks

- With `VOICE_LOCAL_TZ=America/New_York`: the assembled prompt contains no
  "Pacific", the Clock line says EDT/EST, and "what's on tonight?" builds a
  6 pm–5 am Eastern window (check the `start`/`end` in the trace).
- On a lineup without KTVU, or with this lineup's `BRANDS`/`NETWORKS`
  removed: "Put on Fox" and "Fox 2" still reach the local Fox affiliate,
  through the derived aliases.
- On a second lineup (another subscriber on the same edge): the map log shows
  no unmapped channels (or names them), and the lineup-independent benchmark
  passes unchanged.
- On this lineup: the benchmark and `voice_probe.py` are unchanged (no
  regression from removing the literals).

## Not proposed

- Removing the examples in prompts and docstrings (class D): they carry no
  behaviour, and replacing them with neutral placeholders costs clarity.
