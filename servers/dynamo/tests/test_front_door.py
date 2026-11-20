"""The adapter is behind DYNAMO's APIs: every tool uses the EM with the caller's token and no
tool calls the adapter host."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import EM, TOKEN, build_tools, make_settings

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
    rsps.post(f"{EM}/adapter/workflows/generate", json={"plan_id": "p1"})
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
    rsps.post(f"{EM}/adapter/runs/a1/poll", json={"status": "completed"})
    rsps.get(f"{EM}/plans/runs/a1", json={"id": "a1", "status": "completed"})
    t["get_run_status"]("a1", source="adapter", poll=True, tapis_token=TOKEN)
    assert [c.request.url.split("/")[2] for c in rsps.calls] == ["em.test", "em.test"]


# The adapter routes the EM forwards at /v1/adapter (mint-ensemble-manager
# adapterAllowlist.ts). A tool may call only these.
ALLOWED = [
    r"/standard-variables", r"/reachable/\S+", r"/plans/discover", r"/plans/discover-sources",
    r"/datasets/find", r"/data-objects", r"/transform-specs", r"/readiness/check",
    r"/plans/model-run", r"/flows/check", r"/plans/flow", r"/transform-specs/\S+/test",
    r"/runs", r"/runs/\S+/poll", r"/runs/\S+/provenance", r"/workflows/generate",
]


def source_calls() -> list[str]:
    calls: list[str] = []
    for path in sorted(TOOLS_DIR.glob("*.py")):
        text = path.read_text()
        pattern = r'(?:client\.\w+\(|_live\("\w+",\s*)"adapter",\s*f?"([^"]+)"'
        for match in re.finditer(pattern, text):
            calls.append(re.sub(r"\{[^}]+\}", "x", match.group(1)))
    return calls


def test_no_tool_names_the_adapter_host_as_a_client_target():
    for path in TOOLS_DIR.glob("*.py"):
        text = path.read_text()
        assert not re.search(r'client\.\w+\(\s*"svo"', text), path.name
        assert not re.search(r'_live\([^)]*"svo"', text), path.name


def test_the_client_has_no_adapter_host_and_reaches_it_only_through_the_em():
    from dso_dynamo_mcp.client import DynamoClient
    client = DynamoClient(make_settings())
    assert client.base("adapter") == "http://em.test/v1/adapter"
    with pytest.raises(KeyError):
        client.base("svo")


def test_every_adapter_path_a_tool_calls_is_on_the_ems_allowlist():
    calls = source_calls()
    # 17 forwarded routes, but GET and POST /data-objects share a path.
    for pattern in ALLOWED:
        assert any(re.fullmatch(pattern, call) for call in calls), pattern
    for call in calls:
        assert any(re.fullmatch(pattern, call) for pattern in ALLOWED), call
    # Never an operator or service-only route.
    assert not [c for c in calls if c.startswith(("/admin", "/workflows/submit", "/plans/deferred"))
                or "register-output" in c]


def test_every_tool_that_reads_or_writes_through_the_forward_says_so_in_its_docstring():
    tools = build_tools(make_settings())
    for name in ("list_runs", "get_run_status", "get_run_provenance", "generate_workflow",
                 "check_readiness", "plan_model_run", "check_etl_flow", "create_flow_plan",
                 "register_data_object", "test_etl_piece"):
        assert "adapter forward" in (tools[name].__doc__ or ""), name
