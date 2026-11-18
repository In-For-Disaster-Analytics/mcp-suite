# DYNAMO MCP Server (dso-dynamo)

## Status

**Implemented** (2026-11-18) — `servers/dynamo`, 32 tools, 83 unit tests passing against mocked HTTP. **Not yet exercised against the live dev pods**; deviations and unverified items are listed under Decisions.

## Objective

A standalone Python + FastMCP (stdio) server, `dso-dynamo`, at `mcp-suite/servers/dynamo`, that lets an MCP client (Claude Code etc.) work with the DYNAMO apps running as Tapis Pods: **search** models/SVOs, **configure** models, and **run/monitor** model runs.

## User need

Today these actions need the DYNAMO UI or hand-built curl calls against the dev pods. An MCP surface lets an agent discover a model, check that data can feed it, plan the ETL, submit, and watch the run.

## Current code/system summary (verified by reading the repo)

- Dev pods (`monorepo/deploy/tapis/register_mint_stack.py`): `mintdev{graphql,api,ensemble,svo,semanticsearch,ui}` at `https://<pod>.pods.portals.tapis.io`.
- **model-catalog-api** (`mintdevapi`): REST CRUD (GET+POST verified for `/standardvariables`, `/variablepresentations`, `/models`, `/modelconfigurations`, `/datatransformations`, `/datatransformationsetups`) over softwares, softwareversions, softwareconfigurations, configurationsetups, parameters, datasetspecifications (`model-catalog-api/openapi.yaml`).
- **svo-adapter** (`mintdevsvo`, `svo-adapter-service/docs/api.md`): `/standard-variables`, `/reachable/{id}`, `/readiness/check`, `/plans`, `/plans/discover`, `/plans/discover-sources`, `/plans/model-run`, `/workflows/generate|submit`, `/runs`, `/runs/{id}/poll|cancel|provenance`, `/datasets/find`.
- **Model bridge** = the DYNAMO composite run: an adapter Tapis Workflows DAG containing a model task plus ETL pieces (`monorepo/docs/design/2026-11-05-aoi-etl-piece.md`). Composite submissions (`model_task`) require **Ensemble Manager auth, a server-generated descriptor and an EM `execution_id`** — an MCP client cannot construct these directly, so launching goes through EM.
- **Ensemble Manager unified plan API** (`mintdevensemble`, `mint-ensemble-manager/src/api/api-v1/paths/unifiedExecution.ts`, mounted at `/v1/plans`): `POST /plans` (create plan; options `input_etl`, `strict_inputs`, `publish`, `model_output_key`), `GET /plans/publishers`, `GET /plans/{planId}`, `POST /plans/submit`, `POST /plans/scenario`, `GET /plans/runs`, `GET /plans/runs/{runId}`, `POST /plans/runs/{runId}/cancel`. This is the supported way to launch a model-bridge run (it plans input ETL via the adapter and re-derives steps at submit, `steps_digest`/409 `INPUT_PLAN_CHANGED`).
- Auth: Tapis JWT (`X-Tapis-Token`/Bearer), same as the other suite servers.

## Proposed design

Layout mirrors `servers/ckan`: `src/dso_dynamo_mcp/{server,config,catalog_client,adapter_client,audit,tools/}`, `tests/`, `pyproject.toml`, `README.md`. Base URLs from env (`DYNAMO_API_URL`, `DYNAMO_SVO_URL`, `DYNAMO_EM_URL`, `DYNAMO_SEMANTIC_URL`), defaulting to the dev pods. Token per call or `DYNAMO_TAPIS_TOKEN` env; scrubbed from logs/errors.

| Group | Tools | Backend | Writes |
|---|---|---|---|
| Search/discover | `search_models`, `get_model_configuration` (inputs/outputs/parameter defaults), `search_standard_variables`, `find_reachable_variables`, `find_sources_for_target`, `find_datasets` | catalog API, semantic-search, adapter | no |
| Configure | `check_readiness`, `plan_model_run` (preview), `create_configuration_setup`, `update_setup_parameters` | adapter, catalog API | catalog writes |
| Author/register | `create_standard_variable`, `create_model` (composite: software → version → configuration → I/O dataset specs + parameters, one validated call), `create_etl_piece` (a ModelConfiguration with typed I/O DatasetSpecifications + Tapis app binding, the catalog being the sole transform-spec source), `register_data_object` (adapter `POST /data-objects`), `test_etl_piece` (adapter `/transform-specs/{id}/test`, dry-run) | catalog API, adapter | catalog/adapter writes |
| Run (model bridge) | `list_publishers`, `create_run_plan` (EM `POST /plans`), `get_plan`, `submit_run_plan` (EM `POST /plans/submit`), `create_scenario` | Ensemble Manager | run submit |
| Run (adapter ETL-only) | `generate_workflow`, `submit_etl_workflow` | adapter | run submit |
| Monitor | `list_runs`, `get_run_status`, `get_run_provenance` (adapter `/runs/{id}/provenance`, incl. health/stall), `cancel_run` | EM runs + adapter runs | cancel |

Author tools run validation first (required I/O specs, resolvable standard variables, Tapis app ID present), then return a dry-run diff of the objects to be created; they follow the same gates as other catalog writes and share `DYNAMO_ENABLE_CATALOG_WRITES`. Creation order for `create_model` is top-down (parents first so FKs resolve) and is non-atomic across REST calls: on partial failure the tool reports created ids and does not auto-delete (no delete tools in v1).

Run routing: bridge runs are created and submitted through Ensemble Manager (which calls the adapter); ETL-only workflows go straight to the adapter. Monitoring reads EM run state and the adapter run (provenance, `health`) and reports both.

Safety: every write tool defaults to `dry_run=true`; live write needs `confirm=true`; submit tools forward an `Idempotency-Key` where the backend supports it. No delete tools in v1. Audit log of every write call (tool, target id, outcome; no tokens).

## Files likely affected

All new under `mcp-suite/servers/dynamo/`, plus a row in `mcp-suite/README.md`. No changes to monorepo services. DSO-Architecture docs: add a service page + repo-map entry on implementation.

## API/schema changes

None to existing services. Tool schemas are new.

## Data flow

client → tool → `catalog_client`/`adapter_client` (HTTP, Bearer JWT) → pod → JSON normalized, bounded, token-scrubbed → client. `submit_run`: `plan_model_run` → `generate_workflow` → `/workflows/submit` (dry-run first) → `get_run_status` polling.

## Risks and tradeoffs

- **Catalog write permission gap**: role `user` can update *any* configuration/parameter (Hasura filter `{}`, unfixed as of 2026-11-04). Configure tools would expose that to agents. Mitigation: dry-run + confirm + audit; consider shipping configure tools disabled behind `DYNAMO_ENABLE_CATALOG_WRITES=1` until the gap is fixed.
- Bridge runs go through EM, so the server depends on EM's `/v1/plans` contract (plan digest checks, 422/409 codes) — tools should surface those codes verbatim.
- Long ls6 queue waits (20 min–18 h): status is poll-based; no blocking waits.
- Run-stall/Workflows wedge behavior: surface `health` field from runs so agents can report stalls.
- Dev-only default; pointing at prod is a config change and needs the same gates.

## Alternatives considered

- Put it in `ckan-docker/mcp-server` (originally requested): that dir has no real source, and the working servers live in `mcp-suite/servers/`; chosen location follows the user's later answer.
- Adapter-only run path: rejected for bridge runs (composite descriptors are EM-owned); kept for ETL-only workflows.
- TypeScript/HTTP-hosted: rejected for consistency with existing servers.

## Test plan

Unit tests with `responses`-mocked pod APIs per tool (success, 401, 4xx plan errors like `DEFERRED_EXECUTION_ID_MISMATCH`, token scrubbing, dry-run never hits a write endpoint). Optional `integration`-marked tests against dev pods, auto-skipped if unreachable. Live write validation only with explicit approval.

## Documentation plan

`servers/dynamo/README.md` (tools, env, client config), row in `mcp-suite/README.md`, DSO-Architecture service page + repo-map.

## Rollout/rollback plan

Local stdio only; no deployment. Rollback = remove the MCP client registration.

## Open questions

1. ~~Model-bridge launch~~ — resolved: MCP launches via EM `/v1/plans` (see Decisions).
2. ~~Semantic-search endpoint~~ — resolved: `GET /search?q=&target=svo|model_configuration&limit=&region_id=&category_id=&variable_id=&output_variable_id=&role=input|output` (`semantic-search-service/main.py:304`). `search_models` maps to `target=model_configuration`, `search_standard_variables` to `target=svo`. Also `POST /problem-statements/recommendations` (could back a later `recommend_models` tool).
3. ~~Catalog write tokens~~ — resolved: users write with their own Tapis JWT. `model-catalog-api` (`src/security.ts`) forwards the Bearer token to Hasura, whose auth webhook validates it against Tapis; reads use the server's admin secret. No separate catalog credential.
4. Ship configure tools enabled or env-gated (recommended gated)?

## Decisions

### Implementation deviations (2026-11-18)

- **Tool surface is 32 tools**, a superset of the spec table. Added read tools `list_models`, `list_standard_variables`, `find_targets_for_source`, `list_data_objects`, `list_etl_pieces`, and the write `create_adapter_plan` (stores a draft adapter plan, needed to obtain a `plan_id` for `generate_workflow`/`submit_etl_workflow`).
- **`submit_run` became two tools**: `submit_run_plan` (EM `POST /v1/plans/submit`, model bridge) and `submit_etl_workflow` (adapter `POST /workflows/submit`, ETL-only). No tool accepts `model_task`/`execution_id`.
- **Dry-run semantics are a pure echo** for every run/plan write: nothing is sent. The backends' own `dry_run` flags are NOT pure previews (EM `/plans/scenario` still creates region/thread records; adapter `/workflows/submit` still registers the pipeline in Tapis), so they are exposed separately as `adapter_dry_run` and documented as such. Catalog-write dry-runs may issue validation GETs (parent exists, standard variables resolve) but never a POST/PUT.
- **Gating**: `DYNAMO_ENABLE_CATALOG_WRITES` gates configure+author catalog writes and `register_data_object`. It does not gate run/cancel/`create_adapter_plan`/`generate_workflow`/`test_etl_piece` (those need only `confirm=true` + token). `update_setup_parameters` adds a client-side check that every parameter id belongs to the named setup.
- **`create_model` request sequence** follows `modflow-executables/scripts/register_to_mint.py`: POST models → POST softwareversions with scalar `softwareId` (used instead of nesting under `hasVersion`) → POST datasetspecifications → POST modelconfigurations (no I/O nested) → PUT `softwareVersionId` → PUT `hasInput` / `hasOutput` junctions. Non-atomic; partial failure reports created ids.
- **SVO binding limitation**: the catalog REST API cannot link a VariablePresentation to a StandardVariable/Unit on create (the script patches via Hasura). The tools create the presentation and return the exact link under `follow_up`; the SVO/unit link is NOT applied by the MCP server.
- **Idempotency**: adapter submit sends an `Idempotency-Key` header (generated and returned if the caller gives none). EM `/plans/submit` documents no idempotency key, so none is sent; `create_scenario` uses EM's body `idempotency_key`.
- `check_readiness` is ungated although the adapter records a readiness row (provenance only, no catalog/model state).

### Unverified against a live pod (read from code/openapi only)

- `GET /modelconfigurationsetups/{id}` response shape: `update_setup_parameters` assumes a `hasParameter` list of `{id}`; if absent it refuses (fails safe).
- Whether the dev catalog serves the current schema or the "legacy" mapper (`--legacy-api-schema` in the register script, which drops `tapis_app_id`/`tapis_app_version` and expects scalar columns). Payloads use the current array form.
- Scalar FK names `modelConfigurationId` (setup) and `softwareVersionId`/`softwareId` by analogy with the script; `modelConfigurationId` for setups is inferred from the `model_configuration_id` column, not exercised.
- EM run payload field `adapter_run_ids` (`[{run_id,status}]`) used by `get_run_status` to follow adapter children; best effort, skipped if absent.
- Authored ETL pieces reach the adapter only after its catalog sync (`/admin/sync-from-mint`); `test_etl_piece` takes the adapter spec id.
- No live request has been made to any pod (no read or write).

### Earlier decisions

- 2026-11-18: catalog writes use the caller's Tapis JWT forwarded to Hasura; MCP needs no catalog credential, so write safety rests on Hasura role permissions (see the permission-gap risk).
- 2026-11-18: add Author/register tool group (create SVOs, models, ETL pieces, data objects); `create_model` is one composite call (user: "yes"; composite was the recommendation, user did not object). ETL pieces are authored as catalog ModelConfigurations, not adapter-local specs (`svo-adapter-service/docs/design/2026-07-02-mint-catalog-transform-registry.md`).
- 2026-11-18: model-bridge runs are launched through Ensemble Manager `/v1/plans` (user: "if we need to call the ensemble to kick off a job then we should"); adapter used directly for ETL-only workflows and monitoring. Supersedes the earlier "svo-adapter only" run path.
- 2026-11-18: location `mcp-suite/servers/dynamo`; dev pods; all three tool groups; run path via svo-adapter (user).

## User feedback / decisions

- Scope clarified by user: DYNAMO apps on pods — run models, configure models, SVO/model search (not deployment ops).
