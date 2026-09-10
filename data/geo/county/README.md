# County GeoJSON (FIPS)

Required for PAL `license.licenseAreaIdentifier` resolution during PPA creation
(WINNF PCR.1 / WDB.1 and related PCR family cases).

Files are named `<FIPS>.json` (e.g. `20063.json`). Binary/geo payloads stay
gitignored; keep `VERSION` when the pack is versioned.

## Source

Official WInnForum Common-Data county extract (same JSON used by the harness
oracle under `data/county/`). Lab cache often lives at:

`.cache/winnforum-harness-county/<FIPS>.json`

## Sync helper

```bash
tools/winnforum/sync_uut_county_geometries.sh
```

Copies PCR/WDB default FIPS (`20063`, `20195`) into this directory. Override
destination with `SAS_COUNTY_DIR`. The certification runner defaults
`SAS_COUNTY_DIR` to this path when starting the UUT.
