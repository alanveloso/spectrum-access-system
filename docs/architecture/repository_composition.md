# Repository composition

## Layout

| Path | Role |
|------|------|
| `adapters/`, `providers/`, `primitives/`, `rf/`, `runtime/` | Generic platform |
| `services/`, `routes/`, `models/`, `schemas/` | Coordination + HTTP + ORM |
| `profiles/definitions/` | Builtin Spectrum Profiles |
| `plugins/spectrum-propagation/` | Reference RF submodule (pinned) |
| `compliance/` | Matrices + permanent harness evidence |
| `tests/` | Unit / integration / architecture fitness |
| `tests/plugin_packages/` | Test-only entry-point packages |

## Submodules

| Path | Purpose |
|------|---------|
| `plugins/spectrum-propagation` | Official reference RF implementation used by baseline deployments |

Do not absorb submodule source into the SAS tree during cleanup. Do not add
alternative RF products to this baseline repository.

## Packaging

Production `pip install` exposes only production entry points. Synthetic
plugins under `tests/plugin_packages/` require explicit editable installs for
architecture tests.
