"""Resolve-once RuntimeComposition lifecycle (API / worker / CPAS)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from profiles import load_profile
from runtime import PluginRegistry, compose_runtime, load_deployment
from runtime.bootstrap import (
    ProcessCompositionAlreadyInitializedError,
    _reset_process_runtime_composition_for_tests,
    get_process_runtime_composition,
    initialize_process_runtime_composition,
)
from runtime.context import lookup_runtime_composition
from runtime.data_access import ProviderNotBoundError
from runtime.deployment import DEFAULT_DEPLOYMENT_PATH

pytestmark = pytest.mark.no_runtime_composition


@pytest.fixture(autouse=True)
def _isolate_process_composition():
    _reset_process_runtime_composition_for_tests()
    yield
    _reset_process_runtime_composition_for_tests()
    try:
        import main as main_mod

        if hasattr(main_mod.app.state, "runtime_composition"):
            delattr(main_mod.app.state, "runtime_composition")
    except Exception:  # noqa: BLE001
        pass


def _compose():
    return compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )


def test_initialize_once_reuses_same_object():
    first = initialize_process_runtime_composition(composition=_compose())
    second = initialize_process_runtime_composition(composition=first)
    assert second is first
    assert get_process_runtime_composition() is first


def test_initialize_same_config_is_idempotent_different_object():
    first = initialize_process_runtime_composition(composition=_compose())
    twin = _compose()
    assert twin is not first
    assert initialize_process_runtime_composition(composition=twin) is first


def test_initialize_rejects_conflicting_hot_swap():
    initialize_process_runtime_composition(composition=_compose())
    other = MagicMock()
    other.provenance.profile_id = "other_profile"
    other.provenance.profile_hash = "deadbeef"
    other.provenance.deployment_hash = "cafebabe"
    other.provenance.selected_plugins = {"rf": "other"}
    with pytest.raises(ProcessCompositionAlreadyInitializedError):
        initialize_process_runtime_composition(composition=other)


def test_lookup_never_calls_compose_or_discovery(monkeypatch):
    composition = initialize_process_runtime_composition(composition=_compose())

    def _boom(*args, **kwargs):
        raise AssertionError("lookup must not compose or discover")

    monkeypatch.setattr("runtime.composition.compose_runtime", _boom)
    monkeypatch.setattr(
        "runtime.registry.PluginRegistry.from_discovery",
        classmethod(lambda cls, *a, **k: _boom()),
    )
    assert lookup_runtime_composition() is composition


def test_missing_process_composition_fails_closed_for_cpas(db_session, monkeypatch):
    from services.cpas_service import execute_cpas_pipeline

    monkeypatch.setattr(
        "runtime.context.lookup_runtime_composition",
        lambda composition=None: None,
    )
    with pytest.raises(ProviderNotBoundError, match="not bound"):
        execute_cpas_pipeline(db_session)


def test_cpas_uses_same_object_as_startup_authority(db_session, monkeypatch):
    composition = initialize_process_runtime_composition(composition=_compose())
    monkeypatch.setattr(
        "services.cpas_service.run_peer_fad_sync",
        lambda db, client=None: {"peers": 0, "ok": 0, "failed": 0, "errors": []},
    )
    captured: dict[str, object] = {}

    from runtime import context as ctx_mod

    real_scope = ctx_mod.runtime_composition_scope

    def _capture(comp):
        captured["comp"] = comp
        return real_scope(comp)

    monkeypatch.setattr(ctx_mod, "runtime_composition_scope", _capture)
    from services.cpas_service import execute_cpas_pipeline

    assert execute_cpas_pipeline(db_session)["ok"] is True
    assert captured["comp"] is composition


def test_cpas_does_not_call_compose_or_discovery(db_session, monkeypatch):
    initialize_process_runtime_composition(composition=_compose())
    monkeypatch.setattr(
        "services.cpas_service.run_peer_fad_sync",
        lambda db, client=None: {"peers": 0, "ok": 0, "failed": 0, "errors": []},
    )

    def _boom(*args, **kwargs):
        raise AssertionError("CPAS must not compose or discover")

    monkeypatch.setattr("runtime.composition.compose_runtime", _boom)
    monkeypatch.setattr(
        "runtime.registry.PluginRegistry.from_discovery",
        classmethod(lambda cls, *a, **k: _boom()),
    )
    from services.cpas_service import execute_cpas_pipeline

    assert execute_cpas_pipeline(db_session)["ok"] is True


def test_environment_drift_after_init_does_not_reselect(monkeypatch):
    composition = initialize_process_runtime_composition(composition=_compose())
    monkeypatch.setenv("SAS_DEPLOYMENT_CONFIG", "/nonexistent/deployment.yaml")
    monkeypatch.setenv("SAS_PROFILE", "eu_elsa")
    assert lookup_runtime_composition() is composition
    assert get_process_runtime_composition() is composition


def test_explicit_argument_precedes_process_authority():
    process = initialize_process_runtime_composition(composition=_compose())
    explicit = _compose()
    assert explicit is not process
    assert lookup_runtime_composition(explicit) is explicit


def test_discovery_count_once_across_initialize_calls(monkeypatch):
    calls = {"n": 0}
    real = PluginRegistry.from_discovery

    def _counting(cls, *args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(
        PluginRegistry,
        "from_discovery",
        classmethod(_counting),
    )
    _reset_process_runtime_composition_for_tests()
    first = initialize_process_runtime_composition(require_data_plugins=False)
    second = initialize_process_runtime_composition(require_data_plugins=False)
    assert first is second
    assert calls["n"] == 1


def test_worker_hook_is_idempotent(monkeypatch):
    from celery_app import _ensure_worker_runtime_composition

    composition = initialize_process_runtime_composition(composition=_compose())
    monkeypatch.setattr(
        "runtime.bootstrap.initialize_process_runtime_composition",
        MagicMock(side_effect=AssertionError("must not recompose")),
    )
    _ensure_worker_runtime_composition()
    assert get_process_runtime_composition() is composition


def test_semantic_equivalence_same_config_distinct_objects():
    a = _compose()
    b = _compose()
    assert a is not b
    assert a.provenance.profile_hash == b.provenance.profile_hash
    assert a.provenance.deployment_hash == b.provenance.deployment_hash
    assert dict(a.provenance.selected_plugins) == dict(b.provenance.selected_plugins)
