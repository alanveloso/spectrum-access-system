# nlcd

Externally acquired NLCD / clutter land-cover tiles for the protection-data
slot declared in `protection_data/manifests/cbrs_winnforum_protection.yaml`.

Tracked in Git:

- `README.md` (this file)
- `VERSION` — must match the manifest slot version

Binary tiles (`*.int`, `*.hdr`, `*.prj`, …) are **not** product source. Provision
them locally under this directory (or point the runtime at an alternate tree via
`SAS_NLCD_DIR`). Typical sources are WInnForum Common-Data / USGS NLCD packages
used with the official harness reference-model drivers.

See `services/propagation/engines.py` for how `SAS_NLCD_DIR` configures the
harness NLCD driver when reference models are available.
