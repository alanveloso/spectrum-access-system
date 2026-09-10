"""WInnForum harness family → unittest module mapping (official SAS UUT suites)."""

from __future__ import annotations

from dataclasses import dataclass

# Module paths relative to Spectrum-Access-System ``src/harness``.
FAMILY_TEST_MODULES: dict[str, str] = {
    "REG": "testcases.WINNF_FT_S_REG_testcase",
    "SIQ": "testcases.WINNF_FT_S_SIQ_testcase",
    "GRA": "testcases.WINNF_FT_S_GRA_testcase",
    "HBT": "testcases.WINNF_FT_S_HBT_testcase",
    "RLQ": "testcases.WINNF_FT_S_RLQ_testcase",
    "DRG": "testcases.WINNF_FT_S_DRG_testcase",
    "FAD": "testcases.WINNF_FT_S_FAD_testcase",
    "SSS": "testcases.WINNF_FT_S_SSS_testcase",
    "SCS": "testcases.WINNF_FT_S_SCS_testcase",
    "SDS": "testcases.WINNF_FT_S_SDS_testcase",
    "EXZ": "testcases.WINNF_FT_S_EXZ_testcase",
    "BPR": "testcases.WINNF_FT_S_BPR_testcase",
    "EPR": "testcases.WINNF_FT_S_EPR_testcase",
    "QPR": "testcases.WINNF_FT_S_QPR_testcase",
    "WDB": "testcases.WINNF_FT_S_WDB_testcase",
    "FDB": "testcases.WINNF_FT_S_FDB_testcase",
    "GPR": "testcases.WINNF_FT_S_GPR_testcase",
    "PCR": "testcases.WINNF_FT_S_PCR_testcase",
    "PAT": "testcases.WINNF_FT_S_PAT_testcase",
    "IPR": "testcases.WINNF_FT_S_IPR_testcase",
    "PPR": "testcases.WINNF_FT_S_PPR_testcase",
    "FPR": "testcases.WINNF_FT_S_FPR_testcase",
    "MCP": "testcases.WINNF_FT_S_MCP_testcase",
    "MES": "testcases.WINNF_FT_S_MES_testcase",
}


@dataclass(frozen=True)
class UnittestTarget:
    """Module to load, optionally filtered to a single test method name."""

    module: str
    method: str | None = None

    def label(self) -> str:
        return self.module if not self.method else f"{self.module}::{self.method}"


def normalize_family(token: str) -> str:
    return token.strip().upper()


def resolve_unittest_targets(
    families: list[str] | None,
    cases: list[str] | None,
) -> list[UnittestTarget]:
    """Build loadable unittest targets from family codes and/or case ids.

 Case forms (no fixture hardcodes, no guessed TestCase class names):
 - ``REG.1`` → module for REG + method ``test_WINNF_FT_S_REG_1``
 - ``test_WINNF_FT_S_REG_1`` → infer family from token or require ``--family``
 - ``testcases.…`` full module path → entire module (method optional via ``::``)
 """
    targets: list[UnittestTarget] = []
    fams = [normalize_family(f) for f in (families or []) if f.strip()]
    case_list = [c.strip() for c in (cases or []) if c.strip()]

    if not fams and not case_list:
        raise ValueError("at least one --family or --case is required")

    for fam in fams:
        if fam not in FAMILY_TEST_MODULES:
            raise ValueError(f"unknown family {fam!r}; known={sorted(FAMILY_TEST_MODULES)}")
        targets.append(UnittestTarget(module=FAMILY_TEST_MODULES[fam]))

    for case in case_list:
        if "::" in case and case.startswith("testcases."):
            module, method = case.split("::", 1)
            targets.append(UnittestTarget(module=module, method=method or None))
            continue
        if case.startswith("testcases."):
            targets.append(UnittestTarget(module=case))
            continue
        if case.startswith("test_WINNF_FT_S_"):
            method = case
            if len(fams) == 1:
                targets.append(
                    UnittestTarget(module=FAMILY_TEST_MODULES[fams[0]], method=method)
                )
            else:
                parts = case.split("_")
                if len(parts) >= 6:
                    fam = parts[4].upper()
                    if fam not in FAMILY_TEST_MODULES:
                        raise ValueError(f"cannot map case {case!r} to a known family")
                    targets.append(
                        UnittestTarget(module=FAMILY_TEST_MODULES[fam], method=method)
                    )
                else:
                    raise ValueError(f"unrecognized case id {case!r}")
            continue
        if "." in case and not case.startswith("test_"):
            fam_part, num = case.split(".", 1)
            fam = normalize_family(fam_part)
            if fam not in FAMILY_TEST_MODULES:
                raise ValueError(f"unknown family in case {case!r}")
            if not num or not num.replace("_", "").isalnum():
                raise ValueError(f"invalid case number in {case!r}")
            module = FAMILY_TEST_MODULES[fam]
            # Configurable harness cases expand to e.g. test_WINNF_FT_S_FDB_1_0_default.
            try:
                from tools.winnforum.discover_tests import resolve_case_methods

                for method in resolve_case_methods(module, f"{fam}.{num}"):
                    targets.append(UnittestTarget(module=module, method=method))
            except (ImportError, ValueError):
                method = f"test_WINNF_FT_S_{fam}_{num}"
                targets.append(UnittestTarget(module=module, method=method))
            continue
        raise ValueError(f"unrecognized case selector {case!r}")

    seen: set[str] = set()
    ordered: list[UnittestTarget] = []
    for t in targets:
        key = t.label()
        if key not in seen:
            seen.add(key)
            ordered.append(t)
    return ordered
