"""
Configure tools — check/plan (through the EM's adapter forward) and catalog setup writes.

Tools registered here (5)
-------------------------
  check_readiness          EM forward   POST /v1/adapter/readiness/check  (records a readiness row)
  plan_model_run           EM forward   POST /v1/adapter/plans/model-run  (preview only, nothing stored)
  create_adapter_plan      EM           POST /v1/plans          (executor=svo_adapter; stores a draft plan)

check_readiness and plan_model_run go through the EM's allowlisted adapter forward with
the caller's token; no tool calls the adapter host.
  create_configuration_setup  catalog   POST /modelconfigurationsetups (+ FK PUT) — catalog-gated
  update_setup_parameters     catalog   PUT  /parameters/{id}          — catalog-gated

Catalog writes are dry-run by default, need ``confirm=true`` to go live, and are
additionally disabled unless ``DYNAMO_ENABLE_CATALOG_WRITES=1``.
"""

from __future__ import annotations

from typing import Any

from .. import audit
from ..client import DynamoAPIError, DynamoClient, enc
from ..config import Settings
from . import _catalog as cat
from ._common import backend_error, bound, run, run_parameters, write_gate


def register(mcp: Any, client: DynamoClient, settings: Settings) -> None:
    @mcp.tool()
    def check_readiness(
        data_object_id: str,
        dataset_specification_id: str | None = None,
        target_contract: dict[str, Any] | None = None,
        model_configuration_id: str | None = None,
        tapis_token: str | None = None,
    ) -> Any:
        """Check whether a registered data object satisfies a model input requirement.

        Give EITHER dataset_specification_id (a catalog input spec) OR an inline
        target_contract ({standard_variable_uri, unit, format, ...}). Returns
        ready / transform_required / incompatible with per-dimension detail.
        Note: the adapter records each check as a readiness row (provenance); it
        changes no catalog or model state. Through the EM's adapter forward.
        """
        body = {
            "data_object_id": data_object_id,
            "dataset_specification_id": dataset_specification_id,
            "target_contract": target_contract,
            "model_configuration_id": model_configuration_id,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return run(lambda: client.post("adapter", "/readiness/check", token=tapis_token, json_body=body))

    @mcp.tool()
    def plan_model_run(
        run_spec_id: str,
        source_ids: list[str] | None = None,
        tapis_token: str | None = None,
    ) -> Any:
        """Preview an ETL DAG that resolves EVERY input of a multi-input model from registered data objects.

        Read-only: returns per-input resolution (satisfied?, chosen source, ETL chain),
        whether the plan is complete, and the converging plan_json. Nothing is stored.
        source_ids optionally restricts which data objects may be used. Through the EM's
        adapter forward.
        """
        body = {"run_spec_id": run_spec_id, "source_ids": source_ids}
        body = {k: v for k, v in body.items() if v is not None}
        return run(lambda: bound(
            client.post("adapter", "/plans/model-run", token=tapis_token, json_body=body),
            settings, tapis_token))

    @mcp.tool()
    def create_adapter_plan(
        data_object_id: str,
        target_dataset_specification_id: str | None = None,
        target_model_configuration_id: str | None = None,
        target_contract: dict[str, Any] | None = None,
        piece_ids: list[str] | None = None,
        allow_unapproved_piece_ids: list[str] | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Find and STORE a draft ETL plan that closes the gap between a data object and a target.

        Without piece_ids the adapter searches for the shortest chain. When two pieces fit the
        same step it cannot tell them apart, so the plan may not be the flow you meant: always
        read the returned steps. With piece_ids (an ordered list of catalog piece ids, as
        create_etl_function_piece returned them) it plans exactly that chain, or answers 422
        PIECE_NOT_IN_REGISTRY (unknown, not visible to you, or not yet synced) or
        PIECE_DOES_NOT_FIT (with the position of the first step that does not fit).

        allow_unapproved_piece_ids names shared or public pieces whose stored code no admin has
        approved and that you agree to run with your own Tapis token. Never needed for your own
        pieces.

        The plan is stored through the EM (POST /v1/plans, executor=svo_adapter). The returned
        plan_id (prefixed `svo_`) feeds generate_workflow / submit_etl_workflow.
        dry_run=true (default) only echoes the request; dry_run=false + confirm=true stores the plan.

        A stored plan comes back with `run_parameters`, which says what a run of it needs:
        `must_supply` (pass these as submit_etl_workflow args), `optional`, `framing` (the time
        period and area the run is about: required only when `used_by_plan`, and recorded with
        the run when given) and `supplied_by_adapter` (never pass these).
        """
        body = {
            "data_object_id": data_object_id,
            "target_dataset_specification_id": target_dataset_specification_id,
            "target_model_configuration_id": target_model_configuration_id,
            "target_contract": target_contract,
            "piece_ids": piece_ids,
            "allow_unapproved_piece_ids": allow_unapproved_piece_ids,
        }
        body = {k: v for k, v in body.items() if v is not None}
        if dry_run:
            return {"dry_run": True, "would_request": "POST /v1/plans", "body": body,
                    "note": "Nothing sent. Re-call with dry_run=false and confirm=true to store the plan."}
        tok = client.effective_token(tapis_token)
        blocked = write_gate(settings, "create_adapter_plan", tok, confirm=confirm)
        if blocked:
            return blocked
        try:
            result = client.post("em", "/plans", token=tapis_token,
                                 json_body={"executor": "svo_adapter", "adapter_request": body})
        except DynamoAPIError as exc:
            audit.log_write("create_adapter_plan", {"data_object_id": data_object_id}, "em", exc.status)
            return backend_error(exc)
        audit.log_write("create_adapter_plan", {"data_object_id": data_object_id}, "em", 200,
                        result.get("plan_id") if isinstance(result, dict) else None)
        if isinstance(result, dict) and isinstance(result.get("plan_json"), dict):
            result = {**result, "run_parameters": run_parameters(result["plan_json"])}
        return result

    @mcp.tool()
    def create_configuration_setup(
        model_configuration_id: str,
        label: str,
        description: str | None = None,
        region: str | None = None,
        parameters: list[dict[str, Any]] | None = None,
        setup_id: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Create a ModelConfigurationSetup (a region/parameter-specific variant) under a configuration.

        parameters: optional list of {label, default_value, data_type="string",
        parameter_type="model_param", description}. The Tapis execution bindings
        (app id/version/image/component) are copied from the parent configuration
        when it exposes them. Catalog write: dry-run by default, needs confirm=true
        AND DYNAMO_ENABLE_CATALOG_WRITES=1 on the server.
        """
        try:
            parent = client.get("api", f"/modelconfigurations/{enc(model_configuration_id)}",
                                token=tapis_token)
        except DynamoAPIError as exc:
            return backend_error(exc)  # an unresolvable parent is a validation failure
        sid = cat.mint_id(setup_id or f"{cat.slugify(label)}-setup")
        node: dict[str, Any] = {
            "id": sid,
            "type": ["ModelConfigurationSetup"],
            "label": cat.arr(label),
        }
        if description:
            node["description"] = cat.arr(description)
        if region:
            node["has_region"] = cat.arr(region)
        for dest, keys in (
            ("tapis_app_id", ("tapis_app_id", "tapisAppId")),
            ("tapis_app_version", ("tapis_app_version", "tapisAppVersion")),
            ("has_software_image", ("has_software_image", "hasSoftwareImage")),
            ("has_component_location", ("has_component_location", "hasComponentLocation")),
        ):
            value = cat.first(parent, *keys)
            if value:
                node[dest] = cat.arr(value)
        params = []
        for i, p in enumerate(parameters or [], start=1):
            if not p.get("label"):
                return {"error": "invalid_parameter", "message": f"parameters[{i - 1}] needs a label"}
            pn = {
                "id": cat.mint_id(p.get("id") or f"{cat.slugify(sid.rsplit('/', 1)[-1])}-param-{cat.slugify(p['label'])}"),
                "type": ["Parameter"],
                "label": cat.arr(p["label"]),
                "has_data_type": cat.arr(p.get("data_type", "string")),
                "parameter_type": cat.arr(p.get("parameter_type", "model_param")),
                "position": cat.arr(str(i)),
            }
            if p.get("description"):
                pn["description"] = cat.arr(p["description"])
            if p.get("default_value") is not None:
                pn["has_default_value"] = cat.arr(str(p["default_value"]))
            params.append(pn)
        if params:
            node["hasParameter"] = params
        steps = [
            cat.step("POST", "modelconfigurationsetups", node),
            cat.step("PUT", "modelconfigurationsetups",
                     {"id": sid, "modelConfigurationId": [model_configuration_id]}, sid),
        ]
        if dry_run:
            return {"dry_run": True, "setup_id": sid, "steps": cat.describe(steps),
                    "live_writes_enabled": settings.enable_catalog_writes,
                    "note": "Nothing sent. dry_run=false + confirm=true (and the server catalog-write flag) to create."}
        tok = client.effective_token(tapis_token)
        blocked = write_gate(settings, "create_configuration_setup", tok, confirm=confirm, catalog=True)
        if blocked:
            return blocked
        result = cat.execute(client, steps, tapis_token)
        audit.log_write("create_configuration_setup", {"model_configuration_id": model_configuration_id},
                        "api", "ok" if result["ok"] else "error", sid)
        return result

    @mcp.tool()
    def update_setup_parameters(
        setup_id: str,
        parameter_defaults: dict[str, str],
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Change default values of parameters that belong to ONE setup.

        parameter_defaults maps parameter id -> new default value. Every id is
        checked against the setup's own parameter list first, so this cannot be
        used to rewrite another model's parameters (the catalog's server-side
        permission for that is still being tightened). Catalog write: dry-run by
        default, needs confirm=true AND DYNAMO_ENABLE_CATALOG_WRITES=1.
        """
        try:
            setup = client.get("api", f"/modelconfigurationsetups/{enc(setup_id)}", token=tapis_token)
        except DynamoAPIError as exc:
            return backend_error(exc)
        owned = {p.get("id") for p in (setup.get("hasParameter") or []) if isinstance(p, dict)}
        foreign = sorted(set(parameter_defaults) - owned)
        if foreign:
            return {"error": "parameter_not_in_setup",
                    "message": "These parameter ids do not belong to the setup; refusing.",
                    "unknown_parameter_ids": foreign,
                    "setup_parameter_ids": sorted(x for x in owned if x)}
        steps = [cat.step("PUT", "parameters", {"id": pid, "has_default_value": [str(v)]}, pid)
                 for pid, v in parameter_defaults.items()]
        if dry_run:
            return {"dry_run": True, "setup_id": setup_id, "steps": cat.describe(steps),
                    "live_writes_enabled": settings.enable_catalog_writes}
        tok = client.effective_token(tapis_token)
        blocked = write_gate(settings, "update_setup_parameters", tok, confirm=confirm, catalog=True)
        if blocked:
            return blocked
        result = cat.execute(client, steps, tapis_token)
        audit.log_write("update_setup_parameters", {"setup_id": setup_id, "parameter_ids": list(parameter_defaults)},
                        "api", "ok" if result["ok"] else "error", setup_id)
        return result
