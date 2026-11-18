"""
Catalog payload + sequencing helpers shared by the configure and author tools.

Conventions follow the proven registration script
(``modflow-executables/scripts/register_to_mint.py``) and the model-catalog-api
write mapper:

* ids are URLs under ``https://w3id.org/okn/i/mint/``;
* scalar columns are sent as one-element arrays (RDF convention);
* ``keywords`` is a scalar text column -> one comma-joined string;
* a version nests under its model's ``hasVersion`` (sets the FK);
* a configuration's I/O are *junction* relationships and are linked by a
  separate PUT after the DatasetSpecifications exist;
* a configuration's ``softwareVersionId`` / a setup's ``modelConfigurationId``
  are scalar FKs applied by PUT.
"""

from __future__ import annotations

import re
from typing import Any

from ..client import DynamoAPIError, DynamoClient, enc

ID_PREFIX = "https://w3id.org/okn/i/mint/"

# POSTs that hit an existing row are fine (idempotent re-run): same tolerance as the script.
_EXISTS_MARKERS = ("uniqueness violation", "duplicate key")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "item"


def mint_id(slug_or_id: str) -> str:
    """Return *slug_or_id* unchanged if it is already a URL id, else prefix it."""
    return slug_or_id if re.match(r"^https?://", slug_or_id) else ID_PREFIX + slug_or_id


def arr(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def first(obj: dict[str, Any] | None, *keys: str) -> Any:
    """First non-empty value among *keys* (snake/camel variants), unwrapping 1-element lists."""
    for key in keys:
        value = (obj or {}).get(key)
        if value in (None, "", []):
            continue
        return value[0] if isinstance(value, list) and len(value) == 1 else value
    return None


def step(method: str, resource: str, payload: dict[str, Any], entity_id: str | None = None) -> dict[str, Any]:
    path = f"/{resource}" if method == "POST" else f"/{resource}/{enc(entity_id or payload['id'])}"
    return {"method": method, "resource": resource, "path": path,
            "id": entity_id or payload.get("id"), "payload": payload}


def describe(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The preview shown on a dry run: exactly what a live call would send, in order."""
    return [{"order": i + 1, "request": f"{s['method']} {s['path']}", "payload": s["payload"]}
            for i, s in enumerate(steps)]


def _already_exists(exc: DynamoAPIError) -> bool:
    if exc.status == 409:
        return True
    text = str(exc.body).lower()
    return any(m in text for m in _EXISTS_MARKERS)


def execute(client: DynamoClient, steps: list[dict[str, Any]], token: str | None) -> dict[str, Any]:
    """Run *steps* top-down. Non-atomic: on failure stop and report what was created.

    No rollback and no delete tools exist in v1, so the caller is told exactly which
    objects now exist and which step failed.
    """
    done: list[dict[str, Any]] = []
    for s in steps:
        try:
            result = client.request("api", s["method"], s["path"], token=token,
                                    json_body=s["payload"])
            done.append({"request": f"{s['method']} {s['path']}", "id": s["id"], "status": "ok",
                         "result": result})
        except DynamoAPIError as exc:
            if s["method"] == "POST" and _already_exists(exc):
                done.append({"request": f"{s['method']} {s['path']}", "id": s["id"],
                             "status": "exists"})
                continue
            return {
                "ok": False,
                "completed": done,
                "failed": {"request": f"{s['method']} {s['path']}", "id": s["id"],
                           "status": exc.status, "body": exc.body},
                "note": ("Writes are not atomic and v1 has no delete tools: the objects in "
                         "'completed' now exist. Fix the cause and re-run (POSTs are idempotent)."),
            }
    return {"ok": True, "completed": done}
