"""Admin data injection contracts (upsert, generation, URL validation)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app
from models.models import AdminInjectedData
from services.data_injection_service import (
    KIND_DATABASE_URL,
    KIND_FSS,
    KIND_INJECTION_META,
    KIND_WISP,
    KIND_ZONE,
    get_injection_generations,
    is_valid_http_url,
    load_injected,
    persist_cluster_list,
    persist_database_url,
    persist_esc_zone,
    persist_sas_admin,
    persist_zone_data,
    upsert_fss_record,
    upsert_wisp_record,
    verify_optional_checksum,
)
from services.federal_db_service import get_sync_meta
from tools.winnforum.admin_inventory import classify_uut_route
from tests.support.repo import REPO_ROOT

client = TestClient(app)

_FSS = {
    "record": {
        "id": "incumbent/ibfs/test-a",
        "type": "FSS",
        "deploymentParam": [
            {
                "installationParam": {"latitude": 39.0, "longitude": -100.0},
                "operationParam": {
                    "operationFrequencyRange": {
                        "lowFrequency": 3650000000,
                        "highFrequency": 4200000000,
                    }
                },
            }
        ],
    },
    "ttc": True,
}

_WISP = {
    "record": {
        "id": "incumbent/uls/test-a",
        "type": "PART_90",
        "deploymentParam": [
            {
                "operationParam": {
                    "operationFrequencyRange": {
                        "lowFrequency": 3650000000,
                        "highFrequency": 3700000000,
                    }
                }
            }
        ],
    },
    "zone": {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-99.75, 39.05],
                            [-99.65, 39.05],
                            [-99.65, 39.15],
                            [-99.75, 39.15],
                            [-99.75, 39.05],
                        ]
                    ],
                },
            }
        ],
    },
}

_ZONE = {
    "record": {
        "id": "zone/ppa/other/1",
        "usage": "PPA",
        "terminated": False,
        "zone": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
        "ppaInfo": {"palId": ["pal-1"], "cbsdReferenceId": []},
    }
}


def test_url_validation():
    assert is_valid_http_url("https://example.test/fss.json") is True
    assert is_valid_http_url("http://127.0.0.1:8080/pal") is True
    assert is_valid_http_url("file:///etc/passwd") is False
    assert is_valid_http_url("not-a-url") is False


def test_checksum_optional():
    import hashlib

    body = b"abc"
    assert verify_optional_checksum(body, None) is True
    digest = hashlib.sha1(body).hexdigest()
    assert verify_optional_checksum(body, digest) is True
    assert verify_optional_checksum(body, "deadbeef") is False


def test_fss_upsert_and_generation(db_session):
    assert upsert_fss_record(db_session, _FSS) is True
    assert len(load_injected(db_session, KIND_FSS)) == 1
    assert get_sync_meta(db_session)["fss"] == 1
    # Same id → replace, bump again
    again = dict(_FSS)
    again["ttc"] = False
    assert upsert_fss_record(db_session, again) is True
    rows = load_injected(db_session, KIND_FSS)
    assert len(rows) == 1
    assert rows[0]["ttc"] is False
    assert get_sync_meta(db_session)["fss"] == 2
    assert get_injection_generations(db_session).get(KIND_FSS) == 2


def test_fss_rejects_incomplete(db_session):
    assert upsert_fss_record(db_session, {"record": {"id": "x"}}) is False
    assert load_injected(db_session, KIND_FSS) == []


def test_fss_rejects_inverted_freq_and_bad_type(db_session):
    bad_freq = {
        "record": {
            "id": "incumbent/ibfs/bad-freq",
            "type": "FSS",
            "deploymentParam": [
                {
                    "installationParam": {"latitude": 39.0, "longitude": -100.0},
                    "operationParam": {
                        "operationFrequencyRange": {
                            "lowFrequency": 4200000000,
                            "highFrequency": 3650000000,
                        }
                    },
                }
            ],
        }
    }
    assert upsert_fss_record(db_session, bad_freq) is False
    wrong_type = {
        "record": {
            "id": "incumbent/ibfs/not-fss",
            "type": "PART_90",
            "deploymentParam": _FSS["record"]["deploymentParam"],
        }
    }
    assert upsert_fss_record(db_session, wrong_type) is False


def test_wisp_rejects_non_dict_deployment_param(db_session):
    bad = {
        "record": {
            "id": "incumbent/uls/bad",
            "type": "PART_90",
            "deploymentParam": ["not-a-dict"],
        },
        "zone": {"type": "Polygon", "coordinates": []},
    }
    assert upsert_wisp_record(db_session, bad) is False


def test_wisp_upsert(db_session):
    assert upsert_wisp_record(db_session, _WISP) is True
    assert len(load_injected(db_session, KIND_WISP)) == 1
    assert upsert_wisp_record(db_session, {"record": {"id": "x"}}) is False


def test_zone_rewrite_and_upsert(db_session):
    zone_id = persist_zone_data(db_session, _ZONE)
    assert zone_id.startswith("zone/ppa/")
    rows = load_injected(db_session, KIND_ZONE)
    assert len(rows) == 1
    assert rows[0]["record"]["id"] == zone_id
    # Second inject same logical id → still one row
    persist_zone_data(db_session, _ZONE)
    assert len(load_injected(db_session, KIND_ZONE)) == 1


def test_database_url_valid_and_invalid(db_session):
    assert (
        persist_database_url(
            db_session,
            {"type": "PAL", "url": "https://db.example/pal.json", "checksum": "abc"},
        )
        is True
    )
    rows = load_injected(db_session, KIND_DATABASE_URL)
    assert len(rows) == 1
    assert rows[0]["type"] == "PAL"
    assert rows[0]["checksum"] == "abc"
    assert persist_database_url(db_session, {"type": "NOPE", "url": "https://x"}) is False
    assert persist_database_url(db_session, {"type": "FSS", "url": "ftp://x"}) is False
    assert len(load_injected(db_session, KIND_DATABASE_URL)) == 1


def test_esc_cluster_sas_admin(db_session):
    assert persist_esc_zone(db_session, {"id": "esc/1", "channels": [1, 2]}) is True
    assert persist_esc_zone(db_session, {"channels": [1]}) is False
    assert persist_cluster_list(db_session, {"userId": "u1", "cbsdIds": ["a"]}) is True
    assert persist_cluster_list(db_session, {"noise": True}) is False
    assert persist_sas_admin(db_session, {"record": {"id": "admin/1", "name": "A"}}) is True
    assert persist_sas_admin(db_session, {"record": {}}) is False
    gens = get_injection_generations(db_session)
    assert gens.get("esc_zone") == 1
    assert gens.get("cluster_list") == 1
    assert gens.get("sas_admin") == 1


def test_admin_http_inject_paths(db_session):
    assert client.post("/admin/injectdata/fss", json=_FSS).status_code == 200
    assert client.post("/admin/injectdata/wisp", json=_WISP).status_code == 200
    resp = client.post("/admin/injectdata/zone", json=_ZONE)
    assert resp.status_code == 200
    assert isinstance(resp.json(), str)
    assert (
        client.post(
            "/admin/injectdata/database_url",
            json={"type": "CPI", "url": "https://db.example/cpi.csv"},
        ).status_code
        == 200
    )
    assert (
        client.post("/admin/injectdata/esc_zone", json={"record": {"id": "e1"}}).status_code
        == 200
    )
    assert (
        client.post(
            "/admin/injectdata/cluster_list", json={"userId": "u", "cbsdIds": []}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/admin/injectdata/sas_admin", json={"record": {"id": "sas/1"}}
        ).status_code
        == 200
    )
    assert db_session.query(AdminInjectedData).filter_by(kind=KIND_FSS).count() >= 1


def test_reset_clears_injections(db_session):
    from database import SessionLocal, reset_db

    upsert_fss_record(db_session, _FSS)
    persist_database_url(
        db_session, {"type": "PAL", "url": "https://db.example/pal.json"}
    )
    db_session.close()
    reset_db()
    session = SessionLocal()
    try:
        assert load_injected(session, KIND_FSS) == []
        assert load_injected(session, KIND_DATABASE_URL) == []
        assert load_injected(session, KIND_INJECTION_META) == []
    finally:
        session.close()


def test_inventory_classifies_injects_implemented():
    routes = REPO_ROOT / "routes" / "admin_routes.py"
    for path in (
        "injectdata/zone",
        "injectdata/fss",
        "injectdata/wisp",
        "injectdata/database_url",
        "injectdata/esc_zone",
        "injectdata/cluster_list",
        "injectdata/sas_admin",
    ):
        status, _notes = classify_uut_route(path, routes)
        assert status == "implemented", path
