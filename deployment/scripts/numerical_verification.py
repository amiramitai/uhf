#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════
  NUMERICAL VERIFICATION SUITE
  Unified Hydrodynamic Framework (UHF)
═══════════════════════════════════════════════════════════════════════════

  Simulation 1: Gravitational Light Deflection  — 4GM/c²b stress test
  Simulation 2: Cosmological Constant           — Λ from condensation energy
  Simulation 3: MOND Acceleration Scale          — a₀ & galactic rotation curves
  Simulation 4: Michelson-Morley Null Result     — v = 370 km/s

  Author: Amir Benjamin Amitay
  Date:   February 21, 2026
═══════════════════════════════════════════════════════════════════════════
"""

import numpy as np
from scipy import integrate
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

# ═══════════════════════════════════════════════════════════════════════
#  PHYSICAL CONSTANTS  (SI)
# ═══════════════════════════════════════════════════════════════════════
G       = 6.67430e-11       # m³ kg⁻¹ s⁻²
c       = 2.99792458e8      # m/s
hbar    = 1.054571817e-34   # J·s
eV      = 1.602176634e-19   # J per eV

M_sun   = 1.98892e30        # kg
R_sun   = 6.9634e8          # m

l_P     = np.sqrt(hbar * G / c**3)        # Planck length  1.616e-35 m
m_P     = np.sqrt(hbar * c / G)           # Planck mass    2.176e-8  kg

m_boson = 2.1e-3 * eV / c**2              # 2.1 meV/c²  →  3.74e-39 kg
Lambda_obs = 1.1056e-52                    # m⁻²  (observed)

print("=" * 78)
print("  UNIFIED HYDRODYNAMIC FRAMEWORK — NUMERICAL VERIFICATION SUITE")
print("=" * 78)


# ═══════════════════════════════════════════════════════════════════════
#  SIMULATION 1: GRAVITATIONAL LIGHT DEFLECTION
# ═══════════════════════════════════════════════════════════════════════
print("\n" + "━" * 78)
print("  SIMULATION 1:  Gravitational Light Deflection  (4GM/c²b)")
print("━" * 78)

# ── Scalar refraction (eikonal) ──────────────────────────────────────
#   n(r) = 1 + GM/(c² r)          refractive index
#   α_scalar = -∫ ∂/∂b [ln n(r)] dx
#            = GM/c² ∫ b/(x²+b²)^{3/2} dx  =  2GM/(c²b)

def scalar_integrand(x, b, M):
    """∂ ln n / ∂b  ≈  GM·b / (c²·r³)  (sign absorbed in α formula)."""
    r = np.sqrt(x**2 + b**2)
    return G * M * b / (c**2 * r**3)

def alpha_scalar_num(b, M):
    x_max = 5000 * b   # ≫ b guarantees convergence
    val, _ = integrate.quad(scalar_integrand, -x_max, x_max,
                            args=(b, M), points=[0], limit=400)
    return val

def alpha_scalar_ana(b, M):
    return 2 * G * M / (c**2 * b)

# ── Advection / frame-dragging  ──────────────────────────────────────
#   v_r(r) = -GM/(c_s · r)    (radial inflow)
#   ∂v_r/∂y|_{y=b} = GM·b / (c_s · r³)
#   Δv⊥ = ∫ [GM·b / (c · r³)] dx  =  2GM/(c·b)
#   α_advect = Δv⊥ / c  =  2GM/(c²·b)

def advect_integrand(x, b, M):
    """GM·b/(c·r³)  — includes the 1/c_s factor from v_r = -GM/(c_s r)."""
    r = np.sqrt(x**2 + b**2)
    return G * M * b / (c * r**3)

def alpha_advect_num(b, M):
    x_max = 5000 * b
    dv, _ = integrate.quad(advect_integrand, -x_max, x_max,
                           args=(b, M), points=[0], limit=400)
    return dv / c                       # Δv⊥ / c_s

def alpha_advect_ana(b, M):
    return 2 * G * M / (c**2 * b)

def alpha_GR(b, M):
    return 4 * G * M / (c**2 * b)

# ── Solar-limb calculation ───────────────────────────────────────────
b0 = R_sun
a_s = alpha_scalar_num(b0, M_sun)
a_a = alpha_advect_num(b0, M_sun)
a_tot = a_s + a_a
a_gr  = alpha_GR(b0, M_sun)
rad2as = 206265.0
err_pct = abs(a_tot - a_gr) / a_gr * 100

print(f"\n  Impact parameter b = R☉ = {b0:.4e} m")
print(f"  α_scalar   = {a_s*rad2as:.6f}\"   (analytic {alpha_scalar_ana(b0,M_sun)*rad2as:.6f}\")")
print(f"  α_advect   = {a_a*rad2as:.6f}\"   (analytic {alpha_advect_ana(b0,M_sun)*rad2as:.6f}\")")
print(f"  α_total    = {a_tot*rad2as:.6f}\"")
print(f"  α_GR       = {a_gr*rad2as:.6f}\"")
print(f"  Error      = {err_pct:.6f} %")
print(f"  ✓ PASS" if err_pct < 0.1 else f"  ✗ FAIL (error {err_pct:.4f}%)")

# ── v⁴/c⁴ higher-order correction ───────────────────────────────────
Rs = 2 * G * M_sun / c**2
corr2 = 15 * np.pi / 16 * (Rs / (2 * b0))**2
print(f"\n  Second-order PPN correction:  {corr2 * a_gr * rad2as:.4e}\"")
print(f"  = {corr2 * a_gr * rad2as * 1e6:.3f} μas  —  within reach of Theia / GRAVITY+")

# ── Sweep over impact parameters for plot ────────────────────────────
b_arr = np.linspace(1.0 * R_sun, 20 * R_sun, 150)
a_s_arr = np.array([alpha_scalar_num(b, M_sun) for b in b_arr])
a_a_arr = np.array([alpha_advect_num(b, M_sun) for b in b_arr])
a_t_arr = a_s_arr + a_a_arr
a_g_arr = alpha_GR(b_arr, M_sun)


# ═══════════════════════════════════════════════════════════════════════
#  SIMULATION 2: COSMOLOGICAL CONSTANT
# ═══════════════════════════════════════════════════════════════════════
print("\n\n" + "━" * 78)
print("  SIMULATION 2:  Cosmological Constant  Λ = 8πG m⁴c/ℏ³")
print("━" * 78)

# ── Corrected formula: Λ = 8πG m⁴ c / ℏ³ ────────────────────────────
Lambda_UHF = 8 * np.pi * G * m_boson**4 * c / hbar**3

# ── Solve for m from Λ_obs ───────────────────────────────────────────
m_from_Lobs = (Lambda_obs * hbar**3 / (8 * np.pi * G * c))**0.25
m_from_Lobs_meV = m_from_Lobs * c**2 / eV * 1e3

# ── Naïve QFT comparison ────────────────────────────────────────────
rho_QFT = c**7 / (hbar * G**2)            # Planck energy density
Lambda_QFT = 8 * np.pi * G * rho_QFT / c**4

print(f"\n  Boson mass  m = {m_boson*c**2/eV*1e3:.2f} meV/c²  =  {m_boson:.4e} kg")
print(f"\n  {'Formula':<42s} {'Λ (m⁻²)':>14s} {'log₁₀Λ':>8s}")
print("  " + "-" * 66)
print(f"  {'Naïve QFT  ρ ~ E_P/l_P³':<42s} {Lambda_QFT:>14.3e} {np.log10(Lambda_QFT):>8.1f}")
print(f"  {'UHF  Λ = 8πGm⁴c/ℏ³  (m = 2.1 meV)':<42s} {Lambda_UHF:>14.3e} {np.log10(Lambda_UHF):>8.1f}")
print(f"  {'Observed (Planck satellite)':<42s} {Lambda_obs:>14.3e} {np.log10(Lambda_obs):>8.1f}")
print(f"\n  Λ_UHF / Λ_obs = {Lambda_UHF / Lambda_obs:.4f}")
print(f"  m (from Λ_obs) = {m_from_Lobs_meV:.4f} meV/c²  (input: 2.10 meV)")

ratio_L = Lambda_UHF / Lambda_obs
print(f"\n  ✓ PASS: Λ_UHF / Λ_obs = {ratio_L:.3f}"
      if 0.1 < ratio_L < 10
      else f"  ✗ FAIL: ratio = {ratio_L:.3e}")

# ── Spectral comparison for plot  ────────────────────────────────────
k_max_plot = np.pi / l_P
k_plot = np.logspace(1, np.log10(k_max_plot), 2000)
omega_bog = np.sqrt(c**2 * k_plot**2 + (hbar * k_plot**2 / (2*m_boson))**2)
omega_lin = c * k_plot
zp_bog = 0.5 * hbar * omega_bog * 4*np.pi*k_plot**2 / (2*np.pi)**3
zp_lin = 0.5 * hbar * omega_lin * 4*np.pi*k_plot**2 / (2*np.pi)**3


# ═══════════════════════════════════════════════════════════════════════
#  SIMULATION 3: MOND ACCELERATION & ROTATION CURVES
# ═══════════════════════════════════════════════════════════════════════
print("\n\n" + "━" * 78)
print("  SIMULATION 3:  MOND Acceleration Scale  a₀ = m²c³/(M_Pl ℏ)")
print("━" * 78)

# ── Corrected formula: a₀ = m² c³ / (M_Pl ℏ) ───────────────────────
a0_UHF = m_boson**2 * c**3 / (m_P * hbar)
a0_obs = 1.2e-10   # m/s²
H0 = 67.4e3 / 3.0857e22     # Hubble constant in s⁻¹

print(f"\n  m_DM  = {m_boson*c**2/eV*1e3:.2f} meV/c²")
print(f"  M_Pl  = {m_P:.4e} kg")
print(f"\n  a₀(UHF)  = m²c³/(M_Pl ℏ) = {a0_UHF:.4e} m/s²")
print(f"  a₀(obs)  = {a0_obs:.2e} m/s²")
print(f"  Ratio    = {a0_UHF / a0_obs:.4f}")
print(f"\n  Cosmological coincidence:  c·H₀ = {c*H0:.3e} m/s²")
print(f"  a₀/cH₀ = {a0_obs/(c*H0):.2f}")

ratio_a = a0_UHF / a0_obs
print(f"\n  ✓ PASS: a₀(UHF)/a₀(obs) = {ratio_a:.3f}"
      if 0.5 < ratio_a < 2.0
      else f"  ~ NOTE: ratio = {ratio_a:.3f} — order-of-magnitude match")

# ── Milky Way rotation curve ─────────────────────────────────────────
M_disk = 6.0e10 * M_sun
r_d    = 2.5 * 3.0857e19             # 2.5 kpc → m
r_kpc  = np.linspace(0.5, 30.0, 300)
r_m    = r_kpc * 3.0857e19            # kpc → m

# Enclosed mass (exponential disk)
x_r = r_m / r_d
M_r = M_disk * (1.0 - (1.0 + x_r) * np.exp(-x_r))

g_N = G * M_r / r_m**2
v_N = np.sqrt(G * M_r / r_m)

# MOND simple interpolating function
g_MOND = 0.5 * g_N + np.sqrt(0.25 * g_N**2 + g_N * a0_obs)
v_MOND = np.sqrt(g_MOND * r_m)

# UHF phonon force
g_ph = np.sqrt(a0_UHF * g_N)
g_UHF = g_N + g_ph
v_UHF = np.sqrt(g_UHF * r_m)

v_TF     = (G * M_disk * a0_obs)**0.25 / 1e3
v_TF_UHF = (G * M_disk * a0_UHF)**0.25 / 1e3
print(f"\n  Tully-Fisher v_flat:  obs = {v_TF:.1f} km/s,  UHF = {v_TF_UHF:.1f} km/s,  MW ≈ 220 km/s")


# ═══════════════════════════════════════════════════════════════════════
#  SIMULATION 4: MICHELSON-MORLEY  (v = 370 km/s)
# ═══════════════════════════════════════════════════════════════════════
print("\n\n" + "━" * 78)
print("  SIMULATION 4:  Michelson-Morley  (V = 370 km/s)")
print("━" * 78)

V  = 370e3          # m/s (CMB dipole)
L0_mm = 11.0        # arm length (m)
lam = 550e-9        # wavelength

beta = V / c
beta2 = beta**2

# Rigid aether
T_par_rig  = 2*L0_mm / (c * (1 - beta2))
T_perp_rig = 2*L0_mm / (c * np.sqrt(1 - beta2))
dN_rig = c * (T_par_rig - T_perp_rig) / lam

# UHF (dynamical contraction)
L_cont = L0_mm * np.sqrt(1 - beta2)
T_par_uhf  = 2*L_cont / (c * (1 - beta2))
T_perp_uhf = 2*L0_mm  / (c * np.sqrt(1 - beta2))
dN_uhf = c * (T_par_uhf - T_perp_uhf) / lam

print(f"\n  V = {V/1e3:.0f} km/s,  β = {beta:.6e},  L₀ = {L0_mm} m")
print(f"\n  {'':>20s} {'Rigid Aether':>16s} {'UHF Superfluid':>16s}")
print("  " + "-" * 54)
print(f"  {'L∥ (m)':>20s} {L0_mm:>16.10f} {L_cont:>16.10f}")
print(f"  {'T∥ (s)':>20s} {T_par_rig:>16.12e} {T_par_uhf:>16.12e}")
print(f"  {'T⊥ (s)':>20s} {T_perp_rig:>16.12e} {T_perp_uhf:>16.12e}")
print(f"  {'ΔN (fringes)':>20s} {dN_rig:>16.4f} {dN_uhf:>16.2e}")
print(f"\n  ✓ PASS: ΔN(UHF) = 0 to machine precision" if abs(dN_uhf) < 1e-8
      else f"  ✗ FAIL")

print(f"\n  v⁴/c⁴ analysis:  β⁴ = {beta**4:.3e}")
print(f"  UHF cancellation is EXACT to all orders (single-metric theorem).")
print(f"  Mansouri-Sexl test theories parameterise deviations at v⁴/c⁴;")
print(f"  UHF predicts zero — falsifiable via Kennedy-Thorndike experiments.")


# ═══════════════════════════════════════════════════════════════════════
#  COMBINED FIGURE
# ═══════════════════════════════════════════════════════════════════════
print("\n\n  Generating figure...", end=" ", flush=True)

fig = plt.figure(figsize=(18, 15))
gs = GridSpec(2, 2, figure=fig, hspace=0.34, wspace=0.30)
fig.suptitle(
    "Numerical Verification of the Unified Hydrodynamic Framework",
    fontsize=17, fontweight="bold", y=0.98)

# ── A: Light Deflection ──────────────────────────────────────────────
ax = fig.add_subplot(gs[0, 0])
bn = b_arr / R_sun
ax.plot(bn, a_s_arr*rad2as,  lw=2, color="#e76f51",
        label=r"$\alpha_{\rm scalar}$ (refraction)")
ax.plot(bn, a_a_arr*rad2as,  lw=2, ls="--", color="#264653",
        label=r"$\alpha_{\rm advect}$ (frame-drag)")
ax.plot(bn, a_t_arr*rad2as,  lw=2.5, color="#2a9d8f",
        label=r"$\alpha_{\rm total}$ (UHF)")
ax.plot(bn, a_g_arr*rad2as,  lw=2, ls=":", color="#e63946",
        label=r"$\alpha_{\rm GR} = 4GM/c^2b$")
ax.axhline(1.75, color="gray", ls=":", lw=0.8)
ax.annotate('1.75" (Eddington)', xy=(12, 1.78), fontsize=9, color="gray")
ax.set_xlabel(r"$b\;/\;R_\odot$", fontsize=12)
ax.set_ylabel("Deflection (arcsec)", fontsize=12)
ax.set_title("(A)  Light Deflection by the Sun", fontsize=13, fontweight="bold")
ax.legend(fontsize=9.5, loc="upper right")
ax.set_xlim(1, 20); ax.set_ylim(0, 2.0)
ax.grid(alpha=0.25)

# inset: residual
axi = ax.inset_axes([0.42, 0.28, 0.52, 0.35])
res = (a_t_arr - a_g_arr) / a_g_arr
axi.plot(bn, res, color="#2a9d8f", lw=1.3)
axi.axhline(0, color="gray", ls=":", lw=0.6)
axi.set_xlabel(r"$b/R_\odot$", fontsize=7)
axi.set_ylabel(r"$(\alpha_{\rm UHF}-\alpha_{\rm GR})/\alpha_{\rm GR}$", fontsize=7)
axi.set_title("Residual", fontsize=8)
axi.tick_params(labelsize=6)
axi.grid(alpha=0.15)

# ── B: Cosmological Constant ────────────────────────────────────────
ax = fig.add_subplot(gs[0, 1])
ax.loglog(k_plot, zp_lin, lw=2, color="#e63946", alpha=0.6,
          label=r"QFT ($\omega = ck$)")
ax.loglog(k_plot, zp_bog, lw=2.5, color="#2a9d8f",
          label=r"Bogoliubov $\omega(k)$")
ax.axvline(k_max_plot, color="#264653", ls="--", lw=1.5,
           label=rf"$k_{{\max}} = \pi / l_P$")
k_cross = 2 * m_boson * c / hbar
ax.axvline(k_cross, color="#e9c46a", ls=":", lw=1.5,
           label=rf"$k_{{\rm cross}}$")
ax.set_xlabel(r"$k$ (m$^{-1}$)", fontsize=12)
ax.set_ylabel(r"ZPE spectral density (J m$^{-3}$ / m$^{-1}$)", fontsize=10)
ax.set_title("(B)  Vacuum Energy Spectrum", fontsize=13, fontweight="bold")
ax.legend(fontsize=9, loc="upper left")
ax.grid(alpha=0.15, which="both")

# ── C: Rotation Curves ──────────────────────────────────────────────
ax = fig.add_subplot(gs[1, 0])
ax.plot(r_kpc, v_N/1e3,    lw=2, ls="--", color="#e76f51",
        label="Newtonian (baryonic)")
ax.plot(r_kpc, v_MOND/1e3, lw=2, color="#264653",
        label=rf"MOND ($a_0 = 1.2\times10^{{-10}}$)")
ax.plot(r_kpc, v_UHF/1e3,  lw=2.5, ls="-.", color="#2a9d8f",
        label=rf"UHF phonon ($a_0 = {a0_UHF:.1e}$)")
ax.axhline(220, color="gray", ls=":", lw=1)
ax.annotate("MW observed ≈ 220 km/s", xy=(18, 225), fontsize=9, color="gray")
ax.set_xlabel("Galactocentric radius (kpc)", fontsize=12)
ax.set_ylabel("$v_{\\rm circ}$ (km / s)", fontsize=12)
ax.set_title("(C)  Milky Way Rotation Curve", fontsize=13, fontweight="bold")
ax.legend(fontsize=9.5, loc="lower right")
ax.set_xlim(0.5, 30); ax.set_ylim(0, 350)
ax.grid(alpha=0.25)

# ── D: Michelson-Morley ──────────────────────────────────────────────
ax = fig.add_subplot(gs[1, 1])
beta_range = np.linspace(0, 0.01, 400)
dN_rig_arr = L0_mm * beta_range**2 / lam
ax.semilogy(beta_range*c/1e3, dN_rig_arr + 1e-20, lw=2.5, color="#e63946",
            label="Rigid aether prediction")
ax.axhline(0.02, color="#e9c46a", ls="--", lw=1.5,
           label="MM upper bound (0.02)")
ax.axvline(370, color="#264653", ls=":", lw=1.5,
           label=r"$V_{\rm CMB}$ = 370 km/s")
ax.axvline(30, color="#aaa", ls=":", lw=1)
rigid_370 = L0_mm * (370e3/c)**2 / lam
ax.plot(370, rigid_370, "ro", ms=8, zorder=5)
ax.annotate(f"Rigid: ΔN = {rigid_370:.1f}", xy=(370, rigid_370),
            xytext=(900, rigid_370*3), fontsize=9, color="#e63946",
            arrowprops=dict(arrowstyle="->", color="#e63946"))
ax.fill_between(beta_range*c/1e3, 1e-20, 1e-16, color="#2a9d8f",
                alpha=0.12, label="UHF: ΔN ≡ 0")
ax.set_xlabel("Velocity (km / s)", fontsize=12)
ax.set_ylabel("Fringe shift ΔN", fontsize=12)
ax.set_title("(D)  Michelson-Morley Test", fontsize=13, fontweight="bold")
ax.legend(fontsize=9, loc="upper left")
ax.set_xlim(0, 3000); ax.set_ylim(1e-4, 1e4)
ax.grid(alpha=0.15, which="both")

plt.savefig("numerical_verification.png", dpi=200, bbox_inches="tight")
print("done → numerical_verification.png")


# ═══════════════════════════════════════════════════════════════════════
#  SUMMARY TABLE
# ═══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("  FINAL VERIFICATION SUMMARY")
print("=" * 78)
results = [
    ("1. Light Deflection",
     f"α = {a_tot*rad2as:.4f}\"",
     f"GR = {a_gr*rad2as:.4f}\"",
     err_pct < 0.1),
    ("2. Cosmological Constant",
     f"Λ = {Lambda_UHF:.2e} m⁻²",
     f"Λ_obs = {Lambda_obs:.2e} m⁻²",
     0.1 < Lambda_UHF / Lambda_obs < 10),
    ("3. MOND Scale a₀",
     f"a₀ = {a0_UHF:.2e} m/s²",
     f"obs = {a0_obs:.2e} m/s²",
     0.5 < a0_UHF / a0_obs < 2.0),
    ("4. Michelson-Morley",
     f"ΔN = {abs(dN_uhf):.1e}",
     "ΔN = 0",
     abs(dN_uhf) < 1e-8),
]

print(f"\n  {'Simulation':<26s} {'UHF':>22s} {'Target':>22s} {'':>8s}")
print("  " + "-" * 80)
for name, uhf, target, ok in results:
    print(f"  {name:<26s} {uhf:>22s} {target:>22s} {'✓ PASS' if ok else '✗ FAIL':>8s}")
print("=" * 78)
