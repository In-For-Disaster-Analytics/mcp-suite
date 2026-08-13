from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import actor  # noqa: E402


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
