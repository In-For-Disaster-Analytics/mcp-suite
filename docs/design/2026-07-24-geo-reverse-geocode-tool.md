# Reverse-geocoding tools for the DSO Geo MCP server

## Status

Implemented

## Objective

Add `reverse_geocode` (single point) and `reverse_geocode_bbox` (multi-point,
representative-of-extent) tools to the `dso-geo` MCP server, so any MCP-capable
system in the DSO ecosystem — `ckan-agent-api`'s LangGraph agent, Claude Code
sessions, future agents — can turn coordinates into a human-readable place name
through one shared, tested implementation, instead of each hand-rolling its own
Nominatim client.

## User need

**Immediate trigger:** while building `subside:location` labels for the SUBSIDE
Risk Explorer (see `modflow-suite/subside/docs/design/2026-07-24-run-location-labels.md`),
we discovered `agents/ckan-agent-api` already reverse-geocodes bbox centroids
to seed CKAN dataset titles — in **two** separate places
(`app/agents/ckan_registration/persona_nodes.py::_reverse_geocode` and
`app/agents/ckan_registration/nodes.py::_nominatim_fetch_raw` +
`_REVERSE_GEOCODE_TOOL`), each with its own address-field logic, and neither
documented anywhere in DSO-Architecture. A third, independent implementation
was just written for `stac-platform` (`stacmap/geocode.py`) because — critically
— that one runs inside an unattended Tapis pipeline task with no MCP client in
the loop at all, so it *can't* call an MCP tool regardless of this spec.

**Who benefits:** any DSO agent or Claude Code session that needs to label a
dataset, run, or region by location — CKAN dataset registration (already doing
this ad hoc), Risk Explorer analysts asking an agent about a run's location,
future geospatial-agent workflows. **Who this does NOT help:** unattended batch
pipelines with no agent/LLM in the loop (e.g. SUBSIDE's Tapis publish path) —
those need their own direct Nominatim client regardless, since MCP is only
reachable by systems with an MCP client, not general server-side code.

**Definition of success:** `ckan-agent-api`'s two inline implementations are
replaced by calls to the new `dso-geo` MCP tool; the tool is documented in
DSO-Architecture (`docs/services/mcp-servers.md`) so this doesn't get
independently reinvented a fourth time.

## Current code/system summary

- **`agents/ckan-agent-api/app/agents/ckan_registration/persona_nodes.py`**
  (`_reverse_geocode`, lines ~327-410): deterministic, single-centroid lookup.
  Tries Nominatim `zoom=14` then falls back to `zoom=10` if the result is only
  county/state-level. Address-field priority: `neighbourhood, suburb, quarter,
  city_district, city, town, village, hamlet, municipality, borough`, then a
  named-feature fallback that explicitly excludes geographic features (bay,
  river, mountain, etc. — via `_GEOCODE_FEATURE_CLASSES`/`_GEOCODE_FEATURE_TYPES`)
  so e.g. "Hooper Bay" doesn't shadow the nearby town of "Bethel". Uses
  `urllib.request` with a `certifi`-backed SSL context fallback (a real gotcha
  on some environments' default cert stores). Called from `_gather_evidence()`
  to seed a `location_hint` used by the LLM when drafting a new dataset title.
- **`agents/ckan-agent-api/app/agents/ckan_registration/nodes.py`**
  (`_update_field_with_llm`, lines ~3251-3339): a different mechanism — a
  two-turn LLM tool-calling exchange for *interactively revising* a title. The
  LLM itself picks the Nominatim `zoom` level (5/8/10/12/14/15/16/18) based on
  the user's stated specificity preference, then a plain `_nominatim_fetch_raw`
  call resolves it. This is agent-conversation-specific (needs an LLM turn to
  pick zoom) and is not a candidate for consolidation into a stateless MCP tool
  in the same way `_reverse_geocode` is — it stays as-is.
- **`stac-platform/stacmap/geocode.py`** (`resolve_location`, this repo):
  samples 3 points (centroid + 2 opposite corners) and only accepts a name all
  3 agree on, walking from a size-appropriate starting tier (city/county/state)
  up until agreement — built because SUBSIDE bboxes (tens of km) are large
  enough that a single centroid can land ambiguously between two settlements,
  unlike the WebODM drone-survey extents `persona_nodes.py` was built for. This
  module stays independent regardless of this spec (see User need above).
- **`mcp-suite/servers/geo`** (`dso-geo` MCP server, this repo): FastMCP server,
  tools registered via `register(mcp)` functions in `tools/metadata.py`,
  `tools/transform.py`, `tools/status.py`. All current tools resolve a CKAN
  `resource_id`/`dataset_id` to a URL (`ckan_resolve.py`) then dispatch to a
  pre-registered Tapis Abaco actor (`tapis_client.submit_message`) and return
  an `execution_id` to poll (`get_execution_status`) — because GDAL needs to
  run near the data on Corral. **Reverse geocoding needs none of that**: no
  raster, no Corral, no Abaco actor — it's a single fast (~1-3s) HTTP call to
  a free public API. It should be the first *synchronous* tool in this server
  (no execution_id, no polling), which the design below calls out explicitly
  so it isn't forced into the async pattern by copy-paste.
- No existing bbox/spatial helper in `ckan_resolve.py` — `resolve_resource_url`
  and `resolve_dataset_raster_urls` only resolve download URLs, not a package's
  `spatial` GeoJSON field.
- **DSO-Architecture docs gap:** `docs/services/mcp-servers.md`'s "Geo MCP
  Server" tool table has no geocoding entry; `repo-map.md`'s external
  dependencies table has no Nominatim/geocoding row. This spec's rollout
  includes fixing that.

## Proposed design

Two new tools in a new `tools/geocode.py` module, mirroring the existing
`gdalinfo_extract` (single) / `gdalinfo_summary` (aggregate) split, but
**synchronous** (no actor dispatch, no execution_id):

1. **`reverse_geocode(lat: float, lon: float, tapis_token: str | None = None) -> dict`**
   — the shared primitive. Ports `persona_nodes.py::_reverse_geocode`'s proven
   logic (zoom=14 → zoom=10 fallback, full address-field priority list,
   geographic-feature-exclusion fallback) using `requests` (this server's existing
   dependency and mocked-test convention) instead of `urllib`+certifi-workaround.
   Returns `{"name": "New Braunfels, Texas", "tier": "city", "address": {...}}`
   or `{"name": None, "reason": "..."}` on failure. Never raises.
   `tapis_token` is accepted for signature consistency with every other tool
   in this server but is unused (Nominatim is anonymous/keyless) — documented
   as such so callers aren't confused.

2. **`reverse_geocode_bbox(bbox: list[float] | None = None, dataset_id: str | None = None, tapis_token: str | None = None) -> dict`**
   — the representative-of-extent wrapper, for callers with a large or
   uncertain-extent area (ports the current `stacmap/geocode.py` 3-point
   majority-at-city/county algorithm plus centroid fallback, built on top of
   `reverse_geocode` above rather than duplicating its Nominatim-calling logic).
   Accepts either an explicit
   `[west, south, east, north]` bbox, or a CKAN `dataset_id` (resolves the
   package's `spatial` GeoJSON field via a new `ckan_resolve.resolve_dataset_bbox`
   helper — mirrors how `gdalinfo_summary` resolves `dataset_id` today).
   Returns `{"name": ..., "tier": ..., "agreement": true}` or
   `{"name": None, "reason": "no agreement at any tier"}`.

Both tools are read-only, anonymous (no CKAN write, no Tapis Abaco dispatch),
so they need no token gating beyond what every tool already accepts for
signature consistency.

## Files likely affected

- **New:** `mcp-suite/servers/geo/src/dso_geo_mcp/tools/geocode.py` —
  `reverse_geocode`, `reverse_geocode_bbox`, `register(mcp)`.
- **New:** `mcp-suite/servers/geo/src/dso_geo_mcp/geocode_client.py` — the
  actual Nominatim HTTP call + address-field/tier logic (kept separate from
  the `tools/` MCP-decorator layer, same separation `ckan_resolve.py` /
  `tapis_client.py` already model).
- **Edit:** `mcp-suite/servers/geo/src/dso_geo_mcp/ckan_resolve.py` — add
  `resolve_dataset_bbox(dataset_id) -> list[float]`, parsing `package_show`'s
  `spatial` GeoJSON field (same field `stac-platform`/WebODM's CKAN plugin
  already populate) into `[west, south, east, north]`.
- **Edit:** `mcp-suite/servers/geo/src/dso_geo_mcp/server.py` — register the
  new tool module; update the server's `instructions` string to mention it.
- **New tests:** `mcp-suite/servers/geo/tests/test_geocode.py` — mocked
  Nominatim responses (no live network), mirroring
  `stac-platform/tests/test_geocode.py`'s approach.
- **Edit (consumer):** `agents/ckan-agent-api/app/agents/ckan_registration/persona_nodes.py`
  — replace `_reverse_geocode` with an MCP call to `dso-geo`'s
  `reverse_geocode`/`reverse_geocode_bbox` (the agent already has an MCP
  client wired up for CKAN + Geo tools per `docs/services/ckan-agent-api.md`).
  `nodes.py`'s LLM-driven zoom-picking flow can also call `reverse_geocode`
  directly instead of `_nominatim_fetch_raw`, keeping its own zoom-selection
  logic (that part is genuinely agent-specific and not being consolidated).
- **Edit (docs):** `DSO-Architecture/docs/services/mcp-servers.md` — add the
  two new tools to the Geo MCP Server's tool table. `DSO-Architecture/docs/claude-context/repo-map.md`
  — add Nominatim to the external-dependencies table.

## API/schema changes

New MCP tool signatures (both synchronous — no `execution_id`/polling):

```python
@mcp.tool()
def reverse_geocode(lat: float, lon: float, tapis_token: str | None = None) -> dict:
    """Look up a human-readable place name for a single coordinate via Nominatim.

    Synchronous (no execution_id/polling — this is a fast, direct HTTP call,
    not a Tapis Abaco dispatch like the GDAL tools). tapis_token is accepted
    for signature consistency but unused (Nominatim is anonymous).

    Returns: {"name": "New Braunfels, Texas", "tier": "city", "address": {...}}
             or {"name": None, "reason": "..."} on failure.
    """

@mcp.tool()
def reverse_geocode_bbox(
    bbox: list[float] | None = None,
    dataset_id: str | None = None,
    tapis_token: str | None = None,
) -> dict:
    """Look up a place name representative of an entire bbox/dataset extent.

    Samples the centroid + two opposite corners, accepts city/county names when
    a majority of resolved sample points agree, and otherwise falls back to the
    centroid's finest available place field. This avoids returning a generic
    state name for large same-state extents. Pass either
    bbox=[west,south,east,north] directly, or dataset_id to resolve a CKAN
    package's spatial extent.

    Returns: {"name": ..., "tier": ..., "agreement": true}
             or {"name": None, "reason": "no agreement at any tier"}.
    """
```

No changes to any existing tool signature. No new required config beyond what
already exists (`CKAN_URL` for the `dataset_id` path); no new secrets (Nominatim
is keyless).

## Data flow

**Scenario: `ckan-agent-api` drafts a title for a new WebODM upload**

1. Agent extracts a GeoJSON boundary from the upload, computes a centroid.
2. Agent calls `dso-geo`'s `reverse_geocode(lat, lon)` (via its existing MCP
   client) instead of its own inline `_reverse_geocode`.
3. Result feeds the same `location_hint` the LLM already uses to draft a title.

**Scenario: an agent asks about a SUBSIDE run's location**

1. Agent has a run's bbox (from a STAC item's `bbox` field, already public).
2. Agent calls `reverse_geocode_bbox(bbox=[...])`.
3. Gets a name representative of the whole run extent, same algorithm
   `stacmap/geocode.py` uses internally for the automated publish path — just
   invoked live instead of pre-computed.

## Risks and tradeoffs

1. **Two implementations still won't fully converge.** `stacmap/geocode.py`
   cannot call this MCP tool (no MCP client in an unattended Tapis pipeline),
   so it remains a third, independent copy of the same core Nominatim logic.
   *Mitigation*: none technical — this is an inherent constraint of where the
   code runs, not a design flaw. Documented clearly so a future reader doesn't
   try to "fix" it by making the pipeline an MCP client (a much bigger, likely
   not worthwhile change for a single geocode call per publish).
2. **Public Nominatim from a shared MCP server used by multiple agents.**
   Aggregate call volume across every `ckan-agent-api` registration and every
   Claude Code session using this tool is harder to bound than a single
   pipeline's per-publish calls. *Mitigation*: policy-compliant identifying
   `User-Agent`, short timeout, no retries, in-process cache by rounded
   lat/lon/zoom, and an in-process 1-request/second throttle for live requests.
3. **`nodes.py`'s LLM-driven zoom selection is out of scope for consolidation**
   but should still call the new `reverse_geocode` primitive (with its
   chosen zoom folded in as a parameter, or accepting the tool's default
   zoom=14→10 fallback and letting the LLM's "specificity" preference map to
   which of `reverse_geocode`/`reverse_geocode_bbox` it calls instead of a raw
   zoom int) — needs a small design decision during implementation, not fully
   resolved here.
4. **Geographic-feature filtering** (excluding bays/rivers from the named-place
   fallback) is ported as part of `reverse_geocode` faithfully from
   `persona_nodes.py`, but `reverse_geocode_bbox`'s tier-agreement algorithm
   doesn't use that fallback path today (per the SUBSIDE spec's decision) —
   worth a follow-up if bbox-mode ever needs it.

## Alternatives considered

1. **Leave all three implementations as-is.** Rejected: the whole point of
   DSO-Architecture is to prevent exactly this — the same capability
   independently rediscovered a third time with no documentation trail.
2. **Consolidate into a shared Python package instead of an MCP tool**
   (e.g. a `dso-geocode` pip package both `ckan-agent-api` and `stac-platform`
   depend on). Would also solve `stacmap/geocode.py`'s duplication, unlike the
   MCP approach. Rejected for v1 because it's a bigger lift (new package,
   versioning, dependency management across two independently-deployed repos)
   for marginal benefit over just documenting the constraint — worth
   reconsidering if a fourth consumer appears.
3. **Fold into the existing `gdalinfo_extract`/`transform.py` async pattern**
   (execution_id + poll) for consistency with the rest of the server. Rejected:
   Nominatim is fast and needs no Abaco dispatch; forcing an async wrapper
   onto a synchronous call adds latency and complexity for no benefit.

## Test plan

- `mcp-suite/servers/geo/tests/test_geocode.py` — mocked Nominatim responses
  (httpx transport mock, matching this repo's existing test conventions),
  covering: field-priority selection, zoom=14→10 fallback, geographic-feature
  exclusion, bbox-mode majority/disagreement/centroid-fallback behavior, `dataset_id` bbox
  resolution, and the "no MCP client available" non-concern (N/A here, but
  documented in the spec for future readers).
- Manual smoke test: register the updated server locally (stdio), call
  `reverse_geocode(29.7, -98.1)` and confirm a sensible Texas place name.

## Documentation plan

- `DSO-Architecture/docs/services/mcp-servers.md` — add both tools to the Geo
  MCP Server's tool table.
- `DSO-Architecture/docs/claude-context/repo-map.md` — add Nominatim to
  external dependencies.
- `mcp-suite/servers/geo/README.md` (if one documents the tool list) — update.

## Rollout/rollback plan

- **Rollout:** implement + test the new tools; deploy `dso-geo` (rebuild +
  redeploy the pod per `mcp-suite`'s existing CI, per `mcp-servers.md`);
  migrate `ckan-agent-api`'s `persona_nodes.py` to call the MCP tool instead
  of its inline implementation (separate PR, since it's a different repo);
  update DSO-Architecture docs.
- **Rollback:** revert the tool registration in `server.py`; `ckan-agent-api`
  keeps working either way since it isn't required to migrate atomically with
  this deploy (its own inline `_reverse_geocode` keeps working until the
  consumer-side edit lands).

## Open questions

1. Should `reverse_geocode_bbox`'s `dataset_id` path require a `tapis_token`
   for private datasets, matching `gdalinfo_summary`'s pattern? (Recommend:
   same optional/env-fallback pattern as existing metadata tools.)
2. Cross-repo migration sequencing: land `dso-geo`'s new tools and deploy
   first, then migrate `ckan-agent-api` in a follow-up PR — or coordinate both
   in lockstep? (Recommend: sequential, since `persona_nodes.py`'s existing
   inline implementation keeps working unmodified in the meantime.)
3. Does `nodes.py`'s LLM zoom-selection flow get consolidated too, and if so,
   how does its integer `zoom` (5-18) map onto `reverse_geocode`'s fixed
   14→10 fallback? (Not resolved here — flagged as implementation-time
   design work.)

## Decisions

- **2026-07-24:** User asked whether reverse geocoding should be an MCP
  "skill" any DSO system could use. Decision: yes for **agent-driven**
  consumers (ckan-agent-api, Claude Code), scoped onto the existing `dso-geo`
  MCP server rather than a new standalone server. Explicitly NOT a fix for
  `stacmap/geocode.py`'s duplication — that module runs in an unattended Tapis
  pipeline with no MCP client, so it stays independent regardless.

- **2026-08-10:** Implementation review updated the design before coding.
  Decision: use `requests`/`responses` to match `servers/geo`'s current
  dependency and test stack; enforce public Nominatim safeguards with an
  identifying User-Agent, no retries, in-process cache, and a 1-request/second
  live-call throttle; port the current SUBSIDE bbox algorithm (city/county
  majority plus centroid fallback) rather than the older unanimous-agreement
  draft. Impact: `reverse_geocode` accepts an optional `zoom` argument for the
  existing interactive title-revision flow, while its default path preserves
  zoom=14→10 behavior for title drafting.

## User feedback / decisions

- **2026-07-24:** User asked to spec this out and file it as a tracked issue
  in the `agents` repo (this document + the corresponding GitHub issue).
  Status: Draft — not yet reviewed by the team or approved for
  implementation.
- **2026-08-10:** User asked to start issue #3 implementation. Proceeding with
  local implementation and tests; no GitHub push/PR/deploy/write actions are
  authorized by this decision.
- **2026-08-10:** Local implementation completed. Added `geocode_client.py`,
  `tools/geocode.py`, CKAN `spatial` bbox resolution, server registration,
  mocked tests, README/DSO-Architecture docs, and `ckan-agent-api` consumers
  that prefer dso-geo while retaining local fallback for staged rollout. Tested
  with full `servers/geo` pytest and focused `ckan-agent-api` geo integration
  tests. No deploy, push, PR, or GitHub issue update was performed.
