"""Flows: a graph of ETL pieces stored as a model.

A flow is a model configuration with a row that marks it as a flow and one row per
step. Each step runs one piece and receives, link by link, an input from a flow input
or from an output of an earlier step, a setting from a value an earlier step returned,
or nothing ("runs after"). A flow holds no code and grants nothing: every piece in it is
still checked for whoever plans and runs it.

  list_etl_flows     hasura              modelcatalog_configuration with a flow (as the caller)
  check_etl_flow     svo-adapter         POST /flows/check           (read-only; adapter-direct)
  create_etl_flow    model-catalog-api   POST /flows                 (catalog write)
  delete_etl_flow    hasura              delete the flow's configuration (catalog write)
  create_flow_plan   svo-adapter         POST /plans/flow            (write; adapter-direct)

Adapter-direct, and why: the design (2026-11-20-adapter-behind-dynamo-apis) fronts the flow
check with the catalog's POST /v2.0.0/flows/check, which does not exist yet. It fronts a flow
plan with EM POST /v1/plans, but that route plans a flow only for a thread (thread_id +
model_id), and create_flow_plan has no thread, so there is no equivalent route today.

Run a flow plan with submit_etl_workflow. A flow's author chose its default settings, and
the pieces run with the runner's token, so the adapter refuses the submit until the
runner sends accept_flow_settings=true or their own values.
"""

from __future__ import annotations

from typing import Any

from .. import audit
from ..client import DynamoAPIError, DynamoClient
from ..config import Settings
from ._common import backend_error, bound, run, run_parameters, write_gate

MAX_STEPS = 24

LIST_FLOWS = """
query ListFlows {
  modelcatalog_configuration(where: {flow: {}}, order_by: {label: asc}) {
    id label description owner_username
    software_version { id label software { id label } }
    inputs { is_optional input { id label has_format } }
    parameters { parameter { id label has_default_value } }
    flow {
      outputs_json thread_bindings_json
      steps(order_by: {position: asc}) { position etl_process_id settings_json wires_json pause_after }
    }
  }
}
"""

GET_FLOW = """
query GetFlow($id: String!) {
  modelcatalog_configuration_by_pk(id: $id) {
    id label owner_username
    flow { steps(order_by: {position: asc}) { position etl_process_id } }
  }
}
"""

DELETE_FLOW = """
mutation DeleteFlow($id: String!) {
  delete_modelcatalog_configuration_by_pk(id: $id) { id label owner_username }
}
"""


def _step_errors(steps: Any) -> list[str]:
    """What is wrong with a list of steps before anything is sent."""
    errors: list[str] = []
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return [f"steps must list between 1 and {MAX_STEPS} steps"]
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"steps[{index}] must be an object")
            continue
        if type(step.get("position")) is not int:
            errors.append(f"steps[{index}]: position must be a whole number, from 1")
        if not step.get("etl_process_id"):
            errors.append(f"steps[{index}]: etl_process_id (the catalog id of the piece) is required")
        settings = step.get("settings") or {}
        if not isinstance(settings, dict):
            errors.append(f"steps[{index}]: settings must map setting names to values")
            continue
        # A flow is readable by everyone, signed in or not: never a place for a credential.
        # The adapter refuses the names it manages; this catches what merely looks secret.
        looks_secret = sorted(name for name in settings
                              if any(word in str(name).lower() for word in ("key", "token", "secret", "password")))
        if looks_secret:
            errors.append(f"steps[{index}]: a flow may not set: {', '.join(looks_secret)}")
    return errors


def register(mcp: Any, client: DynamoClient, settings: Settings) -> None:
    @mcp.tool()
    def list_etl_flows(tapis_token: str | None = None) -> Any:
        """The flows in the catalog: model configurations that run a graph of ETL pieces.

        Each comes back with its family and version, its inputs (whose `id` is what
        create_flow_plan binds a data object to), its settings with their defaults, and its
        steps in order with what each receives (`wires_json`). `unavailable` is true when a
        piece a step named was deleted: the flow cannot be planned and has to be replaced.
        A flow grants nothing; each piece is still checked for whoever plans it.
        """
        def go() -> Any:
            rows = (client.graphql(LIST_FLOWS, {}, token=tapis_token) or {}).get("modelcatalog_configuration") or []
            for row in rows:
                steps = (row.get("flow") or {}).get("steps") or []
                row["unavailable"] = any(not step.get("etl_process_id") for step in steps)
            return bound({"flows": rows, "count": len(rows)}, settings, tapis_token)
        return run(go)

    @mcp.tool()
    def check_etl_flow(
        steps: list[dict[str, Any]],
        inputs: list[dict[str, Any]] | None = None,
        outputs: list[dict[str, Any]] | None = None,
        tapis_token: str | None = None,
    ) -> Any:
        """Check that a flow's pieces fit together, before registering it. Nothing is stored.

        inputs:  the datasets the flow takes: [{"key": "corpus", "standard_variable": <id>,
                 "format": "folder"?}]. `key` is a short name the steps refer to.
        steps:   [{"position": 1, "etl_process_id": <catalog piece id>, "settings": {...}?,
                 "wires": [...]}], numbered from 1 in an order they can run in. A step may
                 read only from steps before it. Each wire is {"to": ..., "from": ...}:
                   {"to": {"input": k},      "from": {"flow_input": "corpus"}}
                   {"to": {"input": k},      "from": {"step": n, "output": j}}
                   {"to": {"setting": name}, "from": {"step": n, "value": v}}
                   {"to": {},                "from": {"step": n}}      runs after step n
                 Every input of a piece is linked exactly once.
        outputs: which step outputs are the flow's: [{"step": n, "output": j}], or
                 {"step": n, "value": v}, or {"step": n, "record": "catalog"}.

        Returns `ok`, `problems` (each with a code, a message and the `step` and `wire` it is
        on) and, for every piece you can see, its input and output positions with their
        standard variables, the settings a flow may give it, and the values it returns. Call
        it with one step and no wires to learn a piece's positions. Needs a Tapis token.
        Adapter-direct: the catalog's POST /v2.0.0/flows/check is not there yet.
        """
        errors = _step_errors(steps)
        if errors:
            return {"error": "validation_failed", "valid": False, "errors": errors}
        body = {
            "inputs": [{"id": str(item.get("key")),
                        "contract": {"standard_variable_uri": item.get("standard_variable"),
                                     "format": item.get("format")}}
                       for item in inputs or [] if isinstance(item, dict)],
            "steps": [{"position": step["position"], "etl_process_id": step["etl_process_id"],
                       "settings": step.get("settings") or {}, "wires": step.get("wires") or []}
                      for step in steps],
            "outputs": [{key: value for key, value in item.items() if key in ("step", "output", "value", "record")}
                        for item in outputs or [] if isinstance(item, dict)],
        }
        return run(lambda: bound(
            client.post("svo", "/flows/check", token=tapis_token, json_body=body), settings, tapis_token))

    @mcp.tool()
    def create_etl_flow(
        configuration: dict[str, Any],
        steps: list[dict[str, Any]],
        software: dict[str, Any] | None = None,
        version: dict[str, Any] | None = None,
        inputs: list[dict[str, Any]] | None = None,
        outputs: list[dict[str, Any]] | None = None,
        thread_bindings: dict[str, Any] | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Register a flow as a model: a family, a version and a configuration that runs a
        graph of ETL pieces. Run check_etl_flow first; this refuses a flow that does not fit.

        software:      the flow family. {"id": ...} adds to an existing one; {"label": ...,
                       "description"?, "keywords"?, "license"?, "website"?} creates it.
        version:       the pinned code version. {"id": ...} adds to an existing one;
                       {"label": ..., "version_id"?, "source_code"?} creates it.
        configuration: the flow itself. {"label": ..., "description"?, "keywords"?, "id"?}.
        inputs:        [{"key", "label", "standard_variable", "format"?, "optional"?}].
        steps, outputs: as for check_etl_flow. A data output also takes "label" (required),
                       and optionally "format" and "standard_variable".
        thread_bindings: which input or setting a modeling thread's region and dates fill in:
                       {"region": {"input": <key>}, "start_date": {"setting": <name>},
                        "end_date": {"setting": <name>}}.

        The catalog sets the owner from the token. Everything is written in one request, so a
        refused flow leaves nothing behind. A flow is public like any model, and so are its
        steps and settings: never put a credential in a setting. Nothing about a flow is
        changed in place; to change one, register a new configuration and delete the old one.
        Returns the ids of the software, version and configuration, the id of each input (to
        bind in create_flow_plan) and of each setting.
        """
        errors = _step_errors(steps)
        if not isinstance(configuration, dict) or not configuration.get("label"):
            errors.append("configuration.label is required")
        if errors:
            return {"error": "validation_failed", "valid": False, "errors": errors}
        body = {"software": software, "version": version, "configuration": configuration,
                "inputs": inputs, "steps": steps, "outputs": outputs, "thread_bindings": thread_bindings}
        body = {key: value for key, value in body.items() if value is not None}
        label = str(configuration.get("label"))
        if dry_run:
            return {"dry_run": True, "valid": True, "would_request": "POST /flows", "body": body,
                    "live_writes_enabled": settings.enable_catalog_writes,
                    "note": "Nothing sent, and nothing checked against the pieces: use "
                            "check_etl_flow for that. dry_run=false + confirm=true (and the "
                            "server catalog-write flag) registers the flow."}
        blocked = write_gate(settings, "create_etl_flow", client.effective_token(tapis_token),
                             confirm=confirm, catalog=True)
        if blocked:
            return blocked
        try:
            created = client.post("api", "/flows", token=tapis_token, json_body=body)
        except DynamoAPIError as exc:
            audit.log_write("create_etl_flow", {"label": label}, "api", exc.status)
            out = backend_error(exc)
            if exc.status == 422:
                out["hint"] = "The flow does not fit its pieces. `problems` names each step and link."
            return out
        flow_id = created.get("configuration_id") if isinstance(created, dict) else None
        audit.log_write("create_etl_flow", {"label": label}, "api", 201, flow_id)
        return {"ok": True, "status": "created", **(created if isinstance(created, dict) else {}),
                "next": "Plan it with create_flow_plan(flow_id=<configuration_id>, inputs={<input id>: "
                        "<data object id>})."}

    @mcp.tool()
    def delete_etl_flow(
        flow_id: str,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Delete a flow the caller owns, by its configuration id. This cannot be undone.

        A flow is a model configuration, so this deletes that configuration; its flow row and
        steps go with it. The family, the version and the pieces are untouched. Only the
        owner or a catalog admin can delete it. A plan already made from the flow keeps
        working. dry_run=true (default) looks the flow up and sends no write.
        """
        if dry_run:
            try:
                data = client.graphql(GET_FLOW, {"id": flow_id}, token=tapis_token)
            except DynamoAPIError as exc:
                return backend_error(exc)
            found = (data or {}).get("modelcatalog_configuration_by_pk")
            return {"dry_run": True, "flow_id": flow_id, "found": bool(found),
                    "is_flow": bool(found and found.get("flow")), "configuration": found,
                    "would_request": "POST /v1/graphql (delete_modelcatalog_configuration_by_pk)",
                    "live_writes_enabled": settings.enable_catalog_writes,
                    "note": "Nothing sent. The catalog deletes it only if the caller owns it."}
        blocked = write_gate(settings, "delete_etl_flow", client.effective_token(tapis_token),
                             confirm=confirm, catalog=True)
        if blocked:
            return blocked
        try:
            # Never delete a model through the flow tool: look first, as the caller.
            found = (client.graphql(GET_FLOW, {"id": flow_id}, token=tapis_token) or {}).get(
                "modelcatalog_configuration_by_pk")
            if not found or not found.get("flow"):
                audit.log_blocked("delete_etl_flow", "not_a_flow")
                return {"ok": False, "status": "not_a_flow", "flow_id": flow_id,
                        "note": "Nothing was deleted: there is no flow with this configuration id."}
            data = client.graphql(DELETE_FLOW, {"id": flow_id}, token=tapis_token)
        except DynamoAPIError as exc:
            audit.log_write("delete_etl_flow", {"flow_id": flow_id}, "hasura", "error", flow_id)
            return backend_error(exc)
        deleted = (data or {}).get("delete_modelcatalog_configuration_by_pk")
        if not deleted:
            audit.log_write("delete_etl_flow", {"flow_id": flow_id}, "hasura", "not_deleted", flow_id)
            return {"ok": False, "status": "not_deleted", "flow_id": flow_id,
                    "note": "Nothing was deleted: the caller does not own this flow."}
        audit.log_write("delete_etl_flow", {"flow_id": flow_id}, "hasura", "ok", flow_id)
        return {"ok": True, "status": "deleted", "flow_id": deleted.get("id") or flow_id,
                "label": deleted.get("label"), "owner_username": deleted.get("owner_username")}

    @mcp.tool()
    def create_flow_plan(
        flow_id: str,
        inputs: dict[str, str] | None = None,
        allow_unapproved_piece_ids: list[str] | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """STORE a plan for a flow, with a data object for each input the flow reads.

        flow_id: the flow's configuration id (see list_etl_flows).
        inputs:  {<flow input id>: <data object id>}. The input ids are the `id` of each
                 entry under `inputs` in list_etl_flows. A flow with no dataset input takes {}.

        The graph comes from the flow; nothing is searched for. Refusals: FLOW_NOT_FOUND,
        FLOW_INPUT_NOT_BOUND, FLOW_DOES_NOT_CHECK_OUT (with `problems`, for example a piece
        that was deleted or data that does not fit an input), FLOW_SETTING_CONFLICT,
        FLOW_DOES_NOT_BUILD, STORED_SOURCE_REFUSED (a piece you may not run).

        allow_unapproved_piece_ids names shared or public pieces whose stored code no admin
        has approved and that you agree to run with your own Tapis token.

        The plan comes back with `run_parameters`, and `flow_settings`: the defaults the
        flow's author chose (`defaults`) and the settings it links from earlier steps
        (`links`). The pieces run with YOUR token, so show these to the user. To run the plan
        call submit_etl_workflow with accept_flow_settings=true, or pass your own values.
        dry_run=true (default) only echoes the request.
        Adapter-direct: no EM route plans a flow without a thread.
        """
        body: dict[str, Any] = {"flow_id": flow_id, "inputs": inputs or {}}
        if allow_unapproved_piece_ids:
            body["allow_unapproved_piece_ids"] = allow_unapproved_piece_ids
        if dry_run:
            return {"dry_run": True, "would_request": "POST /plans/flow", "body": body,
                    "note": "Nothing sent. Re-call with dry_run=false and confirm=true to store the plan."}
        blocked = write_gate(settings, "create_flow_plan", client.effective_token(tapis_token), confirm=confirm)
        if blocked:
            return blocked
        try:
            result = client.post("svo", "/plans/flow", token=tapis_token, json_body=body)
        except DynamoAPIError as exc:
            audit.log_write("create_flow_plan", {"flow_id": flow_id}, "svo", exc.status)
            return backend_error(exc)
        audit.log_write("create_flow_plan", {"flow_id": flow_id}, "svo", 200,
                        result.get("plan_id") if isinstance(result, dict) else None)
        if isinstance(result, dict) and isinstance(result.get("plan_json"), dict):
            plan_json = result["plan_json"]
            result = {
                **result,
                "run_parameters": run_parameters(plan_json),
                "flow_settings": {
                    "defaults": {p.get("name"): p.get("default") for p in plan_json.get("parameters") or []
                                 if isinstance(p, dict) and p.get("default_source") == "flow"},
                    "links": (plan_json.get("flow") or {}).get("linked_settings") or [],
                },
            }
        return bound(result, settings, tapis_token)
