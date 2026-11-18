"""Shared helpers for the tool modules: error mapping, bounding, write gate."""

from __future__ import annotations

import json
from typing import Any, Callable

from .. import audit
from ..client import DynamoAPIError, scrub
from ..config import Settings

TOKEN_DOC = (
    "tapis_token: Tapis JWT for this call (falls back to DYNAMO_TAPIS_TOKEN). "
    "Never logged or returned."
)


def backend_error(exc: DynamoAPIError) -> dict[str, Any]:
    """Surface a backend failure verbatim: status, service and the parsed body."""
    return {
        "error": "backend_error",
        "status": exc.status,
        "service": exc.service,
        "request": f"{exc.method} {exc.path}",
        "body": exc.body,
    }


def run(fn: Callable[[], Any], token: str | None = None) -> Any:
    """Run *fn*, mapping DynamoAPIError to an error dict (never raising)."""
    try:
        return fn()
    except DynamoAPIError as exc:
        return backend_error(exc)


def bound(payload: Any, settings: Settings, token: str | None = None) -> Any:
    """Truncate oversized results (as a marker dict) so one call cannot flood a client."""
    try:
        text = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return payload
    if len(text) <= settings.max_response_chars:
        return payload
    return {
        "truncated": True,
        "total_chars": len(text),
        "preview": scrub(text[: settings.max_response_chars], token),
        "hint": "Narrow the query (limit/filters) to get the full result.",
    }


def write_gate(
    settings: Settings,
    tool: str,
    effective_token: str | None,
    *,
    confirm: bool,
    catalog: bool = False,
) -> dict[str, Any] | None:
    """Pre-flight checks for a LIVE write. Returns an error dict (no request made) or None.

    Order: confirm -> token -> catalog-write flag. Dry-runs never reach here.
    """
    if not confirm:
        audit.log_blocked(tool, "confirm_required")
        return {
            "error": "confirm_required",
            "message": (
                "Live writes need confirm=true in addition to dry_run=false. "
                "Show the dry-run preview to the user first and only confirm on their go-ahead."
            ),
        }
    if not effective_token:
        audit.log_blocked(tool, "no_token")
        return {
            "error": "writes_require_token",
            "message": (
                "No Tapis token for this write. Pass tapis_token or set DYNAMO_TAPIS_TOKEN."
            ),
        }
    if catalog and not settings.enable_catalog_writes:
        audit.log_blocked(tool, "catalog_writes_disabled")
        return {
            "error": "catalog_writes_disabled",
            "message": (
                "Catalog/registry writes are disabled on this server. Set "
                "DYNAMO_ENABLE_CATALOG_WRITES=1 to enable the configure and author tools "
                "(the catalog's Hasura role permissions are the only server-side guard)."
            ),
        }
    return None
