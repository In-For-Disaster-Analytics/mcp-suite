# dso-dynamo — DYNAMO MCP server

An MCP server (Python + [FastMCP](https://gofastmcp.com), stdio) for the **DYNAMO** model-integration
apps running as Tapis Pods: **search** models and standard variables (SVOs), **configure** models,
**author** new SVOs/models/ETL pieces, **run** them, and **monitor** the runs.

Design spec: [`../../docs/design/2026-11-18-dynamo-mcp-server.md`](../../docs/design/2026-11-18-dynamo-mcp-server.md).

## Backends (defaults = DYNAMO dev pods)

| Backend | Env var | Default | Used for |
|---|---|---|---|
| model-catalog-api | `DYNAMO_API_URL` (+ `DYNAMO_API_PREFIX=/v2.0.0`) | `https://mintdevapi.pods.portals.tapis.io` | models, configurations, setups, SVOs, dataset specs |
| mint-ensemble-manager | `DYNAMO_EM_URL` (+ `DYNAMO_EM_PREFIX=/v1`) | `https://mintdevensemble.pods.portals.tapis.io` | **model-bridge runs** (`/v1/plans`) |
| svo-adapter | `DYNAMO_SVO_URL` | `https://mintdevsvo.pods.portals.tapis.io` | SVO discovery, readiness, ETL plans/workflows, run provenance |
| semantic-search | `DYNAMO_SEMANTIC_URL` | `https://mintdevsemanticsearch.pods.portals.tapis.io` | `search_models`, `search_standard_variables` |
| Hasura GraphQL | `DYNAMO_HASURA_URL` | `https://mintdevgraphql.pods.portals.tapis.io` | `create_etl_function_piece` and `delete_etl_function_piece` (the catalog REST API has no route for ETL processes) |

Other settings: `DYNAMO_TAPIS_TOKEN` (fallback JWT), `DYNAMO_ENABLE_CATALOG_WRITES` (default off),
`DYNAMO_HTTP_TIMEOUT` (60 s), `DYNAMO_MAX_RESPONSE_CHARS` (200000). A `.env` in this directory is read.

## Auth

Every backend takes a **Tapis JWT as `Authorization: Bearer`**; catalog writes are checked by Hasura's
role permissions with that same token. Pass `tapis_token` per call, or set `DYNAMO_TAPIS_TOKEN`.
The token is never logged or returned (errors/results are scrubbed). semantic-search is anonymous and is
never sent a token. Tapis JWTs expire in hours.

## Register with an MCP client

```jsonc
{
  "mcpServers": {
    "dso-dynamo": {
      "command": "uv",
      "args": ["run", "--directory", "/abs/path/to/mcp-suite/servers/dynamo", "dso-dynamo-mcp"],
      "env": { "DYNAMO_TAPIS_TOKEN": "<jwt>" }
    }
  }
}
```

## Tools (33)

| Group | Tools | Backend |
|---|---|---|
| **Search / discover** (read-only) | `search_models`, `search_standard_variables`, `list_models`, `get_model_configuration`, `list_standard_variables`, `find_reachable_variables`, `find_targets_for_source`, `find_sources_for_target`, `find_datasets`, `list_data_objects`, `list_etl_pieces` | semantic-search, catalog, adapter |
| **Configure** | `check_readiness`¹, `plan_model_run` (preview), `create_adapter_plan`², `create_configuration_setup`³, `update_setup_parameters`³ | adapter, catalog |
| **Author / register** | `create_standard_variable`³, `create_model`³ (composite), `create_etl_piece`³, `create_etl_function_piece`³, `delete_etl_function_piece`³, `register_data_object`³, `test_etl_piece` | catalog, Hasura, adapter |
| **Run — model bridge** | `list_publishers`, `create_run_plan`², `get_plan`, `submit_run_plan`², `create_scenario`² | Ensemble Manager |
| **Run — adapter ETL-only** | `generate_workflow`², `submit_etl_workflow`² | adapter |
| **Monitor** | `list_runs`, `get_run_status`, `get_run_provenance`, `cancel_run`² | EM + adapter |

¹ records a readiness row in the adapter (provenance only). ² write: dry-run by default. ³ catalog/registry write: dry-run by default **and** off unless `DYNAMO_ENABLE_CATALOG_WRITES=1`.

### What a run needs: run parameters and framing

`create_adapter_plan` returns a stored plan with `run_parameters`, sorted by who supplies each one:

| Group | Meaning |
|---|---|
| `must_supply` | Pass these as `submit_etl_workflow` args; a run fails without them |
| `optional` | A piece's own settings that have a default or may be left out |
| `framing` | What the run is about: its time period (`start_date`, `end_date`) and area (`aoi_geojson_uri`, `lat`, `lon`). Required only when `used_by_plan`; recorded with the run when given |
| `supplied_by_adapter` | Filled in by the service. Do not pass them |

### Model-bridge vs. ETL-only runs

The adapter only accepts *composite* runs (model task + ETL pieces — the "model bridge") from Ensemble
Manager, so those are launched through EM: `create_run_plan` → `submit_run_plan`, or `create_scenario`
(region + thread + plan + run in one call). `submit_etl_workflow` runs ETL-only adapter plans and never
sends a `model_task`/`execution_id`. To monitor, use `get_run_status` (EM run; follows its adapter child
runs and reports their `health`: `ok` / `stalled` / `reported_failed`) and `get_run_provenance`.

### How an ETL piece executes: function task vs. Tapis app

Every run is a [Tapis Workflows](https://tapis.readthedocs.io/en/latest/technical/workflows.html)
pipeline that the SVO adapter generates. Each ETL piece is one task in it, and the piece's `method`
(see `list_etl_pieces`) decides the task type:

| `method` | Workflows task type | What runs | Start-up | Use for |
|---|---|---|---|---|
| `tapis_function` | `function` | Inline Python 3.11, run as a script inside the Workflows cluster. Pieces holding their own source are capped at 600 s; built-in pieces get the engine default of one hour. | No HPC queue | Light transforms: unit conversion, point extraction, boundary queries, format changes, calls to an external API |
| `tapis_job` | `tapis_job` | A batch job for a registered Tapis app (`tapis_app_id`), polled until terminal | Waits in the HPC queue (minutes to hours) and needs an allocation | Heavy work: models, GPU, large memory, long runtimes |

Prefer `tapis_function` unless the piece needs HPC resources.

* `create_etl_piece` authors **`tapis_job` pieces only** and rejects a piece without `tapis_app_id`.
  The Tapis app itself must already be registered; this server has no tool for that.
* `create_etl_function_piece` authors a **`tapis_function` piece from Python source**. It inserts a
  catalog ETL process through Hasura with the caller's token.
  * The piece is created **private**; Hasura sets its owner from the token. Only its owner can run it.
  * The source either defines an entrypoint function (default `transform`), called with one dict of
    its task inputs and whose return value becomes the task's `result`; or, with `entrypoint=None`,
    is a script that calls `ctx.set_output("result", ...)` itself. Tapis Workflows runs the code as
    a script, so the adapter appends the call to the entrypoint.
  * Checked before any write: the source parses and is at most 64 kB, the entrypoint is a
    top-level non-async function, there is at least one input and one output, every standard
    variable exists, `env_from_args` binds no run-signal, token-fetch, CKAN or STAC credential, and
    every other bound argument is declared in `parameters`.
  * `parameters` is a JSON Schema for the piece's own run arguments. The adapter hands stored code
    only `tapis_token`, `source_uri` and the arguments the piece declares, so anything else bound in
    `env_from_args` must be declared. Declared arguments become parameters of each run.
    These checks catch mistakes; they do not vet the code.
  * The piece becomes visible in `list_etl_pieces` after the adapter's next catalog sync, and runs
    only where the adapter has `SVO_ADAPTER_STORED_SOURCE_ENABLED` on.
  * There is no update. The default id carries a random suffix; pass the dry run's `process_id`
    back to keep it.
* `delete_etl_function_piece` deletes a catalog ETL piece **the caller owns**; the catalog enforces
  ownership with the caller's token, and the piece's contracts go with it. It cannot be undone.
  * The dry run looks the piece up and sends no write.
  * To change a piece, register the new version under a new id and delete the old one. Two pieces
    with the same inputs and outputs compete in planning.
  * The adapter keeps its copy until its next catalog sync. After that, a stored plan that uses
    the piece is refused at submit.
* Built-in `tapis_function` pieces (the adapter's own transform types) are registered by the
  adapter's `scripts/register_existing_etl_pieces.py`, not by this server.
* Long-running services and human review steps are not ETL pieces.

## Safety model

* **Every write tool defaults to `dry_run=true`, which sends nothing** and returns a preview (for the
  catalog tools: the exact ordered requests a live call would send). Reads used for validation are allowed.
* A live call needs `dry_run=false` **and** `confirm=true`. Submitting a run starts real compute on Tapis.
* Catalog/registry writes additionally need `DYNAMO_ENABLE_CATALOG_WRITES=1`. The catalog's Hasura role
  permissions are the only server-side guard for *who may change what*; `update_setup_parameters` adds a
  client-side check that each parameter belongs to the named setup.
* Authoring is **not atomic** (several REST calls). On failure the result lists what was created; the only
  delete tool is `delete_etl_function_piece`. POSTs are idempotent (existing rows are reported as `exists`).
* Backend errors come back **verbatim** (`status`, `service`, `body`), e.g. `INPUT_PLAN_CHANGED`,
  `NO_INPUT_TRANSFORM_PATH`, `DEFERRED_EXECUTION_ID_MISMATCH`, `RUN_CANCEL_FORBIDDEN`.
* Live writes are audited to stderr (`AUDIT tool=… target=… status=…`, argument *names* only).
* `create_scenario` and `submit_etl_workflow` send/return an idempotency key; reuse it on retries.
  `adapter_dry_run=true` on those two is **not** a pure preview (EM still creates region/thread records;
  the adapter still registers the pipeline) — use the tool's own `dry_run` for a pure preview.

### Known limitation

The catalog REST API cannot link a VariablePresentation to a StandardVariable/Unit on create (the
registration script patches that via Hasura). `create_model` / `create_etl_piece` create the presentation and
list the exact link under `follow_up`; apply it server-side.

## Develop

```bash
cd servers/dynamo
uv sync --extra dev
uv run pytest            # HTTP is mocked with `responses`; no pod is contacted
uv run dso-dynamo-mcp    # stdio server
```
