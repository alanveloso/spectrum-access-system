"""Admin contract inventory extractor tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.support.repo import REPO_ROOT
from tools.winnforum.admin_inventory import (
    _request_schema_from_doc,
    build_inventory,
    classify_uut_route,
    default_harness_dir,
    extract_impl_method_meta,
    extract_impl_method_paths,
    render_markdown_table,
    resolve_harness_src,
    write_inventory_yaml,
)

HARNESS = default_harness_dir(REPO_ROOT)

# ---------------------------------------------------------------------------
# Synthetic harness (no sibling checkout required — CI-safe)
# ---------------------------------------------------------------------------

_SYNTH_SAS = '''
class SasAdminImpl(object):
  def Reset(self):
    RequestPost('https://%s/admin/reset' % self._base_url)

  def InjectFccId(self, request):
    RequestPost('https://%s/admin/injectdata/fcc_id' % self._base_url, request)

  def TriggerLoadDpas(self):
    RequestPost('https://%s/admin/trigger/load_dpas' % self._base_url)

  def QueryPropagationAndAntennaModel(self, request):
    return RequestPost(
        'https://%s/admin/query/propagation_and_antenna_model' % self._base_url,
        request)
'''

_SYNTH_IFACE = '''
class SasAdminInterface(object):
  def Reset(self):
    """SAS admin interface to reset the SAS between test cases."""

  def InjectFccId(self, request):
    """Inject an FCC ID.

    Args:
      request: A dictionary with key "fccId" (string).
    """

  def TriggerLoadDpas(self):
    """Load DPAs."""

  def QueryPropagationAndAntennaModel(self, request):
    """Query pathloss.

    Args:
      request: Propagation request dict.
    """
'''


def _write_synth_harness(tmp_path: Path) -> Path:
    root = tmp_path / "Spectrum-Access-System"
    harness = root / "src" / "harness"
    harness.mkdir(parents=True)
    (harness / "sas.py").write_text(_SYNTH_SAS, encoding="utf-8")
    (harness / "sas_interface.py").write_text(_SYNTH_IFACE, encoding="utf-8")
    (harness / "testcases").mkdir()
    (harness / "testcases" / "WINNF_FT_S_GRA_testcase.py").write_text(
        "self._sas_admin.Reset()\nself._sas_admin.TriggerLoadDpas()\n",
        encoding="utf-8",
    )
    (harness / "sas_testcase.py").write_text(
        "# helper\n",
        encoding="utf-8",
    )
    return root


def test_request_schema_none_for_no_arg_methods():
    assert _request_schema_from_doc("SAS admin interface to reset", has_request=False) == (
        "(none)"
    )
    schema = _request_schema_from_doc(
        "Inject.\n\nArgs:\n  request: A dictionary with key fccId.\n",
        has_request=True,
    )
    assert "fccId" in schema


def test_extract_impl_meta_from_synthetic_harness(tmp_path: Path):
    root = _write_synth_harness(tmp_path)
    src = resolve_harness_src(root)
    meta = extract_impl_method_meta(src / "sas.py")
    assert meta["Reset"]["path"] == "reset"
    assert meta["Reset"]["has_request"] is False
    assert meta["InjectFccId"]["has_request"] is True
    assert meta["TriggerLoadDpas"]["path"] == "trigger/load_dpas"
    paths = extract_impl_method_paths(src / "sas.py")
    assert paths["QueryPropagationAndAntennaModel"] == (
        "query/propagation_and_antenna_model"
    )


def test_build_inventory_synthetic_marks_stub_and_none_request(tmp_path: Path):
    root = _write_synth_harness(tmp_path)
    inv = build_inventory(harness_dir=root, repo_root=REPO_ROOT)
    by_name = {m.method: m for m in inv.methods}
    assert by_name["Reset"].request_schema == "(none)"
    assert by_name["TriggerLoadDpas"].request_schema == "(none)"
    assert by_name["TriggerLoadDpas"].uut_status == "implemented"
    assert by_name["QueryPropagationAndAntennaModel"].uut_status == "implemented"
    assert "fccId" in by_name["InjectFccId"].request_schema
    assert "GRA" in by_name["Reset"].consumers


def test_classify_exclusion_zone_as_implemented():
    routes = REPO_ROOT / "routes" / "admin_routes.py"
    status, notes = classify_uut_route("injectdata/exclusion_zone", routes)
    assert status == "implemented"
    assert "domain" in notes.lower()


# ---------------------------------------------------------------------------
# Live sibling harness (optional)
# ---------------------------------------------------------------------------

pytestmark_live = pytest.mark.skipif(
    HARNESS is None or not HARNESS.is_dir(),
    reason="sibling winnforum-sas-harness checkout required",
)


@pytestmark_live
def test_resolve_harness_src_accepts_root_and_src_harness():
    src = resolve_harness_src(HARNESS)
    assert (src / "sas.py").is_file()
    assert resolve_harness_src(src) == src


@pytestmark_live
def test_extract_impl_covers_official_reset_and_load_dpas():
    src = resolve_harness_src(HARNESS)
    paths = extract_impl_method_paths(src / "sas.py")
    assert paths["Reset"] == "reset"
    assert paths["TriggerLoadDpas"] == "trigger/load_dpas"
    assert paths["QueryPropagationAndAntennaModel"] == (
        "query/propagation_and_antenna_model"
    )
    # Impl-only extras still inventoried
    assert "InjectEscZone" in paths


@pytestmark_live
def test_build_inventory_has_required_columns_and_marks_stubs():
    inv = build_inventory(harness_dir=HARNESS, repo_root=REPO_ROOT)
    assert len(inv.methods) >= 30
    by_name = {m.method: m for m in inv.methods}
    reset = by_name["Reset"]
    assert reset.endpoint == "/admin/reset"
    assert reset.http_method == "POST"
    assert reset.request_schema == "(none)"
    assert reset.response_schema
    assert reset.state_changed
    assert reset.consumers
    assert by_name["TriggerLoadDpas"].uut_status == "implemented"
    assert by_name["TriggerLoadDpas"].request_schema == "(none)"
    assert by_name["QueryPropagationAndAntennaModel"].uut_status == "implemented"
    assert by_name["InjectExclusionZone"].uut_status == "implemented"
    assert by_name["GetDailyActivitiesStatus"].uut_status == "implemented"
    assert by_name["GetDailyActivitiesStatus"].request_schema == "(none)"
    assert by_name["TriggerDpaActivation"].uut_status == "implemented"
    assert by_name["TriggerBulkDpaActivation"].uut_status == "implemented"
    assert by_name["TriggerDpaDeactivation"].uut_status == "implemented"
    assert by_name["TriggerPpaCreation"].uut_status == "implemented"
    assert by_name["GetPpaCreationStatus"].uut_status == "implemented"
    assert by_name["TriggerEnableScheduledDailyActivities"].uut_status == "implemented"
    # Schema drift note for blacklist serial field
    assert "cbsdSerialNumber" in by_name["BlacklistByFccIdAndSerialNumber"].notes


@pytestmark_live
def test_inventory_paths_align_with_official_admin_set():
    from services.admin_api_inventory import OFFICIAL_ADMIN_POST_PATHS

    inv = build_inventory(harness_dir=HARNESS, repo_root=REPO_ROOT)
    inventoried = {m.endpoint.removeprefix("/admin/") for m in inv.methods}
    # Every official path must appear (Impl is source of truth for OFFICIAL set).
    missing = set(OFFICIAL_ADMIN_POST_PATHS) - inventoried
    assert not missing, sorted(missing)


@pytestmark_live
def test_write_yaml_roundtrip(tmp_path: Path):
    inv = build_inventory(harness_dir=HARNESS, repo_root=REPO_ROOT)
    dest = tmp_path / "admin_contract.yaml"
    write_inventory_yaml(inv, dest)
    data = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert len(data["methods"]) == len(inv.methods)
    row = data["methods"][0]
    for key in (
        "method",
        "endpoint",
        "request_schema",
        "response_schema",
        "state_changed",
        "consumers",
    ):
        assert key in row
    md = render_markdown_table(inv)
    assert "| método |" in md
    assert "`Reset`" in md
