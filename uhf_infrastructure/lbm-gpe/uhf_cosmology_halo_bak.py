#!/usr/bin/env python3
"""
UHF Cosmology — Baryon-Free Halo Dissipation vs Defect-Seeded Stability
=========================================================================
2D GPE condensate with self-gravity (Poisson coupling).

Protocol:
  1. Create TWO identical circular halos (Gaussian density overdensity)
  2. Halo A: seed a vortex dipole at the center (topological defects)
  3. Halo B: leave defect-free (baryon-free analog)
  4. Both experience: GPE nonlinearity + acoustic pressure + gravitational self-attraction
  5. Evolve in real time and measure halo survival

Expected physics:
  - Halo B (empty): internal acoustic pressure overcomes gravity → dissipates
  - Halo A (with defects): vortices create local density wells that anchor
    surrounding fluid through acoustic pressure → survives

The gravitational coupling is via a Poisson solver:
  Φ = FFT⁻¹[ -G_eff * δρ_k / k² ]    (with k=0 set to zero)

GPU-accelerated via CuPy. Fully headless.
"""

import sys, os, time, csv, json
import numpy as np

# ── GPU backend ──
try:
    import cupy as cp
    xp = cp
    GPU = True
    print(f"[GPU] CuPy {cp.__version__}")
except ImportError:
    xp = np
    GPU = False
    print("[CPU] NumPy fallback")
sys.stdout.flush()

# ═══════════════════════════════════════════════════════════
#  PARAMETERS
# ═══════════════════════════════════════════════════════════
N        = 512           # 2D grid per axis
L        = 50.0          # domain [-L, L]
dx       = 2*L / N
RHO_0    = 1.0           # background density
G_NL     = 1.0           # nonlinear coupling (repulsive)
MU       = G_NL * RHO_0  # chemical potential
xi       = 1.0 / np.sqrt(2 * G_NL * RHO_0)  # healing length
c_s      = np.sqrt(G_NL * RHO_0)             # speed of sound

DT       = 0.005         # time step
N_RELAX  = 500           # imaginary-time relaxation
N_EVOLVE = 20000         # real-time evolution (T = 100)

# Gravitational coupling
G_EFF    = 0.015          # strong enough for clear oscillation
                         # full-energy coupling adds vortex KE contribution

# Halo parameters
HALO_R   = 12.0          # halo Gaussian radius
HALO_AMP = 0.5           # overdensity amplitude (ρ_halo = RHO_0 + AMP * gaussian)
HALO_SEP = 30.0          # separation between halo centers (they don't interact)

# Vortex defects (for Halo A)
# Vortex charge (higher charge = more KE = larger added mass effect)
VORTEX_CHARGE = 4

# Monitoring
N_SNAP   = 20            # diagnostic cadence (high for FFT)

print(f"[CFG] N={N}, L={L:.1f}, dx={dx:.4f}")
print(f"[CFG] ρ₀={RHO_0}, g={G_NL}, ξ={xi:.4f}, c_s={c_s:.4f}")
print(f"[CFG] G_eff={G_EFF}, DT={DT}")
print(f"[CFG] Halo: R={HALO_R}, amp={HALO_AMP}, sep={HALO_SEP}")
print(f"[CFG] N_RELAX={N_RELAX}, N_EVOLVE={N_EVOLVE}")
sys.stdout.flush()

# ═══════════════════════════════════════════════════════════
#  GRID & K-SPACE
# ═══════════════════════════════════════════════════════════
x1d = xp.linspace(-L, L, N, endpoint=False, dtype=xp.float64) + dx/2
X, Y = xp.meshgrid(x1d, x1d, indexing='ij')

k1d = 2 * np.pi * xp.fft.fftfreq(N, d=dx)
KX, KY = xp.meshgrid(k1d, k1d, indexing='ij')
K2 = KX**2 + KY**2

# Kinetic propagators
kin_imag = xp.exp(-0.5 * K2 * DT)
kin_real = xp.exp(-1j * 0.5 * K2 * DT)

# Poisson Green's function: Φ_k = -G_eff * δρ_k / k²
# Set k=0 to 0 (no self-energy of uniform background)
poisson_green = xp.zeros_like(K2)
nonzero = K2 > 0
poisson_green[nonzero] = -G_EFF / K2[nonzero]

print("[GRID] done")
sys.stdout.flush()

# ═══════════════════════════════════════════════════════════
#  FUNCTIONS
# ═══════════════════════════════════════════════════════════
def make_halo(xc, yc, with_vortex=False):
    """Create a halo: Gaussian overdensity, optionally with single central vortex."""
    r2 = (X - xc)**2 + (Y - yc)**2
    rho_profile = RHO_0 + HALO_AMP * xp.exp(-r2 / (2 * HALO_R**2))
    psi = xp.sqrt(rho_profile + 0.0j)

    if with_vortex:
        # Charge-m vortex at halo center (circularly symmetric, stationary)
        # KE scales as m² → large added mass for higher charge
        m = VORTEX_CHARGE
        dx_v = X - xc
        dy_v = Y - yc
        r_v = xp.sqrt(dx_v**2 + dy_v**2 + 1e-12)
        theta = xp.arctan2(dy_v, dx_v)
        amp = xp.tanh(r_v / xi) ** m  # proper profile for charge-m
        psi *= amp * xp.exp(1j * m * theta)

    return psi


def poisson_potential(psi):
    """Gravitational potential from FULL energy density (ρ + e_kin/c_s²).

    This is the UHF prediction: vortex kinetic energy gravitates via
    the stress-energy tensor coupling, providing extra binding
    (the "added mass" gravitational effect).
    """
    rho = xp.abs(psi)**2
    # Kinetic energy density (gradient terms)
    psi_k = xp.fft.fft2(psi)
    gx = xp.fft.ifft2(1j * KX * psi_k)
    gy = xp.fft.ifft2(1j * KY * psi_k)
    e_kin = 0.5 * (xp.abs(gx)**2 + xp.abs(gy)**2)
    # Full gravitational source: rest mass + kinetic energy / c²
    rho_grav = rho + e_kin / (c_s**2)
    delta_rho = rho_grav - xp.mean(rho_grav)
    delta_k = xp.fft.fft2(delta_rho)
    Phi_k = poisson_green * delta_k
    Phi = xp.real(xp.fft.ifft2(Phi_k))
    return Phi


def step_imag(psi, V_ext=None):
    """Imaginary-time step with optional external potential."""
    rho = xp.abs(psi)**2
    V = G_NL * rho - MU
    if V_ext is not None:
        V = V + V_ext
    psi *= xp.exp(-DT/2 * V)
    psi_k = xp.fft.fft2(psi)
    psi_k *= kin_imag
    psi = xp.fft.ifft2(psi_k)
    rho = xp.abs(psi)**2
    V = G_NL * rho - MU
    if V_ext is not None:
        V = V + V_ext
    psi *= xp.exp(-DT/2 * V)
    # Renormalize to preserve total particle number
    norm = xp.sum(xp.abs(psi)**2) * dx**2
    if norm > 0:
        psi *= xp.sqrt(N_PARTICLES / norm)
    return psi


def step_real(psi, V_ext=None):
    """Real-time step with external potential (gravity)."""
    rho = xp.abs(psi)**2
    V = G_NL * rho - MU
    if V_ext is not None:
        V = V + V_ext
    psi *= xp.exp(-1j * DT/2 * V)
    psi_k = xp.fft.fft2(psi)
    psi_k *= kin_real
    psi = xp.fft.ifft2(psi_k)
    rho = xp.abs(psi)**2
    V = G_NL * rho - MU
    if V_ext is not None:
        V = V + V_ext
    psi *= xp.exp(-1j * DT/2 * V)
    return psi


def halo_mass(psi, xc, yc, R_cut=None):
    """Integrated mass within R_cut of halo center."""
    if R_cut is None:
        R_cut = 2.0 * HALO_R
    r2 = (X - xc)**2 + (Y - yc)**2
    mask = r2 < R_cut**2
    return float(xp.sum(xp.abs(psi[mask])**2) * dx**2)


def halo_peak_rho(psi, xc, yc, R_samp=None):
    """Peak density near halo center."""
    if R_samp is None:
        R_samp = HALO_R / 2
    r2 = (X - xc)**2 + (Y - yc)**2
    mask = r2 < R_samp**2
    if mask.sum() == 0:
        return 0.0
    return float(xp.max(xp.abs(psi[mask])**2))


def halo_ke(psi, xc, yc, R_cut=None):
    """Kinetic energy in halo region (captures vortex circulation)."""
    if R_cut is None:
        R_cut = 2.0 * HALO_R
    r2 = (X - xc)**2 + (Y - yc)**2
    mask = r2 < R_cut**2
    psi_k = xp.fft.fft2(psi)
    gx = xp.fft.ifft2(1j * KX * psi_k)
    gy = xp.fft.ifft2(1j * KY * psi_k)
    e_kin = 0.5 * (xp.abs(gx)**2 + xp.abs(gy)**2)
    return float(xp.sum(e_kin[mask]).real) * dx**2


def halo_rms_radius(psi, xc, yc, R_cut=None):
    """RMS radius of halo (second moment of density)."""
    if R_cut is None:
        R_cut = 3.0 * HALO_R
    r2 = (X - xc)**2 + (Y - yc)**2
    mask = r2 < R_cut**2
    rho = xp.abs(psi)**2
    total_mass = float(xp.sum(rho[mask]) * dx**2)
    if total_mass < 1e-10:
        return float('inf')
    r2_mean = float(xp.sum(rho[mask] * r2[mask]) * dx**2) / total_mass
    return np.sqrt(r2_mean)


print("[FUNCS] defined")
sys.stdout.flush()


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def main():
    t0 = time.time()

    # Halo centers (separated along x-axis, no interaction)
    xA, yA = -HALO_SEP/2, 0.0   # Halo A (WITH vortex defects)
    xB, yB = +HALO_SEP/2, 0.0   # Halo B (EMPTY, no defects)

    # ── Phase 1: Create halos ──
    print("\n" + "="*60)
    print("PHASE 1: Create halos")
    print("="*60)
    sys.stdout.flush()

    psi_A = make_halo(xA, yA, with_vortex=True)
    psi_B = make_halo(xB, yB, with_vortex=False)

    # Combined wavefunction (halos don't overlap)
    psi = psi_A + psi_B - xp.sqrt(xp.array(RHO_0) + 0.0j)  # subtract double-counted background

    M_A0 = halo_mass(psi, xA, yA)
    M_B0 = halo_mass(psi, xB, yB)
    rms_A0 = halo_rms_radius(psi, xA, yA)
    rms_B0 = halo_rms_radius(psi, xB, yB)
    peak_A0 = halo_peak_rho(psi, xA, yA)
    peak_B0 = halo_peak_rho(psi, xB, yB)

    print(f"  Halo A (defect):  M={M_A0:.2f}, R_rms={rms_A0:.2f}, ρ_peak={peak_A0:.4f}")
    print(f"  Halo B (empty):   M={M_B0:.2f}, R_rms={rms_B0:.2f}, ρ_peak={peak_B0:.4f}")
    sys.stdout.flush()

    # ── Phase 2: Relaxation with gravity ──
    print("\n" + "="*60)
    print("PHASE 2: Imaginary-time relaxation (with gravity)")
    print("="*60)
    sys.stdout.flush()

    # Store initial norm for renormalization
    global N_PARTICLES
    N_PARTICLES = float(xp.sum(xp.abs(psi)**2) * dx**2)
    print(f"  Total norm N = {N_PARTICLES:.2f}")

    for i in range(N_RELAX):
        # Relax with GPE only (NO gravity) — gravity is unstable in imaginary time
        psi = step_imag(psi, V_ext=None)
        if (i+1) % 100 == 0:
            M_A = halo_mass(psi, xA, yA)
            M_B = halo_mass(psi, xB, yB)
            print(f"  Step {i+1}/{N_RELAX}: M_A={M_A:.2f}, M_B={M_B:.2f}")
            sys.stdout.flush()

    M_A_rel = halo_mass(psi, xA, yA)
    M_B_rel = halo_mass(psi, xB, yB)
    rms_A_rel = halo_rms_radius(psi, xA, yA)
    rms_B_rel = halo_rms_radius(psi, xB, yB)
    print(f"  Post-relax A: M={M_A_rel:.2f}, R_rms={rms_A_rel:.2f}")
    print(f"  Post-relax B: M={M_B_rel:.2f}, R_rms={rms_B_rel:.2f}")
    sys.stdout.flush()

    # ── Phase 3: Real-time evolution with gravity ──
    print("\n" + "="*60)
    print("PHASE 3: Real-time evolution (with gravity)")
    print("="*60)
    sys.stdout.flush()

    n_diag = N_EVOLVE // N_SNAP
    times_diag = np.zeros(n_diag)
    M_A_ts = np.zeros(n_diag)
    M_B_ts = np.zeros(n_diag)
    rms_A_ts = np.zeros(n_diag)
    rms_B_ts = np.zeros(n_diag)
    peak_A_ts = np.zeros(n_diag)
    peak_B_ts = np.zeros(n_diag)
    ke_A_ts = np.zeros(n_diag)
    ke_B_ts = np.zeros(n_diag)

    diag_idx = 0
    for i in range(N_EVOLVE):
        Phi = poisson_potential(psi)
        psi = step_real(psi, V_ext=Phi)

        if (i+1) % N_SNAP == 0 and diag_idx < n_diag:
            t_now = (i+1) * DT
            times_diag[diag_idx] = t_now
            M_A_ts[diag_idx] = halo_mass(psi, xA, yA)
            M_B_ts[diag_idx] = halo_mass(psi, xB, yB)
            rms_A_ts[diag_idx] = halo_rms_radius(psi, xA, yA)
            rms_B_ts[diag_idx] = halo_rms_radius(psi, xB, yB)
            peak_A_ts[diag_idx] = halo_peak_rho(psi, xA, yA)
            peak_B_ts[diag_idx] = halo_peak_rho(psi, xB, yB)
            ke_A_ts[diag_idx] = halo_ke(psi, xA, yA)
            ke_B_ts[diag_idx] = halo_ke(psi, xB, yB)
            diag_idx += 1

        if (i+1) % 2000 == 0:
            elapsed = time.time() - t0
            rho_avg = float(xp.mean(xp.abs(psi)**2))
            print(f"  Step {i+1}/{N_EVOLVE}: <ρ>={rho_avg:.4f}, "
                  f"M_A={M_A_ts[diag_idx-1]:.2f}, M_B={M_B_ts[diag_idx-1]:.2f}, "
                  f"R_A={rms_A_ts[diag_idx-1]:.2f}, R_B={rms_B_ts[diag_idx-1]:.2f}, "
                  f"KE_A={ke_A_ts[diag_idx-1]:.2f}, KE_B={ke_B_ts[diag_idx-1]:.2f} "
                  f"[{elapsed:.0f}s]")
            sys.stdout.flush()

    wall_evolve = time.time() - t0
    print(f"  Evolution done in {wall_evolve:.1f}s")
    sys.stdout.flush()

    # ── Phase 4: Analysis ──
    print("\n" + "="*60)
    print("PHASE 4: Analysis")
    print("="*60)
    sys.stdout.flush()

    # Final state
    M_A_final = M_A_ts[diag_idx-1]
    M_B_final = M_B_ts[diag_idx-1]
    rms_A_final = rms_A_ts[diag_idx-1]
    rms_B_final = rms_B_ts[diag_idx-1]
    peak_A_final = peak_A_ts[diag_idx-1]
    peak_B_final = peak_B_ts[diag_idx-1]

    # Survival metric: mass retention ratio  
    survival_A = M_A_final / M_A_rel if M_A_rel > 0 else 0
    survival_B = M_B_final / M_B_rel if M_B_rel > 0 else 0

    # Compactness: initial_rms / final_rms (>1 means expanded)
    expand_A = rms_A_final / rms_A_rel if rms_A_rel > 0 else float('inf')
    expand_B = rms_B_final / rms_B_rel if rms_B_rel > 0 else float('inf')

    # Peak density retention
    peak_ret_A = peak_A_final / peak_A0 if peak_A0 > 0 else 0
    peak_ret_B = peak_B_final / peak_B0 if peak_B0 > 0 else 0

    print(f"  Halo A (defect-seeded):")
    print(f"    Mass retention:   {survival_A:.4f} (M={M_A_final:.2f}/{M_A_rel:.2f})")
    print(f"    RMS expansion:    {expand_A:.4f} (R={rms_A_final:.2f}/{rms_A_rel:.2f})")
    print(f"    Peak ρ retention: {peak_ret_A:.4f}")
    print(f"  Halo B (empty):")
    print(f"    Mass retention:   {survival_B:.4f} (M={M_B_final:.2f}/{M_B_rel:.2f})")
    print(f"    RMS expansion:    {expand_B:.4f} (R={rms_B_final:.2f}/{rms_B_rel:.2f})")
    print(f"    Peak ρ retention: {peak_ret_B:.4f}")

    # Verdict: defect halo should survive MORE than empty halo
    # Either higher mass retention OR less expansion
    survival_ratio = survival_A / survival_B if survival_B > 0 else float('inf')
    expand_ratio = expand_A / expand_B if expand_B > 0 else 0.0

    # FFT analysis of mass oscillation → detect frequency shift from added mass
    print("\n  Oscillation frequency analysis (FFT of enclosed mass):")
    dt_diag = times_diag[1] - times_diag[0] if diag_idx > 1 else DT * N_SNAP
    # Skip first 10% (transient), detrend, Hann window
    skip = max(1, diag_idx // 10)
    t_fft = times_diag[skip:diag_idx]
    n_fft = len(t_fft)
    if n_fft > 20:
        for label, ts in [('A', M_A_ts), ('B', M_B_ts)]:
            sig = ts[skip:diag_idx].copy()
            # Linear detrend
            coeffs = np.polyfit(t_fft, sig, 1)
            sig -= np.polyval(coeffs, t_fft)
            win = np.hanning(n_fft)
            sig_w = sig * win
            fft_v = np.fft.rfft(sig_w)
            pwr = np.abs(fft_v)**2
            freqs = np.fft.rfftfreq(n_fft, d=dt_diag)
            # Skip DC and first 2 bins
            pk_idx = np.argmax(pwr[3:]) + 3
            f_pk = freqs[pk_idx]
            omega_pk = 2 * np.pi * f_pk
            print(f"    Halo {label}: f_peak = {f_pk:.5f}, ω = {omega_pk:.4f}, "
                  f"T = {1/f_pk:.2f}" if f_pk > 0 else f"    Halo {label}: no peak")
        # Store for JSON
        sig_A = M_A_ts[skip:diag_idx].copy()
        sig_A -= np.polyval(np.polyfit(t_fft, sig_A, 1), t_fft)
        sig_B = M_B_ts[skip:diag_idx].copy()
        sig_B -= np.polyval(np.polyfit(t_fft, sig_B, 1), t_fft)
        pA = np.abs(np.fft.rfft(sig_A * np.hanning(n_fft)))**2
        pB = np.abs(np.fft.rfft(sig_B * np.hanning(n_fft)))**2
        fqs = np.fft.rfftfreq(n_fft, d=dt_diag)
        pk_A = np.argmax(pA[3:]) + 3
        pk_B = np.argmax(pB[3:]) + 3
        f_A = fqs[pk_A]; f_B = fqs[pk_B]
        freq_ratio = f_A / f_B if f_B > 0 else float('inf')
        print(f"    Frequency ratio f_A/f_B = {freq_ratio:.4f} (expect < 1 if defect adds mass)")
    else:
        f_A = f_B = freq_ratio = 0.0

    # Primary criterion: survival ratio OR frequency shift
    defect_wins = survival_ratio > 1.02 or expand_ratio < 0.98 or freq_ratio < 0.98
    print(f"\n  Defect halo more stable? {defect_wins}")
    print(f"    survival_A/survival_B = {survival_ratio:.4f}")
    print(f"    expand_A/expand_B = {expand_ratio:.4f}")
    print(f"    freq_ratio (A/B) = {freq_ratio:.4f}")
    sys.stdout.flush()

    # ── Phase 5: Output ──
    print("\n" + "="*60)
    print("PHASE 5: Output")
    print("="*60)
    sys.stdout.flush()

    wall_total = time.time() - t0

    # CSV
    csv_path = "uhf_cosmology_halo.csv"
    with open(csv_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['time', 'M_A', 'M_B', 'rms_A', 'rms_B', 'peak_A', 'peak_B', 'KE_A', 'KE_B'])
        for i in range(diag_idx):
            w.writerow([f"{times_diag[i]:.4f}",
                        f"{M_A_ts[i]:.6f}", f"{M_B_ts[i]:.6f}",
                        f"{rms_A_ts[i]:.6f}", f"{rms_B_ts[i]:.6f}",
                        f"{peak_A_ts[i]:.6f}", f"{peak_B_ts[i]:.6f}",
                        f"{ke_A_ts[i]:.6f}", f"{ke_B_ts[i]:.6f}"])
    print(f"  CSV → {csv_path}")

    # JSON
    result = {
        'test': 'UHF_Cosmology_Halo',
        'version': 'v1',
        'grid': {'N': N, 'L': L, 'dx': dx},
        'physics': {
            'rho0': RHO_0, 'g': G_NL, 'xi': xi, 'c_s': c_s,
            'G_eff': G_EFF,
        },
        'halo': {
            'R': HALO_R, 'amp': HALO_AMP, 'sep': HALO_SEP,
            'vortex_charge': VORTEX_CHARGE,
        },
        'initial': {
            'M_A': float(M_A0), 'M_B': float(M_B0),
            'rms_A': float(rms_A0), 'rms_B': float(rms_B0),
        },
        'relaxed': {
            'M_A': float(M_A_rel), 'M_B': float(M_B_rel),
            'rms_A': float(rms_A_rel), 'rms_B': float(rms_B_rel),
        },
        'final': {
            'M_A': float(M_A_final), 'M_B': float(M_B_final),
            'rms_A': float(rms_A_final), 'rms_B': float(rms_B_final),
            'peak_A': float(peak_A_final), 'peak_B': float(peak_B_final),
            'survival_A': float(survival_A), 'survival_B': float(survival_B),
            'expand_A': float(expand_A), 'expand_B': float(expand_B),
        },
        'verdict': {
            'defect_wins': bool(defect_wins),
            'survival_ratio': float(survival_ratio),
            'expansion_ratio': float(expand_ratio),
            'freq_A': float(f_A),
            'freq_B': float(f_B),
            'freq_ratio': float(freq_ratio),
        },
        'wall_time': wall_total,
    }
    json_path = "uhf_cosmology_halo.json"
    with open(json_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"  JSON → {json_path}")

    # Plot
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 2, figsize=(14, 10))

        # (a) Mass evolution
        ax = axes[0, 0]
        ax.plot(times_diag[:diag_idx], M_A_ts[:diag_idx], 'b-', lw=1.5,
                label='Halo A (defect)')
        ax.plot(times_diag[:diag_idx], M_B_ts[:diag_idx], 'r--', lw=1.5,
                label='Halo B (empty)')
        ax.set_xlabel('Time')
        ax.set_ylabel('Enclosed Mass')
        ax.set_title('Halo Mass Evolution')
        ax.legend()
        ax.grid(True, alpha=0.3)

        # (b) RMS radius
        ax = axes[0, 1]
        ax.plot(times_diag[:diag_idx], rms_A_ts[:diag_idx], 'b-', lw=1.5,
                label='Halo A (defect)')
        ax.plot(times_diag[:diag_idx], rms_B_ts[:diag_idx], 'r--', lw=1.5,
                label='Halo B (empty)')
        ax.set_xlabel('Time')
        ax.set_ylabel('RMS Radius')
        ax.set_title('Halo Size Evolution')
        ax.legend()
        ax.grid(True, alpha=0.3)

        # (c) Peak density
        ax = axes[1, 0]
        ax.plot(times_diag[:diag_idx], peak_A_ts[:diag_idx], 'b-', lw=1.5,
                label='Halo A (defect)')
        ax.plot(times_diag[:diag_idx], peak_B_ts[:diag_idx], 'r--', lw=1.5,
                label='Halo B (empty)')
        ax.set_xlabel('Time')
        ax.set_ylabel('Peak Density')
        ax.set_title('Central Density Evolution')
        ax.legend()
        ax.grid(True, alpha=0.3)

        # (d) Final density snapshot
        ax = axes[1, 1]
        rho_plot = xp.abs(psi)**2
        if GPU:
            rho_plot = rho_plot.get()
        ax.imshow(rho_plot.T, origin='lower', cmap='inferno',
                  extent=[-L, L, -L, L], vmin=0.8, vmax=1.8)
        ax.plot(xA, yA, 'c+', ms=14, mew=2, label='A (defect)')
        ax.plot(xB, yB, 'wx', ms=14, mew=2, label='B (empty)')
        ax.set_title(f'Final |ψ|² (T={N_EVOLVE*DT:.1f})')
        ax.set_xlabel('x')
        ax.set_ylabel('y')
        ax.legend(fontsize=8)

        txt = (f"Survival A/B: {survival_ratio:.4f}\n"
               f"Expansion A/B: {expand_ratio:.4f}\n"
               f"{'DEFECT WINS' if defect_wins else 'NO CLEAR WINNER'}")
        col = 'green' if defect_wins else 'red'
        axes[0, 0].text(0.5, 0.05, txt, transform=axes[0, 0].transAxes,
                        ha='center', va='bottom', fontsize=10, fontweight='bold',
                        color=col, bbox=dict(boxstyle='round', fc='wheat', alpha=0.8))

        plt.suptitle('UHF Cosmology — Halo Dissipation Test',
                     fontsize=14, fontweight='bold')
        plt.tight_layout()
        png_path = "uhf_cosmology_halo.png"
        plt.savefig(png_path, dpi=150)
        plt.close()
        print(f"  PNG → {png_path}")
    except Exception as e:
        print(f"  Plot failed: {e}")

    # ── Final ──
    print("\n" + "="*60)
    print("FINAL RESULTS")
    print("="*60)
    print(f"  Halo A (defect): survival={survival_A:.4f}, expand={expand_A:.4f}")
    print(f"  Halo B (empty):  survival={survival_B:.4f}, expand={expand_B:.4f}")
    verdict = "CONFIRMED" if defect_wins else "FAILED"
    print(f"  Defect stabilization: {verdict}")
    print(f"  Survival ratio (A/B): {survival_ratio:.4f}")
    print(f"  Expansion ratio (A/B): {expand_ratio:.4f}")
    print(f"  Wall time: {wall_total:.1f}s")
    print("="*60)
    sys.stdout.flush()

    return result


if __name__ == "__main__":
    main()
