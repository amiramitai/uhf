#!/usr/bin/env python3
"""
UHF — Hydrodynamic Relativity v1
==================================
Emergent Lorentz Factor from GP Vortex Dipole Acceleration.

2D Split-Step Fourier GP on RTX 3090 (2048×1536, Lx=120ξ, Lz=160ξ).
Tests m_eff(v) = F/a(v) for a vortex dipole under constant body force.
Periodic boundaries (no sponge — preserves norm and condensate).

Units: ℏ = m = ξ = c_s = 1, κ = 2π, ρ₀ = 1, g = 1.
"""

import os, sys, time, json
import numpy as np
import torch
import torch.fft as fft
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
from scipy.ndimage import gaussian_filter1d

# ══════════════════════════════════════════════════════════════════════
#  Parameters
# ══════════════════════════════════════════════════════════════════════
NX = 2048
NZ = 1536
LX = 120.0
LZ = 160.0        # extended z for travel room (PBC)
XI      = 1.0
RHO_0   = 1.0
G_NL    = 1.0
MU      = G_NL * RHO_0
CS      = 1.0

DX = LX / NX
DZ = LZ / NZ
DA = DX * DZ
DT = 0.02

# ── Vortex dipole ──
# v_self ≈ κ/(2πd) = 1/d.  d=4ξ → v_self ≈ 0.25 c_s.
DIPOLE_D  = 4.0
DIPOLE_X0 = 0.0
DIPOLE_Z0 = 0.0       # center of domain

# ── Imaginary-time ──
IT_STEPS = 600
IT_DT    = 0.02

# ── Evolution ──
RAMP_TIME       = 50.0
EVOLVE_TIME     = 300.0
SAMPLE_INTERVAL = 2.0
SETTLE_TIME     = 30.0
FORCES = [0.005, 0.01, 0.02, 0.05]

# ── Tracker ──
TRACK_WINDOW = 15.0

OUTDIR = "UHF_HydroRelativity_v1_results"
DTYPE  = torch.float64
CDTYPE = torch.complex128


# ══════════════════════════════════════════════════════════════════════
#  GPU
# ══════════════════════════════════════════════════════════════════════
def setup_device():
    assert torch.cuda.is_available(), "CUDA required"
    dev = torch.device('cuda:0')
    p = torch.cuda.get_device_properties(dev)
    print(f"  Device : {p.name}")
    print(f"  VRAM   : {p.total_memory / 1e9:.1f} GB")
    return dev


# ══════════════════════════════════════════════════════════════════════
#  Grids
# ══════════════════════════════════════════════════════════════════════
def build_grids(device):
    x1d = torch.linspace(-LX/2, LX/2 - DX, NX, device=device, dtype=DTYPE)
    z1d = torch.linspace(-LZ/2, LZ/2 - DZ, NZ, device=device, dtype=DTYPE)
    X, Z = torch.meshgrid(x1d, z1d, indexing='ij')
    kx1d = torch.fft.fftfreq(NX, d=DX, device=device).to(DTYPE) * 2 * np.pi
    kz1d = torch.fft.fftfreq(NZ, d=DZ, device=device).to(DTYPE) * 2 * np.pi
    KX, KZ = torch.meshgrid(kx1d, kz1d, indexing='ij')
    K2 = KX**2 + KZ**2
    return X, Z, K2


# ══════════════════════════════════════════════════════════════════════
#  Vortex Dipole Initialization
# ══════════════════════════════════════════════════════════════════════
def init_vortex_dipole(X, Z, device):
    """
    +1 vortex at (x₀ + d/2, z₀), -1 vortex at (x₀ - d/2, z₀).
    Self-propulsion in +z at v_self ≈ 1/d.
    """
    x_p = DIPOLE_X0 + DIPOLE_D / 2.0
    x_m = DIPOLE_X0 - DIPOLE_D / 2.0
    z0  = DIPOLE_Z0

    r_p = torch.sqrt((X - x_p)**2 + (Z - z0)**2)
    r_m = torch.sqrt((X - x_m)**2 + (Z - z0)**2)

    phase_p = torch.atan2(Z - z0, X - x_p)
    phase_m = torch.atan2(Z - z0, X - x_m)
    phase = phase_p - phase_m

    amp = (np.sqrt(RHO_0)
           * torch.tanh(r_p / (np.sqrt(2.0) * XI))
           * torch.tanh(r_m / (np.sqrt(2.0) * XI)))

    psi = (amp * torch.exp(1j * phase.to(CDTYPE))).to(CDTYPE)

    N_target = RHO_0 * LX * LZ
    N_now = torch.sum(torch.abs(psi)**2).item() * DA
    if N_now > 0:
        psi *= np.sqrt(N_target / N_now)

    return psi, phase


# ══════════════════════════════════════════════════════════════════════
#  Phase-Locked Imaginary-Time Relaxation
# ══════════════════════════════════════════════════════════════════════
def imaginary_time_relax(psi, phase_target, K2, device):
    print("\n  ┌─ Phase-Locked Imaginary-Time Relaxation", flush=True)
    dt = IT_DT
    kinetic_prop = torch.exp(-0.5 * K2 * dt)
    phase_factor = torch.exp(1j * phase_target.to(CDTYPE))
    N_target = torch.sum(torch.abs(psi)**2).item() * DA

    t0 = time.time()
    report_every = max(IT_STEPS // 5, 1)

    for step in range(IT_STEPS):
        rho = torch.abs(psi)**2
        psi = psi * torch.exp(-dt / 2.0 * (G_NL * rho - MU))
        psi_k = fft.fftn(psi)
        psi_k *= kinetic_prop
        psi = fft.ifftn(psi_k)
        rho = torch.abs(psi)**2
        psi = psi * torch.exp(-dt / 2.0 * (G_NL * rho - MU))

        amp = torch.abs(psi)
        psi = amp.to(CDTYPE) * phase_factor
        N_now = torch.sum(amp**2).item() * DA
        if N_now > 0:
            psi *= np.sqrt(N_target / N_now)

        if (step + 1) % report_every == 0:
            rho = torch.abs(psi)**2
            print(f"  │  Step {step+1}/{IT_STEPS}  "
                  f"ρ_min={rho.min().item():.6f}  "
                  f"ρ_max={rho.max().item():.6f}  "
                  f"[{time.time()-t0:.1f}s]", flush=True)

    rho = torch.abs(psi)**2
    print(f"  │  Final: ρ_min={rho.min().item():.6f}, "
          f"ρ_max={rho.max().item():.6f}")
    print(f"  └─ {time.time()-t0:.1f}s", flush=True)
    return psi


# ══════════════════════════════════════════════════════════════════════
#  Real-Time Step (no sponge — periodic boundaries)
# ══════════════════════════════════════════════════════════════════════
def real_time_step(psi, kinetic_prop, dt):
    """Strang split-step, pure GP, periodic boundaries."""
    rho = torch.abs(psi)**2
    psi = psi * torch.exp(-0.5j * dt * (G_NL * rho - MU))
    psi_k = fft.fftn(psi)
    psi_k *= kinetic_prop
    psi = fft.ifftn(psi_k)
    rho = torch.abs(psi)**2
    psi = psi * torch.exp(-0.5j * dt * (G_NL * rho - MU))
    return psi


def apply_force_kick(psi, Z, F_eff, dt):
    """Uniform body force via momentum kick."""
    if abs(F_eff) > 0:
        psi = psi * torch.exp(1j * F_eff * dt * Z)
    return psi


# ══════════════════════════════════════════════════════════════════════
#  Tracker: depletion COM in sliding window
# ══════════════════════════════════════════════════════════════════════
def track_dipole(psi, X, Z, z_prev, x_prev):
    rho = torch.abs(psi)**2

    # Wrap-aware window: handle periodic z
    z_lo = z_prev - TRACK_WINDOW
    z_hi = z_prev + TRACK_WINDOW
    x_lo = x_prev - TRACK_WINDOW
    x_hi = x_prev + TRACK_WINDOW

    # Simple window (may fail near boundary — handle wrapping)
    if z_lo < -LZ/2 or z_hi > LZ/2:
        # Near periodic boundary, use shifted frame
        Z_shifted = Z - z_prev
        Z_shifted = Z_shifted - LZ * torch.round(Z_shifted / LZ)
        mask = ((torch.abs(Z_shifted) <= TRACK_WINDOW) &
                (X >= x_lo) & (X <= x_hi))
        depletion = torch.clamp(RHO_0 - rho, min=0) * mask.to(DTYPE)
        total = torch.sum(depletion).item() * DA
        if total > 1e-12:
            # COM in shifted frame, then shift back
            z_com_shift = (torch.sum(depletion * Z_shifted).item() * DA) / total
            z_com = z_prev + z_com_shift
            x_com = (torch.sum(depletion * X).item() * DA) / total
        else:
            z_com, x_com = z_prev, x_prev
    else:
        mask = ((Z >= z_lo) & (Z <= z_hi) &
                (X >= x_lo) & (X <= x_hi))
        depletion = torch.clamp(RHO_0 - rho, min=0) * mask.to(DTYPE)
        total = torch.sum(depletion).item() * DA
        if total > 1e-12:
            z_com = (torch.sum(depletion * Z).item() * DA) / total
            x_com = (torch.sum(depletion * X).item() * DA) / total
        else:
            z_com, x_com = z_prev, x_prev

    rho_masked = rho + (~mask).to(DTYPE) * 1e6
    rho_min = rho_masked.min().item()
    return z_com, x_com, rho_min, total


def unwrap_z(z_list):
    z_arr = np.array(z_list)
    out = np.zeros_like(z_arr)
    out[0] = z_arr[0]
    for i in range(1, len(z_arr)):
        dz = z_arr[i] - z_arr[i-1]
        if dz > LZ / 2:   dz -= LZ
        if dz < -LZ / 2:  dz += LZ
        out[i] = out[i-1] + dz
    return out


# ══════════════════════════════════════════════════════════════════════
#  Single Force Run
# ══════════════════════════════════════════════════════════════════════
def run_single_force(F_val, psi_init, X, Z, K2, device, z0_init, x0_init):
    print(f"\n{'='*70}")
    print(f"  FORCE F = {F_val}")
    print(f"{'='*70}", flush=True)

    psi = psi_init.clone()
    kinetic_prop = torch.exp(-0.5j * K2 * DT)

    total_steps = int(EVOLVE_TIME / DT)
    sample_every = max(int(SAMPLE_INTERVAL / DT), 1)

    z_com, x_com, rho_min, void_mass = track_dipole(
        psi, X, Z, z0_init, x0_init)
    void_mass_0 = void_mass

    times    = [0.0]
    z_pos    = [z_com]
    rho_mins = [rho_min]
    void_m   = [void_mass]

    print(f"  Init: z={z_com:.2f}, x={x_com:.2f}, "
          f"ρ_min={rho_min:.4f}, void={void_mass:.2f}", flush=True)

    t0 = time.time()
    destabilized = False

    for step in range(1, total_steps + 1):
        t = step * DT
        F_current = F_val * min(t / RAMP_TIME, 1.0)

        # Strang: half-kick → GP → half-kick
        psi = apply_force_kick(psi, Z, F_current / 2, DT)
        psi = real_time_step(psi, kinetic_prop, DT)
        psi = apply_force_kick(psi, Z, F_current / 2, DT)

        if step % sample_every == 0:
            z_com, x_com, rho_min, void_mass = track_dipole(
                psi, X, Z, z_com, x_com)
            times.append(t)
            z_pos.append(z_com)
            rho_mins.append(rho_min)
            void_m.append(void_mass)

            if void_mass_0 > 0 and void_mass / void_mass_0 < 0.05:
                print(f"  !! Dipole dissolved t={t:.1f}τ", flush=True)
                destabilized = True
                break

        if step % (total_steps // 8) == 0:
            elapsed = time.time() - t0
            norm = torch.sum(torch.abs(psi)**2).item() * DA / (RHO_0*LX*LZ)
            print(f"  t={t:7.1f}τ  z={z_com:7.2f}  "
                  f"ρ_min={rho_min:.4f}  norm={norm:.4f}  "
                  f"F={F_current:.4f}  [{elapsed:.0f}s]", flush=True)

    wall = time.time() - t0

    # ── Velocity / acceleration ──
    t_arr = np.array(times)
    z_arr = unwrap_z(z_pos)

    v_raw = np.gradient(z_arr, t_arr)
    v_sm  = gaussian_filter1d(v_raw, sigma=5)
    a_raw = np.gradient(v_sm, t_arr)
    a_sm  = gaussian_filter1d(a_raw, sigma=5)

    # Post-ramp analysis
    post = t_arr > RAMP_TIME + 10
    v_p = v_sm[post]
    a_p = a_sm[post]

    valid = (a_p > 1e-5) & (np.abs(v_p) < 0.99) & (np.abs(v_p) > 0.01)
    v_v = np.abs(v_p[valid])
    a_v = a_p[valid]

    m_eff = F_val / a_v if len(a_v) > 0 else np.array([])
    max_v = float(np.max(np.abs(v_sm))) if len(v_sm) > 0 else 0.0

    print(f"  Max v: {max_v:.4f} c_s,  wall: {wall:.0f}s")
    if len(m_eff) > 0:
        print(f"  m_eff range: [{m_eff.min():.2f}, {m_eff.max():.2f}]")

    return {
        'F': F_val, 'times': t_arr, 'z_pos': z_arr,
        'v_sm': v_sm, 'a_sm': a_sm,
        'v_valid': v_v, 'a_valid': a_v, 'm_eff': m_eff,
        'max_v': max_v, 'wall': wall, 'destab': destabilized,
        'rho_mins': np.array(rho_mins), 'void_m': np.array(void_m),
    }


# ══════════════════════════════════════════════════════════════════════
#  Gamma Fit
# ══════════════════════════════════════════════════════════════════════
def gamma_func(v, m0):
    return m0 / np.sqrt(np.clip(1 - (v / CS)**2, 1e-6, None))


def fit_gamma(results):
    v_all, m_all = [], []
    for r in results:
        if len(r['v_valid']) > 0:
            v_all.extend(r['v_valid'].tolist())
            m_all.extend(r['m_eff'].tolist())
    v_all = np.array(v_all)
    m_all = np.array(m_all)

    if len(v_all) < 5:
        return None, None, v_all, m_all

    good = (m_all > 0) & (m_all < 200) & (v_all > 0.01)
    v_f, m_f = v_all[good], m_all[good]
    if len(v_f) < 5:
        return None, None, v_all, m_all

    try:
        low_v = v_f < 0.3
        m0g = m_f[low_v].mean() if np.any(low_v) else m_f.mean()
        popt, _ = curve_fit(gamma_func, v_f, m_f, p0=[m0g], maxfev=5000)
        m0 = popt[0]
        pred = gamma_func(v_f, m0)
        ss_res = np.sum((m_f - pred)**2)
        ss_tot = np.sum((m_f - m_f.mean())**2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
        return m0, r2, v_f, m_f
    except Exception as e:
        print(f"  Fit failed: {e}")
        return None, None, v_all, m_all


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  UHF — Hydrodynamic Relativity v1                        ║")
    print("║  Emergent Lorentz Factor: Vortex Dipole Acceleration      ║")
    print("║  Grid: 2048×1536  Lx=120ξ  Lz=160ξ  (PBC, no sponge)    ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    device = setup_device()
    os.makedirs(OUTDIR, exist_ok=True)

    X, Z, K2 = build_grids(device)
    print(f"\n  Grid: {NX}×{NZ}, Lx={LX}ξ, Lz={LZ}ξ")
    print(f"  Δx={DX:.4f}ξ, Δz={DZ:.4f}ξ")

    v_self_th = 2 * np.pi / (2 * np.pi * DIPOLE_D)
    print(f"\n  Dipole: d={DIPOLE_D}ξ, v_self(theory)≈{v_self_th:.3f} c_s")
    psi, phase0 = init_vortex_dipole(X, Z, device)
    vram = torch.cuda.memory_allocated() / 1e9
    print(f"  VRAM: {vram:.2f} GB")

    # ── Relaxation ──
    psi = imaginary_time_relax(psi, phase0, K2, device)

    # ── Settling ──
    print(f"\n  Settling ({SETTLE_TIME}τ, no force, no sponge)...",
          flush=True)
    kinetic_prop = torch.exp(-0.5j * K2 * DT)
    settle_steps = int(SETTLE_TIME / DT)
    t0s = time.time()
    for s in range(settle_steps):
        psi = real_time_step(psi, kinetic_prop, DT)
    z0, x0, rm0, vm0 = track_dipole(psi, X, Z, DIPOLE_Z0, DIPOLE_X0)
    norm0 = torch.sum(torch.abs(psi)**2).item() * DA / (RHO_0 * LX * LZ)
    v_settle_meas = (z0 - DIPOLE_Z0) / SETTLE_TIME
    print(f"  After settle: z={z0:.2f}, x={x0:.2f}, "
          f"ρ_min={rm0:.4f}, void={vm0:.2f}")
    print(f"  v_self(meas)={v_settle_meas:.4f}  (theory {v_self_th:.4f})")
    print(f"  Norm={norm0:.6f}  [{time.time()-t0s:.0f}s]")

    psi_settled = psi.clone()
    settled_z0 = z0
    settled_x0 = x0

    # ── Run all forces ──
    results = []
    for F in FORCES:
        r = run_single_force(F, psi_settled, X, Z, K2, device,
                             settled_z0, settled_x0)
        results.append(r)
        torch.cuda.empty_cache()

    # ══════════════════════════════════════════════════════════════════
    #  Aggregate
    # ══════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print(f"  AGGREGATE RESULTS")
    print(f"{'='*70}")

    m0_fit, r2, v_fit, m_fit = fit_gamma(results)

    print(f"\n  {'F':>8s}  {'max v/c_s':>10s}  {'N_pts':>6s}  "
          f"{'m_eff range':>20s}  {'destab':>6s}")
    print(f"  {'-'*8}  {'-'*10}  {'-'*6}  {'-'*20}  {'-'*6}")
    for r in results:
        mr = (f"[{r['m_eff'].min():.2f}, {r['m_eff'].max():.2f}]"
              if len(r['m_eff']) > 0 else "N/A")
        print(f"  {r['F']:8.4f}  {r['max_v']:10.4f}  "
              f"{len(r['v_valid']):6d}  {mr:>20s}  "
              f"{'YES' if r['destab'] else 'no':>6s}")

    if m0_fit is not None:
        print(f"\n  Best-fit m₀ = {m0_fit:.4f}")
        print(f"  R² (γ fit) = {r2:.4f}")
    else:
        print(f"\n  γ fit: FAILED (insufficient data)")

    max_v_all = max(r['max_v'] for r in results)
    if r2 is not None and r2 > 0.5:
        conclusion = (f"Dynamic effective mass follows relativistic γ to "
                      f"R² = {r2:.2f} up to v≈{max_v_all:.2f} c_s "
                      f"→ hydrodynamic special relativity confirmed")
    else:
        r2s = f"{r2:.4f}" if r2 is not None else "N/A"
        conclusion = (f"Deviation or collapse before c_s → hypothesis "
                      f"falsified (R²={r2s}, max v={max_v_all:.2f} c_s)")

    print(f"\n  CONCLUSION: {conclusion}", flush=True)

    # ══════════════════════════════════════════════════════════════════
    #  Plots
    # ══════════════════════════════════════════════════════════════════
    print("\n  Generating plots...", flush=True)
    fig, axes = plt.subplots(2, 2, figsize=(16, 14))
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']

    ax = axes[0, 0]
    for i, r in enumerate(results):
        if len(r['v_valid']) > 0:
            m0p = m0_fit if m0_fit else 1.0
            ax.scatter(r['v_valid'] / CS, r['m_eff'] / m0p,
                       c=colors[i % 4], s=10, alpha=0.5,
                       label=f'F={r["F"]}')
    v_th = np.linspace(0.01, 0.99, 200)
    ax.plot(v_th, 1.0 / np.sqrt(1 - v_th**2), 'k-', lw=2,
            label=r'$\gamma = 1/\sqrt{1-v^2/c_s^2}$')
    ax.set_xlabel('v / c_s', fontsize=13)
    ax.set_ylabel('m_eff / m₀', fontsize=13)
    r2t = f'R² = {r2:.3f}' if r2 is not None else 'no fit'
    ax.set_title(f'm_eff(v) vs Relativistic γ  ({r2t})',
                 fontsize=14, fontweight='bold')
    ax.legend(fontsize=10); ax.set_xlim(0, 1); ax.set_ylim(0, 10)
    ax.grid(True, alpha=0.3)

    ax = axes[0, 1]
    for i, r in enumerate(results):
        ax.plot(r['times'], r['v_sm'] / CS, color=colors[i % 4],
                lw=1.2, label=f'F={r["F"]}')
    ax.axhline(1.0, color='red', ls='--', lw=1.5, label='c_s')
    ax.axhline(0, color='gray', ls=':', lw=0.8)
    ax.set_xlabel('t [τ]', fontsize=13)
    ax.set_ylabel('v / c_s', fontsize=13)
    ax.set_title('Dipole Velocity vs Time', fontsize=14, fontweight='bold')
    ax.legend(fontsize=10); ax.grid(True, alpha=0.3)

    ax = axes[1, 0]
    for i, r in enumerate(results):
        ax.plot(r['times'], r['z_pos'], color=colors[i % 4],
                lw=1.2, label=f'F={r["F"]}')
    ax.set_xlabel('t [τ]', fontsize=13)
    ax.set_ylabel('z_com [ξ]', fontsize=13)
    ax.set_title('Dipole Position vs Time', fontsize=14, fontweight='bold')
    ax.legend(fontsize=10); ax.grid(True, alpha=0.3)

    ax = axes[1, 1]
    for i, r in enumerate(results):
        ax.plot(r['times'][:len(r['rho_mins'])], r['rho_mins'],
                color=colors[i % 4], lw=1.2, label=f'F={r["F"]}')
    ax.set_xlabel('t [τ]', fontsize=13)
    ax.set_ylabel('ρ_min in window', fontsize=13)
    ax.set_title('Core Density (Stability)', fontsize=14, fontweight='bold')
    ax.legend(fontsize=10); ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plot_path = os.path.join(OUTDIR, 'hydro_relativity_v1.png')
    fig.savefig(plot_path, dpi=200)
    plt.close(fig)
    print(f"  Plot: {plot_path}")

    jd = {
        'parameters': {
            'NX': NX, 'NZ': NZ, 'LX': LX, 'LZ': LZ, 'DT': DT,
            'DIPOLE_D': DIPOLE_D, 'RAMP_TIME': RAMP_TIME,
            'EVOLVE_TIME': EVOLVE_TIME, 'FORCES': FORCES,
        },
        'results_per_force': [
            {'F': r['F'], 'max_v': float(r['max_v']),
             'n_pts': int(len(r['v_valid'])),
             'm_eff_min': float(r['m_eff'].min()) if len(r['m_eff']) > 0 else None,
             'm_eff_max': float(r['m_eff'].max()) if len(r['m_eff']) > 0 else None,
             'destab': r['destab'], 'wall': float(r['wall'])}
            for r in results
        ],
        'fit': {
            'm0': float(m0_fit) if m0_fit else None,
            'r_squared': float(r2) if r2 else None,
        },
        'conclusion': conclusion,
    }
    with open(os.path.join(OUTDIR, 'results_v1.json'), 'w') as f:
        json.dump(jd, f, indent=2)
    print(f"  JSON: {os.path.join(OUTDIR, 'results_v1.json')}")

    total_wall = sum(r['wall'] for r in results)
    print(f"\n  Total wall time: {total_wall:.0f}s")
    print(f"  CONCLUSION: {conclusion}", flush=True)


if __name__ == '__main__':
    main()
