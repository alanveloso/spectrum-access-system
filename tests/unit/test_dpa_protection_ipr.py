"""Rel1Ext IPR DPA protection (ESC absent, movelist, clutter+loading)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from models.models import Cbsd
from services.dpa_protection import (
    DpaGrantRf,
    DpaPathLossModel,
    DpaPathLossUnavailable,
    ProtectionReason,
    aggregate_within_threshold,
    build_movelist,
    default_rel1ext_path_loss_fn,
    evaluate_protected_channel,
    free_space_path_loss_db,
    interference_dbm_at_point,
    list_protected_dpa_channels,
    make_path_loss_fn,
    proposed_grant_violates_dpa,
    rel1ext_dpa_path_loss_db,
)
from services.dpa_service import activate_dpa, clear_activations, load_dpas
from services.esc_admin_service import is_esc_absent, set_esc_absent
from services.propagation.rel1ext_dpa import ACTIVITY_LOSS_FACTOR_DB, CLUTTER_TX_AGL_MAX_M
from tests.fixtures.factories import make_cbsd


def _stub_itm_fn(itm_db: float = 100.0):
    """Injectable ITM median for unit tests (does not use Free Space)."""

    def _itm(grant, lat_rx, lon_rx, height_rx):
        return float(itm_db)

    return make_path_loss_fn(model=DpaPathLossModel.ITM_REL1EXT, itm_median_fn=_itm)

_SYNTH_KML = """<?xml version="1.0" encoding="utf-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>IprOffshore</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3550-3560</value></Data>
        <Data name="catA_Outdoor_NeighborhoodDistanceKm"><value>50</value></Data>
        <Data name="catBNeighborhoodDistanceKm"><value>80</value></Data>
        <Data name="protectionCritDbmPer10MHz"><value>-144</value></Data>
        <Data name="refHeightMeters"><value>50</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -75.05,38.05,0 -75.0,38.05,0 -75.0,38.0,0 -75.05,38.0,0 -75.05,38.05,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
    <Placemark>
      <name>IprInlandAlwaysOn</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3650-3660</value></Data>
        <Data name="catA_Outdoor_NeighborhoodDistanceKm"><value>30</value></Data>
        <Data name="protectionCritDbmPer10MHz"><value>-144</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -76.05,39.05,0 -76.0,39.05,0 -76.0,39.0,0 -76.05,39.0,0 -76.05,39.05,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""


@pytest.fixture
def synth_kml(tmp_path: Path) -> Path:
    path = tmp_path / "ipr-dpas.kml"
    path.write_text(_SYNTH_KML, encoding="utf-8")
    return path


def _mark_inland_not_esc_monitored(db: Session) -> None:
    from models.models import AdminInjectedData
    from services.dpa_service import KIND_CATALOGUE

    row = db.query(AdminInjectedData).filter_by(kind=KIND_CATALOGUE).first()
    assert row is not None
    payload = json.loads(row.data_json)
    for item in payload["dpas"]:
        if item["dpaId"] == "IprInlandAlwaysOn":
            item["escMonitored"] = False
    row.data_json = json.dumps(payload)
    db.commit()


def test_rel1ext_path_loss_includes_activity_and_height_gate():
    low = DpaGrantRf(
        grant_id="g1",
        cbsd_id="c1",
        latitude=38.02,
        longitude=-75.02,
        height_m=3.0,
        height_is_agl=True,
        indoor=False,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
    )
    high = DpaGrantRf(**{**low.__dict__, "height_m": 10.0, "grant_id": "g2"})
    pl_low = rel1ext_dpa_path_loss_db(
        low, 38.025, -75.025, 50.0, median_path_loss_db=100.0
    )
    pl_high = rel1ext_dpa_path_loss_db(
        high, 38.025, -75.025, 50.0, median_path_loss_db=100.0
    )
    assert pl_high == pytest.approx(100.0 + 0.0 + ACTIVITY_LOSS_FACTOR_DB)
    assert pl_low > pl_high
    assert low.height_m <= CLUTTER_TX_AGL_MAX_M


def test_movelist_drops_strongest_until_threshold_met():
    from services.dpa_protection import PointInterference
    from services.iap.aggregate import dbm_to_mw

    real = [
        PointInterference(
            grant_id="a",
            interference_dbm=-120.0,
            interference_mw=dbm_to_mw(-120.0),
            path_loss_db=1.0,
        ),
        PointInterference(
            grant_id="b",
            interference_dbm=-140.0,
            interference_mw=dbm_to_mw(-140.0),
            path_loss_db=1.0,
        ),
    ]
    moved = build_movelist(real, threshold_dbm=-145.0)
    assert moved[0] == "a"
    _, ok = aggregate_within_threshold(
        [c for c in real if c.grant_id not in moved], threshold_dbm=-145.0
    )
    assert ok


def test_esc_absent_protects_all_esc_monitored_channels(db_session: Session, synth_kml: Path):
    load_dpas(db_session, kml_paths=[synth_kml])
    _mark_inland_not_esc_monitored(db_session)
    clear_activations(db_session)
    protected = list_protected_dpa_channels(db_session)
    assert {p.dpa_id for p in protected} == {"IprInlandAlwaysOn"}
    assert all(p.reason == ProtectionReason.ALWAYS_ON for p in protected)

    set_esc_absent(db_session, absent=True)
    assert is_esc_absent(db_session)
    protected = list_protected_dpa_channels(db_session)
    reasons = {p.dpa_id: p.reason for p in protected}
    assert reasons["IprOffshore"] == ProtectionReason.ESC_ABSENT
    assert reasons["IprInlandAlwaysOn"] == ProtectionReason.ALWAYS_ON


def test_active_dpa_listed_and_evaluate_clutter_cohorts(db_session: Session, synth_kml: Path):
    load_dpas(db_session, kml_paths=[synth_kml])
    _mark_inland_not_esc_monitored(db_session)
    clear_activations(db_session)
    activate_dpa(
        db_session,
        {
            "dpaId": "IprOffshore",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    protected = [
        p
        for p in list_protected_dpa_channels(db_session)
        if p.dpa_id == "IprOffshore" and p.reason == ProtectionReason.ACTIVE
    ]
    assert len(protected) == 1
    channel = protected[0]

    grants = [
        DpaGrantRf(
            grant_id="low",
            cbsd_id="c-low",
            latitude=38.02,
            longitude=-75.02,
            height_m=3.0,
            height_is_agl=True,
            indoor=False,
            low_hz=3_550_000_000,
            high_hz=3_560_000_000,
            max_eirp_dbm_mhz=37.0,
        ),
        DpaGrantRf(
            grant_id="high",
            cbsd_id="c-high",
            latitude=38.02,
            longitude=-75.02,
            height_m=12.0,
            height_is_agl=True,
            indoor=False,
            low_hz=3_550_000_000,
            high_hz=3_560_000_000,
            max_eirp_dbm_mhz=37.0,
        ),
    ]
    contribs, moved, agg, ok = evaluate_protected_channel(
        channel, grants, path_loss_fn=_stub_itm_fn(100.0)
    )
    assert len(contribs) == 2
    by_id = {c.grant_id: c for c in contribs}
    assert by_id["low"].path_loss_db > by_id["high"].path_loss_db
    assert by_id["low"].interference_mw < by_id["high"].interference_mw
    assert isinstance(moved, list)
    assert isinstance(ok, bool)
    assert agg == agg


def test_proposed_grant_in_neighborhood_can_be_rejected(
    db_session: Session, synth_kml: Path
):
    load_dpas(db_session, kml_paths=[synth_kml])
    clear_activations(db_session)
    activate_dpa(
        db_session,
        {
            "dpaId": "IprOffshore",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    cbsd = make_cbsd(
        db_session,
        cbsd_id="c-hot",
        registration={
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": 38.02,
                "longitude": -75.02,
                "height": 10.0,
                "heightType": "AGL",
                "indoorDeployment": False,
            },
        },
    )
    assert isinstance(cbsd, Cbsd)
    assert proposed_grant_violates_dpa(
        db_session,
        cbsd,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=60.0,
        path_loss_fn=_stub_itm_fn(80.0),
    )


def test_interference_helper_skips_non_overlapping_freq():
    grant = DpaGrantRf(
        grant_id="g",
        cbsd_id="c",
        latitude=38.0,
        longitude=-75.0,
        height_m=10.0,
        height_is_agl=True,
        indoor=False,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
    )
    assert (
        interference_dbm_at_point(
            grant,
            lat_rx=38.01,
            lon_rx=-75.01,
            height_rx_m=50.0,
            low_hz=3_650_000_000,
            high_hz=3_660_000_000,
            path_loss_db=100.0,
        )
        is None
    )


def test_missing_geometry_fail_closed_moves_all_overlapping():
    from services.dpa_protection import ProtectedDpaChannel, evaluate_protected_channel

    channel = ProtectedDpaChannel(
        dpa_id="NoGeom",
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        reason=ProtectionReason.ACTIVE,
        geometry=None,
        neighborhood_km={},
        protection_params={},
        threshold_dbm_per_10mhz=-144.0,
        ref_height_m=50.0,
    )
    grants = [
        DpaGrantRf(
            grant_id="in-band",
            cbsd_id="c1",
            latitude=38.0,
            longitude=-75.0,
            height_m=10.0,
            height_is_agl=True,
            indoor=False,
            low_hz=3_550_000_000,
            high_hz=3_560_000_000,
            max_eirp_dbm_mhz=20.0,
        ),
        DpaGrantRf(
            grant_id="out-band",
            cbsd_id="c2",
            latitude=38.0,
            longitude=-75.0,
            height_m=10.0,
            height_is_agl=True,
            indoor=False,
            low_hz=3_650_000_000,
            high_hz=3_660_000_000,
            max_eirp_dbm_mhz=20.0,
        ),
    ]
    _, moved, agg, ok = evaluate_protected_channel(channel, grants)
    assert ok is False
    assert moved == ["in-band"]
    assert agg == float("inf")


def test_proposed_grant_rejected_when_aggregate_already_over(
    db_session: Session, synth_kml: Path
):
    """Even if incumbents already violate TH, a new neighborhood grant is 400."""
    from tests.fixtures.factories import make_grant

    load_dpas(db_session, kml_paths=[synth_kml])
    clear_activations(db_session)
    activate_dpa(
        db_session,
        {
            "dpaId": "IprOffshore",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    incumbent = make_cbsd(
        db_session,
        cbsd_id="c-inc",
        registration={
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": 38.02,
                "longitude": -75.02,
                "height": 10.0,
                "heightType": "AGL",
                "indoorDeployment": False,
            },
        },
    )
    make_grant(
        db_session,
        incumbent,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp=60.0,
    )
    newcomer = make_cbsd(
        db_session,
        cbsd_id="c-new",
        registration={
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": 38.021,
                "longitude": -75.021,
                "height": 10.0,
                "heightType": "AGL",
                "indoorDeployment": False,
            },
        },
    )
    assert proposed_grant_violates_dpa(
        db_session,
        newcomer,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
        path_loss_fn=_stub_itm_fn(80.0),
    )


def test_far_grant_excluded_from_neighborhood_movelist(
    db_session: Session, synth_kml: Path
):
    from services.dpa_protection import (
        filter_grants_in_neighborhood,
        list_protected_dpa_channels,
        collect_active_dpa_grants,
        ProtectionReason,
    )
    from tests.fixtures.factories import make_grant

    load_dpas(db_session, kml_paths=[synth_kml])
    clear_activations(db_session)
    activate_dpa(
        db_session,
        {
            "dpaId": "IprOffshore",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    near = make_cbsd(
        db_session,
        cbsd_id="c-near",
        registration={
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": 38.02,
                "longitude": -75.02,
                "height": 10.0,
                "heightType": "AGL",
                "indoorDeployment": False,
            },
        },
    )
    far = make_cbsd(
        db_session,
        cbsd_id="c-far",
        registration={
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": 10.0,
                "longitude": 10.0,
                "height": 10.0,
                "heightType": "AGL",
                "indoorDeployment": False,
            },
        },
    )
    make_grant(
        db_session,
        near,
        grant_id="g-near",
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp=20.0,
    )
    make_grant(
        db_session,
        far,
        grant_id="g-far",
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp=20.0,
    )
    channel = next(
        p
        for p in list_protected_dpa_channels(db_session)
        if p.dpa_id == "IprOffshore" and p.reason == ProtectionReason.ACTIVE
    )
    filtered = filter_grants_in_neighborhood(
        db_session, channel, collect_active_dpa_grants(db_session)
    )
    ids = {g.grant_id for g in filtered}
    assert "g-near" in ids
    assert "g-far" not in ids


def test_itm_required_without_median_raises_not_free_space():
    grant = DpaGrantRf(
        grant_id="g",
        cbsd_id="c",
        latitude=38.02,
        longitude=-75.02,
        height_m=10.0,
        height_is_agl=True,
        indoor=False,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
    )
    with pytest.raises(DpaPathLossUnavailable, match="ITM median"):
        rel1ext_dpa_path_loss_db(grant, 38.025, -75.025, 50.0)
    # Explicit Free Space profile still allowed.
    fs = rel1ext_dpa_path_loss_db(
        grant, 38.025, -75.025, 50.0, model=DpaPathLossModel.FREE_SPACE
    )
    assert fs == pytest.approx(
        free_space_path_loss_db(38.02, -75.02, 10.0, 38.025, -75.025, 50.0)
        + 0.0  # height > 6 m → clutter 0
        + ACTIVITY_LOSS_FACTOR_DB
    )


def test_itm_available_uses_injected_median_not_fs():
    grant = DpaGrantRf(
        grant_id="g",
        cbsd_id="c",
        latitude=38.02,
        longitude=-75.02,
        height_m=10.0,
        height_is_agl=True,
        indoor=False,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
    )
    fn = _stub_itm_fn(123.0)
    total = fn(grant, 38.025, -75.025, 50.0)
    assert total == pytest.approx(123.0 + ACTIVITY_LOSS_FACTOR_DB)
    fs_total = make_path_loss_fn(model=DpaPathLossModel.FREE_SPACE)(
        grant, 38.025, -75.025, 50.0
    )
    assert total != pytest.approx(fs_total)


def test_missing_itm_backend_evaluate_fail_closed_no_fs():
    from services.dpa_protection import ProtectedDpaChannel

    channel = ProtectedDpaChannel(
        dpa_id="X",
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        reason=ProtectionReason.ACTIVE,
        geometry={
            "type": "Polygon",
            "coordinates": [
                [
                    [-75.05, 38.05],
                    [-75.0, 38.05],
                    [-75.0, 38.0],
                    [-75.05, 38.0],
                    [-75.05, 38.05],
                ]
            ],
        },
        neighborhood_km={},
        protection_params={},
        threshold_dbm_per_10mhz=-144.0,
        ref_height_m=50.0,
    )
    grants = [
        DpaGrantRf(
            grant_id="g1",
            cbsd_id="c1",
            latitude=38.02,
            longitude=-75.02,
            height_m=10.0,
            height_is_agl=True,
            indoor=False,
            low_hz=3_550_000_000,
            high_hz=3_560_000_000,
            max_eirp_dbm_mhz=20.0,
        )
    ]
    missing = make_path_loss_fn(model=DpaPathLossModel.ITM_REL1EXT, itm_median_fn=None)
    contribs, moved, agg, ok = evaluate_protected_channel(
        channel, grants, path_loss_fn=missing
    )
    assert contribs == []
    assert moved == ["g1"]
    assert ok is False
    assert agg == float("inf")


def test_default_rel1ext_fn_without_engines_is_unavailable(monkeypatch):
    from services import dpa_protection as mod

    def _boom():
        raise DpaPathLossUnavailable("ITM extension module not available")

    monkeypatch.setattr(mod, "load_rel1ext_backends", _boom)
    fn = default_rel1ext_path_loss_fn()
    grant = DpaGrantRf(
        grant_id="g",
        cbsd_id="c",
        latitude=38.0,
        longitude=-75.0,
        height_m=10.0,
        height_is_agl=True,
        indoor=False,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
    )
    with pytest.raises(DpaPathLossUnavailable):
        fn(grant, 38.01, -75.01, 50.0)


def test_explicit_free_space_profile_allowed():
    fn = make_path_loss_fn(model=DpaPathLossModel.FREE_SPACE)
    grant = DpaGrantRf(
        grant_id="g",
        cbsd_id="c",
        latitude=38.02,
        longitude=-75.02,
        height_m=10.0,
        height_is_agl=True,
        indoor=False,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
    )
    total = fn(grant, 38.025, -75.025, 50.0)
    assert total > ACTIVITY_LOSS_FACTOR_DB
