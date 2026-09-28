"""Shared Nominatim reverse-geocoding helpers for dso-geo.

The public Nominatim endpoint is a best-effort dependency with strict usage
limits.  This module keeps all calls synchronous, cached in-process, throttled
to at most one live request per second, and failure-tolerant: callers receive a
structured ``{"name": None, "reason": ...}`` result instead of exceptions.
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter
from typing import Any

import requests

NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "DSO-Geo-MCP/1.0 (contact: wmobley@tacc.utexas.edu)"
REQUEST_TIMEOUT_S = 5.0
REQUEST_DELAY_S = 1.1

_CITY_KEYS = (
    "neighbourhood",
    "suburb",
    "quarter",
    "city_district",
    "city",
    "town",
    "village",
    "hamlet",
    "municipality",
    "borough",
)
_FIELD_TIERS = ("city", "county", "state")

# Nominatim OSM class/type values that are geographic features rather than settlements.
_GEOCODE_FEATURE_CLASSES = frozenset({"natural", "waterway", "landuse", "leisure"})
_GEOCODE_FEATURE_TYPES = frozenset(
    {
        "bay",
        "water",
        "peak",
        "ridge",
        "stream",
        "river",
        "lake",
        "sea",
        "sound",
        "strait",
        "wetland",
        "beach",
        "cliff",
        "valley",
        "wood",
        "forest",
        "scrub",
        "coastline",
        "glacier",
        "tundra",
    }
)
_COUNTY_SUFFIXES = ("county", "parish", "borough", "census area", "municipality")

_CACHE: dict[tuple[float, float, int], dict[str, Any] | None] = {}
_CACHE_LOCK = threading.Lock()
_RATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0


def clear_cache() -> None:
    """Clear the in-process Nominatim response cache. Intended for tests."""
    global _LAST_REQUEST_AT
    with _CACHE_LOCK:
        _CACHE.clear()
    with _RATE_LOCK:
        _LAST_REQUEST_AT = 0.0


def _finite_float(value: Any, label: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(out):
        raise ValueError(f"{label} must be a finite number")
    return out


def _validate_point(lat: Any, lon: Any) -> tuple[float, float]:
    lat_f = _finite_float(lat, "lat")
    lon_f = _finite_float(lon, "lon")
    if not -90 <= lat_f <= 90:
        raise ValueError("lat must be between -90 and 90")
    if not -180 <= lon_f <= 180:
        raise ValueError("lon must be between -180 and 180")
    return lat_f, lon_f


def _validate_zoom(zoom: Any) -> int:
    try:
        zoom_i = int(zoom)
    except (TypeError, ValueError) as exc:
        raise ValueError("zoom must be an integer from 1 to 18") from exc
    if not 1 <= zoom_i <= 18:
        raise ValueError("zoom must be an integer from 1 to 18")
    return zoom_i


def validate_bbox(bbox: Any) -> list[float]:
    """Validate and normalize a [west, south, east, north] bbox."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("bbox must be [west, south, east, north]")
    west, south, east, north = (_finite_float(v, "bbox value") for v in bbox)
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        raise ValueError("bbox longitudes must be between -180 and 180")
    if not (-90 <= south <= 90 and -90 <= north <= 90):
        raise ValueError("bbox latitudes must be between -90 and 90")
    if west > east:
        raise ValueError("bbox west must be <= east")
    if south > north:
        raise ValueError("bbox south must be <= north")
    return [west, south, east, north]


def _throttle(request_delay_s: float) -> None:
    if request_delay_s <= 0:
        return
    global _LAST_REQUEST_AT
    with _RATE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_REQUEST_AT if _LAST_REQUEST_AT else request_delay_s
        if elapsed < request_delay_s:
            time.sleep(request_delay_s - elapsed)
        _LAST_REQUEST_AT = time.monotonic()


def _fetch_reverse(
    lat: float,
    lon: float,
    zoom: int,
    *,
    timeout: float = REQUEST_TIMEOUT_S,
    request_delay_s: float = REQUEST_DELAY_S,
) -> dict[str, Any] | None:
    key = (round(lat, 6), round(lon, 6), zoom)
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]

    _throttle(request_delay_s)
    try:
        resp = requests.get(
            NOMINATIM_REVERSE,
            params={
                "format": "json",
                "addressdetails": 1,
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                "zoom": zoom,
            },
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            data = None
        else:
            parsed = resp.json()
            data = parsed if isinstance(parsed, dict) and not parsed.get("error") else None
    except Exception:  # noqa: BLE001 - geocoding is best effort
        data = None

    with _CACHE_LOCK:
        _CACHE[key] = data
    return data


def _address(data: dict[str, Any]) -> dict[str, Any]:
    addr = data.get("address")
    return addr if isinstance(addr, dict) else {}


def _country_code(address: dict[str, Any]) -> str:
    return str(address.get("country_code") or "").upper()


def _format_location(tier: str, value: str, state: str | None, country_code: str = "") -> str:
    value = str(value or "").strip()
    state = str(state or "").strip()
    country_code = str(country_code or "").strip().upper()
    if tier == "state":
        return value
    if tier == "county" and country_code in {"", "US"}:
        lower = value.lower()
        if not any(lower.endswith(suffix) for suffix in _COUNTY_SUFFIXES):
            value = f"{value} County"
    parts = [value]
    if state:
        parts.append(state)
    elif country_code:
        parts.append(country_code)
    if country_code and country_code != "US" and state:
        parts.append(country_code)
    return ", ".join(p for p in parts if p)


def _field_value(address: dict[str, Any], tier: str) -> tuple[str | None, str | None]:
    if tier == "city":
        for key in _CITY_KEYS:
            value = address.get(key)
            if value:
                return str(value), key
        return None, None
    value = address.get(tier)
    return (str(value), tier) if value else (None, None)


def _extract_components(data: dict[str, Any]) -> dict[str, Any]:
    addr = _address(data)
    state = str(addr.get("state") or "")
    cc = _country_code(addr)

    for key in _CITY_KEYS:
        value = addr.get(key)
        if value:
            return {
                "name": _format_location("city", str(value), state, cc),
                "tier": "city",
                "field": key,
            }

    raw_name = str(data.get("name") or "").strip()
    is_geo_feature = data.get("class") in _GEOCODE_FEATURE_CLASSES or data.get("type") in _GEOCODE_FEATURE_TYPES
    if raw_name and not is_geo_feature and raw_name not in {state, str(addr.get("country") or "")}:
        return {
            "name": _format_location("place", raw_name, state, cc),
            "tier": "place",
            "field": "name",
        }

    county = str(addr.get("county") or "")
    if county:
        return {
            "name": _format_location("county", county, state, cc),
            "tier": "county",
            "field": "county",
        }
    if state:
        return {"name": state, "tier": "state", "field": "state"}
    country = str(addr.get("country") or "")
    if country:
        return {"name": country, "tier": "country", "field": "country"}
    return {"name": None, "tier": None, "field": None}


def _point_result(data: dict[str, Any], lat: float, lon: float, zoom: int) -> dict[str, Any]:
    components = _extract_components(data)
    return {
        "name": components.get("name"),
        "tier": components.get("tier"),
        "field": components.get("field"),
        "address": _address(data),
        "display_name": data.get("display_name") or "",
        "class": data.get("class") or "",
        "type": data.get("type") or "",
        "lat": lat,
        "lon": lon,
        "zoom": zoom,
        "source": "nominatim",
    }


def reverse_geocode(
    lat: float,
    lon: float,
    *,
    zoom: int | None = None,
    timeout: float = REQUEST_TIMEOUT_S,
    request_delay_s: float = REQUEST_DELAY_S,
) -> dict[str, Any]:
    """Best-effort place name for a single point. Never raises."""
    try:
        lat_f, lon_f = _validate_point(lat, lon)
        if zoom is not None:
            zoom_i = _validate_zoom(zoom)
            data = _fetch_reverse(lat_f, lon_f, zoom_i, timeout=timeout, request_delay_s=request_delay_s)
            if data is None:
                return {"name": None, "reason": "nominatim returned no result", "lat": lat_f, "lon": lon_f, "zoom": zoom_i}
            result = _point_result(data, lat_f, lon_f, zoom_i)
            if not result.get("name"):
                result["reason"] = "no usable place name in Nominatim response"
            return result

        last: dict[str, Any] | None = None
        for zoom_i in (14, 10):
            data = _fetch_reverse(lat_f, lon_f, zoom_i, timeout=timeout, request_delay_s=request_delay_s)
            if data is None:
                return {"name": None, "reason": "nominatim returned no result", "lat": lat_f, "lon": lon_f, "zoom": zoom_i}
            result = _point_result(data, lat_f, lon_f, zoom_i)
            last = result
            if result.get("name") and result.get("tier") not in {"state", "country"}:
                return result
        if last is not None and last.get("name"):
            return last
        return {"name": None, "reason": "no usable place name in Nominatim response", "lat": lat_f, "lon": lon_f}
    except ValueError as exc:
        return {"name": None, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - defensive best-effort boundary
        return {"name": None, "reason": f"reverse geocoding failed: {exc}"}


def _sample_points(bbox: list[float]) -> list[tuple[float, float]]:
    west, south, east, north = bbox
    centroid = ((south + north) / 2, (west + east) / 2)
    return [centroid, (south, west), (north, east)]


def _samples_payload(points: list[tuple[float, float]], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "lat": lat,
            "lon": lon,
            "name": result.get("name"),
            "tier": result.get("tier"),
            "field": result.get("field"),
        }
        for (lat, lon), result in zip(points, results)
    ]


def reverse_geocode_bbox(
    bbox: list[float],
    *,
    timeout: float = REQUEST_TIMEOUT_S,
    request_delay_s: float = REQUEST_DELAY_S,
) -> dict[str, Any]:
    """Best-effort representative place name for a bbox. Never raises."""
    try:
        bbox_norm = validate_bbox(bbox)
        points = _sample_points(bbox_norm)
        results = [
            reverse_geocode(lat, lon, zoom=14, timeout=timeout, request_delay_s=request_delay_s)
            for lat, lon in points
        ]
        addresses_by_point = [r.get("address") if isinstance(r.get("address"), dict) else None for r in results]
        resolved = [a for a in addresses_by_point if a]
        samples = _samples_payload(points, results)
        if not resolved:
            return {"name": None, "reason": "no sample points resolved", "agreement": False, "bbox": bbox_norm, "samples": samples}

        centroid_address = addresses_by_point[0] or resolved[0]
        state = str(centroid_address.get("state") or "")
        cc = _country_code(centroid_address)

        for tier in ("city", "county"):
            values = []
            fields_by_value: dict[str, str] = {}
            for address in resolved:
                value, field = _field_value(address, tier)
                if value:
                    values.append(value)
                    if field:
                        fields_by_value[value] = field
            if not values:
                continue
            value, count = Counter(values).most_common(1)[0]
            if count * 2 > len(resolved):
                return {
                    "name": _format_location(tier, value, state, cc),
                    "tier": tier,
                    "field": fields_by_value.get(value, tier),
                    "agreement": True,
                    "strategy": "majority",
                    "bbox": bbox_norm,
                    "samples": samples,
                    "source": "nominatim",
                }

        for tier in _FIELD_TIERS:
            value, field = _field_value(centroid_address, tier)
            if value:
                return {
                    "name": _format_location(tier, value, state, cc),
                    "tier": tier,
                    "field": field or tier,
                    "agreement": False,
                    "strategy": "centroid_fallback",
                    "bbox": bbox_norm,
                    "samples": samples,
                    "source": "nominatim",
                }
        return {"name": None, "reason": "no usable place name in resolved sample points", "agreement": False, "bbox": bbox_norm, "samples": samples}
    except ValueError as exc:
        return {"name": None, "reason": str(exc), "agreement": False}
    except Exception as exc:  # noqa: BLE001
        return {"name": None, "reason": f"bbox reverse geocoding failed: {exc}", "agreement": False}
