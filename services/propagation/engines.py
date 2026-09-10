"""Load WInnForum reference-model engines (hybrid/antenna/PAT) + external ITM.

ITM calculator and harness path resolution come from ``spectrum_propagation``
integrations. Hybrid / antenna / NLCD remain SAS orchestration for PAT/PPA.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from services.propagation.errors import PropagationUnavailableError
from services.propagation.service import (
    ACTIVITY_LOSS_FACTOR_DEFAULT,
    PropagationEngines,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def resolve_harness_dir(explicit: Path | str | None = None) -> Path | None:
    from spectrum_propagation.integrations.winnforum.harness import (
        resolve_harness_dir as sp_resolve,
    )

    found = sp_resolve(explicit)
    if found is not None:
        return Path(found)
    # Legacy SAS-only sibling under the product repo parent.
    sibling = _REPO_ROOT.parent / "winnforum-sas-harness" / "src" / "harness"
    if sibling.is_dir():
        return sibling
    return None


def _ensure_harness_on_path(harness_dir: Path) -> None:
    resolved = str(harness_dir.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


def load_reference_engines(
    harness_dir: str | None = None,
    terrain_dir: str | None = None,
    nlcd_dir: str | None = None,
) -> PropagationEngines:
    """Import harness reference models and configure terrain/NLCD drivers.

    Cache key includes harness, terrain and NLCD paths so env changes are not
    silently ignored after the first load.
    """
    root = resolve_harness_dir(harness_dir)
    if root is None:
        raise PropagationUnavailableError(
            "WInnForum harness reference_models not found "
            "(set SAS_HARNESS_DIR or place sibling winnforum-sas-harness)"
        )
    ned = Path(
        terrain_dir
        or os.environ.get("SAS_TERRAIN_DIR")
        or (_REPO_ROOT / "data" / "geo" / "ned")
    )
    nlcd = nlcd_dir if nlcd_dir is not None else os.environ.get("SAS_NLCD_DIR")
    return _load_reference_engines_cached(
        str(root.resolve()),
        str(ned.resolve()),
        (nlcd or "").strip(),
    )


@lru_cache(maxsize=8)
def _load_reference_engines_cached(
    harness_dir: str,
    terrain_dir: str,
    nlcd_dir: str,
) -> PropagationEngines:
    from spectrum_propagation.errors import ModelUnavailableError, TerrainDataError
    from spectrum_propagation.integrations.winnforum.harness import load_itm_engine

    root = Path(harness_dir)
    try:
        calc_itm, terrain_elevation_m = load_itm_engine(harness_dir, terrain_dir)
    except (ModelUnavailableError, TerrainDataError) as exc:
        raise PropagationUnavailableError(str(exc)) from exc

    # Hybrid / antenna / P.2108 remain on the same external checkout.
    _ensure_harness_on_path(root)
    try:
        from reference_models.antenna import antenna
        from reference_models.geo import drive, utils as geoutils
        from reference_models.propagation import p2108, wf_hybrid
    except Exception as exc:  # noqa: BLE001
        raise PropagationUnavailableError(
            f"reference_models import failed: {exc}"
        ) from exc

    if nlcd_dir:
        try:
            drive.ConfigureNlcdDriver(nlcd_dir=nlcd_dir)
        except Exception as exc:  # noqa: BLE001
            raise PropagationUnavailableError(
                f"nlcd driver configure failed: {exc}"
            ) from exc

    activity = float(getattr(p2108, "ACTIVITY_LOSS_FACTOR", ACTIVITY_LOSS_FACTOR_DEFAULT))

    if terrain_elevation_m is None:

        def terrain_elevation_m(lat: float, lon: float) -> float:
            return float(drive.terrain_driver.GetTerrainElevation(lat, lon))

    return PropagationEngines(
        calc_itm=calc_itm,
        calc_hybrid=wf_hybrid.CalcHybridPropagationLoss,
        calc_p2108=p2108.calc_P2108,
        activity_loss_factor=activity,
        antenna_standard_gains=antenna.GetStandardAntennaGains,
        antenna_fss_gains=antenna.GetFssAntennaGains,
        antenna_pattern_gains=antenna.GetAntennaPatternGains,
        grid_polygon=geoutils.GridPolygon,
        region_nlcd_vote=drive.nlcd_driver.RegionNlcdVote,
        terrain_elevation_m=terrain_elevation_m,
    )


def clear_reference_engines_cache() -> None:
    _load_reference_engines_cached.cache_clear()
    try:
        from spectrum_propagation.integrations.winnforum.harness import clear_itm_cache

        clear_itm_cache()
    except Exception:  # noqa: BLE001
        pass
