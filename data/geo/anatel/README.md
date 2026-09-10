# ANATEL SLP operator data  

Providers for `protected_entities` and `boundaries` expected by
`br_anatel_slp_3700` are **capability-based** entry points:

- `spectrum_access.data_providers` → `bundle_protected_entities`
- `spectrum_access.data_providers` → `bundle_boundaries`

They load a single operator YAML bundle. This repo ships only a **template**.
Regulatory geometries (BDTA stations, EMSAT footprint, earth stations, etc.)
must be supplied by the operator from official ANATEL sources.

## Configure

```bash
cp data/geo/anatel/slp_3700_operator_bundle.template.yaml \
   data/geo/anatel/slp_3700_operator_bundle.yaml
# edit YAML with official geometries, then:
export SPECTRUM_ACCESS_FEATURE_BUNDLE="$PWD/data/geo/anatel/slp_3700_operator_bundle.yaml"
```

If the env var is unset, the loader also checks
`data/geo/anatel/slp_3700_operator_bundle.yaml` under the process cwd.

## Fail-closed behavior

- Bundle missing / unreadable → `fetch()` raises `DataBundleUnavailableError`
- Capability section present but `features: []` → `fetch()` fails closed
- Discovery may still advertise capabilities so profile doctor can see plugins

## Do not

- Commit invented EMSAT / earth-station / BDTA coordinates as product data
- Put country branching in Coordination Core
- Treat an empty template as a regulatory dataset
