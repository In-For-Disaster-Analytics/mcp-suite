"""
Monitor tools — run state, provenance and cancellation.

Tools registered here (4)
-------------------------
  list_runs           EM GET /v1/plans/runs  |  adapter GET /runs
  get_run_status      EM GET /v1/plans/runs/{id} (reconciled)  |  adapter GET /runs/{id} [+ POST poll]
  get_run_provenance  adapter GET /runs/{id}/provenance
  cancel_run          EM POST /v1/plans/runs/{id}/cancel  |  adapter POST /runs/{id}/cancel

Run ids differ by service: an EM run id comes from submit_run_plan/create_scenario/
list_runs(source="ensemble_manager"); an adapter run id from submit_etl_workflow or
from the ``adapter_run_ids`` an EM run lists. get_run_status for an EM run follows those
children and returns each adapter run's ``health`` (ok / stalled / reported_failed) so a
stalled queue is visible. Provenance is read-only; cancel is a gated write.
"""

from __future__ import annotations

from typing import Any, Literal

from .. import audit
from ..client import DynamoAPIError, DynamoClient, enc
from ..config import Settings
from ._common import backend_error, bound, run, write_gate

Source = Literal["ensemble_manager", "adapter"]


def register(mcp: Any, client: DynamoClient, settings: Settings) -> None:
    @mcp.tool()
    def list_runs(
        source: Source = "ensemble_manager",
        app_label: str | None = None,
        problem_statement_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tapis_token: str | None = None,
    ) -> Any:
        """List recent runs, newest first.

        source="ensemble_manager": the caller's bridge runs (filter by app_label or
        problem_statement_id; `offset` is ignored). source="adapter": adapter workflow runs
        (limit/offset paging; response has `runs` and `total`).
        """
        if source == "adapter":
            params = {"limit": min(max(limit, 1), 200), "offset": max(offset, 0)}
            return run(lambda: bound(client.get("svo", "/runs", token=tapis_token, params=params),
                                     settings, tapis_token))
        params = {"app_label": app_label, "problem_statement_id": problem_statement_id, "limit": limit}
        return run(lambda: bound(client.get("em", "/plans/runs", token=tapis_token, params=params),
                                 settings, tapis_token))

    @mcp.tool()
    def get_run_status(
        run_id: str,
        source: Source = "ensemble_manager",
        poll: bool = False,
        include_adapter: bool = True,
        tapis_token: str | None = None,
    ) -> Any:
        """Current state of a run.

        source="ensemble_manager": EM reconciles the status of an active run. With
        include_adapter (default) each child in `adapter_run_ids` is also fetched and
        returned under `adapter_runs` (status + health), best effort.
        source="adapter": the adapter run; poll=true first asks the adapter to poll Tapis and
        persist any status transition (a reconcile, not a user write; never revives a
        cancelled run).
        """
        if source == "adapter":
            def adapter() -> Any:
                polled = None
                if poll:
                    polled = client.post("svo", f"/runs/{enc(run_id)}/poll", token=tapis_token)
                got = client.get("svo", f"/runs/{enc(run_id)}", token=tapis_token)
                return bound({**got, **({"poll": polled} if polled is not None else {})},
                             settings, tapis_token)
            return run(adapter)

        def em() -> Any:
            result = client.get("em", f"/plans/runs/{enc(run_id)}", token=tapis_token)
            if include_adapter and isinstance(result, dict):
                children = result.get("adapter_run_ids") or []
                summaries = []
                for child in children:
                    cid = child.get("run_id") if isinstance(child, dict) else child
                    if not cid:
                        continue
                    try:
                        c = client.get("svo", f"/runs/{enc(str(cid))}", token=tapis_token)
                        summaries.append({k: c.get(k) for k in
                                          ("id", "status", "health", "started_at", "last_signal_at",
                                           "error_message", "tapis_run_id") if k in c})
                    except DynamoAPIError as exc:
                        summaries.append({"id": cid, "error": backend_error(exc)})
                if summaries:
                    result = {**result, "adapter_runs": summaries}
            return bound(result, settings, tapis_token)
        return run(em)

    @mcp.tool()
    def get_run_provenance(run_id: str, limit: int = 100, tapis_token: str | None = None) -> Any:
        """Provenance events for an ADAPTER run (run.signal.*, run.stalled, run.cancelled, publication, ...)."""
        return run(lambda: bound(
            client.get("svo", f"/runs/{enc(run_id)}/provenance", token=tapis_token,
                       params={"limit": min(max(limit, 1), 500)}),
            settings, tapis_token))

    @mcp.tool()
    def cancel_run(
        run_id: str,
        source: Source = "ensemble_manager",
        dry_run: bool = True,
        confirm: bool = False,
        tapis_token: str | None = None,
    ) -> Any:
        """Cancel a run (terminates the Workflows run and, when verified, the Tapis job).

        The caller must be the submitter or a configured cancel admin (enforced by the service,
        with the user's own token; 403 RUN_CANCEL_FORBIDDEN is returned verbatim). `cancelled`
        is final. dry_run=true (default) only echoes; dry_run=false + confirm=true cancels.
        """
        service, path = (("em", f"/plans/runs/{enc(run_id)}/cancel") if source == "ensemble_manager"
                         else ("svo", f"/runs/{enc(run_id)}/cancel"))
        if dry_run:
            return {"dry_run": True, "would_request": f"POST {path}", "source": source}
        blocked = write_gate(settings, "cancel_run", client.effective_token(tapis_token), confirm=confirm)
        if blocked:
            return blocked
        try:
            result = client.post(service, path, token=tapis_token)
        except DynamoAPIError as exc:
            audit.log_write("cancel_run", {"run_id": run_id, "source": source}, service, exc.status)
            return backend_error(exc)
        audit.log_write("cancel_run", {"run_id": run_id, "source": source}, service, 200, run_id)
        return result
