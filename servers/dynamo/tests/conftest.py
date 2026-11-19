"""Shared fixtures: a fake MCP that captures tool functions, settings, and clients."""

from __future__ import annotations

from typing import Any, Callable

import pytest
import responses as resp_lib

from dso_dynamo_mcp.client import DynamoClient
from dso_dynamo_mcp.config import Settings
from dso_dynamo_mcp.tools import author, configure, flows, monitor, run, search

API = "http://api.test/v2.0.0"
EM = "http://em.test/v1"
SVO = "http://svo.test"
SEM = "http://sem.test"
HASURA = "http://hasura.test"
GRAPHQL = HASURA + "/v1/graphql"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ0ZXN0dXNlciJ9.c2lnbmF0dXJlc2lnbmF0dXJl"


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *a: Any, **k: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn
        return deco


def make_settings(**over: Any) -> Settings:
    base = dict(api_url="http://api.test", em_url="http://em.test", svo_url=SVO,
                semantic_url=SEM, hasura_url=HASURA, api_prefix="/v2.0.0", em_prefix="/v1",
                tapis_token=None,
                enable_catalog_writes=False, timeout=5.0, max_response_chars=200000)
    base.update(over)
    return Settings(**base)


def build_tools(settings: Settings) -> dict[str, Callable[..., Any]]:
    mcp = FakeMCP()
    client = DynamoClient(settings)
    for mod in (search, configure, author, flows, run, monitor):
        mod.register(mcp, client, settings)
    return mcp.tools


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def tools(settings: Settings) -> dict[str, Callable[..., Any]]:
    return build_tools(settings)


@pytest.fixture
def writable_tools() -> dict[str, Callable[..., Any]]:
    return build_tools(make_settings(enable_catalog_writes=True))


@pytest.fixture
def rsps():
    with resp_lib.RequestsMock(assert_all_requests_are_fired=False) as r:
        yield r


def writes(rsps: resp_lib.RequestsMock) -> list[Any]:
    """Recorded non-GET calls."""
    return [c for c in rsps.calls if c.request.method != "GET"]
