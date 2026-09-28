"""Unit tests for dso-geo reverse geocoding tools (mocked Nominatim/CKAN)."""

from __future__ import annotations

import json
from typing import Any

import pytest
import responses as resp_lib

from dso_geo_mcp.geocode_client import (
    NOMINATIM_REVERSE,
    clear_cache,
    reverse_geocode,
    reverse_geocode_bbox,
)

CKAN_URL = "http://localhost:5001"
PACKAGE_ID = "test-dataset"


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        if args and callable(args[0]) and not kwargs:
            fn = args[0]
            self.tools[fn.__name__] = fn
            return fn

        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _tools_of(module):
    mcp = _FakeMCP()
    module.register(mcp)
    return mcp.tools


def _nominatim(address: dict[str, Any], *, name: str = "", cls: str = "place", typ: str = "city") -> dict[str, Any]:
    return {
        "display_name": ", ".join(str(v) for v in address.values() if v),
        "name": name,
        "class": cls,
        "type": typ,
        "address": address,
    }


def _add_nominatim(address: dict[str, Any], *, name: str = "", cls: str = "place", typ: str = "city") -> None:
    resp_lib.add(resp_lib.GET, NOMINATIM_REVERSE, json=_nominatim(address, name=name, cls=cls, typ=typ), status=200)


@pytest.fixture(autouse=True)
def _clear_geocode_cache():
    clear_cache()
    yield
    clear_cache()


@resp_lib.activate
def test_reverse_geocode_prefers_specific_city_field():
    _add_nominatim({"hamlet": "Guadalupe Heights", "county": "Kerr County", "state": "Texas", "country_code": "us"})
    result = reverse_geocode(30.0, -99.1, request_delay_s=0)
    assert result["name"] == "Guadalupe Heights, Texas"
    assert result["tier"] == "city"
    assert result["field"] == "hamlet"


@resp_lib.activate
def test_reverse_geocode_falls_back_from_state_only_zoom14_to_zoom10():
    _add_nominatim({"state": "Texas", "country_code": "us"})
    _add_nominatim({"city": "New Braunfels", "county": "Comal County", "state": "Texas", "country_code": "us"})
    result = reverse_geocode(29.7, -98.1, request_delay_s=0)
    assert result["name"] == "New Braunfels, Texas"
    zooms = [int(call.request.url.split("zoom=")[1].split("&")[0]) for call in resp_lib.calls]
    assert zooms == [14, 10]


@resp_lib.activate
def test_reverse_geocode_excludes_raw_geographic_feature_name():
    _add_nominatim(
        {"county": "Bethel Census Area", "state": "Alaska", "country_code": "us"},
        name="Hooper Bay",
        cls="natural",
        typ="bay",
    )
    result = reverse_geocode(61.5, -166.1, request_delay_s=0)
    assert result["name"] == "Bethel Census Area, Alaska"
    assert result["tier"] == "county"


@resp_lib.activate
def test_reverse_geocode_bbox_accepts_city_majority():
    _add_nominatim({"city": "New Braunfels", "county": "Comal County", "state": "Texas", "country_code": "us"})
    _add_nominatim({"city": "New Braunfels", "county": "Comal County", "state": "Texas", "country_code": "us"})
    _add_nominatim({"city": "San Marcos", "county": "Hays County", "state": "Texas", "country_code": "us"})
    result = reverse_geocode_bbox([-98.35, 29.55, -97.85, 30.05], request_delay_s=0)
    assert result["name"] == "New Braunfels, Texas"
    assert result["tier"] == "city"
    assert result["agreement"] is True
    assert result["strategy"] == "majority"


@resp_lib.activate
def test_reverse_geocode_bbox_falls_back_to_centroid_not_state_majority():
    _add_nominatim({"hamlet": "Guadalupe Heights", "county": "Kerr County", "state": "Texas", "country_code": "us"})
    _add_nominatim({"county": "Bandera County", "state": "Texas", "country_code": "us"})
    _add_nominatim({"county": "Gillespie County", "state": "Texas", "country_code": "us"})
    result = reverse_geocode_bbox([-99.33, 29.68, -98.89, 30.32], request_delay_s=0)
    assert result["name"] == "Guadalupe Heights, Texas"
    assert result["agreement"] is False
    assert result["strategy"] == "centroid_fallback"


@resp_lib.activate
def test_reverse_geocode_bbox_does_not_accept_two_point_tie_as_majority():
    _add_nominatim({"county": "Kerr County", "state": "Texas", "country_code": "us"})
    _add_nominatim({"county": "Bandera County", "state": "Texas", "country_code": "us"})
    resp_lib.add(resp_lib.GET, NOMINATIM_REVERSE, json={"error": "Unable to geocode"}, status=200)

    result = reverse_geocode_bbox([-99.33, 29.68, -98.89, 30.32], request_delay_s=0)

    assert result["name"] == "Kerr County, Texas"
    assert result["agreement"] is False
    assert result["strategy"] == "centroid_fallback"


@resp_lib.activate
def test_invalid_bbox_returns_structured_failure_without_http():
    result = reverse_geocode_bbox([1, 2, 3], request_delay_s=0)
    assert result["name"] is None
    assert "bbox" in result["reason"]
    assert len(resp_lib.calls) == 0


@resp_lib.activate
def test_mcp_bbox_tool_resolves_dataset_spatial_then_geocodes():
    from dso_geo_mcp.tools import geocode

    spatial = json.dumps({
        "type": "Polygon",
        "coordinates": [[
            [-98.2, 29.7], [-97.9, 29.7], [-97.9, 30.0], [-98.2, 30.0], [-98.2, 29.7]
        ]],
    })
    resp_lib.add(
        resp_lib.GET,
        f"{CKAN_URL}/api/3/action/package_show",
        json={"success": True, "result": {"id": PACKAGE_ID, "spatial": spatial}},
        status=200,
    )
    _add_nominatim({"city": "New Braunfels", "county": "Comal County", "state": "Texas", "country_code": "us"})
    _add_nominatim({"city": "New Braunfels", "county": "Comal County", "state": "Texas", "country_code": "us"})
    _add_nominatim({"city": "New Braunfels", "county": "Comal County", "state": "Texas", "country_code": "us"})

    fn = _tools_of(geocode)["reverse_geocode_bbox"]
    result = fn(dataset_id=PACKAGE_ID)
    assert result["name"] == "New Braunfels, Texas"
    assert result["agreement"] is True
