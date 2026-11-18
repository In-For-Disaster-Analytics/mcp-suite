from __future__ import annotations

import json

import pytest

from dso_dynamo_mcp.client import DynamoAPIError, DynamoClient, scrub
from conftest import API, EM, SEM, SVO, TOKEN, make_settings


def test_scrub_removes_token_bearer_and_jwt_lookalikes():
    out = scrub({"a": f"oops {TOKEN} here", "b": ["Bearer abc.def.ghi"], "n": 3}, TOKEN)
    assert TOKEN not in json.dumps(out)
    assert out["n"] == 3
    assert "[REDACTED]" in out["a"]
    jwt_like = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.sigsigsigsig"
    assert jwt_like not in scrub(f"x {jwt_like}")


def test_bearer_sent_to_backends_but_not_to_semantic_search(rsps):
    c = DynamoClient(make_settings())
    rsps.get(f"{SVO}/health", json={})
    rsps.get(f"{SEM}/search", json={})
    rsps.get(f"{API}/models", json={})
    rsps.get(f"{EM}/plans/runs", json={})
    c.get("svo", "/health", token=TOKEN)
    c.get("semantic", "/search", token=TOKEN)
    c.get("api", "/models", token=TOKEN)
    c.get("em", "/plans/runs", token=TOKEN)
    auth = {call.request.url.split("/")[2]: call.request.headers.get("Authorization")
            for call in rsps.calls}
    assert auth["svo.test"] == f"Bearer {TOKEN}"
    assert auth["api.test"] == f"Bearer {TOKEN}"
    assert auth["em.test"] == f"Bearer {TOKEN}"
    assert auth["sem.test"] is None


def test_env_token_is_fallback_and_per_call_wins(rsps):
    c = DynamoClient(make_settings(tapis_token="envtoken"))
    rsps.get(f"{SVO}/health", json={})
    c.get("svo", "/health")
    c.get("svo", "/health", token="callertoken")
    assert rsps.calls[0].request.headers["Authorization"] == "Bearer envtoken"
    assert rsps.calls[1].request.headers["Authorization"] == "Bearer callertoken"


def test_error_body_and_status_are_verbatim_and_token_scrubbed(rsps):
    c = DynamoClient(make_settings())
    rsps.post(f"{EM}/plans/submit", status=409,
              json={"message": f"changed for {TOKEN}", "code": "INPUT_PLAN_CHANGED",
                    "details": {"x": 1}})
    with pytest.raises(DynamoAPIError) as e:
        c.post("em", "/plans/submit", token=TOKEN, json_body={})
    assert e.value.status == 409
    assert e.value.body["code"] == "INPUT_PLAN_CHANGED"
    assert e.value.body["details"] == {"x": 1}
    assert TOKEN not in json.dumps(e.value.body)


def test_unreachable_backend_is_an_error_with_status_zero(rsps):
    import requests
    c = DynamoClient(make_settings())
    rsps.get(f"{SVO}/health", body=requests.ConnectionError(f"boom {TOKEN}"))
    with pytest.raises(DynamoAPIError) as e:
        c.get("svo", "/health", token=TOKEN)
    assert e.value.status == 0
    assert TOKEN not in json.dumps(e.value.body)


def test_none_params_dropped_and_lists_repeated(rsps):
    c = DynamoClient(make_settings())
    rsps.get(f"{SEM}/search", json={})
    c.get("semantic", "/search", params={"q": "x", "region_id": ["a", "b"], "role": None})
    url = rsps.calls[0].request.url
    assert "region_id=a" in url and "region_id=b" in url and "role" not in url


def test_default_urls_point_at_dev_pods(monkeypatch):
    for v in ("DYNAMO_API_URL", "DYNAMO_EM_URL", "DYNAMO_SVO_URL", "DYNAMO_SEMANTIC_URL"):
        monkeypatch.delenv(v, raising=False)
    from dso_dynamo_mcp.config import Settings
    s = Settings()
    assert s.api_url == "https://mintdevapi.pods.portals.tapis.io"
    assert s.em_url == "https://mintdevensemble.pods.portals.tapis.io"
    assert s.svo_url == "https://mintdevsvo.pods.portals.tapis.io"
    assert s.semantic_url == "https://mintdevsemanticsearch.pods.portals.tapis.io"
    assert s.enable_catalog_writes is False
