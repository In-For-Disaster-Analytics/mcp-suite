"""The adapter is behind DYNAMO's APIs: switched tools use the EM with the caller's token and
never the adapter; tools that still call the adapter say so in their docstring."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import EM, SVO, TOKEN, build_tools, make_settings

TOOLS_DIR = Path(__file__).resolve().parents[1] / "src" / "dso_dynamo_mcp" / "tools"


def hosts(rsps) -> set[str]:
    return {c.request.url.split("/")[2] for c in rsps.calls}


def assert_em_only(rsps) -> None:
    assert rsps.calls, "no request was made"
    assert hosts(rsps) == {"em.test"}
    for call in rsps.calls:
        assert call.request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_create_adapter_plan_goes_through_the_em(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{EM}/plans", json={"plan_id": "svo_p1", "plan_json": {"parameters": []}})
    out = t["create_adapter_plan"]("do1", target_contract={"standard_variable_uri": "x"},
                                   dry_run=False, confirm=True, tapis_token=TOKEN)
    assert out["plan_id"] == "svo_p1"
    assert_em_only(rsps)
    body = json.loads(rsps.calls[0].request.body)
    assert body["executor"] == "svo_adapter" and body["adapter_request"]["data_object_id"] == "do1"


def test_submit_etl_workflow_goes_through_the_em_and_accepts_either_plan_id_form(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{EM}/plans/submit", json={"run_id": "r"})
    t["submit_etl_workflow"]("svo_p1", dry_run=False, confirm=True, tapis_token=TOKEN)
    t["submit_etl_workflow"]("p1", dry_run=False, confirm=True, tapis_token=TOKEN)
    assert_em_only(rsps)
    assert [json.loads(c.request.body)["plan_id"] for c in rsps.calls] == ["svo_p1", "svo_p1"]


def test_generate_workflow_gets_the_adapters_own_plan_id(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{SVO}/workflows/generate", json={"plan_id": "p1"})
    t["generate_workflow"]("svo_p1", dry_run=False, confirm=True, tapis_token=TOKEN)
    assert json.loads(rsps.calls[0].request.body) == {"plan_id": "p1"}


def test_adapter_run_status_and_children_are_read_through_the_em(rsps):
    t = build_tools(make_settings())
    rsps.get(f"{EM}/plans/runs/a1", json={"id": "a1", "status": "running"})
    rsps.get(f"{EM}/plans/runs/r1", json={"run_id": "r1", "adapter_run_ids": ["a1"]})
    t["get_run_status"]("a1", source="adapter", tapis_token=TOKEN)
    t["get_run_status"]("r1", tapis_token=TOKEN)
    assert_em_only(rsps)


def test_cancel_goes_through_the_em_for_both_run_kinds_and_dry_run_sends_nothing(rsps):
    t = build_tools(make_settings())
    assert t["cancel_run"]("a1", source="adapter")["dry_run"] is True
    assert not rsps.calls
    rsps.post(f"{EM}/plans/runs/a1/cancel", json={"status": "cancelled"})
    t["cancel_run"]("a1", source="adapter", dry_run=False, confirm=True, tapis_token=TOKEN)
    assert_em_only(rsps)


def test_poll_is_the_only_adapter_call_when_reading_an_adapter_run(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{SVO}/runs/a1/poll", json={"status": "completed"})
    rsps.get(f"{EM}/plans/runs/a1", json={"id": "a1", "status": "completed"})
    t["get_run_status"]("a1", source="adapter", poll=True, tapis_token=TOKEN)
    assert [c.request.url.split("/")[2] for c in rsps.calls] == ["svo.test", "em.test"]


def test_every_tool_that_still_calls_the_adapter_says_so_in_its_docstring():
    tools = build_tools(make_settings())
    direct = {"list_standard_variables", "find_reachable_variables", "find_targets_for_source",
              "find_sources_for_target", "find_datasets", "list_data_objects", "list_etl_pieces",
              "register_data_object", "test_etl_piece", "check_readiness", "plan_model_run",
              "check_etl_flow", "create_flow_plan", "generate_workflow", "list_runs",
              "get_run_status", "get_run_provenance"}
    undocumented = {n for n in direct if "dapter-direct" not in (tools[n].__doc__ or "")
                    and "adapter-direct" not in (tools[n].__doc__ or "")}
    # The discovery tools share one note in search.py's module docstring.
    search_doc = (TOOLS_DIR / "search.py").read_text()
    assert "adapter-direct" in search_doc
    assert undocumented <= {"list_standard_variables", "find_reachable_variables",
                            "find_targets_for_source", "find_sources_for_target",
                            "find_datasets", "list_data_objects", "list_etl_pieces"}


@pytest.mark.parametrize("name", ["list_standard_variables", "find_reachable_variables",
                                  "find_targets_for_source", "find_sources_for_target",
                                  "find_datasets", "list_data_objects", "list_etl_pieces"])
def test_discovery_tools_are_listed_as_adapter_direct_in_the_module_note(name):
    assert re.search(rf"\b{name}\b", (TOOLS_DIR / "search.py").read_text())
