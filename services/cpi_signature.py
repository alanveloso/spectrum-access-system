"""Cryptographic verification of WInnForum ``cpiSignatureData`` (JWT split fields).

The harness (and WINNF-TS-0016) encode CPI-signed installation data as a compact
JWT whose three segments are carried as:

- ``protectedHeader``
- ``encodedCpiSignedData``
- ``digitalSignature``

Base64URL decoding alone is not validation: the signature must verify under the
injected CPI public key with an allowed algorithm (RS256 / ES256).
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

logger = logging.getLogger(__name__)

# WINNF response codes (protocol); never embed crypto library messages.
MISSING_PARAM = 102
INVALID_PARAM = 103

ALLOWED_ALGORITHMS: frozenset[str] = frozenset({"RS256", "ES256"})

# Reject certification timestamps too far in the future (clock skew / replay).
_DEFAULT_MAX_FUTURE_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class CpiVerifyResult:
    """Outcome of CPI signature verification (safe for callers; no crypto detail)."""

    ok: bool
    response_code: int | None = None
    payload: dict[str, Any] | None = None
    algorithm: str | None = None
    cpi_id: str | None = None


def b64url_decode(data: str) -> bytes:
    """Decode Base64URL (with or without padding). Raises ``ValueError`` on bad input."""
    if not isinstance(data, str) or not data:
        raise ValueError("empty")
    padding_chars = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding_chars)


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cpi_signed_data(cpi_signature: Mapping[str, Any]) -> dict[str, Any] | None:
    """Decode the JWT payload segment without verifying the signature.

 Prefer :func:`verify_cpi_signature` before trusting the returned object.
 """
    encoded = cpi_signature.get("encodedCpiSignedData")
    if not encoded or not isinstance(encoded, str):
        return None
    try:
        payload = json.loads(b64url_decode(encoded).decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def structural_cpi_error(cpi_signature: Mapping[str, Any] | None) -> int | None:
    """Return 102/103 for incomplete or undecodable structure; else ``None``."""
    if not cpi_signature:
        return None

    for field in ("digitalSignature", "encodedCpiSignedData", "protectedHeader"):
        value = cpi_signature.get(field)
        if field not in cpi_signature or value in (None, ""):
            return MISSING_PARAM
        if not isinstance(value, str):
            return INVALID_PARAM

    signed = decode_cpi_signed_data(cpi_signature)
    if signed is None:
        return INVALID_PARAM

    # WINNF CpiSignedData required members (harness always populates these).
    for required in ("fccId", "cbsdSerialNumber", "installationParam"):
        if required not in signed or signed.get(required) in (None, ""):
            return MISSING_PARAM
    if not isinstance(signed.get("installationParam"), dict):
        return INVALID_PARAM

    prof = signed.get("professionalInstallerData") or {}
    if not isinstance(prof, dict):
        return INVALID_PARAM
    if "cpiId" not in prof or prof.get("cpiId") in (None, ""):
        return MISSING_PARAM
    if "installCertificationTime" not in prof or prof.get("installCertificationTime") in (
        None,
        "",
    ):
        return MISSING_PARAM

    return None


def _parse_protected_header(protected_b64: str) -> dict[str, Any]:
    header = json.loads(b64url_decode(protected_b64).decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("header not object")
    return header


def _load_public_key(pem: str):
    key = serialization.load_pem_public_key(pem.encode("utf-8"))
    return key


def _verify_rs256(public_key, signing_input: bytes, signature: bytes) -> None:
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise InvalidSignature()
    public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())


def _verify_es256(public_key, signing_input: bytes, signature: bytes) -> None:
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise InvalidSignature()
    # JWT ES256 uses IEEE P1363 (r||s); cryptography expects DER.
    if len(signature) != 64:
        raise InvalidSignature()
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    der = encode_dss_signature(r, s)
    public_key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))


def _parse_install_cert_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("not a string")
    # Harness: ``%Y-%m-%dT%H:%M:%SZ``
    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=timezone.utc)
    raise ValueError("expected Zulu UTC")


def verify_cpi_signature(
    cpi_signature: Mapping[str, Any],
    *,
    public_key_pem: str | None,
    request_fcc_id: str,
    request_serial: str,
    now: datetime | None = None,
    max_future_skew: timedelta = _DEFAULT_MAX_FUTURE_SKEW,
) -> CpiVerifyResult:
    """Verify CPI JWT segments cryptographically and bind identity/fields.

 Failures always map to ``MISSING_PARAM`` / ``INVALID_PARAM`` without exposing
 underlying cryptographic exception text to protocol clients.
 """
    structural = structural_cpi_error(cpi_signature)
    if structural is not None:
        return CpiVerifyResult(ok=False, response_code=structural)

    protected = str(cpi_signature["protectedHeader"])
    payload_b64 = str(cpi_signature["encodedCpiSignedData"])
    signature_b64 = str(cpi_signature["digitalSignature"])

    try:
        header = _parse_protected_header(protected)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        logger.info("CPI protected header rejected")
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)

    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        logger.info("CPI algorithm rejected")
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)
    typ = header.get("typ")
    if typ is not None and typ != "JWT":
        logger.info("CPI protected typ rejected")
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)

    payload = decode_cpi_signed_data(cpi_signature)
    if payload is None:
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)

    prof = payload.get("professionalInstallerData") or {}
    if not isinstance(prof, dict):
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)
    cpi_id = prof.get("cpiId")
    if not cpi_id:
        return CpiVerifyResult(ok=False, response_code=MISSING_PARAM)

    try:
        certified_at = _parse_install_cert_time(prof.get("installCertificationTime"))
    except (ValueError, TypeError):
        logger.info("CPI installCertificationTime rejected")
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)

    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    if certified_at > clock + max_future_skew:
        logger.info("CPI installCertificationTime in the future")
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)

    signed_fcc = payload.get("fccId")
    signed_serial = payload.get("cbsdSerialNumber")
    if signed_fcc in (None, "") or signed_serial in (None, ""):
        return CpiVerifyResult(ok=False, response_code=MISSING_PARAM)
    if str(signed_fcc) != str(request_fcc_id):
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)
    if str(signed_serial) != str(request_serial):
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)
    install = payload.get("installationParam")
    if not isinstance(install, dict):
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)

    if not public_key_pem or not str(public_key_pem).strip():
        logger.info("CPI public key missing for cpiId")
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)

    try:
        public_key = _load_public_key(str(public_key_pem).strip())
        signature = b64url_decode(signature_b64)
        signing_input = f"{protected}.{payload_b64}".encode("ascii")
        if alg == "RS256":
            _verify_rs256(public_key, signing_input, signature)
        else:
            _verify_es256(public_key, signing_input, signature)
    except (ValueError, TypeError, InvalidSignature, OSError):
        # Never leak OpenSSL / cryptography detail into protocol responses.
        logger.info("CPI signature verification failed")
        return CpiVerifyResult(ok=False, response_code=INVALID_PARAM)

    return CpiVerifyResult(
        ok=True,
        payload=payload,
        algorithm=str(alg),
        cpi_id=str(cpi_id),
    )


def sign_cpi_payload(
    payload: Mapping[str, Any],
    private_key_pem: str,
    *,
    algorithm: str = "RS256",
) -> dict[str, str]:
    """Build a harness-compatible ``cpiSignatureData`` object (tests / tooling)."""
    if algorithm not in ALLOWED_ALGORITHMS:
        raise ValueError(f"unsupported algorithm: {algorithm}")

    header = {"alg": algorithm, "typ": "JWT"}
    protected = b64url_encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    payload_b64 = b64url_encode(
        json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{protected}.{payload_b64}".encode("ascii")
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None
    )

    if algorithm == "RS256":
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise TypeError("RS256 requires an RSA private key")
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    else:
        if not isinstance(private_key, ec.EllipticCurvePrivateKey):
            raise TypeError("ES256 requires an EC private key")
        der_sig = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        r, s = decode_dss_signature(der_sig)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")

    return {
        "protectedHeader": protected,
        "encodedCpiSignedData": payload_b64,
        "digitalSignature": b64url_encode(signature),
    }
