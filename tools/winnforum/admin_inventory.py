""": programmatic WInnForum Admin API contract inventory.

Builds the table required by the master plan:

``método | endpoint | request schema | response schema | estado alterado | casos consumidores``

Sources (no fixture device IDs):

- harness ``sas.py`` ``SasAdminImpl`` → method↔HTTP path;
- harness ``sas_interface.py`` docstrings → request/response hints;
- harness ``testcases/`` + ``sas_testcase.py`` → consumer families;
- sas-core ``routes/admin_routes.py`` → UUT implementation class.
"""

from __future__ import annotations

import argparse
import ast
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from services.admin_api_inventory import (
    EXPLICIT_ROUTED_ADMIN_POST_PATHS,
    EXPLICIT_UNIMPLEMENTED_ADMIN_POST_PATHS,
    OFFICIAL_ADMIN_POST_PATHS,
)

# Path fragment inside RequestPost('https://%s/admin/<path>' % ...)
_PATH_RE = re.compile(
    r"""https://%s/admin/(?P<path>[^'"]+)""",
)
_FAMILY_FROM_TESTCASE = re.compile(
    r"WINNF_FT_S_(?P<fam>[A-Z]+)_testcase\.py$", re.I
)
_METHOD_CALL_RE = re.compile(
    r"""(?:sas_admin|_sas_admin)\.(?P<method>[A-Za-z_][A-Za-z0-9_]*)\s*\("""
)

# Heuristic UUT response classification (route body keywords).
_UUT_JSON_RESPONSE_HINTS: dict[str, str] = {
    "get_daily_activities_status": "{completed: bool}",
    "get_ppa_status": "{completed: bool, withError: bool}",
    "trigger/create_ppa": "ppa_id string | empty",
    "injectdata/zone": "zone id JSON",
    "query/propagation_and_antenna_model": (
        "{pathlossDb, txAntennaGainDbi?, rxAntennaGainDbi?} or HTTP 400/503"
    ),
}

# Tokens that indicate real domain mutation / service use (not AdminInjectedData-only).
_UUT_DOMAIN_TOKENS: tuple[str, ...] = (
    "add_fcc_id_blacklist",
    "add_fcc_id_serial_blacklist",
    "upsert_pal",
    "create_full_activity_dump",
    "trigger_daily_activities",
    "get_daily_activities_completed",
    "enable_scheduled_daily_activities",
    "tick_scheduled_cpas",
    "persist_exclusion_zone",
    "enable_ntia_exclusion_zones",
    "known_pal_ids",
    "load_dpas",
    "activate_dpa",
    "bulk_dpa_activation",
    "deactivate_dpa",
    "create_ppa",
    "get_ppa_creation_status",
    "persist_zone_data",
    "upsert_fss_record",
    "upsert_wisp_record",
    "persist_database_url",
    "persist_esc_zone",
    "persist_cluster_list",
    "persist_sas_admin",
    "apply_esc_detection",
    "reset_esc_zone",
    "disconnect_esc",
    "enable_measurement_report_registration",
    "enable_measurement_report_heartbeat",
    "compute_propagation_and_antenna_model",
    "FccIdRecord",
    "PeerSas",
    "ConditionalRegistration",
    "CpiUser",
    "UserIdRecord",
    "EscSensor",
)


@dataclass(frozen=True)
class AdminMethodContract:
    method: str
    endpoint: str
    http_method: str = "POST"
    in_interface: bool = True
    request_schema: str = ""
    response_schema: str = "empty_200"
    state_changed: str = ""
    uut_status: str = "unknown"
    consumers: tuple[str, ...] = ()
    notes: str = ""

    def to_mapping(self) -> dict[str, Any]:
        data = asdict(self)
        data["consumers"] = list(self.consumers)
        return data


@dataclass
class AdminContractInventory:
    version: int = 1
    harness_root: str = ""
    methods: list[AdminMethodContract] = field(default_factory=list)
    gaps: dict[str, list[str]] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "harness_root": self.harness_root,
            "methods": [m.to_mapping() for m in self.methods],
            "gaps": self.gaps,
        }


def resolve_harness_src(harness_dir: Path) -> Path:
    """Accept Spectrum-Access-System root or ``src/harness`` checkout."""
    path = harness_dir.expanduser().resolve()
    if (path / "sas.py").is_file() and (path / "sas_interface.py").is_file():
        return path
    candidate = path / "src" / "harness"
    if (candidate / "sas.py").is_file():
        return candidate
    raise FileNotFoundError(
        f"harness sas.py/sas_interface.py not found under {harness_dir}"
    )


def _ast_string(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # Python <3.8 style / BinOp string concat not needed for path literals.
    return None


def extract_impl_method_paths(sas_py: Path) -> dict[str, str]:
    """Return ``{MethodName: admin/relative/path}`` from ``SasAdminImpl``."""
    return {name: meta["path"] for name, meta in extract_impl_method_meta(sas_py).items()}


def extract_impl_method_meta(sas_py: Path) -> dict[str, dict[str, Any]]:
    """Return ``{MethodName: {path, has_request}}`` from ``SasAdminImpl``."""
    tree = ast.parse(sas_py.read_text(encoding="utf-8"), filename=str(sas_py))
    mapping: dict[str, dict[str, Any]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "SasAdminImpl":
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            if item.name.startswith("_"):
                continue
            path = _first_admin_path_in_function(item)
            if not path:
                continue
            arg_names = [a.arg for a in item.args.args]
            mapping[item.name] = {
                "path": path,
                "has_request": "request" in arg_names,
            }
    return mapping


def _first_admin_path_in_function(fn: ast.FunctionDef) -> str | None:
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            m = _PATH_RE.search(sub.value)
            if m:
                return m.group("path")
        # ``'https://%s/admin/...' % self._base_url`` as BinOp in older AST?
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mod):
            left = _ast_string(sub.left)
            if left:
                m = _PATH_RE.search(left)
                if m:
                    return m.group("path")
    return None


def extract_interface_docstrings(sas_interface_py: Path) -> dict[str, str]:
    tree = ast.parse(
        sas_interface_py.read_text(encoding="utf-8"),
        filename=str(sas_interface_py),
    )
    docs: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "SasAdminInterface":
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                docs[item.name] = ast.get_docstring(item) or ""
    return docs


def _request_schema_from_doc(doc: str, *, has_request: bool) -> str:
    """Derive request schema hint from interface docstring.

    No-arg Admin methods must not use the summary sentence as a fake request schema.
    """
    if not has_request:
        return "(none)"
    if not doc:
        return "(unspecified)"
    # Prefer Args: request: … block compressed to one line.
    m = re.search(
        r"Args:\s*\n\s*request:\s*(.+?)(?:\n\s*\n|\Z)",
        doc,
        re.S | re.I,
    )
    if m:
        chunk = " ".join(line.strip() for line in m.group(1).splitlines())
        return chunk[:300]
    if "no" in doc.lower() and "arg" in doc.lower():
        return "(none)"
    # Avoid treating method summary as request schema.
    if re.search(r"\brequest\b", doc, re.I):
        first = doc.strip().split("\n", 1)[0].strip()
        return first[:200]
    return "(unspecified)"


def _response_schema_from_doc(doc: str, *, returns_body: bool) -> str:
    if not returns_body:
        return "empty_200"
    lower = doc.lower()
    if "completed" in lower and "witherror" in lower.replace(" ", ""):
        return "{completed: bool, withError: bool}"
    if "completed" in lower:
        return "{completed: bool}"
    if "pathloss" in lower or "antenna" in lower:
        return "{pathlossDb, txAntennaGainDbi, rxAntennaGainDbi?}"
    if "ppa" in lower and "id" in lower:
        return "ppa_id string"
    return "JSON body"


def _state_hint(method: str, path: str, doc: str) -> str:
    name = method.lower()
    if name == "reset":
        return "full UUT baseline reset"
    if name.startswith("inject") or name.startswith("blacklist") or name.startswith(
        "preload"
    ):
        return f"persist injection ({path})"
    if name.startswith("trigger"):
        return f"trigger side-effect ({path})"
    if name.startswith("get") or name.startswith("query"):
        return "read-only status/query"
    if "reset" in doc.lower():
        return "reset related state"
    return path


def scan_consumer_families(harness_src: Path) -> dict[str, set[str]]:
    """Map Admin method → set of family codes found in harness sources."""
    consumers: dict[str, set[str]] = {}
    roots = [
        harness_src / "testcases",
        harness_src / "sas_testcase.py",
    ]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.py")))

    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        fam = _family_label_for_file(path)
        for match in _METHOD_CALL_RE.finditer(text):
            method = match.group("method")
            consumers.setdefault(method, set()).add(fam)
        # Helpers that wrap admin calls — attribute as HELPER + file family if any.
        if "TriggerDailyActivitiesImmediatelyAndWaitUntilComplete" in text:
            consumers.setdefault("TriggerDailyActivitiesImmediately", set()).add(fam)
            consumers.setdefault("GetDailyActivitiesStatus", set()).add(fam)
        if "TriggerFullActivityDumpAndWaitUntilComplete" in text:
            consumers.setdefault("TriggerFullActivityDump", set()).add(fam)
        if "triggerPpaCreationAndWaitUntilComplete" in text or (
            "assertPpaCreationFailure" in text
        ):
            consumers.setdefault("TriggerPpaCreation", set()).add(fam)
            consumers.setdefault("GetPpaCreationStatus", set()).add(fam)
        if "assertRegistered" in text and path.name == "sas_testcase.py":
            for m in ("InjectFccId", "InjectUserId", "PreloadRegistrationData"):
                consumers.setdefault(m, set()).add("HELPER.assertRegistered")
    return consumers


def _family_label_for_file(path: Path) -> str:
    m = _FAMILY_FROM_TESTCASE.search(path.name)
    if m:
        return m.group("fam").upper()
    if path.name == "sas_testcase.py":
        return "HELPER"
    # Security / shared modules
    stem = path.stem.upper()
    if "SECURITY" in stem:
        return "SECURITY"
    return path.stem


def classify_uut_route(
    path: str,
    admin_routes_py: Path,
    *,
    routes_text: str | None = None,
) -> tuple[str, str]:
    """Return (uut_status, notes) for an admin relative path."""
    rel = path.lstrip("/")
    if rel in EXPLICIT_UNIMPLEMENTED_ADMIN_POST_PATHS:
        return "unimplemented", "explicit HTTP 501"
    if rel not in EXPLICIT_ROUTED_ADMIN_POST_PATHS:
        if rel in OFFICIAL_ADMIN_POST_PATHS:
            return "missing_route", "official path without dedicated route"
        return "unknown", "not in official inventory"

    text = routes_text if routes_text is not None else admin_routes_py.read_text(
        encoding="utf-8"
    )
    # Locate decorator block for this path.
    marker = f'@router.post("/{rel}")'
    idx = text.find(marker)
    if idx < 0:
        return "missing_route", "not found in admin_routes.py"
    # Slice until next @router.post or EOF
    rest = text[idx:]
    next_dec = rest.find("@router.post(", 1)
    block = rest if next_dec < 0 else rest[:next_dec]

    if "status_code=501" in block or "HTTP_501" in block:
        return "unimplemented", "returns 501"
    if rel == "trigger/load_dpas" and "load_dpas" not in block:
        return "stub", "HTTP 200 empty; no DPA catalogue load"
    if "reset_db" in block:
        return "implemented", "resets database"
    # Domain services / tables before thin AdminInjectedData heuristics.
    if any(token in block for token in _UUT_DOMAIN_TOKENS):
        return "implemented", "mutates domain tables/services"
    if "_store_injection" in block and block.count("db.") <= 2:
        return "thin", "persisted AdminInjectedData only"
    if "JSONResponse" in block or "return {" in block:
        return "partial", "returns JSON; domain may be incomplete"
    if "_empty_ok" in block and "_store_injection" not in block and "db.add" not in block:
        # Flag-only or empty
        if "AdminInjectedData" in block or "flag" in block.lower():
            return "thin", "flag / minimal side-effect"
        return "thin", "HTTP 200 empty with limited logic"
    return "partial", "routed; review domain completeness"


def build_inventory(
    *,
    harness_dir: Path,
    repo_root: Path,
) -> AdminContractInventory:
    harness_src = resolve_harness_src(harness_dir)
    sas_py = harness_src / "sas.py"
    iface_py = harness_src / "sas_interface.py"
    admin_routes = repo_root / "routes" / "admin_routes.py"

    meta = extract_impl_method_meta(sas_py)
    docs = extract_interface_docstrings(iface_py)
    consumers = scan_consumer_families(harness_src)
    routes_text = admin_routes.read_text(encoding="utf-8")

    methods: list[AdminMethodContract] = []
    for method, info in sorted(meta.items(), key=lambda kv: kv[0]):
        rel_path = info["path"]
        has_request = bool(info["has_request"])
        doc = docs.get(method, "")
        in_iface = method in docs
        returns_body = "return RequestPost" in _method_source_snippet(sas_py, method)
        # Prefer UUT response hint when present.
        response = _UUT_JSON_RESPONSE_HINTS.get(
            rel_path, _response_schema_from_doc(doc, returns_body=returns_body)
        )
        uut_status, uut_notes = classify_uut_route(
            rel_path, admin_routes, routes_text=routes_text
        )
        fams = tuple(sorted(consumers.get(method, set())))
        # Known schema drift: interface says serialNumber; UUT uses cbsdSerialNumber.
        notes_parts = [uut_notes]
        if method == "BlacklistByFccIdAndSerialNumber":
            notes_parts.append(
                "interface docstring uses serialNumber; "
                "sas-core schema expects cbsdSerialNumber"
            )
        if not in_iface:
            notes_parts.append("Impl-only method (not on SasAdminInterface ABC)")
        methods.append(
            AdminMethodContract(
                method=method,
                endpoint=f"/admin/{rel_path}",
                in_interface=in_iface,
                request_schema=_request_schema_from_doc(doc, has_request=has_request),
                response_schema=response,
                state_changed=_state_hint(method, rel_path, doc),
                uut_status=uut_status,
                consumers=fams,
                notes="; ".join(p for p in notes_parts if p),
            )
        )

    official_paths = {f"/admin/{p}" for p in OFFICIAL_ADMIN_POST_PATHS}
    inventoried = {m.endpoint for m in methods}
    gaps = {
        "official_paths_missing_from_impl_parse": sorted(
            p for p in official_paths if p not in inventoried
        ),
        "impl_paths_not_in_official_inventory": sorted(
            p for p in inventoried if p not in official_paths
        ),
        "stub_or_unimplemented": sorted(
            m.endpoint
            for m in methods
            if m.uut_status in {"stub", "unimplemented", "missing_route"}
        ),
    }
    harness_resolved = harness_dir.expanduser().resolve()
    try:
        harness_label = str(harness_src.relative_to(harness_resolved))
    except ValueError:
        harness_label = harness_src.name
    if harness_label in {"", "."}:
        harness_label = "src/harness"

    return AdminContractInventory(
        harness_root=harness_label,
        methods=methods,
        gaps=gaps,
    )


def _method_source_snippet(sas_py: Path, method: str) -> str:
    text = sas_py.read_text(encoding="utf-8")
    m = re.search(
        rf"\n  def {re.escape(method)}\(.*?\n(?:  def |\Z)",
        text,
        re.S,
    )
    return m.group(0) if m else ""


def write_inventory_yaml(inventory: AdminContractInventory, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        yaml.safe_dump(
            inventory.to_mapping(),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=100,
        ),
        encoding="utf-8",
    )


def render_markdown_table(inventory: AdminContractInventory) -> str:
    lines = [
        "| método | endpoint | request schema | response schema | estado alterado | uut | consumidores |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in inventory.methods:
        req = m.request_schema.replace("|", "\\|")[:80]
        resp = m.response_schema.replace("|", "\\|")[:40]
        state = m.state_changed.replace("|", "\\|")[:40]
        cons = ",".join(m.consumers) if m.consumers else "—"
        lines.append(
            f"| `{m.method}` | `{m.endpoint}` | {req} | {resp} | {state} | "
            f"{m.uut_status} | {cons} |"
        )
    return "\n".join(lines) + "\n"


def default_harness_dir(repo_root: Path) -> Path | None:
    sibling = repo_root.parent / "winnforum-sas-harness"
    if sibling.is_dir():
        return sibling
    return None


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--harness-dir",
        type=Path,
        default=None,
        help="Harness checkout (Spectrum-Access-System root or src/harness).",
    )
    parser.add_argument(
        "--write",
        type=Path,
        default=repo_root / "compliance" / "admin_contract.yaml",
        help="Output YAML path.",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="Optional markdown table output path.",
    )
    args = parser.parse_args(argv)
    harness = args.harness_dir or default_harness_dir(repo_root)
    if harness is None:
        print("error: pass --harness-dir (sibling winnforum-sas-harness not found)")
        return 2
    inventory = build_inventory(harness_dir=harness, repo_root=repo_root)
    write_inventory_yaml(inventory, args.write)
    print(f"wrote {args.write} methods={len(inventory.methods)}")
    if args.markdown:
        args.markdown.write_text(render_markdown_table(inventory), encoding="utf-8")
        print(f"wrote {args.markdown}")
    stubs = inventory.gaps.get("stub_or_unimplemented", [])
    if stubs:
        print("stub_or_unimplemented:")
        for s in stubs:
            print(f"  - {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
