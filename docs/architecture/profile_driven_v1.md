# Profile-Driven Architecture v1

Status: **frozen baseline**

This repository is the validated product baseline for Profile-Driven Architecture
v1. It preserves the frozen regulatory and composition contracts as permanent
architecture invariants.

## Scope

In scope:

- Generic platform + in-tree CBRS/WInnForum reference
- Builtin Profiles (`cbrs_winnforum`, `br_anatel_slp_3700`, `eu_elsa`, `us_tvws_15_711`)
- Permanent architecture fitness tests (`tests/architecture/`)
- Extension boundaries for RF, providers, device/network adapters, mechanisms, persistence

Out of scope for this baseline:

- Alternative RF integrations beyond the reference `spectrum-propagation` pin
- Full operationalization of non-CBRS Profiles
- Physical extraction of the CBRS reference into another repository

## Invariants (INV-01 … INV-14)

Fitness tests assert:

| ID | Intent |
|----|--------|
| INV-01… | Profile-driven requirements & synthetic isolation |
| … | Fail-closed capability resolution |
| … | RF / provider / DeviceAdapter / NetworkAdapter substitutability |
| … | Mechanism discovery openness |
| … | Toggle reconciliation (operator cannot drop mandatory Profile reqs) |
| … | Persistence isolation & attribution |
| … | Profile-ID branching guards / zero-core recognition |

Exact case coverage lives under `tests/architecture/` and related unit Lego suites.

## Machine-readable manifest

See [profile_driven_v1_manifest.yaml](profile_driven_v1_manifest.yaml) for
profile hashes, submodule pins, and known deferred debt.
