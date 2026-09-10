"""Automated TLS/mTLS matrix probes ( / SCS·SDS·SSS cipher & PKI negatives).

Exercises the same server context factory used in production
(``create_mtls_ssl_context``) against ephemeral sockets — no harness fixture IDs.
"""

from __future__ import annotations

import socket
import ssl
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable, Sequence

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from services.mtls_auth import (
    ECC_CIPHERS,
    FORBIDDEN_CIPHERS,
    RSA_CIPHERS,
    create_mtls_ssl_context,
)

# Re-export for callers that historically only imported cipher lists from mtls_auth.
__all__ = [
    "CipherFamily",
    "TlsMatrixCase",
    "TlsProbeResult",
    "FORBIDDEN_CIPHERS",
    "run_tls_matrix",
    "probe_handshake",
]


class CipherFamily(str, Enum):
    RSA = "rsa"
    ECC = "ecc"


@dataclass(frozen=True)
class TlsMatrixCase:
    """One automated handshake expectation."""

    case_id: str
    family: CipherFamily
    expect_accept: bool
    description: str


@dataclass(frozen=True)
class TlsProbeResult:
    case_id: str
    family: CipherFamily
    expect_accept: bool
    accepted: bool
    ok: bool
    detail: str = ""


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _write_pem(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _issue_ca(*, key, common_name: str = "tls-matrix-ca") -> x509.Certificate:
    now = datetime.now(timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )


def _issue_leaf(
    *,
    issuer_cert: x509.Certificate,
    issuer_key,
    subject_key,
    common_name: str,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
    for_server: bool = False,
) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    eku = (
        [ExtendedKeyUsageOID.SERVER_AUTH]
        if for_server
        else [ExtendedKeyUsageOID.CLIENT_AUTH]
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(issuer_cert.subject)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or (now - timedelta(minutes=1)))
        .not_valid_after(not_after or (now + timedelta(days=30)))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage(eku), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if for_server:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _pem_cert(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _pem_key(key) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def probe_handshake(
    *,
    server_context: ssl.SSLContext,
    client_context: ssl.SSLContext,
    timeout: float = 2.0,
) -> tuple[bool, str]:
    """Attempt a TLS handshake between ephemeral client/server sockets."""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]
    outcome: Future[tuple[bool, str]] = Future()

    def _serve() -> None:
        conn = None
        try:
            server_sock.settimeout(timeout)
            conn, _addr = server_sock.accept()
            conn.settimeout(timeout)
            tls_conn = server_context.wrap_socket(conn, server_side=True)
            tls_conn.do_handshake()
            tls_conn.close()
            if not outcome.done():
                outcome.set_result((True, "handshake_ok"))
        except Exception as exc:  # noqa: BLE001 — probe surface
            if not outcome.done():
                outcome.set_result((False, type(exc).__name__))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    client = None
    try:
        raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        raw.settimeout(timeout)
        client = client_context.wrap_socket(raw, server_hostname="localhost")
        client.do_handshake()
 # Wait for server side to finish recording.
        accepted, detail = outcome.result(timeout=timeout)
        return accepted, detail
    except Exception as exc:  # noqa: BLE001
 # Ensure server thread unblocks.
        try:
            outcome.result(timeout=timeout)
        except Exception:
            pass
        return False, type(exc).__name__
    finally:
        if client is not None:
            try:
                client.close()
            except OSError:
                pass
        server_sock.close()
        thread.join(timeout=timeout)


def _client_context(
    *,
    ca_pem: Path,
    client_cert: Path | None,
    client_key: Path | None,
    ciphers: Sequence[str] | None,
    minimum_version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_2,
    maximum_version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_2,
    check_hostname: bool = False,
) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = minimum_version
    ctx.maximum_version = maximum_version
    ctx.check_hostname = check_hostname
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(ca_pem))
    if client_cert is not None and client_key is not None:
        ctx.load_cert_chain(certfile=str(client_cert), keyfile=str(client_key))
    if ciphers:
        ctx.set_ciphers(":".join(ciphers))
    return ctx


def _build_material(tmpdir: Path, *, family: CipherFamily) -> dict[str, Path]:
    if family is CipherFamily.RSA:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ciphers = list(RSA_CIPHERS)
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        server_key = ec.generate_private_key(ec.SECP256R1())
        client_key = ec.generate_private_key(ec.SECP256R1())
        ciphers = list(ECC_CIPHERS)

    ca_cert = _issue_ca(key=ca_key, common_name=f"tls-matrix-{family.value}-ca")
    server_cert = _issue_leaf(
        issuer_cert=ca_cert,
        issuer_key=ca_key,
        subject_key=server_key,
        common_name="tls-matrix-server",
        for_server=True,
    )
    client_cert = _issue_leaf(
        issuer_cert=ca_cert,
        issuer_key=ca_key,
        subject_key=client_key,
        common_name="tls-matrix-client",
    )

    paths = {
        "ca": tmpdir / f"{family.value}-ca.cert",
        "server_cert": tmpdir / f"{family.value}-server.cert",
        "server_key": tmpdir / f"{family.value}-server.key",
        "client_cert": tmpdir / f"{family.value}-client.cert",
        "client_key": tmpdir / f"{family.value}-client.key",
        "crl_dir": tmpdir / f"{family.value}-crl",
    }
    paths["crl_dir"].mkdir(parents=True, exist_ok=True)
    _write_pem(paths["ca"], _pem_cert(ca_cert))
    _write_pem(paths["server_cert"], _pem_cert(server_cert))
    _write_pem(paths["server_key"], _pem_key(server_key))
    _write_pem(paths["client_cert"], _pem_cert(client_cert))
    _write_pem(paths["client_key"], _pem_key(client_key))

 # Persist objects for negative case builders.
    paths["_ca_cert_obj"] = ca_cert  # type: ignore[assignment]
    paths["_ca_key_obj"] = ca_key  # type: ignore[assignment]
    paths["_client_cert_obj"] = client_cert  # type: ignore[assignment]
    paths["_ciphers"] = ciphers  # type: ignore[assignment]
    return paths


def expected_matrix_cases() -> list[TlsMatrixCase]:
    """Canonical  case catalogue (RSA + ECC)."""
    cases: list[TlsMatrixCase] = []
    for family, ciphers in (
        (CipherFamily.RSA, RSA_CIPHERS),
        (CipherFamily.ECC, ECC_CIPHERS),
    ):
        for cipher in ciphers:
            cases.append(
                TlsMatrixCase(
                    case_id=f"{family.value}.allow.{cipher}",
                    family=family,
                    expect_accept=True,
                    description=f"allowed cipher {cipher}",
                )
            )
        for cipher in FORBIDDEN_CIPHERS:
            cases.append(
                TlsMatrixCase(
                    case_id=f"{family.value}.forbid.{cipher}",
                    family=family,
                    expect_accept=False,
                    description=f"forbidden cipher {cipher}",
                )
            )
        for suffix, expect in (
            ("unknown_ca", False),
            ("corrupted_client", False),
            ("self_signed_client", False),
            ("non_cbrs_ca", False),
            ("wrong_cert_type", False),
            ("revoked_client", False),
            ("blacklisted_client", False),
            ("expired_client", False),
            ("not_yet_valid_client", False),
            ("tls_1_1", False),
        ):
            cases.append(
                TlsMatrixCase(
                    case_id=f"{family.value}.{suffix}",
                    family=family,
                    expect_accept=expect,
                    description=suffix.replace("_", " "),
                )
            )
    return cases


def run_tls_matrix(
    *,
    cases: Iterable[TlsMatrixCase] | None = None,
) -> list[TlsProbeResult]:
    """Execute the TLS matrix and return per-case handshake outcomes."""
    selected = list(cases) if cases is not None else expected_matrix_cases()
    results: list[TlsProbeResult] = []

    with TemporaryDirectory(prefix="sas-tls-matrix-") as tmp:
        root = Path(tmp)
        materials = {
            CipherFamily.RSA: _build_material(root, family=CipherFamily.RSA),
            CipherFamily.ECC: _build_material(root, family=CipherFamily.ECC),
        }

        for case in selected:
            material = materials[case.family]
            accepted, detail = _run_case(case, material)
            results.append(
                TlsProbeResult(
                    case_id=case.case_id,
                    family=case.family,
                    expect_accept=case.expect_accept,
                    accepted=accepted,
                    ok=accepted is case.expect_accept,
                    detail=detail,
                )
            )
    return results


def _run_case(case: TlsMatrixCase, material: dict) -> tuple[bool, str]:
    family = case.family
    server_ciphers: list[str] = list(material["_ciphers"])
 # Default probes must not inherit CRLs written by earlier revoke/blacklist cases.
    server_ctx = create_mtls_ssl_context(
        certfile=material["server_cert"],
        keyfile=material["server_key"],
        ca_certs=material["ca"],
        crl_dir=None,
        ciphers=server_ciphers,
    )

    ca_cert: x509.Certificate = material["_ca_cert_obj"]
    ca_key = material["_ca_key_obj"]
    client_cert_obj: x509.Certificate = material["_client_cert_obj"]
    tmp = material["ca"].parent

    if ".allow." in case.case_id:
        cipher = case.case_id.split(".allow.", 1)[1]
        client_ctx = _client_context(
            ca_pem=material["ca"],
            client_cert=material["client_cert"],
            client_key=material["client_key"],
            ciphers=[cipher],
        )
        return probe_handshake(server_context=server_ctx, client_context=client_ctx)

    if ".forbid." in case.case_id:
        cipher = case.case_id.split(".forbid.", 1)[1]
        client_ctx = _client_context(
            ca_pem=material["ca"],
            client_cert=material["client_cert"],
            client_key=material["client_key"],
            ciphers=[cipher],
        )
        return probe_handshake(server_context=server_ctx, client_context=client_ctx)

    if case.case_id.endswith(".unknown_ca"):
 # Client leaf from a CA that is not in the server trust store (unknown issuer).
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_ca = _issue_ca(key=other_key, common_name="unknown-root-ca")
        leaf_key = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if family is CipherFamily.RSA
            else ec.generate_private_key(ec.SECP256R1())
        )
        leaf = _issue_leaf(
            issuer_cert=other_ca,
            issuer_key=other_key,
            subject_key=leaf_key,
            common_name="unknown-ca-client",
        )
        cert_path = tmp / f"{case.case_id}-client.cert"
        key_path = tmp / f"{case.case_id}-client.key"
        _write_pem(cert_path, _pem_cert(leaf))
        _write_pem(key_path, _pem_key(leaf_key))
        client_ctx = _client_context(
            ca_pem=material["ca"],
            client_cert=cert_path,
            client_key=key_path,
            ciphers=server_ciphers,
        )
        return probe_handshake(server_context=server_ctx, client_context=client_ctx)

    if case.case_id.endswith(".non_cbrs_ca"):
 # Structured "operator" CA that is not the CBRS trust root loaded by the server.
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_ca = _issue_ca(key=other_key, common_name="non-cbrs-operator-ca")
        leaf_key = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if family is CipherFamily.RSA
            else ec.generate_private_key(ec.SECP256R1())
        )
        leaf = _issue_leaf(
            issuer_cert=other_ca,
            issuer_key=other_key,
            subject_key=leaf_key,
            common_name="non-cbrs-client",
        )
        cert_path = tmp / f"{case.case_id}-client.cert"
        key_path = tmp / f"{case.case_id}-client.key"
        _write_pem(cert_path, _pem_cert(leaf))
        _write_pem(key_path, _pem_key(leaf_key))
 # Client still trusts the UUT server CA; only the client leaf is non-CBRS.
        client_ctx = _client_context(
            ca_pem=material["ca"],
            client_cert=cert_path,
            client_key=key_path,
            ciphers=server_ciphers,
        )
        return probe_handshake(server_context=server_ctx, client_context=client_ctx)

    if case.case_id.endswith(".corrupted_client"):
        bad = tmp / f"{case.case_id}.cert"
        good = material["client_cert"].read_bytes()
 # Flip one byte inside the PEM body while keeping headers intact.
        body = bytearray(good)
        mid = len(body) // 2
        body[mid] = body[mid] ^ 0x01
        bad.write_bytes(bytes(body))
        try:
            client_ctx = _client_context(
                ca_pem=material["ca"],
                client_cert=bad,
                client_key=material["client_key"],
                ciphers=server_ciphers,
            )
        except ssl.SSLError as exc:
            return False, f"corrupt_load:{type(exc).__name__}"
        return probe_handshake(server_context=server_ctx, client_context=client_ctx)

    if case.case_id.endswith(".self_signed_client"):
 # End-entity self-signed leaf (not a CA cert) — mirrors SCS/SDS self-signed negatives.
        if family is CipherFamily.RSA:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        leaf = (
            x509.CertificateBuilder()
            .subject_name(_name("self-signed-client"))
            .issuer_name(_name("self-signed-client"))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        cert_path = tmp / f"{case.case_id}.cert"
        key_path = tmp / f"{case.case_id}.key"
        _write_pem(cert_path, _pem_cert(leaf))
        _write_pem(key_path, _pem_key(key))
        client_ctx = _client_context(
            ca_pem=material["ca"],
            client_cert=cert_path,
            client_key=key_path,
            ciphers=server_ciphers,
        )
        return probe_handshake(server_context=server_ctx, client_context=client_ctx)

    if case.case_id.endswith(".wrong_cert_type"):
 # SCS_10-style: server-purpose leaf (SERVER_AUTH) presented as the mTLS client.
        if family is CipherFamily.RSA:
            leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            leaf_key = ec.generate_private_key(ec.SECP256R1())
        leaf = _issue_leaf(
            issuer_cert=ca_cert,
            issuer_key=ca_key,
            subject_key=leaf_key,
            common_name="server-typed-as-client",
            for_server=True,
        )
        cert_path = tmp / f"{case.case_id}.cert"
        key_path = tmp / f"{case.case_id}.key"
        _write_pem(cert_path, _pem_cert(leaf))
        _write_pem(key_path, _pem_key(leaf_key))
        client_ctx = _client_context(
            ca_pem=material["ca"],
            client_cert=cert_path,
            client_key=key_path,
            ciphers=server_ciphers,
        )
        return probe_handshake(server_context=server_ctx, client_context=client_ctx)

    if case.case_id.endswith(".revoked_client") or case.case_id.endswith(
        ".blacklisted_client"
    ):
 # TLS-layer revoke/blacklist via CRL (harness blacklisted leaves are CRL-backed).
        target_serial = client_cert_obj.serial_number
        crl_name = (
            "blacklisted.crl.pem"
            if case.case_id.endswith(".blacklisted_client")
            else "revoked.crl.pem"
        )
        now = datetime.now(timezone.utc)
        crl = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(ca_cert.subject)
            .last_update(now - timedelta(minutes=1))
            .next_update(now + timedelta(days=1))
            .add_revoked_certificate(
                x509.RevokedCertificateBuilder()
                .serial_number(target_serial)
                .revocation_date(now - timedelta(hours=1))
                .build()
            )
            .sign(ca_key, hashes.SHA256())
        )
        crl_dir = material["crl_dir"] / case.case_id.replace(".", "_")
        crl_dir.mkdir(parents=True, exist_ok=True)
        crl_path = crl_dir / crl_name
        crl_path.write_bytes(crl.public_bytes(serialization.Encoding.PEM))
        server_ctx = create_mtls_ssl_context(
            certfile=material["server_cert"],
            keyfile=material["server_key"],
            ca_certs=material["ca"],
            crl_dir=crl_dir,
            ciphers=server_ciphers,
        )
        client_ctx = _client_context(
            ca_pem=material["ca"],
            client_cert=material["client_cert"],
            client_key=material["client_key"],
            ciphers=server_ciphers,
        )
        return probe_handshake(server_context=server_ctx, client_context=client_ctx)

    if case.case_id.endswith(".expired_client"):
        now = datetime.now(timezone.utc)
        if family is CipherFamily.RSA:
            leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            leaf_key = ec.generate_private_key(ec.SECP256R1())
        leaf = _issue_leaf(
            issuer_cert=ca_cert,
            issuer_key=ca_key,
            subject_key=leaf_key,
            common_name="expired-client",
            not_before=now - timedelta(days=30),
            not_after=now - timedelta(days=1),
        )
        cert_path = tmp / f"{case.case_id}.cert"
        key_path = tmp / f"{case.case_id}.key"
        _write_pem(cert_path, _pem_cert(leaf))
        _write_pem(key_path, _pem_key(leaf_key))
        client_ctx = _client_context(
            ca_pem=material["ca"],
            client_cert=cert_path,
            client_key=key_path,
            ciphers=server_ciphers,
        )
        return probe_handshake(server_context=server_ctx, client_context=client_ctx)

    if case.case_id.endswith(".not_yet_valid_client"):
        now = datetime.now(timezone.utc)
        if family is CipherFamily.RSA:
            leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            leaf_key = ec.generate_private_key(ec.SECP256R1())
        leaf = _issue_leaf(
            issuer_cert=ca_cert,
            issuer_key=ca_key,
            subject_key=leaf_key,
            common_name="future-client",
            not_before=now + timedelta(days=1),
            not_after=now + timedelta(days=30),
        )
        cert_path = tmp / f"{case.case_id}.cert"
        key_path = tmp / f"{case.case_id}.key"
        _write_pem(cert_path, _pem_cert(leaf))
        _write_pem(key_path, _pem_key(leaf_key))
        client_ctx = _client_context(
            ca_pem=material["ca"],
            client_cert=cert_path,
            client_key=key_path,
            ciphers=server_ciphers,
        )
        return probe_handshake(server_context=server_ctx, client_context=client_ctx)

    if case.case_id.endswith(".tls_1_1"):
 # Server is pinned to TLS 1.2; forcing TLS 1.1 on the client must fail.
        try:
            client_ctx = _client_context(
                ca_pem=material["ca"],
                client_cert=material["client_cert"],
                client_key=material["client_key"],
                ciphers=server_ciphers,
                minimum_version=ssl.TLSVersion.TLSv1_1,
                maximum_version=ssl.TLSVersion.TLSv1_1,
            )
        except ValueError as exc:
 # Some OpenSSL builds disable TLS 1.1 entirely — still a reject.
            return False, f"tls11_unavailable:{exc}"
        return probe_handshake(server_context=server_ctx, client_context=client_ctx)

    return False, "unknown_case"
