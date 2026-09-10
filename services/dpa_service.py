"""DPA catalogue load and Admin activation lifecycle.

Loads ESC-monitored DPA definitions from configurable NTIA-style KML paths,
persists a versioned catalogue, and manages per-(dpaId, channel) activations.

No harness fixture device IDs or DPA names are hard-coded: catalogue content
comes from the KML files resolved at runtime.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from models.models import AdminInjectedData
from providers.contract import ResourcePathsRecord, TOKEN_DPA_KML_CATALOGUE
from runtime.data_access import require_composed_provider, try_composed_provider
from services.meas_report import FLAG_DPA_ACTIVE
from services.spectrum_inquiry_service import CHANNEL_HZ, CBRS_HIGH_HZ, CBRS_LOW_HZ

KIND_CATALOGUE = "dpa_catalogue"
KIND_AUDIT = "dpa_audit"
KML_NS = "{http://www.opengis.net/kml/2.2}"

# Default relative filenames under data/ntia/ (provisioned locally; see README).


@dataclass(frozen=True)
class FrequencyRange:
    low_hz: int
    high_hz: int

    def as_dict(self) -> dict[str, int]:
        return {"lowFrequency": self.low_hz, "highFrequency": self.high_hz}

    def overlaps(self, other_low: int, other_high: int) -> bool:
        return self.low_hz < other_high and self.high_hz > other_low


@dataclass
class DpaDefinition:
    """One ESC-monitored DPA from the KML catalogue."""

    dpa_id: str
    freq_low_hz: int
    freq_high_hz: int
    source: str
    esc_monitored: bool = True
    neighborhood_km: dict[str, float] = field(default_factory=dict)
    geometry: dict[str, Any] | None = None
    protection_params: dict[str, Any] = field(default_factory=dict)

    def channels(self) -> list[FrequencyRange]:
        """10 MHz channels covering the DPA protection band (clipped to declared range)."""
        return channelize(self.freq_low_hz, self.freq_high_hz)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "dpaId": self.dpa_id,
            "frequencyRange": {
                "lowFrequency": self.freq_low_hz,
                "highFrequency": self.freq_high_hz,
            },
            "escMonitored": self.esc_monitored,
            "source": self.source,
            "neighborhoodKm": dict(self.neighborhood_km),
            "geometry": self.geometry,
            "protectionParams": dict(self.protection_params),
            "channels": [c.as_dict() for c in self.channels()],
            "movelist": [],  # filled per activation; catalogue keeps empty template
        }


def channelize(low_hz: int, high_hz: int, *, step_hz: int = CHANNEL_HZ) -> list[FrequencyRange]:
    """Emit aligned 10 MHz channels covering [low, high)."""
    if high_hz <= low_hz or step_hz <= 0:
        return []
    # Align start down to channel grid relative to CBRS_LOW when inside CBRS;
    # otherwise start at low_hz rounded down to step boundary from low itself.
    start = low_hz - (low_hz % step_hz) if low_hz % step_hz else low_hz
    if start < low_hz:
        start += step_hz
    if start >= high_hz:
        # Partial head channel still counts as one activation slot.
        return [FrequencyRange(low_hz, high_hz)]
    out: list[FrequencyRange] = []
    if start > low_hz:
        out.append(FrequencyRange(low_hz, start))
    cur = start
    while cur + step_hz <= high_hz:
        out.append(FrequencyRange(cur, cur + step_hz))
        cur += step_hz
    if cur < high_hz:
        out.append(FrequencyRange(cur, high_hz))
    return out


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_dpa_kml_paths(explicit: list[Path] | None = None) -> list[Path]:
    """Resolve KML catalogue paths from explicit override or composed reference_data.

    ``explicit=[]`` means “no paths” (does not fall back to defaults).
    ``explicit=None`` uses the composed ``reference_data`` provider.
    """
    if explicit is not None:
        return [p.expanduser().resolve() for p in explicit if p.is_file()]

    provider = try_composed_provider("reference_data")
    if provider is None:
        provider = require_composed_provider("reference_data")
    record = provider.fetch(token=TOKEN_DPA_KML_CATALOGUE)
    if not isinstance(record, ResourcePathsRecord):
        raise ValueError("composed reference_data provider returned unexpected record")
    return [Path(item) for item in record.paths if Path(item).is_file()]


def _parse_kml_coordinates(text: str | None) -> list[list[float]]:
    ring: list[list[float]] = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            try:
                ring.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    return ring


def _extended_data_map(placemark: ET.Element) -> dict[str, str]:
    data: dict[str, str] = {}
    for node in placemark.findall(f"{KML_NS}ExtendedData/{KML_NS}Data"):
        key = node.get("name")
        value_el = node.find(f"{KML_NS}value")
        if key and value_el is not None and value_el.text is not None:
            data[key] = value_el.text.strip()
    return data


def _parse_freq_range_mhz(raw: str | None) -> tuple[int, int] | None:
    if not raw or "-" not in raw:
        return None
    left, right = raw.split("-", 1)
    try:
        low_mhz = float(left.strip())
        high_mhz = float(right.strip())
    except ValueError:
        return None
    if high_mhz <= low_mhz:
        return None
    return int(low_mhz * 1_000_000), int(high_mhz * 1_000_000)


def _placemark_geometry(placemark: ET.Element) -> dict[str, Any] | None:
    outer = placemark.find(
        f".//{KML_NS}outerBoundaryIs/{KML_NS}LinearRing/{KML_NS}coordinates"
    )
    if outer is not None:
        ring = _parse_kml_coordinates(outer.text)
        if len(ring) >= 3:
            if ring[0] != ring[-1]:
                ring.append(list(ring[0]))
            return {"type": "Polygon", "coordinates": [ring]}

    point_el = placemark.find(f".//{KML_NS}Point/{KML_NS}coordinates")
    if point_el is not None and (point_el.text or "").strip():
        parts = (point_el.text or "").strip().split(",")
        if len(parts) >= 2:
            try:
                lon = float(parts[0])
                lat = float(parts[1])
            except ValueError:
                return None
            return {"type": "Point", "coordinates": [lon, lat]}
    return None


def _definitions_from_kml_root(
    root: ET.Element, *, source: str
) -> list[DpaDefinition]:
    """Canonical placemark extraction shared by path and bytes parsers."""
    definitions: list[DpaDefinition] = []
    for pm in root.iter(f"{KML_NS}Placemark"):
        name_el = pm.find(f"{KML_NS}name")
        if name_el is None or not (name_el.text or "").strip():
            continue
        dpa_id = (name_el.text or "").strip()
        ext = _extended_data_map(pm)
        freqs = _parse_freq_range_mhz(ext.get("freqRangeMHz"))
        if freqs is None:
            continue
        neighborhood: dict[str, float] = {}
        protection: dict[str, Any] = {}
        for key, raw in ext.items():
            if key.endswith("NeighborhoodDistanceKm") or "Neighborhood" in key:
                try:
                    value = float(raw)
                except ValueError:
                    continue
                if math.isfinite(value):
                    neighborhood[key] = value
            elif key in {
                "protectionCritDbmPer10MHz",
                "refHeightMeters",
                "antennaBeamwidthDeg",
                "minAzimuthDeg",
                "maxAzimuthDeg",
            }:
                try:
                    value = float(raw)
                except ValueError:
                    protection[key] = raw
                    continue
                if math.isfinite(value):
                    protection[key] = value
        definitions.append(
            DpaDefinition(
                dpa_id=dpa_id,
                freq_low_hz=freqs[0],
                freq_high_hz=freqs[1],
                source=source,
                neighborhood_km=neighborhood,
                geometry=_placemark_geometry(pm),
                protection_params=protection,
            )
        )
    return definitions


def parse_dpa_kml_bytes(
    data: bytes | str, *, source: str = "scheduled_dpa_kml"
) -> list[DpaDefinition]:
    """Parse NTIA/P-DPA style KML from in-memory bytes/text (no tempfile)."""
    if isinstance(data, bytes):
        text = data.decode("utf-8")
    else:
        text = data
    root = ET.fromstring(text)
    return _definitions_from_kml_root(root, source=source)


def parse_dpa_kml(path: Path) -> list[DpaDefinition]:
    """Parse NTIA-style E-DPA / P-DPA KML into DPA definitions."""
    root = ET.parse(path).getroot()
    return _definitions_from_kml_root(root, source=str(path))


def load_catalogue_from_paths(paths: list[Path]) -> list[DpaDefinition]:
    by_id: dict[str, DpaDefinition] = {}
    for path in paths:
        for definition in parse_dpa_kml(path):
            by_id[definition.dpa_id] = definition
    return list(by_id.values())


def _append_audit(db: Session, event: str, detail: dict[str, Any]) -> None:
    db.add(
        AdminInjectedData(
            kind=KIND_AUDIT,
            data_json=json.dumps(
                {"event": event, "at": _utc_now_iso(), **detail},
                default=str,
            ),
        )
    )


def _activation_key(dpa_id: str, low_hz: int, high_hz: int) -> str:
    return f"{dpa_id}|{low_hz}|{high_hz}"


def _parse_activation_row(row: AdminInjectedData) -> dict[str, Any] | None:
    try:
        data = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def list_catalogue(db: Session) -> list[dict[str, Any]]:
    row = db.query(AdminInjectedData).filter_by(kind=KIND_CATALOGUE).first()
    if not row:
        return []
    try:
        payload = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return []
    items = payload.get("dpas") if isinstance(payload, dict) else None
    return list(items) if isinstance(items, list) else []


def get_catalogue_definition(db: Session, dpa_id: str) -> dict[str, Any] | None:
    for item in list_catalogue(db):
        if isinstance(item, dict) and item.get("dpaId") == dpa_id:
            return item
    return None


def list_active_activations(db: Session) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in db.query(AdminInjectedData).filter_by(kind=FLAG_DPA_ACTIVE).all():
        data = _parse_activation_row(row)
        if data:
            out.append(data)
    return out


def clear_activations(db: Session, *, commit: bool = True) -> int:
    count = db.query(AdminInjectedData).filter_by(kind=FLAG_DPA_ACTIVE).delete()
    if commit:
        db.commit()
    return int(count or 0)


def _upsert_activation(
    db: Session,
    *,
    dpa_id: str,
    freq: FrequencyRange,
    movelist: list[Any] | None = None,
    source: str | None = None,
) -> None:
    key = _activation_key(dpa_id, freq.low_hz, freq.high_hz)
    # SessionLocal uses autoflush=False — flush so prior pending activations are
    # visible to this scan (avoids duplicate rows for the same activation key).
    db.flush()
    # Replace any existing row with same key (scan — AdminInjectedData has no unique).
    for row in db.query(AdminInjectedData).filter_by(kind=FLAG_DPA_ACTIVE).all():
        data = _parse_activation_row(row)
        if not data:
            continue
        fr = data.get("frequencyRange") or {}
        if (
            data.get("dpaId") == dpa_id
            and int(fr.get("lowFrequency", -1)) == freq.low_hz
            and int(fr.get("highFrequency", -1)) == freq.high_hz
        ):
            payload = {
                "dpaId": dpa_id,
                "frequencyRange": freq.as_dict(),
                "activationKey": key,
                "movelist": list(movelist or []),
                "activatedAt": _utc_now_iso(),
                "active": True,
            }
            # Preserve scheduled/manual provenance across movelist refreshes.
            effective_source = source if source is not None else data.get("source")
            if effective_source:
                payload["source"] = effective_source
            row.data_json = json.dumps(payload)
            return
    payload = {
        "dpaId": dpa_id,
        "frequencyRange": freq.as_dict(),
        "activationKey": key,
        "movelist": list(movelist or []),
        "activatedAt": _utc_now_iso(),
        "active": True,
    }
    if source:
        payload["source"] = source
    db.add(AdminInjectedData(kind=FLAG_DPA_ACTIVE, data_json=json.dumps(payload)))
    db.flush()


def _insert_activation(
    db: Session,
    *,
    dpa_id: str,
    freq: FrequencyRange,
    movelist: list[Any] | None = None,
) -> None:
    """Insert activation without scanning (caller must have cleared collisions)."""
    payload = {
        "dpaId": dpa_id,
        "frequencyRange": freq.as_dict(),
        "activationKey": _activation_key(dpa_id, freq.low_hz, freq.high_hz),
        "movelist": list(movelist or []),
        "activatedAt": _utc_now_iso(),
        "active": True,
    }
    db.add(AdminInjectedData(kind=FLAG_DPA_ACTIVE, data_json=json.dumps(payload)))


def _remove_activation(db: Session, dpa_id: str, freq: FrequencyRange) -> bool:
    removed = False
    for row in list(db.query(AdminInjectedData).filter_by(kind=FLAG_DPA_ACTIVE).all()):
        data = _parse_activation_row(row)
        if not data:
            continue
        fr = data.get("frequencyRange") or {}
        if (
            data.get("dpaId") == dpa_id
            and int(fr.get("lowFrequency", -1)) == freq.low_hz
            and int(fr.get("highFrequency", -1)) == freq.high_hz
        ):
            db.delete(row)
            removed = True
    return removed


def persist_catalogue(
    db: Session,
    definitions: list[DpaDefinition],
    *,
    sources: list[str],
) -> dict[str, Any]:
    payload = {
        "version": 1,
        "loadedAt": _utc_now_iso(),
        "sources": sources,
        "bandPlanHz": {"low": CBRS_LOW_HZ, "high": CBRS_HIGH_HZ, "channel": CHANNEL_HZ},
        "dpas": [d.to_mapping() for d in definitions],
    }
    existing = db.query(AdminInjectedData).filter_by(kind=KIND_CATALOGUE).first()
    raw = json.dumps(payload)
    if existing:
        existing.data_json = raw
    else:
        db.add(AdminInjectedData(kind=KIND_CATALOGUE, data_json=raw))
    return payload


def definition_from_mapping(item: dict[str, Any]) -> DpaDefinition | None:
    """Rebuild a ``DpaDefinition`` from a persisted catalogue mapping."""
    if not isinstance(item, dict) or not item.get("dpaId"):
        return None
    fr = item.get("frequencyRange") or {}
    try:
        low = int(fr["lowFrequency"])
        high = int(fr["highFrequency"])
    except (KeyError, TypeError, ValueError):
        return None
    return DpaDefinition(
        dpa_id=str(item["dpaId"]),
        freq_low_hz=low,
        freq_high_hz=high,
        source=str(item.get("source") or ""),
        esc_monitored=bool(item.get("escMonitored", True)),
        neighborhood_km={
            str(k): float(v)
            for k, v in dict(item.get("neighborhoodKm") or {}).items()
            if isinstance(v, (int, float)) and math.isfinite(float(v))
        },
        geometry=item.get("geometry") if isinstance(item.get("geometry"), dict) else None,
        protection_params=dict(item.get("protectionParams") or {}),
    )


def upsert_catalogue_definitions(
    db: Session,
    definitions: list[DpaDefinition],
    *,
    source_label: str,
) -> list[DpaDefinition]:
    """Merge ``definitions`` into the catalogue by ``dpaId``; keep unrelated rows."""
    by_id: dict[str, DpaDefinition] = {}
    for item in list_catalogue(db):
        rebuilt = definition_from_mapping(item) if isinstance(item, dict) else None
        if rebuilt is not None:
            by_id[rebuilt.dpa_id] = rebuilt
    for definition in definitions:
        by_id[definition.dpa_id] = definition
    merged = list(by_id.values())
    existing = db.query(AdminInjectedData).filter_by(kind=KIND_CATALOGUE).first()
    prior_sources: list[str] = []
    if existing:
        try:
            prior = json.loads(existing.data_json or "{}")
            raw_sources = prior.get("sources") if isinstance(prior, dict) else None
            if isinstance(raw_sources, list):
                prior_sources = [str(s) for s in raw_sources]
        except json.JSONDecodeError:
            prior_sources = []
    sources = list(dict.fromkeys([*prior_sources, source_label]))
    persist_catalogue(db, merged, sources=sources)
    return merged


def activate_all_esc_monitored(db: Session, definitions: list[DpaDefinition]) -> int:
    """Activate every ESC-monitored DPA on every channel in its protection range.

 Inserts only — caller must clear prior activations (``load_dpas`` / bulk).
 """
    count = 0
    for definition in definitions:
        if not definition.esc_monitored:
            continue
        for channel in definition.channels():
            _insert_activation(db, dpa_id=definition.dpa_id, freq=channel)
            count += 1
    return count


def load_dpas(
    db: Session,
    *,
    kml_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Load catalogue from KML and immediately activate all ESC-monitored DPAs."""
    paths = resolve_dpa_kml_paths(kml_paths)
    if not paths:
        raise FileNotFoundError(
            "No DPA KML catalogue found. Set SAS_DPA_KML_PATHS or provision "
            "data/ntia/E-DPAs.kml and P-DPAs.kml (see data/ntia/README.md)."
        )
    definitions = load_catalogue_from_paths(paths)
    if not definitions:
        raise ValueError(f"DPA KML parsed zero placemarks from {paths}")

    clear_activations(db, commit=False)
    catalogue = persist_catalogue(
        db, definitions, sources=[p.name for p in paths]
    )
    activated = activate_all_esc_monitored(db, definitions)
    _append_audit(
        db,
        "load_dpas",
        {
            "sources": [p.name for p in paths],
            "catalogueSize": len(definitions),
            "activations": activated,
        },
    )
    db.commit()
    return {
        "catalogueSize": len(definitions),
        "activations": activated,
        "sources": catalogue["sources"],
    }


def bulk_dpa_activation(db: Session, *, activate: bool | None) -> dict[str, Any]:
    """Activate or deactivate all ESC-monitored DPAs on all catalogue channels.

 ``activate`` must be an explicit bool; ``None`` leaves state unchanged.
 """
    if activate is None:
        return {"ok": False, "reason": "activate_required"}

    if not activate:
        cleared = clear_activations(db, commit=False)
        _append_audit(db, "bulk_deactivate", {"cleared": cleared})
        db.commit()
        return {"ok": True, "activate": False, "activations": 0, "cleared": cleared}

    catalogue = list_catalogue(db)
    if not catalogue:
        # No prior load — attempt load from default paths then activate.
        result = load_dpas(db)
        return {"ok": True, "activate": True, **result}

    definitions = [
        DpaDefinition(
            dpa_id=str(item["dpaId"]),
            freq_low_hz=int(item["frequencyRange"]["lowFrequency"]),
            freq_high_hz=int(item["frequencyRange"]["highFrequency"]),
            source=str(item.get("source") or ""),
            esc_monitored=bool(item.get("escMonitored", True)),
            neighborhood_km=dict(item.get("neighborhoodKm") or {}),
            geometry=item.get("geometry"),
            protection_params=dict(item.get("protectionParams") or {}),
        )
        for item in catalogue
        if isinstance(item, dict) and item.get("dpaId")
    ]
    clear_activations(db, commit=False)
    activated = activate_all_esc_monitored(db, definitions)
    _append_audit(db, "bulk_activate", {"activations": activated})
    db.commit()
    return {"ok": True, "activate": True, "activations": activated}


def _parse_request_freq(body: dict[str, Any]) -> FrequencyRange | None:
    fr = body.get("frequencyRange")
    if not isinstance(fr, dict):
        return None
    try:
        low = int(fr["lowFrequency"])
        high = int(fr["highFrequency"])
    except (KeyError, TypeError, ValueError):
        return None
    if high <= low:
        return None
    return FrequencyRange(low, high)


def _channel_in_definition(definition: dict[str, Any], freq: FrequencyRange) -> bool:
    """True when ``freq`` exactly matches a catalogue channel for this DPA."""
    channels = definition.get("channels")
    if isinstance(channels, list) and channels:
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            try:
                if (
                    int(ch["lowFrequency"]) == freq.low_hz
                    and int(ch["highFrequency"]) == freq.high_hz
                ):
                    return True
            except (KeyError, TypeError, ValueError):
                continue
        return False
    # Rebuild from declared band when catalogue row lacks precomputed channels.
    try:
        d_low = int(definition["frequencyRange"]["lowFrequency"])
        d_high = int(definition["frequencyRange"]["highFrequency"])
    except (KeyError, TypeError, ValueError):
        return False
    return any(
        c.low_hz == freq.low_hz and c.high_hz == freq.high_hz
        for c in channelize(d_low, d_high)
    )


def refresh_or_fail_closed_movelists(
    db: Session,
    channels: list[tuple[str, FrequencyRange]],
) -> None:
    """Refresh movelists; on RF/domain error use conservative overlapping grants.

 Shared by explicit ``activate_dpa`` and scheduled DPA materialization so both
 paths share the same fail-closed policy. A successful refresh that yields an
 empty movelist (no grants need to move) remains valid. An RF evaluation that
 cannot complete must never be left as a silent empty movelist when overlapping
 grants exist.

 Provenance ``source`` on existing activation rows is preserved across refresh.
 """
    from services.dpa_protection import (
        collect_active_dpa_grants,
        refresh_activation_movelists,
    )
    from services.propagation.errors import PropagationUnavailableError
    from services.terrain.exceptions import TerrainError

    try:
        refresh_activation_movelists(db, commit=False)
    except (PropagationUnavailableError, TerrainError, ValueError, TypeError, KeyError):
        grants = collect_active_dpa_grants(db)
        for dpa_id, freq in channels:
            moved = [
                g.grant_id
                for g in grants
                if g.low_hz < freq.high_hz and g.high_hz > freq.low_hz
            ]
            _upsert_activation(db, dpa_id=dpa_id, freq=freq, movelist=moved)


def activate_dpa(db: Session, body: dict[str, Any]) -> dict[str, Any]:
    """Activate one DPA on one channel after validating id and channel coverage."""
    dpa_id = body.get("dpaId")
    freq = _parse_request_freq(body)
    if not isinstance(dpa_id, str) or not dpa_id.strip() or freq is None:
        return {"ok": False, "reason": "invalid_request"}
    dpa_id = dpa_id.strip()

    definition = get_catalogue_definition(db, dpa_id)
    if definition is None:
        return {"ok": False, "reason": "unknown_dpaId", "dpaId": dpa_id}

    if not _channel_in_definition(definition, freq):
        return {
            "ok": False,
            "reason": "channel_not_in_catalogue",
            "dpaId": dpa_id,
            "frequencyRange": freq.as_dict(),
        }

    _upsert_activation(db, dpa_id=dpa_id, freq=freq)
    refresh_or_fail_closed_movelists(db, [(dpa_id, freq)])
    _append_audit(
        db,
        "activate",
        {"dpaId": dpa_id, "frequencyRange": freq.as_dict()},
    )
    db.commit()
    return {"ok": True, "dpaId": dpa_id, "frequencyRange": freq.as_dict()}


def deactivate_dpa(db: Session, body: dict[str, Any]) -> dict[str, Any]:
    """Deactivate one DPA on one channel (selective; does not clear others)."""
    dpa_id = body.get("dpaId")
    freq = _parse_request_freq(body)
    if not isinstance(dpa_id, str) or not dpa_id.strip() or freq is None:
        return {"ok": False, "reason": "invalid_request"}
    dpa_id = dpa_id.strip()
    removed = _remove_activation(db, dpa_id, freq)
    _append_audit(
        db,
        "deactivate",
        {
            "dpaId": dpa_id,
            "frequencyRange": freq.as_dict(),
            "removed": removed,
        },
    )
    db.commit()
    return {
        "ok": True,
        "dpaId": dpa_id,
        "frequencyRange": freq.as_dict(),
        "removed": removed,
    }


def grant_overlaps_active_dpa(db: Session, low_hz: int, high_hz: int) -> bool:
    """True when grant frequency overlaps any active DPA channel activation."""
    for data in list_active_activations(db):
        fr = data.get("frequencyRange") or {}
        try:
            a_low = int(fr["lowFrequency"])
            a_high = int(fr["highFrequency"])
        except (KeyError, TypeError, ValueError):
            continue
        if low_hz < a_high and high_hz > a_low:
            return True
    return False


def grant_overlaps_esc_monitored_catalogue(
    db: Session, low_hz: int, high_hz: int
) -> bool:
    """True when grant overlaps any ESC-monitored catalogue DPA channel set."""
    for item in list_catalogue(db):
        if not isinstance(item, dict):
            continue
        if not bool(item.get("escMonitored", True)):
            continue
        for ch in item.get("channels") or []:
            if not isinstance(ch, dict):
                continue
            try:
                a_low = int(ch["lowFrequency"])
                a_high = int(ch["highFrequency"])
            except (KeyError, TypeError, ValueError):
                continue
            if low_hz < a_high and high_hz > a_low:
                return True
    return False


def reset_dpa_state(db: Session) -> None:
    """Clear catalogue, activations and audit (also covered by full reset_db)."""
    for kind in (KIND_CATALOGUE, FLAG_DPA_ACTIVE, KIND_AUDIT):
        db.query(AdminInjectedData).filter_by(kind=kind).delete()
    db.commit()
