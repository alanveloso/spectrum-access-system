"""Phase D: selective Alembic targeting from PersistencePlan."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, String, create_engine, inspect
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.migration_domains import (
    CBRS_FROM_GENERIC_REVISION,
    CBRS_HISTORICAL_REVISION,
    GENERIC_REVISION,
    resolve_migration_targets,
)
from models.persistence import (
    CBRS_WINNFORUM_ONLY_TABLES,
    GENERIC_TABLES,
    PersistenceContribution,
    PersistencePlan,
    clear_extra_persistence_contributions,
    reference_persistence_plan,
    register_persistence_contribution,
    resolve_persistence_plan,
)
from models.registry import REFERENCE_REQUIRED_TABLES, load_all_models
from services.migrations import (
    HEAD_REVISION,
    apply_schema,
    current_revision,
    upgrade_head,
    upgrade_to,
)
from services.schema_fingerprint import schema_fingerprint
from tests.fixtures.synthetic_profile import synthetic_deployment, synthetic_profile


@pytest.fixture
def no_create_all(monkeypatch):
    monkeypatch.delenv("SAS_SCHEMA_VIA_CREATE_ALL", raising=False)


def test_migration_targets_deterministic_for_synthetic():
    from runtime import PluginRegistry, compose_runtime

    composition = compose_runtime(
        synthetic_profile(),
        synthetic_deployment(),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    a = resolve_migration_targets(plan)
    b = resolve_migration_targets(plan)
    assert a == b
    assert a.upgrade_revision == GENERIC_REVISION
    assert a.domain_ids == ("generic",)


def test_migration_targets_cbrs_uses_historical_head():
    targets = resolve_migration_targets(reference_persistence_plan())
    assert targets.upgrade_revision == CBRS_HISTORICAL_REVISION
    assert targets.upgrade_revision == HEAD_REVISION


def test_production_path_generic_excludes_cbrs(tmp_path: Path, no_create_all):
    load_all_models()
    url = f"sqlite:///{tmp_path / 'generic_prod.db'}"
    eng = create_engine(url)
    try:
        plan = PersistencePlan(
            contribution_ids=("generic",),
            table_names=GENERIC_TABLES,
        )
        apply_schema(eng, tables=plan.tables(), persistence_plan=plan)
        present = set(inspect(eng).get_table_names())
        present.discard("alembic_version")
        assert GENERIC_TABLES <= present
        assert present & CBRS_WINNFORUM_ONLY_TABLES == set()
        assert current_revision(database_url=url) == GENERIC_REVISION
    finally:
        eng.dispose()


def test_production_path_cbrs_creates_reference(tmp_path: Path, no_create_all):
    load_all_models()
    url = f"sqlite:///{tmp_path / 'cbrs_prod.db'}"
    eng = create_engine(url)
    try:
        plan = reference_persistence_plan()
        apply_schema(eng, tables=plan.tables(), persistence_plan=plan)
        present = set(inspect(eng).get_table_names())
        assert REFERENCE_REQUIRED_TABLES <= present
        assert current_revision(database_url=url) == CBRS_HISTORICAL_REVISION
    finally:
        eng.dispose()


def test_cbrs_schema_fingerprint_matches_historical_upgrade(
    tmp_path: Path, no_create_all
):
    load_all_models()
    url_a = f"sqlite:///{tmp_path / 'hist.db'}"
    url_b = f"sqlite:///{tmp_path / 'plan.db'}"
    upgrade_head(database_url=url_a)
    eng_b = create_engine(url_b)
    try:
        plan = reference_persistence_plan()
        apply_schema(eng_b, tables=plan.tables(), persistence_plan=plan)
        eng_a = create_engine(url_a)
        try:
            assert schema_fingerprint(eng_a) == schema_fingerprint(eng_b)
        finally:
            eng_a.dispose()
    finally:
        eng_b.dispose()


def test_generic_then_cbrs_from_generic_upgrade(tmp_path: Path, no_create_all):
    load_all_models()
    url = f"sqlite:///{tmp_path / 'promote.db'}"
    eng = create_engine(url)
    try:
        generic = PersistencePlan(
            contribution_ids=("generic",),
            table_names=GENERIC_TABLES,
        )
        apply_schema(eng, tables=generic.tables(), persistence_plan=generic)
        assert current_revision(database_url=url) == GENERIC_REVISION
        present = set(inspect(eng).get_table_names())
        assert "cbsds" not in present

        cbrs = reference_persistence_plan()
        apply_schema(eng, tables=cbrs.tables(), persistence_plan=cbrs)
        assert current_revision(database_url=url) == CBRS_FROM_GENERIC_REVISION
        present = set(inspect(eng).get_table_names())
        assert REFERENCE_REQUIRED_TABLES <= present
    finally:
        eng.dispose()


def test_legacy_cbrs_head_preserves_data(tmp_path: Path, no_create_all):
    load_all_models()
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    upgrade_to(database_url=url, revision=CBRS_HISTORICAL_REVISION)
    eng = create_engine(url)
    Session = sessionmaker(bind=eng)
    session = Session()
    try:
        from models.models import Cbsd, Grant

        cbsd = Cbsd(
            cbsd_id="fcc/serial-d",
            fcc_id="fcc",
            user_id="user",
            cbsd_serial_number="serial-d",
            lifecycle_state="REGISTERED",
            registration_json="{}",
        )
        session.add(cbsd)
        session.flush()
        from datetime import datetime, timezone

        grant = Grant(
            grant_id="g-d-1",
            cbsd_pk=cbsd.id,
            cbsd_id=cbsd.cbsd_id,
            channel_type="GAA",
            low_frequency=3550_000_000,
            high_frequency=3560_000_000,
            grant_expire_time=datetime(2026, 9, 4, tzinfo=timezone.utc),
            heartbeat_interval=60,
            authorized=False,
            meas_report_requested=False,
            terminated=False,
            lifecycle_state="GRANTED",
            grant_json="{}",
        )
        session.add(grant)
        session.commit()
    finally:
        session.close()

    plan = reference_persistence_plan()
    apply_schema(eng, tables=plan.tables(), persistence_plan=plan)
    assert current_revision(database_url=url) == CBRS_HISTORICAL_REVISION
    session = Session()
    try:
        from models.models import Cbsd, Grant

        assert session.query(Cbsd).filter_by(cbsd_id="fcc/serial-d").one()
        assert session.query(Grant).filter_by(grant_id="g-d-1").one()
    finally:
        session.close()
        eng.dispose()


def test_unselected_plugin_domain_not_materialized(tmp_path: Path, no_create_all):
    load_all_models()
    marker = "phase_d_unselected_marker"

    class Marker(Base):
        __tablename__ = marker
        id = Column(Integer, primary_key=True)
        label = Column(String(8), nullable=False, default="x")

    register_persistence_contribution(
        PersistenceContribution(
            contribution_id="phase_d_unselected",
            table_names=frozenset({marker}),
            matches=lambda _c: False,
            origin="plugin",
        )
    )
    try:
        url = f"sqlite:///{tmp_path / 'unselected.db'}"
        eng = create_engine(url)
        try:
            plan = PersistencePlan(
                contribution_ids=("generic",),
                table_names=GENERIC_TABLES,
            )
            apply_schema(eng, tables=plan.tables(), persistence_plan=plan)
            present = set(inspect(eng).get_table_names())
            assert marker not in present
        finally:
            eng.dispose()
    finally:
        clear_extra_persistence_contributions()
        if marker in Base.metadata.tables:
            Base.metadata.remove(Base.metadata.tables[marker])


def test_selected_plugin_domain_materialized(tmp_path: Path, no_create_all):
    load_all_models()
    marker = "phase_d_selected_marker"

    class Marker(Base):
        __tablename__ = marker
        id = Column(Integer, primary_key=True)
        label = Column(String(8), nullable=False, default="x")

    register_persistence_contribution(
        PersistenceContribution(
            contribution_id="phase_d_selected",
            table_names=frozenset({marker}),
            matches=lambda _c: True,
            origin="plugin",
        )
    )
    try:
        url = f"sqlite:///{tmp_path / 'selected.db'}"
        eng = create_engine(url)
        try:
            plan = PersistencePlan(
                contribution_ids=("generic", "phase_d_selected"),
                table_names=GENERIC_TABLES | {marker},
            )
            apply_schema(eng, tables=plan.tables(), persistence_plan=plan)
            present = set(inspect(eng).get_table_names())
            assert marker in present
            assert GENERIC_TABLES <= present
            assert "cbsds" not in present
        finally:
            eng.dispose()
    finally:
        clear_extra_persistence_contributions()
        if marker in Base.metadata.tables:
            Base.metadata.remove(Base.metadata.tables[marker])


def test_missing_migration_domain_fails_closed_no_full_schema(
    tmp_path: Path, no_create_all, monkeypatch
):
    load_all_models()
    url = f"sqlite:///{tmp_path / 'failclosed.db'}"
    eng = create_engine(url)
    called = {"upgrade_head": False}

    def _boom(*_a, **_k):
        called["upgrade_head"] = True
        raise AssertionError("upgrade_head must not be used as fallback")

    monkeypatch.setattr("services.migrations.upgrade_head", _boom)
    try:
        plan = PersistencePlan(
            contribution_ids=("unknown_domain_xyz",),
            table_names=frozenset({"admin_injected_data"}),
        )
        with pytest.raises(RuntimeError, match="refuse silent full-schema fallback"):
            apply_schema(eng, tables=plan.tables(), persistence_plan=plan)
        present = set(inspect(eng).get_table_names())
        assert "cbsds" not in present
        assert called["upgrade_head"] is False
    finally:
        eng.dispose()


def test_refuse_ambiguous_head_target(no_create_all):
    with pytest.raises(RuntimeError, match="ambiguous"):
        upgrade_to(database_url="sqlite://", revision="head")


def test_apply_schema_does_not_rediscover_plugins(tmp_path: Path, no_create_all, monkeypatch):
    load_all_models()
    from runtime import PluginRegistry

    def _forbidden(*_a, **_k):
        raise AssertionError("PluginRegistry.from_discovery must not run during apply_schema")

    monkeypatch.setattr(PluginRegistry, "from_discovery", classmethod(_forbidden))
    url = f"sqlite:///{tmp_path / 'no_rediscover.db'}"
    eng = create_engine(url)
    try:
        plan = PersistencePlan(
            contribution_ids=("generic",),
            table_names=GENERIC_TABLES,
        )
        apply_schema(eng, tables=plan.tables(), persistence_plan=plan)
        assert current_revision(database_url=url) == GENERIC_REVISION
    finally:
        eng.dispose()
