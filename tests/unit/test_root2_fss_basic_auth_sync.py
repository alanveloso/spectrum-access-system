"""ROOT-2: FSS InjectDatabaseUrl sync requires Basic Auth; enforcement after sync.

Mirrors official WInnForum DatabaseServer(authorization=True) without embedding
harness fixture IDs in production code.
"""

from __future__ import annotations

import json


from services.data_injection_service import persist_database_url
from services.database_sync_service import sync_injected_database_urls
from services.federal_db_service import get_sync_meta, heartbeat_federal_code
from services.heartbeat_service import TERMINATED_GRANT
from tests.fixtures.factories import cat_a_install, make_cbsd, make_grant

_LAT, _LON = 40.25, -99.40


def _located_cbsd(db_session, *, lat: float = _LAT, lon: float = _LON, serial: str | None = None):
    cbsd = make_cbsd(db_session, cbsd_category="A", cbsd_serial_number=serial)
    cbsd.registration_json = json.dumps(
        {
            "fccId": cbsd.fcc_id,
            "cbsdSerialNumber": cbsd.cbsd_serial_number,
            "userId": cbsd.user_id,
            "cbsdCategory": "A",
            "installationParam": cat_a_install(lat=lat, lon=lon),
        }
    )
    db_session.commit()
    return cbsd


def _federal_site() -> dict:
    return {
        "earth_station_latitude_decimal": _LAT,
        "earth_station_longitude_decimal": _LON,
        "lower_frequency": "3650",
        "upper_frequency": "4200",
        "FSS_number": "SYNTH-FSS-ROOT2",
        "tracking_telemetry_control": "false",
    }


def test_fss_sync_fails_closed_without_basic_auth_credentials(db_session, monkeypatch):
    monkeypatch.setenv("DB_SYNC_USERNAME", "")
    monkeypatch.setenv("DB_SYNC_PASSWORD", "")
    from config import get_settings

    get_settings.cache_clear()
    persist_database_url(
        db_session, {"type": "FSS", "url": "https://127.0.0.1:65530/db_sync"}
    )
    db_session.commit()
    report = sync_injected_database_urls(db_session)
    assert report["failed"] >= 1
    assert any("credentials" in e or "FSS" in e for e in report["errors"])
    assert get_sync_meta(db_session).get("fss", 0) == 0
    get_settings.cache_clear()


def test_fss_authenticated_sync_terminates_near_grant_spares_far(
    db_session, monkeypatch
):
    """Authenticated FSS fetch → meta bump → near grant 500; far grant unaffected."""
    monkeypatch.setenv("DB_SYNC_USERNAME", "username")
    monkeypatch.setenv("DB_SYNC_PASSWORD", "password")
    from config import get_settings

    get_settings.cache_clear()

    seen: dict[str, object] = {}

    def _fake_http_get(url: str, *, auth: bool = False) -> bytes:
        seen["url"] = url
        seen["auth"] = auth
        assert auth is True
        return json.dumps({"result": [_federal_site()]}).encode("utf-8")

    monkeypatch.setattr(
        "services.database_sync_service._http_get", _fake_http_get
    )

    near = _located_cbsd(db_session, serial="near-root2")
    near_grant = make_grant(
        db_session,
        near,
        low_hz=3_650_000_000,
        high_hz=3_660_000_000,
        authorized=False,
        lifecycle_state="GRANTED",
    )
    far = _located_cbsd(db_session, lat=10.0, lon=10.0, serial="far-root2")
    far_grant = make_grant(
        db_session,
        far,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        authorized=False,
        lifecycle_state="GRANTED",
    )
    db_session.commit()

    persist_database_url(
        db_session, {"type": "FSS", "url": "https://fss.example.test/db_sync"}
    )
    db_session.commit()

    report = sync_injected_database_urls(db_session)
    assert report["ok"] >= 1
    assert report["failed"] == 0
    assert seen.get("auth") is True
    assert get_sync_meta(db_session).get("fss", 0) >= 1

    assert heartbeat_federal_code(db_session, near, near_grant) == TERMINATED_GRANT
    assert heartbeat_federal_code(db_session, far, far_grant) is None
    get_settings.cache_clear()


def test_runner_configures_fss_basic_auth_when_empty():
    from tools.winnforum.runner import apply_certification_fss_db_sync_auth

    filled = apply_certification_fss_db_sync_auth(
        {"DB_SYNC_USERNAME": "", "DB_SYNC_PASSWORD": ""}
    )
    assert filled["DB_SYNC_USERNAME"] == "username"
    assert filled["DB_SYNC_PASSWORD"] == "password"
    preserved = apply_certification_fss_db_sync_auth(
        {"DB_SYNC_USERNAME": "ops", "DB_SYNC_PASSWORD": "secret"}
    )
    assert preserved["DB_SYNC_USERNAME"] == "ops"
    assert preserved["DB_SYNC_PASSWORD"] == "secret"
