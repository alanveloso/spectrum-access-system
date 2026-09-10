"""Grant renewal rules (WINNF grantRenew / GRA.13 PAL license cap).

Persists authorization context at grant creation and applies renewal without
extending past PAL ``licenseExpiration``, and without renewing terminal or
already-expired grants. Federal/CPAS protection drift is re-stamped on success;
callers must still run interference checks before invoking renewal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from models.models import Grant, PalRecord
from primitives.authorization import FixedWindow
from primitives.time import TimeInterval
from services.grant_service import DEFAULT_GRANT_DURATION_SEC
from services.lifecycle import (
    TERMINAL_GRANT_STATES,
    _grant_expired,
    resolve_grant_state,
)

INVALID_PARAM = 103

AUTH_CONTEXT_KEY = "auth_context"


@dataclass(frozen=True)
class RenewalResult:
    ok: bool
    response_code: int
    new_expire: datetime | None = None
    detail: str | None = None


def _parse_utc_z(value: str | datetime | None) -> datetime | None:
    from services.clock import ensure_utc

    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value).replace(microsecond=0)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
            return ensure_utc(parsed).replace(microsecond=0)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return ensure_utc(parsed).replace(microsecond=0)
    except (TypeError, ValueError):
        return None


def build_auth_context(
    *,
    channel_type: str,
    pal_license_exp: datetime | None = None,
    pal_id: str | None = None,
) -> dict[str, Any]:
    """Authorization origin stored inside ``grant.grant_json``."""
    ctx: dict[str, Any] = {"channelType": channel_type}
    if pal_id:
        ctx["palId"] = pal_id
    if pal_license_exp is not None:
        ctx["palLicenseExpiration"] = pal_license_exp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ctx


def load_grant_meta(grant: Grant) -> dict[str, Any]:
    try:
        data = json.loads(grant.grant_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_auth_context(grant: Grant) -> dict[str, Any]:
    meta = load_grant_meta(grant)
    ctx = meta.get(AUTH_CONTEXT_KEY)
    if isinstance(ctx, dict):
        return ctx
    # Legacy grants: derive minimal context from the ORM column.
    return {"channelType": grant.channel_type or "GAA"}


def resolve_pal_license_cap(db: Session, grant: Grant) -> datetime | None:
    """Live PAL licenseExpiration when possible; else the value stored at grant time.

 Returns ``None`` when the PAL row exists but is not ``VALID`` (caller should
 treat that as a hard renew failure), or when no usable expiration is known.
 """
    auth = load_auth_context(grant)
    pal_id = auth.get("palId")
    if pal_id:
        row = db.query(PalRecord).filter_by(pal_id=str(pal_id)).first()
        if row is not None:
            status = (row.license_status or "VALID").upper()
            if status != "VALID":
                return None
            live = _parse_utc_z(row.license_expiration)
            if live is not None:
                return live
            # Prefer JSON record if column empty.
            try:
                payload = json.loads(row.record_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                live = _parse_utc_z(
                    (payload.get("license") or {}).get("licenseExpiration")
                )
                if live is not None:
                    return live
    return _parse_utc_z(auth.get("palLicenseExpiration"))


def pal_license_status(db: Session, grant: Grant) -> str | None:
    """Return the live PAL ``licenseStatus``, or None when no PAL row is linked."""
    auth = load_auth_context(grant)
    pal_id = auth.get("palId")
    if not pal_id:
        return None
    row = db.query(PalRecord).filter_by(pal_id=str(pal_id)).first()
    if row is None:
        return None
    return str(row.license_status or "VALID").upper()


def protection_stamp_drifted(db: Session, grant: Grant) -> bool:
    """True when federal/CPAS generation counters moved since grant creation."""
    from services.federal_db_service import grant_sync_stamp

    meta = load_grant_meta(grant)
    stamp = grant_sync_stamp(db)
    mapping = (
        ("fss_gen", "fss"),
        ("gwbl_gen", "gwbl"),
        ("exz_gen", "exz"),
        ("dpa_gen", "dpa"),
    )
    for stored_key, stamp_key in mapping:
        if int(meta.get(stored_key) or 0) != int(stamp.get(stamp_key) or 0):
            return True
    return False


def refresh_protection_stamp(db: Session, grant: Grant) -> None:
    """Update stored federal generation counters after a successful renew."""
    from services.federal_db_service import grant_sync_stamp

    meta = load_grant_meta(grant)
    stamp = grant_sync_stamp(db)
    meta["fss_gen"] = stamp.get("fss", 0)
    meta["gwbl_gen"] = stamp.get("gwbl", 0)
    meta["exz_gen"] = stamp.get("exz", 0)
    meta["dpa_gen"] = stamp.get("dpa", 0)
    grant.grant_json = json.dumps(meta)


def compute_renewal(
    db: Session,
    grant: Grant,
    *,
    now: datetime | None = None,
) -> RenewalResult:
    """
 Decide the renewed ``grantExpireTime``.

 Does not mutate ``grant``. Callers must have already enforced protection
 checks (DPA/federal/CPAS); this function refuses terminal/expired grants and
 caps PAL renewals at ``licenseExpiration``.
 """
    from services.clock import ensure_utc, utc_now

    wall = ensure_utc(now or utc_now()).replace(microsecond=0)
    state = resolve_grant_state(grant, now=wall)
    if state in TERMINAL_GRANT_STATES or grant.terminated:
        return RenewalResult(
            ok=False, response_code=INVALID_PARAM, detail="terminal_or_expired"
        )
    if _grant_expired(grant, now=wall):
        return RenewalResult(
            ok=False, response_code=INVALID_PARAM, detail="grant_already_expired"
        )

    default = wall + timedelta(seconds=DEFAULT_GRANT_DURATION_SEC)
    auth = load_auth_context(grant)
    channel = str(auth.get("channelType") or grant.channel_type or "GAA").upper()

    if channel == "PAL":
        status = pal_license_status(db, grant)
        if status is not None and status != "VALID":
            return RenewalResult(
                ok=False, response_code=INVALID_PARAM, detail="pal_license_invalid"
            )
        cap = resolve_pal_license_cap(db, grant)
        if cap is None:
            # PAL grant without a usable licenseExpiration cannot be extended safely.
            return RenewalResult(
                ok=False, response_code=INVALID_PARAM, detail="pal_license_missing"
            )
        cap = ensure_utc(cap).replace(microsecond=0)
        if cap <= wall:
            return RenewalResult(
                ok=False, response_code=INVALID_PARAM, detail="pal_license_expired"
            )
        requested = TimeInterval.from_datetimes(wall, default)
        cap_window = FixedWindow(window=TimeInterval.from_datetimes(wall, cap))
        new_expire = default if cap_window.allows_validity(requested) else cap
    else:
        new_expire = default

    return RenewalResult(ok=True, response_code=0, new_expire=new_expire)


def apply_renewal(
    db: Session,
    grant: Grant,
    *,
    now: datetime | None = None,
) -> RenewalResult:
    """Compute and persist renewal; refresh protection stamp when data drifted."""
    result = compute_renewal(db, grant, now=now)
    if not result.ok or result.new_expire is None:
        return result
    grant.grant_expire_time = result.new_expire
    # Recalculate/persist protection generations when federal/CPAS data moved
    # (or always refresh so renewals carry a current stamp).
    drifted = protection_stamp_drifted(db, grant)
    refresh_protection_stamp(db, grant)
    # Keep auth_context palLicenseExpiration in sync when live PAL was used.
    meta = load_grant_meta(grant)
    auth = meta.get(AUTH_CONTEXT_KEY)
    if isinstance(auth, dict) and str(auth.get("channelType") or "").upper() == "PAL":
        cap = resolve_pal_license_cap(db, grant)
        if cap is not None:
            auth["palLicenseExpiration"] = cap.strftime("%Y-%m-%dT%H:%M:%SZ")
            meta[AUTH_CONTEXT_KEY] = auth
            grant.grant_json = json.dumps(meta)
    if drifted:
        return RenewalResult(
            ok=True,
            response_code=0,
            new_expire=result.new_expire,
            detail="protections_restamped",
        )
    return result
