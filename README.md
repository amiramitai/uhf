# UHF — Unified Hydrodynamic Framework

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19190638.svg)](https://doi.org/10.5281/zenodo.19190638)

*What if Spacetime Were Fluid?*

UHF is a research programme exploring whether inertia, gravity, and related phenomena can emerge as effective behaviours of a superfluid substrate. The core results are lattice-Boltzmann and analytic calculations; broader implications remain speculative and are clearly separated.

---

## Core Stack (Peer-Review Track)

Three self-contained papers, each making bounded, testable claims:

| # | Paper | Key result |
|---|---|---|
| 1 | [Added-Mass Drag on Topological Defects — Grid-Converged LBM Study](uhf_physics/papers/paper1/Paper1_Emergent_Inertia_LBM.pdf) | $F = ma$ recovered from added-mass drag; grid-converged in 2D and 3D |
| 2 | [Effective IR Correspondence between Linearized Gravity and Viscoelastic Superfluid Dynamics](uhf_physics/papers/paper2/Paper2_Effective_GR_Viscoelastic.pdf) | Term-by-term IR correspondence with linearized GR |
| 3 | [Acoustic Hawking Radiation in a Numerical Superfluid Analogue Model](uhf_physics/papers/paper3/Paper3_Hawking_Analogue.pdf) | Cross-horizon correlations with SNR = 4.71 in a superfluid analogue |

## Exploratory Extensions

Speculative letters quarantined in `uhf_physics/papers/`. These sketch possible applications of the framework but have not undergone the same level of numerical validation as the core stack.

| Paper | Topic |
|---|---|
| [Paper 4 (Cosmology)](uhf_physics/papers/paper4/Paper4_Superfluid_Cosmology.pdf) | Superfluid cosmology / dark-matter analogues |
| [Paper 5 (Standard Model)](uhf_physics/papers/paper5/Paper5_Topological_Standard_Model.pdf) | Topological Standard Model extension |
| [Paper 6 (QCD)](uhf_physics/papers/paper6/Paper6_Topological_Chromodynamics.pdf) | Color confinement as Borromean vortex reconnection |
| [Paper 7 (Entanglement)](uhf_physics/papers/paper7/Paper7_Quantum_Entanglement.pdf) | Bell correlations via global phase synchronisation |

## Legacy Manuscripts

The original monolithic manuscripts (v1–v10) that preceded the Core Stack restructuring are preserved at the repository root for blockchain-continuity purposes:

| Document | Polygon TX |
|---|---|
| [Part I — Physical Core](uhf_physics/UHF_Part_I_Core.md) | [v8.4](https://polygonscan.com/tx/0xc4f18abc225b84186b5b000e8c582ff3e0c55a17629b75f5d7c99d2b00c8c3af) |
| [Part II — Mathematical Foundations](uhf_physics/UHF_Part_II_Mathematical_Foundations.md) | [v8.4](https://polygonscan.com/tx/0xc2e7150c48a53d0e3ad1a21e16746c278758841ad08d3a09b46db283371ffefb) |
| [Part III — Standard Model Extension](uhf_physics/UHF_Part_III_Standard_Model.md) | [v8.4](https://polygonscan.com/tx/0xf6aafd64b27c0184b352a913e1f812cddb3a90398631c7456605799b816cc5fd) |
| [Defense Addendum](uhf_physics/UHF_Defense_Addendum.md) | [v8.5.1](https://polygonscan.com/tx/0x094e7e499ecae41a2655d423523e88a19ce3b057b266ededf7aba988d0f91fa6) |

All documents are SHA-256 hashed and registered on the **Polygon blockchain** (Contract [`0xe0bB4bC3116e19F2c0c183eFf8802C4F707B0054`](https://polygonscan.com/address/0xe0bB4bC3116e19F2c0c183eFf8802C4F707B0054)).

## Simulation Suite

The `uhf_physics/simulations/` directory contains data and `uhf_infrastructure/lbm-gpe/` contains Python code (GPU-optional) used to generate the numerical results cited in the papers:

- **LIGO GW150914** — matched-filter overlap study (mismatch 4.46 × 10⁻⁸)
- **NANOGrav 15-year PTA** — viscoelastic attenuation spectral fit (ΔAIC ≈ 37.69)
- **JWST early galaxies** — halo enhancement factor estimate at z = 10
- **Core-Cusp** — Bohm quantum pressure density profile
- **Muon g-2** — knot-topology anomalous moment estimate

Simulation suite SHA-256: `8f00520b...bc0d8b6`
Polygon TX (v8.5-Simulation): [0x22bf...1658](https://polygonscan.com/tx/0x22bf049c114f01ddab790df6d980f4bd9faf4eb145ef619bdc68f3bbfd861658)

## Landing Page

**[https://amiramitai.github.io/uhf](https://amiramitai.github.io/uhf)**
Source: `src/App.svelte` (Svelte 5 / Vite SPA)

---

## Peer Review Invitation

UHF makes specific, falsifiable predictions. Critique is welcome.

Please [open a GitHub Issue](https://github.com/amiramitai/uhf/issues/new) with one of:
- `objection` — a theoretical critique
- `proof-of-error` — a demonstrated failure
- `proposed-test` — a new experimental test

Cite the specific equation or section and reference the SHA-256 version so the record is unambiguous. Every substantive critique will receive a written response.

---

## Quick Start (Simulations)

```bash
git clone https://github.com/amiramitai/uhf.git
cd uhf/uhf_infrastructure/lbm-gpe
pip install numpy scipy matplotlib torch
python uhf_ligo_hunter.py
```

## Repository Structure

```
uhf_physics/                      — Foundational physics papers, monographs, simulations
  papers/paper1/                  — Core Paper 1: Emergent inertia (LBM)
  papers/paper2/                  — Core Paper 2: Effective GR correspondence
  papers/paper3/                  — Core Paper 3: Hawking analogue model
  papers/paper4–7/               — Exploratory extensions (cosmology, SM, QCD, entanglement)
  simulations/                    — Simulation data, results, verification outputs
  UHF_Part_I_Core.md              — Legacy monolithic manuscript (v1–v10)
  UHF_Part_II_Mathematical_Foundations.md
  UHF_Part_III_Standard_Model.md
  UHF_Defense_Addendum.md         — Empirical rebuttals to common objections
uhf_chemistry/                    — Placeholder: H₂ validation data forthcoming
uhf_biology/                      — Placeholder: biological analogue research
uhf_infrastructure/lbm-gpe/       — Python LBM/GPE simulation engines
deployment/scripts/               — Blockchain registration scripts
src/                              — Landing page (Svelte/Vite)
```

## On-Chain Log

All registration events are appended to [`deployments/scripts/on-chain-log.txt`](deployments/scripts/on-chain-log.txt).

---

© 2026 Amir Benjamin Amitay · All Rights Reserved · Immutably timestamped on Polygon
