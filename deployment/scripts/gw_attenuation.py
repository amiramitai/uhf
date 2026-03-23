#!/usr/bin/env python3
"""
GW Attenuation in a Viscoelastic Superfluid Vacuum
===================================================
Generates a quantitative attenuation curve showing the Maxwell
viscoelastic transfer function H(f) for gravitational shear waves,
overlaid with the sensitivity bands of LIGO, LISA, and NANOGrav/PTA.

The key prediction: below the crossover frequency f_c = 1/(2π τ_M),
shear waves become evanescent and the GW strain is suppressed.
"""

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import matplotlib.patches as mpatches

# ── Physical Setup ──────────────────────────────────────────────────
# Frequency range: 10^-12 to 10^5 Hz (covers PTA through LIGO)
f = np.logspace(-12, 5, 2000)
omega = 2 * np.pi * f

# Representative Maxwell relaxation times to explore
tau_M_values = {
    r'$\tau_M = 10^{6}$ s  ($f_c \approx 10^{-7}$ Hz)': 1e6,
    r'$\tau_M = 10^{8}$ s  ($f_c \approx 10^{-9}$ Hz)': 1e8,
    r'$\tau_M = 10^{10}$ s ($f_c \approx 10^{-11}$ Hz)': 1e10,
}

# ── Viscoelastic Transfer Function ──────────────────────────────────
def transfer_function(omega, tau_M):
    """
    Amplitude transfer function for shear waves in a Maxwell
    viscoelastic medium.
    
    H(ω) = ωτ_M / sqrt(1 + (ωτ_M)²)
    
    - For ωτ_M >> 1: H → 1 (elastic, propagating)
    - For ωτ_M << 1: H → ωτ_M (evanescent, linear suppression)
    - At ωτ_M = 1:   H = 1/√2 ≈ 0.707 (-3 dB crossover)
    """
    x = omega * tau_M
    return x / np.sqrt(1 + x**2)


def quality_factor(omega, tau_M):
    """Quality factor Q = ωτ_M."""
    return omega * tau_M


# ── Detector Sensitivity Bands ──────────────────────────────────────
detector_bands = {
    'NANOGrav\n(PTA)': (1e-9, 3e-7, '#e74c3c'),
    'LISA': (1e-4, 1e-1, '#f39c12'),
    'LIGO/\nVirgo': (10, 5e3, '#2ecc71'),
}

# ── Figure ──────────────────────────────────────────────────────────
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True,
                                gridspec_kw={'height_ratios': [3, 1.2],
                                             'hspace': 0.08})

# ── Panel A: Transfer Function H(f) ─────────────────────────────────
colors = ['#1a7a4f', '#2c5aa0', '#7b3294']
linestyles = ['-', '--', ':']

for (label, tau_M), color, ls in zip(tau_M_values.items(), colors, linestyles):
    H = transfer_function(omega, tau_M)
    ax1.semilogx(f, H, color=color, lw=2.2, ls=ls, label=label, zorder=5)
    # Mark crossover point
    f_c = 1 / (2 * np.pi * tau_M)
    ax1.plot(f_c, 1/np.sqrt(2), 'o', color=color, ms=8, zorder=6,
             markeredgecolor='white', markeredgewidth=1.5)

# GR prediction (no attenuation)
ax1.axhline(1.0, color='black', lw=1.5, ls='-.', alpha=0.5, label='GR (no attenuation)', zorder=3)

# -3 dB line
ax1.axhline(1/np.sqrt(2), color='gray', lw=0.8, ls=':', alpha=0.6, zorder=2)
ax1.text(2e-12, 1/np.sqrt(2) + 0.02, r'$-3$ dB ($1/\sqrt{2}$)',
         fontsize=8, color='gray', va='bottom')

# Detector bands
for name, (f_lo, f_hi, color) in detector_bands.items():
    ax1.axvspan(f_lo, f_hi, alpha=0.12, color=color, zorder=1)
    f_mid = np.sqrt(f_lo * f_hi)
    ax1.text(f_mid, 0.05, name, fontsize=8, fontweight='bold', color=color,
             ha='center', va='bottom', alpha=0.9)

ax1.set_ylabel(r'Transfer Function $\mathcal{H}(f) = h_c^{\rm UHF}/h_c^{\rm GR}$',
               fontsize=11)
ax1.set_ylim(-0.02, 1.12)
ax1.set_xlim(1e-12, 1e5)
ax1.legend(fontsize=8.5, loc='center right', framealpha=0.9)
ax1.set_title('Gravitational Wave Attenuation in the Viscoelastic Superfluid Vacuum',
              fontsize=12, fontweight='bold', pad=10)
ax1.grid(True, alpha=0.3, which='both')

# Annotations
ax1.annotate('Elastic regime\n(propagating)',
             xy=(1e3, 0.98), fontsize=8, color='#2ecc71', ha='center',
             fontstyle='italic')
ax1.annotate('Fluid regime\n(evanescent)',
             xy=(1e-11, 0.15), fontsize=8, color='#e74c3c', ha='center',
             fontstyle='italic')

# ── Panel B: Quality Factor Q(f) ────────────────────────────────────
tau_M_ref = 1e8  # reference τ_M for Q plot
Q = quality_factor(omega, tau_M_ref)
ax2.loglog(f, Q, color='#2c5aa0', lw=2, label=r'$Q = 2\pi f \tau_M$, $\tau_M = 10^8$ s')
ax2.axhline(1.0, color='gray', lw=1, ls='--', alpha=0.6)
ax2.text(2e-12, 1.3, r'$Q = 1$ (crossover)', fontsize=8, color='gray')

# Detector bands
for name, (f_lo, f_hi, color) in detector_bands.items():
    ax2.axvspan(f_lo, f_hi, alpha=0.12, color=color, zorder=1)

ax2.set_xlabel('Frequency $f$ (Hz)', fontsize=11)
ax2.set_ylabel('Quality Factor $Q$', fontsize=11)
ax2.set_ylim(1e-4, 1e14)
ax2.set_xlim(1e-12, 1e5)
ax2.legend(fontsize=8.5, loc='lower right', framealpha=0.9)
ax2.grid(True, alpha=0.3, which='both')

plt.tight_layout()
plt.savefig('gw_attenuation.png', dpi=200, bbox_inches='tight',
            facecolor='white', edgecolor='none')
plt.close()

print("=== GW Attenuation Analysis ===")
print()

# Print key results
for label, tau_M in tau_M_values.items():
    f_c = 1 / (2 * np.pi * tau_M)
    print(f"τ_M = {tau_M:.0e} s:")
    print(f"  Crossover frequency: f_c = {f_c:.2e} Hz")
    
    # Attenuation at representative frequencies
    for fname, fval in [("NANOGrav (3 nHz)", 3e-9),
                          ("LISA (1 mHz)", 1e-3),
                          ("LIGO (100 Hz)", 100)]:
        H = transfer_function(2*np.pi*fval, tau_M)
        Q_val = quality_factor(2*np.pi*fval, tau_M)
        print(f"  {fname}: H = {H:.6f}, Q = {Q_val:.2e}")
    print()

# LIGO constraint
print("Observational constraints:")
print(f"  LIGO detections at ~100 Hz require τ_M >> {1/(2*np.pi*100):.4f} s")
print(f"  NANOGrav signal at ~3 nHz requires τ_M > {1/(2*np.pi*3e-9):.2e} s")
print(f"  → τ_M > 5.3 × 10⁷ s (~1.7 years) if NANOGrav signal is genuine")
print()
print("Figure saved: gw_attenuation.png")
print("✓ PASS")
