"""
actor.py — GDAL Abaco actor entrypoint.

Reads an operation + params from one of three sources (in priority order):
  1. MSG environment variable (Abaco convention for deployed actors)
  2. --message '<json>' CLI argument (local testing)
  3. stdin (pipe-friendly local testing)

Message schema
--------------
{
    "operation":    "gdalinfo" | "reproject" | "cog" | "clip" | "overviews",
    "input_url":    "https://..." | "tapis://system/path",
    "output_name":  "result.tif",         # validated bare filename; ignored for gdalinfo
    "params": {
        "target_crs":      4326,          # reproject only; int 1–999999
        "compression":     "deflate",     # cog only; enum {deflate,lzw,zstd,none}
        "overview_levels": [2, 4, 8],     # overviews only; list[int 2–512], max 10
        "clip_geometry":   {...}          # clip only; GeoJSON dict Polygon/MultiPolygon
    },
    "include_stats": false,               # gdalinfo only; compute band statistics
    "read_token":    "eyJ...",            # OPTIONAL: Tapis JWT for private /vsicurl/ reads
    "ckan": {                             # OPTIONAL: if present, register output to CKAN
        "url":        "https://ckan.example.org",
        "token":      "eyJ...",
        "package_id": "my-dataset",
        "extra":      {}
    }
}

Private /vsicurl/ reads
-----------------------
If ``read_token`` is supplied, it is set as the ``X-Tapis-Token`` HTTP header
for /vsicurl/ requests by configuring ``GDAL_HTTP_HEADER_FILE`` (written to a
temp file) for the GDAL subprocess.  The token is NEVER written to logs or
included in error messages.

Output
------
Emits a single JSON object to stdout:

  On success:
  {
    "status": "ok",
    "operation": "<op>",
    "output_path": "<path>",          # absent for gdalinfo
    "gdal_version": "<ver>",
    "metrics": {"duration_ms": <n>},
    "metadata": {...},                 # gdalinfo only
    "registered": {...}                # if ckan block present and op produced a file
  }

  On error:
  {"status": "error", "message": "<scrubbed message>"}
  exits non-zero.

Security
--------
- All params are validated by validators.py BEFORE any subprocess is launched.
- subprocess.run() is always called with shell=False and a list[str] args.
- GDAL_HTTP_HEADER_FILE is used instead of GDAL_HTTP_HEADERS to avoid token
  exposure in process environment listings.
- Tokens are scrubbed from all error messages and logs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from validators import (
    ALLOWED_OPERATIONS,
    validate_attribute_filter,
    validate_boundary_uri,
    validate_clip_geometry,
    validate_compression,
    validate_crs_wkt,
    validate_field_name,
    validate_grid_uri,
    validate_input_url,
    validate_operation,
    validate_output_name,
    validate_overview_levels,
    validate_pixel_size,
    validate_target_crs,
)

GAM_ALBERS_USFT_CRS = (
    "+proj=aea +lat_0=31.25 +lon_0=-100 +lat_1=27.5 +lat_2=35 "
    "+x_0=1500000 +y_0=6000000 +datum=NAD83 +units=us-ft +no_defs"
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token scrubbing
# ---------------------------------------------------------------------------

_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*")


def _scrub(text: str) -> str:
    """Remove Bearer tokens and JWTs from *text* before logging/returning."""
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED_JWT]", text)
    return text


# ---------------------------------------------------------------------------
# GDAL version detection
# ---------------------------------------------------------------------------


def _gdal_version() -> str:
    try:
        result = subprocess.run(
            ["gdalinfo", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
        return result.stdout.strip().split(",")[0]
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------


def _output_dir() -> Path:
    out = os.environ.get("OUTPUT_DIR", "/data/out")
    p = Path(out)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# /vsicurl/ path builder
# ---------------------------------------------------------------------------


def _vsicurl(url: str) -> str:
    """Prefix *url* with /vsicurl/ for GDAL HTTP range reads."""
    return f"/vsicurl/{url}"


# ---------------------------------------------------------------------------
# Private read auth: write a GDAL_HTTP_HEADER_FILE to a temp file
# ---------------------------------------------------------------------------


def _make_header_file(token: str) -> str:
    """Write a GDAL_HTTP_HEADER_FILE for X-Tapis-Token and return the path.

    Uses a temp file so the token does not appear in the process environment
    listing.  The caller must delete the file when done.
    """
    fd, path = tempfile.mkstemp(suffix=".hdr", prefix="gdal_auth_")
    with os.fdopen(fd, "w") as fh:
        fh.write(f"X-Tapis-Token: {token}\n")
    return path


# ---------------------------------------------------------------------------
# Operation runners (each returns (stdout_text_or_path, extra_env))
# ---------------------------------------------------------------------------


def _run_gdalinfo(
    vsicurl_path: str,
    include_stats: bool,
    extra_env: dict[str, str],
) -> dict[str, Any]:
    """Run gdalinfo -json and return parsed metadata dict."""
    args: list[str] = ["gdalinfo", "-json"]
    if include_stats:
        args.append("-stats")
    args.append(vsicurl_path)

    env = {**os.environ, **extra_env}
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("GDALINFO_TIMEOUT", "120")),
        shell=False,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(_scrub(result.stderr[:500]))
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gdalinfo returned non-JSON output: {result.stdout[:200]}"
        ) from exc


def _run_reproject(
    vsicurl_path: str,
    output_path: Path,
    target_crs: int,
    extra_env: dict[str, str],
) -> None:
    """Run gdalwarp to reproject to *target_crs* (EPSG integer)."""
    crs_flags = validate_target_crs(target_crs)  # ["-t_srs", "EPSG:<n>"]
    args: list[str] = ["gdalwarp"] + crs_flags + [vsicurl_path, str(output_path)]
    env = {**os.environ, **extra_env}
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("REPROJECT_TIMEOUT", "600")),
        shell=False,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(_scrub(result.stderr[:500]))


def _run_cog(
    vsicurl_path: str,
    output_path: Path,
    compression: str,
    extra_env: dict[str, str],
) -> None:
    """Run gdal_translate to produce a Cloud-Optimized GeoTIFF."""
    comp = validate_compression(compression)
    args: list[str] = [
        "gdal_translate",
        "-of", "COG",
        "-co", f"COMPRESS={comp.upper()}",
        vsicurl_path,
        str(output_path),
    ]
    env = {**os.environ, **extra_env}
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("COG_TIMEOUT", "180")),
        shell=False,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(_scrub(result.stderr[:500]))


def _run_clip(
    vsicurl_path: str,
    output_path: Path,
    clip_geometry: dict[str, Any],
    extra_env: dict[str, str],
) -> None:
    """Run gdalwarp -cutline with a temp GeoJSON file for the clip geometry."""
    validated_geom = validate_clip_geometry(clip_geometry)
    # Wrap in a FeatureCollection so gdalwarp can parse it as OGR datasource
    feature_collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": validated_geom,
                "properties": {},
            }
        ],
    }
    # Write to a temp file (server controls the write; never user-supplied path)
    fd, cutline_path = tempfile.mkstemp(suffix=".geojson", prefix="gdal_clip_")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(feature_collection, fh)
        args: list[str] = [
            "gdalwarp",
            "-cutline", cutline_path,
            "-crop_to_cutline",
            vsicurl_path,
            str(output_path),
        ]
        env = {**os.environ, **extra_env}
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("CLIP_TIMEOUT", "300")),
            shell=False,
            env=env,
        )
    finally:
        try:
            os.unlink(cutline_path)
        except OSError:
            pass

    if result.returncode != 0:
        raise RuntimeError(_scrub(result.stderr[:500]))


def _download_request_url(url: str, read_token: str) -> str:
    """Resolve supported input URI schemes to an HTTP URL urllib can read."""
    parsed = urlparse(url)
    if parsed.scheme != "tapis":
        return url
    if not read_token:
        raise RuntimeError("read_token is required for tapis:// input_url downloads")
    base = os.environ.get("TAPIS_BASE_URL", "https://portals.tapis.io").rstrip("/")
    system_id = quote(parsed.netloc, safe="")
    path = quote(parsed.path.lstrip("/"), safe="/")
    return f"{base}/v3/files/content/{system_id}/{path}"


def _download_to_temp(url: str, suffix: str, read_token: str = "") -> str:
    """Download *url* to a temp file and return its path. Caller must os.unlink."""
    import urllib.error as _ue
    import urllib.request as _ur
    headers: dict[str, str] = {}
    if read_token:
        headers["X-Tapis-Token"] = read_token
    request_url = _download_request_url(url, read_token)
    req = _ur.Request(request_url, headers=headers)
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with _ur.urlopen(req, timeout=int(os.environ.get("DOWNLOAD_TIMEOUT", "600"))) as resp:
            with os.fdopen(fd, "wb") as fh:
                fh.write(resp.read())
    except _ue.HTTPError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            body = exc.read().decode(errors="replace")[:300]
        except Exception:
            body = ""
        try:
            os.unlink(path)
        except OSError:
            pass
        raise RuntimeError(
            _scrub(f"download failed for {url}: HTTP {exc.code} {body}")
        ) from exc
    except _ue.URLError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        raise RuntimeError(_scrub(f"download failed for {url}: {exc.reason}")) from exc
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _gma_number(value: Any) -> int | None:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def _arcgis_query_url(layer_uri: str, gma_id: str) -> str:
    """Return an ArcGIS query URL for FeatureServer/MapServer boundary layers."""
    from urllib.parse import parse_qsl, urlencode

    base, _, query = layer_uri.partition("?")
    existing = dict(parse_qsl(query, keep_blank_values=True))
    gma_num = _gma_number(gma_id)
    if gma_num is None and "where" not in existing:
        raise ValueError("gma_id must include a numeric identifier for ArcGIS boundary queries")
    existing.update({
        "where": f"GMAnum={gma_num}" if gma_num is not None else existing["where"],
        "outFields": existing.get("outFields", "*"),
        "returnGeometry": existing.get("returnGeometry", "true"),
        "f": "geojson",
    })
    if not base.rstrip("/").lower().endswith("/query"):
        base = base.rstrip("/") + "/query"
    return base + "?" + urlencode(existing)


def _load_boundary_geojson(
    boundary_geojson: dict[str, Any] | None,
    boundary_uri: str,
    gma_id: str,
    read_token: str,
) -> dict[str, Any]:
    """Return boundary GeoJSON from an inline object or a validated URI."""
    if boundary_geojson is not None and boundary_uri:
        raise ValueError("provide only one of params.boundary_geojson or params.boundary_uri")
    if boundary_geojson is not None:
        if not isinstance(boundary_geojson, dict):
            raise ValueError("params.boundary_geojson must be a GeoJSON object")
        return boundary_geojson
    boundary_uri = validate_boundary_uri(boundary_uri)
    low = boundary_uri.lower()
    fetch_uri = _arcgis_query_url(boundary_uri, gma_id) if "/featureserver/" in low or "/mapserver/" in low else boundary_uri
    tmp = _download_to_temp(fetch_uri, ".geojson", read_token)
    try:
        with open(tmp, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if not isinstance(loaded, dict):
        raise ValueError("boundary_uri did not resolve to a GeoJSON object")
    return loaded


def _run_extract_point(
    input_url: str,
    lat: float,
    lon: float,
    band: int,
    read_token: str,
) -> dict[str, Any]:
    """Sample a raster GeoTIFF at (lon, lat) using rasterio."""
    try:
        import rasterio  # type: ignore
    except ImportError as exc:
        raise RuntimeError("rasterio not installed in this actor image") from exc

    tmp = _download_to_temp(input_url, ".tif", read_token)
    try:
        with rasterio.open(tmp) as ds:
            vals = list(ds.sample([(lon, lat)], indexes=band))
        if not vals:
            raise RuntimeError("point falls outside raster extent")
        return {"value": float(vals[0][0]), "lat": lat, "lon": lon, "band": band}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _boundary_shapes(boundary_geojson: dict[str, Any]) -> list[dict[str, Any]]:
    """Return geometry shapes from a GeoJSON geometry, Feature, or FeatureCollection."""
    kind = boundary_geojson.get("type")
    if kind == "FeatureCollection":
        shapes = [
            feature.get("geometry")
            for feature in boundary_geojson.get("features", [])
            if feature.get("geometry")
        ]
    elif kind == "Feature":
        shapes = [boundary_geojson.get("geometry")]
    elif kind in ("Polygon", "MultiPolygon"):
        shapes = [boundary_geojson]
    else:
        shapes = []
    if not shapes:
        raise ValueError("boundary_geojson must contain at least one Polygon or MultiPolygon geometry")
    return shapes


def _aggregate_raster_path(
    raster_path: str | Path,
    boundary_geojson: dict[str, Any],
    band: int,
    gma_id: str,
) -> dict[str, Any]:
    """Compute the mean raster value within a GMA polygon from a local GeoTIFF."""
    try:
        import numpy as np  # type: ignore
        import rasterio  # type: ignore
        import rasterio.mask  # type: ignore
        import rasterio.warp  # type: ignore
    except ImportError as exc:
        raise RuntimeError("rasterio / numpy not installed in this actor image") from exc

    shapes = _boundary_shapes(boundary_geojson)
    with rasterio.open(str(raster_path)) as ds:
        if ds.crs and ds.crs.to_string() not in ("EPSG:4326", "OGC:CRS84"):
            shapes = [rasterio.warp.transform_geom("EPSG:4326", ds.crs, shape) for shape in shapes]
        out_image, _ = rasterio.mask.mask(
            ds, shapes, crop=True, nodata=np.nan, indexes=band,
        )
        nodata = ds.nodata
    arr = out_image.astype(float)
    if nodata is not None:
        arr[arr == nodata] = np.nan
    valid = arr[~np.isnan(arr)]
    if len(valid) == 0:
        raise RuntimeError("no valid pixels within GMA boundary")
    return {
        "value": float(np.mean(valid)),
        "pixel_count": int(len(valid)),
        "gma_id": gma_id,
        "band": band,
    }


def _run_aggregate_gma(
    input_url: str,
    boundary_geojson: dict[str, Any],
    band: int,
    gma_id: str,
    read_token: str,
) -> dict[str, Any]:
    """Compute the mean raster value within a GMA polygon."""
    tmp = _download_to_temp(input_url, ".tif", read_token)
    try:
        return _aggregate_raster_path(tmp, boundary_geojson, band, gma_id)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _run_extract_budget_gma(
    input_url: str,
    package: str,
    gma_id: str,
    read_token: str,
) -> dict[str, Any]:
    """Sum MODFLOW CBC budget flows for *package* across all active cells."""
    try:
        import flopy.utils  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise RuntimeError("flopy / numpy not installed in this actor image") from exc

    pkg = package.upper()
    _ALLOWED_PKGS = {"DRN", "RIV", "GHB", "WEL", "EVT", "RCH", "CHD", "SFR"}
    if pkg not in _ALLOWED_PKGS:
        raise ValueError(f"package {pkg!r} not allowed; permitted: {sorted(_ALLOWED_PKGS)}")

    tmp = _download_to_temp(input_url, ".cbc", read_token)
    try:
        for precision in ("double", "single"):
            cbf = flopy.utils.CellBudgetFile(tmp, precision=precision)
            records = cbf.get_data(text=pkg)
            if records:
                break
        if not records:
            available = [t.strip() for t in cbf.textlist]
            raise RuntimeError(
                f"package {pkg!r} not found in CBC file; available: {available}"
            )
        last = records[-1]
        if hasattr(last, "q"):
            total = float(np.sum(last.q))
        elif isinstance(last, np.ndarray):
            total = float(np.sum(last))
        else:
            total = float(np.sum(np.array(last)))
        return {
            "value": total,
            "package": pkg,
            "gma_id": gma_id,
            "time_steps_read": len(records),
        }
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _run_extract_satthk_gma(
    input_url: str,
    layer: int,
    gma_id: str,
    read_token: str,
) -> dict[str, Any]:
    """Return mean head for *layer* from a MODFLOW HDS binary file.

    Full saturated-thickness (head minus bottom elevation) requires the DIS
    package geometry, which is not included in the HDS alone.  Pass
    ``params.grid_uri`` for a future extension; currently returns mean head
    as a proxy and includes a note in the response.
    """
    try:
        import flopy.utils  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise RuntimeError("flopy / numpy not installed in this actor image") from exc

    tmp = _download_to_temp(input_url, ".hds", read_token)
    try:
        hf = flopy.utils.HeadFile(tmp)
        all_heads = hf.get_alldata()   # (ntstep, nlay, nrow, ncol)
        layer_data = all_heads[-1, layer - 1]   # last time step, 1-indexed layer
        active = layer_data[layer_data > -1e29]
        if len(active) == 0:
            raise RuntimeError("no active cells in HDS for specified layer")
        return {
            "value": float(np.mean(active)),
            "layer": layer,
            "gma_id": gma_id,
            "note": (
                "mean head returned; sat-thickness (head-botm) requires "
                "grid_uri with DIS geometry — not yet implemented"
            ),
        }
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _run_hds_to_geotiff(
    input_url: str,
    layer: int,
    stress_period: int,
    timestep: int,
    output_path: Path,
    read_token: str,
    dis_geom: dict[str, Any] | None = None,
    crs_wkt: str | None = None,
) -> None:
    """Convert a MODFLOW HDS binary file to a single-band GeoTIFF."""
    try:
        import flopy.utils  # type: ignore
        import numpy as np  # type: ignore
        import rasterio  # type: ignore
    except ImportError as exc:
        raise RuntimeError("flopy / numpy / rasterio not installed in this actor image") from exc

    tmp = _download_to_temp(input_url, ".hds", read_token)
    try:
        try:
            try:
                hf = flopy.utils.HeadFile(tmp)
                kstpkper = (timestep - 1, stress_period - 1)  # flopy uses 0-indexed
                try:
                    head = hf.get_data(kstpkper=kstpkper)
                except Exception:
                    head = hf.get_alldata()[-1]
                layer_data = head[layer - 1].astype(np.float32)
            except Exception as flopy_exc:
                try:
                    layer_data = _read_single_record_hds_like(tmp, np)
                except Exception:
                    raise flopy_exc
            if dis_geom is not None and layer_data.shape != (dis_geom["nrow"], dis_geom["ncol"]):
                raise RuntimeError(
                    "HDS dimensions "
                    f"({layer_data.shape[0]}x{layer_data.shape[1]}) do not match DIS grid "
                    f"({dis_geom['nrow']}x{dis_geom['ncol']}). Ensure grid_uri points to "
                    "the DIS package matching this HDS output."
                )
            layer_data[layer_data < -1e29] = np.nan   # mask HDRY / inactive
            nrow, ncol = layer_data.shape
            if dis_geom is not None:
                transform = _dis_affine_transform(dis_geom, np, rasterio)
                crs = crs_wkt or GAM_ALBERS_USFT_CRS
            else:
                # Pixel-space fallback for backward compatibility when no grid_uri is supplied.
                transform = rasterio.transform.from_bounds(0, 0, ncol, nrow, ncol, nrow)
                crs = "EPSG:4326"
            with rasterio.open(
                str(output_path), "w",
                driver="GTiff", height=nrow, width=ncol,
                count=1, dtype=np.float32,
                crs=crs,
                transform=transform,
                nodata=np.nan,
            ) as dst:
                dst.write(layer_data, 1)
        except Exception as exc:
            raise RuntimeError(
                f"failed to parse or convert HDS input {input_url}: {type(exc).__name__}: {exc}"
            ) from exc
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _download_and_parse_dis(grid_uri: str, read_token: str) -> dict[str, Any]:
    tmp = _download_to_temp(grid_uri, ".dis", read_token)
    try:
        return _parse_dis_top(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _dis_affine_transform(dis_geom: dict[str, Any], np_module: Any, rasterio_module: Any) -> Any:
    dx0, dy0 = float(dis_geom["delr"][0]), float(dis_geom["delc"][0])
    if not (np_module.allclose(dis_geom["delr"], dx0) and np_module.allclose(dis_geom["delc"], dy0)):
        raise RuntimeError(
            "DIS grid has non-uniform cell spacing; a single GeoTIFF affine "
            "transform cannot represent a variable-spacing grid"
        )
    theta = np_module.radians(dis_geom["angrot"])
    ct, st = np_module.cos(theta), np_module.sin(theta)
    length_y = float(dis_geom["delc"].sum())
    a, b = ct * dx0, st * dy0
    d, e = st * dx0, -ct * dy0
    c = dis_geom["xoff"] - st * length_y
    f = dis_geom["yoff"] + ct * length_y
    return rasterio_module.transform.Affine(a, b, c, d, e, f)


def _read_single_record_hds_like(path: str, np_module: Any) -> Any:
    """Read a CKAN-style single-record HDS-like raster missing the ``ilay`` field.

    Standard MODFLOW binary head records include ``kstp, kper, pertim, totim,
    text, ncol, nrow, ilay`` before the float grid. The NTGAM CKAN demo HDS
    resource currently stores one already-extracted layer as
    ``kstp, kper, pertim, totim, text, ncol, nrow`` plus ``nrow*ncol`` float32
    values. FloPy treats that as EOF because the 4-byte ``ilay`` field is
    absent, so this fallback accepts only that exact one-record shape.
    """
    size = os.path.getsize(path)
    if size < 40:
        raise ValueError("single-record HDS-like file is smaller than 40-byte header")
    with open(path, "rb") as fh:
        header = fh.read(40)
    try:
        _kstp, _kper, _pertim, _totim, text, ncol, nrow = struct.unpack("<iiff16sii", header)
    except struct.error as exc:
        raise ValueError("single-record HDS-like header could not be unpacked") from exc
    text_value = text.decode("ascii", errors="replace").strip()
    if not text_value:
        raise ValueError("single-record HDS-like header has empty text label")
    if nrow <= 0 or ncol <= 0 or nrow > 100000 or ncol > 100000:
        raise ValueError(f"single-record HDS-like dimensions are invalid: {nrow}x{ncol}")
    expected = 40 + (nrow * ncol * 4)
    if size != expected:
        raise ValueError(f"single-record HDS-like size mismatch: expected {expected} bytes, got {size}")
    data = np_module.fromfile(path, dtype="<f4", offset=40)
    if data.size != nrow * ncol:
        raise ValueError(f"single-record HDS-like data size mismatch: expected {nrow * ncol}, got {data.size}")
    return data.reshape((nrow, ncol)).astype(np_module.float32)


def _run_hds_aggregate_gma(
    input_url: str,
    boundary_geojson: dict[str, Any],
    layer: int,
    stress_period: int,
    timestep: int,
    band: int,
    gma_id: str,
    read_token: str,
    grid_uri: str = "",
    crs_wkt: str | None = None,
) -> dict[str, Any]:
    """Convert MODFLOW HDS to a temporary GeoTIFF and aggregate it in one actor run."""
    fd, tmp_tif = tempfile.mkstemp(suffix=".tif", prefix="hds_gma_")
    os.close(fd)
    try:
        dis_geom = _download_and_parse_dis(grid_uri, read_token) if grid_uri else None
        _run_hds_to_geotiff(
            input_url,
            layer,
            stress_period,
            timestep,
            Path(tmp_tif),
            read_token,
            dis_geom,
            crs_wkt,
        )
        result = _aggregate_raster_path(tmp_tif, boundary_geojson, band, gma_id)
        result.update({
            "source_format": "hds",
            "layer": layer,
            "stress_period": stress_period,
            "timestep": timestep,
        })
        return result
    finally:
        try:
            os.unlink(tmp_tif)
        except OSError:
            pass


def _run_rasterize_points(
    input_url: str,
    output_path: Path,
    value_field: str,
    pixel_size: float,
    attribute_filter: str | None,
    layer_name: str | None,
    read_token: str,
) -> None:
    """Rasterize a numeric field from a point/polygon vector layer (e.g. a
    GeoParquet of per-model-cell values) onto a regular grid via
    gdal_rasterize, preserving the source layer's CRS. ``attribute_filter``
    (a single "<field> = <number>" equality) lets a stacked multi-layer
    dataset be split by e.g. MODFLOW layer without a separate extract step.
    """
    suffix = Path(urlparse(input_url).path).suffix or ".parquet"
    tmp = _download_to_temp(input_url, suffix, read_token)
    try:
        args: list[str] = [
            "gdal_rasterize", "-a", value_field,
            "-tr", str(pixel_size), str(pixel_size),
            "-a_nodata", "-9999", "-init", "-9999",
        ]
        if attribute_filter:
            args += ["-where", attribute_filter]
        if layer_name:
            args += ["-l", layer_name]
        args += [tmp, str(output_path)]
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("RASTERIZE_TIMEOUT", "300")),
            shell=False,
        )
        if result.returncode != 0:
            raise RuntimeError(_scrub(result.stderr[:500]))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# MODFLOW 6 text DIS package block names that carry a NROW*NCOL (or NCOL/NROW)
# INTERNAL-FACTOR numeric array, in the shape flopy's DIS writer emits them.
_DIS_BLOCK_RE = re.compile(
    r"^  (delr|delc|top)\b.*?\n(.*?)(?=^  \w+\s*(?:LAYERED)?\s*\n|^END griddata)",
    re.MULTILINE | re.DOTALL,
)


def _parse_dis_top(path: str) -> dict[str, Any]:
    """Parse a MODFLOW 6 text DIS file's grid geometry + `top` array.

    Deliberately does NOT use flopy.discretization.StructuredGrid for the
    coordinate transform: that class's get_coords() silently drops the
    xoff/yoff translation for arrays at real-model size (confirmed with a
    1124x1412 grid — reproduces with synthetic data of the same shape, so it
    is a library issue, not a data issue). The grid geometry values
    (xorigin/yorigin/angrot/delr/delc/top) are still read from the file with
    a plain, independently-testable parser; the affine transform is built by
    the caller from MODFLOW's documented rotation convention directly.
    """
    text = Path(path).read_text()

    def _opt(name: str) -> float | None:
        m = re.search(rf"^\s*{name}\s+([-\d.eE+]+)", text, re.MULTILINE | re.IGNORECASE)
        return float(m.group(1)) if m else None

    def _dim(name: str) -> int:
        m = re.search(rf"^\s*{name}\s+(\d+)", text, re.MULTILINE | re.IGNORECASE)
        if not m:
            raise RuntimeError(f"DIS file missing dimension {name}")
        return int(m.group(1))

    xoff = _opt("XORIGIN") or 0.0
    yoff = _opt("YORIGIN") or 0.0
    angrot = _opt("ANGROT") or 0.0
    nrow = _dim("NROW")
    ncol = _dim("NCOL")

    grid_start = text.index("BEGIN griddata")
    # Keep the "END griddata" marker IN the slice: _DIS_BLOCK_RE's lookahead
    # needs it to terminate whichever block happens to be last (e.g. a file
    # with no botm/idomain after top) — excluding it left the last block
    # unmatched.
    grid_end = text.index("END griddata") + len("END griddata")
    grid_text = text[grid_start:grid_end]

    blocks: dict[str, str] = {}
    for m in _DIS_BLOCK_RE.finditer(grid_text):
        blocks[m.group(1)] = m.group(2)
    for required in ("delr", "delc", "top"):
        if required not in blocks:
            raise RuntimeError(f"DIS file missing griddata block: {required}")

    def _parse_internal(block: str, count: int) -> "np.ndarray":
        import numpy as np
        lines = block.strip().split("\n")
        header = lines[0].strip()
        hm = re.match(r"INTERNAL\s+FACTOR\s+([-\d.eE+]+)", header, re.IGNORECASE)
        if not hm:
            raise RuntimeError(f"expected INTERNAL FACTOR array, got: {header!r}")
        factor = float(hm.group(1))
        values = " ".join(lines[1:]).split()
        if len(values) < count:
            raise RuntimeError(
                f"DIS array short: expected {count} values, found {len(values)}"
            )
        return np.array(values[:count], dtype=np.float64) * factor

    delr = _parse_internal(blocks["delr"], ncol)
    delc = _parse_internal(blocks["delc"], nrow)
    top = _parse_internal(blocks["top"], nrow * ncol).reshape(nrow, ncol)

    return {
        "xoff": xoff, "yoff": yoff, "angrot": angrot,
        "nrow": nrow, "ncol": ncol, "delr": delr, "delc": delc, "top": top,
    }


def _run_dis_top_to_geotiff(
    input_url: str,
    output_path: Path,
    crs_wkt: str | None,
    read_token: str,
) -> None:
    """Extract the land-surface `top` array from a MODFLOW 6 text DIS package
    and write it as a rotation-aware georeferenced GeoTIFF.

    The affine transform follows MODFLOW's documented grid convention
    directly (row 0 / col 0 at the model's upper-left, y decreasing downward
    in the unrotated local frame, then rotated by ANGROT degrees
    counterclockwise about (XORIGIN, YORIGIN) and translated) — see
    _parse_dis_top's docstring for why this is not delegated to flopy.
    """
    try:
        import numpy as np
        import rasterio
    except ImportError as exc:
        raise RuntimeError("numpy / rasterio not installed in this actor image") from exc

    tmp = _download_to_temp(input_url, ".dis", read_token)
    try:
        geom = _parse_dis_top(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    theta = np.radians(geom["angrot"])
    ct, st = np.cos(theta), np.sin(theta)
    dx0, dy0 = float(geom["delr"][0]), float(geom["delc"][0])
    if not (np.allclose(geom["delr"], dx0) and np.allclose(geom["delc"], dy0)):
        raise RuntimeError(
            "DIS grid has non-uniform cell spacing; a single GeoTIFF affine "
            "transform cannot represent a variable-spacing grid"
        )
    length_y = float(geom["delc"].sum())
    a, b = ct * dx0, st * dy0
    d, e = st * dx0, -ct * dy0
    c = geom["xoff"] - st * length_y
    f = geom["yoff"] + ct * length_y
    transform = rasterio.transform.Affine(a, b, c, d, e, f)

    top = geom["top"].astype(np.float32)
    top[top <= -9999] = np.nan

    kwargs: dict[str, Any] = dict(
        driver="GTiff", height=geom["nrow"], width=geom["ncol"],
        count=1, dtype=np.float32, transform=transform, nodata=np.nan,
    )
    if crs_wkt:
        kwargs["crs"] = crs_wkt
    with rasterio.open(str(output_path), "w", **kwargs) as dst:
        dst.write(top, 1)


def _run_overviews(
    vsicurl_path: str,
    output_path: Path,
    overview_levels: list[int],
    extra_env: dict[str, str],
) -> None:
    """Copy source via gdal_translate, then build overviews on the copy.

    The source is NEVER mutated (Decision 8 / copy-not-mutate contract).
    """
    levels = validate_overview_levels(overview_levels)
    env = {**os.environ, **extra_env}

    # Step 1: copy source to output_path
    copy_args: list[str] = ["gdal_translate", vsicurl_path, str(output_path)]
    copy_result = subprocess.run(
        copy_args,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("OVERVIEWS_COPY_TIMEOUT", "180")),
        shell=False,
        env=env,
    )
    if copy_result.returncode != 0:
        raise RuntimeError(
            f"gdal_translate (copy) failed: {_scrub(copy_result.stderr[:500])}"
        )

    # Step 2: build overviews on the copy (never on vsicurl_path)
    levels_str = [str(lvl) for lvl in levels]
    addo_args: list[str] = ["gdaladdo", str(output_path)] + levels_str
    addo_result = subprocess.run(
        addo_args,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("OVERVIEWS_ADDO_TIMEOUT", "300")),
        shell=False,
        env=env,
    )
    if addo_result.returncode != 0:
        raise RuntimeError(
            f"gdaladdo failed: {_scrub(addo_result.stderr[:500])}"
        )


# ---------------------------------------------------------------------------
# Message reader
# ---------------------------------------------------------------------------


def _read_message() -> dict[str, Any]:
    """Read the JSON message from MSG env var, --message arg, or stdin."""
    # Priority 1: Abaco MSG env var
    msg_env = os.environ.get("MSG", "")
    if msg_env:
        try:
            return json.loads(msg_env)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MSG env var is not valid JSON: {exc}") from exc

    # Priority 2: --message CLI arg
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--message", "-m", default=None)
    args, _ = parser.parse_known_args()
    if args.message:
        try:
            return json.loads(args.message)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--message is not valid JSON: {exc}") from exc

    # Priority 3: stdin
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("No message provided via MSG env, --message, or stdin")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stdin is not valid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    t0 = time.monotonic()

    # --- Parse message ---
    try:
        msg = _read_message()
    except ValueError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)

    # --- Validate operation first (before any params) ---
    try:
        op = validate_operation(msg.get("operation", ""))
    except ValueError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)

    # --- Validate input_url ---
    try:
        input_url = validate_input_url(msg.get("input_url", ""))
    except ValueError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)

    # --- output_name (not required for gdalinfo) ---
    output_name_raw = msg.get("output_name", "")
    params = msg.get("params") or {}
    include_stats = bool(msg.get("include_stats", False))

    # --- read_token for private /vsicurl/ reads ---
    read_token: str = msg.get("read_token") or ""

    # --- ckan registration block (optional) ---
    ckan_block: dict[str, Any] | None = msg.get("ckan")

    gdal_ver = _gdal_version()
    vsicurl_path = _vsicurl(input_url)
    response: dict[str, Any] = {
        "status": "ok",
        "operation": op,
        "gdal_version": gdal_ver,
        "metrics": {},
    }

    # --- Build extra subprocess env for private reads ---
    extra_env: dict[str, str] = {}
    header_file_path: str = ""
    if read_token:
        header_file_path = _make_header_file(read_token)
        extra_env["GDAL_HTTP_HEADER_FILE"] = header_file_path

    try:
        # -----------------------------------------------------------------
        # Execute the validated operation
        # -----------------------------------------------------------------
        metadata_result: dict[str, Any] | None = None
        output_path: Path | None = None

        if op == "gdalinfo":
            metadata_result = _run_gdalinfo(vsicurl_path, include_stats, extra_env)

        elif op == "extract_point":
            lat = float(params.get("lat", 0))
            lon = float(params.get("lon", 0))
            band = int(params.get("band", 1))
            try:
                response.update(_run_extract_point(input_url, lat, lon, band, read_token))
            except (RuntimeError, ValueError) as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)

        elif op == "aggregate_gma":
            boundary = params.get("boundary_geojson")
            boundary_uri = str(params.get("boundary_uri") or "")
            if boundary is None and not boundary_uri:
                print(json.dumps({"status": "error",
                                  "message": "params.boundary_geojson or params.boundary_uri required for aggregate_gma"}))
                sys.exit(1)
            band = int(params.get("band", 1))
            gma_id = str(params.get("gma_id", ""))
            try:
                boundary = _load_boundary_geojson(boundary, boundary_uri, gma_id, read_token)
                response.update(_run_aggregate_gma(input_url, boundary, band, gma_id, read_token))
            except (RuntimeError, ValueError) as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)

        elif op == "hds_aggregate_gma":
            boundary = params.get("boundary_geojson")
            boundary_uri = str(params.get("boundary_uri") or "")
            if boundary is None and not boundary_uri:
                print(json.dumps({"status": "error",
                                  "message": "params.boundary_geojson or params.boundary_uri required for hds_aggregate_gma"}))
                sys.exit(1)
            layer = int(params.get("layer", 1))
            sp = int(params.get("stress_period", 1))
            ts = int(params.get("timestep", 1))
            band = int(params.get("band", 1))
            gma_id = str(params.get("gma_id", ""))
            grid_uri = str(params.get("grid_uri") or "")
            crs_wkt = params.get("crs_wkt")
            try:
                if grid_uri:
                    grid_uri = validate_grid_uri(grid_uri)
                if crs_wkt is not None:
                    crs_wkt = validate_crs_wkt(crs_wkt)
                boundary = _load_boundary_geojson(boundary, boundary_uri, gma_id, read_token)
                response.update(_run_hds_aggregate_gma(
                    input_url, boundary, layer, sp, ts, band, gma_id, read_token, grid_uri, crs_wkt,
                ))
            except (RuntimeError, ValueError) as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)

        elif op == "extract_budget_gma":
            package = str(params.get("package", "DRN"))
            gma_id = str(params.get("gma_id", ""))
            try:
                response.update(_run_extract_budget_gma(input_url, package, gma_id, read_token))
            except (RuntimeError, ValueError) as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)

        elif op == "extract_satthk_gma":
            layer = int(params.get("layer", 1))
            gma_id = str(params.get("gma_id", ""))
            try:
                response.update(_run_extract_satthk_gma(input_url, layer, gma_id, read_token))
            except (RuntimeError, ValueError) as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)

        elif op == "hds_to_geotiff":
            try:
                out_name = validate_output_name(output_name_raw or "head_output.tif")
            except ValueError as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)
            output_path = _output_dir() / out_name
            layer = int(params.get("layer", 1))
            sp = int(params.get("stress_period", 1))
            ts = int(params.get("timestep", 1))
            try:
                _run_hds_to_geotiff(input_url, layer, sp, ts, output_path, read_token)
            except (RuntimeError, ValueError) as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)

        elif op == "rasterize_points":
            try:
                out_name = validate_output_name(output_name_raw or "rasterized.tif")
                value_field = validate_field_name(params.get("value_field", ""))
                pixel_size = validate_pixel_size(params.get("pixel_size"))
                attribute_filter = params.get("attribute_filter")
                if attribute_filter is not None:
                    attribute_filter = validate_attribute_filter(attribute_filter)
                layer_name = params.get("layer_name")
                if layer_name is not None:
                    layer_name = validate_field_name(layer_name)
            except ValueError as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)
            output_path = _output_dir() / out_name
            try:
                _run_rasterize_points(
                    input_url, output_path, value_field, pixel_size,
                    attribute_filter, layer_name, read_token,
                )
            except (RuntimeError, ValueError) as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)

        elif op == "dis_top_to_geotiff":
            try:
                out_name = validate_output_name(output_name_raw or "dis_top.tif")
                crs_wkt = params.get("crs_wkt")
                if crs_wkt is not None:
                    crs_wkt = validate_crs_wkt(crs_wkt)
            except ValueError as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)
            output_path = _output_dir() / out_name
            try:
                _run_dis_top_to_geotiff(input_url, output_path, crs_wkt, read_token)
            except (RuntimeError, ValueError) as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)

        else:
            # All other ops produce an output file — validate output_name
            try:
                out_name = validate_output_name(output_name_raw)
            except ValueError as exc:
                print(json.dumps({"status": "error", "message": str(exc)}))
                sys.exit(1)
            output_path = _output_dir() / out_name

            if op == "reproject":
                target_crs = params.get("target_crs")
                if target_crs is None:
                    print(json.dumps({"status": "error", "message": "params.target_crs is required for reproject"}))
                    sys.exit(1)
                try:
                    _run_reproject(vsicurl_path, output_path, target_crs, extra_env)
                except ValueError as exc:
                    print(json.dumps({"status": "error", "message": str(exc)}))
                    sys.exit(1)

            elif op == "cog":
                compression = params.get("compression", "deflate")
                try:
                    _run_cog(vsicurl_path, output_path, compression, extra_env)
                except ValueError as exc:
                    print(json.dumps({"status": "error", "message": str(exc)}))
                    sys.exit(1)

            elif op == "clip":
                clip_geom = params.get("clip_geometry")
                if clip_geom is None:
                    print(json.dumps({"status": "error", "message": "params.clip_geometry is required for clip"}))
                    sys.exit(1)
                try:
                    _run_clip(vsicurl_path, output_path, clip_geom, extra_env)
                except ValueError as exc:
                    print(json.dumps({"status": "error", "message": str(exc)}))
                    sys.exit(1)

            elif op == "overviews":
                levels = params.get("overview_levels", [2, 4, 8])
                try:
                    _run_overviews(vsicurl_path, output_path, levels, extra_env)
                except ValueError as exc:
                    print(json.dumps({"status": "error", "message": str(exc)}))
                    sys.exit(1)

    except RuntimeError as exc:
        print(json.dumps({"status": "error", "message": _scrub(str(exc))}))
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(json.dumps({"status": "error", "message": f"GDAL operation timed out: {op}"}))
        sys.exit(1)
    finally:
        # Always clean up the auth header file
        if header_file_path:
            try:
                os.unlink(header_file_path)
            except OSError:
                pass

    duration_ms = int((time.monotonic() - t0) * 1000)

    # -----------------------------------------------------------------
    # Compose success response
    # -----------------------------------------------------------------
    response["metrics"] = {"duration_ms": duration_ms}
    if output_path is not None:
        response["output_path"] = str(output_path)
    if metadata_result is not None:
        response["metadata"] = metadata_result

    # -----------------------------------------------------------------
    # Optional CKAN registration (gdal+register mode)
    # -----------------------------------------------------------------
    if ckan_block and output_path is not None and output_path.exists():
        from register_to_ckan import register as ckan_register, _scrub as _ckan_scrub

        ckan_url = ckan_block.get("url", "")
        ckan_token = ckan_block.get("token", "")
        package_id = ckan_block.get("package_id", "")
        extra = ckan_block.get("extra") or {}

        if not ckan_url or not ckan_token or not package_id:
            response["registered"] = {
                "status": "error",
                "message": "ckan block missing url, token, or package_id",
            }
        else:
            try:
                reg_result = ckan_register(
                    output_path=str(output_path),
                    ckan_url=ckan_url,
                    token=ckan_token,
                    package_id=package_id,
                    name=output_path.name,
                    extra_metadata=extra,
                )
                response["registered"] = {"status": "ok", "resource": reg_result}
            except Exception as exc:
                response["registered"] = {
                    "status": "error",
                    "message": _scrub(str(exc)),
                }

    print(json.dumps(response))


if __name__ == "__main__":
    main()
