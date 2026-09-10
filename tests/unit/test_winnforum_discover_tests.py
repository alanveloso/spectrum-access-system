"""Tests for official harness testcase discovery (configurable FDB, MES mapping)."""

from __future__ import annotations

import os
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HARNESS_SRC = os.path.join(ROOT, ".cache", "winnforum-harness-src", "src", "harness")


@unittest.skipUnless(
    os.path.isdir(HARNESS_SRC),
    "clean harness archive missing (.cache/winnforum-harness-src)",
)
class DiscoverHarnessTests(unittest.TestCase):
    def test_mes_family_mapped(self):
        from tools.winnforum.families import FAMILY_TEST_MODULES

        self.assertIn("MES", FAMILY_TEST_MODULES)

    def test_fdb_case_expands_configurable_method(self):
        from tools.winnforum.discover_tests import resolve_case_methods

        try:
            methods = resolve_case_methods(
                "testcases.WINNF_FT_S_FDB_testcase",
                "FDB.1",
                workdir=HARNESS_SRC,
            )
        except (ImportError, ModuleNotFoundError, ValueError):
            self.skipTest("host harness import unavailable; covered by docker self-check")
        self.assertTrue(methods)
        self.assertTrue(methods[0].startswith("test_WINNF_FT_S_FDB_1_"))

    def test_reg1_exact_method(self):
        from tools.winnforum.discover_tests import resolve_case_methods

        try:
            methods = resolve_case_methods(
                "testcases.WINNF_FT_S_REG_testcase",
                "REG.1",
                workdir=HARNESS_SRC,
            )
        except (ImportError, ModuleNotFoundError, ValueError):
            self.skipTest("host harness import unavailable; covered by docker self-check")
        self.assertEqual(methods, ["test_WINNF_FT_S_REG_1"])


class DockerHarnessEnvTests(unittest.TestCase):
    def test_default_image_tag_contains_commit_prefix(self):
        from tools.winnforum.docker_runner import DEFAULT_IMAGE

        self.assertIn("928c3150", DEFAULT_IMAGE)
