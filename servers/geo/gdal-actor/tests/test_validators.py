"""
Unit tests for validators.py — the GDAL actor injection-safe parameter contract.

No Docker, no GDAL, no external services required.
Run with:
    python3 -m pytest tests/test_validators.py -v
or from the gdal-actor directory:
    python3 -m pytest tests/ -v

These tests enumerate every rule in the validation contract (Decision 7 in
the design spec: 2026-06-29-geo-actor-mcp-server.md).
"""

from __future__ import annotations

import os
import sys
import pytest

# Ensure the parent directory (gdal-actor/) is on sys.path so validators
# can be imported without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from validators import (
    ALLOWED_OPERATIONS,
    validate_attribute_filter,
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


# ===========================================================================
# validate_operation
# ===========================================================================


class TestValidateOperation:
    def test_all_allowed(self):
        for op in sorted(ALLOWED_OPERATIONS):
            assert validate_operation(op) == op

    def test_rejects_shell_injection(self):
        with pytest.raises(ValueError):
            validate_operation("gdalinfo; rm -rf /")

    def test_rejects_arbitrary_command(self):
        with pytest.raises(ValueError):
            validate_operation("rm")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_operation("")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            validate_operation(42)

    def test_rejects_gdal_warp_direct(self):
        """Full GDAL binary names outside the allowlist are rejected."""
        with pytest.raises(ValueError):
            validate_operation("gdalwarp")

    def test_rejects_gdal_translate_direct(self):
        with pytest.raises(ValueError):
            validate_operation("gdal_translate")


# ===========================================================================
# validate_target_crs
# ===========================================================================


class TestValidateTargetCrs:
    def test_valid_epsg4326(self):
        result = validate_target_crs(4326)
        assert result == ["-t_srs", "EPSG:4326"]

    def test_valid_epsg32614(self):
        result = validate_target_crs(32614)
        assert result == ["-t_srs", "EPSG:32614"]

    def test_valid_minimum(self):
        result = validate_target_crs(1)
        assert result == ["-t_srs", "EPSG:1"]

    def test_valid_maximum(self):
        result = validate_target_crs(999999)
        assert result == ["-t_srs", "EPSG:999999"]

    def test_rejects_zero(self):
        with pytest.raises(ValueError):
            validate_target_crs(0)

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            validate_target_crs(-1)

    def test_rejects_too_large(self):
        with pytest.raises(ValueError):
            validate_target_crs(1_000_000)

    def test_rejects_string(self):
        with pytest.raises(ValueError):
            validate_target_crs("4326")

    def test_rejects_float(self):
        with pytest.raises(ValueError):
            validate_target_crs(4326.0)

    def test_rejects_bool(self):
        """bool is a subclass of int; must be explicitly rejected."""
        with pytest.raises(ValueError):
            validate_target_crs(True)

    def test_rejects_injection_string(self):
        with pytest.raises(ValueError):
            validate_target_crs("4326; rm -rf /")

    def test_rejects_none(self):
        with pytest.raises(ValueError):
            validate_target_crs(None)


# ===========================================================================
# validate_output_name
# ===========================================================================


class TestValidateOutputName:
    def test_valid_simple(self):
        assert validate_output_name("output.tif") == "output.tif"

    def test_valid_with_underscores_dashes(self):
        assert validate_output_name("rainfall_epsg4326-v2.tif") == "rainfall_epsg4326-v2.tif"

    def test_valid_dots_in_name(self):
        assert validate_output_name("file.v1.0.tif") == "file.v1.0.tif"

    # Path traversal
    def test_rejects_dotdot(self):
        with pytest.raises(ValueError):
            validate_output_name("../etc/passwd")

    def test_rejects_dotdot_embedded(self):
        with pytest.raises(ValueError):
            validate_output_name("out/../secret.tif")

    def test_rejects_absolute_path(self):
        with pytest.raises(ValueError):
            validate_output_name("/etc/passwd.tif")

    def test_rejects_forward_slash(self):
        with pytest.raises(ValueError):
            validate_output_name("subdir/output.tif")

    def test_rejects_backslash(self):
        with pytest.raises(ValueError):
            validate_output_name("sub\\output.tif")

    # Metachar injection
    def test_rejects_semicolon(self):
        with pytest.raises(ValueError):
            validate_output_name("output;rm.tif")

    def test_rejects_dollar_sign(self):
        with pytest.raises(ValueError):
            validate_output_name("$(whoami).tif")

    def test_rejects_backtick(self):
        with pytest.raises(ValueError):
            validate_output_name("`id`.tif")

    def test_rejects_space(self):
        with pytest.raises(ValueError):
            validate_output_name("my output.tif")

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError):
            validate_output_name("output\x00.tif")

    # Extension
    def test_rejects_wrong_extension(self):
        with pytest.raises(ValueError):
            validate_output_name("output.nc")

    def test_rejects_no_extension(self):
        with pytest.raises(ValueError):
            validate_output_name("output")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_output_name("")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            validate_output_name(42)


# ===========================================================================
# validate_compression
# ===========================================================================


class TestValidateCompression:
    @pytest.mark.parametrize("comp", ["deflate", "lzw", "zstd", "none"])
    def test_valid_enum(self, comp):
        assert validate_compression(comp) == comp

    def test_rejects_gzip(self):
        with pytest.raises(ValueError):
            validate_compression("gzip")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_compression("")

    def test_rejects_uppercase(self):
        """Enum is case-sensitive; DEFLATE is rejected."""
        with pytest.raises(ValueError):
            validate_compression("DEFLATE")

    def test_rejects_injection(self):
        with pytest.raises(ValueError):
            validate_compression("deflate; rm -rf /")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            validate_compression(1)


# ===========================================================================
# validate_overview_levels
# ===========================================================================


class TestValidateOverviewLevels:
    def test_valid_standard(self):
        assert validate_overview_levels([2, 4, 8]) == [2, 4, 8]

    def test_valid_single(self):
        assert validate_overview_levels([2]) == [2]

    def test_valid_max_elements(self):
        levels = [2, 4, 8, 16, 32, 64, 128, 256, 512, 2]
        assert validate_overview_levels(levels) == levels

    def test_valid_boundary_min(self):
        assert validate_overview_levels([2]) == [2]

    def test_valid_boundary_max(self):
        assert validate_overview_levels([512]) == [512]

    def test_rejects_below_min(self):
        with pytest.raises(ValueError):
            validate_overview_levels([1])

    def test_rejects_above_max(self):
        with pytest.raises(ValueError):
            validate_overview_levels([513])

    def test_rejects_zero(self):
        with pytest.raises(ValueError):
            validate_overview_levels([0])

    def test_rejects_too_many(self):
        with pytest.raises(ValueError):
            validate_overview_levels([2, 4, 8, 16, 32, 64, 128, 256, 512, 2, 4])  # 11 items

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_overview_levels([])

    def test_rejects_non_list(self):
        with pytest.raises(ValueError):
            validate_overview_levels("2 4 8")

    def test_rejects_float_in_list(self):
        with pytest.raises(ValueError):
            validate_overview_levels([2.0, 4, 8])

    def test_rejects_bool_in_list(self):
        """bool is subclass of int; must be rejected."""
        with pytest.raises(ValueError):
            validate_overview_levels([True, 4, 8])

    def test_rejects_string_in_list(self):
        with pytest.raises(ValueError):
            validate_overview_levels(["2", 4, 8])


# ===========================================================================
# validate_clip_geometry
# ===========================================================================


_VALID_POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [
            [-97.75, 30.25],
            [-97.70, 30.25],
            [-97.70, 30.30],
            [-97.75, 30.30],
            [-97.75, 30.25],
        ]
    ],
}

_VALID_MULTIPOLYGON = {
    "type": "MultiPolygon",
    "coordinates": [
        [
            [
                [-97.75, 30.25],
                [-97.70, 30.25],
                [-97.70, 30.30],
                [-97.75, 30.30],
                [-97.75, 30.25],
            ]
        ]
    ],
}


class TestValidateClipGeometry:
    def test_valid_polygon(self):
        result = validate_clip_geometry(_VALID_POLYGON)
        assert result["type"] == "Polygon"

    def test_valid_multipolygon(self):
        result = validate_clip_geometry(_VALID_MULTIPOLYGON)
        assert result["type"] == "MultiPolygon"

    # Type enforcement
    def test_rejects_string(self):
        """Must be a dict, not a JSON string."""
        with pytest.raises(ValueError):
            validate_clip_geometry('{"type": "Polygon", "coordinates": []}')

    def test_rejects_point(self):
        with pytest.raises(ValueError):
            validate_clip_geometry({"type": "Point", "coordinates": [-97.0, 30.0]})

    def test_rejects_linestring(self):
        with pytest.raises(ValueError):
            validate_clip_geometry(
                {
                    "type": "LineString",
                    "coordinates": [[-97.0, 30.0], [-96.0, 30.0]],
                }
            )

    def test_rejects_none_type(self):
        with pytest.raises(ValueError):
            validate_clip_geometry({"type": None, "coordinates": []})

    def test_rejects_missing_coordinates(self):
        with pytest.raises(ValueError):
            validate_clip_geometry({"type": "Polygon"})

    # WGS84 bounds
    def test_rejects_longitude_out_of_bounds(self):
        geom = {
            "type": "Polygon",
            "coordinates": [
                [
                    [200.0, 30.25],  # lon > 180
                    [201.0, 30.25],
                    [201.0, 30.30],
                    [200.0, 30.30],
                    [200.0, 30.25],
                ]
            ],
        }
        with pytest.raises(ValueError):
            validate_clip_geometry(geom)

    def test_rejects_latitude_out_of_bounds(self):
        geom = {
            "type": "Polygon",
            "coordinates": [
                [
                    [-97.75, 91.0],  # lat > 90
                    [-97.70, 91.0],
                    [-97.70, 90.0],
                    [-97.75, 90.0],
                    [-97.75, 91.0],
                ]
            ],
        }
        with pytest.raises(ValueError):
            validate_clip_geometry(geom)

    # Vertex cap
    def test_rejects_too_many_vertices(self):
        # Build a polygon with 1001 vertices
        ring = [[-97.75 + i * 0.0001, 30.25] for i in range(1001)]
        ring.append(ring[0])  # close the ring
        geom = {"type": "Polygon", "coordinates": [ring]}
        with pytest.raises(ValueError):
            validate_clip_geometry(geom)

    def test_accepts_exactly_1000_vertices(self):
        ring = [[-97.75 + i * 0.0001, 30.25] for i in range(999)]
        ring.append(ring[0])  # closing vertex = 1000 total
        geom = {"type": "Polygon", "coordinates": [ring]}
        result = validate_clip_geometry(geom)
        assert result["type"] == "Polygon"

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError):
            validate_clip_geometry(42)

    def test_rejects_list(self):
        with pytest.raises(ValueError):
            validate_clip_geometry([[[-97.75, 30.25]]])


# ===========================================================================
# validate_input_url
# ===========================================================================


class TestValidateInputUrl:
    def test_valid_https(self):
        url = "https://example.com/data/file.tif"
        assert validate_input_url(url) == url

    def test_valid_http(self):
        url = "http://localhost:5001/resource/download/file.tif"
        assert validate_input_url(url) == url

    def test_valid_tapis_uri(self):
        url = "tapis://ls6/modflow/demo/ntgam-v301/heads.hds"
        assert validate_input_url(url) == url

    def test_rejects_tapis_without_path(self):
        with pytest.raises(ValueError):
            validate_input_url("tapis://ls6")

    def test_rejects_tapis_path_traversal(self):
        with pytest.raises(ValueError):
            validate_input_url("tapis://ls6/modflow/../secret.hds")

    def test_rejects_file_scheme(self):
        with pytest.raises(ValueError):
            validate_input_url("file:///etc/passwd")

    def test_rejects_ftp(self):
        with pytest.raises(ValueError):
            validate_input_url("ftp://example.com/data.tif")

    def test_rejects_no_scheme(self):
        with pytest.raises(ValueError):
            validate_input_url("example.com/data.tif")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_input_url("")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            validate_input_url(42)

    def test_allowed_host_accepts_match(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_HOST", "ckan.example.org")
        url = "https://ckan.example.org/resource/download/file.tif"
        assert validate_input_url(url) == url

    def test_allowed_host_rejects_mismatch(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_HOST", "ckan.example.org")
        with pytest.raises(ValueError):
            validate_input_url("https://evil.attacker.com/data.tif")

    def test_allowed_host_ignores_port_in_env(self, monkeypatch):
        """ALLOWED_HOST=ckan.example.org:5001 should match the hostname."""
        monkeypatch.setenv("ALLOWED_HOST", "ckan.example.org:5001")
        url = "https://ckan.example.org/resource/download/file.tif"
        assert validate_input_url(url) == url

    def test_no_allowed_host_allows_any(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_HOST", raising=False)
        url = "https://public-bucket.s3.amazonaws.com/cog.tif"
        assert validate_input_url(url) == url

    def test_rejects_localhost_in_ssrf_guard(self, monkeypatch):
        """If ALLOWED_HOST is set, localhost is blocked by the host check."""
        monkeypatch.setenv("ALLOWED_HOST", "ckan.example.org")
        with pytest.raises(ValueError):
            validate_input_url("http://localhost/internal-service")

    def test_rejects_gopher_scheme(self):
        with pytest.raises(ValueError):
            validate_input_url("gopher://example.com/1/path")


# ===========================================================================
# validate_grid_uri
# ===========================================================================


class TestValidateGridUri:
    def test_accepts_https_grid_uri(self):
        url = "https://ckan.tacc.utexas.edu/model/ntgam.dis"
        assert validate_grid_uri(url) == url

    def test_accepts_tapis_grid_uri(self):
        url = "tapis://ls6/model/ntgam.dis"
        assert validate_grid_uri(url) == url

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="grid_uri path must not contain"):
            validate_grid_uri("tapis://ls6/model/../secret.dis")

    def test_honors_allowed_grid_hosts(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_GRID_HOSTS", "ckan.tacc.utexas.edu")
        assert validate_grid_uri("https://ckan.tacc.utexas.edu/model/ntgam.dis")
        with pytest.raises(ValueError, match="grid_uri host is not permitted"):
            validate_grid_uri("https://evil.example/model/ntgam.dis")


# ===========================================================================
# validate_field_name (rasterize_points: -a / -l identifiers)
# ===========================================================================


class TestValidateFieldName:
    def test_valid_identifier(self):
        assert validate_field_name("storativit") == "storativit"

    def test_valid_with_underscore(self):
        assert validate_field_name("model_layer_4") == "model_layer_4"

    def test_rejects_leading_digit(self):
        with pytest.raises(ValueError):
            validate_field_name("4layer")

    def test_rejects_shell_metacharacters(self):
        with pytest.raises(ValueError):
            validate_field_name("field; rm -rf /")

    def test_rejects_flag_like_value(self):
        with pytest.raises(ValueError):
            validate_field_name("-a")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_field_name("")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            validate_field_name(123)

    def test_rejects_too_long(self):
        with pytest.raises(ValueError):
            validate_field_name("a" * 65)


# ===========================================================================
# validate_attribute_filter (rasterize_points: -where)
# ===========================================================================


class TestValidateAttributeFilter:
    def test_valid_integer_equality(self):
        assert validate_attribute_filter("layer = 4") == "layer = 4"

    def test_valid_no_spaces(self):
        assert validate_attribute_filter("layer=4") == "layer=4"

    def test_valid_decimal(self):
        assert validate_attribute_filter("depth = 1.5") == "depth = 1.5"

    def test_rejects_or_injection(self):
        with pytest.raises(ValueError):
            validate_attribute_filter("1=1 OR 1=1")

    def test_rejects_string_literal(self):
        with pytest.raises(ValueError):
            validate_attribute_filter("name = 'x'")

    def test_rejects_semicolon(self):
        with pytest.raises(ValueError):
            validate_attribute_filter("layer = 4; DROP TABLE x")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            validate_attribute_filter(4)


# ===========================================================================
# validate_pixel_size (rasterize_points: -tr)
# ===========================================================================


class TestValidatePixelSize:
    def test_valid_int(self):
        assert validate_pixel_size(1320) == 1320.0

    def test_valid_float(self):
        assert validate_pixel_size(0.5) == 0.5

    def test_rejects_zero(self):
        with pytest.raises(ValueError):
            validate_pixel_size(0)

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            validate_pixel_size(-100)

    def test_rejects_too_large(self):
        with pytest.raises(ValueError):
            validate_pixel_size(2_000_000)

    def test_rejects_bool(self):
        with pytest.raises(ValueError):
            validate_pixel_size(True)

    def test_rejects_non_number(self):
        with pytest.raises(ValueError):
            validate_pixel_size("1320")


# ===========================================================================
# validate_crs_wkt (dis_top_to_geotiff)
# ===========================================================================


class TestValidateCrsWkt:
    def test_valid_geogcrs(self):
        wkt = 'GEOGCRS["WGS 84"]'
        assert validate_crs_wkt(wkt) == wkt

    def test_valid_compoundcrs(self):
        wkt = 'COMPOUNDCRS["GAM + NAVD88 height"]'
        assert validate_crs_wkt(wkt) == wkt

    def test_rejects_non_wkt_string(self):
        with pytest.raises(ValueError):
            validate_crs_wkt("not a crs")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_crs_wkt("")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError):
            validate_crs_wkt('GEOGCRS["' + "x" * 20_000 + '"]')

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            validate_crs_wkt(123)
