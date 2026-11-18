from __future__ import annotations

import json

import pytest

from conftest import API, SVO, TOKEN, build_tools, make_settings, writes

IO_IN = [{"label": "Precip", "format": "tif", "is_optional": False, "app_input_name": "precip"}]
IO_OUT = [{"label": "Result", "format": "tif", "archived_file": "*.tif"}]
ENC = "https%3A%2F%2Fw3id.org%2Fokn%2Fi%2Fmint%2F"


@pytest.fixture
def wt():
    return build_tools(make_settings(enable_catalog_writes=True, tapis_token=TOKEN))


def test_create_model_dry_run_diff_is_ordered_top_down(wt, rsps):
    out = wt["create_model"]("Model X", "does things", inputs=IO_IN, outputs=IO_OUT,
                             version_id="2", tapis_app_id="app1", tapis_app_version="1.0")
    assert out["dry_run"] is True and out["live_writes_enabled"] is True
    reqs = [s["request"] for s in out["steps"]]
    # parents before children; I/O junctions linked by PUT after the specs exist
    assert reqs[0] == "POST /models" and reqs[1] == "POST /softwareversions"
    assert reqs.index("POST /datasetspecifications") < reqs.index("POST /modelconfigurations")
    assert reqs[-2].startswith("PUT /modelconfigurations/") and reqs[-1].startswith("PUT /modelconfigurations/")
    payloads = {s["request"]: s["payload"] for s in out["steps"]}
    cfg = payloads["POST /modelconfigurations"]
    assert cfg["tapis_app_id"] == ["app1"] and cfg["label"] == ["Model X configuration"]
    assert "hasInput" not in cfg and "hasOutput" not in cfg  # never nested in the POST
    ver = payloads["POST /softwareversions"]
    assert ver["softwareId"] == [out["steps"][0]["payload"]["id"]]
    assert out["steps"][-2]["payload"]["hasInput"] == [
        {"id": out["steps"][2]["payload"]["id"], "isOptional": False, "appInputName": "precip"}]
    assert out["steps"][-1]["payload"]["hasOutput"][0]["archivedFile"] == "*.tif"
    assert out["warnings"] == []
    assert writes(rsps) == []


def test_create_model_validation_errors(wt, rsps):
    out = wt["create_model"]("Model X", "d", inputs=[{"label": "A"}], outputs=[
        {"label": "B", "format": "tif", "archived_file": "bad path/../x"}])
    assert out["error"] == "validation_failed"
    joined = " ".join(out["errors"])
    assert "inputs[0]: format is required" in joined and "archived_file" in joined


def test_create_model_requires_some_io_and_warns_without_tapis_app(wt):
    out = wt["create_model"]("M", "d")
    assert out["error"] == "validation_failed" and "at least one input or output" in out["errors"][0]
    ok = wt["create_model"]("M", "d", outputs=IO_OUT)
    assert any("tapis_app_id" in w for w in ok["warnings"])


def test_unresolvable_standard_variable_fails_validation(wt, rsps):
    rsps.get(f"{API}/standardvariables/{ENC}nope", status=404, json={"message": "not found"})
    out = wt["create_model"]("M", "d", outputs=[{"label": "B", "format": "tif",
                                                 "standard_variable_id": "nope"}])
    assert out["error"] == "validation_failed"
    assert "did not resolve" in out["errors"][0] and "create_standard_variable" in out["errors"][0]


def test_svo_binding_yields_presentation_and_follow_up(wt, rsps):
    rsps.get(f"{API}/standardvariables/{ENC}head", json={"id": "x"})
    out = wt["create_model"]("M", "d", outputs=[{"label": "Heads", "format": "tif",
                                                 "standard_variable_id": "head", "unit_id": "m"}])
    reqs = [s["request"] for s in out["steps"]]
    assert "POST /variablepresentations" in reqs
    assert len(out["follow_up"]) == 1 and "Hasura" in out["follow_up"][0]
    assert f"has_standard_variable=https://w3id.org/okn/i/mint/head" in out["follow_up"][0]


def test_etl_piece_requires_tapis_app_and_both_io(wt):
    out = wt["create_etl_piece"]("P", "d", "", IO_IN, [])
    assert out["error"] == "validation_failed"
    joined = " ".join(out["errors"])
    assert "tapis_app_id is required" in joined and "at least one output" in joined


def test_etl_piece_attaches_to_existing_version_without_creating_a_model(wt):
    out = wt["create_etl_piece"]("P", "d", "app1", IO_IN, IO_OUT, software_version_id="https://w3id.org/okn/i/mint/v1")
    reqs = [s["request"] for s in out["steps"]]
    assert "POST /models" not in reqs and "POST /softwareversions" not in reqs
    assert out["steps"][-3]["payload"]["softwareVersionId"] == ["https://w3id.org/okn/i/mint/v1"]


def test_create_model_live_executes_in_order_and_tolerates_exists(wt, rsps):
    for res in ("models", "softwareversions", "datasetspecifications", "modelconfigurations"):
        rsps.post(f"{API}/{res}", status=409 if res == "models" else 201, json={"id": "x"})
    rsps.put(f"{API}/modelconfigurations/{ENC}model-x-cfg", json={})
    out = wt["create_model"]("Model X", "d", inputs=IO_IN, outputs=IO_OUT, dry_run=False, confirm=True)
    assert out["ok"] is True
    assert out["completed"][0]["status"] == "exists"
    methods = [(c.request.method, c.request.url.rsplit("/", 1)[-1].split("%2F")[-1]) for c in writes(rsps)]
    assert methods[0] == ("POST", "models") and methods[1] == ("POST", "softwareversions")
    assert all(c.request.headers["Authorization"] == f"Bearer {TOKEN}" for c in writes(rsps))


def test_partial_failure_reports_created_objects_and_stops(wt, rsps):
    rsps.post(f"{API}/models", status=201, json={})
    rsps.post(f"{API}/softwareversions", status=500, json={"message": f"db down {TOKEN}"})
    out = wt["create_model"]("Model X", "d", outputs=IO_OUT, dry_run=False, confirm=True)
    assert out["ok"] is False
    assert [c["status"] for c in out["completed"]] == ["ok"]
    assert out["failed"]["status"] == 500 and out["failed"]["request"] == "POST /softwareversions"
    assert "no delete tools" in out["note"]
    assert TOKEN not in json.dumps(out)
    assert len(writes(rsps)) == 2  # stopped; later steps were never sent


def test_create_standard_variable_existing_is_not_recreated(wt, rsps):
    rsps.get(f"{API}/standardvariables/{ENC}head", json={"id": "https://w3id.org/okn/i/mint/head"})
    out = wt["create_standard_variable"]("head", dry_run=False, confirm=True)
    assert out["error"] == "already_exists" and writes(rsps) == []


def test_create_standard_variable_live(wt, rsps):
    rsps.get(f"{API}/standardvariables/{ENC}head", status=404, json={})
    rsps.post(f"{API}/standardvariables", status=201, json={"id": "x"})
    out = wt["create_standard_variable"]("Head", "water level", dry_run=False, confirm=True)
    assert out["ok"] is True
    body = json.loads(writes(rsps)[0].request.body)
    assert body["id"] == "https://w3id.org/okn/i/mint/head" and body["type"] == ["StandardVariable"]


def test_register_data_object_live_posts_to_adapter(wt, rsps):
    rsps.post(f"{SVO}/data-objects", json={"id": "do-1", "label": "L"})
    out = wt["register_data_object"]("L", "https://x/y.tif", variables=[{"standard_variable_uri": "u"}],
                                     format="tif", dry_run=False, confirm=True)
    assert out["id"] == "do-1"
    assert json.loads(writes(rsps)[0].request.body) == {
        "label": "L", "resource_uri": "https://x/y.tif", "format": "tif",
        "variables": [{"standard_variable_uri": "u"}]}


def test_register_data_object_cannot_set_server_owned_fields(wt):
    import inspect
    params = inspect.signature(wt["register_data_object"]).parameters
    assert not {"owner_execution_id", "owner_model_child_id", "owner_tenant"} & set(params)


def test_test_etl_piece_default_is_adapter_generate_only(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{SVO}/transform-specs/spec1/test", json={"status": "generated"})
    out = t["test_etl_piece"]("spec1", args={"a": 1})
    assert out["status"] == "generated"
    assert json.loads(rsps.calls[0].request.body)["dry_run"] is True


def test_test_etl_piece_live_requires_confirm(rsps):
    t = build_tools(make_settings(tapis_token=TOKEN))
    assert t["test_etl_piece"]("spec1", dry_run=False)["error"] == "confirm_required"
    assert len(rsps.calls) == 0


def test_update_setup_parameters_refuses_foreign_parameter_ids(wt, rsps):
    rsps.get(f"{API}/modelconfigurationsetups/setup1", json={"hasParameter": [{"id": "p1"}]})
    out = wt["update_setup_parameters"]("setup1", {"p1": "a", "pX": "b"}, dry_run=False, confirm=True)
    assert out["error"] == "parameter_not_in_setup" and out["unknown_parameter_ids"] == ["pX"]
    assert writes(rsps) == []


def test_update_setup_parameters_live_puts_each_default(wt, rsps):
    rsps.get(f"{API}/modelconfigurationsetups/setup1", json={"hasParameter": [{"id": "p1"}, {"id": "p2"}]})
    rsps.put(f"{API}/parameters/p1", json={})
    rsps.put(f"{API}/parameters/p2", json={})
    out = wt["update_setup_parameters"]("setup1", {"p1": "a", "p2": 5}, dry_run=False, confirm=True)
    assert out["ok"] is True
    assert [json.loads(c.request.body) for c in writes(rsps)] == [
        {"id": "p1", "has_default_value": ["a"]}, {"id": "p2", "has_default_value": ["5"]}]


def test_create_configuration_setup_copies_tapis_bindings_from_parent(wt, rsps):
    rsps.get(f"{API}/modelconfigurations/cfg",
             json={"id": "cfg", "tapisAppId": ["app1"], "tapis_app_version": "1.2"})
    out = wt["create_configuration_setup"]("cfg", "Setup A", region="Texas",
                                           parameters=[{"label": "Dir", "default_value": "/x"}])
    first = out["steps"][0]["payload"]
    assert first["tapis_app_id"] == ["app1"] and first["tapis_app_version"] == ["1.2"]
    assert first["has_region"] == ["Texas"]
    assert first["hasParameter"][0]["has_default_value"] == ["/x"]
    assert out["steps"][1]["payload"]["modelConfigurationId"] == ["cfg"]


def test_unresolvable_parent_configuration_is_reported(wt, rsps):
    rsps.get(f"{API}/modelconfigurations/nope", status=404, json={"message": "nf"})
    out = wt["create_configuration_setup"]("nope", "S")
    assert out["error"] == "backend_error" and out["status"] == 404


def test_check_readiness_and_plan_preview(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{SVO}/readiness/check", json={"status": "transform_required", "dimensions": []})
    rsps.post(f"{SVO}/plans/model-run", json={"complete": True, "branches": [], "plan_json": {}})
    assert t["check_readiness"]("do1", target_contract={"unit": "m"})["status"] == "transform_required"
    assert json.loads(rsps.calls[0].request.body) == {"data_object_id": "do1",
                                                      "target_contract": {"unit": "m"}}
    assert t["plan_model_run"]("spec")["complete"] is True
