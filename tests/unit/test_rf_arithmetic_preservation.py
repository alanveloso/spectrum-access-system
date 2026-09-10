"""generic RF arithmetic matches IAP/DPA linear conversions."""

from __future__ import annotations

import math

from primitives.power import dbm_to_mw, mw_to_dbm
from primitives.rf_arithmetic import (
    db_from_linear,
    linear_from_db,
    received_power_dbm,
    received_power_mw,
    sum_linear_mw,
    within_threshold_dbm,
    within_threshold_mw,
)
from services.iap.aggregate import dbm_to_mw as iap_dbm_to_mw
from services.iap.aggregate import mw_to_dbm as iap_mw_to_dbm
from services.iap.aggregate import sum_interference_mw
from services.iap.coupling import effective_system_eirp_dbm, make_iap_coupling
from services.iap.models import (
    FrequencyChannel,
    GrantRfInfo,
    ProtectedEntityKind,
    ProtectionPoint,
)


def test_iap_power_converters_are_primitive_reexports():
    assert iap_dbm_to_mw is dbm_to_mw
    assert iap_mw_to_dbm is mw_to_dbm
    assert sum_interference_mw([1.0, -2.0, 0.5]) == sum_linear_mw([1.0, -2.0, 0.5])


def test_single_link_and_threshold_identity():
    eirp, pl = 23.0, 110.0
    assert received_power_dbm(eirp, pl) == eirp - pl
    assert received_power_mw(eirp, pl) == dbm_to_mw(eirp - pl)
    assert within_threshold_mw(1.0, 1.0) is True
    assert within_threshold_mw(1.0 + 2e-15, 1.0) is False
    assert within_threshold_dbm(-144.0, -144.0, 0.0) is True
    assert within_threshold_dbm(-143.0, -144.0, 0.0) is False


def test_make_iap_coupling_uses_received_power_mw():
    grant = GrantRfInfo(
        grant_id="g",
        cbsd_id="c",
        latitude=38.0,
        longitude=-77.0,
        height_m=6.0,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
    )
    point = ProtectionPoint(
        point_id="p",
        latitude=38.01,
        longitude=-77.01,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=-80.0,
        entity_kind=ProtectedEntityKind.GENERIC,
    )
    channel = FrequencyChannel(low_hz=3_550_000_000, high_hz=3_555_000_000)
    coupling = make_iap_coupling(path_loss_db_fn=lambda *_a: 100.0)
    got = coupling(grant, point, channel, 20.0)
    assert got == received_power_mw(20.0, 100.0)


def test_esc_eirp_bandwidth_term_uses_db_from_linear():
    # Same term as former 10*log10(RBW/1e6) with RBW=5e6.
    expect = 10.0 * math.log10(5.0)
    assert db_from_linear(5.0) == expect
    got = effective_system_eirp_dbm(10.0, 0.0, 0.0, reference_bandwidth_hz=5_000_000.0)
    assert got == 10.0 + expect
    assert linear_from_db(-3.0) == 10.0 ** (-3.0 / 10.0)
