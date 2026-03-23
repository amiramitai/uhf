#!/usr/bin/env python3
"""
UHF Hawking v5 — Correlation-Based Detection (1+1D Acoustic Horizon)
=====================================================================
Headless. No GUI. Console + CSV + PNG only.

Physics:
  1+1D massless scalar field φ on acoustic metric with tanh velocity profile.
  v(x) = v_inf * tanh(x / delta) where v_inf > c_s creates a sonic horizon.
  Klein-Gordon: ∂²φ/∂t² + ∂(v·∂φ/∂x)/∂x + ∂(v·∂φ/∂t)/∂x = c_s² ∂²φ/∂x²
  Rewritten as coupled first-order system for symplectic integration.

Quantum vacuum:
  Initialize φ_k, π_k in Fourier space with zero-point fluctuations:
  |φ_k|² = 1/(2ω_k), |π_k|² = ω_k/2, ω_k = c_s|k|

Measurement:
  Equal-time density-density correlation G²(x,x') = <δρ(x)δρ(x')>
  averaged over N_ensemble realizations.
  Hawking signature: off-diagonal peak correlating x_in (inside) with x_out (outside).

Engine: NumPy/SciPy vectorized (1D problem, ensemble parallelism).
"""

import numpy as np
import time
import csv
import os
import sys

# ════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════
NX          = 4096        # spatial grid points
L           = 80.0        # domain [-L/2, L/2]  (wider for better sponge)
dx          = L / NX
C_S         = 1.0         # speed of sound
V_INF       = 1.2         # asymptotic flow speed (>c_s, mildly supersonic)
DELTA       = 1.0         # horizon width parameter (gentler transition)
DT          = 0.3 * dx / (C_S + V_INF)  # CFL-safe timestep
T_MAX       = 40.0        # total evolution time
N_STEPS     = int(T_MAX / DT)
T_TRANSIENT = 10.0        # discard initial transient
STEP_TRANS  = int(T_TRANSIENT / DT)

N_ENSEMBLE  = 128         # number of independent realizations
NOISE_SCALE = 1e-3        # overall amplitude of vacuum fluctuations

# Correlation measurement grid (subsample for memory)
N_CORR      = 512         # correlation matrix size
SAMPLE_EVERY = max(1, NX // N_CORR)

# Asymmetric velocity profile:
#   v(x) = -V_INF × (1 + tanh(x/δ)) / 2
# Left (x→-∞): v=0 (subsonic, "outside")
# Right (x→+∞): v=-V_INF (supersonic, "inside" BH)
# Single horizon where |v(x_h)| = c_s
# Solve: V_INF × (1+tanh(x_h/δ))/2 = c_s → x_h = δ arctanh(2c_s/V_INF - 1)
X_HORIZON  = DELTA * np.arctanh(2*C_S/V_INF - 1)
# Surface gravity: κ = |d|v|/dx|_{x_h}
# d|v|/dx = V_INF / (2δ) × 1/cosh²(x_h/δ)
KAPPA      = (V_INF / (2*DELTA)) / np.cosh(X_HORIZON / DELTA)**2
T_HAWKING  = KAPPA / (2.0 * np.pi)

print("=" * 72)
print("  UHF Hawking v5 — Correlation-Based Detection")
print(f"  NX={NX}, L={L}, dx={dx:.5f}, dt={DT:.6f}")
print(f"  v(x) = -{V_INF} × (1+tanh(x/{DELTA}))/2  [ASYMMETRIC]")
print(f"  c_s={C_S}, horizon at x_h = {X_HORIZON:.4f}")
print(f"  κ = {KAPPA:.6f}, T_Hawking = {T_HAWKING:.6f}")
print(f"  N_steps={N_STEPS}, T_max={T_MAX}, T_transient={T_TRANSIENT}")
print(f"  N_ensemble={N_ENSEMBLE}, noise_scale={NOISE_SCALE}")
print(f"  CFL = dt×(c_s+v_inf)/dx = {DT*(C_S+V_INF)/dx:.4f}")
print("=" * 72)
sys.stdout.flush()

# ════════════════════════════════════════════════════════════════
# GRID SETUP
# ════════════════════════════════════════════════════════════════
x = np.linspace(-L/2 + dx/2, L/2 - dx/2, NX)  # cell centers

# Asymmetric profile: ONE horizon only
# Left half (x < x_h): subsonic "outside" region — phonons can escape
# Right half (x > x_h): supersonic "inside" BH — phonons swept in
v_bg = -V_INF * (1.0 + np.tanh(x / DELTA)) / 2.0

# Effective sound speed squared minus flow squared
c_eff_sq = C_S**2 - v_bg**2

# Spatial derivative of velocity (analytic for better accuracy)
dv_dx = -V_INF / (2.0 * DELTA) / np.cosh(x / DELTA)**2

# Sponge layers — extra strong on supersonic (right) side to prevent
# ergoregion instability from reflecting modes
sponge = np.zeros(NX)
sponge_left_width  = L * 0.1  # 10% of domain on left
sponge_right_width = L * 0.2  # 20% of domain on right (stronger absorption)
for i in range(NX):
    # Left sponge (subsonic side — outgoing Hawking phonons absorbed here)
    d_left = x[i] - (-L/2)
    if d_left < sponge_left_width:
        sponge[i] = 10.0 * ((sponge_left_width - d_left) / sponge_left_width)**2
    # Right sponge (supersonic side — STRONG absorption to prevent BH bomb)
    d_right = (L/2) - x[i]
    if d_right < sponge_right_width:
        sponge[i] = max(sponge[i], 20.0 * ((sponge_right_width - d_right) / sponge_right_width)**2)

print(f"  Velocity range: [{v_bg.min():.4f}, {v_bg.max():.4f}]")
print(f"  Horizon region: c_eff²>0 for {np.sum(c_eff_sq > 0)} of {NX} nodes")
sys.stdout.flush()

# ════════════════════════════════════════════════════════════════
# EVOLUTION OPERATORS (vectorized over ensemble)
# ════════════════════════════════════════════════════════════════
# State: phi[NX, N_ens], pi[NX, N_ens]
# Acoustic KG in lab frame (Unruh, 1981):
#   Π = ∂_t φ + v ∂_x φ  (comoving conjugate momentum)
#   ∂_t φ = Π − v ∂_x φ
#   ∂_t Π = c_s² ∂²φ/∂x² − ∂_x(v Π)  − sponge·Π
# 
# Non-separable → use RK4 (not leapfrog).

def spatial_deriv(field):
    """Central finite difference ∂_x on 2D array [NX, N_ens]."""
    d = np.zeros_like(field)
    d[1:-1] = (field[2:] - field[:-2]) / (2 * dx)
    d[0]    = (field[1] - field[0]) / dx
    d[-1]   = (field[-1] - field[-2]) / dx
    return d

def laplacian(field):
    """Second derivative ∂²_x on 2D array [NX, N_ens]."""
    d2 = np.zeros_like(field)
    d2[1:-1] = (field[2:] - 2*field[1:-1] + field[:-2]) / (dx**2)
    d2[0]    = (field[1] - field[0]) / (dx**2)
    d2[-1]   = (field[-2] - field[-1]) / (dx**2)
    return d2

# Broadcast velocity to [NX, 1] for ensemble ops
v_2d     = v_bg[:, None]
dv_2d    = dv_dx[:, None]
sponge_2d = sponge[:, None]
cs2      = C_S**2

def rhs(phi, pi):
    """Full RHS for (φ, Π) system."""
    phi_dot = pi - v_2d * spatial_deriv(phi)
    pi_dot  = cs2 * laplacian(phi) - (v_2d * spatial_deriv(pi) + dv_2d * pi) - sponge_2d * pi
    return phi_dot, pi_dot

def rk4_step(phi, pi, dt):
    """Single RK4 step."""
    k1p, k1q = rhs(phi, pi)
    k2p, k2q = rhs(phi + 0.5*dt*k1p, pi + 0.5*dt*k1q)
    k3p, k3q = rhs(phi + 0.5*dt*k2p, pi + 0.5*dt*k2q)
    k4p, k4q = rhs(phi + dt*k3p, pi + dt*k3q)
    phi_new = phi + (dt/6.0) * (k1p + 2*k2p + 2*k3p + k4p)
    pi_new  = pi  + (dt/6.0) * (k1q + 2*k2q + 2*k3q + k4q)
    return phi_new, pi_new

# ════════════════════════════════════════════════════════════════
# QUANTUM VACUUM INITIALIZATION
# ════════════════════════════════════════════════════════════════
def init_vacuum(rng):
    """Generate vacuum zero-point fluctuations in Fourier space.
    |φ_k|² = 1/(2ω_k), |π_k|² = ω_k/2, for ω_k = c_s|k|.
    Random phases, Gaussian amplitudes."""
    k = np.fft.rfftfreq(NX, d=dx) * 2 * np.pi  # wavenumbers
    omega_k = C_S * np.abs(k)
    omega_k[0] = 1.0  # avoid division by zero at k=0
    
    # Fourier amplitudes
    N_k = len(k)
    
    phi_all = np.zeros((NX, N_ENSEMBLE))
    pi_all  = np.zeros((NX, N_ENSEMBLE))
    
    for n in range(N_ENSEMBLE):
        # Random Gaussian in Fourier space
        re_phi = rng.normal(0, 1, N_k)
        im_phi = rng.normal(0, 1, N_k)
        re_pi  = rng.normal(0, 1, N_k)
        im_pi  = rng.normal(0, 1, N_k)
        
        # Scale by zero-point: φ_k ~ 1/√(2ω), π_k ~ √(ω/2)
        amp_phi = NOISE_SCALE * np.sqrt(1.0 / (2.0 * omega_k + 1e-30))
        amp_pi  = NOISE_SCALE * np.sqrt(omega_k / 2.0)
        
        phi_k = (re_phi + 1j * im_phi) * amp_phi
        pi_k  = (re_pi  + 1j * im_pi)  * amp_pi
        
        # Zero the k=0 mode
        phi_k[0] = 0.0
        pi_k[0]  = 0.0
        
        # Transform to real space
        phi_all[:, n] = np.fft.irfft(phi_k, n=NX)
        pi_all[:, n]  = np.fft.irfft(pi_k, n=NX)
    
    return phi_all, pi_all

# ════════════════════════════════════════════════════════════════
# EVOLUTION (Leapfrog / Störmer-Verlet, symplectic)
# ════════════════════════════════════════════════════════════════
def evolve(phi, pi):
    """Evolve the full ensemble forward using RK4.
    Accumulate δρ correlation after transient."""
    
    idx_corr = np.arange(0, NX, SAMPLE_EVERY)[:N_CORR]
    n_corr = len(idx_corr)
    
    G2 = np.zeros((n_corr, n_corr))
    n_samples = 0
    sample_interval = max(1, N_STEPS // 2000)
    
    t0 = time.time()
    
    for step in range(N_STEPS):
        t = step * DT
        
        # ── RK4 step ──
        phi, pi = rk4_step(phi, pi, DT)
        
        # ── Accumulate correlations (after transient) ──
        if step > STEP_TRANS and step % sample_interval == 0:
            drho = spatial_deriv(phi)
            drho_sub = drho[idx_corr, :]
            G2 += (drho_sub @ drho_sub.T) / N_ENSEMBLE
            n_samples += 1
        
        # Progress
        if step % (N_STEPS // 10) == 0:
            elapsed = time.time() - t0
            phi_max = np.max(np.abs(phi))
            pi_max = np.max(np.abs(pi))
            print(f"    step {step:>7}/{N_STEPS}  t={t:.2f}  "
                  f"|φ|={phi_max:.3e}  |Π|={pi_max:.3e}  "
                  f"el={elapsed:.1f}s", flush=True)
            
            if np.isnan(phi_max) or np.isnan(pi_max) or phi_max > 1e10:
                print("    *** DIVERGENCE detected! Aborting.", flush=True)
                return None, None, None, idx_corr
    
    if n_samples > 0:
        G2 /= n_samples
    
    x_corr = x[idx_corr]
    return G2, x_corr, n_samples, idx_corr

# ════════════════════════════════════════════════════════════════
# FLAT-SPACE REFERENCE (no horizon)
# ════════════════════════════════════════════════════════════════
def evolve_flat(phi, pi):
    """Same evolution but with v=0 (no flow, no horizon).
    This gives the vacuum correlation baseline. For v=0 the system
    is separable so leapfrog is fine (and faster), but use RK4
    for consistency."""
    
    idx_corr = np.arange(0, NX, SAMPLE_EVERY)[:N_CORR]
    n_corr = len(idx_corr)
    G2_flat = np.zeros((n_corr, n_corr))
    n_samples = 0
    sample_interval = max(1, N_STEPS // 2000)
    
    sponge_flat = sponge[:, None]
    
    def rhs_flat(phi_, pi_):
        phi_dot = pi_.copy()
        pi_dot  = cs2 * laplacian(phi_) - sponge_flat * pi_
        return phi_dot, pi_dot
    
    t0 = time.time()
    
    for step in range(N_STEPS):
        t = step * DT
        
        # RK4
        k1p, k1q = rhs_flat(phi, pi)
        k2p, k2q = rhs_flat(phi + 0.5*DT*k1p, pi + 0.5*DT*k1q)
        k3p, k3q = rhs_flat(phi + 0.5*DT*k2p, pi + 0.5*DT*k2q)
        k4p, k4q = rhs_flat(phi + DT*k3p, pi + DT*k3q)
        phi = phi + (DT/6.0) * (k1p + 2*k2p + 2*k3p + k4p)
        pi  = pi  + (DT/6.0) * (k1q + 2*k2q + 2*k3q + k4q)
        
        if step > STEP_TRANS and step % sample_interval == 0:
            drho = spatial_deriv(phi)
            drho_sub = drho[idx_corr, :]
            G2_flat += (drho_sub @ drho_sub.T) / N_ENSEMBLE
            n_samples += 1
        
        if step % (N_STEPS // 10) == 0:
            elapsed = time.time() - t0
            phi_max = np.max(np.abs(phi))
            print(f"    step {step:>7}/{N_STEPS}  t={t:.2f}  "
                  f"|φ|={phi_max:.3e}  el={elapsed:.1f}s", flush=True)
            if np.isnan(phi_max) or phi_max > 1e10:
                print("    *** DIVERGENCE detected! Aborting.", flush=True)
                return None
    
    if n_samples > 0:
        G2_flat /= n_samples
    return G2_flat

# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════
def main():
    rng = np.random.default_rng(seed=42)
    
    # ── Run 1: With acoustic horizon ──
    print(f"\n{'─'*72}")
    print(f"  RUN 1: Acoustic Horizon (v_inf={V_INF}, δ={DELTA})")
    print(f"{'─'*72}", flush=True)
    
    phi_bh, pi_bh = init_vacuum(rng)
    print(f"  Initial: |φ|_max={np.max(np.abs(phi_bh)):.3e}, "
          f"|π|_max={np.max(np.abs(pi_bh)):.3e}", flush=True)
    
    t0 = time.time()
    G2_bh, x_corr, n_samp_bh, idx_corr = evolve(phi_bh, pi_bh)
    dt_bh = time.time() - t0
    
    if G2_bh is None:
        print("  BH run failed (NaN). Exiting.", flush=True)
        return
    
    print(f"  BH run: {dt_bh:.1f}s, {n_samp_bh} correlation samples", flush=True)
    
    # ── Run 2: Flat space reference (same seed) ──
    print(f"\n{'─'*72}")
    print(f"  RUN 2: Flat Space Reference (v=0)")
    print(f"{'─'*72}", flush=True)
    
    rng2 = np.random.default_rng(seed=42)  # same seed
    phi_flat, pi_flat = init_vacuum(rng2)
    
    t0 = time.time()
    G2_flat = evolve_flat(phi_flat, pi_flat)
    dt_flat = time.time() - t0
    
    if G2_flat is None:
        print("  Flat run failed (NaN). Exiting.", flush=True)
        return
    
    print(f"  Flat run: {dt_flat:.1f}s", flush=True)
    
    # ═══════════════════════════════════════════════════════════
    # ANALYSIS
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*72}")
    print("  ANALYSIS")
    print(f"{'='*72}", flush=True)
    
    # Subtracted correlation: Hawking signal = BH − vacuum
    G2_sub = G2_bh - G2_flat
    
    # Find horizon position in correlation grid
    i_horizon = np.argmin(np.abs(x_corr - X_HORIZON))
    
    # Look for off-diagonal structure near horizon
    # Inside horizon: x > x_h (supersonic region, flow carries phonons in)
    # Outside horizon: x < x_h (subsonic region, phonons can escape)
    
    # Cross-horizon correlation: fix x_out in subsonic, scan x_in in supersonic
    margin = 10  # indices away from horizon
    
    # Diagonal value (auto-correlation at horizon)
    diag_bh   = np.diag(G2_bh)
    diag_flat = np.diag(G2_flat)
    diag_sub  = np.diag(G2_sub)
    
    print(f"\n  Horizon at x = {X_HORIZON:.4f} (index {i_horizon})")
    print(f"  G²_BH  diagonal max  = {np.max(diag_bh):.6e}")
    print(f"  G²_flat diagonal max = {np.max(diag_flat):.6e}")
    print(f"  G²_sub diagonal max  = {np.max(np.abs(diag_sub)):.6e}")
    
    # Cross-horizon: pick symmetric points around horizon
    # x_out = x_h - Δ, x_in = x_h + Δ
    print(f"\n  Cross-horizon correlations (subtracted):")
    print(f"  {'Δx':>8} | {'G²_BH':>12} | {'G²_flat':>12} | {'G²_sub':>12} | {'ratio':>8}")
    print(f"  {'─'*8}-+-{'─'*12}-+-{'─'*12}-+-{'─'*12}-+-{'─'*8}")
    
    for delta_i in [2, 5, 10, 20, 30, 50]:
        i_out = i_horizon - delta_i
        i_in  = i_horizon + delta_i
        if 0 <= i_out < len(x_corr) and 0 <= i_in < len(x_corr):
            g_bh  = G2_bh[i_out, i_in]
            g_flat = G2_flat[i_out, i_in]
            g_sub = G2_sub[i_out, i_in]
            ratio = g_bh / (g_flat + 1e-30)
            delta_x = x_corr[i_in] - x_corr[i_out]
            print(f"  {delta_x:8.3f} | {g_bh:12.4e} | {g_flat:12.4e} | "
                  f"{g_sub:12.4e} | {ratio:8.4f}")
    
    # Global maximum of |G²_sub| off-diagonal
    mask = np.ones_like(G2_sub, dtype=bool)
    np.fill_diagonal(mask, False)
    off_diag_sub = G2_sub * mask
    
    max_idx = np.unravel_index(np.argmax(np.abs(off_diag_sub)), G2_sub.shape)
    max_val = G2_sub[max_idx]
    print(f"\n  Max |G²_sub| off-diagonal: {max_val:.6e}")
    print(f"    at x={x_corr[max_idx[0]]:.3f}, x'={x_corr[max_idx[1]]:.3f}")
    
    # Check if max is near horizon crossing
    near_horizon = (abs(max_idx[0] - i_horizon) < len(x_corr)//4 and 
                   abs(max_idx[1] - i_horizon) < len(x_corr)//4)
    
    # Planckian check: extract the cross-horizon correlation as function of Δx
    # and check if its Fourier transform has a thermal spectrum
    n_test = min(60, i_horizon, len(x_corr) - i_horizon)
    deltas = np.arange(1, n_test + 1)
    cross_corr = np.zeros(len(deltas))
    cross_corr_flat = np.zeros(len(deltas))
    
    for idx, di in enumerate(deltas):
        i_out = i_horizon - di
        i_in  = i_horizon + di
        if 0 <= i_out and i_in < len(x_corr):
            cross_corr[idx] = G2_bh[i_out, i_in]
            cross_corr_flat[idx] = G2_flat[i_out, i_in]
    
    cross_sub = cross_corr - cross_corr_flat
    delta_x_arr = deltas * SAMPLE_EVERY * dx
    
    # ═══════════════════════════════════════════════════════════
    # OUTPUT
    # ═══════════════════════════════════════════════════════════
    os.makedirs("UHF_Hawking_v5_results", exist_ok=True)
    
    # Save correlation matrix
    csv_corr = "UHF_Hawking_v5_results/G2_matrix_bh.csv"
    np.savetxt(csv_corr, G2_bh, delimiter=',', fmt='%.8e')
    print(f"\n  Saved: {csv_corr}")
    
    csv_flat = "UHF_Hawking_v5_results/G2_matrix_flat.csv"
    np.savetxt(csv_flat, G2_flat, delimiter=',', fmt='%.8e')
    print(f"  Saved: {csv_flat}")
    
    csv_sub = "UHF_Hawking_v5_results/G2_matrix_subtracted.csv"
    np.savetxt(csv_sub, G2_sub, delimiter=',', fmt='%.8e')
    print(f"  Saved: {csv_sub}")
    
    # Save cross-horizon correlation
    csv_cross = "UHF_Hawking_v5_results/cross_horizon_correlation.csv"
    with open(csv_cross, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['delta_x', 'G2_BH', 'G2_flat', 'G2_subtracted'])
        for i in range(len(deltas)):
            writer.writerow([f"{delta_x_arr[i]:.6f}", f"{cross_corr[i]:.8e}",
                           f"{cross_corr_flat[i]:.8e}", f"{cross_sub[i]:.8e}"])
    print(f"  Saved: {csv_cross}")
    
    # Save x_corr grid
    np.savetxt("UHF_Hawking_v5_results/x_corr.csv", x_corr, delimiter=',', fmt='%.6f')
    
    # ═══════════════════════════════════════════════════════════
    # PLOT (headless matplotlib)
    # ═══════════════════════════════════════════════════════════
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        
        fig, axes = plt.subplots(2, 3, figsize=(20, 12))
        
        # 1. Velocity profile
        ax = axes[0, 0]
        ax.plot(x, v_bg, 'b-', linewidth=1.5)
        ax.axhline(-C_S, color='r', linestyle='--', label=f'-c_s = {-C_S}')
        ax.axhline(C_S,  color='r', linestyle='--', label=f'+c_s = {C_S}')
        ax.axvline(X_HORIZON, color='green', linestyle=':', label=f'horizon x={X_HORIZON:.3f}')
        ax.set_xlabel('x')
        ax.set_ylabel('v(x)')
        ax.set_title('Background Flow Velocity')
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)
        
        # 2. G²_BH correlation matrix
        ax = axes[0, 1]
        vmax = np.percentile(np.abs(G2_bh), 99)
        im = ax.imshow(G2_bh, extent=[x_corr[0], x_corr[-1], x_corr[-1], x_corr[0]],
                       cmap='RdBu_r', vmin=-vmax, vmax=vmax, aspect='auto')
        ax.axhline(X_HORIZON, color='lime', linestyle='--', linewidth=0.8)
        ax.axvline(X_HORIZON, color='lime', linestyle='--', linewidth=0.8)
        plt.colorbar(im, ax=ax, fraction=0.046)
        ax.set_title('G²(x,x\') — BH')
        ax.set_xlabel("x'")
        ax.set_ylabel('x')
        
        # 3. G²_subtracted
        ax = axes[0, 2]
        vmax_s = np.percentile(np.abs(G2_sub), 99)
        if vmax_s < 1e-30:
            vmax_s = 1e-10
        im2 = ax.imshow(G2_sub, extent=[x_corr[0], x_corr[-1], x_corr[-1], x_corr[0]],
                        cmap='RdBu_r', vmin=-vmax_s, vmax=vmax_s, aspect='auto')
        ax.axhline(X_HORIZON, color='lime', linestyle='--', linewidth=0.8)
        ax.axvline(X_HORIZON, color='lime', linestyle='--', linewidth=0.8)
        plt.colorbar(im2, ax=ax, fraction=0.046)
        ax.set_title('G²_BH − G²_flat (Hawking signal)')
        ax.set_xlabel("x'")
        ax.set_ylabel('x')
        
        # 4. Diagonal (auto-correlation)
        ax = axes[1, 0]
        ax.plot(x_corr, diag_bh, 'b-', label='BH', linewidth=1.5)
        ax.plot(x_corr, diag_flat, 'r--', label='Flat', linewidth=1.5)
        ax.plot(x_corr, diag_sub, 'g-', label='Subtracted', linewidth=1.5)
        ax.axvline(X_HORIZON, color='orange', linestyle=':', label='horizon')
        ax.set_xlabel('x')
        ax.set_ylabel('G²(x,x)')
        ax.set_title('Diagonal (Auto-correlation)')
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)
        
        # 5. Cross-horizon correlation
        ax = axes[1, 1]
        ax.plot(delta_x_arr, cross_corr, 'b-o', markersize=3, label='BH', linewidth=1.5)
        ax.plot(delta_x_arr, cross_corr_flat, 'r--s', markersize=3, label='Flat', linewidth=1.5)
        ax.plot(delta_x_arr, cross_sub, 'g-^', markersize=3, label='Subtracted', linewidth=1.5)
        ax.set_xlabel('Δx from horizon')
        ax.set_ylabel('G²(x_h−Δ, x_h+Δ)')
        ax.set_title('Cross-Horizon Correlation')
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)
        
        # 6. Summary text
        ax = axes[1, 2]
        ax.axis('off')
        summary = (
            f"UHF Hawking v5 — Correlation Detection\n"
            f"{'─'*40}\n"
            f"Grid: NX={NX}, L={L}\n"
            f"Velocity: v = -{V_INF}×(1+tanh(x/{DELTA}))/2\n"
            f"Horizon: x_h = {X_HORIZON:.4f}\n"
            f"κ = {KAPPA:.6f}\n"
            f"T_Hawking = {T_HAWKING:.6f}\n"
            f"{'─'*40}\n"
            f"Ensemble: {N_ENSEMBLE} realizations\n"
            f"Evolution: {N_STEPS} steps, T={T_MAX}\n"
            f"Correlation samples: {n_samp_bh}\n"
            f"{'─'*40}\n"
            f"Off-diag |G²_sub| max: {max_val:.4e}\n"
            f"  at x={x_corr[max_idx[0]]:.3f}, x'={x_corr[max_idx[1]]:.3f}\n"
            f"Near horizon: {'YES' if near_horizon else 'NO'}\n"
            f"{'─'*40}\n"
            f"BH time: {dt_bh:.1f}s\n"
            f"Flat time: {dt_flat:.1f}s\n"
        )
        ax.text(0.05, 0.95, summary, transform=ax.transAxes,
                fontsize=11, verticalalignment='top', fontfamily='monospace',
                bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
        
        plt.suptitle('UHF Hawking v5: Density-Density Correlation Across Acoustic Horizon',
                     fontsize=16, fontweight='bold')
        plt.tight_layout()
        plt.savefig("UHF_Hawking_v5_results/hawking_v5_correlation.png",
                    dpi=150, bbox_inches='tight')
        print(f"  Plot: UHF_Hawking_v5_results/hawking_v5_correlation.png")
        plt.close()
    except Exception as e:
        print(f"  Plot failed: {e}")
    
    # ═══════════════════════════════════════════════════════════
    # VERDICT
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*72}")
    
    # Check: is the cross-horizon subtracted signal significantly above noise?
    cross_max = np.max(np.abs(cross_sub))
    cross_noise = np.std(cross_sub[len(cross_sub)//2:])  # far-from-horizon region
    SNR = cross_max / (cross_noise + 1e-30)
    
    # Check: does subtracted off-diagonal have structure near horizon?
    diag_sub_at_h = diag_sub[i_horizon] if i_horizon < len(diag_sub) else 0
    diag_sub_far  = np.mean(np.abs(diag_sub[len(diag_sub)//4:len(diag_sub)//2]))
    enhancement = abs(diag_sub_at_h) / (diag_sub_far + 1e-30)
    
    print(f"  Cross-horizon SNR: {SNR:.4f}")
    print(f"  Horizon auto-correlation enhancement: {enhancement:.4f}×")
    
    if SNR > 3.0 and near_horizon and enhancement > 2.0:
        verdict = "CONFIRMED"
        print(f"\n  ✓ {verdict}: Hawking-like correlation peak detected")
        print(f"    Cross-horizon SNR = {SNR:.2f} > 3")
        print(f"    Enhancement at horizon = {enhancement:.2f}×")
    elif SNR > 1.5:
        verdict = "SUGGESTIVE"
        print(f"\n  ~ {verdict}: Possible Hawking signal (SNR={SNR:.2f})")
    else:
        verdict = "FALSIFIED"
        print(f"\n  ✗ {verdict}: No significant cross-horizon correlation")
        print(f"    SNR = {SNR:.2f}")
    
    print(f"{'='*72}")
    
    # Save summary JSON
    import json
    summary_dict = {
        'NX': NX, 'L': L, 'V_INF': V_INF, 'DELTA': DELTA, 'C_S': C_S,
        'x_horizon': float(X_HORIZON), 'kappa': float(KAPPA),
        'T_hawking': float(T_HAWKING),
        'N_ensemble': N_ENSEMBLE, 'N_steps': N_STEPS,
        'n_correlation_samples': int(n_samp_bh),
        'G2_sub_offdiag_max': float(max_val),
        'cross_horizon_SNR': float(SNR),
        'horizon_enhancement': float(enhancement),
        'verdict': verdict,
        'time_bh_s': float(dt_bh),
        'time_flat_s': float(dt_flat),
    }
    with open("UHF_Hawking_v5_results/results_v5.json", 'w') as f:
        json.dump(summary_dict, f, indent=2)
    print(f"  JSON: UHF_Hawking_v5_results/results_v5.json\n")


if __name__ == "__main__":
    main()
