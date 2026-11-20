"""
Author / register tools — create SVOs, models, ETL pieces and data objects.

Tools registered here (7)
-------------------------
  create_standard_variable  catalog  POST /standardvariables
  create_model              catalog  composite: Model -> SoftwareVersion -> DatasetSpecifications
                                     -> ModelConfiguration (+FK/junction PUTs)
  create_etl_piece          catalog  same composite, for an ETL piece (a ModelConfiguration bound to a Tapis app)
  create_etl_function_piece hasura   insert_modelcatalog_etl_process_one (Python source, no Tapis app)
  delete_etl_function_piece hasura   delete_modelcatalog_etl_process_by_pk (the caller's own piece only)
  register_data_object      adapter  POST /data-objects (adapter-direct)
  test_etl_piece            adapter  POST /transform-specs/{id}/test (adapter-direct)

The two adapter tools stay direct because the design's front routes (EM POST /v1/data-objects,
POST /v1/etl-pieces/{id}/test) are not on the EM yet.

Gates: dry-run by default; live needs confirm=true; the catalog/registry tools
also need DYNAMO_ENABLE_CATALOG_WRITES=1. Writes are NOT atomic (several REST
calls) and the only delete tool is delete_etl_function_piece: on a partial failure
the tool reports exactly what was created.

KNOWN LIMITATION (verified in register_to_mint.py): the REST API cannot link a
VariablePresentation to a StandardVariable/Unit on create. The tools create the
presentation and report the exact link as ``follow_up``; it must be applied
server-side (Hasura) — the registration script does the same.
"""

from __future__ import annotations

import ast
import re
import secrets
from typing import Any

from .. import audit
from ..client import DynamoAPIError, DynamoClient, enc
from ..config import Settings
from . import _catalog as cat
from ._common import backend_error, run, write_gate

ETL_ID_PREFIX = "https://w3id.org/okn/i/mint/etl/"
MAX_SOURCE_BYTES = 64_000
# Pipeline args the adapter never hands to stored code (svo-adapter stored_source.allowed_args).
REFUSED_ARGS = frozenset({
    "credentials_url", "signal_url", "signal_token", "signal_expires", "token_url",
    "token_fetch_token", "token_fetch_expires", "ckan_token", "stac_token"})
# Arguments stored code may always be handed, without declaring them.
ALWAYS_BINDABLE = frozenset({"tapis_token", "source_uri"})
CONTRACT_FIELDS = ("standard_variable_uri", "unit", "format", "dimensionality", "spatial_type",
                   "crs_requirement", "temporal_resolution")
CREATE_ETL_PROCESS = """
mutation CreateETL($object: modelcatalog_etl_process_insert_input!) {
  insert_modelcatalog_etl_process_one(object: $object) { id label owner_username visibility }
}
"""

GET_ETL_PROCESS = """
query GetETL($id: String!) {
  modelcatalog_etl_process_by_pk(id: $id) { id label version owner_username visibility runtime_kind }
}
"""
DELETE_ETL_PROCESS = """
mutation DeleteETL($id: String!) {
  delete_modelcatalog_etl_process_by_pk(id: $id) { id label owner_username }
}
"""


def _check_source(python_source: str, entrypoint: str | None) -> tuple[list[str], list[str]]:
    """Static checks on a piece's source. Returns (errors, warnings).

    These catch mistakes; they say nothing about whether the code is safe.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(python_source, str) or not python_source.strip():
        return ["python_source is required"], warnings
    if len(python_source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return [f"python_source is larger than {MAX_SOURCE_BYTES} bytes"], warnings
    try:
        tree = ast.parse(python_source)
    except SyntaxError as exc:
        return [f"python_source does not parse: {exc.msg} (line {exc.lineno})"], warnings
    except (ValueError, RecursionError, MemoryError) as exc:
        return [f"python_source does not parse: {type(exc).__name__}"], warnings
    if entrypoint:
        if not entrypoint.isidentifier():
            errors.append("entrypoint must be a valid Python function name")
        else:
            found = [n for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == entrypoint]
            if not found:
                errors.append(f"python_source defines no top-level function named {entrypoint!r}")
            elif isinstance(found[-1], ast.AsyncFunctionDef):
                errors.append("the entrypoint must be a plain function, not async")
            elif len(found[-1].args.args) + len(found[-1].args.posonlyargs) > 1:
                warnings.append("the entrypoint is called with one argument, a dict of its inputs")
    elif "set_output" not in python_source:
        warnings.append(
            "no entrypoint was given and the source never calls set_output: as a script it "
            "must call ctx.set_output(\"result\", ...) or the next step gets nothing")
    return errors, warnings


# Same glob alphabet the registration script enforces for archived output files.
ARCHIVED_FILE_RE = re.compile(r"^[A-Za-z0-9._\-*?\[\]{}:]{1,255}$")


def register(mcp: Any, client: DynamoClient, settings: Settings) -> None:
    # ------------------------------------------------------------------ SVO
    @mcp.tool()
    def create_standard_variable(
        label: str,
        description: str = "",
        variable_id: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Create a StandardVariable (SVO) in the catalog.

        The id defaults to a slug of the label under https://w3id.org/okn/i/mint/.
        If it already exists nothing is changed. A canonical SVO IRI (same_as)
        cannot be set through the REST API and must be applied server-side.
        """
        vid = cat.mint_id(variable_id or cat.slugify(label))
        payload = {"id": vid, "type": ["StandardVariable"], "label": cat.arr(label),
                   "description": cat.arr(description)}
        steps = [cat.step("POST", "standardvariables", payload)]
        existing = None
        try:
            existing = client.get("api", f"/standardvariables/{enc(vid)}", token=tapis_token)
        except DynamoAPIError as exc:
            if exc.status not in (404,):
                return backend_error(exc)
        if existing:
            return {"error": "already_exists", "variable_id": vid, "existing": existing,
                    "message": "A StandardVariable with this id exists; nothing to create."}
        if dry_run:
            return {"dry_run": True, "variable_id": vid, "steps": cat.describe(steps),
                    "live_writes_enabled": settings.enable_catalog_writes}
        blocked = write_gate(settings, "create_standard_variable",
                             client.effective_token(tapis_token), confirm=confirm, catalog=True)
        if blocked:
            return blocked
        result = cat.execute(client, steps, tapis_token)
        audit.log_write("create_standard_variable", {"label": label}, "api",
                        "ok" if result["ok"] else "error", vid)
        return result

    # ------------------------------------------------- model / ETL composite
    def _validate_io(kind: str, items: list[dict[str, Any]], errors: list[str]) -> None:
        for i, it in enumerate(items):
            where = f"{kind}[{i}]"
            if not it.get("label"):
                errors.append(f"{where}: label is required")
            if not it.get("format"):
                errors.append(f"{where}: format is required (e.g. 'tif', 'csv', 'nc')")
            af = it.get("archived_file")
            if af and not ARCHIVED_FILE_RE.match(af):
                errors.append(f"{where}: archived_file '{af}' has characters outside the allowed glob alphabet")
            if kind == "inputs" and it.get("is_optional") is not None and not isinstance(it["is_optional"], bool):
                errors.append(f"{where}: is_optional must be a boolean")

    def _resolve_svos(items: list[dict[str, Any]], token: str | None, errors: list[str]) -> None:
        for sv in sorted({it["standard_variable_id"] for it in items if it.get("standard_variable_id")}):
            try:
                client.get("api", f"/standardvariables/{enc(cat.mint_id(sv))}", token=token)
            except DynamoAPIError as exc:
                errors.append(
                    f"standard_variable_id '{sv}' did not resolve ({exc.status}); "
                    "create it first with create_standard_variable")

    def _plan(
        *,
        kind: str,
        label: str,
        description: str,
        tapis_app_id: str | None,
        tapis_app_version: str | None,
        container_image: str | None,
        component_location: str | None,
        inputs: list[dict[str, Any]],
        outputs: list[dict[str, Any]],
        parameters: list[dict[str, Any]],
        version_label: str | None,
        version_id: str | None,
        software_version_id: str | None,
        model_id: str | None,
        keywords: list[str] | None,
        license: str | None,
        website: str | None,
        usage_notes: str | None,
        token: str | None,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if not label.strip():
            errors.append("label is required")
        if kind == "etl_piece":
            if not tapis_app_id:
                errors.append("tapis_app_id is required: an ETL piece is a catalog configuration bound to a Tapis app")
            if not inputs:
                errors.append("an ETL piece needs at least one input spec")
            if not outputs:
                errors.append("an ETL piece needs at least one output spec")
        else:
            if not (inputs or outputs):
                errors.append("provide at least one input or output spec")
            if not tapis_app_id:
                warnings.append("no tapis_app_id: Ensemble Manager cannot execute this configuration until it is bound to a Tapis app")
        _validate_io("inputs", inputs, errors)
        _validate_io("outputs", outputs, errors)
        _resolve_svos(inputs + outputs, token, errors)
        if errors:
            return {"valid": False, "errors": errors, "warnings": warnings}

        slug = cat.slugify(label)
        mid = cat.mint_id(model_id or slug)
        vid = cat.mint_id(f"{slug}-{cat.slugify(version_id or '1')}")
        cfg_id = cat.mint_id(f"{slug}-cfg")
        steps: list[dict[str, Any]] = []
        follow_up: list[str] = []

        if software_version_id:
            vid = software_version_id
        else:
            steps.append(cat.step("POST", "models", {
                "id": mid, "type": ["Model"], "label": cat.arr(label),
                "description": cat.arr(description),
                "keywords": cat.arr(", ".join(keywords or [])),
                **({"license": cat.arr(license)} if license else {}),
                **({"website": cat.arr(website)} if website else {}),
            }))
            steps.append(cat.step("POST", "softwareversions", {
                "id": vid, "type": ["SoftwareVersion"],
                "label": cat.arr(version_label or label),
                "description": cat.arr(description),
                "version_id": cat.arr(version_id or "1"),
                "softwareId": [mid],
            }))

        def spec_node(prefix: str, i: int, it: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": cat.mint_id(it.get("id") or f"{slug}-{prefix}-{cat.slugify(it['label'])}"),
                "type": ["DatasetSpecification"],
                "label": cat.arr(it["label"]),
                "description": cat.arr(it.get("description", "")),
                "has_format": cat.arr(it["format"]),
                "position": cat.arr(str(it.get("position") or i)),
            }

        in_nodes = [spec_node("input", i, it) for i, it in enumerate(inputs, start=1)]
        out_nodes = [spec_node("output", i, it) for i, it in enumerate(outputs, start=1)]
        for node in in_nodes + out_nodes:
            steps.append(cat.step("POST", "datasetspecifications", node))

        # SVO bindings: create the presentation; the SVO/unit link needs a server-side step.
        for it, node in zip(inputs + outputs, in_nodes + out_nodes):
            if not it.get("standard_variable_id"):
                continue
            vp_id = cat.mint_id(f"{node['id'].rsplit('/', 1)[-1]}-vp")
            steps.append(cat.step("POST", "variablepresentations", {
                "id": vp_id, "type": ["VariablePresentation"], "label": node["label"]}))
            steps.append(cat.step("PUT", "datasetspecifications",
                                  {"id": node["id"], "hasPresentation": [{"id": vp_id}]}, node["id"]))
            unit = it.get("unit_id")
            follow_up.append(
                f"Link presentation {vp_id}: set has_standard_variable="
                f"{cat.mint_id(it['standard_variable_id'])}"
                + (f", uses_unit={cat.mint_id(unit)}" if unit else "")
                + " (not settable via the catalog REST API; apply via Hasura).")

        cfg: dict[str, Any] = {
            "id": cfg_id, "type": ["ModelConfiguration"], "label": cat.arr(f"{label} configuration"),
            "description": cat.arr(description),
            "keywords": cat.arr(", ".join(keywords or [])),
            "usage_notes": cat.arr(usage_notes or ""),
        }
        for key, value in (("tapis_app_id", tapis_app_id), ("tapis_app_version", tapis_app_version),
                           ("has_software_image", container_image),
                           ("has_component_location", component_location)):
            if value:
                cfg[key] = cat.arr(value)
        cfg_params = []
        for i, p in enumerate(parameters, start=1):
            if not p.get("label"):
                return {"valid": False, "errors": [f"parameters[{i - 1}]: label is required"], "warnings": warnings}
            pn = {
                "id": cat.mint_id(p.get("id") or f"{slug}-param-{cat.slugify(p['label'])}"),
                "type": ["Parameter"], "label": cat.arr(p["label"]),
                "has_data_type": cat.arr(p.get("data_type", "string")),
                "parameter_type": cat.arr(p.get("parameter_type", "model_param")),
                "position": cat.arr(str(i)),
            }
            if p.get("description"):
                pn["description"] = cat.arr(p["description"])
            if p.get("default_value") is not None:
                pn["has_default_value"] = cat.arr(str(p["default_value"]))
            cfg_params.append(pn)
        if cfg_params:
            cfg["hasParameter"] = cfg_params
        steps.append(cat.step("POST", "modelconfigurations", cfg))
        steps.append(cat.step("PUT", "modelconfigurations",
                              {"id": cfg_id, "softwareVersionId": [vid]}, cfg_id))
        # I/O are junction relationships: linked by PUT after the specs exist (never nested in the POST).
        if in_nodes:
            steps.append(cat.step("PUT", "modelconfigurations", {"id": cfg_id, "hasInput": [
                {"id": n["id"], "isOptional": bool(it.get("is_optional", False)),
                 **({"appInputName": it["app_input_name"]} if it.get("app_input_name") else {})}
                for n, it in zip(in_nodes, inputs)]}, cfg_id))
        if out_nodes:
            steps.append(cat.step("PUT", "modelconfigurations", {"id": cfg_id, "hasOutput": [
                {"id": n["id"], **({"archivedFile": it["archived_file"]} if it.get("archived_file") else {})}
                for n, it in zip(out_nodes, outputs)]}, cfg_id))
        return {"valid": True, "warnings": warnings, "configuration_id": cfg_id,
                "software_version_id": vid, "steps": steps, "follow_up": follow_up}

    def _author(tool: str, plan: dict[str, Any], dry_run: bool, confirm: bool, token: str | None) -> Any:
        if not plan["valid"]:
            return {"error": "validation_failed", **plan}
        steps = plan.pop("steps")
        if dry_run:
            return {"dry_run": True, **plan, "steps": cat.describe(steps),
                    "live_writes_enabled": settings.enable_catalog_writes,
                    "note": "Nothing sent. dry_run=false + confirm=true (and the server catalog-write flag) to create."}
        blocked = write_gate(settings, tool, client.effective_token(token), confirm=confirm, catalog=True)
        if blocked:
            return blocked
        result = cat.execute(client, steps, token)
        audit.log_write(tool, {"configuration_id": plan["configuration_id"]}, "api",
                        "ok" if result["ok"] else "error", plan["configuration_id"])
        return {**result, "configuration_id": plan["configuration_id"],
                "software_version_id": plan["software_version_id"],
                "warnings": plan["warnings"], "follow_up": plan["follow_up"]}

    @mcp.tool()
    def create_model(
        label: str,
        description: str,
        inputs: list[dict[str, Any]] | None = None,
        outputs: list[dict[str, Any]] | None = None,
        parameters: list[dict[str, Any]] | None = None,
        version_label: str | None = None,
        version_id: str | None = None,
        tapis_app_id: str | None = None,
        tapis_app_version: str | None = None,
        container_image: str | None = None,
        component_location: str | None = None,
        keywords: list[str] | None = None,
        license: str | None = None,
        website: str | None = None,
        usage_notes: str | None = None,
        model_id: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Create a model end to end: Model -> SoftwareVersion -> I/O DatasetSpecifications -> ModelConfiguration.

        inputs[]:  {label, format, description?, position?, is_optional?, app_input_name?,
                    standard_variable_id?, unit_id?}
        outputs[]: {label, format, archived_file?, standard_variable_id?, unit_id?}
        parameters[]: {label, default_value?, data_type?, parameter_type?, description?}
        Validates first (labels/formats, archived_file glob, standard_variable_ids resolve),
        then dry-runs a diff of every request. Not atomic; reports created objects on failure.
        Catalog write: needs confirm=true AND DYNAMO_ENABLE_CATALOG_WRITES=1.
        """
        plan = _plan(kind="model", label=label, description=description, tapis_app_id=tapis_app_id,
                     tapis_app_version=tapis_app_version, container_image=container_image,
                     component_location=component_location, inputs=inputs or [], outputs=outputs or [],
                     parameters=parameters or [], version_label=version_label, version_id=version_id,
                     software_version_id=None, model_id=model_id, keywords=keywords, license=license,
                     website=website, usage_notes=usage_notes, token=tapis_token)
        return _author("create_model", plan, dry_run, confirm, tapis_token)

    @mcp.tool()
    def create_etl_piece(
        label: str,
        description: str,
        tapis_app_id: str,
        inputs: list[dict[str, Any]],
        outputs: list[dict[str, Any]],
        tapis_app_version: str | None = None,
        container_image: str | None = None,
        component_location: str | None = None,
        parameters: list[dict[str, Any]] | None = None,
        software_version_id: str | None = None,
        keywords: list[str] | None = None,
        usage_notes: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Register an ETL piece: a catalog ModelConfiguration with typed I/O bound to a Tapis app.

        The catalog is the SOLE source of transform specs for the SVO adapter, so ETL
        pieces are authored here (not via the adapter). Needs >=1 input and >=1 output
        spec and a tapis_app_id. Give software_version_id to attach to an existing
        version, else a Model + version are created from the label. After a live create
        the adapter picks the piece up on its next catalog sync. Same spec shapes and
        gates as create_model.

        This authors a `tapis_job` piece only: at run time the adapter emits a Tapis
        Workflows `tapis_job` task that submits a batch job for tapis_app_id and polls
        it, so every run waits in the HPC queue. The Tapis app must already be
        registered (not done by this server). For a light transform prefer
        create_etl_function_piece, which registers inline Python that runs as a
        Workflows `function` task with no queue.
        """
        plan = _plan(kind="etl_piece", label=label, description=description, tapis_app_id=tapis_app_id,
                     tapis_app_version=tapis_app_version, container_image=container_image,
                     component_location=component_location, inputs=inputs, outputs=outputs,
                     parameters=parameters or [], version_label=None, version_id=None,
                     software_version_id=software_version_id, model_id=None, keywords=keywords,
                     license=None, website=None, usage_notes=usage_notes, token=tapis_token)
        return _author("create_etl_piece", plan, dry_run, confirm, tapis_token)

    # ----------------------------------------------------------- data object
    @mcp.tool()
    def register_data_object(
        label: str,
        resource_uri: str,
        variables: list[dict[str, Any]] | None = None,
        description: str | None = None,
        format: str | None = None,
        extension: str | None = None,
        mime_type: str | None = None,
        filename: str | None = None,
        source_catalog: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Register a data object (a source file) with its SVO variable contracts in the SVO adapter.

        variables[]: {standard_variable_uri, local_name?, unit?, dimensionality?, spatial_type?,
                      crs?, grid_id?, grid_description?, temporal_resolution?}
        Owner-scoped model-output objects (owner_execution_id etc.) are server-owned and not
        creatable here. Same gates as the catalog writes. Adapter-direct: no EM route yet.
        """
        if not label.strip() or not resource_uri.strip():
            return {"error": "validation_failed", "errors": ["label and resource_uri are required"]}
        body = {"label": label, "resource_uri": resource_uri, "description": description,
                "format": format, "extension": extension, "mime_type": mime_type,
                "filename": filename, "source_catalog": source_catalog, "variables": variables or []}
        body = {k: v for k, v in body.items() if v not in (None, [])}
        if dry_run:
            return {"dry_run": True, "would_request": "POST /data-objects", "body": body,
                    "live_writes_enabled": settings.enable_catalog_writes}
        blocked = write_gate(settings, "register_data_object", client.effective_token(tapis_token),
                             confirm=confirm, catalog=True)
        if blocked:
            return blocked
        try:
            result = client.post("svo", "/data-objects", token=tapis_token, json_body=body)
        except DynamoAPIError as exc:
            audit.log_write("register_data_object", {"label": label}, "svo", exc.status)
            return backend_error(exc)
        audit.log_write("register_data_object", {"label": label}, "svo", 200,
                        result.get("id") if isinstance(result, dict) else None)
        return result

    # ------------------------------------------------- function ETL piece
    @mcp.tool()
    def create_etl_function_piece(
        label: str,
        description: str,
        inputs: list[dict[str, Any]],
        outputs: list[dict[str, Any]],
        python_source: str,
        entrypoint: str | None = "transform",
        env_from_args: dict[str, str] | None = None,
        parameters: dict[str, Any] | None = None,
        version: str = "1.0.0",
        process_id: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Register a light ETL piece that runs as a Tapis Workflows function task (no HPC queue).

        The piece is a catalog ETL process holding its own Python source. It is created
        PRIVATE and owned by the caller (Hasura sets the owner from the token); only its
        owner can run it. Use create_etl_piece instead for heavy work bound to a Tapis app.

        inputs / outputs: [{standard_variable_uri, unit?, format?, dimensionality?,
                            spatial_type?, crs_requirement?, temporal_resolution?}], at least
                           one of each; every standard variable must exist in the catalog.
        python_source:    either defines the `entrypoint` function, which is called with ONE
                          dict of its task inputs and whose return value becomes the task's
                          "result" (a str as is, anything else as JSON); or, with
                          entrypoint=None, is a script that calls
                          ctx.set_output("result", ...) itself
                          (from owe_python_sdk.runtime import execution_context as ctx).
                          Python 3.11, at most 64 kB. It must install any library it needs.
        env_from_args:    {INPUT_NAME: pipeline_arg} for the run arguments the code reads.
                          "tapis_token" and "source_uri" are allowed; run-signal, token-fetch,
                          CKAN and STAC credentials are refused. A step that follows another
                          function step also receives its result as SOURCE_RESULT.
        parameters:       JSON Schema for the piece's own run arguments:
                          {"properties": {name: {"type", "description", "default"?}},
                           "required": [name, ...]}. Every argument bound in env_from_args,
                          other than tapis_token and source_uri, MUST be declared here: the
                          adapter refuses to hand stored code an argument the piece did not
                          declare. Declared arguments become parameters of every run that
                          uses the piece; give optional ones a default.
        process_id:       defaults to a slug of the label plus a random suffix. The dry run
                          returns the id it would use; pass it back to keep it.

        After a live create the piece appears in list_etl_pieces once the adapter next syncs
        the catalog, and runs only where the adapter has stored-source execution enabled.
        There is no update; an existing id is reported as `exists`. To change a piece,
        register the new version under a new id and remove the old one with
        delete_etl_function_piece, or the planner will choose between the two.
        """
        errors, warnings = _check_source(python_source, entrypoint)
        contracts: list[dict[str, Any]] = []
        for role, items in (("input", inputs), ("output", outputs)):
            if not items:
                errors.append(f"at least one {role} is required")
            for position, item in enumerate(items or []):
                uri = (item or {}).get("standard_variable_uri")
                if not uri:
                    errors.append(f"{role}s[{position}]: standard_variable_uri is required")
                    continue
                contracts.append({"role": role, "position": position,
                                  **{k: item[k] for k in CONTRACT_FIELDS if item.get(k) is not None}})
        refused = sorted(set((env_from_args or {}).values()) & REFUSED_ARGS)
        if refused:
            errors.append(f"env_from_args may not bind: {', '.join(refused)}")
        properties = (parameters or {}).get("properties") or {}
        if parameters is not None and (
                not isinstance(properties, dict)
                or not all(isinstance(v, dict) for v in properties.values())):
            errors.append("parameters.properties must map each argument name to a schema object")
            properties = {}
        refused = sorted(set(properties) & (REFUSED_ARGS | ALWAYS_BINDABLE))
        if refused:
            errors.append(f"parameters may not declare: {', '.join(refused)}")
        undeclared = sorted(set((env_from_args or {}).values())
                            - set(properties) - ALWAYS_BINDABLE - REFUSED_ARGS)
        if undeclared:
            errors.append(
                "env_from_args binds arguments the piece does not declare in parameters: "
                + ", ".join(undeclared))
        missing = sorted(set((parameters or {}).get("required") or []) - set(properties))
        if missing:
            errors.append(f"parameters.required names undeclared arguments: {', '.join(missing)}")
        if not errors:
            for uri in sorted({c["standard_variable_uri"] for c in contracts}):
                try:
                    client.get("api", f"/standardvariables/{enc(uri)}", token=tapis_token)
                except DynamoAPIError as exc:
                    if exc.status == 404:
                        errors.append(f"standard variable not found in the catalog: {uri}")
                    else:
                        return backend_error(exc)
        if errors:
            return {"error": "validation_failed", "valid": False, "errors": errors, "warnings": warnings}

        pid = process_id or f"{ETL_ID_PREFIX}{cat.slugify(label)}-{secrets.token_hex(3)}"
        runtime_json: dict[str, Any] = {
            "language": "python",
            "source": {"language": "python", "code": python_source},
        }
        if entrypoint:
            runtime_json["entrypoint"] = entrypoint
        if env_from_args:
            runtime_json["env_from_args"] = env_from_args
        if properties:
            runtime_json["parameters_schema_json"] = {
                "type": "object", "properties": properties,
                "required": list((parameters or {}).get("required") or []),
            }
        variables = {"object": {
            "id": pid, "label": label, "description": description, "version": version,
            "visibility": "private", "runtime_kind": "tapis_function", "runtime_json": runtime_json,
            "contracts": {"data": contracts},
        }}
        if dry_run:
            return {"dry_run": True, "valid": True, "warnings": warnings, "process_id": pid,
                    "would_request": "POST /v1/graphql (insert_modelcatalog_etl_process_one)",
                    "variables": variables,
                    "live_writes_enabled": settings.enable_catalog_writes,
                    "note": "Nothing sent. Pass process_id to keep this id, then dry_run=false + "
                            "confirm=true (and the server catalog-write flag) to create."}
        blocked = write_gate(settings, "create_etl_function_piece",
                             client.effective_token(tapis_token), confirm=confirm, catalog=True)
        if blocked:
            return blocked
        try:
            data = client.graphql(CREATE_ETL_PROCESS, variables, token=tapis_token)
        except DynamoAPIError as exc:
            if cat._already_exists(exc):
                audit.log_write("create_etl_function_piece", {"process_id": pid}, "hasura", "exists", pid)
                return {"ok": True, "status": "exists", "process_id": pid, "warnings": warnings,
                        "note": "A process with this id already exists; nothing was changed."}
            audit.log_write("create_etl_function_piece", {"process_id": pid}, "hasura", "error", pid)
            return backend_error(exc)
        created = (data or {}).get("insert_modelcatalog_etl_process_one") or {}
        audit.log_write("create_etl_function_piece", {"process_id": pid}, "hasura", "ok", pid)
        return {"ok": True, "status": "created", "process_id": created.get("id") or pid,
                "owner_username": created.get("owner_username"),
                "visibility": created.get("visibility"), "warnings": warnings,
                "next": "The adapter picks the piece up on its next catalog sync; then find it "
                        "with list_etl_pieces and try it with test_etl_piece."}

    # ------------------------------------------- delete function ETL piece
    @mcp.tool()
    def delete_etl_function_piece(
        process_id: str,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Delete a catalog ETL piece the caller owns. This cannot be undone.

        Only the piece's owner can delete it (the catalog enforces this with the caller's
        token); its contracts are deleted with it. Use it to retire a superseded version, so
        the planner does not choose between two pieces with the same inputs and outputs.

        dry_run=true (default) looks the piece up and reports what would be deleted, sending
        no write. dry_run=false + confirm=true deletes (and needs the server catalog-write flag).

        After a delete:
        * the adapter keeps its copy until its next catalog sync, so the piece can still be
          planned and run until then;
        * a stored plan that uses the piece is refused at submit once the adapter has synced;
        * registering a piece under the same id again starts it private and unapproved.
        """
        if dry_run:
            try:
                data = client.graphql(GET_ETL_PROCESS, {"id": process_id}, token=tapis_token)
            except DynamoAPIError as exc:
                return backend_error(exc)
            found = (data or {}).get("modelcatalog_etl_process_by_pk")
            return {"dry_run": True, "process_id": process_id, "found": bool(found), "piece": found,
                    "would_request": "POST /v1/graphql (delete_modelcatalog_etl_process_by_pk)",
                    "live_writes_enabled": settings.enable_catalog_writes,
                    "note": "Nothing sent. The catalog deletes the piece only if the caller owns "
                            "it; `found` only says the caller can see it."}
        blocked = write_gate(settings, "delete_etl_function_piece",
                             client.effective_token(tapis_token), confirm=confirm, catalog=True)
        if blocked:
            return blocked
        try:
            data = client.graphql(DELETE_ETL_PROCESS, {"id": process_id}, token=tapis_token)
        except DynamoAPIError as exc:
            audit.log_write("delete_etl_function_piece", {"process_id": process_id}, "hasura", "error",
                            process_id)
            return backend_error(exc)
        deleted = (data or {}).get("delete_modelcatalog_etl_process_by_pk")
        if not deleted:
            audit.log_write("delete_etl_function_piece", {"process_id": process_id}, "hasura",
                            "not_deleted", process_id)
            return {"ok": False, "status": "not_deleted", "process_id": process_id,
                    "note": "Nothing was deleted: there is no piece with this id, or the caller "
                            "does not own it."}
        audit.log_write("delete_etl_function_piece", {"process_id": process_id}, "hasura", "ok", process_id)
        return {"ok": True, "status": "deleted", "process_id": deleted.get("id") or process_id,
                "label": deleted.get("label"), "owner_username": deleted.get("owner_username"),
                "next": "The adapter drops the piece on its next catalog sync."}

    # ------------------------------------------------------------- test piece
    @mcp.tool()
    def test_etl_piece(
        spec_id: str,
        args: dict[str, Any] | None = None,
        run_name: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Test one registered ETL piece as a one-step pipeline.

        spec_id is the ADAPTER transform-spec id (see list_etl_pieces); a piece authored
        in the catalog appears there after the adapter's catalog sync. dry_run=true
        (default) has the adapter only GENERATE the pipeline definition; dry_run=false +
        confirm=true submits and runs it on Tapis Workflows. Adapter-direct: no EM route yet.
        """
        body = {"args": args or {}, "dry_run": dry_run, "run_name": run_name}
        body = {k: v for k, v in body.items() if v is not None}
        if not dry_run:
            blocked = write_gate(settings, "test_etl_piece", client.effective_token(tapis_token),
                                 confirm=confirm)
            if blocked:
                return blocked
        try:
            result = client.post("svo", f"/transform-specs/{enc(spec_id)}/test",
                                 token=tapis_token, json_body=body)
        except DynamoAPIError as exc:
            if not dry_run:
                audit.log_write("test_etl_piece", {"spec_id": spec_id}, "svo", exc.status)
            return backend_error(exc)
        if not dry_run:
            audit.log_write("test_etl_piece", {"spec_id": spec_id}, "svo", 200)
        return result
