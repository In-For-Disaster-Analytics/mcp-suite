"""
Structured audit logging for live (``dry_run=False``) write attempts.

One key=value line per live write — attempted, blocked or failed — to stderr via
the stdlib logger (stdout is the MCP protocol stream). Token values never appear:
sensitive argument keys are redacted and only argument *keys* are logged.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SENSITIVE_PREFIXES = ("token", "password", "secret", "authorization", "api_key")
_SENSITIVE_EXACT = {"x-tapis-token", "x_tapis_token", "tapis_token"}


def is_sensitive_key(key: str) -> bool:
    lower = key.lower()
    return lower in _SENSITIVE_EXACT or any(lower.startswith(p) for p in _SENSITIVE_PREFIXES)


def scrub_args(args: dict[str, Any]) -> dict[str, Any]:
    """Copy of *args* with credential-looking values redacted."""
    return {k: ("[REDACTED]" if is_sensitive_key(k) else v) for k, v in args.items()}


def _ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_blocked(tool: str, reason: str) -> None:
    logger.warning("AUDIT tool=%s ts=%s status=blocked reason=%s", tool, _ts(), reason)


def log_write(
    tool: str,
    args: dict[str, Any],
    target: str,
    status: int | str,
    result_id: str | None = None,
) -> None:
    """Audit a live write: tool, target service/object, outcome — never values."""
    safe = scrub_args(args)
    logger.info(
        "AUDIT tool=%s ts=%s target=%s status=%s result_id=%s args_keys=[%s]",
        tool,
        _ts(),
        target,
        status,
        result_id or "None",
        ",".join(safe.keys()),
    )
