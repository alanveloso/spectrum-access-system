"""mTLS identity binding for CBSD and Domain Proxy roles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import (
    ExtendedKeyUsageOID,
    NameOID,
    ObjectIdentifier,
)
from fastapi.testclient import TestClient

from main import app
from services.cbsd_auth import (
    CbsdClientRole,
    authorize_cbsd_operation,
    cbsd_certificate_mismatch,
    classify_cbsd_client_role,
)
from services.error_handlers import CERT_ERROR, INVALID_VALUE
from services.mtls_auth import (
    OID_ROLE_CBSD,
    OID_ROLE_DOMAIN_PROXY,
    OID_ROLE_INSTALLER,
    OID_ROLE_SAS,
    sha1_fingerprint_colon,
)
from tests.fixtures.factories import make_cbsd, make_grant

client = TestClient(app)
SUCCESS = 0


@pytest.fixture(autouse=True)
def _isolate_from_host_ca(monkeypatch: pytest.MonkeyPatch):
    """Self-signed mock peer certs must not be judged against host CERTS_DIR CA."""
    monkeypatch.setattr(
        "services.cbsd_auth.load_runtime_trust_context",
        lambda: (None, None),
    )


def _build_cert(
    *,
    role_oid: ObjectIdentifier,
    key_type: str = "rsa",
    common_name: str = "test-client",
    with_key_usage: bool = True,
    with_client_auth: bool = True,
    not_before_delta: timedelta = timedelta(minutes=1),
    not_after_delta: timedelta = timedelta(days=1),
    extra_policies: list[ObjectIdentifier] | None = None,
) -> x509.Certificate:
    """Issue a short-lived self-signed cert with a WInnForum role policy OID."""
    if key_type == "ecc":
        key = ec.generate_private_key(ec.SECP256R1())
    elif key_type == "rsa":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    else:
        raise ValueError(f"unsupported key_type {key_type!r}")

    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    policies = [role_oid, *(extra_policies or [])]
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - not_before_delta)
        .not_valid_after(now + not_after_delta)
        .add_extension(
            x509.CertificatePolicies(
                [
                    x509.PolicyInformation(
                        policy_identifier=oid, policy_qualifiers=None
                    )
                    for oid in policies
                ]
            ),
            critical=False,
        )
    )
    if with_client_auth:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
    if with_key_usage:
        builder = builder.add_extension(
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
    return builder.sign(key, hashes.SHA256())


def _request_with_cert(cert: x509.Certificate | None) -> MagicMock:
    req = MagicMock()
    if cert is None:
        req.scope = {}
        return req
    from cryptography.hazmat.primitives.serialization import Encoding

    der = cert.public_bytes(Encoding.DER)
    ssl_object = MagicMock()
    ssl_object.getpeercert.return_value = der
    transport = MagicMock()
    transport.get_extra_info.return_value = ssl_object
    req.scope = {"transport": transport}
    return req


@pytest.mark.parametrize("key_type", ["rsa", "ecc"])
@pytest.mark.parametrize(
    "role_oid,expected",
    [
        (OID_ROLE_CBSD, CbsdClientRole.CBSD),
        (OID_ROLE_DOMAIN_PROXY, CbsdClientRole.DOMAIN_PROXY),
        (OID_ROLE_SAS, CbsdClientRole.SAS),
        (OID_ROLE_INSTALLER, CbsdClientRole.INSTALLER),
    ],
)
def test_classify_roles_rsa_and_ecc(key_type, role_oid, expected):
    cert = _build_cert(role_oid=role_oid, key_type=key_type)
    assert classify_cbsd_client_role(cert) == expected
    assert ":" in sha1_fingerprint_colon(cert)


def test_authorize_allows_cbsd_and_domain_proxy():
    cbsd_cert = _build_cert(role_oid=OID_ROLE_CBSD, key_type="rsa")
    dp_cert = _build_cert(role_oid=OID_ROLE_DOMAIN_PROXY, key_type="ecc")

    cbsd_ctx = authorize_cbsd_operation(_request_with_cert(cbsd_cert))
    assert cbsd_ctx.allowed is True
    assert cbsd_ctx.role is CbsdClientRole.CBSD

    dp_ctx = authorize_cbsd_operation(_request_with_cert(dp_cert))
    assert dp_ctx.allowed is True
    assert dp_ctx.role is CbsdClientRole.DOMAIN_PROXY


def test_authorize_rejects_when_runtime_ca_does_not_chain(monkeypatch: pytest.MonkeyPatch):
    """With a configured trust root, a self-signed ROLE_CBSD leaf must be denied."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "runtime-ca")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "runtime-ca")]))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    monkeypatch.setattr(
        "services.cbsd_auth.load_runtime_trust_context",
        lambda: ([ca_cert], None),
    )
    leaf = _build_cert(role_oid=OID_ROLE_CBSD, key_type="rsa")
    ctx = authorize_cbsd_operation(_request_with_cert(leaf))
    assert ctx.allowed is False
    assert ctx.denial_code == CERT_ERROR


@pytest.mark.parametrize(
    "role_oid",
    [OID_ROLE_SAS, OID_ROLE_INSTALLER],
)
def test_authorize_rejects_wrong_role(role_oid):
    cert = _build_cert(role_oid=role_oid, key_type="rsa")
    ctx = authorize_cbsd_operation(_request_with_cert(cert))
    assert ctx.allowed is False
    assert ctx.denial_code == CERT_ERROR


def test_authorize_tls_without_peer_cert_denied():
    """TLS transport present but no client cert → 104 (not TestClient bypass)."""
    ssl_object = MagicMock()
    ssl_object.getpeercert.return_value = None
    transport = MagicMock()
    transport.get_extra_info.return_value = ssl_object
    req = MagicMock()
    req.scope = {"transport": transport}
    ctx = authorize_cbsd_operation(req)
    assert ctx.allowed is False
    assert ctx.denial_code == CERT_ERROR


def test_authorize_rejects_unknown_role():
    # CertificatePolicies extension omitted → UNKNOWN → denied.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "no-role")]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    assert classify_cbsd_client_role(cert) is CbsdClientRole.UNKNOWN
    ctx = authorize_cbsd_operation(_request_with_cert(cert))
    assert ctx.allowed is False
    assert ctx.denial_code == CERT_ERROR


def test_heartbeat_wrong_cert_does_not_echo_on_unknown_grant(db_session):
    owner = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"
    other = "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44"
    cbsd = make_cbsd(db_session, certificate_hash=owner)

    from services.heartbeat_service import process_heartbeat

    hbt = process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": "missing-grant",
                "operationState": "GRANTED",
            }
        ],
        certificate_hash=other,
    )
    assert hbt[0]["response"]["responseCode"] == INVALID_VALUE
    assert "cbsdId" not in hbt[0]
    assert "grantId" not in hbt[0]


def test_registration_rejects_certificate_takeover(db_session):
    owner = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"
    other = "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44"

    from models.models import FccIdRecord, UserIdRecord
    from services.registration_service import _make_cbsd_id, process_registration

    fcc_id = "fccTake1"
    serial = "snTake1"
    user_id = "user_take_1"
    db_session.add(FccIdRecord(fcc_id=fcc_id, fcc_max_eirp=47))
    db_session.add(UserIdRecord(user_id=user_id))
    db_session.commit()

    payload = {
        "fccId": fcc_id,
        "cbsdSerialNumber": serial,
        "userId": user_id,
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "installationParam": {
            "latitude": 39.1,
            "longitude": -77.1,
            "height": 3.0,
            "heightType": "AGL",
            "indoorDeployment": True,
        },
    }
    first = process_registration(db_session, [payload], certificate_hash=owner)
    assert first[0]["response"]["responseCode"] == SUCCESS
    assert first[0]["cbsdId"] == _make_cbsd_id(fcc_id, serial)

    # Foreign certificate must not take over the bound cbsdId.
    resp = process_registration(db_session, [payload], certificate_hash=other)
    assert resp[0]["response"]["responseCode"] == INVALID_VALUE
    assert "cbsdId" not in resp[0]

    from models.models import Cbsd

    row = db_session.query(Cbsd).filter_by(cbsd_id=_make_cbsd_id(fcc_id, serial)).first()
    assert row is not None
    assert row.certificate_hash == owner


def test_certificate_mismatch_blocks_cross_cbsd_use(db_session):
    owner = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"
    other = "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44"
    cbsd = make_cbsd(db_session, certificate_hash=owner)
    assert cbsd_certificate_mismatch(cbsd, other) is True
    assert cbsd_certificate_mismatch(cbsd, owner) is False
    assert cbsd_certificate_mismatch(cbsd, owner.lower()) is False


def test_domain_proxy_fingerprint_covers_multiple_cbsds(db_session):
    """Same Domain Proxy fingerprint may own many cbsdIds; another cert may not."""
    dp_hash = sha1_fingerprint_colon(
        _build_cert(role_oid=OID_ROLE_DOMAIN_PROXY, key_type="ecc")
    )
    foreign = sha1_fingerprint_colon(
        _build_cert(role_oid=OID_ROLE_CBSD, key_type="rsa")
    )
    a = make_cbsd(db_session, certificate_hash=dp_hash)
    b = make_cbsd(db_session, certificate_hash=dp_hash)
    assert cbsd_certificate_mismatch(a, dp_hash) is False
    assert cbsd_certificate_mismatch(b, dp_hash) is False
    assert cbsd_certificate_mismatch(a, foreign) is True


def test_heartbeat_relinquishment_deregistration_reject_wrong_cert(db_session):
    owner = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"
    other = "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44"
    cbsd = make_cbsd(db_session, certificate_hash=owner)
    grant = make_grant(db_session, cbsd)

    from services.deregistration_service import process_deregistration
    from services.heartbeat_service import process_heartbeat
    from services.relinquishment_service import process_relinquishment

    hbt = process_heartbeat(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "grantId": grant.grant_id,
                "operationState": "AUTHORIZED",
            }
        ],
        certificate_hash=other,
    )
    assert hbt[0]["response"]["responseCode"] == INVALID_VALUE
    assert "cbsdId" not in hbt[0]
    assert "grantId" not in hbt[0]

    rlq = process_relinquishment(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id}],
        certificate_hash=other,
    )
    assert rlq[0]["response"]["responseCode"] == INVALID_VALUE
    assert "cbsdId" not in rlq[0]

    drg = process_deregistration(
        db_session,
        [{"cbsdId": cbsd.cbsd_id}],
        certificate_hash=other,
    )
    assert drg[0]["response"]["responseCode"] == INVALID_VALUE
    assert "cbsdId" not in drg[0]
    from models.models import Cbsd

    assert db_session.query(Cbsd).filter_by(cbsd_id=cbsd.cbsd_id).first() is not None


def test_relinquishment_accepts_matching_cert(db_session):
    owner = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"
    cbsd = make_cbsd(db_session, certificate_hash=owner)
    grant = make_grant(db_session, cbsd)

    from services.relinquishment_service import process_relinquishment

    rlq = process_relinquishment(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "grantId": grant.grant_id}],
        certificate_hash=owner,
    )
    assert rlq[0]["response"]["responseCode"] == SUCCESS
    assert rlq[0]["cbsdId"] == cbsd.cbsd_id
    assert rlq[0]["grantId"] == grant.grant_id


def test_route_rejects_sas_role_with_104(monkeypatch):
    sas_cert = _build_cert(role_oid=OID_ROLE_SAS, key_type="rsa")

    def _fake_load(_request):
        return sas_cert

    monkeypatch.setattr(
        "services.cbsd_auth.load_client_certificate", _fake_load
    )
    response = client.post(
        "/v1.2/heartbeat",
        json={
            "heartbeatRequest": [
                {
                    "cbsdId": "c-1",
                    "grantId": "g-1",
                    "operationState": "GRANTED",
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["heartbeatResponse"][0]["response"]["responseCode"] == CERT_ERROR


def test_siq_and_grant_still_reject_cross_cert(db_session):
    owner = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"
    other = "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44"
    cbsd = make_cbsd(db_session, certificate_hash=owner)

    from services.grant_service import process_grant
    from services.spectrum_inquiry_service import process_spectrum_inquiry

    siq = process_spectrum_inquiry(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "inquiredSpectrum": [
                    {"lowFrequency": 3550_000_000, "highFrequency": 3700_000_000}
                ],
            }
        ],
        certificate_hash=other,
    )
    assert siq[0]["response"]["responseCode"] == INVALID_VALUE
    assert "cbsdId" not in siq[0]

    gra = process_grant(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": {
                    "maxEirp": 20,
                    "operationFrequencyRange": {
                        "lowFrequency": 3550_000_000,
                        "highFrequency": 3560_000_000,
                    },
                },
            }
        ],
        certificate_hash=other,
    )
    assert gra[0]["response"]["responseCode"] == INVALID_VALUE
    assert "cbsdId" not in gra[0]
