"""Production image must not embed developer-local certification datasets."""

from __future__ import annotations


from tests.support.repo import REPO_ROOT as ROOT


def _dockerignore_lines() -> list[str]:
    return [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_dockerignore_excludes_certification_geo_payloads_keeps_metadata():
    lines = _dockerignore_lines()
    for payload, readme, version in (
        ("data/geo/ned/**", "!data/geo/ned/README.md", "!data/geo/ned/VERSION"),
        ("data/geo/nlcd/**", "!data/geo/nlcd/README.md", "!data/geo/nlcd/VERSION"),
        ("data/geo/county/**", "!data/geo/county/README.md", "!data/geo/county/VERSION"),
        ("data/ntia/**", "!data/ntia/README.md", "!data/ntia/VERSION"),
    ):
        assert payload in lines, f"missing exclude {payload}"
        assert readme in lines, f"missing re-include {readme}"
        assert version in lines, f"missing re-include {version}"


def test_dockerignore_excludes_secrets_and_acceptance_artifacts():
    lines = set(_dockerignore_lines())
    for path in (".env", "certs", "artifacts", ".cache"):
        assert path in lines, f"missing exclude {path}"
