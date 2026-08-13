# GDAL Actor (Phase 0b)

A containerised GDAL worker designed to run as a **Tapis Abaco actor** but
locally testable with plain `docker run`. It reads an operation and parameters
from a JSON message, validates them strictly, runs one of five allowlisted GDAL
operations against a `/vsicurl/` HTTP source, writes any output to a controlled
directory, and emits a structured JSON result on stdout.

This is the **Phase 0b** deliverable of the `dso-geo` geo MCP server project.
See the full design spec at
`../../../docs/design/2026-06-29-geo-actor-mcp-server.md`.

---

## Build

```bash
cd ckan-docker/mcp-geo/gdal-actor/
docker build -t dso-geo-gdal-actor:latest .
```

Base image: `ghcr.io/osgeo/gdal:ubuntu-small-3.9.2` (pinned). Already includes
`gdalinfo`, `gdalwarp`, `gdal_translate`, `gdaladdo`, and `python3`. Only
`requests` (for the optional CKAN registration step) is added.

---

## Message schema

The actor reads its input from one of three sources (in priority order):

1. `MSG` environment variable (Abaco convention for deployed actors)
2. `--message '<json>'` CLI argument (local testing)
3. stdin (pipe-friendly local testing)

```json
{
    "operation":    "gdalinfo | reproject | cog | clip | overviews",
    "input_url":    "https://example.com/data/file.tif",
    "output_name":  "result.tif",
    "params": {
        "target_crs":      4326,
        "compression":     "deflate",
        "overview_levels": [2, 4, 8],
        "clip_geometry":   { "type": "Polygon", "coordinates": [[...]] }
    },
    "include_stats": false,
    "read_token":    "<tapis-jwt-for-private-reads>",
    "ckan": {
        "url":        "https://ckan.example.org",
        "token":      "<tapis-jwt>",
        "package_id": "my-dataset",
        "extra":      {}
    }
}
```

### Field reference

| Field | Type | Required | Notes |
|---|---|---|---|
| `operation` | string | yes | One of `gdalinfo`, `reproject`, `cog`, `clip`, `overviews` |
| `input_url` | string | yes | `http(s)` URL or `tapis://system/path`; private reads use `X-Tapis-Token` |
| `output_name` | string | for non-`gdalinfo` ops | Bare filename, validated `^[A-Za-z0-9_\-.]+$`, must end `.tif` |
| `params` | object | depends on op | See per-operation details below |
| `include_stats` | bool | no | `gdalinfo` only; compute band statistics (slower) |
| `read_token` | string | no | Tapis JWT; carried as `X-Tapis-Token` header in `/vsicurl/` HTTP reads for private resources |
| `ckan` | object | no | If present, register the output file to CKAN after the op |

### Per-operation params

| Operation | Required params | Param constraints |
|---|---|---|
| `gdalinfo` | none | `include_stats` (top-level bool) |
| `reproject` | `target_crs` (int) | EPSG code, integer 1–999999 |
| `cog` | `compression` (string) | One of `deflate`, `lzw`, `zstd`, `none` |
| `clip` | `clip_geometry` (dict) | GeoJSON dict, Polygon or MultiPolygon, WGS84 bounds, ≤ 1000 vertices |
| `overviews` | `overview_levels` (list[int]) | Each 2–512, max 10 elements |

---

## Output

Every run emits a single JSON object to stdout.

**Success:**
```json
{
    "status": "ok",
    "operation": "gdalinfo",
    "gdal_version": "GDAL 3.9.2, ...",
    "metrics": { "duration_ms": 1423 },
    "output_path": "/data/out/result.tif",
    "metadata": { ... },
    "registered": { "status": "ok", "resource": { ... } }
}
```

- `output_path` is absent for `gdalinfo`
- `metadata` is present only for `gdalinfo`
- `registered` is present only when a `ckan` block was provided and the op produced a file

**Error:**
```json
{"status": "error", "message": "<scrubbed message>"}
```
Exit code is non-zero on error. Tokens and JWTs are always scrubbed from error
messages and logs.

---

## Two operating modes

### gdal-only (no `ckan` block)

Produce an output file and return `output_path` in the JSON result. The caller
(Abaco pipeline, test script, or the dso-geo MCP server) is responsible for
any downstream registration.

```bash
docker run --rm \
  -e MSG='{"operation":"cog","input_url":"https://...","output_name":"out.tif","params":{"compression":"deflate"}}' \
  -e OUTPUT_DIR=/data/out \
  -v /host/out:/data/out \
  dso-geo-gdal-actor:latest
```

### gdal+register (with `ckan` block)

After the GDAL operation, automatically POST the output file to CKAN
`resource_create`. The `registered` key in the JSON result carries the CKAN
resource dict. This implements the Stage-2 register task in the Abaco pipeline.

```bash
docker run --rm \
  -e MSG='{"operation":"reproject","input_url":"https://...","output_name":"out.tif","params":{"target_crs":4326},"ckan":{"url":"https://ckan.example.org","token":"eyJ...","package_id":"my-dataset"}}' \
  -v /host/out:/data/out \
  dso-geo-gdal-actor:latest
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MSG` | (none) | JSON message (Abaco convention); overrides `--message` and stdin |
| `OUTPUT_DIR` | `/data/out` | Directory for output files; created if absent |
| `ALLOWED_HOST` | (none) | If set, `/vsicurl/` reads are restricted to this host (SSRF control, e.g. `ckan.example.org`) |
| `GDALINFO_TIMEOUT` | `60` | Subprocess timeout in seconds for `gdalinfo` |
| `REPROJECT_TIMEOUT` | `600` | Subprocess timeout for `gdalwarp` (reproject) |
| `COG_TIMEOUT` | `180` | Subprocess timeout for `gdal_translate -of COG` |
| `CLIP_TIMEOUT` | `300` | Subprocess timeout for `gdalwarp -cutline` (clip) |
| `OVERVIEWS_COPY_TIMEOUT` | `180` | Subprocess timeout for `gdal_translate` (copy step in overviews) |
| `OVERVIEWS_ADDO_TIMEOUT` | `300` | Subprocess timeout for `gdaladdo` |

### Token handling for private `/vsicurl/` reads

If `read_token` is present in the message, the actor writes a temporary
`GDAL_HTTP_HEADER_FILE` containing `X-Tapis-Token: <token>` and sets that env
var for the GDAL subprocess. This is preferred over `GDAL_HTTP_HEADERS` because
the token does not appear in the process environment listing. The temp file is
deleted after the subprocess exits whether it succeeds or fails.

---

## Local test

```bash
cd ckan-docker/mcp-geo/gdal-actor/
bash tests/local_test.sh
```

The script:
1. Checks Docker is available; skips gracefully if not.
2. Builds the image.
3. Runs `gdalinfo` against `TEST_COG_URL` (a public COG) and asserts the JSON
   output contains CRS and bands.
4. Runs `cog` conversion and asserts the output `.tif` appears in the mounted
   host directory.

Override the COG URL:
```bash
export TEST_COG_URL="https://your-server.example.com/path/to/file.tif"
bash tests/local_test.sh
```

---

## Unit tests (no Docker needed)

```bash
cd ckan-docker/mcp-geo/gdal-actor/
python3 -m pytest tests/test_validators.py -v
```

No external dependencies required beyond pytest. The tests cover every
validation rule in the injection contract (Decision 7 of the design spec):
operation allowlist, CRS range, output_name path traversal, compression enum,
overview bounds, clip geometry type/WGS84/vertex-cap, input URL scheme + SSRF.

---

## Abaco actor wiring (MSG env)

When registered as a Tapis Abaco actor, the actor container receives its
message via the `MSG` environment variable. The entrypoint reads `MSG` first
(before `--message` and stdin), so the same image works both as a local test
(`--message` or stdin) and as a deployed Abaco actor (`MSG`).

Example Abaco execution payload:
```json
{
  "message": "{\"operation\":\"gdalinfo\",\"input_url\":\"https://...\"}"
}
```
Abaco sets `MSG` to the `message` field value in the container environment.

---

## Pipeline shape (Stage 2 decision — hedged)

The design spec records an open question (Decision S5 / Decisions 15–19) about
whether Stage 2 (CKAN registration) runs as:

- A **separate HTTP-triggered actor** (pure two-stage Abaco pipeline), OR
- **Inline in this actor** (if HTTP-triggered chaining is unavailable).

The `ckan` block in the message schema supports the inline path (the actor
itself posts to CKAN after the GDAL op). The Phase-0a gate (`check-abaco.sh`)
settles which form the pipeline takes. No code changes are required in this
actor for either path — only the pipeline definition differs.

---

## Security notes

- Operations are validated against an allowlist (`gdalinfo`, `reproject`, `cog`,
  `clip`, `overviews`) before any subprocess is launched.
- All subprocess calls use `subprocess.run(..., shell=False)` with a `list[str]`
  args — never string interpolation or `os.system`.
- `output_name` is validated against `^[A-Za-z0-9_\-.]+$` and must end `.tif`;
  path separators and `..` are explicitly rejected. The server prepends the
  controlled `OUTPUT_DIR`.
- Tokens are never written to logs or error messages (scrubbed via regex).
- `/vsicurl/` auth uses `GDAL_HTTP_HEADER_FILE` (temp file) rather than
  `GDAL_HTTP_HEADERS` env to avoid token exposure in process listings.
- `ALLOWED_HOST` restricts which host the actor may read from (SSRF control).
