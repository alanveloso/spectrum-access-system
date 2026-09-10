"""CLI: python -m tools.profile_cost — Spectrum Profile cost metrics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from profiles.cost import (
    load_changed_files_list,
    measure_profile_cost,
    render_profile_cost_report,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.profile_cost",
        description=(
            "Measure Spectrum Profile authoring cost: YAML LOC, optional plugin/test/"
            "primitive/core/RF LOC, and catalog mechanism reuse percent. "
            "YAML is parsed as configuration, not executed as code."
        ),
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--id",
        dest="profile_id",
        default=None,
        help="Profile id under profiles/definitions/<id>.yaml",
    )
    src.add_argument(
        "path",
        nargs="?",
        default=None,
        type=Path,
        help="Path to a Spectrum Profile YAML file",
    )
    p.add_argument(
        "--profile-python",
        nargs="*",
        default=[],
        type=Path,
        help="Profile-specific Python files (LOC)",
    )
    p.add_argument(
        "--plugins",
        nargs="*",
        default=[],
        type=Path,
        help="New adapter/provider plugin Python files (LOC)",
    )
    p.add_argument(
        "--primitives",
        nargs="*",
        default=[],
        type=Path,
        help="New generic primitive Python files (LOC)",
    )
    p.add_argument(
        "--tests",
        nargs="*",
        default=[],
        type=Path,
        help="Profile-related test files (LOC)",
    )
    p.add_argument(
        "--core-files",
        nargs="*",
        default=[],
        type=Path,
        help="Coordination/product core files touched (count + paths)",
    )
    p.add_argument(
        "--rf-files",
        nargs="*",
        default=[],
        type=Path,
        help="RF-related files touched (count + LOC)",
    )
    p.add_argument(
        "--changed-files",
        type=Path,
        default=None,
        help="Text file listing repo-relative changed paths (auto-classify buckets)",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root for --changed-files relative paths (default: cwd)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    changed: tuple[Path, ...] = ()
    if args.changed_files is not None:
        changed = load_changed_files_list(args.changed_files)

    report = measure_profile_cost(
        profile_id=args.profile_id,
        path=args.path,
        profile_python=args.profile_python or None,
        plugins=args.plugins or None,
        primitives=args.primitives or None,
        tests=args.tests or None,
        core_files=args.core_files or None,
        rf_files=args.rf_files or None,
        changed_files=changed or None,
        repo_root=args.repo_root,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_profile_cost_report(report))
 # Novel mechanisms are a cost smell; exit 2 so CI can gate if desired.
    if report.mechanisms_novel:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
