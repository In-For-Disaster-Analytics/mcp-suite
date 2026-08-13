"""
Unit tests for the two new gdal-actor operations added to close the MINT
svo-adapter SUBSIDE-forecast completeness gap:

- dis_top_to_geotiff: parse a MODFLOW 6 text DIS package's `top` array +
  grid geometry, write a rotation-aware georeferenced GeoTIFF.
- rasterize_points: rasterize a numeric field from a point/polygon vector
  layer (e.g. a GeoParquet of per-model-cell values) via gdal_rasterize.

Uses small synthetic fixtures (not the real ~190MB NTGAM files) so this runs
fast with no network access. The logic itself was independently verified
against the real downloaded ntgam.dis / ntgam_storativity.parquet files
(cross-checked the resulting raster extents against each other and against
the dataset's documented Dallas-Fort Worth location) before being committed.

numpy/rasterio are optional heavy deps (only present in the actor's Docker
image, per gdal-actor/Dockerfile pins) — skipped gracefully if absent.

Run with:
    python3 -m pytest tests/test_new_ops.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")

import actor  # noqa: E402

_HAS_GDAL_RASTERIZE = subprocess.run(
    ["which", "gdal_rasterize"], capture_output=True
).returncode == 0


# ===========================================================================
# _parse_dis_top
# ===========================================================================

# A tiny 3-row x 4-col, 2-layer synthetic DIS file in the exact shape flopy's
# MF6 text writer emits (verified against the real ntgam.dis format).
_SYNTHETIC_DIS = textwrap.dedent("""\
    # synthetic test fixture
    BEGIN options
      LENGTH_UNITS  feet
      XORIGIN  1000.0
      YORIGIN  2000.0
      ANGROT  30.0
    END options

    BEGIN dimensions
      NLAY  2
      NROW  3
      NCOL  4
    END dimensions

    BEGIN griddata
      delr
        INTERNAL  FACTOR  1.0
          10.0  10.0  10.0  10.0
      delc
        INTERNAL  FACTOR  1.0
          10.0  10.0  10.0
      top
        INTERNAL  FACTOR  1.0
          100.0  101.0  102.0  103.0
          104.0  -9999.00000000  106.0  107.0
          108.0  109.0  110.0  111.0
      botm  LAYERED
        OPEN/CLOSE  'Bot1.ref'  FACTOR  1.0
        OPEN/CLOSE  'Bot2.ref'  FACTOR  1.0
      idomain  LAYERED
        OPEN/CLOSE  'Id1.ref'  FACTOR  1
        OPEN/CLOSE  'Id2.ref'  FACTOR  1
    END griddata
    """)


@pytest.fixture
def synthetic_dis_path(tmp_path):
    p = tmp_path / "test.dis"
    p.write_text(_SYNTHETIC_DIS)
    return str(p)


class TestParseDisTop:
    def test_parses_options_and_dimensions(self, synthetic_dis_path):
        geom = actor._parse_dis_top(synthetic_dis_path)
        assert geom["xoff"] == 1000.0
        assert geom["yoff"] == 2000.0
        assert geom["angrot"] == 30.0
        assert geom["nrow"] == 3
        assert geom["ncol"] == 4

    def test_parses_delr_delc(self, synthetic_dis_path):
        geom = actor._parse_dis_top(synthetic_dis_path)
        assert list(geom["delr"]) == [10.0, 10.0, 10.0, 10.0]
        assert list(geom["delc"]) == [10.0, 10.0, 10.0]

    def test_parses_top_array_shape_and_values(self, synthetic_dis_path):
        geom = actor._parse_dis_top(synthetic_dis_path)
        top = geom["top"]
        assert top.shape == (3, 4)
        assert top[0, 0] == 100.0
        assert top[2, 3] == 111.0
        assert top[1, 1] == -9999.0  # nodata sentinel preserved, not yet masked

    def test_ignores_botm_and_idomain(self, synthetic_dis_path):
        """botm/idomain use external OPEN/CLOSE .ref files this parser never
        touches — dis_top_to_geotiff only needs `top`, per the design note in
        the real CKAN dataset (Bot*.ref arrays are documented as ragged)."""
        geom = actor._parse_dis_top(synthetic_dis_path)
        assert "botm" not in geom
        assert "idomain" not in geom

    def test_missing_dimension_raises(self, tmp_path):
        bad = tmp_path / "bad.dis"
        bad.write_text("BEGIN options\nEND options\nBEGIN dimensions\n  NLAY 1\nEND dimensions\n")
        with pytest.raises(RuntimeError, match="NROW"):
            actor._parse_dis_top(str(bad))

    def test_missing_griddata_block_raises(self, tmp_path):
        bad = tmp_path / "bad.dis"
        bad.write_text(textwrap.dedent("""\
            BEGIN dimensions
              NLAY  1
              NROW  2
              NCOL  2
            END dimensions
            BEGIN griddata
              delr
                INTERNAL  FACTOR  1.0
                  10.0  10.0
            END griddata
            """))
        with pytest.raises(RuntimeError, match="delc"):
            actor._parse_dis_top(str(bad))


# ===========================================================================
# _run_dis_top_to_geotiff — affine transform + GeoTIFF write
# ===========================================================================


class TestRunDisTopToGeotiff:
    def test_writes_georeferenced_geotiff_with_rotation(self, tmp_path, synthetic_dis_path, monkeypatch):
        """End-to-end (minus the network download): patch _download_to_temp
        to hand back the synthetic fixture, then verify the written GeoTIFF's
        shape, nodata masking, and that rotation actually moved the origin
        (i.e. this is not silently falling back to a north-up transform)."""
        monkeypatch.setattr(actor, "_download_to_temp", lambda url, suffix, token: synthetic_dis_path)
        # _download_to_temp normally deletes the temp file; here it's the
        # fixture itself, so make the cleanup a no-op for this test.
        monkeypatch.setattr(actor.os, "unlink", lambda path: None)

        out = tmp_path / "top.tif"
        actor._run_dis_top_to_geotiff("https://example.com/test.dis", out, None, "")

        with rasterio.open(str(out)) as ds:
            assert ds.width == 4
            assert ds.height == 3
            arr = ds.read(1)
            assert np.isnan(arr[1, 1])  # -9999 sentinel masked to nodata
            assert arr[0, 0] == pytest.approx(100.0)
            # A 30-degree rotation must NOT collapse to a north-up transform:
            # the "b"/"d" (rotation/skew) affine terms must be nonzero.
            assert ds.transform.b != 0
            assert ds.transform.d != 0

    def test_rejects_non_uniform_spacing(self, tmp_path, monkeypatch):
        bad_dis = tmp_path / "bad.dis"
        bad_dis.write_text(textwrap.dedent("""\
            BEGIN options
              XORIGIN  0.0
              YORIGIN  0.0
              ANGROT  0.0
            END options
            BEGIN dimensions
              NLAY  1
              NROW  1
              NCOL  2
            END dimensions
            BEGIN griddata
              delr
                INTERNAL  FACTOR  1.0
                  10.0  20.0
              delc
                INTERNAL  FACTOR  1.0
                  10.0
              top
                INTERNAL  FACTOR  1.0
                  1.0  2.0
            END griddata
            """))
        monkeypatch.setattr(actor, "_download_to_temp", lambda url, suffix, token: str(bad_dis))
        monkeypatch.setattr(actor.os, "unlink", lambda path: None)
        with pytest.raises(RuntimeError, match="non-uniform"):
            actor._run_dis_top_to_geotiff("https://example.com/bad.dis", tmp_path / "out.tif", None, "")


class TestRunHdsAggregateGma:
    def test_converts_hds_and_aggregates_same_actor_execution(self, monkeypatch):
        def fake_hds_to_geotiff(input_url, layer, stress_period, timestep, output_path, read_token):
            data = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
            with rasterio.open(
                str(output_path),
                "w",
                driver="GTiff",
                height=2,
                width=2,
                count=1,
                dtype=np.float32,
                crs="EPSG:4326",
                transform=rasterio.transform.from_origin(0, 2, 1, 1),
                nodata=np.nan,
            ) as ds:
                ds.write(data, 1)

        monkeypatch.setattr(actor, "_run_hds_to_geotiff", fake_hds_to_geotiff)
        boundary = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"GMAnum": 12},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]],
                    },
                }
            ],
        }

        result = actor._run_hds_aggregate_gma(
            "https://example.com/heads.hds",
            boundary,
            layer=1,
            stress_period=1,
            timestep=1,
            band=1,
            gma_id="GMA 12",
            read_token="",
        )

        assert result["value"] == pytest.approx(2.5)
        assert result["pixel_count"] == 4
        assert result["source_format"] == "hds"
        assert result["layer"] == 1
        assert result["gma_id"] == "GMA 12"


# ===========================================================================
# _run_rasterize_points — requires the gdal_rasterize CLI
# ===========================================================================


@pytest.mark.skipif(not _HAS_GDAL_RASTERIZE, reason="gdal_rasterize not on PATH")
class TestRunRasterizePoints:
    def _make_points_geojson(self, tmp_path):
        geojson = {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
            "features": [
                {"type": "Feature", "properties": {"val": 1.5, "layer": 1},
                 "geometry": {"type": "Point", "coordinates": [-97.0, 33.0]}},
                {"type": "Feature", "properties": {"val": 2.5, "layer": 1},
                 "geometry": {"type": "Point", "coordinates": [-96.9, 33.0]}},
                {"type": "Feature", "properties": {"val": 99.0, "layer": 2},
                 "geometry": {"type": "Point", "coordinates": [-97.0, 33.1]}},
            ],
        }
        import json
        p = tmp_path / "points.geojson"
        p.write_text(json.dumps(geojson))
        return p

    def test_rasterizes_field_with_layer_filter(self, tmp_path, monkeypatch):
        points_path = self._make_points_geojson(tmp_path)
        monkeypatch.setattr(actor, "_download_to_temp", lambda url, suffix, token: str(points_path))
        monkeypatch.setattr(actor.os, "unlink", lambda path: None)

        out = tmp_path / "rasterized.tif"
        actor._run_rasterize_points(
            "https://example.com/points.geojson", out,
            value_field="val", pixel_size=0.05,
            attribute_filter="layer = 1", layer_name=None, read_token="",
        )

        with rasterio.open(str(out)) as ds:
            arr = ds.read(1)
            nodata = ds.nodata
            valid = arr[arr != nodata]
            # Only the two layer=1 points (1.5, 2.5) should have been burned;
            # the layer=2 point (99.0) must be excluded by attribute_filter.
            assert set(np.round(valid, 1)) <= {1.5, 2.5}
            assert 99.0 not in valid

    def test_background_pixels_are_nodata_not_zero(self, tmp_path, monkeypatch):
        """Regression check for the -init bug: unburned pixels must be nodata,
        not silently 0 (which would be indistinguishable from a real value)."""
        points_path = self._make_points_geojson(tmp_path)
        monkeypatch.setattr(actor, "_download_to_temp", lambda url, suffix, token: str(points_path))
        monkeypatch.setattr(actor.os, "unlink", lambda path: None)

        out = tmp_path / "rasterized.tif"
        actor._run_rasterize_points(
            "https://example.com/points.geojson", out,
            value_field="val", pixel_size=0.01,
            attribute_filter=None, layer_name=None, read_token="",
        )
        with rasterio.open(str(out)) as ds:
            arr = ds.read(1)
            assert ds.nodata == -9999
            # (max_lon, max_lat) corner: none of the 3 fixture points sit there
            # (unlike (0,0), which coincides exactly with the layer=2 point) —
            # must be nodata, not silently 0.
            assert arr[0, -1] == -9999
