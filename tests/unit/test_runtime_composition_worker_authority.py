"""RuntimeComposition authority across worker threads (resolve once, consume everywhere)."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from profiles import load_profile
from runtime import PluginRegistry, compose_runtime, load_deployment
from runtime.bootstrap import (
    _reset_process_runtime_composition_for_tests,
    initialize_process_runtime_composition,
)
from runtime.context import (
    get_bound_runtime_composition,
    lookup_runtime_composition,
    runtime_composition_scope,
)
from runtime.data_access import ProviderNotBoundError, active_runtime_composition
from runtime.deployment import DEFAULT_DEPLOYMENT_PATH


def _compose_reference():
    return compose_runtime(
        load_profile("cbrs_winnforum"),
        load_deployment(DEFAULT_DEPLOYMENT_PATH),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )


def _bind_process_and_app(composition):
    """Install the same instance as process-local and app.state authority."""
    _reset_process_runtime_composition_for_tests()
    initialize_process_runtime_composition(composition=composition)
    import main as main_mod

    previous = getattr(main_mod.app.state, "runtime_composition", None)
    main_mod.app.state.runtime_composition = composition
    return main_mod, previous


def _restore_app(main_mod, previous) -> None:
    if previous is None:
        if hasattr(main_mod.app.state, "runtime_composition"):
            delattr(main_mod.app.state, "runtime_composition")
    else:
        main_mod.app.state.runtime_composition = previous
    _reset_process_runtime_composition_for_tests()


def test_provider_authority_preserved_across_registration_threads():
    """Worker threads see the SAME startup-resolved RuntimeComposition."""
    composition = _compose_reference()
    main_mod, previous = _bind_process_and_app(composition)
    seen: list[Any] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        try:
            assert get_bound_runtime_composition() is None
            active = active_runtime_composition()
            seen.append(active)
        except BaseException as exc:  # noqa: BLE001 — collect for main thread
            errors.append(exc)

    try:
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert errors == []
        assert len(seen) == 2
        assert seen[0] is composition
        assert seen[1] is composition
        assert seen[0] is seen[1]
    finally:
        _restore_app(main_mod, previous)


def test_runtime_composition_available_in_registration_worker():
    """lookup_runtime_composition returns the process/app instance in a worker thread."""
    composition = _compose_reference()
    main_mod, previous = _bind_process_and_app(composition)
    box: dict[str, Any] = {}

    def worker() -> None:
        box["value"] = lookup_runtime_composition()

    try:
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)
        assert box["value"] is composition
    finally:
        _restore_app(main_mod, previous)


def test_contextvar_precedes_process_authority_without_reselection():
    """Request/test ContextVar binding wins; workers still see process authority."""
    composition_a = _compose_reference()
    composition_b = _compose_reference()
    assert composition_a is not composition_b

    main_mod, previous = _bind_process_and_app(composition_a)
    worker_seen: dict[str, Any] = {}

    def worker() -> None:
        worker_seen["value"] = active_runtime_composition()

    try:
        with runtime_composition_scope(composition_b):
            assert active_runtime_composition() is composition_b
            assert lookup_runtime_composition() is composition_b
            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=5)
            assert worker_seen["value"] is composition_a
            assert worker_seen["value"] is not composition_b
    finally:
        _restore_app(main_mod, previous)


def test_unbound_active_runtime_raises_provider_not_bound(monkeypatch):
    """Fail closed when neither ContextVar nor process/app holds a composition."""
    monkeypatch.setattr(
        "runtime.data_access.lookup_runtime_composition",
        lambda composition=None: None,
    )
    with pytest.raises(ProviderNotBoundError, match="not bound"):
        active_runtime_composition()


def test_process_authority_swap_requires_reset():
    """Replacing process authority is only allowed after an explicit test reset."""
    composition_a = _compose_reference()
    composition_b = _compose_reference()
    assert composition_a is not composition_b

    main_mod, previous = _bind_process_and_app(composition_a)
    results: dict[str, Any] = {}

    def observe(label: str) -> None:
        results[label] = active_runtime_composition()

    try:
        t_a = threading.Thread(target=observe, args=("a",))
        t_a.start()
        t_a.join(timeout=5)
        assert results["a"] is composition_a

        _reset_process_runtime_composition_for_tests()
        initialize_process_runtime_composition(composition=composition_b)
        main_mod.app.state.runtime_composition = composition_b
        t_b = threading.Thread(target=observe, args=("b",))
        t_b.start()
        t_b.join(timeout=5)
        assert results["b"] is composition_b
        assert results["a"] is not results["b"]
    finally:
        _restore_app(main_mod, previous)
