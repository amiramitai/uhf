# Infrastructure — Shared Computational Engines

Shared LBM (Lattice Boltzmann Method) and GPE (Gross–Pitaevskii Equation) simulation kernels used across all UHF research domains.

## Structure

```
lbm-gpe/   — Python simulation scripts (LBM, GPE, verification suites)
```

These scripts are domain-agnostic and serve as the computational backbone for `uhf_physics/simulations/`, `uhf_chemistry/simulations/`, and `uhf_biology/simulations/`.
