#!/usr/bin/env python3
"""
Michelson-Morley Simulation in a Superfluid Vacuum
====================================================

This script demonstrates numerically that a Michelson-Morley interferometer
embedded in a flowing superfluid produces a null fringe-shift, provided
the interferometer arms undergo dynamical Lorentz-FitzGerald contraction
(as predicted by the Unified Hydrodynamic Framework).

Two scenarios are contrasted:
  A) A rigid Galilean aether — arms do NOT contract → fringe shift appears.
  B) A dynamical superfluid  — arms contract as L‖ = L₀√(1−β²) → null result.

A third plot shows the light-ray trajectories in both frames.

Author: Amir Benjamin Amitay
Date:   February 21, 2026
"""

import numpy as np
import matplotlib
matplotlib.use("Agg")                       # headless backend for server use
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

# ── Physical Parameters ──────────────────────────────────────────────
c_s   = 1.0              # speed of sound in the superfluid  (normalised)
L0    = 1.0              # proper arm length                 (normalised)
betas = np.linspace(0, 0.95, 500)   # V/c_s ratio range

# ── 1. Round-Trip Times ──────────────────────────────────────────────
def rigid_aether_times(beta):
    """Galilean aether: arms do not contract."""
    T_par  = 2 * L0 / (c_s * (1 - beta**2))           # parallel arm
    T_perp = 2 * L0 / (c_s * np.sqrt(1 - beta**2))    # perpendicular arm
    return T_par, T_perp

def superfluid_times(beta):
    """UHF superfluid: parallel arm contracts by √(1−β²)."""
    gamma = 1.0 / np.sqrt(1 - beta**2)
    L_par  = L0 * np.sqrt(1 - beta**2)                 # contracted length
    T_par  = 2 * L_par / (c_s * (1 - beta**2))         # = 2L₀/(c_s √(1-β²))
    T_perp = 2 * L0    / (c_s * np.sqrt(1 - beta**2))  # unchanged geometry
    return T_par, T_perp

# ── 2. Fringe Shift  ΔN = c_s (T‖ − T⊥) / λ ─────────────────────────
wavelength = 500e-9 / L0   # normalised to arm length (≈ visible light)

def fringe_shift(T_par, T_perp):
    """Phase difference in units of fringe order ΔN = f·ΔT = c/λ · ΔT."""
    return c_s * (T_par - T_perp) / wavelength

# ── Compute ──────────────────────────────────────────────────────────
T_rig_par, T_rig_perp = rigid_aether_times(betas)
T_sf_par,  T_sf_perp  = superfluid_times(betas)

dN_rigid = fringe_shift(T_rig_par, T_rig_perp)
dN_super = fringe_shift(T_sf_par,  T_sf_perp)

# ── 3. Plotting ──────────────────────────────────────────────────────
fig, axes = plt.subplots(2, 2, figsize=(14, 11))
fig.suptitle(
    "Michelson-Morley Experiment:\nRigid Aether  vs.  Dynamical Superfluid Vacuum",
    fontsize=16, fontweight="bold", y=0.98
)

# ── Panel A: Round-trip times (rigid aether) ─────────────────────────
ax = axes[0, 0]
ax.plot(betas, T_rig_par,  label=r"$T_\parallel$ (rigid)", lw=2.2, color="#e63946")
ax.plot(betas, T_rig_perp, label=r"$T_\perp$ (rigid)",     lw=2.2, color="#457b9d", ls="--")
ax.set_xlabel(r"$\beta = V / c_s$", fontsize=13)
ax.set_ylabel(r"Round-trip time  $T / (2L_0/c_s)$", fontsize=13)
ax.set_title("(A)  Rigid Galilean Aether", fontsize=13)
ax.legend(fontsize=12)
ax.set_xlim(0, 0.95)
ax.grid(alpha=0.3)

# ── Panel B: Round-trip times (superfluid) ───────────────────────────
ax = axes[0, 1]
ax.plot(betas, T_sf_par,  label=r"$T_\parallel$ (contracted)", lw=2.2, color="#e63946")
ax.plot(betas, T_sf_perp, label=r"$T_\perp$",                   lw=2.2, color="#457b9d", ls="--")
ax.set_xlabel(r"$\beta = V / c_s$", fontsize=13)
ax.set_ylabel(r"Round-trip time  $T / (2L_0/c_s)$", fontsize=13)
ax.set_title("(B)  Dynamical Superfluid (UHF)", fontsize=13)
ax.legend(fontsize=12)
ax.set_xlim(0, 0.95)
ax.grid(alpha=0.3)
ax.annotate(r"$T_\parallel = T_\perp$ exactly", xy=(0.5, 0.5),
            xycoords="axes fraction", fontsize=14, color="#2a9d8f",
            fontweight="bold", ha="center")

# ── Panel C: Fringe shift comparison ────────────────────────────────
ax = axes[1, 0]
ax.plot(betas, dN_rigid, label="Rigid aether",       lw=2.2, color="#e63946")
ax.plot(betas, dN_super, label="Superfluid (UHF)",   lw=2.2, color="#2a9d8f")
ax.axhline(0, color="gray", lw=0.8, ls=":")
ax.set_xlabel(r"$\beta = V / c_s$", fontsize=13)
ax.set_ylabel(r"Fringe shift $\Delta N$", fontsize=13)
ax.set_title("(C)  Predicted Fringe Shift", fontsize=13)
ax.legend(fontsize=12, loc="upper left")
ax.set_xlim(0, 0.95)
ax.grid(alpha=0.3)
ax.annotate("Michelson-Morley\nobserved: ΔN = 0",
            xy=(0.35, 0.35), xycoords="axes fraction",
            fontsize=11, color="#2a9d8f", fontweight="bold", ha="center",
            bbox=dict(boxstyle="round,pad=0.4", fc="#e0f7f4", ec="#2a9d8f", alpha=0.9))

# ── Panel D: Schematic ray-tracing in lab frame ─────────────────────
ax = axes[1, 1]
ax.set_xlim(-0.3, 2.6)
ax.set_ylim(-0.8, 1.8)
ax.set_aspect("equal")
ax.set_title("(D)  Interferometer in Moving Superfluid", fontsize=13)
ax.axis("off")

V = 0.5 * c_s
L_contracted = L0 * np.sqrt(1 - (V/c_s)**2)

# beam-splitter at origin
bs = np.array([0.5, 0.4])
ax.plot(*bs, "ks", ms=10, zorder=5)
ax.annotate("BS", bs + np.array([0.05, -0.18]), fontsize=10, fontweight="bold",
            ha="center")

# parallel arm (contracted)
m_par = bs + np.array([L_contracted, 0])
ax.annotate("", xy=m_par, xytext=bs,
            arrowprops=dict(arrowstyle="<->", lw=2, color="#e63946"))
ax.plot(*m_par, "ro", ms=8, zorder=5)
ax.annotate(rf"$L_\parallel = L_0\sqrt{{1-\beta^2}}$"
            f"\n= {L_contracted:.3f}",
            xy=((bs[0]+m_par[0])/2, bs[1]-0.25),
            fontsize=9, ha="center", color="#e63946")

# perpendicular arm (unchanged)
m_perp = bs + np.array([0, L0])
ax.annotate("", xy=m_perp, xytext=bs,
            arrowprops=dict(arrowstyle="<->", lw=2, color="#457b9d"))
ax.plot(*m_perp, "bo", ms=8, zorder=5)
ax.annotate(rf"$L_\perp = L_0$ = {L0:.3f}",
            xy=(bs[0]-0.18, (bs[1]+m_perp[1])/2),
            fontsize=9, ha="right", color="#457b9d", rotation=90)

# mirror labels
ax.annotate(r"$M_\parallel$", xy=(m_par[0]+0.08, m_par[1]+0.08),
            fontsize=9, color="#e63946")
ax.annotate(r"$M_\perp$", xy=(m_perp[0]+0.08, m_perp[1]+0.05),
            fontsize=9, color="#457b9d")

# flow arrows (shifted right to avoid arm overlap)
for yy in [-0.4, 0.1, 0.6, 1.1, 1.5]:
    ax.annotate("", xy=(2.3, yy), xytext=(1.8, yy),
                arrowprops=dict(arrowstyle="->", lw=1.3, color="#bbb"))
ax.text(2.45, 0.55, r"$\mathbf{V}$" + "\n(flow)", fontsize=10, color="#999",
        rotation=90, va="center", ha="center")

plt.tight_layout(rect=[0, 0, 1, 0.94])
outpath = "michelson_morley_simulation.png"
plt.savefig(outpath, dpi=200, bbox_inches="tight")
print(f"✓ Saved figure to {outpath}")

# ── 4. Numerical Verification Table ─────────────────────────────────
print("\n" + "="*72)
print("  NUMERICAL VERIFICATION: T‖ = T⊥ in the Superfluid Model")
print("="*72)
print(f"  {'β':>6s}  {'T‖ (rigid)':>12s}  {'T⊥ (rigid)':>12s}  "
      f"{'T‖ (UHF)':>12s}  {'T⊥ (UHF)':>12s}  {'ΔT (UHF)':>12s}")
print("-"*72)
test_betas = [0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 0.95]
for b in test_betas:
    if b >= 1.0:
        continue
    tr_p, tr_n = rigid_aether_times(b)
    ts_p, ts_n = superfluid_times(b)
    print(f"  {b:6.3f}  {tr_p:12.8f}  {tr_n:12.8f}  "
          f"{ts_p:12.8f}  {ts_n:12.8f}  {ts_p - ts_n:12.2e}")
print("="*72)
print("  ΔT(UHF) = 0 to machine precision at every velocity.  QED.\n")
