"""Dry-run never reaches a write endpoint; live writes are gated."""

from __future__ import annotations

import json

import pytest

from conftest import API, EM, TOKEN, build_tools, make_settings, writes

IO_IN = [{"label": "Precip", "format": "tif"}]
IO_OUT = [{"label": "Result", "format": "tif", "archived_file": "*.tif"}]


def _stub_reads(rsps):
    rsps.get(f"{API}/modelconfigurations/cfg", json={"id": "cfg", "tapis_app_id": ["app1"]})
    rsps.get(f"{API}/standardvariables/https%3A%2F%2Fw3id.org%2Fokn%2Fi%2Fmint%2Fnew-var",
             status=404, json={})
    rsps.get(f"{API}/modelconfigurationsetups/setup1",
             json={"id": "setup1", "hasParameter": [{"id": "p1"}]})


def _dry_calls(t):
    return {
        "create_adapter_plan": lambda: t["create_adapter_plan"]("do1"),
        "create_run_plan": lambda: t["create_run_plan"](thread_id="t", model_id="m"),
        "submit_run_plan": lambda: t["submit_run_plan"]("plan1"),
        "create_scenario": lambda: t["create_scenario"]("app", "m", {"region_id": "r"}, "2020-01-01", "2020-02-01"),
        "generate_workflow": lambda: t["generate_workflow"]("plan1"),
        "submit_etl_workflow": lambda: t["submit_etl_workflow"]("plan1"),
        "cancel_run": lambda: t["cancel_run"]("run1"),
        "register_data_object": lambda: t["register_data_object"]("L", "https://x/y.tif"),
        "create_standard_variable": lambda: t["create_standard_variable"]("new var"),
        "create_model": lambda: t["create_model"]("Model X", "desc", inputs=IO_IN, outputs=IO_OUT),
        "create_etl_piece": lambda: t["create_etl_piece"]("Piece", "d", "app1", IO_IN, IO_OUT),
        "create_configuration_setup": lambda: t["create_configuration_setup"]("cfg", "Setup A"),
        "update_setup_parameters": lambda: t["update_setup_parameters"]("setup1", {"p1": "v"}),
    }


@pytest.mark.parametrize("name", list(_dry_calls(build_tools(make_settings()))))
def test_dry_run_sends_no_write_even_with_everything_enabled(name, rsps):
    t = build_tools(make_settings(enable_catalog_writes=True, tapis_token=TOKEN))
    _stub_reads(rsps)
    out = _dry_calls(t)[name]()
    assert out.get("dry_run") is True, out
    assert writes(rsps) == []


@pytest.mark.parametrize("name", list(_dry_calls(build_tools(make_settings()))))
def test_live_without_confirm_is_refused_and_sends_nothing(name, rsps):
    t = build_tools(make_settings(enable_catalog_writes=True, tapis_token=TOKEN))
    _stub_reads(rsps)
    kwargs = {"dry_run": False}
    fn = {
        "create_adapter_plan": lambda: t["create_adapter_plan"]("do1", **kwargs),
        "create_run_plan": lambda: t["create_run_plan"](thread_id="t", model_id="m", **kwargs),
        "submit_run_plan": lambda: t["submit_run_plan"]("plan1", **kwargs),
        "create_scenario": lambda: t["create_scenario"]("app", "m", {"region_id": "r"}, "2020-01-01", "2020-02-01", **kwargs),
        "generate_workflow": lambda: t["generate_workflow"]("plan1", **kwargs),
        "submit_etl_workflow": lambda: t["submit_etl_workflow"]("plan1", **kwargs),
        "cancel_run": lambda: t["cancel_run"]("run1", **kwargs),
        "register_data_object": lambda: t["register_data_object"]("L", "https://x/y.tif", **kwargs),
        "create_standard_variable": lambda: t["create_standard_variable"]("new var", **kwargs),
        "create_model": lambda: t["create_model"]("Model X", "desc", inputs=IO_IN, outputs=IO_OUT, **kwargs),
        "create_etl_piece": lambda: t["create_etl_piece"]("Piece", "d", "app1", IO_IN, IO_OUT, **kwargs),
        "create_configuration_setup": lambda: t["create_configuration_setup"]("cfg", "Setup A", **kwargs),
        "update_setup_parameters": lambda: t["update_setup_parameters"]("setup1", {"p1": "v"}, **kwargs),
    }[name]
    out = fn()
    assert out["error"] == "confirm_required"
    assert writes(rsps) == []


CATALOG_TOOLS = ["register_data_object", "create_standard_variable", "create_model",
                 "create_etl_piece", "create_configuration_setup", "update_setup_parameters"]


@pytest.mark.parametrize("name", CATALOG_TOOLS)
def test_catalog_writes_disabled_blocks_live_even_when_confirmed(name, rsps):
    t = build_tools(make_settings(enable_catalog_writes=False, tapis_token=TOKEN))
    _stub_reads(rsps)
    live = {"dry_run": False, "confirm": True}
    fn = {
        "register_data_object": lambda: t["register_data_object"]("L", "https://x/y.tif", **live),
        "create_standard_variable": lambda: t["create_standard_variable"]("new var", **live),
        "create_model": lambda: t["create_model"]("Model X", "desc", inputs=IO_IN, outputs=IO_OUT, **live),
        "create_etl_piece": lambda: t["create_etl_piece"]("Piece", "d", "app1", IO_IN, IO_OUT, **live),
        "create_configuration_setup": lambda: t["create_configuration_setup"]("cfg", "Setup A", **live),
        "update_setup_parameters": lambda: t["update_setup_parameters"]("setup1", {"p1": "v"}, **live),
    }[name]
    assert fn()["error"] == "catalog_writes_disabled"
    assert writes(rsps) == []


def test_live_without_a_token_is_refused(rsps):
    t = build_tools(make_settings(tapis_token=None))
    out = t["submit_run_plan"]("plan1", dry_run=False, confirm=True)
    assert out["error"] == "writes_require_token"
    assert writes(rsps) == []


def test_run_writes_are_not_blocked_by_the_catalog_flag(rsps):
    t = build_tools(make_settings(enable_catalog_writes=False))
    rsps.post(f"{EM}/plans/submit", status=202, json={"run_id": "r1"})
    out = t["submit_run_plan"]("plan1", dry_run=False, confirm=True, tapis_token=TOKEN)
    assert out["run_id"] == "r1"
    assert json.loads(writes(rsps)[0].request.body) == {"plan_id": "plan1"}


def test_live_write_never_logs_the_token(rsps, caplog):
    import logging
    caplog.set_level(logging.INFO)
    t = build_tools(make_settings())
    rsps.post(f"{EM}/adapter/workflows/generate", json={"plan_id": "p"})
    t["generate_workflow"]("p", dry_run=False, confirm=True, tapis_token=TOKEN)
    assert "AUDIT tool=generate_workflow" in caplog.text
    assert TOKEN not in caplog.text
