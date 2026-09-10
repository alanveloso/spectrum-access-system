"""Profile-driven persistence isolation proofs."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, String, inspect

import database
from models.base import Base
from models.persistence import (
    CBRS_WINNFORUM_ONLY_TABLES,
    GENERIC_TABLES,
    PersistenceContribution,
    clear_extra_persistence_contributions,
    reference_persistence_plan,
    register_persistence_contribution,
    resolve_persistence_plan,
)
from models.registry import REFERENCE_REQUIRED_TABLES, load_all_models
from profiles import load_profile
from runtime import PluginRegistry, compose_runtime, load_deployment
from runtime.deployment import DEFAULT_DEPLOYMENT_PATH
from tests.fixtures.synthetic_profile import synthetic_deployment, synthetic_profile

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated_engine(tmp_path: Path):
    previous = str(database.engine.url)
    url = f"sqlite:///{tmp_path / 'persistence_boundary.db'}"
    database.rebind_engine(url)
    try:
        yield database.engine
    finally:
        database.rebind_engine(previous)
        database.init_db(retries=1, delay_seconds=0)


def test_synthetic_plan_is_generic_only():
    composition = compose_runtime(
        synthetic_profile(),
        synthetic_deployment(),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert plan.contribution_ids == ("generic",)
    assert plan.table_names == GENERIC_TABLES
    assert not (plan.table_names & CBRS_WINNFORUM_ONLY_TABLES)


def test_cbrs_plan_includes_reference_schema():
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    assert "generic" in plan.contribution_ids
    assert "cbrs_winnforum_reference" in plan.contribution_ids
    assert plan.table_names == REFERENCE_REQUIRED_TABLES


def test_synthetic_empty_db_creates_no_cbrs_tables(isolated_engine):
    load_all_models()
    composition = compose_runtime(
        synthetic_profile(),
        synthetic_deployment(),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    database.init_db(retries=1, delay_seconds=0, persistence_plan=plan)
    present = set(inspect(database.engine).get_table_names())
    present.discard("alembic_version")
    assert GENERIC_TABLES <= present
    assert present & CBRS_WINNFORUM_ONLY_TABLES == set()


def test_cbrs_empty_db_creates_reference_tables(isolated_engine):
    load_all_models()
    composition = compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    plan = resolve_persistence_plan(composition)
    database.init_db(retries=1, delay_seconds=0, persistence_plan=plan)
    present = set(inspect(database.engine).get_table_names())
    assert REFERENCE_REQUIRED_TABLES <= present


def test_missing_required_table_fails_closed(isolated_engine, monkeypatch):
    load_all_models()
    plan = reference_persistence_plan()

    def _broken_apply(engine, *, tables=None, persistence_plan=None):
        # Create nothing — readiness must fail.
        return None

    monkeypatch.setattr("services.migrations.apply_schema", _broken_apply)
    with pytest.raises(RuntimeError, match="missing tables"):
        database.init_db(retries=1, delay_seconds=0, persistence_plan=plan)


def test_external_contribution_without_database_py_edit():
    """New contribution registers without editing database.py / profile engine."""
    marker_name = "synthetic_persistence_marker"

    class SyntheticPersistenceMarker(Base):
        __tablename__ = marker_name
        id = Column(Integer, primary_key=True)
        label = Column(String(32), nullable=False, default="x")

    def _matches(composition) -> bool:
        return composition.network_adapter is not None and composition.device_adapter is None

    register_persistence_contribution(
        PersistenceContribution(
            contribution_id="synthetic_marker",
            table_names=frozenset({marker_name}),
            matches=_matches,
            origin="plugin",
        )
    )
    try:
        composition = compose_runtime(
            synthetic_profile(),
            synthetic_deployment(),
            PluginRegistry.from_discovery(),
            require_data_plugins=False,
        )
        plan = resolve_persistence_plan(composition)
        assert "synthetic_marker" in plan.contribution_ids
        assert marker_name in plan.table_names
        # Zero-core metric: database.py must not mention this contribution.
        db_src = (REPO / "database.py").read_text(encoding="utf-8")
        assert "synthetic_marker" not in db_src
        assert "synthetic_persistence_marker" not in db_src
    finally:
        clear_extra_persistence_contributions()
        # Remove test table from metadata so it does not leak into other suites.
        if marker_name in Base.metadata.tables:
            Base.metadata.remove(Base.metadata.tables[marker_name])


def test_database_py_has_no_profile_id_branching():
    import ast

    source = (REPO / "database.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned = {
        "cbrs_winnforum",
        "br_anatel_slp_3700",
        "eu_elsa",
        "us_tvws_15_711",
        "synthetic_minimal",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in banned
    for token in banned:
        assert token not in source


def test_synthetic_startup_persistence_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Real on_startup path with synthetic profile → no CBRS tables."""
    import main as main_mod
    import profiles as profiles_pkg
    import runtime as runtime_pkg
    import protection_data.loader as protection_loader

    previous = str(database.engine.url)
    database.rebind_engine(f"sqlite:///{tmp_path / 'synthetic_persist.db'}")
    monkeypatch.setattr(
        protection_loader, "assert_protection_data_ready", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "providers.protection_bundle.assert_protection_data_ready",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(profiles_pkg, "get_active_profile_document", synthetic_profile)
    monkeypatch.setattr(
        runtime_pkg,
        "load_deployment_for_startup",
        lambda **_kwargs: synthetic_deployment(),
    )
    from runtime.bootstrap import _reset_process_runtime_composition_for_tests

    _reset_process_runtime_composition_for_tests()
    try:
        main_mod.on_startup()
        present = set(inspect(database.engine).get_table_names())
        present.discard("alembic_version")
        assert GENERIC_TABLES <= present
        assert present & CBRS_WINNFORUM_ONLY_TABLES == set()
    finally:
        database.rebind_engine(previous)
        database.init_db(retries=1, delay_seconds=0)
        _reset_process_runtime_composition_for_tests()
