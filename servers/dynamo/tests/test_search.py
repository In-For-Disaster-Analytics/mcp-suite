from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from conftest import API, SEM, SVO, TOKEN


def test_search_models_maps_to_semantic_search(tools, rsps):
    rsps.get(f"{SEM}/search", json={"target": "model_configuration", "results": [{"id": "m1"}]})
    out = tools["search_models"]("rainfall", limit=5, variable_id=["v1"], role="input")
    q = parse_qs(urlparse(rsps.calls[0].request.url).query)
    assert q["target"] == ["model_configuration"] and q["q"] == ["rainfall"]
    assert q["limit"] == ["5"] and q["variable_id"] == ["v1"] and q["role"] == ["input"]
    assert out["results"][0]["id"] == "m1"


def test_search_standard_variables_uses_svo_target(tools, rsps):
    rsps.get(f"{SEM}/search", json={"results": []})
    tools["search_standard_variables"]("head")
    assert parse_qs(urlparse(rsps.calls[0].request.url).query)["target"] == ["svo"]


def test_get_model_configuration_percent_encodes_the_url_id(tools, rsps):
    cid = "https://w3id.org/okn/i/mint/modflow6_cfg"
    rsps.get(f"{API}/modelconfigurations/https%3A%2F%2Fw3id.org%2Fokn%2Fi%2Fmint%2Fmodflow6_cfg",
             json={"id": cid})
    assert tools["get_model_configuration"](cid)["id"] == cid


def test_401_is_passed_through_verbatim(tools, rsps):
    rsps.get(f"{SVO}/standard-variables", status=401,
             json={"message": "Missing or invalid Authorization header"})
    out = tools["list_standard_variables"](tapis_token=TOKEN)
    assert out["error"] == "backend_error" and out["status"] == 401
    assert out["service"] == "svo"
    assert out["body"]["message"].startswith("Missing")
    assert TOKEN not in json.dumps(out)


def test_adapter_error_codes_pass_through(tools, rsps):
    rsps.post(f"{SVO}/plans/discover-sources", status=422,
              json={"detail": {"code": "NO_PATH", "message": "none"}})
    out = tools["find_sources_for_target"](standard_variable_uri="u")
    assert out["status"] == 422 and out["body"]["detail"]["code"] == "NO_PATH"


def test_find_datasets_body_shape(tools, rsps):
    rsps.post(f"{SVO}/datasets/find", json={"datasets": []})
    tools["find_datasets"](["precipitation"], start_time_gte="2020-01-01", limit=10)
    body = json.loads(rsps.calls[0].request.body)
    assert body == {"standard_variable_names__in": ["precipitation"], "limit": 10,
                    "start_time__gte": "2020-01-01"}


def test_large_results_are_bounded(rsps):
    from conftest import build_tools, make_settings
    t = build_tools(make_settings(max_response_chars=100))
    rsps.get(f"{SVO}/data-objects", json=[{"label": "x" * 50}] * 20)
    out = t["list_data_objects"]()
    assert out["truncated"] is True and out["total_chars"] > 100
