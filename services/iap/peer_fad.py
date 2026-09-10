"""Convert frozen peer FAD CBSD records into IAP ``GrantRfInfo`` inputs."""

from __future__ import annotations

from typing import Any, Sequence

from services.iap.models import GrantRfInfo


def peer_grant_rf_id(source_sas_id: str | int, grant_id: str) -> str:
    """Stable namespaced id so peer grant ids cannot collide across SAS."""
    return f"peer/{source_sas_id}/{grant_id}"


def _peer_registration(record: dict[str, Any]) -> dict[str, Any] | None:
    reg = record.get("registration")
    return reg if isinstance(reg, dict) else None


def _peer_installation_param(record: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve installationParam without mutating the FAD record.

 Precedence: ``registration.installationParam`` when it is a dict; else
 top-level ``installationParam`` (compatibility / incomplete nested).
 """
    reg = _peer_registration(record)
    if reg is not None:
        install = reg.get("installationParam")
        if isinstance(install, dict):
            return install
    install = record.get("installationParam")
    return install if isinstance(install, dict) else None


def _peer_cbsd_category_raw(record: dict[str, Any]) -> Any:
    """Resolve cbsdCategory: registration key wins when present, else top-level."""
    reg = _peer_registration(record)
    if reg is not None and "cbsdCategory" in reg:
        return reg.get("cbsdCategory")
    return record.get("cbsdCategory")


def grant_rf_infos_from_peer_cbsd_record(
    record: dict[str, Any],
    *,
    source_sas_id: str | int,
) -> list[GrantRfInfo]:
    """Parse one FAD CBSD record into peer ``GrantRfInfo`` rows (may be empty).

 Official WInnForum peer FAD CBSDs nest RF registration under
 ``registration`` (``installationParam``, ``cbsdCategory``). Top-level
 fields remain supported for compatibility.
 """
    src = str(source_sas_id)
    cbsd_id = str(record.get("id") or "").strip()
    if not cbsd_id:
        return []
    install = _peer_installation_param(record)
    if install is None:
        return []
    try:
        lat = float(install["latitude"])
        lon = float(install["longitude"])
    except (KeyError, TypeError, ValueError):
        return []
    height = float(install.get("height") or 0.0)
    height_type = install.get("heightType") or "AGL"
    indoor = bool(install.get("indoorDeployment"))
    raw_cat = _peer_cbsd_category_raw(record)
    if raw_cat is None or str(raw_cat).strip() == "":
        category: str | None = None
    else:
        cat = str(raw_cat).strip().upper()
        category = cat if cat in {"A", "B"} else None

    def _opt_ant(key: str) -> float | None:
        if key not in install or install.get(key) is None:
            return None
        try:
            return float(install[key])
        except (TypeError, ValueError):
            return None

    ant_az = _opt_ant("antennaAzimuth")
    ant_bw = _opt_ant("antennaBeamwidth")
    ant_gain = _opt_ant("antennaGain")
    grants_raw = record.get("grants")
    if not isinstance(grants_raw, list):
        return []

    out: list[GrantRfInfo] = []
    for grant in grants_raw:
        if not isinstance(grant, dict):
            continue
        if grant.get("terminated") is True:
            continue
        raw_id = str(grant.get("id") or "").strip()
        if not raw_id:
            continue
        op = grant.get("operationParam")
        if not isinstance(op, dict):
            continue
        try:
            eirp = float(op["maxEirp"])
            freq = op["operationFrequencyRange"]
            low_hz = int(freq["lowFrequency"])
            high_hz = int(freq["highFrequency"])
        except (KeyError, TypeError, ValueError):
            continue
        if high_hz <= low_hz:
            continue
        out.append(
            GrantRfInfo(
                grant_id=peer_grant_rf_id(src, raw_id),
                cbsd_id=cbsd_id,
                latitude=lat,
                longitude=lon,
                height_m=height,
                height_is_agl=height_type != "AMSL",
                indoor=indoor,
                low_hz=low_hz,
                high_hz=high_hz,
                max_eirp_dbm_mhz=eirp,
                is_managing_sas=False,
                grant_pk=None,
                source_sas_id=src,
                cbsd_category=category,
                antenna_azimuth_deg=ant_az,
                antenna_beamwidth_deg=ant_bw,
                antenna_gain_dbi=ant_gain,
            )
        )
    return out


def grant_rf_infos_from_frozen_peer_cbsds(
    peer_cbsd_rows: Sequence[tuple[str | int, dict[str, Any]]],
) -> list[GrantRfInfo]:
    """Build peer RF grants from frozen ``(source_sas_id, cbsd_record)`` rows.

    Output is sorted by ``(source_sas_id, grant_id)`` so peer order is irrelevant.
    """
    collected: list[GrantRfInfo] = []
    for source_sas_id, record in peer_cbsd_rows:
        if not isinstance(record, dict):
            continue
        collected.extend(
            grant_rf_infos_from_peer_cbsd_record(record, source_sas_id=source_sas_id)
        )
    collected.sort(key=lambda g: (g.source_sas_id or "", g.grant_id))
    return collected
