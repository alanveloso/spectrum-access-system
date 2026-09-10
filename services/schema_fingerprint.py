"""Deterministic schema fingerprint for Phase D equivalence tests."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy import inspect


def schema_fingerprint(engine: Engine) -> str:
    """Return a stable JSON fingerprint of tables/columns/constraints/indexes.

    Omits DB-generated constraint OIDs and unstable dialect internals.
    """
    insp = inspect(engine)
    tables: dict[str, Any] = {}
    for name in sorted(insp.get_table_names()):
        if name == "alembic_version":
            continue
        columns = []
        for col in insp.get_columns(name):
            columns.append(
                {
                    "name": col["name"],
                    "type": str(col["type"]),
                    "nullable": bool(col.get("nullable", True)),
                    "default": str(col.get("default")) if col.get("default") is not None else None,
                }
            )
        pk = insp.get_pk_constraint(name) or {}
        fks = []
        for fk in insp.get_foreign_keys(name):
            fks.append(
                {
                    "constrained_columns": list(fk.get("constrained_columns") or []),
                    "referred_table": fk.get("referred_table"),
                    "referred_columns": list(fk.get("referred_columns") or []),
                }
            )
        uniques = []
        for uq in insp.get_unique_constraints(name):
            uniques.append(
                {
                    "name": uq.get("name"),
                    "column_names": list(uq.get("column_names") or []),
                }
            )
        indexes = []
        for idx in insp.get_indexes(name):
            indexes.append(
                {
                    "name": idx.get("name"),
                    "unique": bool(idx.get("unique")),
                    "column_names": list(idx.get("column_names") or []),
                }
            )
        tables[name] = {
            "columns": columns,
            "pk": list(pk.get("constrained_columns") or []),
            "foreign_keys": sorted(
                fks, key=lambda x: (x["referred_table"] or "", x["constrained_columns"])
            ),
            "uniques": sorted(uniques, key=lambda x: (x["name"] or "", x["column_names"])),
            "indexes": sorted(indexes, key=lambda x: (x["name"] or "", x["column_names"])),
        }
    return json.dumps(tables, sort_keys=True, separators=(",", ":"))
