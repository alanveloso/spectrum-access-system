# Architecture overview

Spectrum Access System is a **profile-driven** coordination platform.

## Core model

| Concept | Meaning |
|---------|---------|
| **Profile** | Regulatory **WHAT**: bands, channelization, protection mechanisms, required capabilities |
| **Deployment** | Operator **HOW**: which plugins fulfill those capabilities |
| **PluginRegistry** | Entry-point discovery of RF, providers, adapters, mechanisms |
| **RuntimeComposition** | Single resolution of Profile + Deployment → services consume the result |
| **PersistencePlan** | Which ORM tables / contributions activate for this composition |

## Non-negotiable rules

1. **Profile = WHAT, Deployment = HOW**
2. **Resolve once, consume everywhere** — no ad-hoc re-binding in services
3. **Terrain dataset ≠ propagation model**
4. **Protocol representation ≠ canonical representation ≠ persistence representation**
5. **Generic semantics ≠ CBRS persistence ownership**
6. **Required capability missing → fail closed**
7. **Operator toggle cannot disable a mandatory Profile requirement**
8. **Reference implementation ≠ generic platform** (CBRS stays in-tree as reference)
9. **Specific may depend on generic; generic must not depend on business services**
10. **Required data capability → explicit composed authority (no silent ignore)**

## Dependency direction

```
profiles / primitives / contracts
        ↑
runtime / provider contracts / RfPort
        ↑
concrete adapters / RF reference plugins / data providers
        ↑
services / mechanisms (IAP, DPA, PPA, …)
        ↑
protocol / API routes

bootstrap (main, celery_app, runtime.bootstrap) wires across layers
```

IAP adapts its own models to ``RfPort`` via ``services/iap/rf_adapter.py``.
Generic ``runtime`` and RF contracts do not import ``services.*``.

## Documents

- [profile_driven_v1.md](profile_driven_v1.md) — frozen v1 baseline
- [runtime_composition.md](runtime_composition.md)
- [extension_boundaries.md](extension_boundaries.md)
- [rf.md](rf.md)
- [persistence.md](persistence.md)
- [data_capabilities.md](data_capabilities.md)
- [repository_composition.md](repository_composition.md)
- [profile_driven_v1_manifest.yaml](profile_driven_v1_manifest.yaml)
- [../glossary.md](../glossary.md)
