"""Discover official harness unittest methods, including configurable expansions."""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from typing import Iterable


def _ensure_harness_path(workdir: str | None = None) -> str:
    path: str = (
        workdir
        if workdir is not None
        else os.environ.get("HARNESS_WORKDIR", "/opt/winnforum-harness/src/harness")
    )
    if path not in sys.path:
        sys.path.insert(0, path)
    return path


def import_harness_module(module: str, *, workdir: str | None = None):
    """Import a harness testcase module (triggers configurable_testcase registration)."""
    _ensure_harness_path(workdir)
    return importlib.import_module(module)


def list_module_test_methods(module: str, *, workdir: str | None = None) -> list[str]:
    """Return sorted unittest method names for a harness testcase module."""
    import_harness_module(module, workdir=workdir)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(module)
    names: list[str] = []
    for test in _iter_tests(suite):
        names.append(test.id().rsplit(".", 1)[-1])
    return sorted(set(names))


def _iter_tests(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def resolve_case_methods(
    module: str,
    case_token: str,
    *,
    workdir: str | None = None,
) -> list[str]:
    """Map REG.1 / FDB.3 style selectors to concrete harness method names.

    Configurable tests expand at import time to names like
    ``test_WINNF_FT_S_FDB_1_0_default`` rather than ``test_WINNF_FT_S_FDB_1``.
    """
    base = f"test_WINNF_FT_S_{case_token.replace('.', '_')}"
    methods = list_module_test_methods(module, workdir=workdir)
    exact = [m for m in methods if m == base]
    if exact:
        return exact
    prefix = f"{base}_"
    expanded = [m for m in methods if m.startswith(prefix)]
    if expanded:
        return expanded
    # Allow full method pass-through when caller already knows the name.
    if case_token.startswith("test_") and case_token in methods:
        return [case_token]
    raise ValueError(
        f"no harness methods for case {case_token!r} in {module!r}; "
        f"available={[m for m in methods if base in m][:8]}"
    )


def module_from_family(family: str) -> str:
    from tools.winnforum.families import FAMILY_TEST_MODULES, normalize_family

    fam = normalize_family(family)
    if fam not in FAMILY_TEST_MODULES:
        raise ValueError(f"unknown family {fam!r}")
    return FAMILY_TEST_MODULES[fam]


def resolve_case_methods_via_docker(
    module: str,
    case_token: str,
    *,
    image: str,
    repo_root: Path,
    harness_pki_dir: Path | None = None,
) -> list[str]:
    """Resolve configurable testcase names inside the official harness container."""
    import json
    import subprocess

    from tools.winnforum.docker_runner import DockerHarnessConfig, _docker_run_base
    from tools.winnforum.preflight import resolve_harness_pki_dir

    repo_root = repo_root.resolve()
    pki = harness_pki_dir or resolve_harness_pki_dir(repo_root)
    cfg = DockerHarnessConfig(
        image=image,
        sas_repo_root=repo_root,
        harness_pki_dir=pki,
    )
    cmd = _docker_run_base(cfg)
    payload = json.dumps({"module": module, "case": case_token})
    cmd.extend(
        [
            image,
            "python",
            "-c",
            (
                "import json,sys; sys.path.insert(0,'/opt/sas'); "
                "from tools.winnforum.discover_tests import resolve_case_methods; "
                "payload=json.loads(sys.argv[1]); "
                "print(json.dumps(resolve_case_methods("
                "payload['module'], payload['case'], "
                "workdir='/opt/winnforum-harness/src/harness')))"
            ),
            payload,
        ]
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise ValueError(f"docker resolve failed for {case_token!r}: {combined.strip()}")
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("["):
            payload = json.loads(line)
            if not isinstance(payload, list):
                raise ValueError(f"docker resolve JSON is not a list for {case_token!r}")
            return [str(item) for item in payload]
    raise ValueError(f"docker resolve produced no JSON for {case_token!r}: {combined.strip()}")


def method_to_case_token(method: str) -> str | None:
    """Map ``test_WINNF_FT_S_EXZ_1`` or expanded names back to ``EXZ.1``."""
    if not method.startswith("test_WINNF_FT_S_"):
        return None
    body = method[len("test_WINNF_FT_S_") :]
    parts = body.split("_")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return f"{parts[0]}.{parts[1]}"


def reexpand_targets_for_docker(
    targets: list,
    *,
    image: str,
    repo_root: Path,
    harness_pki_dir: Path | None = None,
) -> list:
    """Re-resolve testcase methods inside the harness container when needed."""
    from tools.winnforum.families import UnittestTarget

    expanded: list[UnittestTarget] = []
    for target in targets:
        if not target.method:
            expanded.append(target)
            continue
        case_token = method_to_case_token(target.method)
        if case_token is None:
            expanded.append(target)
            continue
        try:
            methods = resolve_case_methods_via_docker(
                target.module,
                case_token,
                image=image,
                repo_root=repo_root,
                harness_pki_dir=harness_pki_dir,
            )
        except ValueError:
            expanded.append(target)
            continue
        for method in methods:
            expanded.append(UnittestTarget(module=target.module, method=method))
    seen: set[str] = set()
    ordered: list[UnittestTarget] = []
    for item in expanded:
        key = item.label()
        if key not in seen:
            seen.add(key)
            ordered.append(item)
    return ordered


def inspect_configurable_case(case_selector: str) -> list[str]:
    """Best-effort discovery without requiring harness import path (for dry-run)."""
    if case_selector.startswith("testcases."):
        return [case_selector.rsplit("::", 1)[-1]] if "::" in case_selector else []
    if "." not in case_selector or case_selector.startswith("test_"):
        return []
    fam_part, num = case_selector.split(".", 1)
    module = module_from_family(fam_part)
    return resolve_case_methods(module, f"{fam_part.upper()}.{num}")
