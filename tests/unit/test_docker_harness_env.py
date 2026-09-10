"""Regression checks for the official WInnForum harness Docker image."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
IMAGE = "winnforum-sas-harness:928c3150"
EXPECTED_COMMIT_PREFIX = "928c3150"


@unittest.skipUnless(
    subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode
    == 0,
    f"docker image {IMAGE} not built",
)
class DockerHarnessEnvRegression(unittest.TestCase):
    def test_image_reports_python311_and_shapely171(self):
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{ROOT}:/opt/sas:ro",
                "-v",
                f"{ROOT / 'data/geo'}:/common-data/geo:ro",
                "-v",
                f"{ROOT / '.cache/winnforum-harness-pki'}:/opt/winnforum-harness/src/harness/certs:ro",
                IMAGE,
                "python",
                "-c",
                "import platform, shapely; "
                "print(platform.python_version()); print(shapely.__version__)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = (proc.stdout or "").strip().splitlines()
        self.assertTrue(lines[0].startswith("3.11."), lines)
        self.assertEqual(lines[1], "1.7.1")

    def test_self_check_passes_when_pki_mounted(self):
        from tools.winnforum.docker_runner import docker_self_check

        if not (ROOT / ".cache/winnforum-harness-pki/ca.cert").is_file():
            self.skipTest("harness PKI cache missing")
        rc, output = docker_self_check()
        self.assertEqual(rc, 0, output)
        self.assertIn("OVERALL: PASS", output)

    def test_harness_commit_label(self):
        proc = subprocess.run(
            ["docker", "image", "inspect", IMAGE, "--format", "{{json .Config.Labels}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        labels = json.loads(proc.stdout)
        commit = labels.get("winnforum.harness.commit", "")
        self.assertTrue(commit.startswith(EXPECTED_COMMIT_PREFIX), commit)
