# dso-geo MCP Server

A FastMCP stdio server that exposes DSO geospatial tools to AI models and MCP
clients. It provides synchronous reverse-geocoding through Nominatim and
dispatches GDAL metadata extraction / raster transformations to a pre-registered
Tapis Abaco actor for data stored on TACC Corral.

## Quick start

```bash
cd servers/geo
uv sync --extra dev
uv run dso-geo-mcp
```

Copy `.env.example` to `.env` and fill in `GEO_ACTOR_ID` (required) and
`CKAN_URL`.

### HTTP transport (for a long-running consumer such as ckan-agent-api)

By default the server runs over **stdio**. To serve over **HTTP**, set
`MCP_TRANSPORT=http`. Unlike the CKAN server, the shared secret is **mandatory**
in HTTP mode (the server refuses to start without it) because the
`GEO_TAPIS_TOKEN` env fallback grants ambient Abaco compute to any caller that
can reach the port:

```bash
MCP_TRANSPORT=http \
MCP_HTTP_HOST=127.0.0.1 \
MCP_HTTP_PORT=8200 \
MCP_HTTP_SHARED_SECRET="$(openssl rand -hex 32)" \
uv run dso-geo-mcp
# serves at http://127.0.0.1:8200/mcp ; clients send Authorization: Bearer <secret>
```

The server also refuses to start if `MCP_HTTP_HOST` is non-loopback while
`GEO_TAPIS_TOKEN` is set. Never expose the endpoint publicly without a fronting
auth proxy.

## Prerequisites

- A pre-registered Tapis Abaco actor running the GHCR image
  `ghcr.io/wmobley/mcp-suite/gdal-actor`.  Register the actor once; paste
  the actor ID into `GEO_ACTOR_ID`.  **dso-geo never registers actors at
  runtime.** This is required for GDAL tools, but not for reverse geocoding.
- Tapis JWT (obtained via `scripts/tapis-oauth/get-jwt.sh`).  Pass as
  `tapis_token` per-call argument or set `GEO_TAPIS_TOKEN` env fallback
  (metadata tools only). Reverse-geocoding tools ignore `tapis_token`.

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEO_ACTOR_ID` | YES | — | Pre-registered Abaco actor ID |
| `TAPIS_BASE` | no | `https://portals.tapis.io` | Tapis tenant base URL |
| `CKAN_URL` | no | `http://localhost:5001` | CKAN portal base URL |
| `GEO_ALLOWED_CKAN_HOST` | no | CKAN_URL hostname | SSRF guard hostname |
| `GEO_TAPIS_TOKEN` | no | — | Env fallback JWT (metadata only; warns on production) |
| `GEO_POLL_TIMEOUT_S` | no | `10` | HTTP timeout per poll call (seconds) |
| `GEO_POLL_RETRIES` | no | `1` | Retries per poll call |

## Tools

### Geocoding (read-only, synchronous)

These tools call the public Nominatim reverse-geocoding API directly and return
immediately. They do **not** dispatch Abaco, return `execution_id`, or require
polling.

**`reverse_geocode(lat, lon, zoom=None, tapis_token=None)`**
Resolve one WGS84 coordinate to a human-readable place label. When `zoom` is
omitted, the tool tries zoom 14 and falls back to zoom 10 if the first result is
only state/country level. `tapis_token` is accepted for signature consistency
but ignored.

**`reverse_geocode_bbox(bbox=None, dataset_id=None, tapis_token=None)`**
Resolve a representative place label for an extent. Pass exactly one of
`bbox=[west, south, east, north]` or `dataset_id`; the dataset path reads CKAN's
`spatial` GeoJSON field. The bbox algorithm samples centroid + southwest +
northeast, accepts city/county labels when a majority agrees, and otherwise
falls back to the centroid's finest available place field.

### Metadata (read-only)

**`gdalinfo_extract(resource_id, include_stats=True, tapis_token=None)`**
Extract GDAL metadata from a raster resource. Returns `execution_id` immediately;
poll with `get_execution_status`.

**`gdalinfo_summary(dataset_id, tapis_token=None)`**
Extract metadata from all rasters in a CKAN dataset (max 10). Returns a list
of `execution_id`s.

### Transformations (token required, auto-register output to CKAN)

All transform tools require an explicit `tapis_token`.  They include a `ckan`
block in the actor message so the SAME execution registers the output as a
new CKAN resource automatically (single-actor mode).

**`reproject_raster(resource_id, target_crs, output_name, register_to_dataset=None, tapis_token=None)`**
Reproject a raster to an EPSG CRS via gdalwarp.

**`convert_to_cog(resource_id, output_name, compression="deflate", register_to_dataset=None, tapis_token=None)`**
Convert to Cloud-Optimized GeoTIFF via gdal_translate.

**`clip_raster(resource_id, clip_geometry, output_name, register_to_dataset=None, tapis_token=None)`**
Clip to a GeoJSON Polygon/MultiPolygon via gdalwarp -cutline.

**`build_overviews(resource_id, output_name, overview_levels=[2,4,8], register_to_dataset=None, tapis_token=None)`**
Build overviews on a COPY of a raster via gdaladdo. Source never mutated.

### Status polling

**`get_execution_status(execution_id, tapis_token=None)`**
Poll once; the MCP client/model drives the retry loop. When terminal
(COMPLETE/FAILED/ERROR), fetches actor logs and parses structured JSON.
Returns `result` (actor JSON) on COMPLETE; `error` on FAILED/ERROR.

## Typical workflow

```
1. Use dso-ckan tools to find a dataset and resource_id.
2. For a place label, call reverse_geocode_bbox(dataset_id="...")
   → {"name": "New Braunfels, Texas", "tier": "city", "agreement": true}
3. For raster metadata, call gdalinfo_extract(resource_id, tapis_token="eyJ...")
   → {"execution_id": "abc123", "status": "SUBMITTED"}
4. Poll get_execution_status("abc123", tapis_token="eyJ...")
   → {"status": "RUNNING", ...}  (poll again)
   → {"status": "COMPLETE", "result": {"metadata": {...}}}
```

For transforms, the result also includes:
```json
{
  "status": "COMPLETE",
  "result": {"operation": "reproject", "output_path": "...", ...},
  "registered": {"status": "ok", "resource": {"id": "new-ckan-resource-uuid"}}
}
```

## Composing with dso-ckan

dso-ckan finds datasets and resource IDs; dso-geo operates on them.  The
model uses both servers together:

1. `dso-ckan`: `package_search("twdb-ntgam")` → dataset + resource list
2. `dso-geo`: `gdalinfo_extract(resource_id, tapis_token=...)` → metadata
3. `dso-geo`: `reproject_raster(resource_id, 4326, "out.tif", tapis_token=...)` → new CKAN resource

dso-geo calls CKAN directly for URL resolution and actor registration; it
does NOT call dso-ckan via MCP.

## Security

- **Token handling**: Per-call `tapis_token` args are never stored, logged,
  or returned.  Bearer/JWT patterns are scrubbed from all Tapis error
  responses before they surface to the caller.
- **SSRF guard**: Resolved CKAN download URLs are validated to point at
  `GEO_ALLOWED_CKAN_HOST` (defaults to CKAN_URL hostname) before being
  forwarded to the Abaco actor.
- **Parameter validation**: All params are validated at the MCP layer (and
  again inside the actor) before any actor message is built or submitted.
- **Transform token gate**: Transform tools explicitly require `tapis_token`
  and do NOT fall back to the `GEO_TAPIS_TOKEN` env var, reducing ambient
  write exposure.
- **Nominatim policy guardrails**: Reverse-geocoding calls use an identifying
  User-Agent, in-process cache, no retries, and a one-request-per-second live
  request throttle. Do not use this server for bulk geocoding or grid scans;
  deploy a dedicated provider/self-hosted Nominatim for larger workloads.

## Tests

```bash
cd servers/geo
PATH="$HOME/.local/bin:$PATH" uv run pytest -q
```

All tests are mocked — no live Tapis or CKAN required.
