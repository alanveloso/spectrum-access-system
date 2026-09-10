# C2 — CPAS IAP production wiring

**Date:** 2026-08-07  
**Scope:** Wire `execute_cpas_pipeline` → ProtectionPoints → production coupling → IAP → decisions → apply  
**Out of scope:** GPR/PPR/FPR/EXZ/EPR complete; QPR/WDB/PCR/FDB residual; official PASS claims

## Audit (before)

| Question | Answer |
|----------|--------|
| Where `iap_points` entered | kwargs to `evaluate_cpas_protections` only |
| Where `iap_coupling` entered | kwargs; IAP ran **only** if `iap_coupling is not None` |
| Production callers providing them | **none** (`execute_cpas_pipeline` → `evaluate_cpas_protections(db, snapshot)`) |
| Test-only callers | `test_iap_service`, `test_mcp_iap_dpa` |
| Builders already present | FSS_COCHANNEL, GWPZ (WISP), PPA (`usage==PPA`/`ppaInfo`), ESC |

## Entity wiring classification

| Entity | Builder exists? | Data source | Production-ready? | Wire in C2? | Deferred |
|--------|-----------------|-------------|-------------------|-------------|----------|
| FSS_COCHANNEL | yes | inject FSS | builder yes; full FPR no | **YES** | FPR residual → C3 |
| GWPZ | yes | inject WISP | builder yes; full GPR no | **YES** (points only) | GPR → C3 |
| PPA | yes | zone+PPA gate | builder yes; full PPR no | **YES** (points only) | PPR → C3 |
| ESC | yes | EscSensor | builder yes; full EPR no | **YES** (points only) | EPR → C4 |
| EXZ | no IAP builder | — | no | NO | C4 |
| DPA | movelist path | activations | DPA≠IAP aggregate | DPA path kept | IPR→ENV |
| FSS_BLOCKING/TTC | no | — | no | NO | C3/C5 |

## Flow after

```
execute_cpas_pipeline
  → sync DBs / peer FAD
  → freeze_cpas_snapshot
       (local_grants RF + peer_records + protection_records)
  → evaluate_cpas_protections
       → peer boolean rules (frozen RF)
       → resolve_iap_context
            → ProtectionPoints from snapshot.protection_records
            → make_production_iap_coupling (ITM; FS only if SAS_IAP_PATH_LOSS_MODEL=free_space)
       → run_iap (frozen local + peer GrantRfInfo)
       → refresh DPA movelists (frozen RF + effective EIRP)
  → apply_cpas_decisions (locks on live rows)
  → FAD publish / commit
```

### Precedence

1. Explicit `iap_points` / `iap_coupling` (tests) → `source=override`
2. Else production builders → `source=production`
3. Entities present + coupling unavailable → `CpasRfEvaluationError` (fail-closed)
4. `SAS_IAP_ENABLED=false` → skip IAP (`source=disabled`)
5. No entities → skip IAP (`source=none`)

## Failure semantics

- No IAP entities → OK without IAP  
- Entities + ITM/coupling missing → pipeline fails / rollback (not silent skip)  
- Free Space never substitutes ITM silently  

## Snapshot

- Local RF: `local_grants` (C1)  
- Protection entities: `protection_records` frozen at same generation  
- Peers: `peer_records`  

## Local vs peer

Unchanged: peers in aggregate / fairshare; never local terminate/reduce/persist.

## Tests executed

| Command | Result |
|---------|--------|
| Targeted groups (C2/C1/CPAS/IAP/MCP/DPA/prop/multi-SAS/FAD/matrix/delta/PG CPAS) | **148 passed** |
| `pytest -q` (full) | **712 passed, 7 skipped** |
| `ruff check .` | All checks passed |
| `mypy` (changed modules, `--follow-imports=silent`) | Success |

Skips: `test_concurrency_postgres` (PG :55432 fixture absent). CPAS multi-SAS PG including IAP freeze N/N+1 **passed**.

## Deferred

- **C3:** GPR / PPR / FPR complete domain  
- **C4:** EXZ / EPR complete domain  
- **C5:** QPR / WDB / PCR / FDB residual  

Matrix/delta: notes/implementation updated; **status remains failing** (no official PASS).
