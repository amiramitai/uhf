# Infrastructure — Shared Computational Engines

Shared LBM (Lattice Boltzmann Method) and GPE (Gross–Pitaevskii Equation) simulation kernels used across all UHF research domains.

## Structure

```
lbm-gpe/   — Python simulation scripts (LBM, GPE, verification suites)
```

These scripts are domain-agnostic and serve as the computational backbone for `physics-core/simulations/`, `chemistry/simulations/`, and `biology/simulations/`.
