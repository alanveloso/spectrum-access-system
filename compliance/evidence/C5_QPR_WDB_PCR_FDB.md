# C5 — QPR / WDB / PCR / FDB residual

Date: 2026-08-08  
Scope: pré-P8 C5 — quiet zones, PAL/CPI DB update, PPA contour semantics, federal residual  
Official harness: **NOT_RUN**

## Inventário (pré-implementação)

| Família | Case | Requirement | Existing | Production wiring | Snapshot/data | Missing (pré-C5) | ENV |
|---------|------|-------------|----------|-------------------|---------------|------------------|-----|
| QPR | QPR.2 | Reject reg in NRQZ | `quiet_zone_service` + Registration | Registration | 47 CFR §1.924(a) bounds | — | — |
| QPR | QPR.6 | FCC Cat A ≤2.4 km | Code path dead (CSV path `parents[2]` + missing file) | Registration only | — | Dataset + path | DATASET |
| QPR | QPR.5–8 | Cat B 4.8 / TM / config areas / grant | Absent | — | — | Implement | — |
| WDB | WDB.1 | PAL DB update | Upsert only | CPAS sync | PalRecord | Replace/remove, checksum, gen | — |
| WDB | WDB.2 | CPI DB update | ACTIVE upsert only | CPAS sync | CpiUser | Revoke absent, checksum | — |
| PCR | PCR.1–7 | Max contour / census / claimed | Hull + optional SA + overlap | Admin TriggerPpaCreation | census marker only | Clip/census/claimed | DATASET census |
| FDB | FDB.1 | EXZ reevaluation | C4 | CPAS freeze | exz_gen | Reconcile only | ENV KML |
| FDB | FDB.2 | Scheduled DPA | Meta bump + HB MVP | Sync stores raw | dpa_gen | Materialize activations | — |
| FDB | FDB.3–6 | FSS/GWBL | C3 | CPAS/IAP/pre-IAP | freeze | Reconcile | ENV |
| FDB | FDB.8 | Scheduled window | P5 schedule | CPAS | — | — | — |

## QPR applicability

| Case | Applicability | Config | Expected | Current (C5) |
|------|---------------|--------|----------|--------------|
| QPR.2 NRQZ | Always | — | Reject Registration | Preserved |
| FCC Cat A 2.4 km | Always | `fccOfficesEnabled` | Reject Reg/Grant | Implemented (`data/fcc/` 47 CFR §0.121) |
| FCC Cat B 4.8 km | Always | same | Reject Reg/Grant | Implemented |
| Table Mountain | Always (TS-0112) | `tableMountainEnabled` | Coordination distance by Cat/BW | Implemented |
| Configurable areas | `[Configurable]` | `quiet_protected_area` + flag | Reject when enabled | Implemented |
| Puerto Rico island-wide PRCZ | **C — configurable / N/A as dedicated always-on** | No distinct QPR harness island case at pin 928c315; FCC Santa Isabel office + `quiet_protected_area` | Configurable polygon required for island-wide semantics; office point alone is not equivalent | Classified C + configurable-area test |

Distance provenance: **A** (normative CFR / TS-0112) for NRQZ, FCC radii, TM table, FCC coordinates; **B** (profile/config) for enable flags and injectable protected areas.

## WDB

- PAL: `replace_pal_records` full replace (insert/update/delete absent) + injection generation bump.
- CPI: ACTIVE upsert; INACTIVE/absent revoked; generation bump.
- Checksum: optional (Admin contract); mismatch → fail that URL (`DatabaseSyncError`); no partial publish for that URL.
- Fetch: TLS client, `follow_redirects=False`.
- After successful sync: `mark_cpas_reevaluation_required`; next CPAS freezes N+1; in-flight N unchanged.
- CPAS success clears reevaluation flag.

## PCR

### RF Maximum / Largest Allowable PPA Contour (C5 follow-up)

Order (aligned with `reference_models.ppa.ppa.PpaCreationModel`):

1. Per-CBSD RF contour (−96 dBm / 10 MHz, hybrid reliability=0.5, RX height 1.5 m);
2. Union of CBSD contours (`services/ppa_rf_contour.py`);
3. Clip census GeoJSON when present;
4. Clip PAL service-area when present;
5. Optional `claimedBoundary` / `providedContour` must be ⊆ RF max (after clips).

Inputs per CBSD:

- lat/lon, height/heightType (AGL/AMSL);
- antennaGain / azimuth / beamwidth;
- **maximum allowable EIRP** (`eirpCapability` or Cat A 30 / Cat B 47 dBm/10 MHz) — never CPAS-reduced grant EIRP.

Propagation:

- Reuses `load_reference_engines()` hybrid + antenna (no parallel engine);
- Unit tests inject `PpaRfEngines` stubs only via `body['_rfEngines']`;
- Missing ITM/reference/terrain → `rf_contour_unavailable` fail-closed (no hull substitute).

Harness parity: reference_models available in sibling checkout but ITM extension / shapely often **BLOCKED_BY_ENV** locally — no PASS_LOCAL.

Also:

- `claimedBoundary` alias preserved;
- `requireCensusClip` / `requireServiceArea` fail-closed;
- InjectClusterList; overlap regressions preserved.

## FDB reconciliation

| FDB case | Previously missing | C2 | C3 | C4 | C5 | Still missing? |
|----------|--------------------|----|----|----|----|----------------|
| FDB.1 EXZ | EXZ in federal reeval | — | — | EXZ CPAS | reeval flag + freeze | Official harness |
| FDB.2 Scheduled DPA | Materialize channels | — | — | — | activations+movelist+freeze | Official / ENV KML peers |
| FDB.3–4 FSS | IAP path | — | FSS IAP | — | consume C3 | Official |
| FDB.5–6 GWBL | pre-IAP | — | GWBL | — | consume C3 | Official |
| FDB.8 window | schedule | P5 | — | — | — | Official wait |
| Post-sync reevaluation | generation only | sync-in-pipeline | — | — | explicit reeval flag | — |

## Snapshot / generation

- `capture_protection_records_for_freeze` includes `scheduled_dpa` + `dpa_activation`.
- Sync commits generation then marks reevaluation; frozen CPAS N unchanged.

## Fail-closed

- Missing FCC office CSV → deny (not allow).
- Census required + missing → PPA `withError`.
- Checksum mismatch → URL sync fails (rollback that URL).
- RF contour backend unavailable → PPA `withError` (no geometric hull fallback).
- ClaimedBoundary exceeding RF maximum → rejected.
- C1–C4 paths preserved.

## PostgreSQL

- CPAS freeze N vs ingest N+1 proven in `tests/integration/test_cpas_multi_sas_postgres.py` (C1–C4).
- WDB dedicated: `test_postgres_wdb_pal_freeze_n_vs_n1` — PAL replace bumps injection generation; frozen PAL capture stays on N; next freeze sees N+1.
- Unit: `test_wdb_n_n1_reevaluation_flag`, PAL replace/checksum.

## Tests

- `tests/unit/test_c5_qpr_wdb_pcr_fdb.py`
- `tests/unit/test_ppa_rf_contour.py` (RF max contour A–J + PRCZ classification)
- `tests/unit/test_ppa_creation.py` (updated for RF path)
- Regression: C2/C3/C4, registration NRQZ, data_injection
- PG: `test_postgres_wdb_pal_freeze_n_vs_n1`

## Official blockers

- QPR/WDB/PCR/FDB harness: **NOT_RUN**
- Census county official polygons: **BLOCKED_BY_DATASET** when `requireCensusClip` without provisioned GeoJSON
- Federal FSS/EXZ full campaign: **BLOCKED_BY_ENV**

## Hard-code scan

- No WInnForum fixture IDs/coordinates in product branches.
- FCC / TM / NRQZ coordinates are **normative CFR/TS** datasets, not harness fixtures.
- No family marked `passing` without official evidence.
