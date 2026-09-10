"""custom profile template/example — copy/adapt YAML, validate without Python."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from profiles.doctor import run_profile_doctor
from profiles.parse import load_profile, load_profile_document

_PROFILES = Path(__file__).resolve().parents[2] / "profiles" / "profiles"
_TEMPLATE = _PROFILES / "templates" / "custom_profile.template.yaml"
_EXAMPLE = _PROFILES / "examples" / "custom_campus_6ghz.yaml"


def test_template_and_example_pass_profile_doctor():
    for path in (_TEMPLATE, _EXAMPLE):
        report = run_profile_doctor(path=path)
        assert report.ok, path.name + ": " + "; ".join(
            f"{f.name}={f.detail}" for f in report.findings if not f.ok
        )
        assert report.profile_id
        assert report.profile_hash


def test_example_is_custom_status_and_builtin_catalog_untouched():
    parsed = load_profile_document(_EXAMPLE)
    assert parsed.metadata.status == "custom"
    assert parsed.metadata.id == "custom_campus_6ghz"
    assert parsed.spectrum.ranges[0].low_hz == 5_925_000_000
    assert [c.id for c in (parsed.access.classes if parsed.access else ())] == [
        "safety",
        "enterprise",
        "guest",
    ]
    # Builtin reference catalog remains loadable by id.
    cbrs = load_profile("cbrs_winnforum")
    assert cbrs.metadata.version == "2.0.0"
    assert cbrs.metadata.status == "reference"


def test_copy_adapt_template_without_python(tmp_path: Path):
    """User workflow: copy template, edit band/classes/id, validate with doctor."""
    dest = tmp_path / "my_site.yaml"
    shutil.copyfile(_TEMPLATE, dest)
    doc = yaml.safe_load(dest.read_text(encoding="utf-8"))
    doc["metadata"]["id"] = "my_site_private"
    doc["metadata"]["version"] = "1.0.1"
    doc["spectrum"]["ranges"][0]["low_hz"] = 6_000_000_000
    doc["spectrum"]["ranges"][0]["high_hz"] = 6_200_000_000
    doc["spectrum"]["channelization"]["origin_hz"] = 6_000_000_000
    doc["spectrum"]["channelization"]["width_hz"] = 10_000_000
    doc["access"]["classes"] = [
        {"id": "owner", "priority": 200, "preemptible": False},
        {"id": "visitor", "priority": 100, "preemptible": True},
    ]
    doc["authorization"]["duration_s"] = 180
    dest.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    report = run_profile_doctor(path=dest)
    assert report.ok
    assert report.profile_id == "my_site_private"
    adapted = load_profile_document(dest)
    assert adapted.access is not None
    assert [c.id for c in adapted.access.classes] == ["owner", "visitor"]
    assert adapted.spectrum.ranges[0].low_hz == 6_000_000_000
