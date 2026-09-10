# FCC datasets for CBRS quiet-zone / Arrangement R border protection

## Field offices (47 CFR § 0.121)

Normative NAD-83 coordinates for CBRS quiet-zone protection.
Source: 47 CFR 0.121(b). Not WInnForum harness fixtures.

File: `fcc_field_office_locations.csv`

`VERSION` in this directory marks the **field-office / FCC layout package**
revision for quiet-zone datasets. It does **not** uniquely identify the
US–Canada border KMZ below.

## US–Canada border / Arrangement R

### Artifact

File: `uscabdry_sampled.kmz`

Sampled US–Canada border geometry used for WINNF Arrangement R / BPR
**Border Sharing Zone** membership (8 km / 56 km PFD gate). The SAS loads
this file for closest-border-vertex checks before applying path-loss / PFD
limits to CBSDs whose grants overlap Arrangement R frequencies
(`highFrequency` > 3650 MHz).

### Provenance (exact)

| Field | Value |
|-------|--------|
| Upstream repository | [Wireless-Innovation-Forum/Spectrum-Access-System](https://github.com/Wireless-Innovation-Forum/Spectrum-Access-System) |
| Pinned harness commit | `928c3150adf7b31e53a96b695bf1fbdd3284ecb2` |
| Upstream path | `data/fcc/uscabdry_sampled.kmz` |
| Upstream git blob | `3812d9a0e2e73edda519958738e5368023ffb384` |
| Local SHA-256 | `3e3c28dbcecde2b06886507bec8691e23a6a12e2a498b6ac575fe8c1f16b4ec1` |
| Protection-data slot version id | `winnforum_uscabdry_sampled_928c3150adf7b31e` |

**Common-Data equivalent:** same bytes at
[Wireless-Innovation-Forum/Common-Data](https://github.com/Wireless-Innovation-Forum/Common-Data)
path `data/zones/uscabdry_sampled.kmz` (git blob `3812d9a0e2e73edda519958738e5368023ffb384`).
Known revision that last touched that path on Common-Data:
`cd18b089fe7d3169af3877b7ba5a7e0438882567` (2021-05-11). Content remains
byte-identical to the SAS harness pin above.

### How this copy was obtained

This project **copies the artifact byte-for-byte** from the pinned WInnForum
SAS harness tree. It is **not** regenerated, re-sampled, or transformed here.

Upstream already applies a **resampling** process: starting from the base
US–Canada border (`uscabdry.kml` / related sources), additional vertices are
injected so consecutive vertices are at most **200 m** apart. That spacing
lets reference models and this SAS find the closest border point
deterministically by selecting the closest **vertex** (see WInnForum
`data/fcc/README.md` on the harness pin).

Upstream resampling scripts:

- SAS harness: `src/data/resample_uscabdry.py` (commit `30f17fac…` / PR #563)
- Common-Data: `scripts/resample_uscabdry.py`

### License / copyright notice

Upstream notice (WInnForum `data/fcc/README.md`): *“Copyright on data files
is by their creators.”* The SAS / Common-Data repositories redistribute the
packaged KMZ under the **Apache License 2.0** of those projects. That does
**not** imply that all underlying geographic source data is relicensed by
this product; creator copyright on the geographic inputs remains with their
original authors / agencies.

### Startup readiness vs runtime hash

Protection-data readiness (`assert_protection_data_ready`) verifies that
`uscabdry_sampled.kmz` is **present** under `data/fcc/` (slot
`us_canada_border`, required even when `SAS_PROTECTION_DATA_STRICT=false`).
Startup does **not** currently verify the SHA-256 byte hash (no generic
checksum field on `DatasetSlot`). Operators should still verify the hash
when provisioning:

```bash
sha256sum data/fcc/uscabdry_sampled.kmz
# expected:
# 3e3c28dbcecde2b06886507bec8691e23a6a12e2a498b6ac575fe8c1f16b4ec1
```

If the file is missing or unreadable at runtime, Arrangement R membership
evaluation **fail-closes** (grants overlapping Arrangement R are not
authorized).
