"""Canonical WInnForum CBRS certificate role OIDs (WINNF-TS-0022 / openssl.cnf).

Single source for mTLS enforcement (`mtls_auth`, `cbsd_auth`, `certificate_policy`)
and the Admin/CBSD/DP/SAS surface matrix in ``services.rbac``.

Do not duplicate these OID strings elsewhere.
"""

from __future__ import annotations

from cryptography.x509.oid import ObjectIdentifier

# Role policy OIDs (must match harness / Fake SAS expectations).
OID_ROLE_SAS = ObjectIdentifier("1.3.6.1.4.1.46609.1.1.1")
OID_ROLE_INSTALLER = ObjectIdentifier("1.3.6.1.4.1.46609.1.1.2")
OID_ROLE_CBSD = ObjectIdentifier("1.3.6.1.4.1.46609.1.1.3")
OID_ROLE_DOMAIN_PROXY = ObjectIdentifier("1.3.6.1.4.1.46609.1.1.4")
OID_ZONE = ObjectIdentifier("1.3.6.1.4.1.46609.1.2")
