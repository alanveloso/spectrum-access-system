"""Automatic Spectrum Profile cost metrics .

Reports YAML LOC, optional plugin/profile/primitive/test/core/RF LOC, and
catalog mechanism reuse. YAML is parsed as configuration, not executed as code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Sequence

from primitives.registry import MechanismRegistry, builtin_mechanism_registry
from profiles.context import profile_hash, selected_mechanism_ids
from profiles.parse import load_profile, load_profile_document
from profiles.schema import ProfileDocument

# Path prefixes used when classifying a changed-files list (posix-style).
_PLUGIN_PREFIXES = ("adapters/", "providers/")
_RF_PREFIXES = ("rf/",)
_PRIMITIVE_PREFIXES = ("primitives/",)
_TEST_PREFIXES = ("tests/",)
_YAML_PREFIXES = ("profiles/",)
_TOOLING_PREFIXES = ("tools/", "docs/")
_CORE_PREFIXES = (
    "services/",
    "models/",
    "routes/",
    "schemas/",
    "database",
    "main.py",
    "config.py",
    "celery_app.py",
    "tasks.py",
)


class CostBucket(StrEnum):
    YAML = "yaml"
    PROFILE_PYTHON = "profile_python"
    PLUGIN = "plugin"
    PRIMITIVE = "primitive"
    TEST = "test"
    CORE = "core"
    RF = "rf"
    TOOLING = "tooling"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class PathLoc:
    path: str
    loc: int
    bucket: CostBucket


@dataclass
class ProfileCostReport:
    source: str
    profile_id: str
    profile_version: str
    profile_hash: str
    yaml_loc: int
    yaml_path: str
    mechanisms_used: tuple[str, ...]
    mechanisms_reused: tuple[str, ...]
    mechanisms_novel: tuple[str, ...]
    mechanism_reuse_pct: float
    profile_python_loc: int = 0
    plugin_loc: int = 0
    primitive_loc: int = 0
    tests_loc: int = 0
    core_files_changed: int = 0
    core_file_paths: tuple[str, ...] = ()
    rf_files_changed: int = 0
    rf_file_paths: tuple[str, ...] = ()
    rf_loc: int = 0
    classified_paths: list[PathLoc] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "profile_hash": self.profile_hash,
            "yaml_loc": self.yaml_loc,
            "yaml_path": self.yaml_path,
            "mechanisms_used": list(self.mechanisms_used),
            "mechanisms_reused": list(self.mechanisms_reused),
            "mechanisms_novel": list(self.mechanisms_novel),
            "mechanism_reuse_pct": self.mechanism_reuse_pct,
            "profile_python_loc": self.profile_python_loc,
            "plugin_loc": self.plugin_loc,
            "primitive_loc": self.primitive_loc,
            "tests_loc": self.tests_loc,
            "core_files_changed": self.core_files_changed,
            "core_file_paths": list(self.core_file_paths),
            "rf_files_changed": self.rf_files_changed,
            "rf_file_paths": list(self.rf_file_paths),
            "rf_loc": self.rf_loc,
            "classified_paths": [
                {"path": item.path, "loc": item.loc, "bucket": item.bucket.value}
                for item in self.classified_paths
            ],
            "notes": list(self.notes),
        }


def count_nonblank_loc(path: Path) -> int:
    """Count non-blank lines (physical LOC proxy)."""
    text = path.read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if line.strip())


def classify_repo_path(rel_posix: str) -> CostBucket:
    """Classify a repo-relative path into a cost bucket."""
    norm = rel_posix.replace("\\", "/").lstrip("./")
    if any(norm.startswith(prefix) for prefix in _YAML_PREFIXES):
        return CostBucket.YAML
    if any(norm.startswith(prefix) for prefix in _TEST_PREFIXES):
        return CostBucket.TEST
    if any(norm.startswith(prefix) for prefix in _PRIMITIVE_PREFIXES):
        return CostBucket.PRIMITIVE
    if any(norm.startswith(prefix) for prefix in _PLUGIN_PREFIXES):
        return CostBucket.PLUGIN
    if any(norm.startswith(prefix) for prefix in _RF_PREFIXES):
        return CostBucket.RF
    if any(norm.startswith(prefix) for prefix in _TOOLING_PREFIXES):
        return CostBucket.TOOLING
    if any(norm == prefix or norm.startswith(prefix) for prefix in _CORE_PREFIXES):
        return CostBucket.CORE
    return CostBucket.OTHER


def mechanism_reuse(
    mechanism_ids: Sequence[str],
    *,
    registry: MechanismRegistry | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...], float]:
    catalog = registry or builtin_mechanism_registry()
    known = catalog.ids()
    reused = tuple(mid for mid in mechanism_ids if mid in known)
    novel = tuple(mid for mid in mechanism_ids if mid not in known)
    if not mechanism_ids:
        return reused, novel, 100.0
    pct = round(100.0 * len(reused) / len(mechanism_ids), 2)
    return reused, novel, pct


def _resolve_paths(paths: Iterable[Path] | None) -> tuple[Path, ...]:
    if not paths:
        return ()
    return tuple(p.expanduser().resolve() for p in paths)


def _sum_loc(paths: Sequence[Path]) -> int:
    total = 0
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"path not found: {path}")
        total += count_nonblank_loc(path)
    return total


def _rel_display(path: Path, *, repo_root: Path | None) -> str:
    if repo_root is not None:
        try:
            return path.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def measure_profile_cost(
    *,
    profile_id: str | None = None,
    path: Path | None = None,
    registry: MechanismRegistry | None = None,
    profile_python: Sequence[Path] | None = None,
    plugins: Sequence[Path] | None = None,
    primitives: Sequence[Path] | None = None,
    tests: Sequence[Path] | None = None,
    core_files: Sequence[Path] | None = None,
    rf_files: Sequence[Path] | None = None,
    changed_files: Sequence[Path] | None = None,
    repo_root: Path | None = None,
) -> ProfileCostReport:
    """Compute cost metrics for one Spectrum Profile document."""
    if (profile_id is None) == (path is None):
        raise ValueError("provide exactly one of profile_id or path")

    if profile_id is not None:
        parsed = load_profile(profile_id, registry=registry)
        # Mirror load_profile path layout for display/LOC.
        yaml_path = (
            Path(__file__).resolve().parent / "definitions" / f"{profile_id}.yaml"
        )
        source = f"id:{profile_id}"
    else:
        assert path is not None
        yaml_path = path.expanduser().resolve()
        parsed = load_profile_document(yaml_path, registry=registry)
        source = str(yaml_path)

    return measure_parsed_profile_cost(
        parsed,
        yaml_path=yaml_path,
        source=source,
        registry=registry,
        profile_python=profile_python,
        plugins=plugins,
        primitives=primitives,
        tests=tests,
        core_files=core_files,
        rf_files=rf_files,
        changed_files=changed_files,
        repo_root=repo_root,
    )


def measure_parsed_profile_cost(
    parsed: ProfileDocument,
    *,
    yaml_path: Path,
    source: str,
    registry: MechanismRegistry | None = None,
    profile_python: Sequence[Path] | None = None,
    plugins: Sequence[Path] | None = None,
    primitives: Sequence[Path] | None = None,
    tests: Sequence[Path] | None = None,
    core_files: Sequence[Path] | None = None,
    rf_files: Sequence[Path] | None = None,
    changed_files: Sequence[Path] | None = None,
    repo_root: Path | None = None,
) -> ProfileCostReport:
    catalog = registry or builtin_mechanism_registry()
    used = selected_mechanism_ids(parsed)
    reused, novel, reuse_pct = mechanism_reuse(used, registry=catalog)

    yaml_resolved = yaml_path.expanduser().resolve()
    yaml_loc = count_nonblank_loc(yaml_resolved)

    profile_py = _resolve_paths(profile_python)
    plugin_paths = _resolve_paths(plugins)
    primitive_paths = _resolve_paths(primitives)
    test_paths = _resolve_paths(tests)
    core_paths = _resolve_paths(core_files)
    rf_paths = _resolve_paths(rf_files)

    classified: list[PathLoc] = []
    notes: list[str] = []

    if changed_files:
        root = repo_root or Path.cwd()
        for raw in changed_files:
            item = raw.expanduser()
            if item.is_absolute():
                rel = _rel_display(item, repo_root=root)
                resolved = item.resolve()
            else:
                rel = item.as_posix().replace("\\", "/")
                resolved = (root / item).resolve()
            bucket = classify_repo_path(rel)
            loc = count_nonblank_loc(resolved) if resolved.is_file() else 0
            classified.append(PathLoc(path=rel, loc=loc, bucket=bucket))
            if bucket == CostBucket.PLUGIN and resolved.is_file():
                plugin_paths = plugin_paths + (resolved,)
            elif bucket == CostBucket.PRIMITIVE and resolved.is_file():
                primitive_paths = primitive_paths + (resolved,)
            elif bucket == CostBucket.TEST and resolved.is_file():
                test_paths = test_paths + (resolved,)
            elif bucket == CostBucket.CORE:
                core_paths = core_paths + (resolved,)
            elif bucket == CostBucket.RF and resolved.is_file():
                rf_paths = rf_paths + (resolved,)
            elif bucket == CostBucket.PROFILE_PYTHON and resolved.is_file():
                profile_py = profile_py + (resolved,)
            elif bucket == CostBucket.YAML and resolved.resolve() != yaml_resolved:
                notes.append(f"additional yaml path classified but not summed: {rel}")
            elif bucket == CostBucket.OTHER:
                notes.append(f"unclassified changed path: {rel}")

    # Deduplicate path lists while preserving order.
    def _uniq(paths: Sequence[Path]) -> tuple[Path, ...]:
        seen: set[Path] = set()
        out: list[Path] = []
        for p in paths:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return tuple(out)

    profile_py = _uniq(profile_py)
    plugin_paths = _uniq(plugin_paths)
    primitive_paths = _uniq(primitive_paths)
    test_paths = _uniq(test_paths)
    core_paths = _uniq(core_paths)
    rf_paths = _uniq(rf_paths)

    if not any((profile_py, plugin_paths, primitive_paths, test_paths, core_paths, rf_paths)):
        notes.append(
            "optional LOC buckets empty; pass --plugins/--tests/--core-files/--rf-files "
            "or --changed-files for a full cost sheet"
        )

    core_display = tuple(_rel_display(p, repo_root=repo_root) for p in core_paths)
    rf_display = tuple(_rel_display(p, repo_root=repo_root) for p in rf_paths)

    return ProfileCostReport(
        source=source,
        profile_id=parsed.metadata.id,
        profile_version=parsed.metadata.version,
        profile_hash=profile_hash(parsed),
        yaml_loc=yaml_loc,
        yaml_path=_rel_display(yaml_resolved, repo_root=repo_root),
        mechanisms_used=used,
        mechanisms_reused=reused,
        mechanisms_novel=novel,
        mechanism_reuse_pct=reuse_pct,
        profile_python_loc=_sum_loc(profile_py) if profile_py else 0,
        plugin_loc=_sum_loc(plugin_paths) if plugin_paths else 0,
        primitive_loc=_sum_loc(primitive_paths) if primitive_paths else 0,
        tests_loc=_sum_loc(test_paths) if test_paths else 0,
        core_files_changed=len(core_paths),
        core_file_paths=core_display,
        rf_files_changed=len(rf_paths),
        rf_file_paths=rf_display,
        rf_loc=_sum_loc(rf_paths) if rf_paths else 0,
        classified_paths=classified,
        notes=notes,
    )


def render_profile_cost_report(report: ProfileCostReport) -> str:
    lines = [
        f"Profile cost: {'OK' if not report.mechanisms_novel else 'NOVEL_MECHANISMS'}",
        f"  source: {report.source}",
        f"  profile: {report.profile_id}@{report.profile_version}",
        f"  hash: {report.profile_hash}",
        f"  yaml_loc: {report.yaml_loc} ({report.yaml_path})",
        f"  profile_python_loc: {report.profile_python_loc}",
        f"  plugin_loc: {report.plugin_loc}",
        f"  primitive_loc: {report.primitive_loc}",
        f"  tests_loc: {report.tests_loc}",
        f"  core_files_changed: {report.core_files_changed}",
    ]
    if report.core_file_paths:
        lines.append(f"    paths: {', '.join(report.core_file_paths)}")
    lines.append(f"  rf_files_changed: {report.rf_files_changed}")
    if report.rf_file_paths:
        lines.append(f"    paths: {', '.join(report.rf_file_paths)}")
    lines.append(f"  rf_loc: {report.rf_loc}")
    lines.append(
        f"  mechanism_reuse_pct: {report.mechanism_reuse_pct} "
        f"({len(report.mechanisms_reused)}/{len(report.mechanisms_used)})"
    )
    lines.append(f"  mechanisms_used: {', '.join(report.mechanisms_used) or '(none)'}")
    if report.mechanisms_novel:
        lines.append(f"  mechanisms_novel: {', '.join(report.mechanisms_novel)}")
    for note in report.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def load_changed_files_list(path: Path) -> tuple[Path, ...]:
    """Load repo-relative or absolute paths, one per line (# comments allowed)."""
    items: list[Path] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        items.append(Path(stripped))
    return tuple(items)
