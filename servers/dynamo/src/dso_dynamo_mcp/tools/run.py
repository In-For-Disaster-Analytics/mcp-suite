"""
Run tools — launch model runs.

Two paths (the adapter itself refuses composite descriptors from anyone but
Ensemble Manager, so the bridge path goes through EM):

  Model bridge (model + ETL pieces, composite)   Ensemble Manager  /v1/plans
      list_publishers, create_run_plan, get_plan, submit_run_plan, create_scenario
  Adapter ETL-only workflows                      Ensemble Manager  /v1/plans/submit
      submit_etl_workflow  (an `svo_` plan runs through the EM, which calls the adapter)
      generate_workflow    adapter-direct: POST /workflows/generate has no EM route
                           (design 2026-11-20-adapter-behind-dynamo-apis, open question 5)

Every write tool is dry-run by default (the dry run is a pure echo — NO request is
sent), needs ``confirm=true`` to go live, and is audited. No tool here accepts a
``model_task`` or ``execution_id``: those are EM-owned and server-generated.
"""

from __future__ import annotations

import uuid
from typing import Any

from .. import audit
from ..client import DynamoAPIError, DynamoClient, enc
from ..config import Settings
from ._common import backend_error, bound, run, write_gate


def _adapter_plan_id(plan_id: str) -> str:
    """The EM names an adapter plan ``svo_<id>``; the adapter's own routes take ``<id>``."""
    return plan_id[len("svo_"):] if plan_id.startswith("svo_") else plan_id


def _em_plan_id(plan_id: str) -> str:
    return plan_id if plan_id.startswith("svo_") else f"svo_{plan_id}"


def register(mcp: Any, client: DynamoClient, settings: Settings) -> None:
    def _live(
        tool: str,
        service: str,
        path: str,
        body: dict[str, Any] | None,
        token: str | None,
        confirm: bool,
        audit_args: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> Any:
        blocked = write_gate(settings, tool, client.effective_token(token), confirm=confirm)
        if blocked:
            return blocked
        try:
            result = client.post(service, path, token=token, json_body=body, headers=headers)
        except DynamoAPIError as exc:
            audit.log_write(tool, audit_args, service, exc.status)
            return backend_error(exc)
        rid = None
        if isinstance(result, dict):
            rid = result.get("run_id") or result.get("plan_id") or result.get("id")
        audit.log_write(tool, audit_args, service, 200, rid)
        return result

    # ------------------------------------------------------ model bridge (EM)
    @mcp.tool()
    def list_publishers(model_id: str, tapis_token: str | None = None) -> Any:
        """Catalogs (ckan, stac, ...) a model's output can be published to, with their arguments.

        model_id is the model configuration id. Use the result's catalog_tokens as `publish`
        in create_run_plan / create_scenario.
        """
        return run(lambda: client.get("em", "/plans/publishers", token=tapis_token,
                                      params={"model_id": model_id}))

    @mcp.tool()
    def create_run_plan(
        executor: str = "ensemble_manager",
        thread_id: str | None = None,
        model_id: str | None = None,
        execution_engine: str | None = None,
        input_etl: bool | None = None,
        strict_inputs: bool | None = None,
        publish: list[str] | None = None,
        model_output_key: str | None = None,
        adapter_request: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Create a unified run plan through Ensemble Manager (POST /v1/plans). Stores the plan.

        executor="ensemble_manager": needs thread_id + model_id (an EM thread with the model
        bound). Tapis models automatically get input-side ETL planned by the bridge unless
        input_etl=false; strict_inputs=true fails the plan on any input warning; publish=[...]
        names catalogs for the model output (see list_publishers); model_output_key picks
        which declared output the post-model stage applies to.
        executor="svo_adapter": pass adapter_request (an adapter plan request).
        `extra` is merged into the request body for fields not modelled here.
        Errors (e.g. 422 NO_INPUT_TRANSFORM_PATH, INPUT_WARNINGS, PUBLISH_CATALOG_UNKNOWN) are
        returned verbatim. dry_run=true (default) only echoes the request.
        """
        body: dict[str, Any] = {"executor": executor, "thread_id": thread_id, "model_id": model_id,
                                "execution_engine": execution_engine, "input_etl": input_etl,
                                "strict_inputs": strict_inputs, "publish": publish,
                                "model_output_key": model_output_key,
                                "adapter_request": adapter_request}
        body = {k: v for k, v in body.items() if v is not None}
        body.update(extra or {})
        if dry_run:
            return {"dry_run": True, "would_request": "POST /v1/plans", "body": body,
                    "note": "Nothing sent. dry_run=false + confirm=true to create the plan."}
        return _live("create_run_plan", "em", "/plans", body, tapis_token, confirm,
                     {"executor": executor, "model_id": model_id})

    @mcp.tool()
    def get_plan(plan_id: str, tapis_token: str | None = None) -> Any:
        """Retrieve a unified plan from Ensemble Manager (steps, input_steps, steps_digest, warnings)."""
        return run(lambda: bound(client.get("em", f"/plans/{enc(plan_id)}", token=tapis_token),
                                 settings, tapis_token))

    @mcp.tool()
    def submit_run_plan(
        plan_id: str,
        parameter_values: dict[str, Any] | None = None,
        adapter_parameter_values: dict[str, Any] | None = None,
        max_minutes: int | None = None,
        queue: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Submit a unified plan for execution (POST /v1/plans/submit) — STARTS A REAL RUN.

        max_minutes bounds the model batch job (EM default 60); queue picks the Tapis logical
        queue. EM re-derives the planned input steps at submit and refuses a changed plan with
        409 INPUT_PLAN_CHANGED (returned verbatim). dry_run=true (default) only echoes.
        """
        body = {"plan_id": plan_id, "parameter_values": parameter_values,
                "adapter_parameter_values": adapter_parameter_values,
                "max_minutes": max_minutes, "queue": queue}
        body = {k: v for k, v in body.items() if v is not None}
        if dry_run:
            return {"dry_run": True, "would_request": "POST /v1/plans/submit", "body": body,
                    "note": "Nothing sent. dry_run=false + confirm=true starts the run."}
        return _live("submit_run_plan", "em", "/plans/submit", body, tapis_token, confirm,
                     {"plan_id": plan_id})

    @mcp.tool()
    def create_scenario(
        app_label: str,
        model_id: str,
        region: dict[str, Any],
        start_date: str,
        end_date: str,
        name: str | None = None,
        parameter_values: dict[str, Any] | None = None,
        publish: list[str] | None = None,
        max_minutes: int | None = None,
        idempotency_key: str | None = None,
        adapter_dry_run: bool = False,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Create a region, thread, bindings, plan AND START A RUN in one call (POST /v1/plans/scenario).

        region: {region_id} or {geojson, name}. Dates are YYYY-MM-DD. A generated
        idempotency_key is returned; re-calling with the same key returns the existing run
        (reused=true), so retries are safe. adapter_dry_run=true asks EM to have the adapter
        generate the pipeline WITHOUT starting it — but EM still creates the region/thread
        records, so it is not a pure preview. The tool's own dry_run=true is a pure echo.
        """
        key = idempotency_key or f"dso-dynamo-{uuid.uuid4()}"
        body = {"idempotency_key": key, "app_label": app_label, "name": name, "region": region,
                "model_id": model_id, "start_date": start_date, "end_date": end_date,
                "parameter_values": parameter_values, "publish": publish,
                "max_minutes": max_minutes, "dry_run": True if adapter_dry_run else None}
        body = {k: v for k, v in body.items() if v is not None}
        if dry_run:
            return {"dry_run": True, "would_request": "POST /v1/plans/scenario", "body": body,
                    "note": "Nothing sent. dry_run=false + confirm=true creates records and starts the run; "
                            "reuse idempotency_key on retries."}
        result = _live("create_scenario", "em", "/plans/scenario", body, tapis_token, confirm,
                       {"app_label": app_label, "model_id": model_id})
        if isinstance(result, dict) and "error" not in result:
            result.setdefault("idempotency_key", key)
        return result

    # ------------------------------------------------- adapter ETL-only (svo)
    @mcp.tool()
    def generate_workflow(
        plan_id: str,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Generate (and persist onto the plan) the Tapis Workflows definition for an adapter plan.

        Does not register or run anything on Tapis. dry_run=true (default) only echoes the request.
        Adapter-direct: the EM has no route for this yet (the design leaves it on the adapter).
        """
        plan_id = _adapter_plan_id(plan_id)
        body = {"plan_id": plan_id}
        if dry_run:
            return {"dry_run": True, "would_request": "POST /workflows/generate", "body": body}
        return _live("generate_workflow", "svo", "/workflows/generate", body, tapis_token, confirm,
                     {"plan_id": plan_id})

    @mcp.tool()
    def submit_etl_workflow(
        plan_id: str,
        args: dict[str, Any] | None = None,
        run_name: str | None = None,
        recreate: bool = False,
        idempotency_key: str | None = None,
        adapter_dry_run: bool = False,
        accept_flow_settings: bool = False,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Register the pipeline in Tapis Workflows and RUN an ETL-only adapter plan.

        Goes through the EM (POST /v1/plans/submit with the plan's `svo_` id), which calls the
        adapter with your token.

        For model + ETL composite runs use create_run_plan / submit_run_plan (EM-owned).
        args are run arguments ({"output_uri": "..."} or {"output_uri": {"value": "..."}}).
        Pass what create_adapter_plan listed under run_parameters.must_supply. Framing arguments
        (start_date, end_date, aoi_geojson_uri, lat, lon) may be added to say what the run is
        about; they are recorded with the run even when no piece reads them.
        recreate=true deletes + recreates the pipeline so task changes re-sync. An
        Idempotency-Key is sent (generated if you give none and returned as
        idempotency_key); reuse it when retrying. adapter_dry_run=true registers/generates
        without triggering a run — it still REGISTERS the pipeline in Tapis, so it is not a
        pure preview. The tool's own dry_run=true is a pure echo.

        For a plan made from a flow (create_flow_plan): the flow's author chose default
        settings, and may have linked settings from earlier steps, for pieces that run with
        YOUR token. The adapter answers FLOW_SETTINGS_NOT_ACCEPTED, listing them, until you
        send accept_flow_settings=true or pass your own value for each default. Show them to
        the user before accepting.
        """
        key = idempotency_key or f"dso-dynamo-{uuid.uuid4()}"
        # The EM reads the run arguments from `parameter_values` and the key from the body.
        body = {"plan_id": _em_plan_id(plan_id), "parameter_values": args or {}, "run_name": run_name,
                "recreate": recreate, "dry_run": adapter_dry_run, "idempotency_key": key}
        if accept_flow_settings:
            body["accept_flow_settings"] = True
        body = {k: v for k, v in body.items() if v is not None}
        if dry_run:
            return {"dry_run": True, "would_request": "POST /v1/plans/submit", "body": body,
                    "idempotency_key": key,
                    "note": "Nothing sent. dry_run=false + confirm=true registers and runs the pipeline."}
        result = _live("submit_etl_workflow", "em", "/plans/submit", body, tapis_token, confirm,
                       {"plan_id": plan_id}, headers={"Idempotency-Key": key})
        if isinstance(result, dict) and "error" not in result:
            result.setdefault("idempotency_key", key)
        return result
