#!/usr/bin/env python3
"""
UHF — Hydrodynamic Inertia Theorem v3: Momentum-Kick Body Force
=========================================================
3D Split-Step Fourier GP Solver with:
  - Uniform body force via momentum kick: ψ → ψ·exp(i·F·dt·z)
  - Rectangular domain Nx=Ny=192, Nz=512 (Lx=Ly=40ξ, Lz=80ξ)
  - Topological phase-winding ring tracker
  - Small forces: F = 0.005, 0.01, 0.02, 0.05
  - 800τ_heal evolution, 50τ ramp

Physics (healing-length units: ℏ = m = ξ = c_s = 1):
  i ∂ψ/∂t = [-½∇² + |ψ|² - 1 + V_ext(z)] ψ
"""

import os, sys, time, json
import numpy as np
import torch
import torch.fft as fft
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit

# ══════════════════════════════════════════════════════════════════════
#  Parameters
# ══════════════════════════════════════════════════════════════════════
NX = NY = 192
NZ      = 512
LX = LY = 40.0   # [ξ]
LZ      = 80.0   # [ξ] — extended z for ring travel
XI      = 1.0
RHO_0   = 1.0
G_NL    = 1.0
MU      = G_NL * RHO_0
R_RING  = 8.0    # vortex ring radius [ξ]

DX = LX / NX
DY = LY / NY
DZ = LZ / NZ
DV = DX * DY * DZ

# Imaginary-time relaxation
IT_TIME = 30.0
IT_DT   = 0.05

# Real-time evolution
RAMP_TIME       = 50.0
EVOLVE_TIME     = 800.0
SAMPLE_INTERVAL = 10.0
DT_FIXED        = 0.02

FORCES = [0.005, 0.01, 0.02, 0.05]

# Pre-flight settling
SETTLE_TIME = 50.0

# Stability monitors (generous)
VOID_MASS_CHANGE_TOL = 0.40
RADIATION_LOSS_TOL   = 0.30

# Adaptive mask
MASK_RADIUS_XY = 12.0  # cylindrical mask radius [ξ]
MASK_UPDATE_STEPS = 20  # recenter mask every N sample points

OUTDIR = "UHF_HydroInertia_v3_results"
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
#  Grids (rectangular)
# ══════════════════════════════════════════════════════════════════════
def build_grids(device):
    x1d = torch.linspace(-LX/2, LX/2 - DX, NX, device=device, dtype=DTYPE)
    y1d = torch.linspace(-LY/2, LY/2 - DY, NY, device=device, dtype=DTYPE)
    z1d = torch.linspace(-LZ/2, LZ/2 - DZ, NZ, device=device, dtype=DTYPE)
    X, Y, Z = torch.meshgrid(x1d, y1d, z1d, indexing='ij')

    kx1d = torch.fft.fftfreq(NX, d=DX, device=device).to(DTYPE) * 2 * np.pi
    ky1d = torch.fft.fftfreq(NY, d=DY, device=device).to(DTYPE) * 2 * np.pi
    kz1d = torch.fft.fftfreq(NZ, d=DZ, device=device).to(DTYPE) * 2 * np.pi
    KX, KY, KZ = torch.meshgrid(kx1d, ky1d, kz1d, indexing='ij')
    K2 = KX**2 + KY**2 + KZ**2
    return X, Y, Z, K2


# ══════════════════════════════════════════════════════════════════════
#  Vortex Ring
# ══════════════════════════════════════════════════════════════════════
def vortex_ring_distance(X, Y, Z, R):
    rho_cyl = torch.sqrt(X**2 + Y**2)
    s = rho_cyl - R
    return torch.sqrt(s**2 + Z**2)


def init_vortex_ring(X, Y, Z, device):
    rho_cyl = torch.sqrt(X**2 + Y**2)
    s = rho_cyl - R_RING
    d = torch.sqrt(s**2 + Z**2)
    phase = torch.atan2(Z, s)
    amp = np.sqrt(RHO_0) * torch.tanh(d / XI)
    psi = (amp * torch.exp(1j * phase.to(CDTYPE))).to(CDTYPE)
    return psi, phase


# ══════════════════════════════════════════════════════════════════════
#  Phase-Locked Imaginary-Time Relaxation
# ══════════════════════════════════════════════════════════════════════
def imaginary_time_relax(psi, phase_target, K2, device):
    print("\n  ┌─ Phase-Locked Imaginary-Time Relaxation", flush=True)
    n_steps = int(IT_TIME / IT_DT)
    dt = IT_DT
    print(f"  │  {n_steps} steps, dt={dt}", flush=True)

    kinetic_full = torch.exp(-0.5 * K2 * dt)
    phase_factor = torch.exp(1j * phase_target.to(CDTYPE))
    N_particles = torch.sum(torch.abs(psi)**2).item() * DV

    t0 = time.time()
    report_every = max(n_steps // 10, 1)

    for step in range(n_steps):
        rho = torch.abs(psi)**2
        psi = psi * torch.exp(-dt / 2.0 * (G_NL * rho - MU))

        psi_k = fft.fftn(psi)
        psi_k *= kinetic_full
        psi = fft.ifftn(psi_k)

        rho = torch.abs(psi)**2
        psi = psi * torch.exp(-dt / 2.0 * (G_NL * rho - MU))

        amp = torch.abs(psi)
        psi = amp.to(CDTYPE) * phase_factor

        N_now = torch.sum(amp**2).item() * DV
        if N_now > 0:
            psi *= np.sqrt(N_particles / N_now)

        if (step + 1) % report_every == 0:
            rho = torch.abs(psi)**2
            ix = min(int((R_RING + LX/2) / DX), NX-1)
            iy = NY // 2
            iz = NZ // 2
            rho_core = rho[ix, iy, iz].item()
            elapsed = time.time() - t0
            print(f"  │  Step {step+1}/{n_steps}  ρ_min={rho.min().item():.6f}  "
                  f"ρ_max={rho.max().item():.6f}  ρ_core={rho_core:.6f}  "
                  f"[{elapsed:.1f}s]", flush=True)

    rho = torch.abs(psi)**2
    elapsed = time.time() - t0
    ix = min(int((R_RING + LX/2) / DX), NX-1)
    iy = NY // 2
    iz = NZ // 2
    rho_core = rho[ix, iy, iz].item()
    print(f"  │  ρ_min={rho.min().item():.6f}, ρ_max={rho.max().item():.6f}")
    print(f"  │  ρ_core={rho_core:.6f} (depletion {(1-rho_core)*100:.1f}%)")
    print(f"  └─ {elapsed:.1f}s", flush=True)
    return psi


# ══════════════════════════════════════════════════════════════════════
#  Body force via momentum kick
# ══════════════════════════════════════════════════════════════════════
# At each timestep, apply ψ → ψ·exp(i·F·dt·z).
# This adds momentum F·dt per particle per step = constant body force F.
# PBC-compatible (phase wraps cleanly over periodic domain).
# Equivalent to a spatially-uniform, constant gravitational field.


# ══════════════════════════════════════════════════════════════════════
#  Diagnostics — Adaptive Core Mask
# ══════════════════════════════════════════════════════════════════════
def _plaquette_hits_xz(psi, iy_slice, ix_center, ix_half_win):
    """Compute plaquette winding in the (x,z) plane at a given y-slice.
    Returns z-values of hits near ix_center ± ix_half_win."""
    device = psi.device
    phi = torch.angle(psi[:, iy_slice, :])  # (NX, NZ)
    iz_p1 = (torch.arange(NZ, device=device) + 1) % NZ
    
    phiA = phi[:-1, :]
    phiB = phi[1:, :]
    phiC = phi[1:, :][:, iz_p1]
    phiD = phi[:-1, :][:, iz_p1]
    
    w_AB = torch.remainder(phiB - phiA + torch.pi, 2*torch.pi) - torch.pi
    w_BC = torch.remainder(phiC - phiB + torch.pi, 2*torch.pi) - torch.pi
    w_CD = torch.remainder(phiD - phiC + torch.pi, 2*torch.pi) - torch.pi
    w_DA = torch.remainder(phiA - phiD + torch.pi, 2*torch.pi) - torch.pi
    wind = (w_AB + w_BC + w_CD + w_DA) / (2 * torch.pi)
    
    ix_lo = max(0, ix_center - ix_half_win)
    ix_hi = min(wind.shape[0], ix_center + ix_half_win + 1)
    wind_near = wind[ix_lo:ix_hi, :]
    hits = (torch.abs(wind_near) > 0.5).nonzero(as_tuple=False)
    if hits.shape[0] == 0:
        return torch.tensor([], device=device, dtype=DTYPE)
    
    z1d = torch.linspace(-LZ/2, LZ/2 - DZ, NZ, device=device, dtype=DTYPE)
    return z1d[hits[:, 1]]


def _plaquette_hits_yz(psi, ix_slice, iy_center, iy_half_win):
    """Compute plaquette winding in the (y,z) plane at a given x-slice.
    Returns z-values of hits near iy_center ± iy_half_win."""
    device = psi.device
    phi = torch.angle(psi[ix_slice, :, :])  # (NY, NZ)
    iz_p1 = (torch.arange(NZ, device=device) + 1) % NZ
    
    phiA = phi[:-1, :]
    phiB = phi[1:, :]
    phiC = phi[1:, :][:, iz_p1]
    phiD = phi[:-1, :][:, iz_p1]
    
    w_AB = torch.remainder(phiB - phiA + torch.pi, 2*torch.pi) - torch.pi
    w_BC = torch.remainder(phiC - phiB + torch.pi, 2*torch.pi) - torch.pi
    w_CD = torch.remainder(phiD - phiC + torch.pi, 2*torch.pi) - torch.pi
    w_DA = torch.remainder(phiA - phiD + torch.pi, 2*torch.pi) - torch.pi
    wind = (w_AB + w_BC + w_CD + w_DA) / (2 * torch.pi)
    
    iy_lo = max(0, iy_center - iy_half_win)
    iy_hi = min(wind.shape[0], iy_center + iy_half_win + 1)
    wind_near = wind[iy_lo:iy_hi, :]
    hits = (torch.abs(wind_near) > 0.5).nonzero(as_tuple=False)
    if hits.shape[0] == 0:
        return torch.tensor([], device=device, dtype=DTYPE)
    
    z1d = torch.linspace(-LZ/2, LZ/2 - DZ, NZ, device=device, dtype=DTYPE)
    return z1d[hits[:, 1]]


def find_vortex_z_center(psi, z_prev=None):
    """Find z of vortex ring via topological phase-winding detection.
    
    Uses 4 detection planes (y=0 x±R, x=0 y±R) with wide x/y filters
    (±10 cells ≈ ±2ξ) to accommodate Kelvin wave oscillations.
    Picks the hit closest to z_prev for each plane, then takes median.
    
    Returns z in [-Lz/2, Lz/2).
    """
    device = psi.device
    hw = 10  # ±10 grid cells ≈ ±2ξ
    
    # Grid indices for ring crossings
    ix_pR = int((R_RING + LX/2) / DX)   # x = +R
    ix_nR = int((-R_RING + LX/2) / DX)  # x = -R
    iy_pR = int((R_RING + LY/2) / DY)   # y = +R
    iy_nR = int((-R_RING + LY/2) / DY)  # y = -R
    iy0 = NY // 2  # y = 0
    ix0 = NX // 2  # x = 0
    
    z_estimates = []
    z_ref = z_prev if z_prev is not None else 0.0
    
    # Detection 1: (x,z) plane at y=0, near x=+R
    z_hits = _plaquette_hits_xz(psi, iy0, ix_pR, hw)
    if z_hits.numel() > 0:
        dz = z_hits - z_ref
        dz = dz - LZ * torch.round(dz / LZ)
        z_estimates.append(z_hits[torch.argmin(torch.abs(dz))].item())
    
    # Detection 2: (x,z) plane at y=0, near x=-R
    z_hits = _plaquette_hits_xz(psi, iy0, ix_nR, hw)
    if z_hits.numel() > 0:
        dz = z_hits - z_ref
        dz = dz - LZ * torch.round(dz / LZ)
        z_estimates.append(z_hits[torch.argmin(torch.abs(dz))].item())
    
    # Detection 3: (y,z) plane at x=0, near y=+R
    z_hits = _plaquette_hits_yz(psi, ix0, iy_pR, hw)
    if z_hits.numel() > 0:
        dz = z_hits - z_ref
        dz = dz - LZ * torch.round(dz / LZ)
        z_estimates.append(z_hits[torch.argmin(torch.abs(dz))].item())
    
    # Detection 4: (y,z) plane at x=0, near y=-R
    z_hits = _plaquette_hits_yz(psi, ix0, iy_nR, hw)
    if z_hits.numel() > 0:
        dz = z_hits - z_ref
        dz = dz - LZ * torch.round(dz / LZ)
        z_estimates.append(z_hits[torch.argmin(torch.abs(dz))].item())
    
    if not z_estimates:
        return z_ref
    
    # Circular median (pick the estimate closest to the circular mean)
    z_arr = torch.tensor(z_estimates, device=device, dtype=DTYPE)
    angles = (2.0 * torch.pi * z_arr) / LZ
    z_mean = (LZ / (2.0 * torch.pi)) * torch.atan2(
        torch.sin(angles).mean(), torch.cos(angles).mean()
    ).item()
    
    # Pick estimate closest to circular mean
    dz_from_mean = z_arr - z_mean
    dz_from_mean = dz_from_mean - LZ * torch.round(dz_from_mean / LZ)
    best = torch.argmin(torch.abs(dz_from_mean))
    return z_arr[best].item()


def unwrap_z(z_list):
    """Unwrap periodic z positions to get cumulative displacement."""
    if len(z_list) < 2:
        return np.array(z_list)
    unwrapped = [z_list[0]]
    for i in range(1, len(z_list)):
        dz = z_list[i] - z_list[i-1]
        # If jump > Lz/2, it crossed the boundary
        if dz > LZ / 2:
            dz -= LZ
        elif dz < -LZ / 2:
            dz += LZ
        unwrapped.append(unwrapped[-1] + dz)
    return np.array(unwrapped)


def compute_z_ring(psi, z_prev=None):
    """Ring z-position via density minimum tracking."""
    return find_vortex_z_center(psi, z_prev=z_prev)


def compute_core_void_mass_adaptive(psi, X, Y, Z, z_prev=None):
    """Core void mass within 3ξ of ring core, periodic z-distance."""
    z_center = find_vortex_z_center(psi, z_prev=z_prev)
    rho = torch.abs(psi)**2
    void = torch.clamp(RHO_0 - rho, min=0.0)

    rho_cyl = torch.sqrt(X**2 + Y**2)
    dz = Z - z_center
    dz = dz - LZ * torch.round(dz / LZ)  # periodic z-distance
    d_ring = torch.sqrt((rho_cyl - R_RING)**2 + dz**2)
    mask = (d_ring < 3.0 * XI)
    return torch.sum(void * mask).item() * DV, z_center


# ══════════════════════════════════════════════════════════════════════
#  Split-Step Real-Time Steppers
# ══════════════════════════════════════════════════════════════════════
def real_time_step_kick(psi, kinetic_prop, dt, F_now, Z):
    """Strang split-step with uniform body force via momentum kick.
    ψ → ψ·exp(i·F·dt·z) applied symmetrically for Strang splitting."""
    hdt = dt / 2.0

    # Half momentum kick
    if abs(F_now) > 0:
        psi = psi * torch.exp(1j * F_now * hdt * Z)

    # Potential half-step (GP nonlinearity only)
    V = G_NL * torch.abs(psi)**2 - MU
    psi = psi * torch.exp(-1j * V * hdt)

    # Full kinetic step
    psi_k = fft.fftn(psi)
    psi_k *= kinetic_prop
    psi = fft.ifftn(psi_k)

    # Potential half-step
    V = G_NL * torch.abs(psi)**2 - MU
    psi = psi * torch.exp(-1j * V * hdt)

    # Half momentum kick
    if abs(F_now) > 0:
        psi = psi * torch.exp(1j * F_now * hdt * Z)

    return psi


def real_time_step_free(psi, kinetic_prop, dt):
    """Strang split-step with NO external force (for settling)."""
    hdt = dt / 2.0
    V = G_NL * torch.abs(psi)**2 - MU
    psi = psi * torch.exp(-1j * V * hdt)
    psi_k = fft.fftn(psi)
    psi_k *= kinetic_prop
    psi = fft.ifftn(psi_k)
    V = G_NL * torch.abs(psi)**2 - MU
    psi = psi * torch.exp(-1j * V * hdt)
    return psi


# ══════════════════════════════════════════════════════════════════════
#  Single Force Run
# ══════════════════════════════════════════════════════════════════════
def run_single_force(F_val, psi_init, X, Y, Z, K2, device, z_prev_init=None):
    print(f"\n{'='*62}")
    print(f"  FORCE  F = {F_val}")
    print(f"  Ramp: {RAMP_TIME:.0f}τ → Hold: {EVOLVE_TIME-RAMP_TIME:.0f}τ → "
          f"Total: {EVOLVE_TIME:.0f}τ")
    print(f"{'='*62}", flush=True)

    psi = psi_init.clone()
    z_prev = z_prev_init
    z0_raw = compute_z_ring(psi, z_prev=z_prev)
    z_prev = z0_raw
    vm0, _ = compute_core_void_mass_adaptive(psi, X, Y, Z, z_prev=z_prev)
    print(f"  Init: z_ring={z0_raw:.6f}, core_void={vm0:.4f}", flush=True)

    dt = DT_FIXED
    kinetic_prop = torch.exp(-1j * 0.5 * K2 * dt)
    n_total = int(EVOLVE_TIME / dt)
    sample_every = int(SAMPLE_INTERVAL / dt)
    print(f"  dt={dt}, steps={n_total}, sample every {sample_every}", flush=True)

    times = [0.0]
    z_raw_list = [z0_raw]
    cvoids = [vm0]
    status = "OK"
    tw = time.time()

    for step in range(1, n_total + 1):
        t = step * dt
        F_now = F_val * min(t / RAMP_TIME, 1.0)
        psi = real_time_step_kick(psi, kinetic_prop, dt, F_now, Z)

        if step % sample_every == 0:
            zc = compute_z_ring(psi, z_prev=z_prev)
            z_prev = zc
            vm, _ = compute_core_void_mass_adaptive(psi, X, Y, Z, z_prev=zc)
            times.append(t)
            z_raw_list.append(zc)
            cvoids.append(vm)

            if vm0 > 0:
                chg = abs(vm - vm0) / vm0
                if chg > VOID_MASS_CHANGE_TOL:
                    print(f"  ⚠ VOID Δ {chg*100:.1f}% at t={t:.1f}", flush=True)
                    status = f"VOID_CHANGE at t={t:.1f}"
                    break
                loss = (vm0 - vm) / vm0
                if loss > RADIATION_LOSS_TOL:
                    print(f"  ⚠ RAD LOSS {loss*100:.1f}% at t={t:.1f}", flush=True)
                    status = f"RADIATION at t={t:.1f}"
                    break

            wall = time.time() - tw
            z_unwrapped = unwrap_z(z_raw_list)
            print(f"  t={t:7.1f}  z_ring={z_unwrapped[-1]:+.4f}  "
                  f"cvoid={vm:.4f}  [{wall:.0f}s]", flush=True)

    del kinetic_prop
    wall = time.time() - tw
    vram = torch.cuda.max_memory_allocated(device) / 1e9
    print(f"  {n_total} steps, {wall:.1f}s, VRAM {vram:.2f} GB", flush=True)

    z_coms_unwrapped = unwrap_z(z_raw_list)
    return np.array(times), z_coms_unwrapped, np.array(cvoids), status, z_prev


# ══════════════════════════════════════════════════════════════════════
#  Fit
# ══════════════════════════════════════════════════════════════════════
def quadratic_model(t, z0, v0, a):
    return z0 + v0 * t + 0.5 * a * t**2


def fit_trajectory(times, z_coms, ramp_time):
    mask = times >= ramp_time
    tf, zf = times[mask], z_coms[mask]
    if len(tf) < 4:
        return None, None, None, {}
    ts = tf - tf[0]
    try:
        popt, pcov = curve_fit(quadratic_model, ts, zf, p0=[zf[0], 0, 0])
        z0, v0, a = popt
        pe = np.sqrt(np.diag(pcov))
        res = zf - quadratic_model(ts, *popt)
        sstot = np.sum((zf - np.mean(zf))**2)
        r2 = 1 - np.sum(res**2)/sstot if sstot > 0 else 0
        return z0, v0, a, {'a_err': pe[2], 'r_squared': r2,
                           't_fit': tf, 'z_fit': zf,
                           'z_model': quadratic_model(ts, *popt)}
    except RuntimeError:
        return None, None, None, {}


# ══════════════════════════════════════════════════════════════════════
#  Plotting
# ══════════════════════════════════════════════════════════════════════
def plot_per_force(all_results, m_effs, outdir):
    """One plot per F: z_ring(t) with quadratic fit overlay."""
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
    for i, (F, r) in enumerate(all_results.items()):
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.plot(r['times'], r['z_coms'], 'o-', color=colors[i % 4], ms=3,
                label=f'z_ring(t) [F={F}]')
        fi = r.get('fit_info', {})
        if fi.get('t_fit') is not None:
            ax.plot(fi['t_fit'], fi['z_model'], '--', color='red',
                    lw=2, alpha=0.8, label='Quadratic fit')
        ax.axvline(RAMP_TIME, color='gray', ls=':', label='Ramp end')
        ax.set_xlabel('Time [τ_heal]')
        ax.set_ylabel('z_ring (unwrapped) [ξ]')
        info = m_effs.get(F, {})
        meff_s = f"{info['m_eff']:.4f}" if info.get('m_eff') is not None else "N/A"
        r2_s = f"{info['r_squared']:.6f}" if info.get('r_squared') is not None else "N/A"
        a_s = f"{info.get('a', 0):.6e}" if info.get('a') is not None else "N/A"
        v0_s = f"{info.get('v0', 0):.6f}" if info.get('v0') is not None else "N/A"
        ax.set_title(f'F={F}  |  a={a_s}  |  v0={v0_s}  |  m_eff={meff_s}  |  R²={r2_s}')
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        path = os.path.join(outdir, f'zcom_F{F}.png')
        fig.savefig(path, dpi=150)
        plt.close(fig)
        print(f"  Plot: {path}", flush=True)


def plot_dashboard(all_results, m_effs, outdir):
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']

    ax = axes[0, 0]
    for i, (F, r) in enumerate(all_results.items()):
        ax.plot(r['times'], r['z_coms'], 'o-', color=colors[i], ms=3,
                label=f'F={F}')
        fi = r.get('fit_info', {})
        if fi.get('t_fit') is not None:
            ax.plot(fi['t_fit'], fi['z_model'], '--', color=colors[i],
                    lw=2, alpha=0.7)
    ax.axvline(RAMP_TIME, color='gray', ls=':', label='Ramp end')
    ax.set_xlabel('Time [τ]'); ax.set_ylabel('z_ring (unwrapped) [ξ]')
    ax.set_title('Vortex Ring z_ring(t)'); ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    ax = axes[0, 1]
    for i, (F, r) in enumerate(all_results.items()):
        t, z = r['times'], r['z_coms']
        m = t >= RAMP_TIME
        if np.sum(m) > 1:
            tp = t[m] - RAMP_TIME
            ax.plot(tp**2, z[m], 'o-', color=colors[i], ms=3, label=f'F={F}')
    ax.set_xlabel('(t−t_ramp)² [τ²]'); ax.set_ylabel('z_ring [ξ]')
    ax.set_title('z_com vs t² (linearity ⇒ const accel)'); ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    ax = axes[1, 0]
    vF = [F for F in sorted(m_effs) if m_effs[F].get('m_eff') is not None]
    if vF:
        mv = [m_effs[F]['m_eff'] for F in vF]
        me = [m_effs[F].get('m_eff_err', 0) or 0 for F in vF]
        ax.errorbar(vF, mv, yerr=me, fmt='o-', color='#d62728',
                    capsize=5, ms=8, lw=2)
        if len(mv) >= 2:
            mm = np.mean(mv)
            ax.axhline(mm, color='green', ls='--', lw=1.5,
                       label=f'Mean={mm:.4f}')
            ax.fill_between([min(vF)*0.5, max(vF)*1.5],
                            mm*0.99, mm*1.01, alpha=0.2, color='green',
                            label='±1%')
    ax.set_xlabel('Force F'); ax.set_ylabel('m_eff = F/a')
    ax.set_title('Effective Mass vs Force'); ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    ax = axes[1, 1]
    for i, (F, r) in enumerate(all_results.items()):
        t, cv = r['times'], r['core_voids']
        if len(cv) > 0 and cv[0] > 0:
            ax.plot(t, np.array(cv)/cv[0], 'o-', color=colors[i], ms=3,
                    label=f'F={F}')
    ax.axhline(0.95, color='red', ls=':', label='5% loss')
    ax.set_xlabel('Time [τ]'); ax.set_ylabel('Core Void / Initial')
    ax.set_title('Vortex Core Stability')
    ax.legend(fontsize=9); ax.grid(True, alpha=0.3)
    ax.set_ylim(0.5, 1.5)

    fig.suptitle(f'UHF — Hydrodynamic Inertia Theorem v3\n'
                 f'Ring R={R_RING}ξ, Grid {NX}×{NY}×{NZ}, '
                 f'Lxy={LX}ξ Lz={LZ}ξ, Momentum-Kick Body Force',
                 fontsize=13, fontweight='bold')
    plt.tight_layout(rect=[0, 0, 1, 0.93])
    path = os.path.join(outdir, 'hydro_inertia_dashboard_v3.png')
    fig.savefig(path, dpi=200); plt.close(fig)
    print(f"\n  Dashboard: {path}", flush=True)


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  UHF — Hydrodynamic Inertia v3: Momentum-Kick Force       ║")
    print("║  3D SSS GP Solver, Topological Ring Tracker                ║")
    print("╚══════════════════════════════════════════════════════════════╝",
          flush=True)

    device = setup_device()
    os.makedirs(OUTDIR, exist_ok=True)

    print(f"\n  Grid: {NX}×{NY}×{NZ} = {NX*NY*NZ:,}")
    print(f"  Δx={DX:.4f}, Δy={DY:.4f}, Δz={DZ:.4f}")
    print(f"  Lx=Ly={LX}ξ, Lz={LZ}ξ", flush=True)

    X, Y, Z, K2 = build_grids(device)
    mem = torch.cuda.memory_allocated(device)
    print(f"  VRAM after grids: {mem/1e9:.2f} GB", flush=True)

    # ── Vortex ring ──
    print(f"\n  Vortex ring: R={R_RING}ξ in x-y plane at z=0", flush=True)
    psi, phase_target = init_vortex_ring(X, Y, Z, device)
    rho = torch.abs(psi)**2
    ix = min(int((R_RING + LX/2) / DX), NX-1)
    iy = NY // 2
    iz = NZ // 2
    print(f"  ρ range: [{rho.min().item():.6f}, {rho.max().item():.6f}]")
    print(f"  ρ at core: {rho[ix,iy,iz].item():.6f}", flush=True)

    # ── Imaginary-time relaxation ──
    psi = imaginary_time_relax(psi, phase_target, K2, device)
    torch.cuda.synchronize(device)
    del phase_target  # no longer needed

    z_prev = None  # initial tracking anchor
    z0 = compute_z_ring(psi, z_prev=z_prev)
    z_prev = z0
    vm0, _ = compute_core_void_mass_adaptive(psi, X, Y, Z, z_prev=z_prev)
    print(f"\n  Post-relax: z_ring={z0:.6f}, core_void={vm0:.4f}", flush=True)

    # ── Pre-flight settling (F=0) to damp phonons ──
    print(f"\n  ┌─ Pre-flight settling: {SETTLE_TIME}τ with F=0", flush=True)
    kinetic_prop_settle = torch.exp(-1j * 0.5 * K2 * DT_FIXED)
    n_settle = int(SETTLE_TIME / DT_FIXED)
    sample_s = int(SAMPLE_INTERVAL / DT_FIXED)
    settle_z_raw = [z0]
    t0w = time.time()
    for step in range(1, n_settle + 1):
        psi = real_time_step_free(psi, kinetic_prop_settle, DT_FIXED)
        if step % sample_s == 0:
            t_s = step * DT_FIXED
            zc = compute_z_ring(psi, z_prev=z_prev)
            z_prev = zc
            settle_z_raw.append(zc)
            vm, _ = compute_core_void_mass_adaptive(psi, X, Y, Z, z_prev=zc)
            z_unw = unwrap_z(settle_z_raw)
            wall = time.time() - t0w
            print(f"  │  t={t_s:6.1f}  z_ring={z_unw[-1]:+.4f}  "
                  f"cvoid={vm:.4f}  [{wall:.0f}s]", flush=True)
    del kinetic_prop_settle
    zc_final = compute_z_ring(psi, z_prev=z_prev)
    z_prev = zc_final
    vm_settled, _ = compute_core_void_mass_adaptive(psi, X, Y, Z, z_prev=z_prev)
    z_unw = unwrap_z(settle_z_raw)
    wall = time.time() - t0w
    v_self = (z_unw[-1] - z_unw[0]) / SETTLE_TIME if len(z_unw) > 1 else 0.0
    print(f"  └─ Settled: z_ring(unwrap)={z_unw[-1]:.4f}, cvoid={vm_settled:.4f}, "
          f"v_self≈{v_self:.4f} ξ/τ  [{wall:.0f}s]", flush=True)
    print(f"  VRAM: {torch.cuda.memory_allocated(device)/1e9:.2f} GB", flush=True)

    # ── Force sweep ──
    all_results = {}
    m_effs = {}

    for F_val in FORCES:
        torch.cuda.reset_peak_memory_stats(device)
        times, z_coms, cvoids, status, _ = run_single_force(
            F_val, psi, X, Y, Z, K2, device, z_prev_init=z_prev)

        res = {'times': times, 'z_coms': z_coms,
               'core_voids': cvoids, 'status': status, 'fit_info': {}}

        if status == "OK":
            _, v0f, af, fi = fit_trajectory(times, z_coms, RAMP_TIME)
            res['a_fit'] = af; res['fit_info'] = fi
            if af is not None and abs(af) > 1e-20:
                meff = F_val / af
                merr = F_val * fi.get('a_err', 0) / af**2
                m_effs[F_val] = {'m_eff': meff, 'm_eff_err': merr,
                                 'a': af, 'a_err': fi.get('a_err'),
                                 'v0': v0f,
                                 'r_squared': fi.get('r_squared')}
                print(f"\n  F={F_val}: v0={v0f:.6f}, a={af:.6e}, "
                      f"m_eff={meff:.6f}, R²={fi['r_squared']:.6f}",
                      flush=True)
            else:
                m_effs[F_val] = {'m_eff': None, 'a': af}
                print(f"\n  F={F_val}: fit failed (a≈0)", flush=True)
        else:
            m_effs[F_val] = {'m_eff': None, 'a': None, 'status': status}
            print(f"\n  F={F_val}: {status}", flush=True)

        all_results[F_val] = res

    # ── Results Table ──
    print("\n" + "═"*70)
    print("  RESULTS TABLE")
    print("═"*70)
    hdr = f"  {'F':>8s}  {'v0':>12s}  {'a':>14s}  {'m_eff':>12s}  {'R²':>10s}  Status"
    print(hdr)
    print(f"  {'─'*8}  {'─'*12}  {'─'*14}  {'─'*12}  {'─'*10}  {'─'*20}")
    for F in FORCES:
        info = m_effs.get(F, {})
        v0_s = f"{info['v0']:.6f}" if info.get('v0') is not None else "—"
        a_s = f"{info['a']:.6e}" if info.get('a') is not None else "—"
        m_s = f"{info['m_eff']:.6f}" if info.get('m_eff') is not None else "—"
        r_s = f"{info['r_squared']:.6f}" if info.get('r_squared') is not None else "—"
        st = info.get('status', all_results[F]['status'])
        print(f"  {F:8.4f}  {v0_s:>12s}  {a_s:>14s}  {m_s:>12s}  {r_s:>10s}  {st}")

    # ── Constancy check ──
    valid_m = [m_effs[F]['m_eff'] for F in FORCES
               if m_effs.get(F, {}).get('m_eff') is not None]
    m_mean = m_std = m_var = max_dev = None

    if len(valid_m) >= 2:
        m_mean = float(np.mean(valid_m))
        m_std  = float(np.std(valid_m))
        m_var  = m_std / abs(m_mean) if abs(m_mean) > 1e-30 else float('inf')
        max_dev = max(abs(m - m_mean) / abs(m_mean) for m in valid_m)
        print(f"\n  m_eff mean    = {m_mean:.6f}")
        print(f"  m_eff std     = {m_std:.6f}")
        print(f"  Variation     = {m_var*100:.4f}%")
        print(f"  Max deviation = {max_dev*100:.4f}%")
        if max_dev < 0.01:
            conc = (f"✓ HYDRODYNAMIC INERTIA CONFIRMED: m_eff constant "
                    f"within {max_dev*100:.2f}% (< 1%)")
        else:
            conc = (f"✗ m_eff varies by {max_dev*100:.2f}% "
                    f"(exceeds 1% tolerance)")
    elif len(valid_m) == 1:
        m_mean = valid_m[0]
        conc = f"Only 1 valid m_eff = {m_mean:.6f}; need ≥2"
    else:
        conc = "No valid m_eff obtained"

    print(f"\n  CONCLUSION: {conc}", flush=True)

    # ── Plots ──
    plot_per_force(all_results, m_effs, OUTDIR)
    plot_dashboard(all_results, m_effs, OUTDIR)

    # ── JSON ──
    jd = {
        'parameters': {
            'NX': NX, 'NY': NY, 'NZ': NZ,
            'LX': LX, 'LY': LY, 'LZ': LZ,
            'xi': XI, 'rho0': RHO_0, 'R_ring': R_RING,
            'dx': DX, 'dy': DY, 'dz': DZ,
            'ramp_time': RAMP_TIME, 'evolve_time': EVOLVE_TIME,
            'settle_time': SETTLE_TIME,
            'forces': FORCES,
            'V_ext': 'momentum_kick: psi *= exp(i*F*dt*z)'},
        'results': {},
        'conclusion': {
            'm_eff_mean': m_mean,
            'm_eff_std': float(m_std) if m_std is not None else None,
            'variation_pct': float(m_var*100) if m_var is not None else None,
            'max_deviation_pct': float(max_dev*100) if max_dev is not None else None,
            'within_1pct': bool(max_dev < 0.01) if max_dev is not None else None,
            'text': conc}
    }
    for F in FORCES:
        info = m_effs.get(F, {})
        jd['results'][str(F)] = {
            'F': F, 'acceleration': info.get('a'),
            'm_eff': info.get('m_eff'), 'm_eff_err': info.get('m_eff_err'),
            'r_squared': info.get('r_squared'),
            'status': all_results[F]['status'],
            'times': all_results[F]['times'].tolist(),
            'z_coms': all_results[F]['z_coms'].tolist()}

    jp = os.path.join(OUTDIR, 'hydro_inertia_v3_results.json')
    with open(jp, 'w') as f:
        json.dump(jd, f, indent=2)
    print(f"  JSON: {jp}", flush=True)

    print("\n" + "═"*70)
    print(f"  {conc}")
    print("═"*70, flush=True)


if __name__ == '__main__':
    main()
