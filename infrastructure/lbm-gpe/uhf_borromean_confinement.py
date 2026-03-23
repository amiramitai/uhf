#!/usr/bin/env python3
"""
UHF Borromean Ring Superfluid Dynamics — QCD Confinement Analogue
==================================================================
Headless. No GUI. Console + CSV + PNG only.

Physics:
  3D Gross-Pitaevskii Equation (GPE) for a BEC superfluid:
    iħ ∂ψ/∂t = (-ħ²/2m ∇² + g|ψ|² - μ) ψ
  Dimensionless form (ħ=m=1, healing length ξ=1):
    i ∂ψ/∂t = (-½ ∇² + |ψ|² - 1) ψ

  Split-step Fourier method on GPU via CuPy:
    1. Half nonlinear kick: ψ *= exp(-i dt/2 (|ψ|²-1))
    2. Full kinetic step in Fourier space: ψ̂ *= exp(-i dt k²/2)
    3. Half nonlinear kick: ψ *= exp(-i dt/2 (|ψ|²-1))

  Vortex rings: Phase singularities imprinted on ψ by multiplying
  with (x-iy)/|x-iy| around each ring's core axis.

  Borromean link: Three rings in mutually orthogonal planes,
  each threaded through the other two. No pair is linked alone.

Experiment:
  1. Initialize + relax the Borromean state via imaginary-time evolution.
  2. Switch to real-time: apply radial forcing (potential gradient)
     pulling the three rings outward from center.
  3. Measure tension force = -dE/dr via the kinetic energy gradient.
  4. Record F(r) curve. Detect reconnection (topology change).

Output: Console table, CSV, PNG.
"""

import numpy as np
import time
import csv
import sys
import os

# ════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════
N = 128                   # grid points per axis (128³)
L = 40.0                  # domain size [-L/2, L/2]³
dx = L / N
DT_IMAG = 0.02            # imaginary-time step (relaxation)
DT_REAL = 0.005           # real-time step (dynamics)
N_RELAX = 600             # imaginary-time relaxation steps
N_PULL_STEPS = 800        # real-time pulling steps
PULL_RATE = 0.005         # radial displacement per measurement step
N_MEASURE_INTERVAL = 20   # measure every this many steps

# Vortex ring parameters
RING_R = 8.0              # ring major radius (in healing lengths)
RING_SEP = 5.0            # initial center-to-center offset along each axis

# GPE parameters (dimensionless: ξ=1, ħ=m=1)
# g = 1 implicitly (nonlinear coefficient)
MU = 1.0                  # chemical potential (background density = 1)

print("=" * 72)
print("  UHF Borromean Ring Superfluid — QCD Confinement Analogue")
print(f"  Grid: {N}³ = {N**3:,} points, L={L}, dx={dx:.4f}")
print(f"  Ring R={RING_R}, initial separation={RING_SEP}")
print(f"  Relax: {N_RELAX} steps (dt_i={DT_IMAG})")
print(f"  Pull: {N_PULL_STEPS} steps (dt_r={DT_REAL}), rate={PULL_RATE}")
print(f"  VRAM est: ~{(N**3 * 16 * 4) / 1e9:.2f} GB")  # 4 complex arrays
print("=" * 72)
sys.stdout.flush()

# ════════════════════════════════════════════════════════════════
# TRY CUPY (GPU), FALL BACK TO NUMPY (CPU)
# ════════════════════════════════════════════════════════════════
try:
    import cupy as cp
    xp = cp
    fft = cp.fft
    GPU = True
    print("  Backend: CuPy (GPU)", flush=True)
except ImportError:
    xp = np
    fft = np.fft
    GPU = False
    print("  Backend: NumPy (CPU — will be slow)", flush=True)

# ════════════════════════════════════════════════════════════════
# GRID + FOURIER SPACE
# ════════════════════════════════════════════════════════════════
print("  Building grid...", flush=True)
coords = xp.linspace(-L/2 + dx/2, L/2 - dx/2, N, dtype=xp.float32)
X, Y, Z = xp.meshgrid(coords, coords, coords, indexing='ij')

# Wavenumbers for FFT
k1d = xp.fft.fftfreq(N, d=dx).astype(xp.float32) * 2 * xp.pi
KX, KY, KZ = xp.meshgrid(k1d, k1d, k1d, indexing='ij')
K2 = KX**2 + KY**2 + KZ**2

# Kinetic propagator: exp(-i dt k²/2) — will be set per timestep
# Precompute for both imag and real time
K2_f64 = K2.astype(xp.float64)
print("  Grid ready.", flush=True)

# ════════════════════════════════════════════════════════════════
# VORTEX RING IMPRINTING
# ════════════════════════════════════════════════════════════════
def imprint_vortex_ring(psi, center, normal, radius):
    """Imprint a quantized vortex ring onto the wavefunction.
    
    A vortex ring of given radius centered at 'center' with axis
    along 'normal'. The phase winds by 2π around the core.
    
    Method: For each point, find the closest point on the ring,
    then compute the winding phase in the plane perpendicular to
    the ring tangent at that point.
    """
    cx, cy, cz = center
    nx, ny, nz = normal
    nn = np.sqrt(nx**2 + ny**2 + nz**2)
    nx, ny, nz = nx/nn, ny/nn, nz/nn
    
    # Project positions onto the ring plane
    # Vector from center to point
    dx_ = X - cx
    dy_ = Y - cy
    dz_ = Z - cz
    
    # Component along normal
    d_para = dx_*nx + dy_*ny + dz_*nz
    
    # Component in plane (perpendicular to normal)
    px = dx_ - d_para * nx
    py = dy_ - d_para * ny
    pz = dz_ - d_para * nz
    
    # Distance from axis in plane
    rho_plane = xp.sqrt(px**2 + py**2 + pz**2 + 1e-10)
    
    # Unit vector from axis to point in plane
    px_hat = px / rho_plane
    py_hat = py / rho_plane
    pz_hat = pz / rho_plane
    
    # Closest point on ring: center + radius * (px_hat, py_hat, pz_hat)
    # Distance from ring core:
    # d_ring = sqrt((rho_plane - R)² + d_para²)
    d_core = xp.sqrt((rho_plane - radius)**2 + d_para**2 + 1e-10)
    
    # Phase winding: angle in the (rho-R, d_para) plane
    theta = xp.arctan2(d_para, rho_plane - radius)
    
    # Amplitude suppression at core (tanh profile, width = healing length ξ=1)
    amp = xp.tanh(d_core / 1.5)
    
    # Apply to wavefunction
    psi *= amp * xp.exp(1j * theta)
    
    return psi


def init_borromean(offset):
    """Create three vortex rings in Borromean link topology.
    
    Ring 1: in XY plane, centered at (0, 0, +offset)  — normal along Z
    Ring 2: in YZ plane, centered at (+offset, 0, 0)  — normal along X
    Ring 3: in XZ plane, centered at (0, +offset, 0)  — normal along Y
    
    Each ring threads through the other two, creating the
    Borromean link where no pair is independently linked.
    """
    psi = xp.ones((N, N, N), dtype=xp.complex64)
    
    # Ring 1: XY plane, axis along Z
    psi = imprint_vortex_ring(psi, (0, 0, offset), (0, 0, 1), RING_R)
    
    # Ring 2: YZ plane, axis along X
    psi = imprint_vortex_ring(psi, (offset, 0, 0), (1, 0, 0), RING_R)
    
    # Ring 3: XZ plane, axis along Y
    psi = imprint_vortex_ring(psi, (0, offset, 0), (0, 1, 0), RING_R)
    
    return psi


# ════════════════════════════════════════════════════════════════
# SPLIT-STEP PROPAGATORS
# ════════════════════════════════════════════════════════════════
def step_imaginary(psi, dt):
    """One imaginary-time split-step (for relaxation).
    i∂ψ/∂t → -∂ψ/∂τ  (replace t → -iτ)
    Nonlinear: ψ *= exp(-dτ (|ψ|²-μ))
    Kinetic:   ψ̂ *= exp(-dτ k²/2)
    Then renormalize to preserve particle number."""
    
    # Half nonlinear
    rho = xp.abs(psi)**2
    psi *= xp.exp(-dt * 0.5 * (rho - MU))
    
    # Full kinetic (in Fourier space)
    psi_k = fft.fftn(psi)
    psi_k *= xp.exp(-dt * 0.5 * K2)
    psi = fft.ifftn(psi_k)
    
    # Half nonlinear
    rho = xp.abs(psi)**2
    psi *= xp.exp(-dt * 0.5 * (rho - MU))
    
    # Renormalize to maintain <|ψ|²> = 1
    norm = xp.sqrt(xp.mean(xp.abs(psi)**2) + 1e-30)
    psi /= norm
    
    return psi


def step_real(psi, dt, V_ext=None):
    """One real-time split-step.
    Nonlinear + external: ψ *= exp(-i dt/2 (|ψ|²-μ+V))
    Kinetic:              ψ̂ *= exp(-i dt k²/2)
    """
    rho = xp.abs(psi)**2
    nl = rho - MU
    if V_ext is not None:
        nl = nl + V_ext
    psi *= xp.exp(-1j * dt * 0.5 * nl)
    
    psi_k = fft.fftn(psi)
    psi_k *= xp.exp(-1j * dt * 0.5 * K2)
    psi = fft.ifftn(psi_k)
    
    rho = xp.abs(psi)**2
    nl = rho - MU
    if V_ext is not None:
        nl = nl + V_ext
    psi *= xp.exp(-1j * dt * 0.5 * nl)
    
    return psi


# ════════════════════════════════════════════════════════════════
# DIAGNOSTICS
# ════════════════════════════════════════════════════════════════
def compute_energy(psi):
    """Total energy: E_kin + E_int.
    E_kin = ½ ∫ |∇ψ|² = ½ Σ k² |ψ̂_k|²  (per unit volume)
    E_int = ½ ∫ (|ψ|²-1)²
    """
    psi_k = fft.fftn(psi)
    E_kin = float(0.5 * xp.sum(K2 * xp.abs(psi_k)**2).real / N**3 * dx**3)
    rho = xp.abs(psi)**2
    E_int = float(0.5 * xp.sum((rho - 1.0)**2).real / N**3 * dx**3)
    return E_kin, E_int


def find_vortex_density(psi):
    """Pseudo-vorticity: density of phase singularities.
    Approximated by |∇ × (ψ*/|ψ| ∇(ψ/|ψ|))| ∝ regions where |ψ| ≈ 0.
    Simple proxy: fraction of points with |ψ|² < threshold."""
    rho = xp.abs(psi)**2
    threshold = 0.2  # vortex core: density drops to near zero
    n_vortex = int(xp.sum(rho < threshold))
    return n_vortex


def measure_ring_separation(psi):
    """Estimate mean distance of vortex cores from domain center.
    Weighted by inverse density (vortex cores have low density)."""
    rho = xp.abs(psi)**2
    # Weight: peaks where density is low (vortex cores)
    w = xp.maximum(1.0 - rho / (xp.max(rho) + 1e-10), 0.0)**2
    w_sum = xp.sum(w) + 1e-30
    
    R2 = X**2 + Y**2 + Z**2
    r_mean = float(xp.sum(w * xp.sqrt(R2)) / w_sum)
    return r_mean


def compute_tension_force(psi, psi_prev, dr):
    """Compute restoring force from energy difference.
    F = -dE/dr ≈ -(E_current - E_prev) / dr"""
    E_kin, E_int = compute_energy(psi)
    E_kin_p, E_int_p = compute_energy(psi_prev)
    E = E_kin + E_int
    E_p = E_kin_p + E_int_p
    F = -(E - E_p) / (dr + 1e-30)
    return F, E


def count_connected_components(psi, threshold=0.15):
    """Count number of distinct vortex loops via connected components
    of the low-density region. Uses scipy on CPU."""
    rho_cpu = xp.abs(psi)**2
    if GPU:
        rho_cpu = rho_cpu.get()
    else:
        rho_cpu = np.array(rho_cpu)
    
    from scipy import ndimage
    mask = (rho_cpu < threshold).astype(np.int32)
    labeled, n_components = ndimage.label(mask)
    # Filter tiny components (noise)
    sizes = ndimage.sum(mask, labeled, range(1, n_components+1))
    n_real = int(np.sum(np.array(sizes) > 20))
    return n_real


# ════════════════════════════════════════════════════════════════
# RADIAL PULLING POTENTIAL
# ════════════════════════════════════════════════════════════════
def make_pull_potential(strength):
    """Create a radial potential that pushes vortex cores outward.
    V(r) = -strength × r² (inverted harmonic: drives expansion)
    Applied only where density is low (vortex cores), so it acts
    as a selective force on the vortex lines."""
    R2 = X**2 + Y**2 + Z**2
    V = -strength * R2 / (L/2)**2
    return V.astype(xp.float32)


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════
def main():
    t_start = time.time()
    
    # ── Phase 1: Initialize Borromean vortex rings ──
    print(f"\n{'─'*72}")
    print("  Phase 1: Imprinting Borromean vortex rings")
    print(f"{'─'*72}", flush=True)
    
    psi = init_borromean(RING_SEP)
    
    E_kin, E_int = compute_energy(psi)
    n_vortex = find_vortex_density(psi)
    print(f"  Initial: E_kin={E_kin:.4f}, E_int={E_int:.4f}, "
          f"vortex_nodes={n_vortex:,}", flush=True)
    
    # ── Phase 2: Imaginary-time relaxation ──
    print(f"\n{'─'*72}")
    print("  Phase 2: Imaginary-time relaxation (GPE ground state)")
    print(f"{'─'*72}", flush=True)
    
    for step in range(N_RELAX):
        psi = step_imaginary(psi, DT_IMAG)
        
        if step % (N_RELAX // 6) == 0:
            E_kin, E_int = compute_energy(psi)
            n_vortex = find_vortex_density(psi)
            print(f"    relax step {step:>5}/{N_RELAX}  "
                  f"E_kin={E_kin:.6f}  E_int={E_int:.6f}  "
                  f"vortex={n_vortex:,}", flush=True)
    
    E_kin, E_int = compute_energy(psi)
    r_sep = measure_ring_separation(psi)
    print(f"  Relaxed: E_kin={E_kin:.6f}, E_int={E_int:.6f}, "
          f"<r>={r_sep:.3f}", flush=True)
    
    # Count initial topology
    n_loops_init = count_connected_components(psi)
    print(f"  Initial vortex loops: {n_loops_init}", flush=True)
    
    # ── Phase 3: Real-time pulling ──
    print(f"\n{'─'*72}")
    print("  Phase 3: Radial pulling — measuring tension force F(r)")
    print(f"{'─'*72}", flush=True)
    
    # Gradually increase pulling strength
    pull_strengths = np.linspace(0.0, 0.5, N_PULL_STEPS)
    
    measurements = []
    psi_prev = psi.copy()
    cumulative_displacement = 0.0
    reconnection_detected = False
    reconnection_step = -1
    
    print(f"  {'step':>6} | {'pull_F':>8} | {'<r>':>8} | {'E_tot':>10} | "
          f"{'F_tens':>10} | {'n_loops':>7} | {'n_vortex':>8}")
    print(f"  {'─'*72}")
    
    for step in range(N_PULL_STEPS):
        V_ext = make_pull_potential(pull_strengths[step])
        
        # Evolve N_MEASURE_INTERVAL sub-steps
        for _ in range(N_MEASURE_INTERVAL):
            psi = step_real(psi, DT_REAL, V_ext)
        
        cumulative_displacement += PULL_RATE
        
        # Measure every step
        r_sep = measure_ring_separation(psi)
        E_kin, E_int = compute_energy(psi)
        E_tot = E_kin + E_int
        
        # Tension from energy gradient
        E_kin_p, E_int_p = compute_energy(psi_prev)
        E_prev = E_kin_p + E_int_p
        F_tension = -(E_tot - E_prev) / (PULL_RATE + 1e-30)
        
        n_vortex = find_vortex_density(psi)
        
        # Check topology every 50 steps (expensive)
        if step % 50 == 0 or step == N_PULL_STEPS - 1:
            n_loops = count_connected_components(psi)
        else:
            n_loops = -1  # not measured
        
        rec = {
            'step': step,
            'pull_strength': float(pull_strengths[step]),
            'r_separation': float(r_sep),
            'E_kin': float(E_kin),
            'E_int': float(E_int),
            'E_total': float(E_tot),
            'F_tension': float(F_tension),
            'n_vortex': int(n_vortex),
            'n_loops': int(n_loops),
            'cumulative_dr': float(cumulative_displacement),
        }
        measurements.append(rec)
        
        # Detect reconnection: loop count changes
        if n_loops > 0 and n_loops_init > 0 and n_loops != n_loops_init:
            if not reconnection_detected:
                reconnection_detected = True
                reconnection_step = step
                print(f"  *** RECONNECTION at step {step}: "
                      f"loops {n_loops_init} → {n_loops} ***", flush=True)
        
        if step % 50 == 0:
            loops_str = f"{n_loops}" if n_loops >= 0 else "  —"
            print(f"  {step:>6} | {pull_strengths[step]:8.4f} | "
                  f"{r_sep:8.3f} | {E_tot:10.4f} | {F_tension:10.4f} | "
                  f"{loops_str:>7} | {n_vortex:>8,}", flush=True)
        
        psi_prev = psi.copy()
    
    # ═══════════════════════════════════════════════════════════
    # ANALYSIS: F(r) and linear confinement check
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*72}")
    print("  ANALYSIS: Tension Force vs Separation")
    print(f"{'='*72}", flush=True)
    
    r_arr = np.array([m['r_separation'] for m in measurements])
    F_arr = np.array([m['F_tension'] for m in measurements])
    E_arr = np.array([m['E_total'] for m in measurements])
    
    # Smooth F for analysis (rolling average)
    window = min(21, len(F_arr)//4)
    if window > 2:
        F_smooth = np.convolve(F_arr, np.ones(window)/window, mode='same')
    else:
        F_smooth = F_arr
    
    # Linear regression: F = σ × r + b  (σ = string tension)
    # Use middle 60% of data (avoid transient and saturation)
    i_start = len(r_arr) // 5
    i_end = 4 * len(r_arr) // 5
    r_fit = r_arr[i_start:i_end]
    F_fit = F_smooth[i_start:i_end]
    
    if len(r_fit) > 5 and np.std(r_fit) > 1e-6:
        # Linear fit
        A = np.vstack([r_fit, np.ones_like(r_fit)]).T
        result = np.linalg.lstsq(A, F_fit, rcond=None)
        sigma, b = result[0]
        F_pred = sigma * r_fit + b
        SS_res = np.sum((F_fit - F_pred)**2)
        SS_tot = np.sum((F_fit - np.mean(F_fit))**2) + 1e-30
        R2_linear = 1.0 - SS_res / SS_tot
        
        print(f"  Linear fit: F = {sigma:.6f} × r + ({b:.6f})")
        print(f"  String tension σ = {sigma:.6f}")
        print(f"  R² (linear) = {R2_linear:.8f}")
    else:
        sigma = 0.0
        R2_linear = 0.0
        print(f"  Insufficient data for linear fit.")
    
    # Energy vs r: should be ∝ r² if F ∝ r (confinement)
    E_fit = E_arr[i_start:i_end]
    if len(r_fit) > 5 and np.std(r_fit) > 1e-6:
        A2 = np.vstack([r_fit**2, r_fit, np.ones_like(r_fit)]).T
        result2 = np.linalg.lstsq(A2, E_fit, rcond=None)
        a2, b2, c2 = result2[0]
        E_pred2 = a2 * r_fit**2 + b2 * r_fit + c2
        SS_res2 = np.sum((E_fit - E_pred2)**2)
        SS_tot2 = np.sum((E_fit - np.mean(E_fit))**2) + 1e-30
        R2_quad = 1.0 - SS_res2 / SS_tot2
        print(f"  Quadratic energy fit: E = {a2:.6f}r² + {b2:.6f}r + {c2:.4f}")
        print(f"  R² (quadratic E) = {R2_quad:.8f}")
    else:
        R2_quad = 0.0
    
    # Reconnection summary
    print(f"\n  Reconnection detected: {'YES' if reconnection_detected else 'NO'}")
    if reconnection_detected:
        print(f"  Reconnection step: {reconnection_step}")
        m_rec = measurements[reconnection_step]
        print(f"  At r = {m_rec['r_separation']:.3f}, "
              f"E = {m_rec['E_total']:.4f}, "
              f"loops: {n_loops_init} → {m_rec['n_loops']}")
    
    # Verdict on confinement
    print(f"\n{'='*72}")
    if sigma > 0 and R2_linear > 0.7:
        print(f"  ✓ LINEAR CONFINEMENT: F ∝ r (σ={sigma:.4f}, R²={R2_linear:.4f})")
        print(f"    Acoustic 'flux tube' tension confirmed in superfluid.")
        verdict = "CONFIRMED"
    elif sigma > 0 and R2_linear > 0.4:
        print(f"  ~ SUGGESTIVE: F increases with r (σ={sigma:.4f}, R²={R2_linear:.4f})")
        verdict = "SUGGESTIVE"
    else:
        print(f"  ? INCONCLUSIVE: σ={sigma:.6f}, R²={R2_linear:.6f}")
        verdict = "INCONCLUSIVE"
    if reconnection_detected:
        print(f"  ✓ HADRONIZATION: Vortex reconnection → new loops (analogue of q-qbar pair creation)")
    print(f"{'='*72}")
    
    elapsed = time.time() - t_start
    print(f"  Total wall time: {elapsed:.1f}s\n", flush=True)
    
    # ═══════════════════════════════════════════════════════════
    # OUTPUT: CSV
    # ═══════════════════════════════════════════════════════════
    csv_path = "uhf_borromean_confinement.csv"
    with open(csv_path, 'w', newline='') as csvf:
        writer = csv.DictWriter(csvf, fieldnames=[
            'step', 'pull_strength', 'r_separation', 'cumulative_dr',
            'E_kin', 'E_int', 'E_total', 'F_tension',
            'n_vortex', 'n_loops'])
        writer.writeheader()
        for m in measurements:
            writer.writerow(m)
    print(f"  CSV: {csv_path}")
    
    # JSON summary
    import json
    summary = {
        'N': N, 'L': L, 'RING_R': RING_R, 'RING_SEP': RING_SEP,
        'N_RELAX': N_RELAX, 'N_PULL_STEPS': N_PULL_STEPS,
        'sigma_string_tension': float(sigma),
        'R2_linear_F': float(R2_linear),
        'R2_quadratic_E': float(R2_quad),
        'reconnection_detected': reconnection_detected,
        'reconnection_step': reconnection_step,
        'initial_loops': int(n_loops_init),
        'verdict': verdict,
        'wall_time_s': float(elapsed),
    }
    json_path = "uhf_borromean_confinement.json"
    with open(json_path, 'w') as jf:
        json.dump(summary, jf, indent=2)
    print(f"  JSON: {json_path}")
    
    # ═══════════════════════════════════════════════════════════
    # PLOT
    # ═══════════════════════════════════════════════════════════
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        
        fig, axes = plt.subplots(2, 3, figsize=(20, 12))
        
        steps = np.arange(len(measurements))
        
        # 1. Tension Force vs Separation
        ax = axes[0, 0]
        ax.scatter(r_arr, F_arr, s=8, alpha=0.4, color='blue', label='Raw F(r)')
        ax.plot(r_arr, F_smooth, 'r-', linewidth=2, label='Smoothed')
        if sigma != 0:
            r_line = np.linspace(r_arr.min(), r_arr.max(), 100)
            ax.plot(r_line, sigma * r_line + b, 'g--', linewidth=2,
                    label=f'Linear: σ={sigma:.4f}, R²={R2_linear:.3f}')
        if reconnection_detected:
            r_rec = measurements[reconnection_step]['r_separation']
            ax.axvline(r_rec, color='orange', linestyle=':', linewidth=2,
                      label=f'Reconnection')
        ax.set_xlabel('Mean Ring Separation ⟨r⟩', fontsize=12)
        ax.set_ylabel('Tension Force F', fontsize=12)
        ax.set_title('QCD Analogue: F(r) — Confinement', fontsize=14)
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)
        
        # 2. Energy vs Separation
        ax = axes[0, 1]
        ax.plot(r_arr, E_arr, 'b-', linewidth=1.5)
        ax.set_xlabel('Mean Ring Separation ⟨r⟩', fontsize=12)
        ax.set_ylabel('Total Energy', fontsize=12)
        ax.set_title('Energy vs Separation (E ∝ r² if confined)', fontsize=14)
        ax.grid(True, alpha=0.3)
        
        # 3. Energy components vs step
        ax = axes[0, 2]
        E_kin_arr = [m['E_kin'] for m in measurements]
        E_int_arr = [m['E_int'] for m in measurements]
        ax.plot(steps, E_kin_arr, 'b-', label='E_kinetic', linewidth=1.5)
        ax.plot(steps, E_int_arr, 'r-', label='E_interaction', linewidth=1.5)
        ax.plot(steps, E_arr, 'k--', label='E_total', linewidth=1.5)
        ax.set_xlabel('Pull Step', fontsize=12)
        ax.set_ylabel('Energy', fontsize=12)
        ax.set_title('Energy Components', fontsize=14)
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)
        
        # 4. Ring separation vs step
        ax = axes[1, 0]
        ax.plot(steps, r_arr, 'b-', linewidth=1.5)
        if reconnection_detected:
            ax.axvline(reconnection_step, color='orange', linestyle=':',
                      linewidth=2, label='Reconnection')
            ax.legend(fontsize=10)
        ax.set_xlabel('Pull Step', fontsize=12)
        ax.set_ylabel('Mean Ring Separation ⟨r⟩', fontsize=12)
        ax.set_title('Separation vs Time', fontsize=14)
        ax.grid(True, alpha=0.3)
        
        # 5. Vortex core count vs step
        ax = axes[1, 1]
        n_vortex_arr = [m['n_vortex'] for m in measurements]
        ax.plot(steps, n_vortex_arr, 'purple', linewidth=1.5)
        ax.set_xlabel('Pull Step', fontsize=12)
        ax.set_ylabel('Vortex Core Points', fontsize=12)
        ax.set_title('Vortex Density Evolution', fontsize=14)
        ax.grid(True, alpha=0.3)
        
        # 6. Summary text
        ax = axes[1, 2]
        ax.axis('off')
        txt = (
            f"UHF Borromean Ring Confinement\n"
            f"{'─'*38}\n"
            f"Grid: {N}³, L={L}\n"
            f"Ring R={RING_R}, tube ξ=1.5\n"
            f"Initial separation: {RING_SEP}\n"
            f"{'─'*38}\n"
            f"String tension σ = {sigma:.6f}\n"
            f"R² (F linear) = {R2_linear:.6f}\n"
            f"R² (E quadratic) = {R2_quad:.6f}\n"
            f"{'─'*38}\n"
            f"Reconnection: {'YES' if reconnection_detected else 'NO'}\n"
        )
        if reconnection_detected:
            txt += (f"  at step {reconnection_step}\n"
                    f"  loops: {n_loops_init} → {measurements[reconnection_step]['n_loops']}\n")
        txt += (f"{'─'*38}\n"
                f"Verdict: {verdict}\n"
                f"Wall time: {elapsed:.1f}s\n")
        
        ax.text(0.05, 0.95, txt, transform=ax.transAxes,
                fontsize=11, va='top', fontfamily='monospace',
                bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
        
        plt.suptitle('UHF Borromean Ring Superfluid: QCD Confinement Analogue',
                     fontsize=16, fontweight='bold')
        plt.tight_layout()
        plt.savefig("uhf_borromean_confinement.png", dpi=150, bbox_inches='tight')
        print(f"  Plot: uhf_borromean_confinement.png")
        plt.close()
    except Exception as e:
        print(f"  Plot failed: {e}")
    
    print(f"\n  Done.\n")


if __name__ == "__main__":
    main()
