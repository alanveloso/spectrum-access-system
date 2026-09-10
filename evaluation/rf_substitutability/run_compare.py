"""Regenerate RF A/B comparison tables for evaluation/rf_substitutability."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "tests" / "plugin_packages" / "independent_fspl"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

from primitives.geography import GeoPoint  # noqa: E402
from rf.cbrs_winnforum import free_space_rf_adapter  # noqa: E402
from rf.port import PathLossRequest  # noqa: E402
from sas_rf_independent_fspl.adapter import independent_fspl_rf_adapter  # noqa: E402

HERE = Path(__file__).resolve().parent


def main() -> None:
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    freq = int(cases["frequency_hz"])
    rx_h = float(cases["rx_height_m"])
    ref = free_space_rf_adapter()
    alt = independent_fspl_rf_adapter()
    rows: list[dict[str, object]] = []
    for case in cases["cases"]:
        req = PathLossRequest(
            tx=GeoPoint(latitude_deg=case["tx_lat"], longitude_deg=case["tx_lon"]),
            rx=GeoPoint(latitude_deg=case["rx_lat"], longitude_deg=case["rx_lon"]),
            tx_height_m=float(case["tx_height_m"]),
            rx_height_m=rx_h,
            frequency_hz=freq,
        )
        a = ref.path_loss(req).loss_db
        b = alt.path_loss(req).loss_db
        abs_delta = abs(a - b)
        rel_delta = abs_delta / max(abs(a), 1e-9)
        rows.append(
            {
                "case": case["id"],
                "reference_loss_db": a,
                "alternate_loss_db": b,
                "abs_delta_db": abs_delta,
                "rel_delta": rel_delta,
            }
        )
    out_csv = HERE / "comparison.csv"
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (HERE / "results_reference.json").write_text(
        json.dumps(
            [{"case": r["case"], "loss_db": r["reference_loss_db"]} for r in rows],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (HERE / "results_alternate.json").write_text(
        json.dumps(
            [{"case": r["case"], "loss_db": r["alternate_loss_db"]} for r in rows],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out_csv}")


if __name__ == "__main__":
    main()
