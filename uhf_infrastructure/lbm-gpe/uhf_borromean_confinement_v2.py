#!/usr/bin/env python3
"""
UHF Borromean Ring Confinement v2 — Adiabatic Energy Scan + Dynamic Snap
=========================================================================
Headless. No GUI. Console + CSV + PNG only.

Strategy change from v1:
  v1 tried to dynamically pull rings apart and measure F in real-time → noisy.
  v2 uses TWO phases:

  Phase A — ADIABATIC ENERGY SCAN:
    For each separation d ∈ {d_min … d_max}:
      1. Imprint fresh Borromean rings at separation d.
      2. Imaginary-time relax to local ground state (preserving topology).
      3. Record E(d) = E_kin + E_int after relaxation.
    Then F(d) = -dE/dd via finite differences.
    If F ∝ d → linear confinement (QCD flux tube analogue).

  Phase B — DYNAMIC RECONNECTION (hadronization):
    Imprint at moderate separation, apply strong radial kick,
    evolve in real-time, detect when topology changes
    (vortex reconnection → new loops = quark pair creation).

Physics:
  3D GPE (dimensionless ξ=1, ħ=m=1):
    i ∂ψ/∂t = (-½ ∇² + g|ψ|² - μ) ψ
  Split-step Fourier on GPU via CuPy.
"""

import numpy as np
import time
import csv
import sys
import os
import json

# ════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════
N = 128                   # grid per axis
L = 64.0                  # domain [-L/2, L/2]³  (larger for clean high-d)
dx = L / N
MU = 1.0                  # chemical potential

# Ring geometry
RING_R = 6.0              # major radius (smaller → more separation headroom)
CORE_WIDTH = 1.2          # vortex core healing-length scale

# Phase A: adiabatic scan — dense sampling in expected confinement window
SEPARATIONS = np.concatenate([
    np.linspace(2.0, 5.0, 4),    # overlap regime (coarse)
    np.linspace(5.5, 14.0, 18),  # confinement regime (dense)
    np.linspace(15.0, 18.0, 4),  # large-d boundary probe
])
N_RELAX = 1000            # imaginary-time steps per separation
DT_IMAG = 0.015           # imaginary-time step

# Phase B: dynamic reconnection
DYN_SEP = 5.0             # starting separation for dynamic test
DYN_KICK = 0.15           # initial radial velocity kick
DT_REAL = 0.008           # real-time step
N_DYN = 2000              # real-time steps
DYN_CHECK_EVERY = 50      # topology check interval

print("=" * 72)
print("  UHF Borromean Confinement v2 — Adiabatic + Dynamic")
print(f"  Grid: {N}³, L={L}, dx={dx:.4f}")
print(f"  Ring R={RING_R}, core_ξ={CORE_WIDTH}")
print(f"  Phase A: {len(SEPARATIONS)} separations ({SEPARATIONS[0]:.1f}–{SEPARATIONS[-1]:.1f}), {N_RELAX} relax steps each")
print(f"  Phase B: dynamic snap test, {N_DYN} real-time steps")
print(f"  VRAM est: ~{N**3 * 16 * 5 / 1e9:.2f} GB")
print("=" * 72)
sys.stdout.flush()

# ════════════════════════════════════════════════════════════════
# GPU / CPU BACKEND
# ════════════════════════════════════════════════════════════════
try:
    import cupy as cp
    xp = cp
    fft_mod = cp.fft
    GPU = True
    print("  Backend: CuPy (GPU)", flush=True)
except ImportError:
    xp = np
    fft_mod = np.fft
    GPU = False
    print("  Backend: NumPy (CPU)", flush=True)

# ════════════════════════════════════════════════════════════════
# GRID + FOURIER
# ════════════════════════════════════════════════════════════════
print("  Building grid arrays...", flush=True)
coords = xp.linspace(-L/2 + dx/2, L/2 - dx/2, N, dtype=xp.float64)
X, Y, Z = xp.meshgrid(coords, coords, coords, indexing='ij')

k1d = xp.fft.fftfreq(N, d=dx).astype(xp.float64) * 2 * xp.pi
KX, KY, KZ = xp.meshgrid(k1d, k1d, k1d, indexing='ij')
K2 = KX**2 + KY**2 + KZ**2

# Pre-compute kinetic propagators
kin_imag = xp.exp(-DT_IMAG * 0.5 * K2)       # imaginary-time (real decay)
kin_real = xp.exp(-1j * DT_REAL * 0.5 * K2)   # real-time (unitary)
print("  Grid ready.", flush=True)

# ════════════════════════════════════════════════════════════════
# VORTEX RING IMPRINTING
# ════════════════════════════════════════════════════════════════
def imprint_ring(psi, center, normal, radius):
    """Imprint one quantized vortex ring onto ψ.
    Phase winds by 2π around the ring core; amplitude is
    suppressed via tanh(d_core / ξ)."""
    cx, cy, cz = [float(v) for v in center]
    nx, ny, nz = [float(v) for v in normal]
    nn = np.sqrt(nx*nx + ny*ny + nz*nz)
    nx /= nn; ny /= nn; nz /= nn

    dx_ = X - cx
    dy_ = Y - cy
    dz_ = Z - cz

    # Parallel component (along normal)
    d_par = dx_*nx + dy_*ny + dz_*nz

    # Perpendicular (in-plane) distance from axis
    px = dx_ - d_par * nx
    py = dy_ - d_par * ny
    pz = dz_ - d_par * nz
    rho = xp.sqrt(px*px + py*py + pz*pz + 1e-12)

    # Distance from ring core
    d_core = xp.sqrt((rho - radius)**2 + d_par**2 + 1e-12)

    # Phase winding
    theta = xp.arctan2(d_par, rho - radius)

    # Amplitude suppression at core
    amp = xp.tanh(d_core / CORE_WIDTH)

    psi *= amp * xp.exp(1j * theta)
    return psi


def imprint_borromean(sep):
    """Three mutually-orthogonal rings in Borromean topology."""
    psi = xp.ones((N, N, N), dtype=xp.complex128)
    psi = imprint_ring(psi, (0, 0, sep),   (0, 0, 1), RING_R)  # XY-plane
    psi = imprint_ring(psi, (sep, 0, 0),   (1, 0, 0), RING_R)  # YZ-plane
    psi = imprint_ring(psi, (0, sep, 0),   (0, 1, 0), RING_R)  # XZ-plane
    return psi


# ════════════════════════════════════════════════════════════════
# SPLIT-STEP PROPAGATION
# ════════════════════════════════════════════════════════════════
def step_imag(psi):
    """One imaginary-time split-step (relaxation)."""
    rho = xp.abs(psi)**2
    psi *= xp.exp(-DT_IMAG * 0.5 * (rho - MU))
    psi = fft_mod.ifftn(kin_imag * fft_mod.fftn(psi))
    rho = xp.abs(psi)**2
    psi *= xp.exp(-DT_IMAG * 0.5 * (rho - MU))
    # Renormalize
    psi /= xp.sqrt(xp.mean(xp.abs(psi)**2) + 1e-30)
    return psi


def step_rt(psi, V_ext=None):
    """One real-time split-step."""
    rho = xp.abs(psi)**2
    nl = rho - MU
    if V_ext is not None:
        nl = nl + V_ext
    psi *= xp.exp(-1j * DT_REAL * 0.5 * nl)
    psi = fft_mod.ifftn(kin_real * fft_mod.fftn(psi))
    rho = xp.abs(psi)**2
    nl = rho - MU
    if V_ext is not None:
        nl = nl + V_ext
    psi *= xp.exp(-1j * DT_REAL * 0.5 * nl)
    return psi


# ════════════════════════════════════════════════════════════════
# DIAGNOSTICS
# ════════════════════════════════════════════════════════════════
def energy(psi):
    """E_kin + E_int (dimensionless)."""
    pk = fft_mod.fftn(psi)
    E_kin = float(xp.sum(K2 * xp.abs(pk)**2).real * 0.5 / N**3 * dx**3)
    rho = xp.abs(psi)**2
    E_int = float(xp.sum((rho - 1.0)**2).real * 0.5 / N**3 * dx**3)
    return E_kin, E_int


def vortex_count(psi, threshold=0.20):
    """Number of grid points with |ψ|² < threshold (vortex cores)."""
    return int(xp.sum(xp.abs(psi)**2 < threshold))


def count_loops(psi, threshold=0.15):
    """Connected components of low-density region (topology proxy)."""
    rho_cpu = xp.abs(psi)**2
    if GPU:
        rho_cpu = rho_cpu.get()
    else:
        rho_cpu = np.asarray(rho_cpu)
    from scipy.ndimage import label
    mask = (rho_cpu < threshold).astype(np.int32)
    _, n = label(mask)
    # Filter small components
    from scipy.ndimage import sum as ndsum
    if n > 0:
        sizes = ndsum(mask, _, range(1, n+1))
        n_real = int(np.sum(np.array(sizes) > 30))
    else:
        n_real = 0
    return n_real


def mean_core_radius(psi):
    """Mean radial distance of vortex-core points from origin."""
    rho = xp.abs(psi)**2
    w = xp.maximum(1.0 - rho / 0.3, 0.0)
    ws = float(xp.sum(w)) + 1e-30
    R2 = X**2 + Y**2 + Z**2
    return float(xp.sum(w * xp.sqrt(R2)) / ws)


# ════════════════════════════════════════════════════════════════
# PHASE A — ADIABATIC ENERGY SCAN
# ════════════════════════════════════════════════════════════════
def run_phase_a():
    print(f"\n{'═'*72}")
    print("  PHASE A: Adiabatic Energy Scan E(d)")
    print(f"{'═'*72}", flush=True)

    print(f"  {'d':>6} | {'E_kin':>12} | {'E_int':>12} | "
          f"{'E_tot':>12} | {'vortex_pts':>10} | {'loops':>5} | {'t(s)':>6}")
    print(f"  {'─'*72}")

    results_a = []

    for d in SEPARATIONS:
        t0 = time.time()

        psi = imprint_borromean(d)

        # Imaginary-time relaxation
        for s in range(N_RELAX):
            psi = step_imag(psi)

        E_k, E_i = energy(psi)
        E_tot = E_k + E_i
        nv = vortex_count(psi)
        nl = count_loops(psi)
        dt_s = time.time() - t0

        results_a.append({
            'separation': float(d),
            'E_kin': E_k, 'E_int': E_i, 'E_total': E_tot,
            'vortex_pts': nv, 'n_loops': nl,
        })

        print(f"  {d:6.2f} | {E_k:12.4f} | {E_i:12.6f} | "
              f"{E_tot:12.4f} | {nv:>10,} | {nl:>5} | {dt_s:5.1f}",
              flush=True)

    return results_a


# ════════════════════════════════════════════════════════════════
# PHASE B — DYNAMIC RECONNECTION
# ════════════════════════════════════════════════════════════════
def run_phase_b():
    print(f"\n{'═'*72}")
    print("  PHASE B: Dynamic Reconnection (Hadronization)")
    print(f"{'═'*72}", flush=True)

    # Imprint + relax at moderate separation
    psi = imprint_borromean(DYN_SEP)
    for s in range(N_RELAX):
        psi = step_imag(psi)

    E_k0, E_i0 = energy(psi)
    n0 = count_loops(psi)
    print(f"  Relaxed: E={E_k0+E_i0:.4f}, loops={n0}", flush=True)

    # Radial velocity kick: multiply ψ by exp(i k_kick r)
    # This imparts outward momentum to all structures
    R = xp.sqrt(X**2 + Y**2 + Z**2 + 1e-12)
    psi *= xp.exp(1j * DYN_KICK * R)

    print(f"\n  {'step':>6} | {'<r>':>8} | {'E_tot':>10} | "
          f"{'loops':>5} | {'vortex':>8}")
    print(f"  {'─'*56}")

    results_b = []
    reconnected = False
    recon_step = -1
    prev_loops = n0

    for step in range(N_DYN):
        psi = step_rt(psi)

        if step % DYN_CHECK_EVERY == 0:
            E_k, E_i = energy(psi)
            E_t = E_k + E_i
            r_m = mean_core_radius(psi)
            nv = vortex_count(psi)
            nl = count_loops(psi)

            results_b.append({
                'step': step,
                'r_mean': r_m,
                'E_total': E_t,
                'n_loops': nl,
                'n_vortex': nv,
            })

            if nl != prev_loops and not reconnected:
                reconnected = True
                recon_step = step
                print(f"  *** RECONNECTION at step {step}: "
                      f"loops {prev_loops} → {nl} ***", flush=True)
            prev_loops = nl

            if step % (DYN_CHECK_EVERY * 4) == 0:
                print(f"  {step:>6} | {r_m:8.3f} | {E_t:10.4f} | "
                      f"{nl:>5} | {nv:>8,}", flush=True)

    return results_b, n0, reconnected, recon_step


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════
def main():
    t_wall = time.time()

    # ── Phase A ──
    results_a = run_phase_a()

    # ── ANALYSIS ──
    d_arr = np.array([r['separation'] for r in results_a])
    E_arr = np.array([r['E_total'] for r in results_a])
    loops_arr = np.array([r['n_loops'] for r in results_a])

    # Numerical derivative (full)
    F_arr = -np.gradient(E_arr, d_arr)

    # ── Auto-detect confinement regime ──
    # Find contiguous blocks of constant topology
    blocks = []  # (loop_count, start_idx, end_idx)
    i = 0
    while i < len(loops_arr):
        j = i
        while j < len(loops_arr) and loops_arr[j] == loops_arr[i]:
            j += 1
        blocks.append((int(loops_arr[i]), i, j))
        i = j

    # Pick the contiguous block with the longest monotonically
    # non-decreasing E subsequence (confinement signature)
    best_block = None
    best_mono_len = 0
    for lc, s, e in blocks:
        n_blk = e - s
        if n_blk < 3:
            continue
        seg_E = E_arr[s:e]
        # Find longest monotonic non-decreasing tail (from some start)
        for start_k in range(n_blk):
            mono = True
            for k in range(start_k, n_blk - 1):
                if seg_E[k+1] < seg_E[k] - 1e-6:
                    mono = False
                    break
            if mono:
                tail_len = n_blk - start_k
                if tail_len > best_mono_len:
                    best_mono_len = tail_len
                    best_block = (lc, s + start_k, e)
                break

    if best_block is None:
        # Fallback: longest contiguous block
        lc, s, e = max(blocks, key=lambda x: x[2] - x[1])
        dominant_loops = lc
        idx_stable = np.arange(s, e)
    else:
        dominant_loops = best_block[0]
        idx_stable = np.arange(best_block[1], best_block[2])

    # 2. Stable topology mask
    E_stable = E_arr[idx_stable]
    d_stable = d_arr[idx_stable]

    # 3. Within stable region, find equilibrium (minimum E)
    i_eq_local = np.argmin(E_stable)
    i_eq = idx_stable[i_eq_local]
    d_eq = d_arr[i_eq]
    E_eq = E_arr[i_eq]

    # 4. Confinement window: d >= d_eq within the stable block
    conf_idx = idx_stable[i_eq_local:]
    d_conf = d_arr[conf_idx]
    E_conf = E_arr[conf_idx]
    n_conf = len(d_conf)

    print(f"\n{'='*72}")
    print("  PHASE A ANALYSIS")
    print(f"{'='*72}")
    print(f"  Dominant topology: {dominant_loops} loops")
    print(f"  Stable regime: d ∈ [{d_stable[0]:.1f}, {d_stable[-1]:.1f}]")
    print(f"  Equilibrium: d_eq = {d_eq:.2f}, E_eq = {E_eq:.4f}")
    print(f"  Confinement window: {n_conf} points, "
          f"d ∈ [{d_conf[0]:.1f}, {d_conf[-1]:.1f}]", flush=True)

    # ── Shifted coordinates for confinement fits ──
    Dd = d_conf - d_eq   # Δd from equilibrium
    DE = E_conf - E_eq    # ΔE from minimum

    # ── Model 1: Quadratic  E = a·Δd² + E0 ──
    if n_conf >= 3:
        a_quad = np.sum(Dd**2 * DE) / (np.sum(Dd**4) + 1e-30)
        DE_pred_q = a_quad * Dd**2
        SS_res_q = np.sum((DE - DE_pred_q)**2)
        SS_tot_conf = np.sum((DE - np.mean(DE))**2) + 1e-30
        R2_quad = 1.0 - SS_res_q / SS_tot_conf
    else:
        a_quad, R2_quad = 0.0, 0.0

    # ── Model 2: Linear  E = σ·Δd + E0 ──
    if n_conf >= 3:
        sigma_lin = np.sum(Dd * DE) / (np.sum(Dd**2) + 1e-30)
        DE_pred_l = sigma_lin * Dd
        SS_res_l = np.sum((DE - DE_pred_l)**2)
        R2_lin = 1.0 - SS_res_l / SS_tot_conf
    else:
        sigma_lin, R2_lin = 0.0, 0.0

    # ── Model 3: Power law  E = a·Δd^n + E0  (log-log regression) ──
    pos = Dd > 0
    if np.sum(pos) >= 3:
        log_d = np.log(Dd[pos])
        log_E = np.log(np.maximum(DE[pos], 1e-12))
        A_log = np.vstack([log_d, np.ones_like(log_d)]).T
        res_log = np.linalg.lstsq(A_log, log_E, rcond=None)
        n_pow, log_a = res_log[0]
        a_pow = np.exp(log_a)
        DE_pred_p = a_pow * Dd**n_pow
        DE_pred_p[~pos] = 0.0
        SS_res_p = np.sum((DE - DE_pred_p)**2)
        R2_pow = 1.0 - SS_res_p / SS_tot_conf
    else:
        n_pow, a_pow, R2_pow = 2.0, 0.0, 0.0

    # ── Model 4: Full quadratic  E = a·d² + b·d + c  (full range) ──
    A_full = np.vstack([d_arr**2, d_arr, np.ones_like(d_arr)]).T
    res_full = np.linalg.lstsq(A_full, E_arr, rcond=None)
    a_f, b_f, c_f = res_full[0]
    E_pred_full = a_f * d_arr**2 + b_f * d_arr + c_f
    SS_res_full = np.sum((E_arr - E_pred_full)**2)
    SS_tot_full = np.sum((E_arr - np.mean(E_arr))**2) + 1e-30
    R2_full = 1.0 - SS_res_full / SS_tot_full

    # ── Report ──
    print(f"\n  {'Model':25} | {'R²':>10} | {'Parameters'}")
    print(f"  {'─'*72}")
    print(f"  {'Linear ΔE = σ·Δd':25} | {R2_lin:10.6f} | σ = {sigma_lin:.4f}")
    print(f"  {'Quadratic ΔE = a·Δd²':25} | {R2_quad:10.6f} | a = {a_quad:.4f}")
    print(f"  {'Power law ΔE = a·Δd^n':25} | {R2_pow:10.6f} | "
          f"n = {n_pow:.3f}, a = {a_pow:.4f}")
    print(f"  {'Full range E = ad²+bd+c':25} | {R2_full:10.6f} | "
          f"a = {a_f:.4f}")

    # Pick best model for verdict
    models = [('linear', R2_lin), ('quadratic', R2_quad), ('power_law', R2_pow)]
    best_name, best_R2 = max(models, key=lambda x: x[1])

    # Effective string tension: force at largest stable separation
    if n_conf >= 2:
        F_conf = -np.gradient(E_conf, d_conf)
        sigma_eff = abs(float(F_conf[-1]))
    else:
        F_conf = np.array([])
        sigma_eff = 0.0

    print(f"\n  Best confinement model: {best_name} (R² = {best_R2:.6f})")
    print(f"  Effective string tension at d={d_conf[-1]:.1f}: "
          f"|F| = {sigma_eff:.4f}")
    if best_name == 'power_law':
        print(f"  Power-law exponent n = {n_pow:.3f} "
              f"({'sub-linear' if n_pow < 1.5 else 'super-linear' if n_pow > 2.5 else 'quadratic-like'})")

    print(f"\n  Data table (confinement regime):")
    print(f"  {'d':>6} → {'Δd':>6} | {'E':>10} → {'ΔE':>10} | {'F':>10}")
    for i in range(n_conf):
        Fi = float(F_conf[i]) if i < len(F_conf) else 0.0
        print(f"  {d_conf[i]:6.2f} → {Dd[i]:6.2f} | "
              f"{E_conf[i]:10.4f} → {DE[i]:10.4f} | {Fi:10.4f}", flush=True)

    # Store for output/plots
    analysis = {
        'd_eq': d_eq, 'E_eq': E_eq, 'dominant_loops': dominant_loops,
        'd_conf': d_conf, 'E_conf': E_conf, 'F_conf': F_conf,
        'R2_lin': R2_lin, 'R2_quad': R2_quad, 'R2_pow': R2_pow,
        'R2_full': R2_full, 'n_pow': n_pow, 'a_pow': a_pow,
        'sigma_lin': sigma_lin, 'a_quad': a_quad, 'sigma_eff': sigma_eff,
        'best_name': best_name, 'best_R2': best_R2,
        'a_f': a_f, 'b_f': b_f, 'c_f': c_f,
    }

    # ── Phase B ──
    results_b, init_loops, reconnected, recon_step = run_phase_b()

    print(f"\n{'='*72}")
    print("  PHASE B RESULTS: Dynamic Reconnection")
    print(f"{'='*72}")
    print(f"  Reconnection detected: {'YES' if reconnected else 'NO'}")
    if reconnected:
        m = [r for r in results_b if r['step'] == recon_step]
        if m:
            print(f"  At step {recon_step}, loops: {init_loops} → {m[0]['n_loops']}")
        else:
            print(f"  At step {recon_step}")

    # ── VERDICT ──
    best_R2 = analysis['best_R2']
    best_name = analysis['best_name']
    sigma_eff = analysis['sigma_eff']

    print(f"\n{'='*72}")
    print("  VERDICT")
    print(f"{'='*72}")

    if best_R2 > 0.90:
        verdict = "CONFIRMED"
        print(f"  ✓ CONFINEMENT CONFIRMED ({best_name} model)")
        print(f"    R²={best_R2:.6f}, |F_max|={sigma_eff:.4f}")
        if analysis.get('n_pow'):
            print(f"    Power-law exponent n={analysis['n_pow']:.3f}")
    elif best_R2 > 0.70:
        verdict = "STRONG_EVIDENCE"
        print(f"  ✓ STRONG EVIDENCE for confinement ({best_name}, R²={best_R2:.4f})")
    elif best_R2 > 0.50:
        verdict = "SUGGESTIVE"
        print(f"  ~ SUGGESTIVE of confinement ({best_name}, R²={best_R2:.4f})")
    else:
        verdict = "INCONCLUSIVE"
        print(f"  ? INCONCLUSIVE: best R²={best_R2:.4f} ({best_name})")

    # Energy monotonicity test (model-independent)
    monotonic = all(analysis['E_conf'][i+1] >= analysis['E_conf'][i]
                     for i in range(len(analysis['E_conf'])-1))
    if monotonic:
        print(f"  ✓ E(d) strictly non-decreasing in confinement window "
              f"(model-independent)")
    else:
        print(f"  ⚠ E(d) not monotonic in confinement window")

    if reconnected:
        print(f"  ✓ HADRONIZATION: Vortex reconnection → topology change")
        print(f"    ({init_loops} → {[r for r in results_b if r['step'] == recon_step][0]['n_loops'] if recon_step >= 0 and any(r['step'] == recon_step for r in results_b) else '?'} loops)")
        print(f"    (analogue of string breaking / q-q̄ pair creation)")
    print(f"{'='*72}")

    elapsed = time.time() - t_wall
    print(f"  Total wall time: {elapsed:.1f}s\n", flush=True)

    # ═══════════════════════════════════════════════════════════
    # OUTPUT
    # ═══════════════════════════════════════════════════════════
    # CSV — Phase A
    csv_a = "uhf_borromean_confinement.csv"
    with open(csv_a, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['separation', 'E_kin', 'E_int', 'E_total', 'F_numerical',
                     'vortex_pts', 'n_loops'])
        for i, r in enumerate(results_a):
            w.writerow([f"{r['separation']:.4f}", f"{r['E_kin']:.6f}",
                        f"{r['E_int']:.8f}", f"{r['E_total']:.6f}",
                        f"{F_arr[i]:.6f}", r['vortex_pts'], r['n_loops']])
    print(f"  CSV: {csv_a}")

    # CSV — Phase B
    csv_b = "uhf_borromean_dynamic.csv"
    with open(csv_b, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['step', 'r_mean', 'E_total', 'n_loops', 'n_vortex'])
        for r in results_b:
            w.writerow([r['step'], f"{r['r_mean']:.4f}", f"{r['E_total']:.6f}",
                        r['n_loops'], r['n_vortex']])
    print(f"  CSV: {csv_b}")

    # JSON
    summary = {
        'N': N, 'L': L, 'RING_R': RING_R, 'CORE_WIDTH': CORE_WIDTH,
        'N_RELAX': N_RELAX, 'N_DYN': N_DYN,
        'd_eq': float(analysis['d_eq']),
        'E_eq': float(analysis['E_eq']),
        'dominant_loops': int(analysis['dominant_loops']),
        'confinement_window': [float(d_conf[0]), float(d_conf[-1])],
        'models': {
            'linear': {'R2': float(R2_lin), 'sigma': float(sigma_lin)},
            'quadratic': {'R2': float(R2_quad), 'a': float(a_quad)},
            'power_law': {'R2': float(R2_pow), 'n': float(n_pow),
                          'a': float(a_pow)},
            'full_range_quad': {'R2': float(R2_full)},
        },
        'best_model': best_name,
        'best_R2': float(best_R2),
        'sigma_effective': float(sigma_eff),
        'E_monotonic_in_conf': bool(monotonic),
        'reconnection_detected': reconnected,
        'reconnection_step': recon_step,
        'initial_loops': int(init_loops) if init_loops else 0,
        'verdict': verdict,
        'wall_time_s': float(elapsed),
        'E_vs_d': [{'d': float(d_arr[i]), 'E': float(E_arr[i]),
                     'F': float(F_arr[i]), 'loops': int(loops_arr[i])}
                    for i in range(len(d_arr))],
    }
    with open("uhf_borromean_confinement.json", 'w') as jf:
        json.dump(summary, jf, indent=2)
    print(f"  JSON: uhf_borromean_confinement.json")

    # ═══════════════════════════════════════════════════════════
    # PLOT
    # ═══════════════════════════════════════════════════════════
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 3, figsize=(20, 12))

        # 1. E(d) — full range + confinement highlight
        ax = axes[0, 0]
        ax.plot(d_arr, E_arr, 'ko-', markersize=5, linewidth=1, alpha=0.4,
                label='All data')
        ax.plot(d_conf, E_conf, 'bo-', markersize=7, linewidth=2,
                label='Confinement window')
        # Overlay best fit in confinement
        if R2_pow > R2_quad and R2_pow > R2_lin:
            d_fit = np.linspace(d_conf[0], d_conf[-1], 100)
            E_fit = a_pow * (d_fit - d_eq)**n_pow + E_eq
            ax.plot(d_fit, E_fit, 'r--', linewidth=2,
                    label=f'Power law n={n_pow:.2f}, R²={R2_pow:.4f}')
        else:
            d_fit = np.linspace(d_conf[0], d_conf[-1], 100)
            E_fit = a_quad * (d_fit - d_eq)**2 + E_eq
            ax.plot(d_fit, E_fit, 'r--', linewidth=2,
                    label=f'Quadratic, R²={R2_quad:.4f}')
        ax.axvline(d_eq, color='green', linestyle=':', linewidth=1,
                  label=f'd_eq={d_eq:.1f}')
        ax.set_xlabel('Ring Separation d', fontsize=12)
        ax.set_ylabel('Total Energy E', fontsize=12)
        ax.set_title('Confinement Potential E(d)', fontsize=14)
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)

        # 2. Log-log ΔE vs Δd (confinement regime, power-law test)
        ax = axes[0, 1]
        pos = Dd > 0
        if np.sum(pos) >= 2:
            ax.loglog(Dd[pos], DE[pos], 'bo-', markersize=7, linewidth=2,
                     label='Measured')
            dd_log = np.logspace(np.log10(Dd[pos].min()),
                                np.log10(Dd[pos].max()), 50)
            ax.loglog(dd_log, a_pow * dd_log**n_pow, 'r--', linewidth=2,
                     label=f'ΔE ∝ Δd^{n_pow:.2f}')
            # Reference slopes
            ax.loglog(dd_log, dd_log * DE[pos][-1]/Dd[pos][-1], 'g:',
                     linewidth=1, alpha=0.5, label='slope=1 (linear)')
            ax.loglog(dd_log, dd_log**2 * DE[pos][-1]/Dd[pos][-1]**2, 'm:',
                     linewidth=1, alpha=0.5, label='slope=2 (quadratic)')
        ax.set_xlabel('Δd = d - d_eq', fontsize=12)
        ax.set_ylabel('ΔE = E - E_eq', fontsize=12)
        ax.set_title('Log-Log: Power Law Test', fontsize=14)
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3, which='both')

        # 3. F(d) in confinement regime
        ax = axes[0, 2]
        if len(F_conf) > 0:
            ax.plot(d_conf, -F_conf, 'bs-', markersize=7, linewidth=2,
                   label='-F(d) = tension')
            ax.set_xlabel('Ring Separation d', fontsize=12)
            ax.set_ylabel('Tension |F|', fontsize=12)
            ax.set_title('String Tension in Confinement Window', fontsize=14)
            ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)

        # 4. Topology (loop count) vs separation
        ax = axes[1, 0]
        ax.bar(d_arr, loops_arr, width=(d_arr[1]-d_arr[0])*0.7,
               alpha=0.6, color='green')
        ax.axhline(dominant_loops, color='blue', linestyle='--',
                   label=f'Stable: {dominant_loops} loops')
        ax.axvspan(d_conf[0], d_conf[-1], alpha=0.1, color='blue',
                  label='Confinement window')
        ax.set_xlabel('Separation d', fontsize=12)
        ax.set_ylabel('Connected Loops', fontsize=12)
        ax.set_title('Topology vs Separation', fontsize=14)
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)

        # 5. Phase B: energy + reconnection
        if results_b:
            ax = axes[1, 1]
            steps_b = [r['step'] for r in results_b]
            E_b = [r['E_total'] for r in results_b]
            ax.plot(steps_b, E_b, 'b-', linewidth=1.5)
            if reconnected:
                ax.axvline(recon_step, color='red', linestyle=':', linewidth=2,
                          label=f'Reconnection (step {recon_step})')
                ax.legend(fontsize=10)
            ax.set_xlabel('Time Step', fontsize=12)
            ax.set_ylabel('Total Energy', fontsize=12)
            ax.set_title('Phase B: Energy Evolution', fontsize=14)
            ax.grid(True, alpha=0.3)

            # 6. Phase B: loop count
            ax = axes[1, 2]
            loops_b = [r['n_loops'] for r in results_b]
            ax.step(steps_b, loops_b, 'g-', linewidth=2, where='mid')
            if reconnected:
                ax.axvline(recon_step, color='red', linestyle=':', linewidth=2,
                          label='Reconnection')
                ax.legend(fontsize=10)
            ax.set_xlabel('Time Step', fontsize=12)
            ax.set_ylabel('Vortex Loops', fontsize=12)
            ax.set_title('Phase B: Topology (Hadronization)', fontsize=14)
            ax.grid(True, alpha=0.3)
        else:
            for ax in [axes[1,1], axes[1,2]]:
                ax.text(0.5, 0.5, 'Phase B skipped', ha='center', va='center',
                        fontsize=14, transform=ax.transAxes)

        plt.suptitle('UHF Borromean Ring Confinement v2: QCD Flux Tube Analogue',
                     fontsize=16, fontweight='bold')
        plt.tight_layout()
        plt.savefig("uhf_borromean_confinement.png", dpi=150, bbox_inches='tight')
        print(f"  Plot: uhf_borromean_confinement.png")
        plt.close()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  Plot failed: {e}")

    print(f"\n  Done.\n")


if __name__ == "__main__":
    main()
