"""
HTTP client for the four DYNAMO backends.

  api       model-catalog-api            ``{api_url}/v2.0.0``
  em        mint-ensemble-manager        ``{em_url}/v1``
  adapter   svo-adapter-service, THROUGH the EM  ``{em_url}/v1/adapter``  (the adapter is an
            internal engine; the EM forwards an allowlist of its user routes with the caller's
            token. No tool calls the adapter host.)
  semantic  semantic-search-service      ``{semantic_url}``
  hasura    Hasura GraphQL (catalog)     ``{hasura_url}/v1/graphql``

Every backend authenticates a Tapis JWT as ``Authorization: Bearer``. The token
is passed per call (falling back to ``DYNAMO_TAPIS_TOKEN``) and is scrubbed from
anything returned or raised. Backend errors are surfaced VERBATIM (status +
parsed JSON body) so callers see codes like ``INPUT_PLAN_CHANGED`` or
``DEFERRED_EXECUTION_ID_MISMATCH`` exactly as the service produced them.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import requests

from .config import Settings

_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")


class DynamoAPIError(Exception):
    """A backend returned a non-2xx status or could not be reached."""

    def __init__(self, status: int, body: Any, service: str, method: str, path: str) -> None:
        self.status = status
        self.body = body
        self.service = service
        self.method = method
        self.path = path
        super().__init__(f"{service} {method} {path} -> {status}")


def scrub(value: Any, token: str | None = None) -> Any:
    """Recursively remove the caller's token (and any bearer/JWT lookalike)."""
    if isinstance(value, str):
        out = value
        if token:
            out = out.replace(token, "[REDACTED]")
        out = _BEARER_RE.sub("Bearer [REDACTED]", out)
        return _JWT_RE.sub("[REDACTED]", out)
    if isinstance(value, list):
        return [scrub(v, token) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v, token) for k, v in value.items()}
    return value


def enc(identifier: str) -> str:
    """Percent-encode a catalog id (they are URLs) for use as a path segment."""
    return quote(identifier, safe="")


class DynamoClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self._s = settings
        self._session = session or requests.Session()

    # ---- routing --------------------------------------------------------
    def base(self, service: str) -> str:
        s = self._s
        return {
            "api": s.api_url + s.api_prefix,
            "em": s.em_url + s.em_prefix,
            "adapter": s.em_url + s.em_prefix + "/adapter",
            "semantic": s.semantic_url,
            "hasura": s.hasura_url,
        }[service]

    def effective_token(self, token: str | None) -> str | None:
        return token or self._s.tapis_token

    # ---- transport ------------------------------------------------------
    def request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        tok = self.effective_token(token)
        hdrs = {"Accept": "application/json"}
        # semantic-search is unauthenticated; never send a credential it does not need.
        if tok and service != "semantic":
            hdrs["Authorization"] = f"Bearer {tok}"
        if headers:
            hdrs.update(headers)
        # Drop None-valued params; keep lists (requests repeats the key).
        clean = {k: v for k, v in (params or {}).items() if v is not None} or None
        url = self.base(service) + path
        try:
            resp = self._session.request(
                method,
                url,
                params=clean,
                json=json_body,
                headers=hdrs,
                timeout=self._s.timeout,
            )
        except requests.RequestException as exc:
            raise DynamoAPIError(
                0,
                {"message": scrub(f"{type(exc).__name__}: {exc}", tok)},
                service,
                method,
                path,
            ) from None
        body = self._parse(resp)
        if not resp.ok:
            raise DynamoAPIError(resp.status_code, scrub(body, tok), service, method, path)
        return scrub(body, tok)

    @staticmethod
    def _parse(resp: requests.Response) -> Any:
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"text": resp.text[:2000]}

    # ---- GraphQL --------------------------------------------------------
    def graphql(self, query: str, variables: dict[str, Any], *, token: str | None = None) -> Any:
        """Run one GraphQL operation against Hasura as the caller.

        Hasura answers 200 even when the operation fails, so an ``errors`` body is
        raised like any other backend failure.
        """
        path = "/v1/graphql"
        body = self.request("hasura", "POST", path, token=token,
                            json_body={"query": query, "variables": variables})
        if isinstance(body, dict) and body.get("errors"):
            raise DynamoAPIError(200, {"errors": body["errors"]}, "hasura", "POST", path)
        return (body or {}).get("data") if isinstance(body, dict) else body

    # ---- verbs ----------------------------------------------------------
    def get(self, service: str, path: str, **kw: Any) -> Any:
        return self.request(service, "GET", path, **kw)

    def post(self, service: str, path: str, **kw: Any) -> Any:
        return self.request(service, "POST", path, **kw)

    def put(self, service: str, path: str, **kw: Any) -> Any:
        return self.request(service, "PUT", path, **kw)
