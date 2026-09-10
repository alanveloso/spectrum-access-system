"""Plugin authoring guide stays aligned with discovery contracts."""

from __future__ import annotations

from pathlib import Path

from adapters.discovery import (
    GROUP_DATA_PROVIDERS,
    GROUP_DEVICE_ADAPTERS,
    GROUP_MECHANISMS,
    GROUP_NETWORK_ADAPTERS,
    GROUP_PROTOCOL_ADAPTERS,
    GROUP_RF_MODELS,
)
from adapters.device import ADAPTER_API_VERSION
from providers.contract import PROVIDER_API_VERSION
from rf.port import RF_API_VERSION, RF_MODEL_PATH_LOSS

GUIDE = (
    Path(__file__).resolve().parents[2] / "docs" / "plugins" / "creating_plugins.md"
)


def test_plugin_guide_exists() -> None:
    assert GUIDE.is_file()
    text = GUIDE.read_text(encoding="utf-8")
    assert len(text) > 500


def test_plugin_guide_lists_canonical_entry_point_groups() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    for group in (
        GROUP_DEVICE_ADAPTERS,
        GROUP_NETWORK_ADAPTERS,
        GROUP_PROTOCOL_ADAPTERS,
        GROUP_DATA_PROVIDERS,
        GROUP_RF_MODELS,
        GROUP_MECHANISMS,
    ):
        assert group in text, group


def test_plugin_guide_states_python_for_new_behavior_and_yaml_for_known() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert "Extensão em Python só quando o comportamento é novo" in text
    assert "Profiles YAML só selecionam mecanismos" in text
    assert "não é linguagem de programação" in text
    assert "Coordination Core" in text
    assert "falham fechado" in text


def test_plugin_guide_mentions_api_versions_and_path_loss_model() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert ADAPTER_API_VERSION in text
    assert PROVIDER_API_VERSION in text
    assert RF_API_VERSION in text
    assert RF_MODEL_PATH_LOSS in text
    assert "primitives.registry" in text
    assert "tools.profile_doctor" in text
