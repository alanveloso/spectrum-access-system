"""RBAC surface matrix for mTLS identities.

Enforcement remains in ``mtls_auth`` / ``cbsd_auth`` / route dependencies.
Role OIDs come exclusively from ``services.winnf_role_oids`` (same source as
enforcement) so this matrix cannot drift from the live validators.

Kept (not deleted): documents which roles may call Admin / CBSD / SAS↔SAS
surfaces without duplicating enforcement logic.
"""

from __future__ import annotations

from typing import Final

from services.winnf_role_oids import (
    OID_ROLE_CBSD,
    OID_ROLE_DOMAIN_PROXY,
    OID_ROLE_INSTALLER,
    OID_ROLE_SAS,
)

ROLE_SAS: Final = OID_ROLE_SAS.dotted_string
ROLE_INSTALLER: Final = OID_ROLE_INSTALLER.dotted_string
ROLE_CBSD: Final = OID_ROLE_CBSD.dotted_string
ROLE_DOMAIN_PROXY: Final = OID_ROLE_DOMAIN_PROXY.dotted_string

# Surface → allowed roles (logical). Installer is never an API client here.
ROLE_MATRIX: Final[dict[str, frozenset[str]]] = {
    "admin": frozenset({ROLE_SAS}),
    "cbsd_api": frozenset({ROLE_CBSD, ROLE_DOMAIN_PROXY}),
    "sas_sas": frozenset({ROLE_SAS}),
}


def roles_for_surface(surface: str) -> frozenset[str]:
    try:
        return ROLE_MATRIX[surface]
    except KeyError as exc:
        raise KeyError(f"unknown RBAC surface: {surface!r}") from exc
