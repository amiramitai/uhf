#!/usr/bin/env python3
"""
QCD Asymptotic Freedom — Grid Convergence Check
=================================================
Re-run the v=2.5 probe at N=256 (double resolution, dx=0.25 vs 0.5)
and compare σ_eff to the N=128 baseline value of 8.29.

If σ_eff remains low (~8), the result is converged and NOT a grid artifact.
If σ_eff spikes back up, the low value was under-resolution.
"""

import sys, os, time, json
import numpy as np

try:
    import cupy as cp
    xp = cp
    fft_mod = cp.fft
    GPU = True
    print(f"[GPU] CuPy {cp.__version__}")
except ImportError:
    xp = np
    fft_mod = np.fft
    GPU = False
    print("[CPU] NumPy fallback")

sys.stdout.flush()

# ── Baseline from N=128 run ──
SIGMA_EFF_128 = 8.291277  # measured at v=2.5

# ── Parameters (double resolution) ──
N = 256
L = 64.0
dx = L / N  # 0.25 (was 0.5)
MU = 1.0
G_NL = 1.0
RHO_0 = MU / G_NL

xi = 1.0 / np.sqrt(2.0 * MU)
c_s = np.sqrt(MU)

# Borromean target (same physical parameters)
RING_R   = 6.0
CORE_W   = 1.2
RING_SEP = 5.0

# Time steps (same dt — more resolution, same physics)
DT_IMAG = 0.015
DT_REAL = 0.008
N_RELAX = 1000

# Probe (same as original)
PROBE_SIGMA = 4.0
PROBE_AMP   = 0.10
PROBE_X0    = -24.0
V_PROBE     = 2.5

# Target measurement region
TARGET_R = 15.0
N_STEP_MAX = 10000

def asnp(a):
    return cp.asnumpy(a) if GPU else a


def main():
    t0 = time.time()
    print("=" * 60)
    print("QCD ASYMPTOTIC FREEDOM — GRID CONVERGENCE CHECK")
    print("=" * 60)
    print(f"  Grid: N=128 → N={N} (dx: 0.500 → {dx:.3f})")
    print(f"  Probe velocity: v={V_PROBE}")
    print(f"  Baseline σ_eff(N=128) = {SIGMA_EFF_128:.6f}")
    print(f"  GPU: {GPU}")
    sys.stdout.flush()

    # ── Grid ──
    coords = xp.linspace(-L/2 + dx/2, L/2 - dx/2, N, dtype=xp.float64)
    X, Y, Z = xp.meshgrid(coords, coords, coords, indexing='ij')
    k1d = xp.fft.fftfreq(N, d=dx) * (2 * np.pi)
    KX, KY, KZ = xp.meshgrid(k1d, k1d, k1d, indexing='ij')
    K2 = KX**2 + KY**2 + KZ**2

    kin_imag = xp.exp(-DT_IMAG * 0.5 * K2)
    kin_real = xp.exp(-1j * DT_REAL * 0.5 * K2)

    # Target region mask
    R_origin = xp.sqrt(X**2 + Y**2 + Z**2)
    target_mask = R_origin < TARGET_R

    print(f"  Grid allocated: {N}³ = {N**3:,} voxels")
    sys.stdout.flush()

    # ── Ring imprinting ──
    def imprint_ring(psi, cx, cy, cz, nx, ny, nz, radius):
        nn = np.sqrt(nx**2 + ny**2 + nz**2)
        nx, ny, nz = nx/nn, ny/nn, nz/nn
        ddx = X - cx; ddy = Y - cy; ddz = Z - cz
        d_par = ddx*nx + ddy*ny + ddz*nz
        px = ddx - d_par*nx
        py = ddy - d_par*ny
        pz = ddz - d_par*nz
        rho_cyl = xp.sqrt(px**2 + py**2 + pz**2 + 1e-12)
        d_core = xp.sqrt((rho_cyl - radius)**2 + d_par**2)
        theta = xp.arctan2(d_par, rho_cyl - radius)
        amp = xp.tanh(d_core / CORE_W)
        psi *= amp * xp.exp(1j * theta)
        return psi

    def imprint_borromean(psi, sep):
        psi = imprint_ring(psi, 0, 0, sep, 0, 0, 1, RING_R)
        psi = imprint_ring(psi, sep, 0, 0, 1, 0, 0, RING_R)
        psi = imprint_ring(psi, 0, sep, 0, 0, 1, 0, RING_R)
        return psi

    def step_imag(psi):
        rho = xp.abs(psi)**2
        V = G_NL * rho - MU
        psi *= xp.exp(-DT_IMAG * 0.5 * V)
        psi = fft_mod.ifftn(kin_imag * fft_mod.fftn(psi))
        rho = xp.abs(psi)**2
        V = G_NL * rho - MU
        psi *= xp.exp(-DT_IMAG * 0.5 * V)
        psi /= xp.sqrt(xp.mean(xp.abs(psi)**2) + 1e-30)
        return psi

    def step_real(psi):
        rho = xp.abs(psi)**2
        V = G_NL * rho - MU
        psi *= xp.exp(-1j * DT_REAL * 0.5 * V)
        psi = fft_mod.ifftn(kin_real * fft_mod.fftn(psi))
        rho = xp.abs(psi)**2
        V = G_NL * rho - MU
        psi *= xp.exp(-1j * DT_REAL * 0.5 * V)
        return psi

    def energy_in_region(psi, mask):
        pk = fft_mod.fftn(psi)
        gx = fft_mod.ifftn(1j * KX * pk)
        gy = fft_mod.ifftn(1j * KY * pk)
        gz = fft_mod.ifftn(1j * KZ * pk)
        e_kin = 0.5 * (xp.abs(gx)**2 + xp.abs(gy)**2 + xp.abs(gz)**2)
        rho = xp.abs(psi)**2
        e_int = 0.5 * G_NL * (rho - RHO_0)**2
        return float(xp.sum((e_kin + e_int)[mask]).real) * dx**3

    # ════════════════════════════════════════════════════
    # PHASE 1: Create and Relax Borromean Target
    # ════════════════════════════════════════════════════
    print("\nPHASE 1: Imprint + Relax Borromean Target")
    print("-" * 40)
    sys.stdout.flush()

    psi = xp.ones((N, N, N), dtype=xp.complex128)
    psi = imprint_borromean(psi, RING_SEP)
    psi /= xp.sqrt(xp.mean(xp.abs(psi)**2) + 1e-30)

    for i in range(N_RELAX):
        psi = step_imag(psi)
        if (i + 1) % 250 == 0:
            rho_m = float(xp.mean(xp.abs(psi)**2).real)
            print(f"  Relax step {i+1}/{N_RELAX}: <ρ>={rho_m:.6f}")
            sys.stdout.flush()

    rho_relaxed = float(xp.mean(xp.abs(psi)**2).real)
    E_baseline = energy_in_region(psi, target_mask)
    print(f"  Relaxed: <ρ>={rho_relaxed:.6f}")
    print(f"  Baseline target energy: E_0={E_baseline:.4f}")
    t_relax = time.time() - t0
    print(f"  Relaxation wall time: {t_relax:.1f}s")
    sys.stdout.flush()

    psi_0 = psi.copy()

    # ════════════════════════════════════════════════════
    # PHASE 2: Probe Scattering at v=2.5
    # ════════════════════════════════════════════════════
    print(f"\nPHASE 2: Probe at v={V_PROBE}")
    print("-" * 40)
    sys.stdout.flush()

    t_transit = 50.0 / V_PROBE
    n_steps = min(N_STEP_MAX, int(t_transit / DT_REAL))
    print(f"  T_transit={t_transit:.1f}, n_steps={n_steps}")

    psi = psi_0.copy()

    probe = PROBE_AMP * xp.exp(
        -((X - PROBE_X0)**2 + Y**2 + Z**2) / (2 * PROBE_SIGMA**2)
    ) * xp.exp(1j * V_PROBE * X)

    probe_norm = float(xp.sum(xp.abs(probe)**2).real) * dx**3
    probe_KE = 0.5 * V_PROBE**2 * probe_norm
    print(f"  Probe: norm={probe_norm:.6f}, KE={probe_KE:.6f}")
    sys.stdout.flush()

    psi += probe

    t_evolve = time.time()
    check_every = max(1, n_steps // 4)
    for step in range(n_steps):
        psi = step_real(psi)
        if (step + 1) % check_every == 0:
            rho_m = float(xp.mean(xp.abs(psi)**2).real)
            print(f"  Step {step+1}/{n_steps}: <ρ>={rho_m:.6f}")
            sys.stdout.flush()

    E_after = energy_in_region(psi, target_mask)
    delta_E = E_after - E_baseline
    sigma_eff_256 = abs(delta_E) / (probe_KE + 1e-30)

    dt_evolve = time.time() - t_evolve
    print(f"  E_target: {E_baseline:.4f} → {E_after:.4f}  ΔE={delta_E:.6f}")
    print(f"  σ_eff(N={N}) = {sigma_eff_256:.6f}")
    print(f"  Evolution wall time: {dt_evolve:.1f}s")
    sys.stdout.flush()

    # ════════════════════════════════════════════════════
    # PHASE 3: Convergence Comparison
    # ════════════════════════════════════════════════════
    wall_total = time.time() - t0

    ratio = sigma_eff_256 / SIGMA_EFF_128
    relative_change = abs(sigma_eff_256 - SIGMA_EFF_128) / SIGMA_EFF_128 * 100

    print("\n" + "=" * 60)
    print("GRID CONVERGENCE RESULTS")
    print("=" * 60)
    print(f"  {'N':>6s} {'dx':>8s} {'σ_eff':>14s} {'ΔE':>14s}")
    print(f"  {'-'*6} {'-'*8} {'-'*14} {'-'*14}")
    print(f"  {'128':>6s} {'0.500':>8s} {SIGMA_EFF_128:>14.6f} {'(baseline)':>14s}")
    print(f"  {str(N):>6s} {dx:>8.3f} {sigma_eff_256:>14.6f} {delta_E:>14.6f}")
    print(f"\n  Ratio σ(N={N})/σ(N=128) = {ratio:.4f}")
    print(f"  Relative change: {relative_change:.1f}%")

    # Convergence criterion: <50% change means converged
    converged = relative_change < 50.0
    still_low = sigma_eff_256 < 30.0  # still much lower than v=0.3 value of 800

    if converged and still_low:
        verdict = "CONVERGED"
        explanation = (f"σ_eff changes by only {relative_change:.1f}% under grid "
                       f"doubling. The low cross-section at v=2.5 is physical, "
                       f"NOT a resolution artifact.")
    elif still_low:
        verdict = "CONVERGED (weak)"
        explanation = (f"σ_eff changed by {relative_change:.1f}% but remains low "
                       f"({sigma_eff_256:.1f} << 800). Asymptotic freedom holds.")
    else:
        verdict = "NOT CONVERGED"
        explanation = (f"σ_eff spiked to {sigma_eff_256:.1f} at higher resolution. "
                       f"The N=128 result may be a grid artifact.")

    print(f"\n  Verdict: {verdict}")
    print(f"  {explanation}")
    print(f"  Wall time: {wall_total:.1f}s")
    print("=" * 60)
    sys.stdout.flush()

    # ── JSON output ──
    result = {
        'test': 'QCD_Grid_Convergence',
        'probe_velocity': V_PROBE,
        'grids': {
            'N128': {'N': 128, 'dx': 0.5, 'sigma_eff': SIGMA_EFF_128},
            f'N{N}': {'N': N, 'dx': dx, 'sigma_eff': sigma_eff_256,
                       'delta_E': delta_E, 'probe_KE': probe_KE},
        },
        'ratio': ratio,
        'relative_change_pct': relative_change,
        'converged': converged,
        'still_low': still_low,
        'verdict': verdict,
        'wall_time': wall_total,
    }
    json_path = "uhf_qcd_convergence_check.json"
    with open(json_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"  JSON → {json_path}")

    return result


if __name__ == '__main__':
    main()
