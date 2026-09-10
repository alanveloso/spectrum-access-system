# C4 — EXZ / EPR (Exclusion Zones + ESC Protection)

**Date:** 2026-08-08  
**Scope:** Close EXZ.1–2 and EPR.1–2 functional gaps on the existing C1–C3 CPAS → IAP path.  
**Out of scope:** QPR / WDB / PCR / FDB residual (**DEFERRED_TO_C5**); BPR Rel1Ext SPEC; official PASS.

## Before → after

| Area | Before | After |
|------|--------|-------|
| EXZ | Admission grant + SIQ + federal HBT only | + freeze in `protection_records` + pre-IAP `exz_exclusion` on CPAS |
| NTIA 15-517 | Empty FeatureCollection if KML missing (fail-open enable) | `ExclusionZoneUnavailable` / HTTP 503; no fake contours |
| ESC IAP | Local EscSensor points (C2) | + frozen `esc_state`; peer ESC → ProtectionPoints; invalid state fail-closed |
| ESC disconnect | DPA/IPR only | Connectivity frozen; IAP ESC points **remain** (not dropped) |

## EXZ

- Geometry: GeoJSON Polygon/MultiPolygon; **50 m** buffer; invalid geometry → `ExclusionZoneError` (not “outside”).
- Frequency: `frequencyRanges` overlap required when present.
- CPAS: `exclusion_zone` (+ `ntia_exclusion_zones` when flag set) frozen at generation N; evaluate uses frozen only.
- Existing grants: next `execute_cpas_pipeline` terminates locals inside EXZ (`exz_exclusion`).
- NTIA: parser for West / East-Gulf Combined Contours from `data/ntia/protection_zones.kml`; dataset typically gitignored → **BLOCKED_BY_DATASET** when enabling without file.

## EPR

- Single-SAS: ESC sensor → `ProtectedEntityKind.ESC`, −109 dBm; neighborhood **Cat A 40 km / Cat B 80 km** per frozen `cbsd_category`.
- Multi-SAS: peer CBSD grants in IAP fairshare (same Cat A/B filter); peer `esc_sensor` also become ProtectionPoints; peers never receive local apply.
- Connectivity enum: `connected` / `disconnected` / `absent` / `unknown` / `invalid` (no truthiness).
- Disconnect/absent: keep ESC IAP active; DPA/IPR continues fail-closed channel protection (precedence documented, not contradictory).
- Invalid frozen state → `ProtectionEntityError` / CPAS RF fail-closed.

## Numeric provenance (C4 verification)

| Parameter | Class | Source |
|-----------|-------|--------|
| EXZ buffer **50 m** (`EXZ_BUFFER_M`) | **A** | WINNF SAS FT Exclusion Zone suite: CBSD inside zone **or within 50 m** of boundary → interference. |
| ESC threshold **−109 dBm** (`IapThresholdProfile.esc_dbm`) | **A** | WINNF-TS-0061 Table 8.4-2; harness `THRESH_ESC_DBM_PER_RBW = -109`. Mg=1 dB same table/SSC. |
| ESC IAP neighborhood **Cat A 40 km / Cat B 80 km** | **A** | WINNF-TS-0112 / harness `ESC_NEIGHBORHOOD_DIST_A=40`, `ESC_NEIGHBORHOOD_DIST_B=80`. Applied **per grant** in `grants_in_neighborhood` from frozen `GrantRfInfo.cbsd_category` (snapshot N). Missing/invalid category → **80 km** (conservative; never silent Cat A). |
| Peer ESC boolean radius **40_000 m** | **B** | `cbrs_winnforum.yaml` `peer_esc.params.radius_m` (boolean near-peer terminate; IAP ESC still uses Cat A/B above). |

ProtectionPoint ESC stores the Cat-B envelope (80 km); the contribution filter applies 40/80 by frozen category — one IAP algorithm, not two pipelines.

Provenance / Cat A/B regressions: `test_c4_numeric_provenance_not_fixture_ids`, `test_esc_cat_a_b_neighborhood_filter`, `test_esc_cat_b_40_80_production_path`, `test_esc_category_snapshot_n_vs_n1`.

## Thresholds (summary)

| Value | Class |
|-------|-------|
| EXZ 50 m | **A** FT EXZ |
| ESC −109 dBm, Mg=1 | **A** TS-0061 Table 8.4-2 |
| ESC neighborhood Cat A 40 / Cat B 80 km | **A** TS-0112 / interference (per frozen category) |
| Peer ESC boolean 40 km | **B** spectrum profile |
| Default ESC band when freq omitted (lower 100 MHz of CBRS) | **B** spectrum profile |
| Free Space coupling | **C** explicit lab config only |

## Snapshot / concurrency

Freeze includes EXZ, NTIA cache, EscSensor, `esc_state`. Mid-run N+1 inject does not alter evaluate on snapshot N (unit + PG tests).

## Deferred to C5

QPR, WDB, PCR, FDB residual — **DEFERRED_TO_C5** (not declared complete).

## Official status

EXZ / EPR families: **NOT_RUN** (no PASS_OFFICIAL). NTIA official geometry: **BLOCKED_BY_DATASET** until `protection_zones.kml` provisioned.

## Tests

- Unit: `tests/unit/test_c4_exz_epr.py` (EXZ-A…I, EPR-A…J + invalid ESC freq) — **19 passed**
- PG: `test_postgres_c4_exz_esc_freeze_n_vs_n1` (+ CPAS multi-SAS suite)
- Regression groups (C1–C4, MCP, DPA, grant, multi-SAS, matrix, PG): **146 passed**
- Full: `pytest -q` → **756 passed, 7 skipped**
- `ruff check .` → All checks passed
- `mypy` (changed modules, `--follow-imports=silent`) → Success
