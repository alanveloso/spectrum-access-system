# Extension boundaries

Production entry-point groups (see `pyproject.toml`):

| Group | Purpose |
|-------|---------|
| `spectrum_access.rf_models` | Propagation / RfPort adapters |
| `spectrum_access.data_providers` | External / bundled datasets |
| `spectrum_access.device_adapters` | Device identity & capability mapping |
| `spectrum_access.network_adapters` | Network / managed-consumer mapping |
| `spectrum_access.protocol_adapters` | Wire protocols |
| `spectrum_access.mechanisms` | Protection / authorization mechanisms (when registered) |

## Rules

- Production installation must **not** expose `tests.*` entry points
- Synthetic plugins used for architecture proofs live under
  `tests/plugin_packages/` and are installed only in the test environment
- Substituting an RF model must not change IAP / protection semantics beyond
  path-loss outputs contracted by RfPort
- Providers are authoritative for their tokens; alternate providers prove
  substitutability without forking core
