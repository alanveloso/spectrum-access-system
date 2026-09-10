"""Multi-profile isolation, determinism, and compatibility regression."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from primitives.time import TimeInterval, UtcInstant
from providers.discovery import DataProviderDiscovery
from profiles.selection import (
    DEFAULT_PROFILE_ID,
    active_profile_id,
    clear_profile_override,
)
from profiles import get_active_profile_document, primary_spectrum_range
from profiles.context import profile_context_from_document, profile_hash
from profiles.cost import measure_profile_cost
from profiles.doctor import run_profile_doctor
from profiles.parse import load_profile

_PROFILE_IDS = (
    "cbrs_winnforum",
    "br_anatel_slp_3700",
    "eu_elsa",
    "us_tvws_15_711",
)

_REPO = Path(__file__).resolve().parents[2]


def test_all_campaign_profiles_load_doctor_and_isolate_hashes() -> None:
    parsed = {pid: load_profile(pid) for pid in _PROFILE_IDS}
    hashes = {pid: profile_hash(doc) for pid, doc in parsed.items()}
    assert len(set(hashes.values())) == len(_PROFILE_IDS)
    for pid, doc in parsed.items():
        assert doc.metadata.id == pid
        report = run_profile_doctor(profile_id=pid, check_plugins=False)
        assert report.ok, (pid, [f for f in report.findings if not f.ok])
        ctx = profile_context_from_document(doc)
        assert ctx.profile_id == pid
        assert ctx.profile_hash == hashes[pid]


def test_profile_hash_deterministic_across_repeated_loads() -> None:
    for pid in _PROFILE_IDS:
        first = profile_hash(load_profile(pid))
        again = [profile_hash(load_profile(pid)) for _ in range(5)]
        assert set(again) == {first}


def test_profile_hash_invariant_under_process_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    baseline = {pid: profile_hash(load_profile(pid)) for pid in _PROFILE_IDS}

    monkeypatch.setenv("TZ", "America/Sao_Paulo")
    if hasattr(time, "tzset"):
        time.tzset()
    shifted = {pid: profile_hash(load_profile(pid)) for pid in _PROFILE_IDS}
    assert shifted == baseline

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    if hasattr(time, "tzset"):
        time.tzset()
    again = {pid: profile_hash(load_profile(pid)) for pid in _PROFILE_IDS}
    assert again == baseline


def test_concurrent_multi_profile_loads_preserve_hashes() -> None:
    expected = {pid: profile_hash(load_profile(pid)) for pid in _PROFILE_IDS}

    def _one(pid: str) -> tuple[str, str, str]:
        doc = load_profile(pid)
        return pid, doc.metadata.id, profile_hash(doc)

    jobs = list(_PROFILE_IDS) * 8
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_one, pid) for pid in jobs]
        results = [fut.result() for fut in as_completed(futures)]

    assert len(results) == len(jobs)
    for pid, meta_id, digest in results:
        assert meta_id == pid
        assert digest == expected[pid]


def test_utc_instant_and_interval_timezone_normalization() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        UtcInstant(datetime(2026, 8, 20, 12, 0, 0))

    utc = datetime(2026, 8, 20, 15, 0, 0, tzinfo=timezone.utc)
    sp = utc.astimezone(ZoneInfo("America/Sao_Paulo"))
    tokyo = utc.astimezone(ZoneInfo("Asia/Tokyo"))
    assert UtcInstant(sp).value == UtcInstant(utc).value
    assert UtcInstant(tokyo).value == UtcInstant(utc).value

    start_sp = datetime(2026, 8, 20, 9, 0, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
    end_sp = start_sp + timedelta(hours=1)
    interval = TimeInterval.from_datetimes(start_sp, end_sp)
    assert interval.start.value.tzinfo == timezone.utc
    inside = UtcInstant(start_sp + timedelta(minutes=30))
    outside = UtcInstant(end_sp)
    assert interval.contains(inside) is True
    assert interval.contains(outside) is False


def test_dataset_versions_recorded_and_doctor_fail_closed_without_providers() -> None:
    # Profiles that declare data capabilities must surface them on ProfileContext.
    for pid in ("cbrs_winnforum", "br_anatel_slp_3700", "eu_elsa", "us_tvws_15_711"):
        doc = load_profile(pid)
        ctx = profile_context_from_document(doc)
        if doc.data is not None and doc.data.required_capabilities:
            assert ctx.dataset_versions
            for cap in doc.data.required_capabilities:
                assert (cap, "required") in ctx.dataset_versions
        else:
            assert ctx.dataset_versions == ()

    empty = DataProviderDiscovery(overlays={}, list_entry_points=lambda _g: ())
    report = run_profile_doctor(
        profile_id="cbrs_winnforum",
        check_plugins=True,
        require_data_plugins=True,
        data_discovery=empty,
    )
    assert report.ok is False
    assert any(not f.ok for f in report.findings)


def test_default_active_profile_remains_cbrs() -> None:
    clear_profile_override()
    assert DEFAULT_PROFILE_ID == "cbrs_winnforum"
    assert active_profile_id() == "cbrs_winnforum"
    active = get_active_profile_document()
    assert active.metadata.id == "cbrs_winnforum"
    assert active.metadata.version == "2.0.0"
    band = primary_spectrum_range(active)
    assert band.low_hz == 3_550_000_000
    assert band.high_hz == 3_700_000_000


def test_load_br_does_not_mutate_cbrs_active_or_band() -> None:
    clear_profile_override()
    before = get_active_profile_document()
    br = load_profile("br_anatel_slp_3700")
    after = get_active_profile_document()
    assert br.metadata.id == "br_anatel_slp_3700"
    assert before.metadata.id == after.metadata.id == "cbrs_winnforum"
    assert primary_spectrum_range(before).low_hz == primary_spectrum_range(after).low_hz
    assert primary_spectrum_range(before).high_hz == primary_spectrum_range(after).high_hz
    assert active_profile_id() == "cbrs_winnforum"
    assert br.metadata.id != active_profile_id()


def test_multi_profile_contexts_are_isolated() -> None:
    cbrs = load_profile("cbrs_winnforum")
    br = load_profile("br_anatel_slp_3700")
    ctx_cbrs = profile_context_from_document(cbrs)
    ctx_br = profile_context_from_document(br)
    assert ctx_cbrs.profile_id == "cbrs_winnforum"
    assert ctx_br.profile_id == "br_anatel_slp_3700"
    assert ctx_cbrs.profile_hash != ctx_br.profile_hash
    assert ctx_cbrs.profile_hash == profile_hash(cbrs)
    assert ctx_br.profile_hash == profile_hash(br)
    # Distinct regime shapes
    assert cbrs.access is not None
    assert br.access is None
    assert cbrs.authorization is not None and cbrs.authorization.mechanism == "dynamic_lease"
    assert br.authorization is not None and br.authorization.mechanism == "static_authorization"
    assert cbrs.rf is not None and cbrs.rf.required is True
    assert br.rf is not None and br.rf.required is False
    assert cbrs.constraints == ()
    assert len(br.constraints) >= 4


def test_both_reference_profiles_pass_doctor() -> None:
    for profile_id in ("cbrs_winnforum", "br_anatel_slp_3700"):
        report = run_profile_doctor(profile_id=profile_id)
        assert report.ok, profile_id + ": " + "; ".join(
            f"{f.name}={f.detail}" for f in report.findings if not f.ok
        )


def test_mechanism_reuse_full_catalog_for_both_profiles() -> None:
    for profile_id in ("cbrs_winnforum", "br_anatel_slp_3700"):
        cost = measure_profile_cost(profile_id=profile_id, repo_root=_REPO)
        assert cost.mechanism_reuse_pct == 100.0, profile_id
        assert cost.mechanisms_novel == ()
        assert cost.profile_python_loc == 0


def test_builtin_catalog_loads_non_cbrs_by_id() -> None:
    """Non-CBRS reference profiles resolve from the builtin catalog."""
    br = load_profile("br_anatel_slp_3700")
    assert br.metadata.id == "br_anatel_slp_3700"
    assert br.spectrum.ranges[0].low_hz == 3_700_000_000


def test_interleaved_loads_preserve_invariants() -> None:
    """Alternate loads; CBRS band and BR constraints remain stable."""
    for _ in range(3):
        cbrs = load_profile("cbrs_winnforum")
        br = load_profile("br_anatel_slp_3700")
        assert cbrs.spectrum.ranges[0].high_hz == 3_700_000_000
        assert br.spectrum.ranges[0].low_hz == 3_700_000_000
        assert [c.mechanism for c in br.constraints][0] == "duplex_mode"
