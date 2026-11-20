from __future__ import annotations

import json

from conftest import EM, TOKEN, build_tools, make_settings, writes


def test_create_run_plan_live_body_and_pass_through_of_422(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{EM}/plans", status=422,
              json={"message": "no path", "code": "NO_INPUT_TRANSFORM_PATH", "details": {"input": "x"}})
    out = t["create_run_plan"](thread_id="th", model_id="cfg", publish=["ckan"], input_etl=True,
                               dry_run=False, confirm=True, tapis_token=TOKEN)
    assert out["status"] == 422 and out["body"]["code"] == "NO_INPUT_TRANSFORM_PATH"
    assert out["service"] == "em"
    assert json.loads(writes(rsps)[0].request.body) == {
        "executor": "ensemble_manager", "thread_id": "th", "model_id": "cfg",
        "input_etl": True, "publish": ["ckan"]}


def test_submit_run_plan_409_input_plan_changed_is_verbatim(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{EM}/plans/submit", status=409,
              json={"message": "steps differ", "code": "INPUT_PLAN_CHANGED"})
    out = t["submit_run_plan"]("p1", max_minutes=30, queue="development",
                               dry_run=False, confirm=True, tapis_token=TOKEN)
    assert out["status"] == 409 and out["body"]["code"] == "INPUT_PLAN_CHANGED"
    assert json.loads(writes(rsps)[0].request.body) == {
        "plan_id": "p1", "max_minutes": 30, "queue": "development"}


def test_create_scenario_returns_idempotency_key_and_sends_it(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{EM}/plans/scenario", json={"run_id": "r9", "reused": False})
    out = t["create_scenario"]("subside", "cfg", {"region_id": "r"}, "2020-01-01", "2020-12-31",
                               dry_run=False, confirm=True, tapis_token=TOKEN)
    sent = json.loads(writes(rsps)[0].request.body)
    assert out["idempotency_key"] == sent["idempotency_key"].__str__()
    assert "dry_run" not in sent  # EM-level dry_run only when adapter_dry_run is requested


def test_create_scenario_adapter_dry_run_is_forwarded_explicitly(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{EM}/plans/scenario", json={"run_id": "r"})
    t["create_scenario"]("a", "m", {"region_id": "r"}, "2020-01-01", "2020-02-01",
                         adapter_dry_run=True, idempotency_key="k1",
                         dry_run=False, confirm=True, tapis_token=TOKEN)
    sent = json.loads(writes(rsps)[0].request.body)
    assert sent["dry_run"] is True and sent["idempotency_key"] == "k1"


def test_dry_run_scenario_echo_includes_key_for_retry(rsps):
    t = build_tools(make_settings())
    out = t["create_scenario"]("a", "m", {"region_id": "r"}, "2020-01-01", "2020-02-01")
    assert out["dry_run"] is True and out["body"]["idempotency_key"].startswith("dso-dynamo-")
    assert len(rsps.calls) == 0


def test_submit_etl_workflow_sends_idempotency_header_not_composite_fields(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{EM}/plans/submit", json={"run_id": "ar1"})
    out = t["submit_etl_workflow"]("plan1", args={"start_date": "2020-01-01"}, run_name="r",
                                   dry_run=False, confirm=True, tapis_token=TOKEN)
    call = writes(rsps)[0].request
    assert call.headers["Idempotency-Key"] == out["idempotency_key"]
    body = json.loads(call.body)
    assert body == {"plan_id": "svo_plan1", "parameter_values": {"start_date": "2020-01-01"},
                    "run_name": "r", "recreate": False, "dry_run": False,
                    "idempotency_key": out["idempotency_key"]}
    assert "model_task" not in body and "execution_id" not in body


def test_adapter_error_code_passes_through_on_submit(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{EM}/plans/submit", status=502,
              json={"detail": {"code": "TAPIS_WORKFLOW_SUBMISSION_FAILED", "run_id": "ar2"}})
    out = t["submit_etl_workflow"]("p", dry_run=False, confirm=True, tapis_token=TOKEN)
    assert out["status"] == 502
    assert out["body"]["detail"]["code"] == "TAPIS_WORKFLOW_SUBMISSION_FAILED"


def test_list_publishers_and_get_plan_are_reads(rsps):
    t = build_tools(make_settings())
    rsps.get(f"{EM}/plans/publishers", json={"publishers": []})
    rsps.get(f"{EM}/plans/svo_1", json={"id": "svo_1"})
    t["list_publishers"]("cfg", tapis_token=TOKEN)
    t["get_plan"]("svo_1", tapis_token=TOKEN)
    assert "model_id=cfg" in rsps.calls[0].request.url
    assert writes(rsps) == []
