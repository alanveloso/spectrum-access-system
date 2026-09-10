# C1 — Safety / consistency fixes (pré-P8)

**Date:** 2026-08-07  
**Scope:** BPR fail-closed, CPAS-DPA fail-closed, RF snapshot freeze  
**Out of scope:** GPR/PPR/FPR/EXZ/EPR/WDB/PCR/FDB domain features; Rel1Ext BPR.1 claim; official campaigns; matrix `passing` updates

## Changes

1. **BPR** (`services/border_protection.py`): Arrangement R decisions return `ALLOW` / `DENY` / `UNAVAILABLE`. Missing `reference_models`, missing coordinates in-band, or zone-check failure → fail-closed deny (never silent authorize). Explicit `SAS_BPR_PATH_LOSS_MODEL=free_space` only for lab FS path loss.
2. **CPAS-DPA** (`services/cpas_service.py`): removed swallow/`pass` on RF errors; raises `CpasRfEvaluationError` so the pipeline rolls back without pretending DPA completed. Movelist refresh evaluates all channels before upserting (atomic pending).
3. **RF freeze** (`CpasSnapshot.local_grants` / `FrozenLocalGrantRf`): freeze captures grant freqs/EIRP/lifecycle + installation (lat/lon/height/heightType/indoor/category/antenna). CPAS DPA/IAP/peer-geo evaluation uses the frozen records as source of truth.

## Verification

| Command | Result |
|---------|--------|
| `ruff check .` | All checks passed |
| `mypy` on changed modules (`--follow-imports=silent`) | Success: no issues found in 5 source files |
| Targeted pytest (BPR/C1/MCP/DPA/CPAS/IAP/multi-SAS/propagation + PG CPAS) | 76 passed, 7 skipped (concurrency PG fixture absent) |
| `pytest -q` (full) | **700 passed, 7 skipped** |
| PG RF freeze N/N+1 | `test_postgres_rf_snapshot_n_vs_n1_registration_mutation` **passed** |

Matrix case statuses were **not** changed to `passing`. Rel1Ext BPR.1 remains SPEC/HARNESS blocked.
