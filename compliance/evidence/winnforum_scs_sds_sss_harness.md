# WInnForum SCS / SDS / SSS harness evidence

**Data:** 2026-08-07  
**Branch:** `fix/p3-security-pki`  

## Harness result (authoritative)

```text
tools.run_winnforum --family SCS --family SDS --family SSS
  --harness-dir <winnforum-sas-harness>
  --certs-dir <harness>/src/harness/certs
  --client-cert <harness>/.../admin.cert
  --client-key  <harness>/.../admin.key
  --ca-certs    <harness>/.../ca.cert
  --python <harness>/.venv/bin/python
UUT env: CERTS_DIR=<harness certs> SAS_ADMIN_CERT_SHA1=<admin leaf SHA-1>

→ Ran 56 tests in 396.555s
→ OK
→ raw_ok=True  OFFICIAL_EXIT=0
```

Artefacto local (não versionado): `artifacts/winnforum/p3_gate_20260807T130841Z/official/20260807T131016Z/`

## Product fixes enabling the run

- `tools/winnforum/openssl_compat.py` — OpenSSL 3.x version decode for harness TLS1.3 cipher strip
- `SAS_ADMIN_CERT_SHA1` — Admin allowlist (harness `admin.cert` is ROLE_CBSD, not ROLE_SAS)
- Runner derives fingerprint from `--client-cert`; UUT uses `sys.executable`

## Cases

All `WINNF.FT.S.SCS.1`–`19`, `SDS.1`–`19`, `SSS.1`–`18` (56 methods) passed under the run above.
