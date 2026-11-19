from __future__ import annotations

import json

import pytest

from conftest import API, GRAPHQL, SVO, TOKEN, build_tools, make_settings, writes

STEPS = [
    {"position": 1, "etl_process_id": "p:ingest", "wires": [{"to": {"input": 0}, "from": {"flow_input": "corpus"}}]},
    {"position": 2, "etl_process_id": "p:score", "settings": {"top_n": 25},
     "wires": [{"to": {"input": 0}, "from": {"step": 1, "output": 0}}]},
]
INPUTS = [{"key": "corpus", "label": "Documents", "standard_variable": "sv:text", "format": "folder"}]
OUTPUTS = [{"step": 2, "output": 0, "label": "Term counts"}]
CONFIGURATION = {"label": "Dictionary"}


@pytest.fixture
def wt():
    return build_tools(make_settings(enable_catalog_writes=True, tapis_token=TOKEN))


def sent(rsps, index=0):
    return json.loads(writes(rsps)[index].request.body)


# --- check ---------------------------------------------------------------------

def test_check_sends_the_graph_to_the_adapter_and_returns_its_answer(wt, rsps):
    rsps.post(f"{SVO}/flows/check", json={"ok": False, "problems": [{"code": "FLOW_WIRE_DOES_NOT_FIT", "step": 2, "wire": 0}]})
    out = wt["check_etl_flow"](steps=STEPS, inputs=INPUTS, outputs=OUTPUTS)
    assert out["ok"] is False and out["problems"][0]["step"] == 2
    body = sent(rsps)
    assert body["inputs"] == [{"id": "corpus", "contract": {"standard_variable_uri": "sv:text", "format": "folder"}}]
    assert body["steps"][1] == {"position": 2, "etl_process_id": "p:score", "settings": {"top_n": 25},
                                "wires": [{"to": {"input": 0}, "from": {"step": 1, "output": 0}}]}
    # A label describes an output in the catalog; the adapter is only asked where it comes from.
    assert body["outputs"] == [{"step": 2, "output": 0}]
    assert writes(rsps)[0].request.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("steps,fragment", [
    ([], "between 1 and 24"),
    ([{"position": 1, "etl_process_id": "p"}] * 25, "between 1 and 24"),
    (["nope"], "must be an object"),
    ([{"etl_process_id": "p"}], "position must be a whole number"),
    ([{"position": True, "etl_process_id": "p"}], "position must be a whole number"),
    ([{"position": 1}], "etl_process_id"),
    ([{"position": 1, "etl_process_id": "p", "settings": ["x"]}], "settings must map"),
    ([{"position": 1, "etl_process_id": "p", "settings": {"model_api_key": "x"}}], "may not set: model_api_key"),
    ([{"position": 1, "etl_process_id": "p", "settings": {"Ckan_Token": "x"}}], "may not set: Ckan_Token"),
])
def test_steps_that_are_not_in_the_expected_form_stop_before_any_request(wt, rsps, steps, fragment):
    for tool, kwargs in (("check_etl_flow", {}), ("create_etl_flow", {"configuration": CONFIGURATION})):
        out = wt[tool](steps=steps, **kwargs)
        assert out["error"] == "validation_failed" and any(fragment in e for e in out["errors"]), out
    assert len(rsps.calls) == 0


# --- create --------------------------------------------------------------------

def create(tools, **over):
    args = dict(configuration=CONFIGURATION, steps=STEPS, inputs=INPUTS, outputs=OUTPUTS,
                software={"label": "Semantic Sensemaking"}, version={"label": "a23bfc5"})
    args.update(over)
    return tools["create_etl_flow"](**args)


def test_create_dry_run_echoes_the_request_and_sends_nothing(wt, rsps):
    out = create(wt)
    assert out["dry_run"] is True and out["would_request"] == "POST /flows"
    assert out["body"]["steps"] == STEPS and "thread_bindings" not in out["body"]
    assert len(rsps.calls) == 0


def test_create_needs_confirm_a_token_and_the_catalog_write_flag(rsps):
    assert create(build_tools(make_settings(enable_catalog_writes=True, tapis_token=TOKEN)),
                  dry_run=False)["error"] == "confirm_required"
    assert create(build_tools(make_settings(enable_catalog_writes=True)),
                  dry_run=False, confirm=True)["error"] == "writes_require_token"
    assert create(build_tools(make_settings(tapis_token=TOKEN)),
                  dry_run=False, confirm=True)["error"] == "catalog_writes_disabled"
    assert len(rsps.calls) == 0


def test_create_posts_to_the_model_catalog_api_and_returns_the_ids(wt, rsps):
    rsps.post(f"{API}/flows", status=201, json={
        "software_id": "sw", "version_id": "ver", "configuration_id": "cfg",
        "inputs": {"corpus": "cfg_input_1"}, "settings": {"top_n": "cfg_setting_top_n"}})
    out = create(wt, dry_run=False, confirm=True, thread_bindings={"region": {"input": "corpus"}})
    assert out["status"] == "created" and out["configuration_id"] == "cfg"
    assert out["inputs"] == {"corpus": "cfg_input_1"}
    body = sent(rsps)
    assert body["configuration"] == CONFIGURATION and body["thread_bindings"] == {"region": {"input": "corpus"}}
    assert body["software"] == {"label": "Semantic Sensemaking"}


def test_create_passes_on_why_the_flow_does_not_fit(wt, rsps):
    rsps.post(f"{API}/flows", status=422, json={
        "code": "FLOW_DOES_NOT_CHECK_OUT", "problems": [{"code": "FLOW_INPUT_UNWIRED", "step": 2}]})
    out = create(wt, dry_run=False, confirm=True)
    assert out["status"] == 422 and out["body"]["problems"][0]["code"] == "FLOW_INPUT_UNWIRED"
    assert "names each step and link" in out["hint"]


# --- list and delete -----------------------------------------------------------

def flow_row(piece="p:ingest"):
    return {"id": "cfg", "label": "Dictionary", "owner_username": "alice",
            "flow": {"steps": [{"position": 1, "etl_process_id": piece}]}}


def test_list_marks_a_flow_whose_piece_was_deleted(wt, rsps):
    rsps.post(GRAPHQL, json={"data": {"modelcatalog_configuration": [flow_row(), flow_row(piece=None)]}})
    out = wt["list_etl_flows"]()
    assert out["count"] == 2 and [f["unavailable"] for f in out["flows"]] == [False, True]
    assert "where: {flow: {}}" in sent(rsps)["query"]


def test_delete_dry_run_looks_the_flow_up_and_deletes_nothing(wt, rsps):
    rsps.post(GRAPHQL, json={"data": {"modelcatalog_configuration_by_pk": flow_row()}})
    out = wt["delete_etl_flow"]("cfg")
    assert out["dry_run"] is True and out["found"] is True and out["is_flow"] is True
    assert "delete_modelcatalog_configuration_by_pk" not in sent(rsps)["query"]


def test_delete_removes_the_flows_configuration(wt, rsps):
    rsps.post(GRAPHQL, json={"data": {"modelcatalog_configuration_by_pk": flow_row()}})
    rsps.post(GRAPHQL, json={"data": {"delete_modelcatalog_configuration_by_pk": {"id": "cfg", "label": "Dictionary", "owner_username": "alice"}}})
    out = wt["delete_etl_flow"]("cfg", dry_run=False, confirm=True)
    assert out == {"ok": True, "status": "deleted", "flow_id": "cfg", "label": "Dictionary", "owner_username": "alice"}
    assert "delete_modelcatalog_configuration_by_pk" in sent(rsps, 1)["query"]


def test_delete_never_deletes_a_configuration_that_is_a_model(wt, rsps):
    rsps.post(GRAPHQL, json={"data": {"modelcatalog_configuration_by_pk": {"id": "cfg", "label": "A model", "flow": None}}})
    out = wt["delete_etl_flow"]("cfg", dry_run=False, confirm=True)
    assert out["status"] == "not_a_flow"
    assert len(writes(rsps)) == 1  # the lookup only


def test_delete_says_so_when_the_caller_does_not_own_the_flow(wt, rsps):
    rsps.post(GRAPHQL, json={"data": {"modelcatalog_configuration_by_pk": flow_row()}})
    rsps.post(GRAPHQL, json={"data": {"delete_modelcatalog_configuration_by_pk": None}})
    assert wt["delete_etl_flow"]("cfg", dry_run=False, confirm=True)["status"] == "not_deleted"


# --- plan and run --------------------------------------------------------------

PLAN = {"status": "transform_required", "plan_id": "plan-1", "plan_json": {
    "steps": [], "flow": {"configuration_id": "cfg", "linked_settings": [{"step": 2, "setting": "name", "from": {"step": 1, "value": "v"}}]},
    "parameters": [{"name": "top_n", "default": 25, "default_source": "flow", "required": False},
                   {"name": "source_uri", "default": "tapis://sys/docs", "managed": True, "required": False}]}}


def test_plan_dry_run_echoes_the_request(wt, rsps):
    out = wt["create_flow_plan"]("cfg", inputs={"cfg_input_1": "do-1"})
    assert out == {"dry_run": True, "would_request": "POST /plans/flow",
                   "body": {"flow_id": "cfg", "inputs": {"cfg_input_1": "do-1"}}, "note": out["note"]}
    assert len(rsps.calls) == 0


def test_plan_stores_the_plan_and_shows_what_the_flows_author_chose(wt, rsps):
    rsps.post(f"{SVO}/plans/flow", json=PLAN)
    out = wt["create_flow_plan"]("cfg", inputs={"cfg_input_1": "do-1"}, dry_run=False, confirm=True)
    assert out["plan_id"] == "plan-1"
    assert out["flow_settings"] == {
        "defaults": {"top_n": 25},
        "links": [{"step": 2, "setting": "name", "from": {"step": 1, "value": "v"}}]}
    assert "run_parameters" in out
    assert sent(rsps) == {"flow_id": "cfg", "inputs": {"cfg_input_1": "do-1"}}


def test_plan_passes_a_refusal_through(wt, rsps):
    rsps.post(f"{SVO}/plans/flow", status=422, json={"detail": {"code": "FLOW_INPUT_NOT_BOUND", "inputs": ["cfg_input_1"]}})
    out = wt["create_flow_plan"]("cfg", dry_run=False, confirm=True)
    assert out["status"] == 422 and out["body"]["detail"]["code"] == "FLOW_INPUT_NOT_BOUND"


def test_a_flows_settings_are_accepted_only_when_the_caller_says_so(wt, rsps):
    rsps.post(f"{SVO}/workflows/submit", json={"run_id": "r1"})
    wt["submit_etl_workflow"]("plan-1", dry_run=False, confirm=True)
    wt["submit_etl_workflow"]("plan-1", accept_flow_settings=True, dry_run=False, confirm=True)
    assert "accept_flow_settings" not in sent(rsps, 0)
    assert sent(rsps, 1)["accept_flow_settings"] is True
