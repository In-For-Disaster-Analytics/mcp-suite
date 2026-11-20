"""
Search / discover tools — read-only.

Tools registered here (11)
--------------------------
  search_models              semantic-search  GET /search?target=model_configuration
  search_standard_variables  semantic-search  GET /search?target=svo
  list_models                catalog API      GET /modelconfigurations
  get_model_configuration    catalog API      GET /modelconfigurations/{id}
  (all of the svo-adapter tools below are adapter-direct: the design's EM front routes, e.g.
   GET /v1/standard-variables, POST /v1/plans/discover, GET /v1/data-objects,
   GET /v1/etl-pieces, are not on the EM yet. Switch them to client "em" when they land.)
  list_standard_variables    svo-adapter      GET /standard-variables
  find_reachable_variables   svo-adapter      GET /reachable/{data_object_id}
  find_targets_for_source    svo-adapter      POST /plans/discover
  find_sources_for_target    svo-adapter      POST /plans/discover-sources
  find_datasets              svo-adapter      POST /datasets/find
  list_data_objects          svo-adapter      GET /data-objects
  list_etl_pieces            svo-adapter      GET /transform-specs
"""

from __future__ import annotations

from typing import Any, Literal

from ..client import DynamoClient, enc
from ..config import Settings
from ._common import bound, run


def register(mcp: Any, client: DynamoClient, settings: Settings) -> None:
    def _search(
        target: str,
        query: str,
        limit: int,
        region_id: list[str] | None,
        category_id: list[str] | None,
        variable_id: list[str] | None,
        output_variable_id: list[str] | None,
        role: str | None,
    ) -> Any:
        params = {
            "q": query,
            "target": target,
            "limit": limit,
            "region_id": region_id or None,
            "category_id": category_id or None,
            "variable_id": variable_id or None,
            "output_variable_id": output_variable_id or None,
            "role": role,
        }
        return run(lambda: bound(client.get("semantic", "/search", params=params), settings))

    @mcp.tool()
    def search_models(
        query: str,
        limit: int = 20,
        region_id: list[str] | None = None,
        category_id: list[str] | None = None,
        variable_id: list[str] | None = None,
        output_variable_id: list[str] | None = None,
        role: Literal["input", "output"] | None = None,
    ) -> Any:
        """Semantic search for DYNAMO model configurations.

        Filters are deterministic relationship filters applied alongside the text
        query: models usable in a region/category, that take (role=input) or
        produce (role=output) a standard variable, or that output a given variable.
        Each result is a model configuration; pass its id to get_model_configuration.
        """
        return _search("model_configuration", query, limit, region_id, category_id,
                       variable_id, output_variable_id, role)

    @mcp.tool()
    def search_standard_variables(
        query: str,
        limit: int = 20,
        region_id: list[str] | None = None,
        category_id: list[str] | None = None,
        variable_id: list[str] | None = None,
        output_variable_id: list[str] | None = None,
        role: Literal["input", "output"] | None = None,
    ) -> Any:
        """Semantic search for standard variables (SVOs) known to the catalog."""
        return _search("svo", query, limit, region_id, category_id,
                       variable_id, output_variable_id, role)

    @mcp.tool()
    def list_models(
        label: str | None = None,
        page: int = 1,
        per_page: int = 50,
        tapis_token: str | None = None,
    ) -> Any:
        """List model configurations from the catalog (optionally filtered by label).

        tapis_token: Tapis JWT (falls back to DYNAMO_TAPIS_TOKEN); catalog reads are
        served without one, a token is only sent when available.
        """
        params = {"label": label, "page": page, "per_page": min(max(per_page, 1), 200)}
        return run(lambda: bound(
            client.get("api", "/modelconfigurations", token=tapis_token, params=params),
            settings, tapis_token))

    @mcp.tool()
    def get_model_configuration(configuration_id: str, tapis_token: str | None = None) -> Any:
        """Get one model configuration with its inputs, outputs and parameter defaults.

        configuration_id is the catalog id (a URL such as
        https://w3id.org/okn/i/mint/<slug>); it is percent-encoded for you.
        """
        return run(lambda: bound(
            client.get("api", f"/modelconfigurations/{enc(configuration_id)}", token=tapis_token),
            settings, tapis_token))

    @mcp.tool()
    def list_standard_variables(tapis_token: str | None = None) -> Any:
        """Standard variables the SVO adapter knows, with which ETL pieces produce/consume each."""
        return run(lambda: bound(
            client.get("svo", "/standard-variables", token=tapis_token), settings, tapis_token))

    @mcp.tool()
    def find_reachable_variables(data_object_id: str, tapis_token: str | None = None) -> Any:
        """SVO variables reachable FROM a registered data object via ETL chains, with the shortest chain."""
        return run(lambda: bound(
            client.get("svo", f"/reachable/{enc(data_object_id)}", token=tapis_token),
            settings, tapis_token))

    @mcp.tool()
    def find_targets_for_source(
        standard_variable_uri: str | None = None,
        unit: str | None = None,
        format: str | None = None,
        tapis_token: str | None = None,
    ) -> Any:
        """Given a source contract (SVO uri / unit / format), list every target contract reachable by ETL."""
        body = {"standard_variable_uri": standard_variable_uri, "unit": unit, "format": format}
        return run(lambda: bound(
            client.post("svo", "/plans/discover", token=tapis_token, json_body=body),
            settings, tapis_token))

    @mcp.tool()
    def find_sources_for_target(
        standard_variable_uri: str | None = None,
        unit: str | None = None,
        format: str | None = None,
        tapis_token: str | None = None,
    ) -> Any:
        """Given a wanted target contract, list the source contracts that can reach it by ETL."""
        body = {"standard_variable_uri": standard_variable_uri, "unit": unit, "format": format}
        return run(lambda: bound(
            client.post("svo", "/plans/discover-sources", token=tapis_token, json_body=body),
            settings, tapis_token))

    @mcp.tool()
    def find_datasets(
        standard_variable_names: list[str],
        start_time_gte: str | None = None,
        end_time_lte: str | None = None,
        spatial_coverage_intersects: Any = None,
        limit: int = 100,
        tapis_token: str | None = None,
    ) -> Any:
        """Find catalog datasets (grouped by CKAN package) carrying the given SVO variable names."""
        body: dict[str, Any] = {"standard_variable_names__in": standard_variable_names,
                                "limit": limit}
        if start_time_gte:
            body["start_time__gte"] = start_time_gte
        if end_time_lte:
            body["end_time__lte"] = end_time_lte
        if spatial_coverage_intersects is not None:
            body["spatial_coverage__intersects"] = spatial_coverage_intersects
        return run(lambda: bound(
            client.post("svo", "/datasets/find", token=tapis_token, json_body=body),
            settings, tapis_token))

    @mcp.tool()
    def list_data_objects(tapis_token: str | None = None) -> Any:
        """Registered data objects (sources) with each variable's SVO uri, unit and format."""
        return run(lambda: bound(
            client.get("svo", "/data-objects", token=tapis_token), settings, tapis_token))

    @mcp.tool()
    def list_etl_pieces(tapis_token: str | None = None) -> Any:
        """The ETL/transform registry: each piece with its input/output contracts.

        `method` says how a piece executes as a Tapis Workflows task: `tapis_function`
        (inline Python `function` task, no HPC queue) or `tapis_job` (batch job for
        `tapis_app_id`, waits in the HPC queue).
        """
        return run(lambda: bound(
            client.get("svo", "/transform-specs", token=tapis_token), settings, tapis_token))
