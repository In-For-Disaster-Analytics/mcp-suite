"""
DSO DYNAMO MCP Server — entrypoint (stdio).

Registers five tool groups against the DYNAMO apps running as Tapis Pods:

  search     find models / standard variables / data / reachable ETL
  configure  readiness + plan previews; catalog setup writes (gated)
  author     create SVOs, models, ETL pieces (Tapis app or Python source), data objects (gated)
  run        launch runs: model bridge via Ensemble Manager, ETL-only via the adapter
  monitor    run status, provenance, cancel

Run with ``uv run dso-dynamo-mcp`` or ``python -m dso_dynamo_mcp.server``.
"""

from __future__ import annotations

import logging
import sys

import fastmcp

from .client import DynamoClient
from .config import settings
from .tools import author, configure, monitor, run, search

logging.basicConfig(
    stream=sys.stderr,  # stdout is the MCP protocol stream
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are connected to the DYNAMO model-integration apps (catalog, Ensemble Manager, SVO adapter, "
    "semantic search). Use search tools to find models, standard variables (SVOs), data objects and "
    "reachable ETL; check_readiness / plan_model_run to see whether data can feed a model; "
    "create_run_plan -> submit_run_plan (or create_scenario) to run a model through the model bridge "
    "(Ensemble Manager); submit_etl_workflow for ETL-only adapter workflows; and list_runs / "
    "get_run_status / get_run_provenance / cancel_run to monitor. "
    "Every run is a Tapis Workflows pipeline the SVO adapter generates; each ETL piece is one task in "
    "it, executed one of two ways (the `method` field in list_etl_pieces). `tapis_function`: a "
    "Workflows `function` task — inline Python 3.11 run by the workflow executor, no HPC queue; "
    "preferred for light transforms. `tapis_job`: a Workflows `tapis_job` task that submits a batch "
    "job for a registered Tapis app and polls it; it waits in the HPC queue (minutes to hours) and "
    "needs an allocation, so keep it for heavy work (models, GPU, large memory, long runtimes). "
    "create_etl_function_piece authors a `tapis_function` piece from Python source: private, owned "
    "by the caller, and runnable only where the adapter has stored-source execution enabled. "
    "create_etl_piece authors a `tapis_job` piece and the Tapis app must already be registered (no "
    "tool here registers Tapis apps). Long-running services and human review steps are not ETL "
    "pieces. "
    "Every write tool defaults to dry_run=true, which sends NOTHING and returns a preview. Show the "
    "preview to the user and only call again with dry_run=false AND confirm=true after their explicit "
    "go-ahead. Submitting a run starts real compute on Tapis. Configure and author tools (catalog "
    "writes) additionally require the server to be started with DYNAMO_ENABLE_CATALOG_WRITES=1. "
    "Backend errors are returned verbatim (status + body) — read the code field before retrying. "
    "No delete tools exist."
)

mcp = fastmcp.FastMCP("DSO DYNAMO MCP Server", instructions=INSTRUCTIONS)
_client = DynamoClient(settings)

search.register(mcp, _client, settings)
configure.register(mcp, _client, settings)
author.register(mcp, _client, settings)
run.register(mcp, _client, settings)
monitor.register(mcp, _client, settings)


def main() -> None:
    settings.log_startup_banner()
    mcp.run()


if __name__ == "__main__":
    main()
