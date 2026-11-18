# Authoring function-task ETL pieces from the DYNAMO MCP

## Status

Superseded by `monorepo/docs/design/2026-11-18-adapter-user-scoped-etl-pieces.md`
(2026-11-18). The MCP tool designed here carries over as Phase 6 of that spec, with changes.

## Objective

Let an agent using `dso-dynamo` register a light ETL piece that runs as a Tapis Workflows
`function` task (inline Python, no HPC queue), and make such a piece actually execute the
code that was registered.

## User need

Tapis app jobs wait in the HPC queue, sometimes for hours. Light transforms should run as
workflow function tasks instead. Today `create_etl_piece` can only author app-bound
(`tapis_job`) pieces, so every piece an agent registers pays the queue wait. The first
consumer is the Semantic-Sensemaking / DNO work (dictionary counts, SVO grounding,
`svo_validate`), which is mostly light Python.

## Current code/system summary

Verified by reading the code on 2026-11-18 (`monorepo` branch
`fix/publish-submit-accepts-catalogs-on-old-plans`, `mcp-suite` branch `fix/dfc-hds-gma-actor`).

**Two catalog entities can become an adapter transform spec** (`svo-adapter-service/app/mint_sync.py`):

| Catalog entity | Written through | `method` when no Tapis app |
|---|---|---|
| ModelConfiguration | model-catalog-api REST (what `create_etl_piece` uses) | `tapis_function` |
| `modelcatalog_etl_process` (+ `_contract`) | Hasura GraphQL only | `tapis_function` |

**ETL process table** (`graphql_engine/migrations/1771200020000_modelcatalog_etl_process/up.sql`):
`id, label, description, version, owner_username, visibility (private|shared|public),
runtime_kind (tapis_function|tapis_app), runtime_json`. Contracts are keyed
`(etl_process_id, role, position)`.

**Hasura permissions** (`graphql_engine/metadata/tables.yaml`):

- `user` may insert processes and contracts, with `check: {}` and no column preset.
  `owner_username` is a client-supplied column.
- `user` has no update or delete permission on either table.
- `user` selects processes that are public, shared, or owned by them. Contracts are
  selectable by any user (`filter: {}`).

**How the UI writes one** (`ui-react/src/pages/ETLProcessRegisterPage.tsx`): a single
`insert_modelcatalog_etl_process_one` with nested contracts. The code goes in
`runtime_json.source = {language: "python", code}` and the function name in
`runtime_json.entrypoint`. The UI does not set `owner_username`.

**How the adapter turns a step into a task** (`app/tapis.py`): a step with no Tapis app
becomes `{type: "function", runtime: "python:3.11", installer: "pip", max_exec_time: 600}`
with `code = step.python_source or task_code.get_code(transform_type)`. There is no
`packages` field. `get_code` fails closed for an unknown transform type.

**Defect.** The planner (`app/planner.py`, two places) reads the code from
`parameters_schema_json.metadata.source.code`. The sync (`etl_process_to_spec_row`) puts
the whole `runtime_json` at `metadata.runtime`, so the code is at
`metadata.runtime.source`. Running the sync on a UI-shaped row gives
`metadata.source = None`, `transform_type = "etl_process"`. Such a piece therefore gets the
fail-closed stub, not its own code. A third shape exists in
`tests/test_mint_etl_sync.py`, where `runtime.source` is a plain string. Not confirmed by a
live pipeline run.

**MCP today** (`servers/dynamo`): backends `api`, `em`, `svo`, `semantic`. No Hasura
backend. `create_etl_piece` rejects a piece without `tapis_app_id`.

## Proposed design

Two changes, shipped as two PRs. The adapter fix merges first.

### 1. Adapter: carry catalog-authored source to the function task

In `svo-adapter-service`, resolve a step's source in one helper used by both planner sites:

1. `metadata.source` (current behaviour, kept for fixtures), else
2. `metadata.runtime.source`, accepting `{code: str}` or a plain `str`.

The entrypoint comes from the source dict's `entrypoint`, else `metadata.runtime.entrypoint`.
No schema change and no change to built-in transform types.

### 2. MCP: `create_etl_function_piece`

New backend `hasura` (`DYNAMO_HASURA_URL`, default
`https://mintdevgraphql.pods.portals.tapis.io`, path `/v1/graphql`), sent the caller's Tapis
JWT as `Authorization: Bearer` like the other authenticated backends.

```
create_etl_function_piece(
    label, description, inputs, outputs, python_source,
    entrypoint="transform", version="1.0.0", visibility="private",
    process_id=None, dry_run=True, confirm=False, tapis_token=None)
```

- `inputs` / `outputs`: lists of `{standard_variable_uri, unit?, format?, dimensionality?,
  spatial_type?, crs_requirement?, temporal_resolution?}`. Position is the list index.
- Sends one `insert_modelcatalog_etl_process_one` with nested contracts, the same shape the
  UI sends: `runtime_kind: "tapis_function"`,
  `runtime_json: {language: "python", entrypoint, source: {language: "python", code}}`.
- `id` defaults to `https://w3id.org/okn/i/mint/etl/<slug(label)>`, matching
  `scripts/register_existing_etl_pieces.py`.
- `owner_username` is set from the token's username claim, so a private piece is visible to
  its author. (The UI omits it today; see Risks.)

**Validation before any write** (all local, plus read-only lookups):

- at least one input and one output; each has a `standard_variable_uri`
- `python_source` parses with `ast.parse`, and defines a top-level function named `entrypoint`
- `python_source` is at most 64 kB
- `visibility` is one of the three allowed values
- standard variables resolve (same lookup the other author tools use); unresolved ones are
  errors

**Gates**: identical to the other catalog writes. `dry_run=true` sends nothing and returns
the exact GraphQL variables; a live call needs `dry_run=false`, `confirm=true` and
`DYNAMO_ENABLE_CATALOG_WRITES=1`. Live writes are audited to stderr with argument names
only; the source is never logged.

**Insert-only.** A primary-key conflict is reported as `exists`. There is no update or
delete, because the `user` role has neither.

**Not in scope**: a `packages` field, updating or deleting a piece, authoring `tapis_app`
ETL processes through this tool, and any change to `create_etl_piece`.

## Files likely affected

`monorepo/svo-adapter-service`
- `app/planner.py`: source/entrypoint helper, used at both sites
- `tests/`: sync → planner → generated task test for the UI shape and the string shape

`mcp-suite/servers/dynamo`
- `src/dso_dynamo_mcp/config.py`, `client.py`: `hasura` backend and a GraphQL call helper
- `src/dso_dynamo_mcp/tools/author.py`: the new tool
- `src/dso_dynamo_mcp/server.py`: instructions
- `tests/`: mocked-HTTP tests
- `README.md`: tool table, backend table, the function-vs-app section

`DSO-Architecture/docs`: the MINT platform service page and `dev/environment-variables.md`
(new `DYNAMO_HASURA_URL`); `dev/port-reference.md` currently lists the Hasura production
URL as "not found in source".

## API/schema changes

- New MCP tool `create_etl_function_piece` (count 32 → 33) and env var `DYNAMO_HASURA_URL`.
- No database, Hasura metadata, or REST API change.
- Adapter behaviour change: an ETL process with `runtime_json.source` now runs that source.
  Before, it ran the fail-closed stub.

## Data flow

1. Agent calls `create_etl_function_piece` (dry run), shows the preview, then calls it live.
2. MCP inserts the process and contracts into the catalog through Hasura with the user's JWT.
3. The adapter's catalog sync (`/admin/sync-from-mint`) creates a transform spec with
   `method = tapis_function`; the piece appears in `list_etl_pieces`.
4. `test_etl_piece` or a plan generates a pipeline; the planner copies the source into the
   step; the task runs it as a Workflows `function` task under the submitting user's token.

## Risks and tradeoffs

- **Stored code that Tapis executes.** The function task runs registered Python with the
  submitting user's Tapis token in its environment. A `shared` or `public` piece is code
  written by one user and run with another user's token. This already exists through the
  UI; the MCP makes it scriptable. Default `private` limits it, but see the next two points.
- **`owner_username` is client-supplied.** Hasura does not preset it from the session, so a
  caller can attribute a piece to someone else. The tool sets it from the token, but a
  direct GraphQL caller is not bound by that. Related to the known catalog user-write
  permission gap.
- **Does the sync honour visibility?** Not yet verified whether `/admin/sync-from-mint`
  reads with an admin role and so copies private pieces into the shared transform registry,
  where the planner could choose them for any user. If it does, "private" protects nothing
  at execution time. This must be answered before the live check.
- **No update or delete.** A wrong piece stays until an admin removes it. Versioned ids
  (`…-v2`) are the only correction path for a user.
- **No package installs.** A piece needing a library must install it inside its own code on
  every run, as one built-in piece does. The 600 s cap includes that time.
- **Function runtime limits are undocumented.** The Tapis Workflows docs give no memory, CPU
  or code-size limit and do not say where function code runs.
- **Sync delay.** A piece is not runnable until the adapter syncs. The tool result will say so.

## Alternatives considered

- **Write through the adapter's `POST /transform-specs`.** Rejected: the catalog is the sole
  source of transform specs, and the row would not be a catalog entity.
- **Add a REST route to model-catalog-api.** Cleaner long-term, but a larger change across a
  third service for no functional gain over the path the UI already uses.
- **Allow `create_etl_piece` without `tapis_app_id`.** The ModelConfiguration path has
  nowhere to store Python source.
- **Fix only the MCP.** Rejected: pieces would register and then fail closed at run time.

## Test plan

- Adapter: unit test from a catalog row (UI shape and string shape) through
  `etl_process_to_spec_row` and the planner to the generated task, asserting the task `code`
  and `entrypoint`. Existing `test_tapis_user_functions.py` and `test_mint_etl_sync.py` stay
  green. Run in a clean `develop` worktree (five adapter tests fail locally from the
  git-ignored `.env`).
- MCP (mocked HTTP): dry run sends nothing; each validation error; gate refusals; successful
  insert; primary-key conflict → `exists`; GraphQL `errors` returned verbatim; token and
  source absent from logs and error output.
- Live, with explicit approval: register one trivial private piece on dev, sync, run
  `test_etl_piece`, confirm the task output.

## Documentation plan

- `servers/dynamo/README.md`: replace "This server cannot author them yet" with the new
  tool; add the backend and env var.
- Server instructions and `create_etl_piece` docstring: point to the new tool.
- DSO-Architecture pages listed above.

## Rollout/rollback plan

1. Adapter PR to `develop`, deploy to dev (`mintdevsvo`).
2. MCP PR to `main`. The tool is inert unless `DYNAMO_ENABLE_CATALOG_WRITES=1`.
3. Live check on dev.

Rollback: revert either PR. Rows already inserted need an admin delete in Hasura.

## Open questions

1. ~~Does the adapter sync copy private ETL processes into the shared registry?~~ Answered
   by review: yes. See "Review findings" under Decisions. The design above must be revised
   before approval.
2. Should `visibility` be restricted to `private` in v1 until the ownership preset is fixed
   in Hasura?
3. Is the 64 kB source cap right, given Workflows documents no limit?
4. Should the UI also set `owner_username`? Out of scope here, but the same gap.

## Decisions

- 2026-11-18: Light ETL pieces run as Workflows function tasks; Tapis apps are for heavy
  work only (user).
- 2026-11-18: One spec, two PRs, adapter fix first (user approved the plan).
- 2026-11-18: Default visibility `private`; no `packages` field in v1 (proposed, accepted
  with the plan).

### Review findings (2026-11-18) — design NOT approvable as written

`security-reviewer`: no-go for the adapter fix as specced. `skeptic`: revise. The "Proposed
design" section above is unchanged and is superseded by these findings until it is rewritten.

Blocking, verified in code:

1. **Cross-user code execution.** The adapter uses the Hasura admin secret whenever one is
   set and ignores the caller's token (`app/hasura.py:34-41`). The sync therefore reads
   private pieces, drops `owner_username`, keeps `visibility` only as metadata, and writes
   one shared registry that the planner reads unfiltered. The planner picks pieces by SVO
   contract match, so one user's stored code can be routed into another user's run. Today
   the source-path defect is the only thing preventing this; fixing it alone enables it.
2. **Unauthenticated adapter endpoints.** `/admin/sync-from-mint` and `POST /transform-specs`
   never validate the bearer token (`app/main.py:281`, `750`, `960`). The second writes the
   registry directly with the admin secret.
3. **Inputs and chaining are not wired for a generic piece.** Task inputs come only from
   `runtime_json.env_from_args`, which neither the UI nor this spec's payload sends, and the
   upstream-output handoff is hard-wired to `unit_convert` (`app/tapis.py:988-1015`). A
   piece registered as specced would get no input. The statement above that the user's token
   is "in its environment" is wrong: it is present only if mapped.
4. **Author-controlled `env_from_args`.** Because the author chooses the mapping, they can
   map the submitter's `tapis_token` into their code. Secret binding is blocked for input
   steps but not main steps.
5. **Owner spoofing.** As already listed under Risks; needs a Hasura insert preset.

Not yet verified (must be settled by one live hand-written function task):

6. Whether the Workflows runtime calls `entrypoint`, with what arguments, and what the task
   must emit. Three conventions exist in the repo (`def transform()`, `def main(inputs)`,
   module-level script calling `ctx.set_output`).
7. Whether the 600 s cap applies to main function steps (set explicitly only for input steps).

Other gaps: ETL rows removed from the catalog are never deleted from the registry by the
sync; label-slug ids collide with no recovery path; no kill switch for stored-source
execution.

## User feedback / decisions

- 2026-11-18: "Apps can take hours to start up so if they aren't heavy pieces then a
  workflow task with code is preferred."
- 2026-11-18: Plan approved ("Do it"); spec to be reviewed by `skeptic` and
  `security-reviewer` before implementation.
