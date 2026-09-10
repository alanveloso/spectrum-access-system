# RF boundary

## Contract

The **RfPort** is the sole propagation boundary used by protection / IAP paths.
Terrain datasets are separate from the propagation model.

IAP/BPR consumers adapt specialized models (`GrantRfInfo`, …) to `RfPort` on the
**service side** (`services/iap/rf_adapter.py`). Generic `runtime` and RF contract
modules must not import those service models.

## Reference implementation

The pinned submodule `plugins/spectrum-propagation` provides the reference
Free-Space / ITM-backed adapters registered as production RF plugins
(`free_space`, `itm`).

## Fitness / substitutability

An independent Free-Space plugin used only for RF Lego proofs lives under
`tests/plugin_packages/independent_fspl/` and is **not** a supported production
RF product in this baseline.

## Deferred

Specialized ESC/DPA RF paths outside the generic RfPort contract remain known
limitations. Alternative RF products (beyond this baseline) are out of tree.
