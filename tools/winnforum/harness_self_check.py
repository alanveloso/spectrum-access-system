#!/usr/bin/env python3
"""Deterministic self-check for the official WInnForum harness Docker image."""

from __future__ import annotations

import importlib
import os
import platform
import sys
from pathlib import Path


def _check(label: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f": {detail}"
    print(line)
    return ok


def main() -> int:
    ok = True
    commit_file = Path("/opt/winnforum-harness/HARNESS_COMMIT")
    expected_commit = os.environ.get(
        "HARNESS_COMMIT", "928c3150adf7b31e53a96b695bf1fbdd3284ecb2"
    )
    observed_commit = (
        commit_file.read_text(encoding="utf-8").strip() if commit_file.is_file() else ""
    )
    ok &= _check(
        "harness commit",
        observed_commit.startswith(expected_commit[:12]),
        observed_commit or "missing",
    )

    py = platform.python_version()
    ok &= _check("python version", py.startswith("3.11."), py)

    import shapely

    ok &= _check("shapely version", shapely.__version__ == "1.7.1", shapely.__version__)

    import numpy

    ok &= _check("numpy import", True, numpy.__version__)

    try:
        from osgeo import gdal  # type: ignore[import-untyped]

        ok &= _check("GDAL import", True, gdal.__version__)
    except Exception as exc:  # noqa: BLE001
        ok &= _check("GDAL import", False, str(exc))

    try:
        from shapely.geos import geos_version_string

        geos_ver = (
            geos_version_string
            if isinstance(geos_version_string, str)
            else geos_version_string()
        )
        ok &= _check("GEOS", True, geos_ver)
    except Exception as exc:  # noqa: BLE001
        ok &= _check("GEOS", False, str(exc))

    for mod in (
        "lxml",
        "pykml",
        "shapefile",
        "pygc",
        "jsonschema",
        "OpenSSL",
        "pycurl",
        "portpicker",
        "psutil",
        "pytest",
        "pytz",
    ):
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "ok")
            ok &= _check(f"import {mod}", True, str(ver))
        except Exception as exc:  # noqa: BLE001
            ok &= _check(f"import {mod}", False, str(exc))

    # Official harness import (workdir must be src/harness).
    workdir = Path(os.environ.get("HARNESS_WORKDIR", "/opt/winnforum-harness/src/harness"))
    os.chdir(workdir)
    sys.path.insert(0, str(workdir))
    try:
        import sas  # noqa: F401  # type: ignore[import-untyped]
        import sas_testcase  # noqa: F401  # type: ignore[import-untyped]

        ok &= _check("harness import sas/sas_testcase", True)
    except Exception as exc:  # noqa: BLE001
        ok &= _check("harness import sas/sas_testcase", False, str(exc))

    certs = workdir / "certs"
    ok &= _check(
        "harness PKI directory",
        certs.is_dir() and (certs / "ca.cert").is_file(),
        str(certs),
    )

    common = Path(os.environ.get("COMMON_DATA_DIR", "/common-data"))
    if common.is_dir():
        ok &= _check("Common-Data mount", True, str(common))
        commit_file = common / "COMMON_DATA_COMMIT"
        # Prefer sidecar commit file or sibling .git via env.
        cd_commit = os.environ.get("COMMON_DATA_COMMIT", "")
        if commit_file.is_file():
            cd_commit = commit_file.read_text(encoding="utf-8").strip()
        if cd_commit:
            _check("Common-Data commit", True, cd_commit)
        ned = common / "ned"
        nlcd = common / "nlcd"
        ned_flt = list(ned.glob("*.flt")) if ned.is_dir() else []
        nlcd_int = list(nlcd.glob("*.int")) if nlcd.is_dir() else []
        ok &= _check("NED root", ned.is_dir() and bool(ned_flt), f"{ned} tiles={len(ned_flt)}")
        ok &= _check("NLCD root", nlcd.is_dir() and bool(nlcd_int), f"{nlcd} tiles={len(nlcd_int)}")
    else:
        ok &= _check("Common-Data mount", False, f"not mounted ({common})")

    county = Path(os.environ.get("COMMON_DATA_COUNTY_DIR", "/common-data-county"))
    harness_county = Path("/opt/winnforum-harness/data/county")
    county_ok = harness_county.is_dir() and any(harness_county.glob("*.json"))
    if not county_ok:
        county_ok = county.is_dir() and any(county.glob("*.json"))
    _check("county root", county_ok, str(harness_county if harness_county.is_dir() else county))

    # Official reference-model driver smoke (not mere file existence).
    try:
        from reference_models.geo import drive  # type: ignore[import-untyped]

        drive.ConfigureTerrainDriver(
            terrain_dir=str(Path("/opt/winnforum-harness/data/geo/ned")),
            cache_size=2,
        )
        elev = drive.terrain_driver.GetTerrainElevation(38.7, -97.4, do_interp=False)
        ok &= _check("NED driver smoke", elev is not None, f"elev={elev}")
    except Exception as exc:  # noqa: BLE001
        ok &= _check("NED driver smoke", False, str(exc))

    try:
        from reference_models.geo import drive  # type: ignore[import-untyped]

        drive.ConfigureNlcdDriver(
            nlcd_dir=str(Path("/opt/winnforum-harness/data/geo/nlcd")),
            cache_size=2,
        )
        code = drive.nlcd_driver.GetLandCoverCodes(39.5, -99.5)
        ok &= _check("NLCD driver smoke", code is not None, f"code={code}")
    except Exception as exc:  # noqa: BLE001
        ok &= _check("NLCD driver smoke", False, str(exc))

    sas_mount = Path(os.environ.get("SAS_REPO_MOUNT", "/opt/sas"))
    if (sas_mount / "tools" / "winnforum" / "exec_unittest.py").is_file():
        ok &= _check("SAS runner overlay", True, str(sas_mount))
    else:
        _check("SAS runner overlay", False, f"missing at {sas_mount}")

    # Shapely 1.x API used by official harness reference models.
    try:
        from shapely import geometry

        ok &= _check(
            "shapely.geometry.asMultiPoint",
            hasattr(geometry, "asMultiPoint"),
            shapely.__version__,
        )
    except Exception as exc:  # noqa: BLE001
        ok &= _check("shapely.geometry.asMultiPoint", False, str(exc))

    print("---")
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
