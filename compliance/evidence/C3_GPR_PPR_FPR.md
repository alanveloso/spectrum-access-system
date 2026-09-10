# C3 — GPR / PPR / FPR protection domains

**Date:** 2026-08-07  
**Scope:** Close functional gaps for aggregated GWPZ (GPR), PPA (PPR), and FSS/GWBL (FPR) on the **existing** C2 CPAS → IAP production path.  
**Out of scope:** EXZ/EPR (C4); QPR/WDB/PCR/FDB residual (C5); BPR Rel1Ext SPEC; official harness PASS.

## Before → after

| Area | Before (post-C2) | After (C3) |
|------|------------------|------------|
| GWPZ | WISP → ProtectionPoint; SIQ/HB boolean | Aggregate IAP + neighborhood + pre-IAP geometry EZ; multi-SAS peer RF |
| PPA | Builder; full CBRS band risk; live PAL in peer PPA | PAL-bound freqs only; PAL frozen in `protection_records`; non-PPA excluded |
| FSS | Co-channel builder only | `FSS_COCHANNEL` + `FSS_BLOCKING` distinct; TTC gate; invalid RX → domain error |
| GWBL | Grant-time boolean | Pre-IAP FSS+GWBL EZ (150 km / 3650–3700) from frozen GWBL; grant boolean retained for admission |
| Freeze | FSS/WISP/ZONE/ESC | + PAL (`PalRecord`) + GWBL |
| Pipeline | C2 production path | Same path; pre-IAP exclusions before IAP |

## Requirements / cases

| Family | Cases (scope) | Product stance |
|--------|---------------|----------------|
| GPR | GPR.1–3 | Implemented + production wired; official **NOT_RUN** |
| PPR | PPR.1–3 | Implemented + production wired; official **NOT_RUN** |
| FPR | FPR.1–5 | Implemented + production wired; official **NOT_RUN** |

## GWPZ (GPR)

- Source: injected WISP records with GWPZ geometry + frequency.
- IAP: `ProtectedEntityKind.GWPZ`, threshold −80 dBm (TS-0061 Table 8.4-2 class **A**), neighborhood 40 km.
- Pre-IAP: grant inside GWPZ polygon with frequency overlap → terminate (`gwpz_exclusion`) before IAP.
- Peers contribute to aggregate; never receive local apply.
- Fail-closed: entities present + coupling unavailable → `CpasRfEvaluationError`.

## PPA (PPR)

- Valid only when `usage=="PPA"` or `ppaInfo` present (P7-005 preserved).
- Protected band from frozen PAL channel assignment only — **no silent full-CBRS invent**.
- Missing/unresolvable PAL → no PPA ProtectionPoint (no silent allow via invented band).
- Peer PPA boolean + IAP aggregate use `_frozen_pal_index(snapshot.protection_records)`.

## FSS co-channel

- Requires `fss_high >= CBRS high` and overlap; channels clipped to CBRS; threshold −129 dBm; neighborhood 150 km.
- Point id prefix `fss-cc:`.

## FSS blocking

- Distinct entity `FSS_BLOCKING`; protects CBRS below FSS low edge; threshold −60 dBm; neighborhood 40 km.
- Point id prefix `fss-bl:`.
- Entirely in 3700–4200 MHz: `ttc=False` → skip blocking; `ttc` missing → `ProtectionEntityError`; `ttc=True` → blocking applies.

## TTC

| Value | Behavior |
|-------|----------|
| `true` | Blocking allowed in 3700–4200; conservative pre-IAP purge within 40 km (`fss_ttc_purge`) |
| `false` | No blocking when FSS entirely in 3700–4200 |
| missing | `None` — fail-closed when blocking applicability requires explicit TTC |
| invalid | `ProtectionEntityError` |

**Not assumed:** `ttc missing == true`.

## GWBL

| Stage | Rule |
|-------|------|
| Grant / heartbeat admission | Existing boolean FSS+GWBL neighbor deny (federal_db_service) — immediate reject |
| CPAS pre-IAP | Frozen GWBL + FSS + grant on 3650–3700 within 150 km → `fss_gwbl_exclusion` terminate |
| IAP aggregate | GWBL is not a standalone ProtectionPoint; it gates EZ / coexists with FSS IAP points |

No contradictory dual allow: admission deny and CPAS terminate are both protective; IAP does not re-authorize GWBL-blocked geometry.

## Single / multi-SAS

- Local + peer FAD grants in same IAP fairshare.
- Decisions / apply only for `is_managing_sas=True` local grants.
- Peer snapshot frozen at generation N.

## Fail-closed

- Coupling / ITM unavailable with applicable points → error (no silent Free Space).
- Invalid FSS frequency / TTC → domain `ProtectionEntityError` (surfaced as CPAS RF error when on evaluate path).
- Free Space only via explicit `SAS_IAP_PATH_LOSS_MODEL=free_space`.

## Snapshot

`protection_records` includes FSS, WISP, ZONE, PAL, GWBL, ESC at freeze. Evaluate does not re-read live PAL/GWPZ/FSS/GWBL for IAP points.

## Threshold classification

| Parameter | Class |
|-----------|-------|
| PPA/GWPZ −80, FSS CC −129, FSS blocking −60, ESC −109, Mg=1 | **A** normative defaults (TS-0061 / harness iap constants) |
| Neighborhood km | **A** / TS-0112 interference constants |
| CBRS band edges | **B** spectrum profile |
| Lab Free Space coupling | **C** explicit config only |

## Deferred

| Item | Tag |
|------|-----|
| EXZ / EPR complete | **DEFERRED_TO_C4** |
| QPR / WDB / PCR / FDB residual | **DEFERRED_TO_C5** |
| Full R2-SGN-29 Monte-Carlo TTC purge parity | ENV / harness |
| Official GPR/PPR/FPR harness | **NOT_RUN** (ENV/datasets) |

## Tests

- Unit: `tests/unit/test_c3_gpr_ppr_fpr.py` (GPR-A…G, PPR-A…G, FPR-A…K + E2E pipeline)
- Regression groups: C2, C1, MCP, CPAS, IAP, multi-SAS, DPA/IPR, protection_data, propagation, grant PAL/PPA, heartbeat, matrix, BPR, FAD
- PostgreSQL: `test_postgres_iap_protection_records_freeze_n_vs_n1`, `test_postgres_c3_gwpz_pal_freeze_n_vs_n1` (+ concurrent CPAS suite) — **10 passed**
- Full: `pytest -q` → **736 passed, 7 skipped**
- `ruff check .` → All checks passed
- `mypy` (changed modules, `--follow-imports=silent`) → Success

## Official status

All GPR/PPR/FPR cases: **NOT_RUN** (no PASS_OFFICIAL fabricated).
