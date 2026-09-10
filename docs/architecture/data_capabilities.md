# Data capabilities

## Model

```
Profile declares required data.* tokens (WHAT)
        ↓
Deployment selects DataProvider plugins (HOW)
        ↓
RuntimeComposition validates every required token has a composed authority
        ↓
Consumers call provider_for(capability) / data_access helpers
```

## Fail-closed

Missing required data capability → `MissingCapabilityError` at composition/startup.

Production bootstrap (`main`, Celery worker, `initialize_process_runtime_composition`)
always enforces data capabilities. `require_data_plugins=False` is reserved for
explicit test fixtures that intentionally compose incomplete deployments.

## Capability vs tile availability

| Layer | Meaning |
|---|---|
| Composition | An authoritative provider for `terrain` / `land_cover` / … is selected |
| Request/task | Specific NED/NLCD tile may still be absent → coverage / unavailable error |

Startup does **not** preload every geographic tile.

## CBRS reference authorities

| Capability | Selected plugin |
|---|---|
| terrain | `protection_terrain` |
| land_cover | `protection_land_cover` |
| boundaries | `protection_boundaries` |
| reference_data | `protection_reference` |
| rights | `protection_rights` |
| protected_entities | `bundle_protected_entities` |

Installed but unselected plugins never satisfy a requirement.
