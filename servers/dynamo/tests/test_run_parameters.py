from __future__ import annotations

import pytest

from conftest import SVO, TOKEN, build_tools, make_settings
from dso_dynamo_mcp.tools._common import run_parameters

PLAN = {"steps": [], "parameters": [
    {"name": "output_uri", "type": "string", "required": True, "description": "Where to write."},
    {"name": "match_mode", "type": "string", "required": False, "default": "auto"},
    {"name": "source_uri", "type": "string", "required": True, "managed": True, "managed_source": "execution_handoff"},
    {"name": "allocation", "type": "string", "required": False, "managed": True},
    {"name": "start_date", "type": "string", "required": False, "framing": "time_period", "used_by_plan": False},
    {"name": "aoi_geojson_uri", "type": "string", "required": True, "framing": "area", "used_by_plan": True},
]}


def test_parameters_are_sorted_by_who_supplies_them():
    out = run_parameters(PLAN)
    assert [p["name"] for p in out["must_supply"]] == ["output_uri", "aoi_geojson_uri"]
    assert out["must_supply"][0] == {"name": "output_uri", "type": "string", "description": "Where to write."}
    assert out["optional"] == [{"name": "match_mode", "type": "string", "default": "auto"}]
    assert out["supplied_by_adapter"] == ["source_uri", "allocation"]
    assert out["framing"] == [
        {"name": "start_date", "type": "string", "framing": "time_period", "required": False, "used_by_plan": False},
        {"name": "aoi_geojson_uri", "type": "string", "framing": "area", "required": True, "used_by_plan": True},
    ]


@pytest.mark.parametrize("plan", [None, {}, {"parameters": None}, {"parameters": ["x", {}, {"type": "string"}]}])
def test_missing_or_malformed_parameters_give_empty_groups(plan):
    assert run_parameters(plan) == {"must_supply": [], "optional": [], "framing": [], "supplied_by_adapter": []}


def test_a_stored_plan_comes_back_with_its_run_parameters(rsps):
    tools = build_tools(make_settings(tapis_token=TOKEN))
    rsps.post(f"{SVO}/plans", json={"status": "transform_required", "plan_id": "plan-1", "plan_json": PLAN})
    out = tools["create_adapter_plan"]("do1", target_contract={"standard_variable_uri": "x"},
                                       dry_run=False, confirm=True)
    assert out["plan_id"] == "plan-1"
    assert [p["name"] for p in out["run_parameters"]["must_supply"]] == ["output_uri", "aoi_geojson_uri"]


def test_an_already_ready_object_has_no_plan_and_no_run_parameters(rsps):
    tools = build_tools(make_settings(tapis_token=TOKEN))
    rsps.post(f"{SVO}/plans", json={"status": "ready", "plan": None})
    out = tools["create_adapter_plan"]("do1", dry_run=False, confirm=True)
    assert out == {"status": "ready", "plan": None}
