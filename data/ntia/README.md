# NTIA DPA / protection-zone catalogue

Official WInnForum harness NTIA KML payloads for DPA load and NTIA TR 15-517
coastal exclusion (EXZ_2).

## Provenance

| Field | Value |
|-------|--------|
| Upstream repository | Wireless-Innovation-Forum/Spectrum-Access-System |
| Pinned harness commit | `928c3150adf7b31e53a96b695bf1fbdd3284ecb2` |
| Upstream path | `data/ntia/` |
| Local copy | byte-for-byte from that pin (not regenerated) |

## Files

| File | Role |
|------|------|
| `E-DPAs.kml` | East / coastal ESC-monitored DPAs (`load_dpas`) |
| `P-DPAs.kml` | Portal DPAs (`load_dpas`) |
| `protection_zones.kml` | NTIA TR 15-517 contours (`enable_ntia_15_517` / EXZ_2) |
| `GB_Part90_EZ.kml` | Part 90 exclusion zones (reference catalogue) |

See `PROVENANCE_SHA256.txt` for content hashes.
`VERSION` marks the package revision for protection-data readiness.

## Path resolution

UUT loaders resolve this directory via the canonical protection-data root
(`protection_data.get_data_root()` → default `<repo>/data`, overridable by
`set_data_root` / `SAS_PROTECTION_DATA_ROOT` at startup). Do not rely on
external symlinks such as `~/Código/data/ntia`.

## Fetch (CI / fresh clone)

KML binaries stay gitignored. Provision from the pinned harness commit:

```bash
tools/winnforum/fetch_ntia_catalogue.sh
```

The script downloads from GitHub raw at `HARNESS_COMMIT` and verifies SHA-256.
