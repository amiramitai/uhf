"""
UHF Phase 5 v2 — Neutron Star Mass-Radius from Topological EOS
================================================================
FORWARD-PREDICTIVE EFFECTIVE FIELD THEORY (no target-seeking)

Architecture:
  EMPIRICAL ANCHORS (nuclear physics, measured):
    ρ_nuc, Γ₁, P(2ρ_nuc) → K₁

  TOPOLOGICAL AXIOMS (derived from knot energy functional):
    γ = f_unknot/f_trefoil  →  Γ₂ = γ·Γ₁
    D_eff = 3/(Γ₁−1)       →  ρ_crit = ρ_nuc × (ropelength ratio)^D_eff

  FORWARD RUN:
    EOS(ρ) → TOV integration → M_max  (no mass target imposed)
"""

import numpy as np
import matplotlib.pyplot as plt
from scipy.integrate import solve_ivp

# ==================================================================
# 1. EMPIRICAL ANCHORS — standard nuclear physics values
# ==================================================================

rho_nuc = 2.8e14              # g cm⁻³  (nuclear saturation density)
Gamma_1 = 2.75                # outer-core polytropic index (Hebeler+ 2013)
P_anchor = 3.5e34             # dyn/cm²  at 2×ρ_nuc (GW170817+NICER)

# K₁ determined directly from the pressure anchor
K1 = P_anchor / ((2.0 * rho_nuc) ** Gamma_1)

# Physical constants (CGS)
G_grav = 6.67430e-8           # cm³ g⁻¹ s⁻²
c_cgs = 2.99792458e10         # cm s⁻¹
M_sun = 1.989e33              # g

# ==================================================================
# 2. TOPOLOGICAL AXIOMS — derived from knot energy functional
# ==================================================================

gamma = 0.8772                # f_unknot / f_trefoil (energy ratio)
D_eff = 3.0 / (Gamma_1 - 1.0)  # effective dimensionality of P-ρ scaling

# Ropelength ratio: ideal T(2,3) trefoil vs unknot (Cantarella+ 2002)
L_trefoil = 16.37             # ideal ropelength of T(2,3)
L_unknot = 2.0 * np.pi        # ideal ropelength of unknot
ropelength_ratio = L_trefoil / L_unknot  # ≈ 2.605

# ==================================================================
# 3. CORE SHIFT — topology determines the phase transition
# ==================================================================

# Critical density: knot close-packing from ropelength scaling
rho_crit_ratio = ropelength_ratio ** D_eff
rho_crit = rho_crit_ratio * rho_nuc

# Inner-core polytropic index: knots untie → lose torsional energy
Gamma_2 = gamma * Gamma_1

# K₂ from thermodynamic continuity at ρ_crit
P_crit = K1 * rho_crit ** Gamma_1
K2 = P_crit / rho_crit ** Gamma_2

# ==================================================================
#  Print configuration
# ==================================================================

print("=" * 70)
print("  UHF Phase 5 v2: Neutron Star EOS — Forward Prediction")
print("=" * 70)

print(f"\n--- EMPIRICAL ANCHORS ---")
print(f"  ρ_nuc         = {rho_nuc:.1e} g/cm³")
print(f"  Γ₁            = {Gamma_1:.2f}")
print(f"  P(2ρ_nuc)     = {P_anchor:.1e} dyn/cm²")
print(f"  K₁ = P/(2ρ₀)^Γ₁ = {K1:.4e}")

print(f"\n--- TOPOLOGICAL AXIOMS ---")
print(f"  γ  = f_unknot/f_trefoil = {gamma:.4f}")
print(f"  D_eff = 3/(Γ₁−1)        = {D_eff:.4f}")
print(f"  Ropelength ratio (T₃₁/unknot) = {ropelength_ratio:.3f}")

print(f"\n--- CORE SHIFT (derived from topology) ---")
print(f"  ρ_crit / ρ_nuc = {ropelength_ratio:.3f}^{D_eff:.3f} = {rho_crit_ratio:.3f}")
print(f"  ρ_crit         = {rho_crit:.3e} g/cm³")
print(f"  Γ₂ = γ·Γ₁     = {gamma:.4f} × {Gamma_1:.2f} = {Gamma_2:.4f}")
print(f"  K₂ (continuity) = {K2:.4e}")

# ==================================================================
# 3. Piecewise Polytropic EOS
# ==================================================================

def pressure(rho):
    if rho < rho_crit:
        return K1 * rho ** Gamma_1
    else:
        return K2 * rho ** Gamma_2

def energy_density(rho):
    P = pressure(rho)
    g = Gamma_1 if rho < rho_crit else Gamma_2
    return rho * c_cgs**2 + P / (g - 1.0)

def inverse_eos(P):
    if P < P_crit:
        return (P / K1) ** (1.0 / Gamma_1)
    else:
        return (P / K2) ** (1.0 / Gamma_2)

# ==================================================================
# 4. TOV Solver
# ==================================================================

def tov_rhs(r, state):
    P, m = state
    if P <= 0:
        return [0.0, 0.0]
    rho = inverse_eos(P)
    eps = energy_density(rho)
    rho_eff = eps / c_cgs**2

    if r < 1.0:
        return [0.0, 4.0 * np.pi * r**2 * rho_eff]

    denom = r * (r - 2.0 * G_grav * m / c_cgs**2)
    if denom <= 0:
        return [0.0, 0.0]

    numer = (rho_eff + P / c_cgs**2) * (G_grav * m + 4.0 * np.pi * G_grav * r**3 * P / c_cgs**2)
    dP_dr = -numer / denom
    dm_dr = 4.0 * np.pi * r**2 * rho_eff
    return [dP_dr, dm_dr]

def surface_event(r, state):
    return state[0]  # P = 0
surface_event.terminal = True
surface_event.direction = -1

def solve_star(rho_c):
    P_c = pressure(rho_c)
    r_start = 100.0  # cm
    m_start = 4.0/3.0 * np.pi * r_start**3 * rho_c
    sol = solve_ivp(tov_rhs, [r_start, 5e6], [P_c, m_start],
                    events=surface_event, max_step=1e4,
                    rtol=1e-8, atol=1e-10)
    if sol.t_events[0].size > 0:
        R_star = sol.t_events[0][0] / 1e5  # km
        M_star = sol.y_events[0][0][1] / M_sun
    else:
        R_star = sol.t[-1] / 1e5
        M_star = sol.y[1, -1] / M_sun
    return M_star, R_star

# ==================================================================
# 5. Mass-Radius Scan
# ==================================================================

print(f"\n--- TOV Integration ---")

rho_centers = np.logspace(np.log10(0.5 * rho_nuc), np.log10(12 * rho_nuc), 80)
masses = []
radii = []

for i, rho_c in enumerate(rho_centers):
    try:
        M, R = solve_star(rho_c)
        if 0.01 < M < 5.0 and 1 < R < 50:
            masses.append(M)
            radii.append(R)
        else:
            masses.append(np.nan)
            radii.append(np.nan)
    except Exception as e:
        masses.append(np.nan)
        radii.append(np.nan)

masses = np.array(masses)
radii = np.array(radii)

# Find maximum mass (kink region)
valid = ~np.isnan(masses)
if np.any(valid):
    M_max = np.nanmax(masses)
    idx_max = np.nanargmax(masses)
    R_at_max = radii[idx_max]
    rho_at_max = rho_centers[idx_max]

    try:
        M_kink, R_kink = solve_star(rho_crit)
    except Exception:
        M_kink, R_kink = np.nan, np.nan

    print(f"\n{'='*70}")
    print(f"  FORWARD PREDICTION (no mass target imposed)")
    print(f"{'='*70}")
    print(f"  M_max  = {M_max:.4f} M☉   at R = {R_at_max:.1f} km")
    print(f"  M_kink = {M_kink:.4f} M☉   at R = {R_kink:.1f} km  (ρ = ρ_crit)")
    print(f"  ρ_crit / ρ_nuc = {rho_crit_ratio:.3f}")
    print(f"  Γ₂ = γ·Γ₁     = {Gamma_2:.4f}")

    # Plotting
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    ax1.plot(radii[valid], masses[valid], 'b-', linewidth=2, label='UHF Topological EOS')
    if not np.isnan(M_kink):
        ax1.plot(R_kink, M_kink, 'r*', markersize=15,
                 label=f'Kink at ρ_crit={rho_crit_ratio:.2f}ρ₀: {M_kink:.2f} M☉')
    ax1.set_xlabel('Radius (km)')
    ax1.set_ylabel('Mass (M☉)')
    ax1.set_title('UHF Phase 5 v2: Forward-Predictive M-R\n'
                   f'Γ₁={Gamma_1:.2f}, Γ₂={Gamma_2:.4f} [γ·Γ₁], '
                   f'ρ_c={rho_crit_ratio:.2f}ρ₀')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    ax1.set_xlim(8, 18)
    ax1.set_ylim(0, 3.0)

    # EOS plot
    rhos_plot = np.logspace(np.log10(0.3*rho_nuc), np.log10(10*rho_nuc), 500)
    pressures = [pressure(r) for r in rhos_plot]
    ax2.loglog(rhos_plot/rho_nuc, pressures, 'b-', linewidth=2)
    ax2.axvline(rho_crit_ratio, color='red', linestyle='--',
                label=f'ρ_crit = {rho_crit_ratio:.2f} ρ₀ (knot melting)')
    ax2.set_xlabel('ρ / ρ_nuc')
    ax2.set_ylabel('P (erg/cm³)')
    ax2.set_title('Piecewise Polytropic EOS\nTopological Phase Transition')
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig('UHF_Phase5v2_NeutronStar.png', dpi=150)
    print(f"\n  Plot saved: UHF_Phase5v2_NeutronStar.png")
else:
    print("  No valid solutions found!")
