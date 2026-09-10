# WInnForum FAD harness evidence

**Data:** 2026-08-07 (15:10–15:19 local)  
**Branch:** `fix/p5-fad-sas-cpas`  
**HEAD:** `6bdb370` (+ working-tree fix: FAD client permite omitir `coordination` vazio)  
**Harness:** `winnforum-sas-harness` @ `928c3150adf7b31e53a96b695bf1fbdd3284ecb2`  
**Comando:** P5-GATE fechamento Fase 5  
**Artefactos:** `artifacts/winnforum/p5_gate_final_20260807T181029Z/`

## Resultado do gate

**FASE 5: APROVADA** (fronteira P5/P6)

Pergunta L36 — *Há defeito restante de distribuição/sincronização/consistência
corrigível sem física RF?*  
**NÃO** após o fix A de FAD.2 (manifest). Falhas oficiais remanescentes de FDB/IPR/PAT
exigem path-loss / IAP / DPA / FSS / aggregate interference → **P6** (ou P7).

| Task | Resultado |
|---|---|
| P5-001 FAD server | **PASS** |
| P5-002 FAD client | **PASS** |
| P5-003 CPAS transacional | **PASS** |
| P5-004 Multi-SAS | **PASS** |
| **P5 overall** | **PASS** |

---

## Parte A — Inventário

### Implementado (P5)

| Área | Estado local |
|---|---|
| FAD local / serving | PASS — `fad_service`, ready/published, chunks, checksum |
| FAD peer acquisition | PASS — `fad_client_service` |
| SAS-SAS mTLS | PASS — SSS oficial 18/18 |
| Manifest/files/checksum | PASS |
| Staging + current snapshot | PASS — published atómico + PG |
| Multi-peer + isolamento | PASS — sync por peer; falha isolada |
| Provenance/ownership | PASS local — `PeerFadRecord.peer_sas_id` |
| Coordination dump | PASS local — tipo suportado; corpo vazio até eventos; omitido no peer OK |
| CPAS lifecycle + scheduler | PASS — pipeline + schedule; FAD.2 exercita TriggerDaily |
| Recovery / concurrency PG | PASS — publish + CPAS multi-sas PG / multiprocess |
| Transactional apply | PASS — evaluate→apply sob advisory + grant FOR UPDATE |

### Débitos (não P5)

| Débito | Classificação |
|---|---|
| IAP numérico / aggregate interference | **P6 RF/protection** |
| Propagation / path loss / ITM | **P6** |
| DPA neighborhood / movelist | **P6** |
| FSS/GWBL/EXZ protection completa | **P6** |
| PAT | **P6** |
| Coordination *events* ricos / IPR avançado | **P7 extension** (além do dump vazio) |
| `CpasRun` entidade nomeada (há audit/stages/flags) | residual naming — funcionalidade P5 coberta |

---

## Parte B — Casos oficiais (harness `928c315`)

| Case | FAD | SAS-SAS | CPAS | IAP/RF | Expected phase |
|---|---|---|---|---|---|
| WINNF.FT.S.FAD.1 | serving | mTLS pull | dump via CPAS path | não (conteúdo/format) | **P5** |
| WINNF.FT.S.FAD.2 | peer pull | peer TH | TriggerDaily | peer conflict (não IAP numérico) | **P5** |
| WINNF.FT.S.SSS.1–18 | dump/TLS | sim | não | não | **P5** (segurança; já P3) |
| WINNF.FT.S.FDB.1 | não | não | daily | EXZ | **P6** |
| WINNF.FT.S.FDB.2 | não | não | daily | DPA | **P6** |
| WINNF.FT.S.FDB.3–6 | não | não | daily | FSS/GWBL | **P6** |
| WINNF.FT.S.FDB.8 | não | não | scheduled window | FSS | **P6** (+ wait de janela) |
| IPR / PAT / MCP DPA | parcial | — | TriggerDaily | sim | **P6/P7** |

---

## Parte C — Execução oficial e classificação

### Smoke / aplicáveis P5

| Suite | Resultado | Artefacto |
|---|---|---|
| SSS (18) ×3 | **PASS** 18/18 | `official/20260807T181334Z` + reruns |
| FAD (2) — 1ª corrida | FAD.1 PASS; **FAD.2 FAIL** | `official/20260807T181401Z` |
| FAD (2) ×3 pós-fix | **PASS** 2/2 cada | `181618Z`, `181641Z`, `181705Z` |

### FAIL classificado (corrigido)

| Case | Class | Diagnóstico | Fix |
|---|---|---|---|
| FAD.2 | **A Produto P5** | Cliente rejeitava manifest sem `coordination`; harness omite tipo vazio → sync falha → grants não terminam → HB ≠ 103/500 | `validate_manifest`: tipos ausentes = vazios; teste unitário atualizado |

### Não executados como gate P5 (bloqueio intencional)

| Case | Class |
|---|---|
| FDB.1–6, FDB.8 | **B BLOCKED_BY_P6** |
| IPR/PAT/MCP RF | **B/C** |

Não se transformou B/C em PASS artificial.

---

## Partes D–J — Gates locais (comandos observados)

```text
FAD unit+client+multi+PG publish     → 40 passed   LOCAL_FAD_EXIT=0
CPAS schedule/pipeline + multi PG    → 31 passed   LOCAL_CPAS_EXIT=0
SSS/security local                   → 71 passed   LOCAL_SSS_EXIT=0
ruff check .                         → All checks passed
hardcode scanners (3)                → 3 passed
pytest -q                            → 565 passed, 7 skipped
doctor CERTS_DIR=harness             → PASS
postfix (após fix FAD.2)             → 40 passed; ruff PASS

PG: test_fad_publish_postgres + test_cpas_multi_sas_postgres incluídos
(advisory real; sem mock de pg_advisory_xact_lock / lock_grant_row)
SAS_FAD_CLIENT_CHECK_HOSTNAME default True (false só opt-in)
```

Determinismo oficial P5 aplicável: FAD ×3 e SSS ×3 → raw_ok=True.

---

## Parte K — Evidências task

| Evidence | Papel |
|---|---|
| `P5-001_fad_server.md` | local server |
| `P5-002_fad_client.md` | local client |
| `P5-003_cpas_transactional.md` | local CPAS |
| `P5-004_multi_sas.md` | multi-SAS + PG |
| **Este ficheiro** | gate final + oficial |

Family FAD **passing** na matrix com PASS oficial FAD.1/2 (não só unitário).  
FDB permanece **failing** / BLOCKED_BY_P6.

---

## Parte L — Fronteira P5/P6

Sem defeito P5 aberto de sync/consistência após o fix de manifest.  
Restante oficial FDB/IPR/PAT = física RF / databases federais → **P6**.

Placeholders RF **não** implementados.

---

## Tabela de requisitos (critério M38–39)

| Requirement | Evidence | Local result | Official result | Status | Remaining phase |
|---|---|---|---|---|---|
| FAD server consistente | P5-001, FAD.1 | PASS | PASS (FAD.1 ×3) | PASS oficial | — |
| FAD client seguro | P5-002, testes | PASS | PASS (via FAD.2 pull) | PASS | — |
| SAS-SAS autenticado | P3-004 / SSS | PASS | PASS SSS ×3 | PASS oficial | — |
| Múltiplos peers | P5-004 | PASS | PASS (FAD.2 peer TH) | PASS | — |
| Provenance preservada | PeerFadRecord | PASS | PASS local | PASS local | — |
| CoordinationView / dump | fad_service empty OK | PASS | PASS (FAD.1) | PASS | eventos ricos → P7 |
| Snapshots congelados | P5-004 PG N→N+1 | PASS | exercitado FAD.2 | PASS | — |
| CPAS transacional | P5-003, FAD.2 | PASS | PASS (FAD.2) | PASS | IAP numérico → P6 |
| Recovery consistente | PG rollback tests | PASS | — | PASS local | — |
| PG multi-worker | fad_publish + cpas_multi_sas PG | PASS | — | PASS local | — |
| Federal DB update (FDB) | — | parcial scheduler | UNTESTED/BLOCKED | BLOCKED_BY_P6 | P6 |
| pytest + ruff | gate artifacts | PASS | — | PASS | — |

---

## Riscos remanescentes (não bloqueiam P5)

1. Ordenação explícita peer `[A,B,C]` vs `[C,B,A]` sem teste dedicado de equivalência lógica (sync é por peer; risco baixo).
2. Entidade `CpasRun` não nomeada — audit `cpas_pipeline_audit` + flags.
3. FDB.8 (janela CPAS + FSS) não corrido — **P6**.
4. Artefactos `artifacts/winnforum/…` gitignored; evidência tracked aponta resultados e paths.

## Próximo

**P6-001** — empacotar modelos/dados de proteção (ITM/NED/DPA/FSS/…).
