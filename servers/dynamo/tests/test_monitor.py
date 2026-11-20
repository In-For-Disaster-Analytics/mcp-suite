from __future__ import annotations

import json

from conftest import EM, TOKEN, build_tools, make_settings, writes


def test_list_runs_em_and_adapter(rsps):
    t = build_tools(make_settings())
    rsps.get(f"{EM}/plans/runs", json={"problem_statement_id": "ps", "runs": []})
    rsps.get(f"{EM}/adapter/runs", json={"runs": [], "total": 0})
    t["list_runs"](app_label="subside", limit=5, tapis_token=TOKEN)
    t["list_runs"](source="adapter", limit=10, offset=20, tapis_token=TOKEN)
    assert "app_label=subside" in rsps.calls[0].request.url and "limit=5" in rsps.calls[0].request.url
    assert "limit=10" in rsps.calls[1].request.url and "offset=20" in rsps.calls[1].request.url


def test_em_run_status_follows_adapter_children_and_reports_health(rsps):
    t = build_tools(make_settings())
    rsps.get(f"{EM}/plans/runs/r1", json={"run_id": "r1", "status": "running",
                                          "adapter_run_ids": [{"run_id": "a1", "status": "running"}]})
    rsps.get(f"{EM}/plans/runs/a1", json={"id": "a1", "status": "running", "health": "stalled",
                                     "last_signal_at": None, "secret_internal": "x"})
    out = t["get_run_status"]("r1", tapis_token=TOKEN)
    assert out["status"] == "running"
    assert out["adapter_runs"] == [{"id": "a1", "status": "running", "health": "stalled",
                                    "last_signal_at": None}]


def test_em_run_status_survives_a_failing_child_lookup(rsps):
    t = build_tools(make_settings())
    rsps.get(f"{EM}/plans/runs/r1", json={"run_id": "r1", "adapter_run_ids": ["a9"]})
    rsps.get(f"{EM}/plans/runs/a9", status=404, json={"detail": "run not found"})
    out = t["get_run_status"]("r1", tapis_token=TOKEN)
    assert out["adapter_runs"][0]["error"]["status"] == 404


def test_adapter_run_status_poll_reconciles_then_reads(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{EM}/adapter/runs/a1/poll", json={"status": "completed"})
    rsps.get(f"{EM}/plans/runs/a1", json={"id": "a1", "status": "completed", "health": "ok"})
    out = t["get_run_status"]("a1", source="adapter", poll=True, tapis_token=TOKEN)
    assert out["status"] == "completed" and out["poll"] == {"status": "completed"}
    assert [c.request.method for c in rsps.calls] == ["POST", "GET"]


def test_provenance_is_read_only_and_bounded(rsps):
    t = build_tools(make_settings())
    rsps.get(f"{EM}/adapter/runs/a1/provenance", json={"run_id": "a1", "events": [{"event_type": "run.stalled"}]})
    out = t["get_run_provenance"]("a1", limit=9999, tapis_token=TOKEN)
    assert out["events"][0]["event_type"] == "run.stalled"
    assert "limit=500" in rsps.calls[0].request.url
    assert writes(rsps) == []


def test_cancel_routes_by_source_and_passes_403_verbatim(rsps):
    t = build_tools(make_settings())
    rsps.post(f"{EM}/plans/runs/r1/cancel", json={"status": "cancelled"})
    rsps.post(f"{EM}/plans/runs/a1/cancel", status=403,
              json={"detail": {"code": "RUN_CANCEL_FORBIDDEN", "message": "not yours"}})
    assert t["cancel_run"]("r1", dry_run=False, confirm=True, tapis_token=TOKEN)["status"] == "cancelled"
    out = t["cancel_run"]("a1", source="adapter", dry_run=False, confirm=True, tapis_token=TOKEN)
    assert out["status"] == 403 and out["body"]["detail"]["code"] == "RUN_CANCEL_FORBIDDEN"
