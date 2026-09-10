"""Architecture guards for Phase D persistence modularization."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Profile document ids — contribution ids like cbrs_winnforum_reference are OK.
BANNED_PROFILE_IDS = {
    "cbrs_winnforum",
    "br_anatel_slp_3700",
    "eu_elsa",
    "us_tvws_15_711",
}


def test_migrations_have_no_profile_id_dispatch():
    for rel in (
        "services/migrations.py",
        "models/migration_domains.py",
        "database.py",
    ):
        path = REPO / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for comparator in node.comparators:
                if (
                    isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, str)
                    and comparator.value in BANNED_PROFILE_IDS
                ):
                    pytest.fail(
                        f"{rel} compares against profile id {comparator.value!r}"
                    )


def test_migrations_refuse_ambiguous_head_literal_in_upgrade_path():
    source = (REPO / "services/migrations.py").read_text(encoding="utf-8")
    assert 'revision in {"head", "heads"}' in source
    assert "refuse" in source.lower() or "refusing" in source.lower()


def test_business_services_do_not_import_alembic_command():
    services = REPO / "services"
    banned_imports = {"alembic.command", "alembic.config"}
    allowed = {"migrations.py", "schema_fingerprint.py", "db_backup.py"}
    for path in services.glob("*.py"):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for banned in banned_imports:
            assert banned not in text, f"{path.name} imports {banned}"
