# Official WInnForum harness — reproducible Docker validation

This directory contains **tooling only**. It does not change SAS regulatory behavior.

## Purpose

Run the official [WInnForum Spectrum Access System test harness](https://github.com/Wireless-Innovation-Forum/Spectrum-Access-System.git) in a reproducible container against the clean SAS v1 UUT.

Pinned harness commit:

```text
928c3150adf7b31e53a96b695bf1fbdd3284ecb2
(v1.0.29-34-g928c3150)
```

Expected runtime inside the image:

- Python **3.11.x**
- Shapely **1.7.1**
- Official `requirements.txt` from the pinned commit

## Prerequisites

1. Docker with `host-gateway` support (`--add-host=localhost:host-gateway`).
2. Clean harness source exported to `.cache/winnforum-harness-src/`:

   ```bash
   cd /path/to/Spectrum-Access-System
   git archive --format=tar 928c3150 | tar -x -C /path/to/sas-v1/.cache/winnforum-harness-src
   ```

3. Official harness PKI (not in git archive). Copy from a sibling checkout or generate:

   ```bash
   rsync -a ../Spectrum-Access-System/src/harness/certs/ .cache/winnforum-harness-pki/
   ```

   The directory is mounted read-only into the harness container at runtime.
4. SAS UUT listening on host `localhost:9000` / `9001` using **the same harness PKI**.

## Build

From the SAS repository root:

```bash
docker build -f tools/winnforum/Dockerfile.harness \
  --build-arg HARNESS_COMMIT=928c3150adf7b31e53a96b695bf1fbdd3284ecb2 \
  -t winnforum-sas-harness:928c3150 .
```

Or via Compose:

```bash
docker compose -f tools/winnforum/docker-compose.harness.yml build
```

Record image digest:

```bash
docker image inspect winnforum-sas-harness:928c3150 --format '{{.Id}} {{index .RepoDigests 0}}'
```

## Self-check

```bash
docker compose -f tools/winnforum/docker-compose.harness.yml run --rm harness self-check
```

Must report Python 3.11, Shapely 1.7.1, harness commit, PKI present, and `geometry.asMultiPoint`.

## Run one LIVE case (REG.1 smoke)

Start UUT on the host (certification mode, harness PKI):

```bash
export CERTS_DIR="$PWD/.cache/winnforum-harness-src/src/harness/certs"
export SAS_ADMIN_CERT_SHA1="$(python -c 'from pathlib import Path; from services.certificate_policy import fingerprint_from_cert_pem; print(fingerprint_from_cert_pem(Path(\".cache/winnforum-harness-src/src/harness/certs/admin.cert\")))')"
SAS_EXECUTION_MODE=certification python main.py
```

In another terminal:

```bash
.venv/bin/python -m tools.run_winnforum \
  --docker-harness \
  --case REG.1 \
  --start-uut \
  --certs-dir .cache/winnforum-harness-pki \
  --client-cert .cache/winnforum-harness-pki/admin.cert \
  --client-key .cache/winnforum-harness-pki/admin.key \
  --ca-certs .cache/winnforum-harness-pki/ca.cert \
  --artifacts-root artifacts/winnforum/docker-harness
```

## Run a family (FDB with configurable cases)

```bash
.venv/bin/python -m tools.run_winnforum \
  --docker-harness \
  --family FDB \
  --start-uut \
  --certs-dir .cache/winnforum-harness-pki \
  --client-cert .cache/winnforum-harness-pki/admin.cert \
  --client-key .cache/winnforum-harness-pki/admin.key \
  --ca-certs .cache/winnforum-harness-pki/ca.cert \
  --artifacts-root artifacts/winnforum/docker-harness
```

Configurable cases expand to official method names such as `test_WINNF_FT_S_FDB_1_0_default`.

## Full non-time-gated campaign

Omit `--case FDB.8` unless inside the real **02:00–04:00 US/Pacific** CPAS window.

```bash
.venv/bin/python -m tools.run_winnforum \
  --docker-harness \
  --family REG --family SIQ --family GRA --family HBT --family RLQ --family DRG \
  --family FAD --family SSS --family BPR --family EPR --family EXZ --family FPR \
  --family GPR --family IPR --family MCP --family MES --family PAT --family PCR \
  --family PPR --family QPR --family SCS --family SDS --family WDB --family FDB \
  --start-uut \
  --certs-dir .cache/winnforum-harness-pki \
  --client-cert .cache/winnforum-harness-pki/admin.cert \
  --client-key .cache/winnforum-harness-pki/admin.key \
  --ca-certs .cache/winnforum-harness-pki/ca.cert \
  --artifacts-root artifacts/winnforum/docker-harness
```

Or: `tools/winnforum/run_docker_campaign.sh`

## FDB.8 (time-gated)

Required window: **02:00–04:00 America/Los_Angeles**.

Do not fake container time. Run only in the real window:

```bash
.venv/bin/python -m tools.run_winnforum \
  --docker-harness \
  --case FDB.8 \
  --start-uut \
  ... same PKI flags ...
```

## Scheduled GitHub Actions (regression only)

Workflow: `.github/workflows/winnforum-scheduled.yml`

- Runs **daily** near the LA CPAS window (cron + wait script).
- Order: **FDB.8 first**, then the broad non-time-gated campaign.
- **Not** a merge/PR gate; **not** certification. Artifacts are regression telemetry.
- Manual: Actions → *winnforum-scheduled* → *Run workflow*.
- Expect multi-hour runtime and meaningful Actions minutes; prefer a paid plan or
  self-hosted runner if the free quota is tight.

Local prepare (same steps the workflow uses):

```bash
COMMON_DATA_ROOT=$PWD/.cache/Common-Data tools/winnforum/ci_prepare_scheduled.sh
```

## Artifacts

Host path: `artifacts/winnforum/docker-harness/` (gitignored).

Each run creates a timestamped directory with `harness.log`, `uut.log`, `results.json`, `junit.xml`, and failure excerpts.

## Federal FSS database Basic Auth

Official WInnForum `DatabaseServer(..., authorization=True)` (FDB.3–6 / FDB.8)
requires HTTP Basic Auth with the harness constants `username` / `password`
(`src/harness/database.py`).

When `--start-uut` runs under certification, the runner configures:

```bash
DB_SYNC_USERNAME=username
DB_SYNC_PASSWORD=password
```

if those variables are empty. Operator-provided non-empty values are preserved.

Production deployments must set real FSS credentials; do not rely on lab defaults.
Do not commit those secrets into `.env.example`.


Official harness `test.cfg` uses `hostname: localhost` and serves injected
databases (CPI/PAL/…) **and** `SasTestHarnessServer` peer FAD endpoints on
ports in `minPort`–`maxPort` (9010–9020). The UUT must fetch those URLs from
the **same** network namespace as the harness process. Host networking
preserves that topology without changing official testcase source.

Cases that inject `https://localhost:<port>/…` into the UUT include WDB/CPI
database URLs (`InjectDatabaseUrl`) and multi-SAS peer URLs
(`InjectPeerSas` → GRA.5 / GRA.6 / FAD.2 Full Activity Dump pull).

Legacy bridge mode (`WINNFORUM_HARNESS_NETWORK_MODE=bridge`) maps
`localhost→host-gateway` so the harness can reach a host UUT, but then
`InjectDatabaseUrl` / `InjectPeerSas(https://localhost:9010/…)` is unreachable
from the UUT (Connection refused → empty peer FAD → Grant/Heartbeat stay
`responseCode 0`). Prefer host networking for WDB/CPI/PAL and GRA/FAD peer
cases.

No TLS verification bypass is used.

## Common-Data

Large geo datasets are **not** baked into the image or into this git repo.

Preferred host checkout (sibling of the SAS repo):

```text
../Common-Data   # git clone https://github.com/Wireless-Innovation-Forum/Common-Data.git
```

Bootstrap (git-lfs + official `data/extract_geo.py` only):

```bash
tools/winnforum/fetch_common_data.sh          # seed tiles used by prior LIVE geo failures
tools/winnforum/fetch_common_data.sh --conus  # remaining CONUS NED LFS gaps (lat 25–50)
```

Docker runner auto-mounts when `../Common-Data/data/ned/*.flt` exists:

| Role | Path |
|------|------|
| Host Common-Data | `/…/Common-Data/data` |
| Container mount | `/common-data` (`:ro`) |
| County JSON (harness zip) | `/common-data-county` → `/opt/winnforum-harness/data/county` |
| Entrypoint symlinks | `/opt/winnforum-harness/data/geo/{ned,nlcd}` → `/common-data/{ned,nlcd}` |

UUT PPA creation resolves PAL `licenseAreaIdentifier` via county GeoJSON under
`data/geo/county/<FIPS>.json` (or `SAS_COUNTY_DIR`). The harness county mount is
**not** the UUT pack — sync official JSON into the UUT tree:

```bash
tools/winnforum/sync_uut_county_geometries.sh
```

`--start-uut` defaults `SAS_COUNTY_DIR` to `data/geo/county` (same pattern as
`SAS_TERRAIN_DIR` for NED).

PPA RF contour also needs the official harness `reference_models` stack on the
**UUT** interpreter (not only inside the Docker harness image):

```bash
pip install numpy==1.26.4 pykml==0.2.0
tools/winnforum/build_uut_reference_extensions.sh   # ITM/eHata for UUT Python
```

`--start-uut` sets `SAS_HARNESS_DIR` to `.cache/winnforum-harness-src/src/harness`
when unset so hybrid/ITM imports resolve to the campaign pin.

NLCD land-cover tiles for PPA RF region typing and PAT.1 hybrid (modelType 2):

```bash
tools/winnforum/sync_uut_nlcd_tiles.sh
```

NED tiles for outdoor HAAT and PAT.2 DPA AMSL (modelType 3, e.g. San Diego
corridor `n33–n34` / `w117–w118`):

```bash
tools/winnforum/sync_uut_ned_tiles.sh
```

`--start-uut` defaults `SAS_NLCD_DIR` to `data/geo/nlcd`.

Official terrain driver accepts either layout from Common-Data:

```text
ned/floatn39w098_1_std.flt
ned/usgs_ned_1_n39w098_gridfloat_std.flt
nlcd/nlcd_n40w099_ref.int
nlcd/nlcd_n40w100_ref.int
```

Do **not** rename SAS-local tiles to fake WInnForum Common-Data. Keep SAS `data/geo/` and official Common-Data separate.
Pin the Common-Data commit used for each acceptance campaign (recorded in self-check via `COMMON_DATA_COMMIT`).

## MES applicability

`WINNF_FT_S_MES_*` cases validate measurement-report configuration during registration/grant flows. They are **APPLICABLE** to CBRS SAS UUT certification scope and are mapped in `tools/winnforum/families.py`.
