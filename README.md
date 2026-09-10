# Spectrum Access System

Profile-driven spectrum access / coordination platform with an in-tree
**CBRS / WInnForum** reference implementation.

Regulatory behavior is selected by **Spectrum Profiles** (YAML). Operator
**Deployments** choose concrete plugins. Composition yields a fail-closed
**RuntimeComposition**.

Python ≥3.11 · FastAPI · SQLAlchemy · Pydantic · Celery · Uvicorn (mTLS)

This repository begins from the validated **Profile-Driven Architecture v1**
baseline. Detailed development history is maintained separately.

---

## What it is

| Layer | Role |
|-------|------|
| Profile | **WHAT** — bands, mechanisms, required capabilities |
| Deployment | **HOW** — plugin selection and operator toggles |
| PluginRegistry | Discover adapters / providers / RF / mechanisms |
| RuntimeComposition | Resolve once, consume everywhere |
| Reference CBRS | In-tree WInnForum protocol + protection behavior |

---

## Architecture

```text
Spectrum Profile
  -> selects/configures generic mechanisms
  -> declares required capabilities

Deployment + PluginRegistry
  -> bind RF / providers / device / network / protocol plugins

RuntimeComposition
  -> capability resolution (missing required capability → fail closed)
  -> PersistencePlan attribution
  -> coordination core (snapshot → evaluate → decide → apply)
```

See [docs/architecture/overview.md](docs/architecture/overview.md) and
[docs/architecture/profile_driven_v1.md](docs/architecture/profile_driven_v1.md).

Domain acronyms (FAD, IAP, DPA, ESC, …): [docs/glossary.md](docs/glossary.md).

---

## Supported Profiles

| Profile ID | Maturity |
|------------|----------|
| `cbrs_winnforum` | Reference operational implementation (primary certification target) |
| `br_anatel_slp_3700` | Profile / schema / composition support — **not** a full operational implementation |
| `eu_elsa` | Profile / network composition support — **not** a full operational implementation |
| `us_tvws_15_711` | Profile / schema / composition support — **not** a full operational implementation |

Official WInnForum PASS claims require an external harness campaign plus stored
evidence under `compliance/evidence/`.

---

## Extension points

- **RF** (`spectrum_access.rf_models`) — reference via `plugins/spectrum-propagation`
- **Data providers** (`spectrum_access.data_providers`)
- **Device / network adapters**
- **Protocol adapters**
- **Mechanisms** (entry-point discovery)
- **Persistence** contributions (reference vs generic plan)

Synthetic fitness plugins live under `tests/plugin_packages/` and are **not**
part of the production distribution.

---

## Interfaces

| Interface | Prefix | Auth | Purpose |
|-----------|--------|------|---------|
| **CBSD ↔ SAS** | `/v1.2` | mTLS (CBSD) | Registration, SIQ, Grant, Heartbeat, Relinquishment, Deregistration |
| **SAS ↔ SAS** | `/v1.3` | mTLS (peer SAS) | Full Activity Dump (FAD) |
| **Admin** | `/admin` | mTLS (harness / operator) | Injects, CPAS, sync, PAT query |

TLS listeners: `https://0.0.0.0:9000` (RSA), `https://0.0.0.0:9001` (ECDSA / SSS).

---

## Certificates

Default mTLS material directory is `./certs` (override with `CERTS_DIR`). Generate development certificates with `python -m tools.generate_dev_certs`.

### Execution mode and mTLS

`SAS_EXECUTION_MODE` controls both CPAS dispatch and transport auth:

| Mode | mTLS | Non-TLS TestClient |
|------|------|--------------------|
| `production` | required | **denied** |
| `certification` | required | **denied** |
| `test` | optional | allowed (pytest / local only) |

Absence of an ASGI TLS peer certificate is **never** treated as an implicit trusted client outside `test`. Reverse-proxy TLS termination that forwards plain HTTP is **not** supported unless a future trusted-proxy design is added; do not send forgeable client-cert headers.

Application certificate policy validates role/EKU and chain to configured trust anchors (WInnForum `ca.cert` typically bundles roots and issuing CAs). TLS handshake validation remains authoritative for the wire path.

## Installation

```bash
git clone --recurse-submodules <this-repo>
cd spectrum-access-system

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade 'pip==25.2'
pip install -r requirements.lock.txt
bash scripts/install_plugins.sh
pip install -e .

python -m tools.doctor
```

For architecture / substitutability tests that discover synthetic plugins:

```bash
pip install -e tests/plugin_packages/independent_fspl \
            -e tests/plugin_packages/alternate_boundaries \
            -e tests/plugin_packages/synthetic_mechanism
```

---

## Running tests

```bash
pip install -r requirements-dev.txt
# plus test plugin packages above
pytest -q
```

Architecture invariant suite: `tests/architecture/`.

---

## Architecture invariants

Permanent fitness coverage for INV-01…INV-14 (profile-driven requirements,
fail-closed capabilities, RF / provider / adapter substitutability, mechanism
discovery, toggle reconciliation, persistence isolation & attribution, and
zero-core recognition). See the v1 manifest under `docs/architecture/`.

---

## Known limitations

Deferred / non-blocking for this baseline:

- NetworkAdapter operational HTTP workflows
- Protocol / router composition beyond current adapters
- Full persistence implementations for non-CBRS operational regimes remain deferred
- Physical extraction of the CBRS reference into a separate repository
- Specialized ESC/DPA paths outside the generic RfPort contract
- Full ANATEL / eLSA / TVWS operationalization

---

## Repository composition

Reference RF implementation is the `plugins/spectrum-propagation` submodule
(pinned; do not absorb its source into the SAS tree). See
[docs/architecture/repository_composition.md](docs/architecture/repository_composition.md).

---

## License

Apache-2.0 — see [LICENSE](LICENSE).
