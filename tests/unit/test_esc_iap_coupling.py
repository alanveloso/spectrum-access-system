"""ESC IAP coupling parity (antenna wiring + reference ESC RF semantics)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.cpas_service import FrozenLocalGrantRf, frozen_to_iap_grant_rf
from services.iap.aggregate import ESC_CAT_A_HIGH_FREQ_HZ, grant_overlaps_channel
from services.iap.coupling import (
    ESC_FREQ_PROP_MHZ,
    ESC_IN_BAND_INSERTION_LOSS_DB,
    ESC_MASK_EDGE_HZ,
    ESC_PASSBAND_HIGH_HZ,
    ESC_PASSBAND_LOW_HZ,
    ESC_RBW_HZ,
    IapCouplingUnavailable,
    effective_system_eirp_dbm,
    esc_mask_loss_db,
    make_esc_iap_coupling,
    make_iap_coupling,
    make_production_iap_coupling,
)
from services.iap.models import (
    FrequencyChannel,
    GrantRfInfo,
    ProtectedEntityKind,
    ProtectionPoint,
)
from services.iap.peer_fad import grant_rf_infos_from_peer_cbsd_record
from services.iap.protection_points import (
    ProtectionEntityError,
    protection_point_from_esc_sensor_record,
)


def _pattern(gain: float = 30.0) -> tuple[float, ...]:
    return tuple(float(gain) for _ in range(360))


def _esc_record(
    *,
    height: float = 9.3,
    azimuth: float = 120.0,
    pattern_gain: float = 30.0,
    include_pattern: bool = True,
    include_height: bool = True,
    include_azimuth: bool = True,
    freq: tuple[int, int] | None = None,
) -> dict:
    install: dict = {
        "latitude": 40.0,
        "longitude": -100.0,
    }
    if include_height:
        install["height"] = height
        install["heightType"] = "AGL"
    if include_azimuth:
        install["antennaAzimuth"] = azimuth
    if include_pattern:
        install["azimuthRadiationPattern"] = [
            {"angle": i, "gain": pattern_gain} for i in range(360)
        ]
    rec: dict = {
        "id": "esc_sensor/unit/0",
        "installationParam": install,
    }
    if freq is not None:
        rec["protectionFrequencyRange"] = {
            "lowFrequency": freq[0],
            "highFrequency": freq[1],
        }
    return rec


def _grant_rf(**extra) -> GrantRfInfo:
    base = dict(
        grant_id="g1",
        cbsd_id="cbsd/1",
        latitude=40.01,
        longitude=-100.01,
        height_m=3.4,
        height_is_agl=True,
        indoor=True,
        low_hz=3_630_000_000,
        high_hz=3_640_000_000,
        max_eirp_dbm_mhz=10.0,
        is_managing_sas=True,
        cbsd_category="A",
        antenna_azimuth_deg=0.0,
        antenna_beamwidth_deg=60.0,
        antenna_gain_dbi=90.0,
    )
    base.update(extra)
    return GrantRfInfo(**base)


def _esc_point(**extra) -> ProtectionPoint:
    base = dict(
        point_id="esc:unit",
        latitude=40.0,
        longitude=-100.0,
        low_hz=ESC_PASSBAND_LOW_HZ,
        high_hz=ESC_PASSBAND_HIGH_HZ,
        threshold_dbm=-109.0,
        entity_kind=ProtectedEntityKind.ESC,
        receiver_height_m=9.3,
        receiver_antenna_azimuth_deg=120.0,
        receiver_antenna_gain_pattern_dbi=_pattern(30.0),
    )
    base.update(extra)
    return ProtectionPoint(**base)


# --- A: local antenna wiring ---


def test_a_frozen_local_antenna_fields_reach_grant_rf_info():
    frozen = FrozenLocalGrantRf(
        grant_pk=1,
        grant_id="grant/1",
        cbsd_id="fcc/sn",
        fcc_id="fcc",
        cbsd_serial_number="sn",
        low_hz=3_630_000_000,
        high_hz=3_640_000_000,
        max_eirp_dbm_mhz=10.0,
        lifecycle_state="AUTHORIZED",
        terminated=False,
        latitude=40.0,
        longitude=-100.0,
        height_m=3.4,
        height_type="AGL",
        indoor=True,
        cbsd_category="A",
        antenna_azimuth=0.0,
        antenna_beamwidth=60.0,
        antenna_gain=90.0,
    )
    g = frozen_to_iap_grant_rf(frozen)
    assert g is not None
    assert g.antenna_azimuth_deg == 0.0
    assert g.antenna_beamwidth_deg == 60.0
    assert g.antenna_gain_dbi == 90.0


# --- B: peer antenna wiring ---


def test_b_peer_nested_installation_antenna_fields():
    record = {
        "id": "cbsd/peer-a",
        "registration": {
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": 40.0,
                "longitude": -100.0,
                "height": 3.4,
                "heightType": "AGL",
                "indoorDeployment": True,
                "antennaAzimuth": 15.0,
                "antennaBeamwidth": 45.0,
                "antennaGain": 12.5,
            },
        },
        "grants": [
            {
                "id": "SAMPLE_1",
                "terminated": False,
                "operationParam": {
                    "maxEirp": 10.0,
                    "operationFrequencyRange": {
                        "lowFrequency": 3_630_000_000,
                        "highFrequency": 3_640_000_000,
                    },
                },
            }
        ],
    }
    rows = grant_rf_infos_from_peer_cbsd_record(record, source_sas_id="2")
    assert len(rows) == 1
    assert rows[0].antenna_azimuth_deg == 15.0
    assert rows[0].antenna_beamwidth_deg == 45.0
    assert rows[0].antenna_gain_dbi == 12.5


def test_b_peer_top_level_antenna_fallback():
    record = {
        "id": "cbsd/peer-b",
        "cbsdCategory": "B",
        "installationParam": {
            "latitude": 40.0,
            "longitude": -100.0,
            "height": 7.0,
            "heightType": "AGL",
            "indoorDeployment": False,
            "antennaAzimuth": 90.0,
            "antennaBeamwidth": 30.0,
            "antennaGain": 16.0,
        },
        "grants": [
            {
                "id": "g",
                "terminated": False,
                "operationParam": {
                    "maxEirp": 20.0,
                    "operationFrequencyRange": {
                        "lowFrequency": 3_550_000_000,
                        "highFrequency": 3_560_000_000,
                    },
                },
            }
        ],
    }
    rows = grant_rf_infos_from_peer_cbsd_record(record, source_sas_id="1")
    assert rows[0].antenna_gain_dbi == 16.0
    assert rows[0].antenna_azimuth_deg == 90.0
    assert rows[0].antenna_beamwidth_deg == 30.0


# --- C / D: ESC metadata + passband ---


def test_c_esc_sensor_preserves_antenna_metadata():
    pt = protection_point_from_esc_sensor_record(
        _esc_record(), record_id="esc_sensor/unit/0"
    )
    assert pt is not None
    assert pt.entity_kind is ProtectedEntityKind.ESC
    assert pt.receiver_height_m == 9.3
    assert pt.receiver_antenna_azimuth_deg == 120.0
    assert pt.receiver_antenna_gain_pattern_dbi is not None
    assert len(pt.receiver_antenna_gain_pattern_dbi) == 360
    assert pt.receiver_antenna_gain_pattern_dbi[0] == 30.0
    assert pt.receiver_antenna_gain_pattern_dbi[359] == 30.0


def test_d_esc_default_passband_3550_3680():
    pt = protection_point_from_esc_sensor_record(
        _esc_record(), record_id="esc_sensor/unit/0"
    )
    assert pt is not None
    assert pt.low_hz == ESC_PASSBAND_LOW_HZ
    assert pt.high_hz == ESC_PASSBAND_HIGH_HZ


# --- E / F / G / H: ESC coupling math ---


def test_e_esc_coupling_reference_vector_with_stubbed_itm():
    """Independently supplied parameters + stubbed ITM/incidence."""

    def itm_stub(grant, point, **_kw):
        del grant, point
        return SimpleNamespace(
            db_loss=123.33285143848266,
            incidence_angles=SimpleNamespace(
                hor_cbsd=171.26022279816468,
                hor_rx=351.26210542841386,
                ver_rx=0.0,
            ),
        )

    def cbsd_gain(hor_cbsd, az, bw, peak):
        assert hor_cbsd == pytest.approx(171.26022279816468)
        assert az == 0.0 and bw == 60.0 and peak == 90.0
        return 70.0

    def esc_gain(hor_rx, az, pattern):
        assert hor_rx == pytest.approx(351.26210542841386)
        assert az == 120.0
        assert len(pattern) == 360
        return 30.0

    coupling = make_esc_iap_coupling(
        itm_result_fn=itm_stub,
        cbsd_antenna_gain_fn=cbsd_gain,
        esc_antenna_gain_fn=esc_gain,
    )
    grant = _grant_rf()
    point = _esc_point()
    ch = FrequencyChannel(low_hz=3_635_000_000, high_hz=3_640_000_000)
    mw = coupling(grant, point, ch, 10.0)

    eff = effective_system_eirp_dbm(10.0, 90.0, 70.0 + 30.0)
    mask = esc_mask_loss_db(ch)
    expected_dbm = eff - 123.33285143848266 - mask
    expected_mw = 10.0 ** (expected_dbm / 10.0)
    assert mw == pytest.approx(expected_mw, rel=0, abs=0.0)
    assert expected_dbm == pytest.approx(-96.84315139512248, rel=0, abs=1e-9)


def test_f_esc_itm_call_contract(monkeypatch):
    calls: list[dict] = []

    def fake_itm(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(
            db_loss=100.0,
            incidence_angles=SimpleNamespace(hor_cbsd=10.0, hor_rx=20.0, ver_rx=0.0),
        )

    monkeypatch.setattr(
        "services.propagation.engines.load_reference_engines",
        lambda: SimpleNamespace(
            calc_itm=fake_itm,
            antenna_standard_gains=lambda *a, **k: 0.0,
            antenna_pattern_gains=lambda *a, **k: 0.0,
        ),
    )
    coupling = make_esc_iap_coupling()
    grant = _grant_rf(indoor=True, height_is_agl=True, height_m=3.4)
    point = _esc_point(receiver_height_m=9.3)
    ch = FrequencyChannel(low_hz=3_550_000_000, high_hz=3_555_000_000)
    coupling(grant, point, ch, 10.0)
    assert len(calls) == 1
    args = calls[0]["args"]
    kwargs = calls[0]["kwargs"]
    assert args[0] == grant.latitude
    assert args[1] == grant.longitude
    assert args[2] == grant.height_m
    assert args[3] == point.latitude
    assert args[4] == point.longitude
    assert args[5] == 9.3  # ESC antenna height
    assert kwargs.get("cbsd_indoor") is True
    assert kwargs.get("reliability") == -1
    assert kwargs.get("freq_mhz") == ESC_FREQ_PROP_MHZ
    assert kwargs.get("is_height_cbsd_amsl") is False


def test_g_esc_mask_in_band():
    ch = FrequencyChannel(low_hz=3_635_000_000, high_hz=3_640_000_000)
    assert esc_mask_loss_db(ch) == ESC_IN_BAND_INSERTION_LOSS_DB
    assert ch.high_hz <= ESC_MASK_EDGE_HZ


def test_h_esc_mask_out_of_band():
    ch = FrequencyChannel(low_hz=3_650_000_000, high_hz=3_655_000_000)
    # Reference getEscMaskLoss: 1 MHz bins @ 3650.5..3654.5 → ~2.772 dB
    assert esc_mask_loss_db(ch) == pytest.approx(2.7723321858267305, abs=1e-9)


def test_h_esc_mask_inconsistent_crossing_3650():
    ch = FrequencyChannel(low_hz=3_648_000_000, high_hz=3_653_000_000)
    with pytest.raises(IapCouplingUnavailable):
        esc_mask_loss_db(ch)


def test_effective_system_eirp_matches_reference_formula():
    # (10 - 90) + 100 + 10*log10(5) 
    got = effective_system_eirp_dbm(10.0, 90.0, 100.0, reference_bandwidth_hz=ESC_RBW_HZ)
    assert got == pytest.approx(26.989700043360187, abs=1e-12)


# --- I: non-ESC unchanged ---


def test_i_generic_coupling_not_esc_semantics():
    def pl(_g, _p, _c):
        return 50.0

    generic = make_iap_coupling(path_loss_db_fn=pl)
    point = ProtectionPoint(
        point_id="ppa:1",
        latitude=40.0,
        longitude=-100.0,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        threshold_dbm=-80.0,
        entity_kind=ProtectedEntityKind.PPA,
    )
    grant = _grant_rf()
    ch = FrequencyChannel(low_hz=3_550_000_000, high_hz=3_555_000_000)
    mw = generic(grant, point, ch, 10.0)
    assert mw == pytest.approx(10.0 ** ((10.0 - 50.0) / 10.0))


def test_i_production_dispatch_uses_generic_for_non_esc():
    coupling = make_production_iap_coupling(path_loss_model="free_space")
    point = ProtectionPoint(
        point_id="gwpz:1",
        latitude=40.0,
        longitude=-100.0,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        threshold_dbm=-80.0,
        entity_kind=ProtectedEntityKind.GWPZ,
    )
    grant = _grant_rf(latitude=40.0, longitude=-100.0, height_m=1.5)
    ch = FrequencyChannel(low_hz=3_550_000_000, high_hz=3_555_000_000)
    mw = coupling(grant, point, ch, 0.0)
    assert mw >= 0.0
    assert point.entity_kind is not ProtectedEntityKind.ESC


# --- J: fail closed ---


def test_j_missing_esc_antenna_metadata_fail_closed():
    with pytest.raises(ProtectionEntityError):
        protection_point_from_esc_sensor_record(
            _esc_record(include_pattern=False), record_id="esc_sensor/unit/0"
        )


def test_j_missing_grant_antenna_fail_closed_at_coupling():
    coupling = make_esc_iap_coupling(
        itm_result_fn=lambda *_a, **_k: SimpleNamespace(
            db_loss=100.0,
            incidence_angles=SimpleNamespace(hor_cbsd=0.0, hor_rx=0.0, ver_rx=0.0),
        ),
        cbsd_antenna_gain_fn=lambda *_a, **_k: 0.0,
        esc_antenna_gain_fn=lambda *_a, **_k: 0.0,
    )
    grant = _grant_rf(antenna_gain_dbi=None, antenna_azimuth_deg=None, antenna_beamwidth_deg=None)
    point = _esc_point()
    ch = FrequencyChannel(low_hz=3_550_000_000, high_hz=3_555_000_000)
    with pytest.raises(IapCouplingUnavailable):
        coupling(grant, point, ch, 10.0)


# --- Cat-A ESC rule ---


def test_cat_a_below_3660_overlaps():
    g = _grant_rf(cbsd_category="A", low_hz=3_630_000_000, high_hz=3_640_000_000)
    ch = FrequencyChannel(low_hz=3_635_000_000, high_hz=3_640_000_000)
    assert grant_overlaps_channel(g, ch, entity_kind=ProtectedEntityKind.ESC) is True


def test_cat_a_at_or_above_3660_excluded():
    g = _grant_rf(cbsd_category="A", low_hz=3_650_000_000, high_hz=3_660_000_000)
    ch = FrequencyChannel(low_hz=3_655_000_000, high_hz=3_660_000_000)
    assert ch.low_hz < ESC_CAT_A_HIGH_FREQ_HZ
    ch3660 = FrequencyChannel(low_hz=ESC_CAT_A_HIGH_FREQ_HZ, high_hz=ESC_CAT_A_HIGH_FREQ_HZ + 5_000_000)
    assert (
        grant_overlaps_channel(g, ch3660, entity_kind=ProtectedEntityKind.ESC) is False
    )
    g_high = _grant_rf(
        cbsd_category="A",
        low_hz=ESC_CAT_A_HIGH_FREQ_HZ,
        high_hz=ESC_CAT_A_HIGH_FREQ_HZ + 5_000_000,
    )
    ch_low = FrequencyChannel(low_hz=3_550_000_000, high_hz=3_555_000_000)
    assert (
        grant_overlaps_channel(g_high, ch_low, entity_kind=ProtectedEntityKind.ESC)
        is False
    )


def test_cat_b_through_esc_passband_to_3680():
    g = _grant_rf(
        cbsd_category="B",
        low_hz=3_670_000_000,
        high_hz=3_680_000_000,
        antenna_gain_dbi=16.0,
    )
    ch = FrequencyChannel(low_hz=3_670_000_000, high_hz=3_675_000_000)
    assert grant_overlaps_channel(g, ch, entity_kind=ProtectedEntityKind.ESC) is True
    assert grant_overlaps_channel(g, ch, entity_kind=ProtectedEntityKind.PPA) is True


def test_cat_a_rule_does_not_apply_to_ppa():
    g = _grant_rf(
        cbsd_category="A",
        low_hz=ESC_CAT_A_HIGH_FREQ_HZ,
        high_hz=ESC_CAT_A_HIGH_FREQ_HZ + 5_000_000,
    )
    ch = FrequencyChannel(
        low_hz=ESC_CAT_A_HIGH_FREQ_HZ, high_hz=ESC_CAT_A_HIGH_FREQ_HZ + 5_000_000
    )
    assert grant_overlaps_channel(g, ch, entity_kind=ProtectedEntityKind.PPA) is True
