"""Generate ephemeral mTLS material for local/CI smoke (RSA + ECC + CA + CRL + client).

Harness-agnostic filenames under CERTS_DIR. No fixture device IDs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from services.cert_layout import REQUIRED_DIRS, REQUIRED_FILES


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _write_key(path: Path, key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def generate_dev_certs(out_dir: Path, *, force: bool = False) -> dict[str, Path]:
    """Create a minimal valid CERTS_DIR layout plus a client cert/key for probes."""
    out_dir = out_dir.resolve()
    if out_dir.exists() and any(out_dir.iterdir()) and not force:
        raise FileExistsError(
            f"refusing to overwrite non-empty certs dir {out_dir}; pass force=True/--force"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    crl_dir = out_dir / "crl"
    crl_dir.mkdir(parents=True, exist_ok=True)

    now = _utc_now()
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("sas-core-dev-ca"))
        .issuer_name(_name("sas-core-dev-ca"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    def _sign_server(
        *,
        common_name: str,
        key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    ) -> x509.Certificate:
        return (
            x509.CertificateBuilder()
            .subject_name(_name(common_name))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=1))
            .not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_cert = _sign_server(common_name="sas-core-dev-rsa", key=rsa_key)

    ecc_key = ec.generate_private_key(ec.SECP256R1())
    ecc_cert = _sign_server(common_name="sas-core-dev-ecc", key=ecc_key)

    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("sas-core-dev-client"))
        .issuer_name(ca_cert.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_cert.subject)
        .last_update(now - dt.timedelta(minutes=1))
        .next_update(now + dt.timedelta(days=30))
        .sign(ca_key, hashes.SHA256())
    )

    paths = {
        "ca.cert": out_dir / "ca.cert",
        "server.cert": out_dir / "server.cert",
        "server.key": out_dir / "server.key",
        "server-ecc.cert": out_dir / "server-ecc.cert",
        "server-ecc.key": out_dir / "server-ecc.key",
        "crl": crl_dir / "empty.crl.pem",
        "client.cert": out_dir / "client.cert",
        "client.key": out_dir / "client.key",
    }
    _write_cert(paths["ca.cert"], ca_cert)
    _write_cert(paths["server.cert"], rsa_cert)
    _write_key(paths["server.key"], rsa_key)
    _write_cert(paths["server-ecc.cert"], ecc_cert)
    _write_key(paths["server-ecc.key"], ecc_key)
    paths["crl"].write_bytes(crl.public_bytes(serialization.Encoding.PEM))
    _write_cert(paths["client.cert"], client_cert)
    _write_key(paths["client.key"], client_key)

    for name in REQUIRED_FILES:
        if not (out_dir / name).is_file():
            raise RuntimeError(f"missing required file after generation: {name}")
    for name in REQUIRED_DIRS:
        if not (out_dir / name).is_dir():
            raise RuntimeError(f"missing required dir after generation: {name}")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output CERTS_DIR (created if missing).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow overwriting a non-empty output directory.",
    )
    args = parser.parse_args(argv)
    generate_dev_certs(args.out, force=args.force)
    print(f"generated certs under {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
