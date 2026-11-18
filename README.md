# mcp-suite

A suite of [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
servers for the DSO / MODFLOW data platform. The servers are independent stdio
processes that an MCP client (Claude Code, Cursor, Windsurf, …) launches and
**composes** — e.g. the model finds a dataset with the CKAN server, then runs a
GDAL transform on it with the geo server.

## Servers

| Server | Path | What it does | Auth |
|--------|------|--------------|------|
| **ckan** (`dso-ckan`) | [`servers/ckan/`](servers/ckan/) | Read/search CKAN datasets, inspect scheming schemas, validate metadata, and (token-gated) create/update datasets + upload resources. | Reads anonymous; writes use a Tapis JWT via `X-Tapis-Token`. |
| **geo** (`dso-geo`) | [`servers/geo/`](servers/geo/) | Run GDAL metadata + transforms (reproject, COG, clip, overviews) on CKAN-hosted rasters via **Tapis Abaco actors**, reading inputs over `/vsicurl/` and registering outputs back to CKAN. | Tapis JWT (per call). |
| **dynamo** (`dso-dynamo`) | [`servers/dynamo/`](servers/dynamo/) | Search, configure, author, run and monitor DYNAMO models on the Tapis Pods (model catalog, Ensemble Manager model-bridge runs, SVO adapter, semantic search). Writes are dry-run-first, `confirm`-gated and (catalog) env-gated. | Tapis JWT (per call or `DYNAMO_TAPIS_TOKEN`). |

The ckan and geo servers are implemented and tested; the dynamo server is implemented and unit-tested (mocked HTTP; not yet exercised against the live dev pods). The CKAN server is also live-verified.
For the geo server: Phase-0a **passed** on the live Tapis tenant (the actor
registers, runs, and returns results over Abaco), and the GDAL actor image is
validated on real data ([`servers/geo/gdal-actor/`](servers/geo/gdal-actor/)).
The remaining live step is one end-to-end transform against a TACC-routable
CKAN URL — see [`servers/geo/README.md`](servers/geo/README.md).

## Design docs

- [CKAN MCP server spec](docs/design/2026-06-29-ckan-mcp-server.md) — Implemented
- [Geo (GDAL/Abaco) MCP server spec](docs/design/2026-06-29-geo-actor-mcp-server.md) — Implemented; Phase-0a passed
- [DYNAMO MCP server spec](docs/design/2026-11-18-dynamo-mcp-server.md) — Implemented (mocked-HTTP tested)

## Quick start (CKAN server)

```bash
cd servers/ckan
uv sync
uv run dso-ckan-mcp        # stdio MCP server
```

Register it with your MCP client (absolute path):

```jsonc
{
  "mcpServers": {
    "dso-ckan": {
      "command": "uv",
      "args": ["run", "--directory", "/abs/path/to/mcp-suite/servers/ckan", "dso-ckan-mcp"],
      "env": { "CKAN_URL": "http://localhost:5001" }
    }
  }
}
```

Write tools additionally need a Tapis JWT — see
[`servers/ckan/README.md`](servers/ckan/README.md).

## Quick start (geo server)

One-time: register the persistent Abaco actor (needs a fresh Tapis JWT), then
use its id as `GEO_ACTOR_ID`:

```bash
cd servers/geo
export TAPIS_TOKEN=...                  # fresh Tapis JWT
./register-actor.sh                     # prints the actor id
```

Register the server with your MCP client (composes with `dso-ckan`):

```jsonc
{
  "mcpServers": {
    "dso-geo": {
      "command": "uv",
      "args": ["run", "--directory", "/abs/path/to/mcp-suite/servers/geo", "dso-geo-mcp"],
      "env": {
        "GEO_ACTOR_ID": "<id from register-actor.sh>",
        "TAPIS_BASE": "https://portals.tapis.io",
        "CKAN_URL": "https://<your-ckan-host>"
      }
    }
  }
}
```

Tools take a CKAN resource/dataset id and a per-call `tapis_token`; transforms
register the output back to CKAN automatically. See
[`servers/geo/README.md`](servers/geo/README.md).

## GDAL actor image (geo server)

The geo server dispatches GDAL work to a Tapis Abaco actor running the image in
[`servers/geo/gdal-actor/`](servers/geo/gdal-actor/). CI builds and pushes it to
GHCR on changes:

```
ghcr.io/<owner>/mcp-suite/gdal-actor:<tag>
```

See [`.github/workflows/build-gdal-actor.yml`](.github/workflows/build-gdal-actor.yml).
To run the Tapis-side proof-of-life with the published image, set `ACTOR_IMAGE`
to the pushed tag and follow [`servers/geo/phase0a/README.md`](servers/geo/phase0a/README.md).

## CI

- **build-gdal-actor** — builds + pushes the GDAL actor image to GHCR (on changes
  under `servers/geo/gdal-actor/`, on `v*` tags, or manual dispatch).
- **test** — runs the CKAN server test suite and the actor's validator tests.

## Layout

```
mcp-suite/
├── servers/
│   ├── ckan/      # dso-ckan MCP server (implemented)
│   ├── geo/       # dso-geo: gdal-actor image + phase-0a proof-of-life kit
│   └── dynamo/    # dso-dynamo: DYNAMO models on Tapis Pods (search/configure/author/run/monitor)
├── docs/design/   # design specs (source of truth for decisions)
└── .github/workflows/
```
