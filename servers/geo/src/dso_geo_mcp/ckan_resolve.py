"""
CKAN resource/dataset URL resolver for dso-geo.

Resolves CKAN resource IDs → download URLs and dataset IDs → resource lists
via anonymous CKAN Action API calls (GET only; no token required for public
resources).

SSRF protection
---------------
After resolving a resource_id → CKAN resource record, the ``download_url``
field's hostname is validated against the configured allowed CKAN host
(``settings.allowed_ckan_host``).  If the resolved URL points to a different
host, a ValueError is raised — the URL is never forwarded to the Abaco actor.

Only http(s) schemes are accepted; file://, ftp://, gopher:// etc. are rejected.

RFC-1918 / loopback / link-local addresses are also blocked when the allowed
host is a public hostname, preventing SSRF pivots through a compromised CKAN
portal.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from typing import Any
from urllib.parse import urlparse

import requests

from .audit import scrub
from .config import settings

logger = logging.getLogger(__name__)

# Private / loopback / link-local IP ranges (SSRF guard)
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class CKANResolveError(Exception):
    """Raised when a resource/dataset cannot be resolved or the URL fails SSRF checks."""


def _ckan_api_get(action: str, params: dict[str, str], ckan_url: str) -> dict[str, Any]:
    """Call CKAN Action API (GET) and return the ``result`` dict.

    Parameters
    ----------
    action:
        CKAN action name (e.g. ``"resource_show"``).
    params:
        Query params dict (e.g. ``{"id": "abc123"}``).
    ckan_url:
        CKAN portal base URL.

    Raises
    ------
    CKANResolveError
        On HTTP error or CKAN success=False.
    """
    url = f"{ckan_url.rstrip('/')}/api/3/action/{action}"
    try:
        resp = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:
        raise CKANResolveError(scrub(f"CKAN API request failed: {exc}")) from exc

    if not resp.ok:
        raise CKANResolveError(
            f"CKAN {action} returned HTTP {resp.status_code}: {resp.text[:200]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise CKANResolveError(f"CKAN {action} returned non-JSON response") from exc

    if not data.get("success"):
        error = data.get("error", {})
        raise CKANResolveError(f"CKAN {action} returned success=false: {error}")

    return data.get("result", {})


def _validate_bbox_values(values: list[Any]) -> list[float]:
    if len(values) != 4:
        raise ValueError("bbox must have four values")
    try:
        west, south, east, north = [float(v) for v in values]
    except (TypeError, ValueError) as exc:
        raise ValueError("bbox values must be numeric") from exc
    if west > east or south > north:
        raise ValueError("bbox values must be ordered [west, south, east, north]")
    if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= 90 and -90 <= north <= 90):
        raise ValueError("bbox values are outside WGS84 bounds")
    return [west, south, east, north]


def _iter_positions(coords: Any):
    if not isinstance(coords, (list, tuple)):
        return
    if len(coords) >= 2 and not isinstance(coords[0], (list, tuple)) and not isinstance(coords[1], (list, tuple)):
        try:
            lon = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError):
            return
        yield lon, lat
        return
    for item in coords:
        yield from _iter_positions(item)


def _bbox_from_positions(positions: list[tuple[float, float]]) -> list[float]:
    if not positions:
        raise ValueError("GeoJSON contains no coordinate positions")
    lons = [p[0] for p in positions]
    lats = [p[1] for p in positions]
    return _validate_bbox_values([min(lons), min(lats), max(lons), max(lats)])


def _merge_bboxes(boxes: list[list[float]]) -> list[float]:
    if not boxes:
        raise ValueError("GeoJSON contains no geometry")
    return _validate_bbox_values([
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ])


def _bbox_from_geojson_obj(obj: Any) -> list[float]:
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except json.JSONDecodeError as exc:
            raise ValueError("spatial field is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise ValueError("spatial field must be a GeoJSON object")

    raw_bbox = obj.get("bbox")
    if isinstance(raw_bbox, list) and len(raw_bbox) == 4:
        return _validate_bbox_values(raw_bbox)

    typ = obj.get("type")
    if typ == "Feature":
        geometry = obj.get("geometry")
        if not geometry:
            raise ValueError("GeoJSON Feature has no geometry")
        return _bbox_from_geojson_obj(geometry)
    if typ == "FeatureCollection":
        features = obj.get("features") or []
        return _merge_bboxes([_bbox_from_geojson_obj(feature) for feature in features])
    if typ == "GeometryCollection":
        geometries = obj.get("geometries") or []
        return _merge_bboxes([_bbox_from_geojson_obj(geometry) for geometry in geometries])

    coordinates = obj.get("coordinates")
    if coordinates is None:
        raise ValueError("GeoJSON geometry has no coordinates")
    return _bbox_from_positions(list(_iter_positions(coordinates)))


def _spatial_value(package: dict[str, Any]) -> Any:
    spatial = package.get("spatial")
    if spatial:
        return spatial
    for extra in package.get("extras") or []:
        if isinstance(extra, dict) and extra.get("key") == "spatial" and extra.get("value"):
            return extra["value"]
    return None


def _validate_url_ssrf(url: str, allowed_host: str) -> None:
    """Raise ValueError if *url* fails the SSRF host allowlist check.

    Rules:
    - Scheme must be http or https.
    - Hostname must match *allowed_host* (case-insensitive).
    - If the hostname parses as an IP address, reject RFC-1918 / loopback /
      link-local regardless of the allowed_host setting.

    Parameters
    ----------
    url:
        Resolved download URL to validate.
    allowed_host:
        Expected hostname (from ``settings.allowed_ckan_host``).

    Raises
    ------
    ValueError
        With a safe message (never includes token data).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError(f"Cannot parse resolved URL")

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Resolved URL uses disallowed scheme {parsed.scheme!r}; "
            "only http and https are permitted"
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Resolved URL has no hostname")

    # Block private / loopback IPs regardless of allowed_host
    try:
        ip = ipaddress.ip_address(host)
        for net in _BLOCKED_NETWORKS:
            if ip in net:
                raise ValueError(
                    f"Resolved URL hostname {host!r} is a private/loopback address "
                    "(SSRF guard)"
                )
    except ValueError as exc:
        # ip_address() raises ValueError for non-IP hostnames — re-raise only
        # if it's our SSRF error, otherwise continue with hostname check
        if "SSRF guard" in str(exc) or "private/loopback" in str(exc):
            raise

    # Hostname allowlist check
    if allowed_host and host != allowed_host.lower():
        raise ValueError(
            f"Resolved URL hostname {host!r} does not match allowed CKAN host "
            f"{allowed_host!r} (SSRF guard)"
        )


def resolve_resource_url(resource_id: str) -> tuple[str, str]:
    """Resolve a CKAN resource ID to its download URL and dataset (package) ID.

    Parameters
    ----------
    resource_id:
        CKAN resource UUID (e.g. ``"abc123def456"``).

    Returns
    -------
    tuple[str, str]
        ``(download_url, package_id)`` — both validated; download_url has
        passed the SSRF host check.

    Raises
    ------
    CKANResolveError
        If the resource cannot be fetched.
    ValueError
        If the resolved URL fails SSRF validation.
    """
    result = _ckan_api_get("resource_show", {"id": resource_id}, settings.ckan_url)

    download_url: str = result.get("url") or result.get("download_url") or ""
    package_id: str = result.get("package_id") or ""

    if not download_url:
        raise CKANResolveError(
            f"CKAN resource {resource_id!r} has no download URL (url field is empty)"
        )

    _validate_url_ssrf(download_url, settings.allowed_ckan_host)
    return download_url, package_id


def resolve_dataset_raster_urls(dataset_id: str, max_resources: int = 10) -> list[dict[str, str]]:
    """Resolve a CKAN dataset ID to a list of raster resource records.

    Returns at most *max_resources* resources.  Only resources with a non-empty
    URL are included.  SSRF validation is applied to each URL.

    Parameters
    ----------
    dataset_id:
        CKAN dataset ID or name slug (e.g. ``"twdb-ntgam"``).
    max_resources:
        Maximum number of resources to return (default 10, concurrency cap).

    Returns
    -------
    list[dict[str, str]]
        Each element: ``{"resource_id": "...", "url": "...", "name": "..."}``.
        Validated; SSRF-checked.

    Raises
    ------
    CKANResolveError
        If the dataset cannot be fetched.
    ValueError
        If a resolved URL fails SSRF validation.
    """
    result = _ckan_api_get("package_show", {"id": dataset_id}, settings.ckan_url)

    resources = result.get("resources") or []
    out: list[dict[str, str]] = []

    for res in resources:
        if len(out) >= max_resources:
            break
        url: str = res.get("url") or res.get("download_url") or ""
        if not url:
            continue
        resource_id: str = res.get("id") or ""
        name: str = res.get("name") or resource_id

        try:
            _validate_url_ssrf(url, settings.allowed_ckan_host)
        except ValueError as exc:
            logger.warning(
                "Skipping resource %s in dataset %s — SSRF check failed: %s",
                resource_id,
                dataset_id,
                exc,
            )
            continue

        out.append({"resource_id": resource_id, "url": url, "name": name})

    return out


def resolve_dataset_bbox(dataset_id: str) -> list[float]:
    """Resolve a CKAN dataset's ``spatial`` GeoJSON field to a WGS84 bbox.

    Parameters
    ----------
    dataset_id:
        CKAN dataset ID or slug.

    Returns
    -------
    list[float]
        ``[west, south, east, north]``.

    Raises
    ------
    CKANResolveError
        If the dataset cannot be fetched or has no valid spatial GeoJSON.
    """
    result = _ckan_api_get("package_show", {"id": dataset_id}, settings.ckan_url)
    spatial = _spatial_value(result)
    if not spatial:
        raise CKANResolveError(f"CKAN dataset {dataset_id!r} has no spatial GeoJSON field")
    try:
        return _bbox_from_geojson_obj(spatial)
    except ValueError as exc:
        raise CKANResolveError(f"CKAN dataset {dataset_id!r} has invalid spatial GeoJSON: {exc}") from exc
