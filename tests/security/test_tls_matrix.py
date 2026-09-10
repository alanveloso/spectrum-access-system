"""automated TLS/mTLS cipher and PKI negative matrix."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.mtls_auth import (
    ALLOWED_CIPHERS,
    ECC_CIPHERS,
    FORBIDDEN_CIPHERS,
    RSA_CIPHERS,
    create_mtls_ssl_context,
)
from services.tls_matrix import (
    CipherFamily,
    TlsMatrixCase,
    expected_matrix_cases,
    run_tls_matrix,
)


def test_forbidden_ciphers_not_in_allowed_lists():
    for cipher in FORBIDDEN_CIPHERS:
        assert cipher not in ALLOWED_CIPHERS
        assert cipher not in RSA_CIPHERS
        assert cipher not in ECC_CIPHERS


def test_create_mtls_ssl_context_rejects_forbidden_ciphers(tmp_path: Path):
    """SSS_14-class ciphers must not be installable on the production factory."""
    cert = tmp_path / "server.cert"
    key = tmp_path / "server.key"
    ca = tmp_path / "ca.cert"
    # Minimal placeholders — factory validates cipher list before chain load.
    for path in (cert, key, ca):
        path.write_text("placeholder\n", encoding="utf-8")
    poisoned = list(RSA_CIPHERS) + list(FORBIDDEN_CIPHERS)
    with pytest.raises(ValueError, match="forbidden TLS cipher"):
        create_mtls_ssl_context(
            certfile=cert,
            keyfile=key,
            ca_certs=ca,
            ciphers=poisoned,
        )


def test_expected_matrix_covers_plan_negatives_for_rsa_and_ecc():
    cases = expected_matrix_cases()
    ids = {c.case_id for c in cases}
    for family in ("rsa", "ecc"):
        for suffix in (
            "unknown_ca",
            "corrupted_client",
            "self_signed_client",
            "non_cbrs_ca",
            "wrong_cert_type",
            "revoked_client",
            "blacklisted_client",
            "expired_client",
            "not_yet_valid_client",
            "tls_1_1",
        ):
            assert f"{family}.{suffix}" in ids
        for cipher in FORBIDDEN_CIPHERS:
            assert f"{family}.forbid.{cipher}" in ids
        allowed = RSA_CIPHERS if family == "rsa" else ECC_CIPHERS
        for cipher in allowed:
            assert f"{family}.allow.{cipher}" in ids


def test_run_tls_matrix_full_catalogue():
    results = run_tls_matrix()
    assert results, "matrix produced no results"
    failures = [r for r in results if not r.ok]
    assert not failures, [
        f"{r.case_id}: expect_accept={r.expect_accept} accepted={r.accepted} detail={r.detail}"
        for r in failures
    ]


def test_run_tls_matrix_subset_forbidden_wrong_type_and_self_signed():
    subset = [
        TlsMatrixCase(
            case_id=f"rsa.allow.{RSA_CIPHERS[0]}",
            family=CipherFamily.RSA,
            expect_accept=True,
            description="smoke allow",
        ),
        TlsMatrixCase(
            case_id=f"rsa.forbid.{FORBIDDEN_CIPHERS[0]}",
            family=CipherFamily.RSA,
            expect_accept=False,
            description="smoke forbid",
        ),
        TlsMatrixCase(
            case_id="rsa.wrong_cert_type",
            family=CipherFamily.RSA,
            expect_accept=False,
            description="server EKU as client",
        ),
        TlsMatrixCase(
            case_id="ecc.self_signed_client",
            family=CipherFamily.ECC,
            expect_accept=False,
            description="self-signed leaf",
        ),
        TlsMatrixCase(
            case_id="ecc.blacklisted_client",
            family=CipherFamily.ECC,
            expect_accept=False,
            description="CRL blacklist",
        ),
    ]
    results = run_tls_matrix(cases=subset)
    assert len(results) == 5
    assert all(r.ok for r in results), results
