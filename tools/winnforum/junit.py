"""JUnit XML writer for harness case results."""

from __future__ import annotations

import html
from pathlib import Path
from xml.etree.ElementTree import Element, ElementTree, SubElement

from tools.winnforum.unittest_parse import HarnessRunResult


def write_junit_xml(result: HarnessRunResult, path: Path, *, suite_name: str) -> None:
    failures = sum(1 for c in result.cases if c.status == "failed")
    errors = sum(1 for c in result.cases if c.status == "error")
    skipped = sum(1 for c in result.cases if c.status == "skipped")
    suite = Element(
        "testsuite",
        {
            "name": suite_name,
            "tests": str(len(result.cases)),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "time": str(result.duration_seconds or 0),
        },
    )
    for case in result.cases:
        classname = case.class_name
        tc = SubElement(
            suite,
            "testcase",
            {
                "classname": classname,
                "name": case.name,
                "time": "0",
            },
        )
        if case.status == "failed":
            failure = SubElement(tc, "failure", {"message": case.status})
            failure.text = html.escape(case.message or case.status)
        elif case.status == "error":
            err = SubElement(tc, "error", {"message": case.status})
            err.text = html.escape(case.message or case.status)
        elif case.status == "skipped":
            SubElement(tc, "skipped")
        elif case.status == "unexpected":
            err = SubElement(tc, "error", {"message": "unexpected status"})
            err.text = case.status

    path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)
