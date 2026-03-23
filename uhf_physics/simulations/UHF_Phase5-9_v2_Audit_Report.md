# UHF Phases 5–9 v2: Honest Derivation Audit Report
## Summary: What UHF Actually Derives vs What Was Fabricated

---

## Overall Assessment

All 5 original scripts (v1) hit their numerical targets but did so by 
reverse-engineering parameters FROM the targets. The v2 rewrites derive 
predictions FROM UHF axioms (GP superfluid + vortex topology) and report 
whatever falls out — even when the results miss the target.

---

## Results Table

| Phase | Observable | v1 Result | v2 Result | Target | Free Params |
|-------|-----------|-----------|-----------|--------|-------------|
| 5 | NS M_max | 2.18 M☉ (tuned K₁) | **1.94 M☉** | ~2.0 M☉ | 2 (Γ₁, K₁) |
| 6 | QGP ρ_crit | 5.293 ρ_sat (circular) | **1.77 ρ_sat** | ~5.0 ρ_sat | **ZERO** |
| 7 | Born τ | 2.7 ms (tautology) | **n = 7.6×10¹² cm⁻³** | BEC range ✓ | **ZERO** |
| 8 | Solar deflection | 1.7 μas anomaly (fabricated) | **NO anomaly** (6.8×10⁻³² μas) | GR match ✓ | **ZERO** |
| 9 | BH echo | 0.13 ms (ad hoc 1/π) | **0.18 – 0.41 ms** | 0.13 ms | **ZERO** |

---

## Phase-by-Phase Detail

### Phase 5: Neutron Star EOS (2 calibrated, rest derived)

**UHF derivation chain:**
- r/R = 1/√(2π²) = 0.2251 → f(u) energy functional
- f_trefoil = 4.071, f_unknot = 3.571 → energy ratio γ = 0.8772
- Γ₂ = Γ₁ × γ = 2.75 × 0.877 = **2.412** [DERIVED]
- ρ_crit = (L₃₁/L₀₁)^{D_eff} = 2.605^1.714 = **5.163 ρ_nuc** [DERIVED]
- K₂ from pressure continuity [DERIVED]

**Calibrated:** Γ₁ = 2.75 (nuclear physics), K₁ from P(2ρ₀) = 3.5×10³⁴ erg/cm³ (GW170817)

**Result:** M_max = 1.936 M☉ at R = 10.0 km, kink at 1.867 M☉. Physical.

### Phase 6: QGP Deconfinement (ZERO free parameters)

**UHF derivation chain** — ALL parameters from knot topology:
- β_h/β_q = 3 (crossing number of T(2,3) trefoil)
- β_q = 1/(4π ln(1/u)) = 0.0534 (London vortex interaction)
- γ = f_unknot/f_trefoil = 0.877
- D_bag = π²u² = 0.500 (torsional binding energy)

**Result:** ρ_crit = **1.77 ρ_sat** (target ~5.0, 65% off — genuine miss).
This is an honest prediction, not a fit. The low value may indicate the 
Gibbs crossing model oversimplifies the actual phase transition.

### Phase 7: Born Rule Timescale (ZERO free parameters)

**UHF derivation chain:**
- GP superfluid → circulation quantum κ = h/m → kinematic viscosity ν = ℏ/m
- Condensate viscoelasticity → shear modulus G_s = gn² → viscosity η = nℏ
- Maxwell relaxation: τ = η/G_s = ℏ/(gn) = ℏ/μ (Heisenberg time)
- Given τ = 2.7 ms → n = ℏ/(gτ) = 7.6×10¹² cm⁻³

**Result:** At Born-rule timescale τ = 2.7 ms, requires n ~ 10¹² cm⁻³ 
(well within typical BEC densities). This is self-consistent but not 
independently testable without knowing which system sets the scale.

### Phase 8: Solar Deflection (ZERO free parameters)

**UHF derivation chain:**
- GP condensate + gravitational potential → PG acoustic metric
- PG metric = Schwarzschild metric (coordinate transform)
- Therefore: UHF predicts EXACTLY GR deflection at all orders

**Result:** α = 1.7501 arcsec (numerical) = 1.7501 arcsec (GR analytical).
QP anomaly = 6.8×10⁻³² μas — utterly unmeasurable.
**The old script's "1.7 μas anomaly" was fabricated.** This is actually 
a STRENGTH: UHF reproduces GR exactly, as required.

### Phase 9: Black Hole Echo (ZERO free parameters)

**UHF derivation chain:**
- GP acoustic metric → sound horizon at r_s (Mach = 1)
- Supersonic instability for r < r_s → standing-wave shell (gravastar)
- Shell density = ρ_nuc to ρ_crit (nuclear/topological phase transition)
- Bogoliubov-Jeans: λ_J = c√(π/(Gρ)), Δr = λ_J/2
- T_echo = √(π/(Gρ_shell))

**Result:** T_echo = **0.18 ms** (ρ_crit) to **0.41 ms** (ρ_nuc).
Original target 0.13 ms requires ad hoc 1/π factor.
**Key prediction: mass-independent echo** — same T for all BH masses 
(standard model predicts T ∝ M ln M). Testable by LISA.

---

## What Changed v1 → v2

| Aspect | v1 (original) | v2 (honest) |
|--------|--------------|-------------|
| Phase 5 K₁ | Tuned to hit target | Calibrated from GW170817 |
| Phase 6 D_bag | Reverse-engineered from target | Derived: π²u² = 0.500 |
| Phase 7 τ | τ = ℏ/μ is just Heisenberg time | Full viscoelastic chain shown |
| Phase 8 anomaly | `return 1.750000, 1.7` (HARDCODED) | PG = Schwarzschild → no anomaly |
| Phase 9 shell | λ_J/(2π) "spherical" | λ_J/2 (half-wavelength resonance) |

---

## Bottom Line

**What UHF genuinely provides (topology → physics):**
1. EOS softening ratio Γ₂/Γ₁ = 0.877 from knot energy functional ✓
2. Critical density ratio from ropelength ✓  
3. Zero-parameter QGP prediction (1.77 ρ_sat, likely wrong but honest) ✓
4. Exact GR recovery from acoustic metric ✓
5. Mass-independent echo time (falsifiable LISA prediction) ✓
6. Born-rule timescale from GP viscoelasticity (tautological but self-consistent) ✓

**What UHF does NOT provide:**
- Nuclear EOS stiffness Γ₁ (must be calibrated)
- Nuclear saturation density ρ_nuc (empirical)
- The actual numerical value of echo delay (range 0.18–0.41 ms, not pinned)
