"""
GDAL actor — injection-safe parameter validation contract.

Every public function either returns the validated value or raises ValueError
with a message that contains NO token or secret data.

Contract (locked by Decision 7 in the design spec):
  - operation       : must be in ALLOWED_OPERATIONS
  - target_crs      : int in [1, 999999]; returns ["-t_srs", "EPSG:<n>"]
  - output_name     : matches ^[A-Za-z0-9_\\-.]+$, ends with .tif; no slashes or ..
  - compression     : one of {deflate, lzw, zstd, none}
  - overview_levels : list of int, each 2–512, at most 10 elements
  - clip_geometry   : GeoJSON dict, type Polygon or MultiPolygon,
                      coords within WGS84 bounds, at most 1000 vertices
  - input_url       : http(s) or tapis scheme; if ALLOWED_HOST env is set, host must match
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_OPERATIONS: frozenset[str] = frozenset([
    "gdalinfo", "reproject", "cog", "clip", "overviews",
    # MODFLOW / rasterio operations (require flopy + rasterio in image)
    "extract_point", "aggregate_gma",
    "extract_budget_gma", "extract_satthk_gma", "hds_to_geotiff",
    "hds_aggregate_gma",
    "rasterize_points", "dis_top_to_geotiff",
])

_COMPRESSION_ENUM: frozenset[str] = frozenset(["deflate", "lzw", "zstd", "none"])

_OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9_\-.]+$")

# WGS84 coordinate bounds
_WGS84_LON_MIN, _WGS84_LON_MAX = -180.0, 180.0
_WGS84_LAT_MIN, _WGS84_LAT_MAX = -90.0, 90.0

_MAX_OVERVIEW_LEVELS = 10
_MAX_CLIP_VERTICES = 1000

_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ATTRIBUTE_FILTER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=\s*-?\d+(\.\d+)?$")
_MAX_CRS_WKT_LEN = 20_000
_CRS_WKT_ROOT_NODES = (
    "GEOGCRS", "PROJCRS", "COMPOUNDCRS", "BOUNDCRS", "GEOCCS", "PROJCS", "GEOGCS",
)


# ---------------------------------------------------------------------------
# Operation allowlist
# ---------------------------------------------------------------------------


def validate_operation(op: Any) -> str:
    """Return *op* if it is in the allowed-operations set, else raise ValueError.

    >>> validate_operation("gdalinfo")
    'gdalinfo'
    >>> validate_operation("rm")  # doctest: +ELLIPSIS
    Traceback (most recent call last):
        ...
    ValueError: ...
    """
    if not isinstance(op, str):
        raise ValueError(f"operation must be a string, got {type(op).__name__}")
    if op not in ALLOWED_OPERATIONS:
        raise ValueError(
            f"operation {op!r} is not allowed; permitted: {sorted(ALLOWED_OPERATIONS)}"
        )
    return op


# ---------------------------------------------------------------------------
# input_url — SSRF guard
# ---------------------------------------------------------------------------


def validate_input_url(url: Any) -> str:
    """Validate *url* is an http(s) or tapis URL and, if ALLOWED_HOST is set, matches it.

    Returns the validated URL string on success, raises ValueError on failure.
    Never logs the URL token portions.

    The ALLOWED_HOST env var (host[:port]) restricts which host /vsicurl/ reads
    are allowed to target (SSRF control). If unset, any http(s) host is
    accepted (suitable for local-test mode with public COGs).
    """
    if not isinstance(url, str):
        raise ValueError("input_url must be a string")
    parsed = urlparse(url)
    if parsed.scheme == "tapis":
        if not parsed.netloc or not parsed.path.strip("/"):
            raise ValueError("tapis input_url must include a system and path")
        if any(part == ".." for part in parsed.path.split("/")):
            raise ValueError("tapis input_url path must not contain '..'")
        return url

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"input_url must use http, https, or tapis scheme (got {parsed.scheme!r})"
        )
    if not parsed.netloc:
        raise ValueError("input_url has no host")

    allowed_host = os.environ.get("ALLOWED_HOST", "").strip()
    if allowed_host:
        # parsed.netloc may include :port; compare against ALLOWED_HOST which
        # may also include :port.  Strip to hostname only for a forgiving match.
        request_host = parsed.hostname or ""
        allowed_hostname = allowed_host.split(":")[0].lower()
        if request_host.lower() != allowed_hostname:
            raise ValueError(
                f"input_url host {request_host!r} is not permitted "
                f"(ALLOWED_HOST={allowed_hostname!r})"
            )
    return url


def _host_allowed(request_host: str, allowed_hosts: str) -> bool:
    """Return true if request_host matches a comma-separated host allowlist."""
    host = request_host.lower()
    for allowed in (item.strip().lower() for item in allowed_hosts.split(",")):
        if not allowed:
            continue
        allowed = allowed.split(":")[0]
        if allowed.startswith("*."):
            suffix = allowed[1:]
            if host.endswith(suffix):
                return True
        elif host == allowed:
            return True
    return False


def validate_boundary_uri(uri: Any) -> str:
    """Validate a boundary URI used by aggregate_gma operations.

    Boundary URIs may be HTTPS/HTTP GeoJSON or ArcGIS FeatureServer/MapServer
    endpoints, or tapis:// files. If ALLOWED_BOUNDARY_HOSTS is set, HTTP(S)
    hosts must match the comma-separated allowlist. tapis:// URIs follow the
    same path traversal guard as input_url.
    """
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("boundary_uri must be a non-empty string")
    parsed = urlparse(uri)
    if parsed.scheme == "tapis":
        if not parsed.netloc or not parsed.path.strip("/"):
            raise ValueError("tapis boundary_uri must include a system and path")
        if any(part == ".." for part in parsed.path.split("/")):
            raise ValueError("tapis boundary_uri path must not contain '..'")
        return uri

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"boundary_uri must use http, https, or tapis scheme (got {parsed.scheme!r})"
        )
    if not parsed.netloc:
        raise ValueError("boundary_uri has no host")

    allowed_hosts = os.environ.get("ALLOWED_BOUNDARY_HOSTS", "").strip()
    if allowed_hosts and not _host_allowed(parsed.hostname or "", allowed_hosts):
        raise ValueError("boundary_uri host is not permitted")
    return uri


def validate_grid_uri(uri: Any) -> str:
    """Validate a model grid/DIS URI used by HDS georeferencing.

    Grid URIs may be HTTPS/HTTP resources or tapis:// files. If
    ALLOWED_GRID_HOSTS is set, HTTP(S) hosts must match the comma-separated
    allowlist. tapis:// URIs follow the same path traversal guard as input_url.
    """
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("grid_uri must be a non-empty string")
    parsed = urlparse(uri)
    if parsed.scheme == "tapis":
        if not parsed.netloc or not parsed.path.strip("/"):
            raise ValueError("tapis grid_uri must include a system and path")
        if any(part == ".." for part in parsed.path.split("/")):
            raise ValueError("tapis grid_uri path must not contain '..'")
        return uri

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"grid_uri must use http, https, or tapis scheme (got {parsed.scheme!r})"
        )
    if not parsed.netloc:
        raise ValueError("grid_uri has no host")

    allowed_hosts = os.environ.get("ALLOWED_GRID_HOSTS", "").strip()
    if allowed_hosts and not _host_allowed(parsed.hostname or "", allowed_hosts):
        raise ValueError("grid_uri host is not permitted")
    return uri


# ---------------------------------------------------------------------------
# target_crs — EPSG integer guard
# ---------------------------------------------------------------------------


def validate_target_crs(crs: Any) -> list[str]:
    """Validate *crs* is an integer in [1, 999999] and return the gdalwarp flag list.

    Returns ``["-t_srs", "EPSG:<n>"]``.  Never forwards a raw string to the
    subprocess.

    >>> validate_target_crs(4326)
    ['-t_srs', 'EPSG:4326']
    >>> validate_target_crs(0)
    Traceback (most recent call last):
        ...
    ValueError: target_crs must be an integer in [1, 999999]
    """
    if not isinstance(crs, int) or isinstance(crs, bool):
        raise ValueError("target_crs must be an integer in [1, 999999]")
    if not (1 <= crs <= 999999):
        raise ValueError("target_crs must be an integer in [1, 999999]")
    return ["-t_srs", f"EPSG:{crs}"]


# ---------------------------------------------------------------------------
# output_name — path traversal / metachar guard
# ---------------------------------------------------------------------------


def validate_output_name(name: Any) -> str:
    """Validate *name* as a safe bare filename (no path separators or metacharacters).

    Rules:
    - Must be a non-empty string.
    - Must match ``^[A-Za-z0-9_\\-.]+$`` (alphanumeric, underscore, dash, dot).
    - Must end with ``.tif`` (case-sensitive).
    - Must not contain ``/``, ``\\``, or ``..``.

    Returns the validated name on success.

    >>> validate_output_name("output.tif")
    'output.tif'
    >>> validate_output_name("../etc/passwd")
    Traceback (most recent call last):
        ...
    ValueError: ...
    """
    if not isinstance(name, str) or not name:
        raise ValueError("output_name must be a non-empty string")
    # Explicit checks before regex (defence in depth)
    if "/" in name or "\\" in name:
        raise ValueError("output_name must not contain path separators")
    if ".." in name:
        raise ValueError("output_name must not contain '..'")
    if not _OUTPUT_NAME_RE.match(name):
        raise ValueError(
            "output_name must match ^[A-Za-z0-9_\\-.]+$ "
            "(letters, digits, underscore, dash, dot only)"
        )
    if not name.endswith(".tif"):
        raise ValueError("output_name must end with '.tif'")
    return name


# ---------------------------------------------------------------------------
# compression — enum guard
# ---------------------------------------------------------------------------


def validate_compression(comp: Any) -> str:
    """Validate *comp* is one of {deflate, lzw, zstd, none}.

    >>> validate_compression("deflate")
    'deflate'
    >>> validate_compression("gzip")
    Traceback (most recent call last):
        ...
    ValueError: ...
    """
    if not isinstance(comp, str):
        raise ValueError(f"compression must be a string, got {type(comp).__name__}")
    if comp not in _COMPRESSION_ENUM:
        raise ValueError(
            f"compression {comp!r} is not allowed; "
            f"permitted: {sorted(_COMPRESSION_ENUM)}"
        )
    return comp


# ---------------------------------------------------------------------------
# overview_levels — list of bounded ints
# ---------------------------------------------------------------------------


def validate_overview_levels(levels: Any) -> list[int]:
    """Validate *levels* is a list of ints each in [2, 512] with at most 10 elements.

    Returns the validated list on success.

    >>> validate_overview_levels([2, 4, 8])
    [2, 4, 8]
    >>> validate_overview_levels([1])
    Traceback (most recent call last):
        ...
    ValueError: ...
    """
    if not isinstance(levels, list):
        raise ValueError("overview_levels must be a list")
    if len(levels) == 0:
        raise ValueError("overview_levels must not be empty")
    if len(levels) > _MAX_OVERVIEW_LEVELS:
        raise ValueError(
            f"overview_levels must have at most {_MAX_OVERVIEW_LEVELS} elements "
            f"(got {len(levels)})"
        )
    for i, lvl in enumerate(levels):
        if not isinstance(lvl, int) or isinstance(lvl, bool):
            raise ValueError(
                f"overview_levels[{i}] must be an integer (got {type(lvl).__name__})"
            )
        if not (2 <= lvl <= 512):
            raise ValueError(
                f"overview_levels[{i}]={lvl} is out of range; each value must be in [2, 512]"
            )
    return list(levels)


# ---------------------------------------------------------------------------
# clip_geometry — GeoJSON Polygon / MultiPolygon guard
# ---------------------------------------------------------------------------


def _count_vertices(geometry: dict[str, Any]) -> int:
    """Count total coordinate pairs in a GeoJSON Polygon or MultiPolygon."""
    geo_type = geometry.get("type", "")
    coords = geometry.get("coordinates", [])
    if geo_type == "Polygon":
        return sum(len(ring) for ring in coords)
    elif geo_type == "MultiPolygon":
        return sum(len(ring) for polygon in coords for ring in polygon)
    return 0


def _check_wgs84_bounds(geometry: dict[str, Any]) -> None:
    """Raise ValueError if any coordinate is outside WGS84 bounds."""
    geo_type = geometry.get("type", "")
    coords = geometry.get("coordinates", [])

    def check_ring(ring: list) -> None:
        for pt in ring:
            if not (isinstance(pt, (list, tuple)) and len(pt) >= 2):
                raise ValueError("clip_geometry coordinate must be [lon, lat] pair")
            lon, lat = pt[0], pt[1]
            if not (_WGS84_LON_MIN <= lon <= _WGS84_LON_MAX):
                raise ValueError(
                    f"clip_geometry longitude {lon} is outside WGS84 bounds "
                    f"[{_WGS84_LON_MIN}, {_WGS84_LON_MAX}]"
                )
            if not (_WGS84_LAT_MIN <= lat <= _WGS84_LAT_MAX):
                raise ValueError(
                    f"clip_geometry latitude {lat} is outside WGS84 bounds "
                    f"[{_WGS84_LAT_MIN}, {_WGS84_LAT_MAX}]"
                )

    if geo_type == "Polygon":
        for ring in coords:
            check_ring(ring)
    elif geo_type == "MultiPolygon":
        for polygon in coords:
            for ring in polygon:
                check_ring(ring)


def validate_clip_geometry(geometry: Any) -> dict[str, Any]:
    """Validate *geometry* as a safe GeoJSON Polygon or MultiPolygon dict.

    Rules:
    - Must be a Python dict (not a JSON string).
    - ``type`` must be ``"Polygon"`` or ``"MultiPolygon"``.
    - All coordinates must be within WGS84 bounds (lon ±180, lat ±90).
    - Total vertex count must not exceed 1000.

    Returns the validated dict on success.
    """
    if not isinstance(geometry, dict):
        raise ValueError(
            "clip_geometry must be a GeoJSON dict (not a string or other type)"
        )
    geo_type = geometry.get("type")
    if geo_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(
            f"clip_geometry type must be 'Polygon' or 'MultiPolygon' "
            f"(got {geo_type!r})"
        )
    if "coordinates" not in geometry:
        raise ValueError("clip_geometry must have a 'coordinates' field")
    if not isinstance(geometry["coordinates"], list):
        raise ValueError("clip_geometry.coordinates must be a list")

    _check_wgs84_bounds(geometry)

    n_vertices = _count_vertices(geometry)
    if n_vertices > _MAX_CLIP_VERTICES:
        raise ValueError(
            f"clip_geometry has {n_vertices} vertices; "
            f"maximum allowed is {_MAX_CLIP_VERTICES}"
        )
    return geometry


# ---------------------------------------------------------------------------
# value_field / layer_name — safe OGR identifier guard (rasterize_points)
# ---------------------------------------------------------------------------


def validate_field_name(name: Any) -> str:
    """Validate *name* as a safe OGR field/layer identifier.

    Must match ``^[A-Za-z_][A-Za-z0-9_]{0,63}$`` — a bare identifier, never a
    raw string forwarded to a shell. Used for both the ``-a`` burn field and
    the optional ``-l`` layer name of ``gdal_rasterize``.

    >>> validate_field_name("storativit")
    'storativit'
    >>> validate_field_name("1; drop")
    Traceback (most recent call last):
        ...
    ValueError: ...
    """
    if not isinstance(name, str) or not _FIELD_NAME_RE.match(name):
        raise ValueError(
            "field name must match ^[A-Za-z_][A-Za-z0-9_]{0,63}$ (bare identifier only)"
        )
    return name


# ---------------------------------------------------------------------------
# attribute_filter — narrow "<field> = <number>" guard (rasterize_points)
# ---------------------------------------------------------------------------


def validate_attribute_filter(where: Any) -> str:
    """Validate *where* is a simple ``<field> = <number>`` equality filter.

    Deliberately narrow (not general SQL) — this is forwarded to
    ``gdal_rasterize -where`` as a subprocess argument, so only a single
    equality comparison is permitted (e.g. selecting one MODFLOW layer out of
    a stacked point dataset: ``"layer = 4"``).

    >>> validate_attribute_filter("layer = 4")
    'layer = 4'
    >>> validate_attribute_filter("1=1 OR 1=1")
    Traceback (most recent call last):
        ...
    ValueError: ...
    """
    if not isinstance(where, str) or not _ATTRIBUTE_FILTER_RE.match(where.strip()):
        raise ValueError(
            "attribute_filter must match '<field> = <number>' (a single equality only)"
        )
    return where.strip()


# ---------------------------------------------------------------------------
# pixel_size — positive bounded number guard (rasterize_points)
# ---------------------------------------------------------------------------


def validate_pixel_size(size: Any) -> float:
    """Validate *size* is a positive number in (0, 1_000_000] (source CRS units).

    >>> validate_pixel_size(1320)
    1320.0
    >>> validate_pixel_size(-1)
    Traceback (most recent call last):
        ...
    ValueError: ...
    """
    if not isinstance(size, (int, float)) or isinstance(size, bool):
        raise ValueError(f"pixel_size must be a number, got {type(size).__name__}")
    size = float(size)
    if not (0 < size <= 1_000_000):
        raise ValueError("pixel_size must be in (0, 1000000]")
    return size


# ---------------------------------------------------------------------------
# crs_wkt — bounded, structurally-sane WKT guard (dis_top_to_geotiff)
# ---------------------------------------------------------------------------


def validate_crs_wkt(wkt: Any) -> str:
    """Validate *wkt* looks like a CRS WKT string (length-capped, recognized root node).

    This only guards against resource exhaustion and obvious garbage; full
    parsing (and the corresponding clean error on malformed WKT) happens in
    rasterio at write time, since this module has no rasterio dependency.

    >>> validate_crs_wkt('GEOGCRS["WGS 84"]')
    'GEOGCRS["WGS 84"]'
    >>> validate_crs_wkt("not a crs")
    Traceback (most recent call last):
        ...
    ValueError: ...
    """
    if not isinstance(wkt, str) or not wkt.strip():
        raise ValueError("crs_wkt must be a non-empty string")
    if len(wkt) > _MAX_CRS_WKT_LEN:
        raise ValueError(f"crs_wkt exceeds maximum length of {_MAX_CRS_WKT_LEN} characters")
    head = wkt.lstrip()[:20].upper()
    if not any(head.startswith(k) for k in _CRS_WKT_ROOT_NODES):
        raise ValueError(
            "crs_wkt does not look like a CRS WKT string "
            f"(must start with one of {sorted(_CRS_WKT_ROOT_NODES)})"
        )
    return wkt
