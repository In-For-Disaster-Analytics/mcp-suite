"""
Configuration for the DSO DYNAMO MCP server (environment variables only).

Base URLs default to the DYNAMO *dev* pods on the Tapis ``portals`` tenant
(``deploy/tapis/register_mint_stack.py`` in the monorepo defines the pod ids).
Pointing them at another stack is a config change; the same write gates apply.

Credentials
-----------
A Tapis JWT is supplied per call (``tapis_token``) or, as a fallback, via
``DYNAMO_TAPIS_TOKEN``. Every backend (model-catalog-api, Ensemble Manager)
takes it as ``Authorization: Bearer``. Values are never logged.

Gates
-----
``DYNAMO_ENABLE_CATALOG_WRITES`` (default off) additionally gates every tool
that writes catalog/registry objects (configure + author groups). Live writes of
any kind also need ``confirm=true`` on the call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# src/dso_dynamo_mcp/config.py -> parents[2] is the server project root.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
load_dotenv(override=False)

_POD_DOMAIN = "pods.portals.tapis.io"


def _url(env: str, pod: str) -> str:
    return (os.environ.get(env) or f"https://{pod}.{_POD_DOMAIN}").rstrip("/")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    api_url: str = field(default_factory=lambda: _url("DYNAMO_API_URL", "mintdevapi"))
    em_url: str = field(default_factory=lambda: _url("DYNAMO_EM_URL", "mintdevensemble"))
    # The SVO adapter is an internal engine behind the EM and catalog APIs: there is no
    # adapter URL here. Tools reach its user routes through the EM's /v1/adapter forward.
    semantic_url: str = field(
        default_factory=lambda: _url("DYNAMO_SEMANTIC_URL", "mintdevsemanticsearch")
    )
    # Hasura GraphQL in front of the catalog. Used only to author function ETL
    # pieces, which the catalog REST API has no route for.
    hasura_url: str = field(default_factory=lambda: _url("DYNAMO_HASURA_URL", "mintdevgraphql"))
    # Path prefixes the services mount their routes under.
    api_prefix: str = field(default_factory=lambda: os.environ.get("DYNAMO_API_PREFIX", "/v2.0.0"))
    em_prefix: str = field(default_factory=lambda: os.environ.get("DYNAMO_EM_PREFIX", "/v1"))

    tapis_token: str | None = field(
        default_factory=lambda: os.environ.get("DYNAMO_TAPIS_TOKEN") or None
    )
    enable_catalog_writes: bool = field(
        default_factory=lambda: _truthy(os.environ.get("DYNAMO_ENABLE_CATALOG_WRITES"))
    )
    timeout: float = field(
        default_factory=lambda: float(os.environ.get("DYNAMO_HTTP_TIMEOUT", "60"))
    )
    # Bounds a single tool response so a huge catalog listing cannot flood the client.
    max_response_chars: int = field(
        default_factory=lambda: int(os.environ.get("DYNAMO_MAX_RESPONSE_CHARS", "200000"))
    )

    def log_startup_banner(self) -> None:
        logger.info(
            "DSO DYNAMO MCP Server starting — api=%s em=%s semantic=%s hasura=%s "
            "DYNAMO_TAPIS_TOKEN=%s catalog_writes=%s",
            self.api_url,
            self.em_url,
            self.semantic_url,
            self.hasura_url,
            "[SET]" if self.tapis_token else "[NOT SET]",
            "ENABLED" if self.enable_catalog_writes else "disabled",
        )


settings = Settings()
