"""Pull external PAL / CPI / FSS databases injected via /admin/injectdata/database_url.

Sync semantics:
* validate checksum when provided (fail that URL; no partial publish);
* PAL full-replace (insert/update/remove absent);
* CPI ACTIVE upsert + revoke absent/inactive;
* SCHEDULED_DPA materializes activations via existing DPA domain;
* bump generation then mark CPAS reevaluation required (N+1 for next pipeline);
* per-URL failures are isolated; successful URLs remain committed only after
 their own validate→persist→generation→reeval mark completes.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import ssl
import xml.etree.ElementTree as ET
from typing import Any, cast

import httpx
from sqlalchemy.orm import Session

from config import get_settings
from models.models import AdminInjectedData, CpiUser

logger = logging.getLogger(__name__)

class DatabaseSyncError(RuntimeError):
    """Raised when a single database_url sync fails validation or apply."""


def _ssl_context() -> ssl.SSLContext:
    settings = get_settings()
    ca_cert = settings.resolved_ssl_ca_certs
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    if ca_cert.is_file():
        ctx.load_verify_locations(cafile=str(ca_cert))
    return ctx


def _http_get(url: str, *, auth: bool = False) -> bytes:
    """Fetch URL without following redirects (SSRF / open-redirect control)."""
    from services.ssrf import SsrfError, allow_lab_private_egress, assert_https_egress_url_allowed

    try:
        assert_https_egress_url_allowed(
            url, allow_lab_private=allow_lab_private_egress()
        )
    except SsrfError as exc:
        raise DatabaseSyncError(f"ssrf_blocked:{exc}") from exc

    settings = get_settings()
    kwargs: dict[str, Any] = {
        "verify": _ssl_context(),
        "timeout": settings.http_timeout_seconds,
        "follow_redirects": False,
    }
    if auth:
        user, password = settings.db_sync_basic_auth
        if not user or not password:
            raise DatabaseSyncError("db_sync_credentials_not_configured")
        kwargs["auth"] = (user, password)
    with httpx.Client(**kwargs) as client:
        resp = client.get(url)
        if resp.is_redirect:
            raise DatabaseSyncError(f"redirect_refused:{url}")
        resp.raise_for_status()
        max_bytes = int(settings.sas_max_request_body_bytes)
        if max_bytes > 0 and len(resp.content) > max_bytes:
            raise DatabaseSyncError("response_body_too_large")
        return resp.content


def _store_injection(db: Session, kind: str, payload: Any) -> None:
    db.add(
        AdminInjectedData(
            kind=kind,
            data_json=json.dumps(payload if payload is not None else {}),
        )
    )


def _verify_checksum(body: bytes, checksum: Any, *, label: str) -> None:
    from services.data_injection_service import verify_optional_checksum

    if checksum is None or checksum == "":
        # Optional by Admin contract; document in evidence (not fail-closed).
        return
    if not verify_optional_checksum(body, checksum):
        raise DatabaseSyncError(f"checksum_mismatch:{label}")


def _mark_reeval(db: Session, reason: str) -> None:
    from services.cpas_reevaluation import mark_cpas_reevaluation_required
    from services.data_injection_service import get_injection_generations
    from services.federal_db_service import get_sync_meta

    mark_cpas_reevaluation_required(
        db,
        reason=reason,
        generation={
            "federal": get_sync_meta(db),
            "injection": get_injection_generations(db),
        },
    )


def sync_injected_database_urls(db: Session) -> dict[str, Any]:
    """Fetch every injected database_url during CPAS / daily activities.

 Returns a report with ok/failed URL counts. Does not abort the whole CPAS
 pipeline on a single URL failure (logged); successful syncs are durable.
 """
    rows = list(db.query(AdminInjectedData).filter_by(kind="database_url").all())
    report: dict[str, Any] = {"ok": 0, "failed": 0, "errors": []}
    for row in rows:
        try:
            meta = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            report["failed"] += 1
            report["errors"].append("invalid_database_url_json")
            continue
        db_type = (meta.get("type") or "").upper()
        url = meta.get("url") or ""
        if not url:
            continue
        try:
            if db_type == "PAL":
                _sync_pal(db, url, checksum=meta.get("checksum"))
            elif db_type == "CPI":
                _sync_cpi(db, url, checksum=meta.get("checksum"))
            elif db_type == "EXCLUSION_ZONE":
                body = _http_get(url, auth=False)
                _verify_checksum(body, meta.get("checksum"), label="EXCLUSION_ZONE")
                _apply_exclusion_zone_kml(db, body)
                _mark_reeval(db, "exz_sync")
            elif db_type == "SCHEDULED_DPA":
                body = _http_get(url, auth=False)
                _verify_checksum(body, meta.get("checksum"), label="SCHEDULED_DPA")
                _apply_scheduled_dpa(db, body)
                _mark_reeval(db, "scheduled_dpa_sync")
            elif db_type == "FSS":
                body = _http_get(url, auth=True)
                _verify_checksum(body, meta.get("checksum"), label="FSS")
                payload = json.loads(body.decode("utf-8"))
                from services.federal_db_service import replace_fss_from_federal_payload

                replace_fss_from_federal_payload(db, payload)
                _mark_reeval(db, "fss_sync")
            elif db_type == "GWBL":
                body = _http_get(url, auth=False)
                _verify_checksum(body, meta.get("checksum"), label="GWBL")
                from services.federal_db_service import replace_gwbl_from_zip

                replace_gwbl_from_zip(db, body)
                _mark_reeval(db, "gwbl_sync")
            else:
                continue
            db.commit()
            report["ok"] += 1
        except Exception as exc:
            db.rollback()
            logger.exception("Failed syncing database_url type=%s url=%s", db_type, url)
            report["failed"] += 1
            report["errors"].append(f"{db_type}:{type(exc).__name__}:{exc}")

    return report


def _parse_freq_range_mhz(text: str | None) -> list[dict[str, int]]:
    """Parse '3600-3650' into Hz ranges."""
    if not text:
        return []
    out: list[dict[str, int]] = []
    for part in re.split(r"[;,]", text):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*$", part)
        if not m:
            continue
        lo_mhz, hi_mhz = float(m.group(1)), float(m.group(2))
        out.append(
            {
                "lowFrequency": int(lo_mhz * 1_000_000),
                "highFrequency": int(hi_mhz * 1_000_000),
            }
        )
    return out


def _parse_kml_ring(text: str | None) -> list[list[float]]:
    ring: list[list[float]] = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            try:
                ring.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    if len(ring) >= 3 and ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring


def _apply_exclusion_zone_kml(db: Session, body: bytes) -> None:
    """Replace exclusion_zone records with polygons parsed from federal EXZ KML."""
    from services.exclusion_zone_service import KIND_EXCLUSION_ZONE
    from services.federal_db_service import bump_sync_meta

    text = body.decode("utf-8", errors="replace")
    root = ET.fromstring(text)
    db.query(AdminInjectedData).filter_by(kind=KIND_EXCLUSION_ZONE).delete()

    for pm in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
        coords_el = pm.find(
            ".//{http://www.opengis.net/kml/2.2}outerBoundaryIs"
            "/{http://www.opengis.net/kml/2.2}LinearRing"
            "/{http://www.opengis.net/kml/2.2}coordinates"
        )
        ring = _parse_kml_ring(coords_el.text if coords_el is not None else None)
        if len(ring) < 4:
            continue
        freq_text = None
        for data in pm.findall(".//{http://www.opengis.net/kml/2.2}Data"):
            if data.get("name") == "freqRangeMhz":
                val = data.find("{http://www.opengis.net/kml/2.2}value")
                if val is not None:
                    freq_text = val.text
        freq_ranges = _parse_freq_range_mhz(freq_text)
        if not freq_ranges:
            freq_ranges = [
                {"lowFrequency": 3_550_000_000, "highFrequency": 3_650_000_000}
            ]
        payload = {
            "zone": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": [ring]},
                        "properties": {},
                    }
                ],
            },
            "frequencyRanges": freq_ranges,
        }
        _store_injection(db, KIND_EXCLUSION_ZONE, payload)

    bump_sync_meta(db, "exz")


def _scheduled_dpa_looks_like_kml(text: str) -> bool:
    """Deterministic format probe: XML/KML vs JSON object/array."""
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        return False
    if stripped[0] in "{[":
        return False
    if stripped[0] == "<":
        return True
    return False


def _apply_scheduled_dpa_json(
    db: Session,
    *,
    text: str,
    dpa_svc: Any,
) -> list[tuple[str, Any]]:
    """Existing JSON activation contract (catalogue must already contain dpaId)."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DatabaseSyncError("scheduled_dpa_invalid_json") from exc

    if isinstance(payload, dict) and isinstance(payload.get("activations"), list):
        activations = [a for a in payload["activations"] if isinstance(a, dict)]
    elif isinstance(payload, list):
        activations = [a for a in payload if isinstance(a, dict)]
    elif isinstance(payload, dict) and payload.get("dpaId"):
        activations = [payload]
    else:
        raise DatabaseSyncError("scheduled_dpa_missing_activations")
    if not activations:
        raise DatabaseSyncError("scheduled_dpa_empty_activations")

    channels: list[tuple[str, Any]] = []
    for act in activations:
        dpa_id = str(act.get("dpaId") or "").strip()
        fr = act.get("frequencyRange") or {}
        try:
            low = int(fr["lowFrequency"])
            high = int(fr["highFrequency"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DatabaseSyncError("scheduled_dpa_invalid_frequency") from exc
        if not dpa_id or low >= high:
            raise DatabaseSyncError("scheduled_dpa_invalid_activation")
        definition = dpa_svc.get_catalogue_definition(db, dpa_id)
        if definition is None:
            raise DatabaseSyncError(f"scheduled_dpa_unknown_dpaId:{dpa_id}")
        freq = dpa_svc.FrequencyRange(low, high)
        if not dpa_svc._channel_in_definition(definition, freq):
            raise DatabaseSyncError(
                f"scheduled_dpa_channel_not_in_catalogue:{dpa_id}"
            )
        channels.append((dpa_id, freq))

    db.query(AdminInjectedData).filter_by(kind="scheduled_dpa").delete()
    _store_injection(
        db,
        "scheduled_dpa",
        {"raw": text, "format": "json", "activations": activations},
    )
    return channels


def _apply_scheduled_dpa_kml(
    db: Session,
    *,
    body: bytes,
    text: str,
    dpa_svc: Any,
) -> list[tuple[str, Any]]:
    """Official portal SCHEDULED_DPA KML → catalogue upsert + channel activations."""
    try:
        definitions = dpa_svc.parse_dpa_kml_bytes(body, source="scheduled_dpa_kml")
    except ET.ParseError as exc:
        raise DatabaseSyncError("scheduled_dpa_invalid_kml") from exc
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise DatabaseSyncError("scheduled_dpa_invalid_kml") from exc
    if not definitions:
        raise DatabaseSyncError("scheduled_dpa_kml_empty")

    dpa_svc.upsert_catalogue_definitions(
        db, definitions, source_label="scheduled_dpa_kml"
    )

    channels: list[tuple[str, Any]] = []
    activation_records: list[dict[str, Any]] = []
    for definition in definitions:
        chans = dpa_svc.channelize(definition.freq_low_hz, definition.freq_high_hz)
        if not chans:
            raise DatabaseSyncError(
                f"scheduled_dpa_kml_no_channels:{definition.dpa_id}"
            )
        for freq in chans:
            channels.append((definition.dpa_id, freq))
            activation_records.append(
                {
                    "dpaId": definition.dpa_id,
                    "frequencyRange": freq.as_dict(),
                }
            )

    db.query(AdminInjectedData).filter_by(kind="scheduled_dpa").delete()
    _store_injection(
        db,
        "scheduled_dpa",
        {
            "raw": text,
            "format": "kml",
            "dpaIds": [d.dpa_id for d in definitions],
            "activations": activation_records,
        },
    )
    return channels


def _apply_scheduled_dpa(db: Session, body: bytes) -> None:
    """Materialize SCHEDULED_DPA JSON activations or official portal KML.

 JSON shapes (unchanged):
 * ``{"activations":[{"dpaId":...,"frequencyRange":{...}}, ...]}``
 * ``[{"dpaId":...,"frequencyRange":{...}}, ...]``
 * ``{"dpaId":...,"frequencyRange":{...}}``

 KML: NTIA/P-DPA placemarks (same semantic parser as ``load_dpas``).
 Format selection is deterministic from the payload prefix (JSON vs XML).
 """
    from services import dpa_service as dpa_svc
    from services.federal_db_service import bump_sync_meta

    text = body.decode("utf-8")
    if _scheduled_dpa_looks_like_kml(text):
        channels = _apply_scheduled_dpa_kml(
            db, body=body, text=text, dpa_svc=dpa_svc
        )
    else:
        channels = _apply_scheduled_dpa_json(db, text=text, dpa_svc=dpa_svc)

    # Drop previous scheduled activations only (retain non-scheduled).
    for row in list(
        db.query(AdminInjectedData).filter_by(kind=dpa_svc.FLAG_DPA_ACTIVE).all()
    ):
        try:
            data = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        if data.get("source") == "scheduled_dpa":
            db.delete(row)

    for dpa_id, freq in channels:
        dpa_svc._upsert_activation(
            db,
            dpa_id=dpa_id,
            freq=freq,
            movelist=[],
            source="scheduled_dpa",
        )

    dpa_svc.refresh_or_fail_closed_movelists(db, channels)

    bump_sync_meta(db, "dpa")


def _sync_pal(db: Session, url: str, *, checksum: Any = None) -> None:
    from services.pal_service import replace_pal_records

    body = _http_get(url, auth=False)
    _verify_checksum(body, checksum, label="PAL")
    try:
        records = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise DatabaseSyncError("pal_invalid_json") from exc
    replace_pal_records(db, records, commit=False)
    _mark_reeval(db, "pal_sync")


def _sync_cpi(db: Session, index_url: str, *, checksum: Any = None) -> None:
    from services.data_injection_service import bump_injection_generation

    body = _http_get(index_url, auth=False)
    _verify_checksum(body, checksum, label="CPI")
    raw = body.decode("utf-8")
    reader = csv.DictReader(io.StringIO(raw))
    seen_active: set[str] = set()
    for row in reader:
        cpi_id = (row.get("cpiId") or "").strip()
        status = (row.get("status") or "").strip().upper()
        key_url = (row.get("publicKeyIdentifier") or "").strip()
        if not cpi_id:
            continue
        if status != "ACTIVE":
            # Revoke / skip inactive — delete local key material.
            existing = db.query(CpiUser).filter_by(cpi_id=cpi_id).first()
            if existing:
                db.delete(existing)
            continue
        if not key_url:
            raise DatabaseSyncError(f"cpi_missing_public_key:{cpi_id}")
        try:
            public_key = _http_get(key_url, auth=False).decode("utf-8")
        except Exception as exc:
            raise DatabaseSyncError(f"cpi_key_fetch_failed:{cpi_id}") from exc
        if not public_key.strip():
            raise DatabaseSyncError(f"cpi_empty_public_key:{cpi_id}")
        existing = db.query(CpiUser).filter_by(cpi_id=cpi_id).first()
        if existing:
            existing.cpi_public_key = public_key
            existing.cpi_name = existing.cpi_name or cpi_id
        else:
            db.add(
                CpiUser(
                    cpi_id=cpi_id,
                    cpi_name=cpi_id,
                    cpi_public_key=public_key,
                )
            )
        seen_active.add(cpi_id)

    # Remove CPI users absent from the ACTIVE feed (full reconcile).
    for cpi_user in cast(list[CpiUser], list(db.query(CpiUser).all())):
        if cpi_user.cpi_id not in seen_active:
            db.delete(cpi_user)

    bump_injection_generation(db, "cpi")
    _mark_reeval(db, "cpi_sync")
