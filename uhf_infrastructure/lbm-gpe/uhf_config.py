"""
UHF Phase 0 — Configuration & Physical Constants
==================================================
Defines the fundamental constants of the Unified Hydrodynamic Framework
(Version 8.0) for the sub-Planckian viscoelastic superfluid vacuum.

All values in SI units unless noted otherwise.
"""

import math

# ─────────────────────────────────────────────────────────────────────
# Fundamental physical constants
# ─────────────────────────────────────────────────────────────────────
HBAR: float = 1.054571817e-34      # ℏ  reduced Planck constant  [J·s]
C: float = 2.99792458e8            # c  speed of light / sound   [m/s]
G_NEWTON: float = 6.67430e-11      # G  gravitational constant   [m³ kg⁻¹ s⁻²]

# ─────────────────────────────────────────────────────────────────────
# UHF condensate parameters  (Version 8.0)
# ─────────────────────────────────────────────────────────────────────
M_BOSON_EV: float = 2.1e-3         # Sub-Planckian boson mass    [eV/c²]
M_BOSON: float = 3.74e-36          # Same, in SI                 [kg]

RHO_0: float = 5.155e96            # ρ₀  background (Planck) density  [kg/m³]
EPSILON: float = 1.0 / math.sqrt(2.0 * math.pi)  # ≈ 0.399 pulsation amplitude

# ─────────────────────────────────────────────────────────────────────
# Derived scales  (computed once, used everywhere)
# ─────────────────────────────────────────────────────────────────────
OMEGA_K: float = M_BOSON * C**2 / HBAR              # Kuramoto / Compton frequency  [rad/s]
XI: float = HBAR / (M_BOSON * C)                     # Healing length ξ  [m]
L_PLANCK: float = math.sqrt(HBAR * G_NEWTON / C**3)  # Planck length  [m]

# Torus geometry  (placeholder ratios — business logic NOT implemented)
R_OVER_R_NOMINAL: float = 0.22     # r/R from condensate EOS (Section 9.3.29)

# ─────────────────────────────────────────────────────────────────────
# Simulation Scale
# ─────────────────────────────────────────────────────────────────────
#
# The simulation domain is a periodic cube of N³ voxels.  Each voxel
# has side-length Δx expressed as a multiple of the healing length ξ.
# Physical box side = N · Δx.
#
DEFAULT_GRID_SMALL: int = 256      # N³ grid for prototyping
DEFAULT_GRID_LARGE: int = 512      # N³ grid for production runs

DX_OVER_XI: float = 0.5            # Δx / ξ  — half a healing length per voxel
DX: float = DX_OVER_XI * XI        # Δx in metres
BOX_LENGTH_256: float = DEFAULT_GRID_SMALL * DX   # physical box side for 256³
BOX_LENGTH_512: float = DEFAULT_GRID_LARGE * DX   # physical box side for 512³

# ─────────────────────────────────────────────────────────────────────
# GPU memory budget
# ─────────────────────────────────────────────────────────────────────
VRAM_REQUIRED_256: float = 2.0     # approx GB for 256³ complex128
VRAM_REQUIRED_512: float = 16.0    # approx GB for 512³ complex128
VRAM_SAFETY_FRACTION: float = 0.80 # never exceed 80 % of total VRAM

# ─────────────────────────────────────────────────────────────────────
# Quick sanity banner (runs on import during dev)
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║       UHF Phase 0 — Config Sanity Check                 ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print(f"║  ℏ           = {HBAR:.6e}  J·s              ║")
    print(f"║  c           = {C:.6e}  m/s               ║")
    print(f"║  m_boson     = {M_BOSON:.2e}  kg  ({M_BOSON_EV*1e3:.1f} μeV)    ║")
    print(f"║  ρ₀          = {RHO_0:.3e}  kg/m³             ║")
    print(f"║  ε           = {EPSILON:.6f}                        ║")
    print(f"║  ω_K         = {OMEGA_K:.3e}  rad/s             ║")
    print(f"║  ξ (healing) = {XI:.3e}  m                  ║")
    print(f"║  l_P         = {L_PLANCK:.3e}  m                  ║")
    print(f"║  r/R nominal = {R_OVER_R_NOMINAL}                            ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print(f"║  Δx/ξ        = {DX_OVER_XI}                              ║")
    print(f"║  Δx          = {DX:.3e}  m                  ║")
    print(f"║  Box (256³)  = {BOX_LENGTH_256:.3e}  m                  ║")
    print(f"║  Box (512³)  = {BOX_LENGTH_512:.3e}  m                  ║")
    print(f"║  VRAM safety = {VRAM_SAFETY_FRACTION*100:.0f} %                              ║")
    print("╚══════════════════════════════════════════════════════════╝")
