"""
DSO Geo MCP Server — entrypoint.

Creates the FastMCP application and registers all geo tools:

  Metadata tools (read-only, no CKAN write):
    gdalinfo_extract      — extract metadata from a single raster resource
    gdalinfo_summary      — extract metadata from all rasters in a dataset

  Transform tools (token-required, CKAN registration via actor ckan block):
    reproject_raster      — gdalwarp to target CRS
    convert_to_cog        — gdal_translate to Cloud-Optimized GeoTIFF
    clip_raster           — gdalwarp -cutline to GeoJSON geometry
    build_overviews       — gdaladdo on a copy (source never mutated)

  Geocoding tools (read-only, synchronous):
    reverse_geocode       — label a single WGS84 point through Nominatim
    reverse_geocode_bbox  — label a bbox or CKAN dataset extent through Nominatim

  Status tool (shared):
    get_execution_status  — poll Tapis Abaco execution; parse actor logs

Usage
-----
Run via the installed script::

    dso-geo-mcp

Or directly::

    python -m dso_geo_mcp.server

Or via uv::

    uv run dso-geo-mcp
"""

from __future__ import annotations

import logging
import sys

import fastmcp

from .config import settings
from .tools import geocode, metadata, status, transform

# Configure logging to stderr so tool results (stdout) are clean.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# FastMCP application
# ------------------------------------------------------------------

mcp = fastmcp.FastMCP(
    "DSO Geo MCP Server",
    instructions=(
        "You are connected to the DSO geospatial processing service. "
        "Use the available tools to reverse-geocode coordinates, extract "
        "metadata from rasters, and run "
        "GDAL transformations on datasets stored on TACC Corral, dispatched "
        "to Tapis Abaco compute actors — no local download required. "
        "\n\n"
        "Typical workflow:\n"
        "1. Use dso-ckan tools to find a dataset and its resource IDs.\n"
        "2. For place labels, call reverse_geocode or reverse_geocode_bbox; "
        "   these return synchronously and do not need a token or polling.\n"
        "3. For raster metadata/transforms, call a geo tool (e.g. "
        "   gdalinfo_extract, reproject_raster) with the "
        "   resource_id and a Tapis JWT as tapis_token.\n"
        "4. Actor-backed tools return an execution_id immediately.\n"
        "5. Poll get_execution_status(execution_id) until status is COMPLETE "
        "   or FAILED.\n"
        "6. When COMPLETE, the result contains metadata or provenance, and "
        "   transform outputs are automatically registered as new CKAN resources.\n"
        "\n"
        "Token requirement:\n"
        "  Metadata tools accept an optional tapis_token (falls back to "
        "  GEO_TAPIS_TOKEN env var for anonymous CKAN reads).\n"
        "  Transform tools REQUIRE an explicit tapis_token — no env fallback.\n"
        "  Never log or return the token.\n"
        "\n"
        "SSRF guard: all CKAN resource URLs are validated to point at the "
        "configured CKAN host before being forwarded to the Abaco actor."
    ),
)

# ------------------------------------------------------------------
# Register tool modules
# ------------------------------------------------------------------

metadata.register(mcp)
geocode.register(mcp)
transform.register(mcp)
status.register(mcp)


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------


def _build_http_app():
    """Build the Starlette ASGI app for HTTP transport, guarded by a shared secret.

    Every request must carry ``Authorization: Bearer <MCP_HTTP_SHARED_SECRET>``; the secret
    is mandatory in HTTP mode (``main`` refuses to start without it), so there is no
    no-auth path here — unlike the CKAN server, because the ``GEO_TAPIS_TOKEN`` env fallback
    grants ambient Abaco compute to any caller that can reach the port.
    """
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.types import ASGIApp, Receive, Scope, Send

    secret = settings.mcp_http_shared_secret

    class SharedSecretMiddleware:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http" or not secret:
                await self.app(scope, receive, send)
                return
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            import hmac

            if not hmac.compare_digest(headers.get("authorization", ""), f"Bearer {secret}"):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
            await self.app(scope, receive, send)

    return mcp.http_app(middleware=[Middleware(SharedSecretMiddleware)])


def main() -> None:
    """Start the MCP server. Transport is stdio by default, or HTTP when
    ``MCP_TRANSPORT=http`` (which requires a shared secret and a loopback bind)."""
    settings.log_startup_banner()
    if settings.mcp_transport == "http":
        if not settings.mcp_http_shared_secret:
            raise SystemExit(
                "MCP_TRANSPORT=http requires MCP_HTTP_SHARED_SECRET to be set — the geo HTTP "
                "endpoint must not run unauthenticated (GEO_TAPIS_TOKEN grants ambient compute)."
            )
        host = settings.mcp_http_host
        if host not in {"127.0.0.1", "::1", "localhost"} and settings.geo_tapis_token:
            raise SystemExit(
                f"Refusing to start: MCP_HTTP_HOST={host!r} is non-loopback while GEO_TAPIS_TOKEN "
                "is set — this exposes ambient Abaco compute. Bind 127.0.0.1 or front with an auth proxy."
            )
        import uvicorn

        logger.info("Serving Geo MCP over HTTP at http://%s:%d/mcp (auth=shared-secret)", host, settings.mcp_http_port)
        uvicorn.run(_build_http_app(), host=host, port=settings.mcp_http_port, log_level="info")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
