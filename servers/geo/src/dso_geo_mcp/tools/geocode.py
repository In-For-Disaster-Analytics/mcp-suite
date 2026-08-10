"""Synchronous reverse-geocoding tools.

Unlike the GDAL tools in this server, these do not dispatch a Tapis Abaco
actor and do not return an execution_id. They call Nominatim directly through
``geocode_client`` and return a best-effort place label immediately.
"""

from __future__ import annotations

from typing import Any

import fastmcp

from ..ckan_resolve import CKANResolveError, resolve_dataset_bbox
from ..geocode_client import reverse_geocode as reverse_geocode_point
from ..geocode_client import reverse_geocode_bbox as reverse_geocode_extent


def register(mcp: fastmcp.FastMCP) -> None:
    """Register geocoding tools with the FastMCP app."""

    @mcp.tool()
    def reverse_geocode(
        lat: float,
        lon: float,
        zoom: int | None = None,
        tapis_token: str | None = None,
    ) -> dict[str, Any]:
        """Look up a human-readable place name for one coordinate.

        This is synchronous and read-only: no execution_id, no polling, no
        Tapis Abaco dispatch. ``tapis_token`` is accepted for signature
        consistency with other geo tools but is not used; Nominatim is keyless.

        Args:
            lat: Latitude in WGS84 degrees.
            lon: Longitude in WGS84 degrees.
            zoom: Optional Nominatim zoom level from 1 to 18. When omitted,
                dso-geo tries zoom=14 and falls back to zoom=10 only when the
                first result is too coarse.
            tapis_token: Ignored. Present only for cross-tool signature consistency.

        Returns:
            {"name": "New Braunfels, Texas", "tier": "city", "address": {...}}
            or {"name": None, "reason": "..."} on failure.
        """
        _ = tapis_token
        return reverse_geocode_point(lat, lon, zoom=zoom)

    @mcp.tool()
    def reverse_geocode_bbox(
        bbox: list[float] | None = None,
        dataset_id: str | None = None,
        tapis_token: str | None = None,
    ) -> dict[str, Any]:
        """Look up a representative place name for a bbox or CKAN dataset.

        Pass exactly one of ``bbox=[west, south, east, north]`` or
        ``dataset_id``. The dataset path reads the package's ``spatial`` GeoJSON
        field through CKAN ``package_show``. This tool is synchronous and
        read-only; ``tapis_token`` is accepted but unused.

        Returns:
            {"name": ..., "tier": ..., "agreement": true, "strategy": "majority"}
            or {"name": None, "reason": "..."} on failure.
        """
        _ = tapis_token
        has_bbox = bbox is not None
        has_dataset = bool(str(dataset_id or "").strip())
        if has_bbox == has_dataset:
            return {"name": None, "reason": "pass exactly one of bbox or dataset_id", "agreement": False}
        if has_dataset:
            try:
                bbox = resolve_dataset_bbox(str(dataset_id).strip())
            except CKANResolveError as exc:
                return {"name": None, "reason": f"CKAN dataset bbox resolution failed: {exc}", "agreement": False}
        return reverse_geocode_extent(bbox or [])
