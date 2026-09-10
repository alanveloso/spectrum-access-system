# USGS NED 1″ terrain tiles (GridFloat)

Required for Category A outdoor HAAT validation (47 CFR § 96.43 / WINNF REG.7).

Package contract: `protection_data/manifests/cbrs_winnforum_protection.yaml`
(slot `terrain_ned` / `terrain_ned_payload`). Keep `VERSION` in sync with the
manifest; binary `.flt` tiles remain gitignored.

## Source

WInnForum [Common-Data](https://github.com/Wireless-Innovation-Forum/Common-Data)
(`data/ned/usgs_ned_1_*_gridfloat_std.zip`). Algorithm and tile layout match the
official harness `reference_models.geo.terrain.TerrainDriver`.

## Minimum tiles for REG family (Cat A outdoor HAAT)

Extract these `.flt` files into this directory (Common-Data naming):

**REG.7 (DC area)**

- `usgs_ned_1_n39w077_gridfloat_std.flt`
- `usgs_ned_1_n39w078_gridfloat_std.flt`
- `usgs_ned_1_n40w077_gridfloat_std.flt`
- `usgs_ned_1_n40w078_gridfloat_std.flt`

**Other REG fixtures (Kansas)**

- `usgs_ned_1_n39w098_gridfloat_std.flt` (device_c)
- `usgs_ned_1_n40w101_gridfloat_std.flt` (device_g)

## Expanded tiles (additional Cat-A outdoor HAAT fixtures)

Additional tiles for SIQ/GRA/EXZ outdoor Cat-A HAAT and related fixtures
(harness `ceil(lat)` / `floor(lon)` NW-corner encoding, including HAAT
3–16 km radials). Prefer `usgs_ned_1_*_gridfloat_std` when present on the
Common-Data revision; otherwise `floatn*_1_std` (same GridFloat payload;
both names accepted by `NedTerrainProvider` / harness `TerrainDriver`).

Sync helper (copies from a local Common-Data checkout into this directory):

```bash
tools/winnforum/sync_uut_ned_tiles.sh
```

Provenance: Common-Data commit `f53c776f657b5965d1f5aa7d12ff786265ca91dd`
(+ `master` floatn fallbacks).

Minimum expanded set used by ROOT-1 / SIQ.3 outdoor HAAT:

- `floatn40w098_1_std.flt` (+ n40w097/n40w099/n41w098/n41w099 radials) — SIQ.3 device_c @ 40°N/−97.87°
- `usgs_ned_1_n39w100_gridfloat_std.flt` — SIQ.3 device_g @ 38.2°N/−99.5°
- `usgs_ned_1_n40w097_gridfloat_std.flt` / `usgs_ned_1_n40w100_gridfloat_std.flt`
- `floatn41w098_1_std.flt` / `floatn43w106_1_std.flt` / related MES/device_i radials
- `floatn34w107_1_std.flt` (EXZ outdoor)
- `usgs_ned_1_n31w090_gridfloat_std.flt` (EXZ outdoor)
- `usgs_ned_1_n39w097_gridfloat_std.flt` / `usgs_ned_1_n39w101_gridfloat_std.flt`
  (SIQ.9 / GRA.13 outdoor Cat-A inside PPA footprints + radials)

## Configuration

- Default path: `data/geo/ned` (this directory)
- Override: `SAS_TERRAIN_DIR` / `TERRAIN_DIR`
- Dataset version label precedence: non-empty `SAS_TERRAIN_DATASET_VERSION`,
  else `VERSION` marker, else built-in default (cache key component)

## HAAT tolerances

Documented in `services/terrain/haat.py`:

| Constant | Value | Use |
|---|---|---|
| `HAAT_SYNTHETIC_ABS_TOL_M` | `1e-9` | Analytic terrain regression |
| `HAAT_NED_ABS_TOL_M` | `1e-3` | NED float32 + bilinear vs recorded refs |
| `HAAT_REPEATABILITY_ABS_TOL_M` | `0` | Same inputs → bit-identical |

Tiles are gitignored; do not commit binary DEM data.
