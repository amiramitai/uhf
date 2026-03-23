#!/usr/bin/env python3
"""
Higgs Breathing Mode — Null Model (Gaussian Blob)
===================================================
Replace the topological trefoil knot with a generic, non-topological
high-density Gaussian fluid clump. Run identical FFT analysis.

Null Hypothesis: The ω*=0.1178 peak is just a generic fluid mode,
not specific to the trefoil topology.

If the peak at ω*≈0.1178 DISAPPEARS or shifts significantly,
the trefoil result is confirmed as topologically unique.
"""

import sys, os, time, csv, json
import numpy as np

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

# ── Baseline from trefoil run ──
OMEGA_TREFOIL = 0.1178  # measured peak angular frequency

# ── Parameters (identical to trefoil run) ──
N        = 128
L        = 64.0
dx       = L / N
MU       = 1.0
G_NL     = 1.0
RHO_0    = MU / G_NL
xi       = 1.0 / np.sqrt(2 * G_NL * RHO_0)

DT_IMAG  = 0.02
DT_REAL  = 0.01
N_RELAX  = 2000
N_EVOLVE = 16000

# Gaussian blob parameters: match the trefoil's spatial extent
# Trefoil occupies a torus of R≈18, r≈8 → overall extent ~26 lattice units
# Gaussian blob: centered at grid center, σ chosen to have similar volume
BLOB_SIGMA = 12.0       # comparable to trefoil extent
BLOB_AMP   = 0.5        # density enhancement factor (ρ = ρ₀ + BLOB_AMP × ρ₀ × gaussian)

# Noise (identical)
NOISE_AMP = 0.005

# Derived
c_s = np.sqrt(G_NL * RHO_0)
CX, CY, CZ = L/2, L/2, L/2

print(f"[CFG] N={N}, L={L:.1f}, dx={dx:.4f}")
print(f"[CFG] μ={MU}, g={G_NL}, ρ₀={RHO_0:.2f}, ξ={xi:.4f}, c_s={c_s:.4f}")
print(f"[CFG] Gaussian blob: σ={BLOB_SIGMA}, amp={BLOB_AMP}")
print(f"[CFG] N_RELAX={N_RELAX}, N_EVOLVE={N_EVOLVE}")
print(f"[CFG] NOISE_AMP={NOISE_AMP}")
sys.stdout.flush()

# ── Grid ──
x1d = xp.linspace(0, L, N, endpoint=False, dtype=xp.float64) + dx/2
X, Y, Z = xp.meshgrid(x1d, x1d, x1d, indexing='ij')

k1d = 2 * np.pi * xp.fft.fftfreq(N, d=dx)
KX, KY, KZ = xp.meshgrid(k1d, k1d, k1d, indexing='ij')
K2 = KX**2 + KY**2 + KZ**2

kin_imag = xp.exp(-0.5 * K2 * DT_IMAG)
kin_real = xp.exp(-1j * 0.5 * K2 * DT_REAL)


def step_imag(psi):
    rho = xp.abs(psi)**2
    psi *= xp.exp(-DT_IMAG/2 * (G_NL * rho - MU))
    psi = xp.fft.ifftn(kin_imag * xp.fft.fftn(psi))
    rho = xp.abs(psi)**2
    psi *= xp.exp(-DT_IMAG/2 * (G_NL * rho - MU))
    norm2 = float(xp.mean(xp.abs(psi)**2))
    if norm2 > 0:
        psi *= np.sqrt(RHO_0 / norm2)
    return psi


def step_real(psi):
    rho = xp.abs(psi)**2
    psi *= xp.exp(-1j * DT_REAL/2 * (G_NL * rho - MU))
    psi = xp.fft.ifftn(kin_real * xp.fft.fftn(psi))
    rho = xp.abs(psi)**2
    psi *= xp.exp(-1j * DT_REAL/2 * (G_NL * rho - MU))
    return psi


def main():
    t0 = time.time()

    # ════════════════════════════════════════════════════
    # PHASE 1: Create Gaussian blob (NOT a vortex)
    # ════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("PHASE 1: Imprint Gaussian blob (null model)")
    print("=" * 60)
    sys.stdout.flush()

    # Uniform background + Gaussian density enhancement
    R2_grid = (X - CX)**2 + (Y - CY)**2 + (Z - CZ)**2
    blob_profile = BLOB_AMP * xp.exp(-R2_grid / (2 * BLOB_SIGMA**2))
    psi = xp.sqrt(RHO_0 * (1.0 + blob_profile)).astype(xp.complex128)

    # Create measurement mask: region near the blob center
    # (analogous to knot core mask in trefoil run)
    R_grid = xp.sqrt(R2_grid)
    core_mask_np = (R_grid < 3.0 * 1.2).get() if GPU else (R_grid < 3.0 * 1.2)
    sample_mask_np = (R_grid < 6.0 * 1.2).get() if GPU else (R_grid < 6.0 * 1.2)
    core_mask = xp.array(core_mask_np) if GPU else core_mask_np
    sample_mask = xp.array(sample_mask_np) if GPU else sample_mask_np

    n_core = int(np.sum(core_mask_np))
    n_sample = int(np.sum(sample_mask_np))
    rho_init = float(xp.mean(xp.abs(psi)**2))
    print(f"  <ρ>={rho_init:.4f}, core voxels={n_core:,}, sample voxels={n_sample:,}")
    print(f"  (No vortex — no topological charge — just a smooth density bump)")
    sys.stdout.flush()

    # ════════════════════════════════════════════════════
    # PHASE 2: Imaginary-time relaxation
    # ════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("PHASE 2: Imaginary-time relaxation")
    print("=" * 60)
    sys.stdout.flush()

    for i in range(N_RELAX):
        psi = step_imag(psi)
        if (i + 1) % 500 == 0:
            rho_avg = float(xp.mean(xp.abs(psi)**2))
            rho_core = float(xp.mean(xp.abs(psi[core_mask])**2))
            print(f"  Step {i+1}/{N_RELAX}: <ρ>={rho_avg:.4f}, ρ_core={rho_core:.4f}")
            sys.stdout.flush()

    rho_relaxed = float(xp.mean(xp.abs(psi)**2))
    rho_core_relaxed = float(xp.mean(xp.abs(psi[core_mask])**2))
    print(f"  Final: <ρ>={rho_relaxed:.4f}, ρ_core={rho_core_relaxed:.4f}")

    # For Gaussian blob, core density should be AT or ABOVE background
    # (no vortex depletion — the key difference from trefoil)
    density_dip = 1.0 - rho_core_relaxed / rho_relaxed
    print(f"  Core density dip: {density_dip*100:.1f}% (expect ~0% — no vortex)")
    sys.stdout.flush()

    psi_relaxed = psi.copy()

    # ════════════════════════════════════════════════════
    # PHASE 3: Noise injection + real-time evolution
    # ════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("PHASE 3: Noise injection + real-time evolution")
    print("=" * 60)
    sys.stdout.flush()

    rng = np.random.default_rng(42)  # same seed as trefoil
    noise_re = rng.normal(0, NOISE_AMP * np.sqrt(RHO_0), (N, N, N))
    noise_im = rng.normal(0, NOISE_AMP * np.sqrt(RHO_0), (N, N, N))
    noise = noise_re + 1j * noise_im
    if GPU:
        noise = xp.array(noise)
    psi = psi_relaxed + noise

    times = np.zeros(N_EVOLVE)
    rho_core_ts = np.zeros(N_EVOLVE)

    print(f"  Evolving {N_EVOLVE} steps (T={N_EVOLVE*DT_REAL:.1f})...")
    sys.stdout.flush()

    for i in range(N_EVOLVE):
        psi = step_real(psi)
        times[i] = (i + 1) * DT_REAL
        rho_core_ts[i] = float(xp.mean(xp.abs(psi[core_mask])**2))

        if (i + 1) % 4000 == 0:
            rho_avg = float(xp.mean(xp.abs(psi)**2))
            elapsed = time.time() - t0
            print(f"  Step {i+1}/{N_EVOLVE}: <ρ>={rho_avg:.4f}, "
                  f"ρ_core={rho_core_ts[i]:.4f} [{elapsed:.0f}s]")
            sys.stdout.flush()

    wall_evolve = time.time() - t0
    print(f"  Evolution done in {wall_evolve:.1f}s")
    sys.stdout.flush()

    # ════════════════════════════════════════════════════
    # PHASE 4: FFT Analysis (identical to trefoil)
    # ════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("PHASE 4: FFT spectral analysis")
    print("=" * 60)
    sys.stdout.flush()

    signal = rho_core_ts

    # Polynomial detrend
    poly_order = 3
    coeffs = np.polyfit(times, signal, poly_order)
    trend = np.polyval(coeffs, times)
    delta_rho = signal - trend
    print(f"  Detrended with order-{poly_order} polynomial")
    print(f"  δρ range: [{delta_rho.min():.6e}, {delta_rho.max():.6e}]")

    # Hann window
    window = np.hanning(N_EVOLVE)
    delta_rho_windowed = delta_rho * window

    # Zero-pad 8× (same as trefoil)
    n_pad = N_EVOLVE * 8
    fft_vals = np.fft.rfft(delta_rho_windowed, n=n_pad)
    power = np.abs(fft_vals)**2
    freqs = np.fft.rfftfreq(n_pad, d=DT_REAL)

    # Skip lowest bins
    min_bin = 3 * 8
    peak_idx = np.argmax(power[min_bin:]) + min_bin
    f_peak = freqs[peak_idx]
    omega_peak = 2 * np.pi * f_peak
    P_peak = power[peak_idx]

    # SNR
    noise_mask = np.ones(len(power), dtype=bool)
    noise_mask[:min_bin] = False
    peak_window = max(40, int(0.02 * len(power)))
    lo_exc = max(min_bin, peak_idx - peak_window)
    hi_exc = min(len(power), peak_idx + peak_window)
    noise_mask[lo_exc:hi_exc] = False
    noise_floor = np.median(power[noise_mask]) if noise_mask.sum() > 10 else 1e-30
    snr = P_peak / noise_floor if noise_floor > 0 else float('inf')

    # Prominence
    prom_mask = np.ones(len(power), dtype=bool)
    prom_mask[:min_bin] = False
    prom_mask[max(min_bin, peak_idx - peak_window):min(len(power), peak_idx + peak_window)] = False
    P_second = np.max(power[prom_mask]) if prom_mask.sum() > 0 else 1e-30
    prominence = P_peak / P_second if P_second > 0 else float('inf')

    # FWHM
    df = freqs[1] - freqs[0]
    half_max = P_peak / 2
    left = peak_idx
    while left > 1 and power[left] > half_max:
        left -= 1
    right = peak_idx
    while right < len(power) - 1 and power[right] > half_max:
        right += 1
    fwhm_bins = right - left
    fwhm_freq = fwhm_bins * df
    Q_factor = f_peak / fwhm_freq if fwhm_freq > 0 else float('inf')

    # Check if trefoil frequency is present
    trefoil_f = OMEGA_TREFOIL / (2 * np.pi)
    trefoil_bin = int(round(trefoil_f / df))
    P_at_trefoil = power[trefoil_bin] if trefoil_bin < len(power) else 0.0
    trefoil_ratio = P_at_trefoil / P_peak if P_peak > 0 else 0.0

    print(f"  Blob peak: f*={f_peak:.6f} (ω*={omega_peak:.4f})")
    print(f"  Trefoil peak was at: ω={OMEGA_TREFOIL:.4f}")
    print(f"  Blob peak power: P*={P_peak:.4e}")
    print(f"  SNR: {snr:.1f}")
    print(f"  Q-factor: {Q_factor:.1f}")
    print(f"  Prominence: {prominence:.2f}")
    print(f"\n  Power at trefoil ω={OMEGA_TREFOIL:.4f}: {P_at_trefoil:.4e}")
    print(f"  Ratio P(trefoil_ω)/P(blob_peak) = {trefoil_ratio:.6f}")

    # Does the blob reproduce the trefoil peak?
    omega_match = abs(omega_peak - OMEGA_TREFOIL) / OMEGA_TREFOIL < 0.15
    strong_peak = prominence > 3.0 and snr > 5.0

    # Top 5 peaks
    sorted_idx = np.argsort(power[min_bin:])[::-1] + min_bin
    print(f"\n  Top 5 spectral peaks:")
    for rank, idx in enumerate(sorted_idx[:5]):
        f_i = freqs[idx]
        omega_i = 2 * np.pi * f_i
        ratio_i = power[idx] / P_peak
        print(f"    #{rank+1}: f={f_i:.6f}, ω={omega_i:.4f}, P/P*={ratio_i:.4f}")
    sys.stdout.flush()

    # ════════════════════════════════════════════════════
    # PHASE 5: Null Model Verdict
    # ════════════════════════════════════════════════════
    wall_total = time.time() - t0

    print("\n" + "=" * 60)
    print("NULL MODEL RESULTS")
    print("=" * 60)
    print(f"  Trefoil knot peak: ω*={OMEGA_TREFOIL:.4f} (SNR=inf, prominence=33.6M)")
    print(f"  Gaussian blob peak: ω*={omega_peak:.4f} (SNR={snr:.1f}, prominence={prominence:.2f})")

    if omega_match and strong_peak:
        verdict = "FAIL — null model reproduces trefoil peak"
        detail = (f"The Gaussian blob shows the SAME resonance at ω≈{omega_peak:.4f} "
                  f"with comparable SNR and prominence. The trefoil result is NOT unique.")
    elif omega_match and not strong_peak:
        verdict = "PASS — peak not significant in null model"
        detail = (f"Gaussian blob shows a weak mode near ω≈{omega_peak:.4f} but with "
                  f"poor SNR={snr:.1f} / prominence={prominence:.2f}. "
                  f"The trefoil's strong resonance is topologically unique.")
    else:
        verdict = "PASS — null model peak differs from trefoil"
        detail = (f"Gaussian blob's dominant mode is at ω={omega_peak:.4f}, "
                  f"NOT at the trefoil's ω={OMEGA_TREFOIL:.4f}. "
                  f"The 125 GeV resonance is specific to the trefoil topology.")

    print(f"  Verdict: {verdict}")
    print(f"  {detail}")
    print(f"  Wall time: {wall_total:.1f}s")
    print("=" * 60)
    sys.stdout.flush()

    # ── JSON ──
    result = {
        'test': 'Higgs_Null_Model',
        'blob': {'sigma': BLOB_SIGMA, 'amp': BLOB_AMP},
        'trefoil_omega': OMEGA_TREFOIL,
        'blob_spectrum': {
            'peak_freq': float(f_peak),
            'peak_omega': float(omega_peak),
            'peak_power': float(P_peak),
            'snr': float(snr),
            'Q_factor': float(Q_factor),
            'prominence': float(prominence),
        },
        'power_at_trefoil_omega': float(P_at_trefoil),
        'trefoil_ratio': float(trefoil_ratio),
        'omega_match': bool(omega_match),
        'strong_peak': bool(strong_peak),
        'verdict': verdict,
        'wall_time': wall_total,
    }
    json_path = "uhf_higgs_null_model.json"
    with open(json_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"  JSON → {json_path}")

    return result


if __name__ == '__main__':
    main()
