"""Full Activity Dump server — manifest, checksum/size, pagination, snapshot."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from main import app
from models.models import AdminInjectedData, Cbsd, EscSensor, FadDump, FadFile, Grant
from services import clock
from services.fad_service import (
    RECORD_TYPES,
    create_full_activity_dump,
    fad_cbsd_id,
    get_dump_file_by_path,
    get_latest_ready_dump,
    max_records_per_file,
    rewrite_esc_sensor_id,
    rewrite_zone_id,
    verify_ready_dump_integrity,
)
from services.mtls_auth import require_peer_sas

client = TestClient(app)


def setup_function() -> None:
    clock.reset_clock_provider()


def teardown_function() -> None:
    clock.reset_clock_provider()


def _add_cbsd(db, *, fcc: str, serial: str, idx: int = 0) -> Cbsd:
    cbsd = Cbsd(
        cbsd_id=f"{fcc}/{serial}",
        fcc_id=fcc,
        cbsd_serial_number=serial,
        user_id=f"user-{idx}",
        registration_json=json.dumps(
            {
                "fccId": fcc,
                "cbsdSerialNumber": serial,
                "cbsdCategory": "A",
                "airInterface": {"radioTechnology": "E_UTRA"},
                "measCapability": [],
                "installationParam": {
                    "latitude": 39.0 + idx * 0.001,
                    "longitude": -100.0,
                    "height": 10,
                    "heightType": "AGL",
                },
            }
        ),
    )
    db.add(cbsd)
    db.flush()
    return cbsd


def test_id_rewrites_use_admin_id(monkeypatch):
    monkeypatch.setenv("SAS_ADMIN_ID", "admin_uut")
    # Settings may be cached — rewrite reads get_settings each call.
    from config import get_settings

    get_settings.cache_clear()
    assert rewrite_zone_id("zone/ppa/other/7").startswith("zone/ppa/admin_uut/")
    assert rewrite_esc_sensor_id("esc_sensor/x/9").startswith("esc_sensor/admin_uut/")
    get_settings.cache_clear()


def test_empty_dump_has_all_record_types_and_integrity(db_session):
    dump = create_full_activity_dump(db_session)
    assert dump.ready is True
    report = verify_ready_dump_integrity(db_session, dump)
    assert report["ok"] is True
    manifest = json.loads(dump.manifest_json)
    types = {f["recordType"] for f in manifest["files"]}
    assert types == set(RECORD_TYPES)
    for entry in manifest["files"]:
        for key in ("url", "checksum", "size", "version", "recordType"):
            assert key in entry
        assert entry["size"] > 0
        assert len(entry["checksum"]) == 40


def test_checksum_and_size_match_utf8_body(db_session):
    create_full_activity_dump(db_session)
    dump = get_latest_ready_dump(db_session)
    assert dump is not None
    manifest = json.loads(dump.manifest_json)
    for entry in manifest["files"]:
        from urllib.parse import urlparse

        path = urlparse(entry["url"]).path
        row = (
            db_session.query(FadFile)
            .filter_by(dump_id=dump.id, url_path=path)
            .one()
        )
        body = row.content_json.encode("utf-8")
        import hashlib

        assert hashlib.sha1(body).hexdigest() == entry["checksum"]
        assert len(body) == entry["size"]
        envelope = json.loads(row.content_json)
        assert envelope["startTime"] == manifest["generationDateTime"]
        assert envelope["endTime"] == manifest["generationDateTime"]


def test_cbsd_and_zone_and_esc_appear_in_dump(db_session, monkeypatch):
    monkeypatch.setenv("SAS_ADMIN_ID", "sas_admin_id")
    from config import get_settings

    get_settings.cache_clear()
    cbsd = _add_cbsd(db_session, fcc="fcc-a", serial="sn-a")
    expire = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    db_session.add(
        Grant(
            grant_id="G1",
            cbsd_pk=cbsd.id,
            cbsd_id=cbsd.cbsd_id,
            low_frequency=3550000000,
            high_frequency=3560000000,
            max_eirp=20.0,
            channel_type="GAA",
            grant_expire_time=expire.replace(tzinfo=None),
            terminated=False,
            grant_json=json.dumps(
                {
                    "operationParam": {
                        "maxEirp": 20.0,
                        "operationFrequencyRange": {
                            "lowFrequency": 3550000000,
                            "highFrequency": 3560000000,
                        },
                    }
                }
            ),
        )
    )
    zone_id = rewrite_zone_id("zone/ppa/x/1")
    db_session.add(
        AdminInjectedData(
            kind="zone",
            data_json=json.dumps(
                {
                    "record": {
                        "id": zone_id,
                        "usage": "PPA",
                        "ppaInfo": {"cbsdReferenceId": [cbsd.cbsd_id]},
                        "zone": {"type": "Polygon", "coordinates": []},
                    }
                }
            ),
        )
    )
    esc_id = rewrite_esc_sensor_id("esc_sensor/peer/1")
    db_session.add(
        EscSensor(
            record_id=esc_id,
            data_json=json.dumps(
                {
                    "id": esc_id,
                    "installationParam": {"latitude": 39.1, "longitude": -100.1},
                }
            ),
        )
    )
    db_session.commit()

    dump = create_full_activity_dump(db_session)
    files = {f.record_type: f for f in dump.files}
    cbsd_body = json.loads(files["cbsd"].content_json)
    assert len(cbsd_body["recordData"]) == 1
    assert cbsd_body["recordData"][0]["id"] == fad_cbsd_id("fcc-a", "sn-a")
    assert cbsd_body["recordData"][0]["grants"][0]["id"] == "G1"
    zone_body = json.loads(files["zone"].content_json)
    assert zone_body["recordData"][0]["id"].startswith("zone/ppa/")
    refs = zone_body["recordData"][0]["ppaInfo"]["cbsdReferenceId"]
    assert refs[0] == fad_cbsd_id("fcc-a", "sn-a").removeprefix("cbsd/")
    esc_body = json.loads(files["esc_sensor"].content_json)
    assert esc_body["recordData"][0]["id"] == esc_id
    get_settings.cache_clear()


def test_pagination_splits_cbsd_files(db_session, monkeypatch):
    monkeypatch.setenv("SAS_FAD_MAX_RECORDS_PER_FILE", "2")
    assert max_records_per_file() == 2
    for i in range(5):
        _add_cbsd(db_session, fcc=f"fcc{i}", serial=f"sn{i}", idx=i)
    db_session.commit()
    dump = create_full_activity_dump(db_session)
    cbsd_files = [f for f in dump.files if f.record_type == "cbsd"]
    assert len(cbsd_files) == 3  # 2+2+1
    total = 0
    for f in cbsd_files:
        total += len(json.loads(f.content_json)["recordData"])
    assert total == 5
    assert verify_ready_dump_integrity(db_session, dump)["ok"] is True


def test_new_dump_supersedes_previous_published_keeps_historical_ready(db_session):
    """Historical ready dumps coexist; exactly one published/current."""
    first = create_full_activity_dump(db_session)
    second = create_full_activity_dump(db_session)
    db_session.refresh(first)
    assert first.ready is True
    assert first.published is False
    assert second.ready is True
    assert second.published is True
    assert get_latest_ready_dump(db_session).id == second.id
    assert verify_ready_dump_integrity(db_session, first)["ok"] is True
    assert verify_ready_dump_integrity(db_session, second)["ok"] is True


def test_publish_failure_rolls_back_keeps_previous_current(db_session, monkeypatch):
    first = create_full_activity_dump(db_session)
    first_id = first.id

    def _fail_publish(db, dump=None):
        raise RuntimeError("inject publish failure")

    monkeypatch.setattr(
        "services.fad_service.verify_ready_dump_integrity",
        _fail_publish,
    )
    with pytest.raises(RuntimeError, match="inject publish failure"):
        create_full_activity_dump(db_session)

    db_session.expire_all()
    current = get_latest_ready_dump(db_session)
    assert current is not None
    assert current.id == first_id
    assert current.published is True
    assert db_session.query(FadDump).filter_by(published=True).count() == 1
    assert db_session.query(FadDump).filter(FadDump.id != first_id).count() == 0


def test_get_dump_file_by_path_scoped_to_published_only(db_session):
    first = create_full_activity_dump(db_session)
    second = create_full_activity_dump(db_session)
    db_session.refresh(first)
    # Same filename exists on both historical and published dumps.
    hist = next(f for f in first.files if f.record_type == "cbsd")
    pub = next(f for f in second.files if f.record_type == "cbsd")
    assert hist.url_path.rsplit("/", 1)[-1] == pub.url_path.rsplit("/", 1)[-1]

    by_full = get_dump_file_by_path(db_session, pub.url_path)
    assert by_full is not None
    assert by_full.dump_id == second.id
    assert by_full.id == pub.id

    by_name = get_dump_file_by_path(db_session, hist.url_path.rsplit("/", 1)[-1])
    assert by_name is not None
    assert by_name.dump_id == second.id

    assert get_dump_file_by_path(db_session, "../etc/passwd") is None
    assert get_dump_file_by_path(db_session, "/tmp/activity_dump_file_cbsd0.json") is None


def test_deregistered_cbsd_excluded_from_dump(db_session):
    active = _add_cbsd(db_session, fcc="fcc-on", serial="sn-on", idx=0)
    gone = _add_cbsd(db_session, fcc="fcc-off", serial="sn-off", idx=1)
    gone.lifecycle_state = "DEREGISTERED"
    db_session.commit()
    dump = create_full_activity_dump(db_session)
    cbsd_file = next(f for f in dump.files if f.record_type == "cbsd")
    ids = [r["id"] for r in json.loads(cbsd_file.content_json)["recordData"]]
    assert fad_cbsd_id(active.fcc_id, active.cbsd_serial_number) in ids
    assert fad_cbsd_id(gone.fcc_id, gone.cbsd_serial_number) not in ids


def test_terminated_zone_excluded_from_dump(db_session, monkeypatch):
    monkeypatch.setenv("SAS_ADMIN_ID", "sas_admin_id")
    from config import get_settings

    get_settings.cache_clear()
    live = rewrite_zone_id("zone/ppa/x/live")
    dead = rewrite_zone_id("zone/ppa/x/dead")
    db_session.add(
        AdminInjectedData(
            kind="zone",
            data_json=json.dumps(
                {
                    "record": {
                        "id": live,
                        "usage": "PPA",
                        "terminated": False,
                        "ppaInfo": {"cbsdReferenceId": []},
                        "zone": {"type": "Polygon", "coordinates": []},
                    }
                }
            ),
        )
    )
    db_session.add(
        AdminInjectedData(
            kind="zone",
            data_json=json.dumps(
                {
                    "record": {
                        "id": dead,
                        "usage": "PPA",
                        "terminated": True,
                        "ppaInfo": {"cbsdReferenceId": []},
                        "zone": {"type": "Polygon", "coordinates": []},
                    }
                }
            ),
        )
    )
    db_session.commit()
    dump = create_full_activity_dump(db_session)
    zone_file = next(f for f in dump.files if f.record_type == "zone")
    ids = [r["id"] for r in json.loads(zone_file.content_json)["recordData"]]
    assert live in ids
    assert dead not in ids
    get_settings.cache_clear()


def test_admin_trigger_and_sas_sas_download(db_session, monkeypatch):
    monkeypatch.setenv("SAS_ADMIN_ID", "sas_admin_id")
    from config import get_settings

    get_settings.cache_clear()
    _add_cbsd(db_session, fcc="fcc-z", serial="sn-z")
    db_session.commit()

    # Bypass peer mTLS for SAS-SAS download in unit test.
    app.dependency_overrides[require_peer_sas] = lambda: "peer-hash"
    try:
        resp = client.post("/admin/trigger/create_full_activity_dump")
        assert resp.status_code == 200
        dump = get_latest_ready_dump(db_session)
        assert dump is not None
        manifest_resp = client.get("/v1.3/dump")
        assert manifest_resp.status_code == 200
        manifest = manifest_resp.json()
        assert "files" in manifest
        cbsd_entry = next(f for f in manifest["files"] if f["recordType"] == "cbsd")
        from urllib.parse import urlparse

        filename = urlparse(cbsd_entry["url"]).path.rsplit("/", 1)[-1]
        file_resp = client.get(f"/v1.3/cbsd/{filename}")
        assert file_resp.status_code == 200
        body = file_resp.json()
        assert "recordData" in body
        assert body["startTime"] == manifest["generationDateTime"]
    finally:
        app.dependency_overrides.pop(require_peer_sas, None)
        get_settings.cache_clear()
