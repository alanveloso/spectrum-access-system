# Runtime composition

`compose_runtime(profile, deployment, registry)` produces a
**RuntimeComposition** consumed by the coordination core and HTTP adapters.

## Flow

1. Load Spectrum Profile (canonical hashable document)
2. Load Deployment (plugin IDs + operator toggles)
3. Discover plugins via entry points
4. Resolve required capabilities against selected plugins
5. Fail closed if a required capability is missing or a toggle would disable a Profile mandate
6. Build PersistencePlan for the composition
7. Hand immutable composition to services

## Lifecycle (resolve once per process)

RuntimeComposition is resolved **once per OS process**:

| Process | When | Holder |
|---------|------|--------|
| API (uvicorn) | FastAPI `on_startup` | process-local + `app.state` |
| Celery worker | `worker_process_init` / `worker_ready` | process-local |
| Certification inline CPAS | same API process | reuses startup composition |
| Unit tests | fixture / explicit inject | ContextVar and/or process-local |

Business code (including CPAS) **looks up** the already-resolved authority via
`lookup_runtime_composition()`. It must never call `PluginRegistry.from_discovery()`
or `compose_runtime()`.

Changes to Profile, Deployment, or installed plugins require a **process restart**.
Mid-process environment changes do not hot-swap implementations.

Distinct API and worker processes each hold their own Python object; for the same
configuration they are semantically equivalent (same provenance hashes / plugins).

## Semantics

- **Resolve once, consume everywhere** — services must not re-select plugins
- Capability tokens are Profile data; adapters/providers fulfill them
- Protocol plugin identity ≠ protocol semantic ID (e.g. plugin `winnforum_rest`
  vs protocol id `winnforum-rest`)
