# Persistence boundary

## PersistencePlan

Composition selects which table contributions activate:

- **Generic** tables always available to the platform
- **CBRS / WInnForum reference** tables activate only when attribution matches
  the reference regime

## Attribution (v1)

Reference CBRS persistence activates when either:

- protocol semantic id is `winnforum-rest`, **or**
- selected mechanisms include `aggregate_linear_power` or `single_link_threshold`

Generic DeviceAdapter / NetworkAdapter / geolocation / shared mechanisms such as
`protection_entitlement`, `channel_exclusion`, or `snapshot_evaluate_apply` alone
must **not** activate CBRS-only tables.

## Representations

Protocol representation ≠ canonical domain representation ≠ ORM persistence.
Generic semantics do not imply CBRS ownership of persistence.

## Migration domains (Phase D)

Physical schema materialization follows:

```
RuntimeComposition
  → PersistencePlan
  → MigrationTargetSet (explicit Alembic revision)
  → database schema
```

| Contribution | Virgin Alembic target | Notes |
|---|---|---|
| `generic` only | `20260904_generic_0001` | `admin_injected_data` only |
| `cbrs_winnforum_reference` (+ generic) | `20260808_0001` | Historical monolithic CBRS head (not rewritten) |
| generic DB later needing CBRS | `20260904_cbrs_0001` | From-generic additive path |

Bare `alembic upgrade head` / `heads` is **not** used by application bootstrap.
Application / `services.migrations.apply_schema` always upgrades to an **explicit**
revision derived from the PersistencePlan. Silent full-schema fallback is refused.

Legacy databases already at `20260808_0001` remain valid; extra historical tables
are not dropped when a newer composition selects fewer domains.

Workers consume the schema; they do not run Alembic.

## Deferred regimes

Selective PersistencePlan → MigrationTargetSet → explicit Alembic ownership is
already in place for the generic platform and the CBRS / WInnForum reference
contribution. Full persistence implementations for other operational regimes
(non-CBRS) remain deferred; that is a regime-content gap, not missing migration
modularization.
