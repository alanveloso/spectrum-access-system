"""Execute unittest modules inside the harness workdir with optional method filters."""

from __future__ import annotations

import json
import sys
import unittest
from typing import Any


def _iter_tests(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    found: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            found.extend(_iter_tests(item))
        else:
            found.append(item)
    return found


def run_targets(targets: list[dict[str, Any]], verbosity: int = 2) -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for target in targets:
        module = target["module"]
        method = target.get("method")
        loaded = loader.loadTestsFromName(module)
        if not method:
            suite.addTests(loaded)
            continue
        matched = False
        for test in _iter_tests(loaded):
            if test.id().rsplit(".", 1)[-1] == method:
                suite.addTest(test)
                matched = True
        if not matched:
            print(
                f"error: no test method {method!r} found in module {module!r}",
                file=sys.stderr,
            )
            return 2
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: exec_unittest.py '<targets-json>'", file=sys.stderr)
        return 2
    targets = json.loads(args[0])
    if not isinstance(targets, list) or not targets:
        print("error: targets must be a non-empty JSON list", file=sys.stderr)
        return 2
 # OpenSSL 3.x: patch harness version decoder before test modules load.
    try:
        from tools.winnforum.openssl_compat import (
            patch_harness_openssl_version_decoder,
        )

        patch_harness_openssl_version_decoder()
    except Exception as exc:  # noqa: BLE001 — best-effort compat only
        print(f"warning: openssl compat patch skipped: {exc}", file=sys.stderr)
    return run_targets(targets)


if __name__ == "__main__":
    raise SystemExit(main())
