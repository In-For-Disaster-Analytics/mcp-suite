# Design Spec: `grid_uri` Support for HDS Aggregation Georeferencing

**Status:** Implementing

---

## Objective

Enable `hds_aggregate_gma` in the geo_actor to produce spatially correct GeoTIFFs from MODFLOW HDS binary files by deriving the affine transform and CRS from a companion DIS (MODFLOW 6 text discretization) file — eliminating the "Input shapes do not overlap raster" error caused by the current placeholder EPSG:4326 / pixel-space transform.

---

## User Need

**Primary user:** DFC pipeline (svo-adapter) calling `hds_aggregate_gma` on NTGAM and similar GAM outputs.

**Job-to-be-done:** Aggregate MODFLOW head values over a GMA boundary to produce a GMA-average scalar for DFC compliance checking.

**Current pain:** The geo_actor's `hds_aggregate_gma` operation converts HDS → GeoTIFF with placeholder coordinates (`crs=EPSG:4326`, pixel-space transform), so rasterio's mask operation finds zero overlap with the real GMA boundary polygon, returning `no valid pixels within GMA boundary`.

**Definition of success:** When `grid_uri` is supplied, the HDS layer is written as a rotation-aware GeoTIFF with the correct CRS and affine transform derived from the DIS geometry, enabling accurate spatial overlap with GMA boundaries.

---

## Current Code/System Summary

### geo_actor/actor.py

- `_run_hds_to_geotiff` (line 621): Converts HDS binary → single-band GeoTIFF. Hard-codes placeholder:
  - `transform = rasterio.transform.from_bounds(0, 0, ncol, nrow, ncol, nrow)` (pixel-space)
  - `crs="EPSG:4326"` with comment "placeholder — override when grid_uri is available"
- `_run_hds_aggregate_gma` (line 710): Calls `_run_hds_to_geotiff` then `_aggregate_raster_path`. No `grid_uri` parameter.
- `_parse_dis_top` (line 799): Already exists and correctly parses DIS geometry (xorigin, yorigin, angrot, delr, delc, top). Used by `_run_dis_top_to_geotiff`.
- `_run_dis_top_to_geotiff` (line 869): Uses `_parse_dis_top` + uniform-cell affine formula to write a properly georeferenced `top` array GeoTIFF. This is the template for the HDS georeferencing fix.

### svo-adapter-service/app/task_code.py

- `_FUSED_DFC_CHAIN_SNIPPET` (line 542): Calls `hds_aggregate_gma` on geo_actor but does not pass `grid_uri`.
- `STANDARD_PARAMS` in `tapis.py` (line 51): No `grid_uri` param defined.

### NTGAM DIS geometry (confirmed)

```
BEGIN options
  LENGTH_UNITS  feet
  XORIGIN  6.16901400E+06
  YORIGIN  1.90677430E+07
  ANGROT      65.00000000
END options
BEGIN dimensions
  NLAY  8
  NROW  1124
  NCOL  1412
END dimensions
```
- delr = delc = 1320 ft (uniform)
- CRS: NAD83 Albers (EPSG:4269) in US survey feet — **not** EPSG:4326

---

## Proposed Design

### 1. Add `grid_uri` param to `hds_aggregate_gma` message schema

In `actor.py`, extend the `hds_aggregate_gma` operation handler to accept an optional `grid_uri` string in `params`. When present, the DIS file at that URI is downloaded and parsed to extract grid geometry.

```python
# In _run_hds_aggregate_gma signature and handler (lines 1105-1124)
grid_uri = str(params.get("grid_uri") or "")
# Pass grid_uri to _run_hds_to_geotiff
```

### 2. Extend `_run_hds_to_geotiff` to accept DIS geometry

Add an optional `dis_geom: dict | None` parameter to `_run_hds_to_geotiff`. When provided:

- Compute the rotation-aware affine transform from XORIGIN, YORIGIN, ANGROT, delr[0], delc[0] using the same formula as `_run_dis_top_to_geotiff`:
  ```python
  theta = np.radians(geom["angrot"])
  ct, st = np.cos(theta), np.sin(theta)
  dx0, dy0 = float(geom["delr"][0]), float(geom["delc"][0])
  length_y = float(geom["delc"].sum())
  a, b = ct * dx0, st * dy0
  d, e = st * dx0, -ct * dy0
  c = geom["xoff"] - st * length_y
  f = geom["yoff"] + ct * length_y
  transform = rasterio.transform.Affine(a, b, c, d, e, f)
  ```
- Write `crs` from `geom.get("crs")` (WKT string or EPSG). Fall back to the DIS file's `length_units` → NAD83 Albers US-ft if no explicit CRS in DIS.

### 3. Add `_parse_dis_geometry` helper (reuse `_parse_dis_top`)

`_parse_dis_top` already parses XORIGIN, YORIGIN, ANGROT, delr, delc, nrow, ncol. Extract grid geometry parsing into a shared helper or call `_parse_dis_top` and extract only the geometry fields (discard `top`). The `top` array is not needed for HDS aggregation.

### 4. Wire `grid_uri` through the SVO adapter

- Add `grid_uri` to `STANDARD_PARAMS` in `tapis.py`.
- Add `GRID_URI` env var binding to the `hds_aggregate_gma` step in `_FUSED_DFC_CHAIN_SNIPPET` in `task_code.py`.
- Pass `grid_uri` in the geo_actor message from the fused DFC chain.

### 5. CRS resolution

DIS files do not embed CRS. Infer from `LENGTH_UNITS`:
- `feet` → NAD83 Albers US survey feet proj4: `+proj=aea +lat_0=31.25 +lon_0=-100 +lat_1=27.5 +lat_2=35 +x_0=1500000 +y_0=6000000 +datum=NAD83 +units=us-ft +vunits=m +no_defs`
- `meters` → NAD83 Albers meters (same projection, `units=m`)

This matches the NTGAM CRS and is consistent with TWDB model grid conventions.

---

## Files Likely Affected

| File | Change |
|------|--------|
| `mcp-suite/servers/geo/gdal-actor/actor.py` | Add `grid_uri` param to `hds_aggregate_gma` handler; extend `_run_hds_to_geotiff` with DIS geometry |
| `mcp-suite/servers/geo/gdal-actor/validators.py` | Add `validate_grid_uri` (URL/Tapis path validator) |
| `monorepo/svo-adapter-service/app/task_code.py` | Add `GRID_URI` binding to `hds_aggregate_gma` call in fused DFC chain |
| `monorepo/svo-adapter-service/app/tapis.py` | Add `grid_uri` to `STANDARD_PARAMS` |

---

## API/Schema Changes

### geo_actor message (hds_aggregate_gma operation)

```json
{
  "operation": "hds_aggregate_gma",
  "input_url": "https://...",
  "params": {
    "layer": 8,
    "stress_period": 92,
    "timestep": 1,
    "gma_id": "3",
    "boundary_uri": "https://services1.arcgis.com/...",
    "grid_uri": "https://.../ntgam.dis"
  },
  "read_token": "eyJ..."
}
```

`grid_uri` is optional. When absent, behavior is unchanged (placeholder georeferencing).

### Response

No change to response schema. The `note` field in `extract_satthk_gma` already documents the grid_uri gap; that note can be removed once this is implemented.

---

## Data Flow

```
hds_aggregate_gma message
  ├── input_url  → HDS binary (tapis:// or https://)
  ├── grid_uri   → DIS text file (tapis:// or https://)  [optional]
  ├── boundary_uri → GMA polygon (ArcGIS FeatureServer)
  │
  ▼
geo_actor._run_hds_aggregate_gma
  ├── If grid_uri: download DIS → _parse_dis_top → {xoff, yoff, angrot, delr, delc, nrow, ncol, crs}
  ├── Download HDS binary
  ├── _run_hds_to_geotiff (with dis_geom)
  │     └── Write GeoTIFF with rotation-aware affine + correct CRS
  └── _aggregate_raster_path (GeoTIFF × GMA boundary)
        └── Return {value, pixel_count, gma_id, band}
```

---

## Risks and Tradeoffs

1. **Non-uniform cell spacing:** `_run_dis_top_to_geotiff` already rejects non-uniform delr/delc with an error. HDS aggregation will do the same. This is acceptable — NTGAM and most TWDB GAMs use uniform spacing; non-uniform grids would require a different raster representation.

2. **DIS file download overhead:** Adding a second download (DIS) per `hds_aggregate_gma` call doubles the I/O. For pipelines that aggregate multiple layers/timesteps from the same model, the DIS could be cached in the temp directory for the duration of the actor run.

3. **CRS inference from LENGTH_UNITS:** Hard-coded NAD83 Albers US-ft for `feet`. This matches NTGAM but may not cover all MODFLOW models. A future extension could accept an explicit `crs_wkt` or `crs_epsg` param alongside `grid_uri`.

4. **No CKAN live writes:** Implementation only; no external writes.

---

## Alternatives Considered

1. **Embed CRS in HDS metadata:** Would require changing the NTGAM CKAN resource schema. Rejected — requires dataset migration.

2. **Use flopy's StructuredGrid.get_coords():** The existing `_parse_dis_top` docstring documents why this was rejected — flopy silently drops xoff/yoff translation at real model sizes.

3. **Pass explicit transform+CRS params:** Would require callers to compute the affine themselves. The user specifically wants to use the model grid file because it is "spatially aware and more flexible."

4. **Parse DIS geometry inline in `_run_hds_to_geotiff`:** More coupling. Reusing `_parse_dis_top` (already tested) is cleaner.

---

## Test Plan

1. **Unit test `_run_hds_to_geotiff` with DIS geometry:** Parse NTGAM DIS, pass to `_run_hds_to_geotiff`, verify output GeoTIFF has correct bounds/CRS via rasterio. Use the local `ntgam.dis` file.

2. **Integration test `hds_aggregate_gma` with `grid_uri`:** Call the actor with `input_url` = NTGAM HDS, `grid_uri` = NTGAM DIS, `boundary_uri` = GMA 3 ArcGIS query. Verify response contains a valid `value` (not an error about no overlapping pixels).

3. **Regression test without `grid_uri`:** Ensure existing behavior (placeholder transform) is unchanged when `grid_uri` is omitted.

4. **Non-uniform grid error test:** Provide a DIS with non-uniform delr/delc; verify a clear error is returned.

---

## Documentation Plan

- Update `actor.py` docstring for `hds_aggregate_gma` operation to document `grid_uri` param.
- Add `grid_uri` to the `STANDARD_PARAMS` table in `tapis.py` with description.
- No external user-facing docs (no CKAN writes, no public API docs change).

---

## Rollout/Rollback Plan

- **Rollout:** Deploy updated geo_actor image. SVO adapter can begin passing `grid_uri` without any schema change (param is optional). Backward-compatible.
- **Rollback:** Revert geo_actor image. SVO adapter stops passing `grid_uri` → returns to placeholder behavior.

---

## Open Questions

1. Should `grid_uri` also enable proper georeferencing for `hds_to_geotiff` (not just `hds_aggregate_gma`)? The same DIS file could be used. Recommend: yes, extend both operations.

2. Should `extract_satthk_gma` also accept `grid_uri` to enable true saturated-thickness (head − botm) rather than mean-head proxy? The docstring at line 587-590 already flags this. Recommend: separate follow-on spec.

3. For pipelines aggregating multiple HDS layers/timesteps, should the DIS be downloaded once and reused within the same actor run? Yes — cache in the temp directory keyed by `grid_uri`.

---

## Decisions

- 2026-08-20: User approved proceeding with the model-grid approach ("do it") after rejecting per-raster GeoTIFF uploads as less flexible for other GAMs.
- 2026-08-20: Implementation must reproject boundary GeoJSON from EPSG:4326 into the raster/model CRS before masking; `grid_uri` alone is insufficient.
- 2026-08-20: Actor must fail closed on HDS/DIS dimension mismatch. The current CKAN demo HDS-like file is 200x200, while the full NTGAM DIS is 1124x1412, so a matching full HDS or subset grid metadata is still required for that specific resource.

---

## User Feedback / Decisions

- User requested using the TWDB model grid files instead of uploading all GeoTIFFs because the model grid is spatially aware and more flexible for other GAMs.
