# RF substitutability evaluation

Deterministic comparison of Deployment A (`free_space`) vs Deployment B
(`independent_fspl`) path-loss samples.

This directory is an **evaluation** artifact area, not production code.

## Cases

See `cases.json` — fixed TX/RX geometry and frequency samples.

## How to regenerate

```bash
python -m evaluation.rf_substitutability.run_compare
```

(or run the unit test that writes comparison when requested)

## Interpretation

Differences are expected: the backends use different Free-Space distance models.
They do **not** indicate an architecture failure.
