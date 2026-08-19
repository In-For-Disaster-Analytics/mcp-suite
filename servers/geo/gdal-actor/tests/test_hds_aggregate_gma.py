from __future__ import annotations

import os
import sys
import json
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import actor  # noqa: E402
from validators import validate_boundary_uri  # noqa: E402


def test_validate_boundary_uri_blocks_unsupported_schemes():
    try:
        validate_boundary_uri("file:///etc/passwd")
    except ValueError as exc:
        assert "boundary_uri must use" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_boundary_uri_honors_allowed_boundary_hosts(monkeypatch):
    monkeypatch.setenv("ALLOWED_BOUNDARY_HOSTS", "*.arcgis.com,example.org")

    assert validate_boundary_uri("https://services.arcgis.com/demo/layer")
    try:
        validate_boundary_uri("https://not-allowed.test/boundary.geojson")
    except ValueError as exc:
        assert "not permitted" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_load_boundary_geojson_accepts_inline_boundary():
    boundary = {"type": "Polygon", "coordinates": [[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]]}

    assert actor._load_boundary_geojson(boundary, "", "GMA 12", "token") is boundary


def test_load_boundary_geojson_downloads_boundary_uri(monkeypatch, tmp_path):
    boundary_path = tmp_path / "boundary.geojson"
    boundary_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    calls = {}

    def fake_download(url, suffix, read_token=""):
        calls["url"] = url
        calls["suffix"] = suffix
        calls["read_token"] = read_token
        return str(boundary_path)

    monkeypatch.setattr(actor, "_download_to_temp", fake_download)

    result = actor._load_boundary_geojson(
        None,
        "https://services.arcgis.com/demo/arcgis/rest/services/GMA/FeatureServer/4",
        "GMA 12",
        "token",
    )

    assert result["type"] == "FeatureCollection"
    assert calls["suffix"] == ".geojson"
    assert calls["read_token"] == "token"
    assert calls["url"].endswith("/query?where=GMAnum%3D12&outFields=%2A&returnGeometry=true&f=geojson")


def test_arcgis_boundary_query_requires_numeric_gma_id():
    try:
        actor._arcgis_query_url(
            "https://services.arcgis.com/demo/arcgis/rest/services/GMA/FeatureServer/4",
            "GMA",
        )
    except ValueError as exc:
        assert "numeric identifier" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_load_boundary_geojson_rejects_both_inline_and_uri():
    boundary = {"type": "FeatureCollection", "features": []}

    try:
        actor._load_boundary_geojson(boundary, "https://example.com/boundary.geojson", "GMA 12", "token")
    except ValueError as exc:
        assert "provide only one" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_main_aggregate_gma_boundary_uri_returns_json(monkeypatch, tmp_path, capsys):
    boundary_path = tmp_path / "boundary.geojson"
    boundary_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

    monkeypatch.setenv("MSG", json.dumps({
        "operation": "aggregate_gma",
        "input_url": "https://example.com/raster.tif",
        "read_token": "token",
        "params": {
            "boundary_uri": "https://services.arcgis.com/demo/arcgis/rest/services/GMA/FeatureServer/4",
            "gma_id": "GMA 12",
            "band": 1,
        },
    }))
    monkeypatch.setattr(actor, "_gdal_version", lambda: "GDAL test")
    monkeypatch.setattr(actor, "_download_to_temp", lambda url, suffix, read_token="": str(boundary_path))
    monkeypatch.setattr(
        actor,
        "_run_aggregate_gma",
        lambda input_url, boundary, band, gma_id, read_token: {
            "value": 42.0,
            "pixel_count": 5,
            "gma_id": gma_id,
            "band": band,
        },
    )

    actor.main()

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["operation"] == "aggregate_gma"
    assert out["value"] == 42.0
    assert out["metrics"]["duration_ms"] >= 0


def test_tapis_uri_resolves_to_files_content_endpoint(monkeypatch):
    monkeypatch.setenv("TAPIS_BASE_URL", "https://portals.tapis.io")

    url = actor._download_request_url(
        "tapis://ls6/modflow/demo/ntgam-v301/heads.hds",
        read_token="token",
    )

    assert url == "https://portals.tapis.io/v3/files/content/ls6/modflow/demo/ntgam-v301/heads.hds"


def test_tapis_uri_download_requires_read_token():
    try:
        actor._download_request_url("tapis://ls6/modflow/demo/heads.hds", read_token="")
    except RuntimeError as exc:
        assert "read_token is required" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_hds_aggregate_gma_keeps_conversion_and_aggregation_in_one_actor_run(monkeypatch):
    calls = {}

    def fake_hds_to_geotiff(input_url, layer, stress_period, timestep, output_path, read_token):
        calls["convert"] = {
            "input_url": input_url,
            "layer": layer,
            "stress_period": stress_period,
            "timestep": timestep,
            "output_path": Path(output_path),
            "read_token": read_token,
        }
        Path(output_path).write_text("synthetic geotiff")

    def fake_aggregate_raster_path(raster_path, boundary_geojson, band, gma_id):
        calls["aggregate"] = {
            "raster_path": Path(raster_path),
            "boundary_geojson": boundary_geojson,
            "band": band,
            "gma_id": gma_id,
            "exists_during_aggregate": Path(raster_path).exists(),
        }
        return {"value": 42.0, "pixel_count": 5, "gma_id": gma_id, "band": band}

    monkeypatch.setattr(actor, "_run_hds_to_geotiff", fake_hds_to_geotiff)
    monkeypatch.setattr(actor, "_aggregate_raster_path", fake_aggregate_raster_path)
    boundary = {"type": "Polygon", "coordinates": [[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]]}

    result = actor._run_hds_aggregate_gma(
        "https://example.com/heads.hds",
        boundary,
        layer=2,
        stress_period=3,
        timestep=4,
        band=1,
        gma_id="GMA 12",
        read_token="token",
    )

    assert calls["convert"]["input_url"] == "https://example.com/heads.hds"
    assert calls["convert"]["layer"] == 2
    assert calls["convert"]["stress_period"] == 3
    assert calls["convert"]["timestep"] == 4
    assert calls["aggregate"]["raster_path"] == calls["convert"]["output_path"]
    assert calls["aggregate"]["exists_during_aggregate"] is True
    assert calls["aggregate"]["gma_id"] == "GMA 12"
    assert calls["convert"]["output_path"].exists() is False
    assert result["value"] == 42.0
    assert result["source_format"] == "hds"
    assert result["layer"] == 2
