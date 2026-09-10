# sas-rf-independent-fspl

Alternate **RfPort** plugin for Spectrum Access System RF substitutability proofs.

This package is intentionally **outside** the SAS installable package tree.
It does not contain WInnForum `reference_models` or NTIA C++ ITM sources.

## Algorithm

Independent free-space path loss using **great-circle 2D distance** (no
slant-range height correction). This is deliberately different from the
in-tree `CbrsWinnForumRfAdapter(backend="free_space")` slant-range FSPL so
Deployment A vs B produce distinguishable `loss_db` values.

Provenance string: `independent-fspl:v1`.

## Install

```bash
pip install -e tests/plugin_packages/independent_fspl
```

## Entry point

```text
spectrum_access.rf_models → independent_fspl
```

## Relation to NTIA/ITS ITM

The official NTIA/ITS ITM is distributed as a C++ library/DLL
(https://github.com/NTIA/itm), not as a first-party Python package.
Third-party ports (`pyitm`, `itmlogic`) exist but are optional and not
required for this proof. This plugin validates the **plugin boundary**;
any alternate RfPort adapter can implement the same contract for substitutability proofs.
