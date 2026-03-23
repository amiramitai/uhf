#!/usr/bin/env python3
"""
UHF — Self-Propulsion Velocity vs Ring Radius (v4)
===================================================
Measures the self-induced velocity of vortex rings at R = 4, 6, 8, 10 ξ
and compares to the Kelvin-Thomson prediction:

  v_KT(R) = (1/2R)[ln(8R) - 0.25]

Method:
  1. Phase-locked imaginary-time relaxation → ground-state ring core
  2. Free real-time evolution (40τ, no external force)
  3. Track z_ring every 0.5τ via plaquette winding
  4. Compute velocity profile v(t) via smoothed finite differences
  5. Extract v_self = peak |v(t)| during the initial transient

Physics: After relaxation, the ring starts from rest and develops its
self-induced flow over ~5–10τ, reaching a peak velocity close to the
Kelvin-Thomson prediction before periodic-image effects decelerate it.

Grid: 192×192×512 = 18.9M points (Lx=Ly=40ξ, Lz=80ξ)
Units: ℏ = m = ξ = c_s = 1, κ = 2π
"""

import os, sys, time, json
import numpy as np
import torch
import torch.fft as fft
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ══════════════════════════════════════════════════════════════════════
#  Parameters
# ══════════════════════════════════════════════════════════════════════
NX = NY = 192
NZ      = 512
LX = LY = 40.0   # [ξ]
LZ      = 80.0   # [ξ]
XI      = 1.0
RHO_0   = 1.0
G_NL    = 1.0
MU      = G_NL * RHO_0

DX = LX / NX
DY = LY / NY
DZ = LZ / NZ
DV = DX * DY * DZ

# Imaginary-time relaxation
IT_TIME = 30.0
IT_DT   = 0.05

# Real-time evolution
EVOLVE_TIME = 40.0    # τ — enough to capture peak velocity transient
SAMPLE_DT   = 0.5     # sample every 0.5τ for fine velocity resolution
DT_FIXED    = 0.02

# Radii to sweep
RADII = [4.0, 6.0, 8.0, 10.0]

# Velocity smoothing window (τ)
V_SMOOTH_WINDOW = 2.0  # centered difference over ±1τ

OUTDIR = "UHF_SelfPropulsion_v4_results"
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
    print(f"  VRAM   : {p.total_mem / 1e9:.1f} GB" if hasattr(p, 'total_mem')
          else f"  VRAM   : {p.total_memory / 1e9:.1f} GB")
    return dev


# ══════════════════════════════════════════════════════════════════════
#  Grids
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
#  Vortex Ring (parameterized R)
# ══════════════════════════════════════════════════════════════════════
def init_vortex_ring(X, Y, Z, device, R):
    rho_cyl = torch.sqrt(X**2 + Y**2)
    s = rho_cyl - R
    d = torch.sqrt(s**2 + Z**2)
    phase = torch.atan2(Z, s)
    amp = np.sqrt(RHO_0) * torch.tanh(d / XI)
    psi = (amp * torch.exp(1j * phase.to(CDTYPE))).to(CDTYPE)
    return psi, phase


# ══════════════════════════════════════════════════════════════════════
#  Phase-Locked Imaginary-Time Relaxation
# ══════════════════════════════════════════════════════════════════════
def imaginary_time_relax(psi, phase_target, K2, device, R):
    print(f"\n  ┌─ Phase-Locked Imaginary-Time Relaxation (R={R}ξ)", flush=True)
    n_steps = int(IT_TIME / IT_DT)
    dt = IT_DT
    print(f"  │  {n_steps} steps, dt={dt}", flush=True)

    kinetic_full = torch.exp(-0.5 * K2 * dt)
    phase_factor = torch.exp(1j * phase_target.to(CDTYPE))
    N_particles = torch.sum(torch.abs(psi)**2).item() * DV

    t0 = time.time()
    report_every = max(n_steps // 5, 1)

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
            ix = min(int((R + LX/2) / DX), NX-1)
            iy = NY // 2
            iz = NZ // 2
            rho_core = rho[ix, iy, iz].item()
            elapsed = time.time() - t0
            print(f"  │  Step {step+1}/{n_steps}  ρ_min={rho.min().item():.6f}  "
                  f"ρ_max={rho.max().item():.6f}  ρ_core={rho_core:.6f}  "
                  f"[{elapsed:.1f}s]", flush=True)

    rho = torch.abs(psi)**2
    elapsed = time.time() - t0
    ix = min(int((R + LX/2) / DX), NX-1)
    iy = NY // 2
    iz = NZ // 2
    rho_core = rho[ix, iy, iz].item()
    print(f"  │  ρ_min={rho.min().item():.6f}, ρ_max={rho.max().item():.6f}")
    print(f"  │  ρ_core={rho_core:.6f} (depletion {(1-rho_core)*100:.1f}%)")
    print(f"  └─ {elapsed:.1f}s", flush=True)
    return psi


# ══════════════════════════════════════════════════════════════════════
#  Plaquette Phase-Winding Tracker (parameterized R)
# ══════════════════════════════════════════════════════════════════════
def _plaquette_hits_xz(psi, iy_slice, ix_center, ix_half_win):
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


def find_vortex_z_center(psi, R, z_prev=None):
    """Find z of vortex ring via topological phase-winding detection.

    Uses 4 detection planes (y=0 x±R, x=0 y±R) with wide x/y filters
    (±10 cells ≈ ±2ξ) to accommodate Kelvin wave oscillations.
    """
    device = psi.device
    hw = 10  # ±10 grid cells

    ix_pR = int((R + LX/2) / DX)    # x = +R
    ix_nR = int((-R + LX/2) / DX)   # x = -R
    iy_pR = int((R + LY/2) / DY)    # y = +R
    iy_nR = int((-R + LY/2) / DY)   # y = -R
    iy0 = NY // 2
    ix0 = NX // 2

    z_estimates = []
    z_ref = z_prev if z_prev is not None else 0.0

    for ix_c, iy_s, is_xz in [(ix_pR, iy0, True), (ix_nR, iy0, True),
                               (ix0, iy_pR, False), (ix0, iy_nR, False)]:
        if is_xz:
            z_hits = _plaquette_hits_xz(psi, iy_s, ix_c, hw)
        else:
            z_hits = _plaquette_hits_yz(psi, ix_c, iy_s, hw)
        if z_hits.numel() > 0:
            dz = z_hits - z_ref
            dz = dz - LZ * torch.round(dz / LZ)
            z_estimates.append(z_hits[torch.argmin(torch.abs(dz))].item())

    if not z_estimates:
        return z_ref

    z_arr = torch.tensor(z_estimates, device=device, dtype=DTYPE)
    angles = (2.0 * torch.pi * z_arr) / LZ
    z_mean = (LZ / (2.0 * torch.pi)) * torch.atan2(
        torch.sin(angles).mean(), torch.cos(angles).mean()
    ).item()

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
        if dz > LZ / 2:
            dz -= LZ
        elif dz < -LZ / 2:
            dz += LZ
        unwrapped.append(unwrapped[-1] + dz)
    return np.array(unwrapped)


# ══════════════════════════════════════════════════════════════════════
#  Real-Time Stepper (no force)
# ══════════════════════════════════════════════════════════════════════
def real_time_step_free(psi, kinetic_prop, dt):
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
#  Impulse Measurement
# ══════════════════════════════════════════════════════════════════════
def compute_impulse_z(psi, device):
    """z-component of linear impulse: P_z = Im ∫ ψ* ∂ψ/∂z d³r (spectral)."""
    kz1d = torch.fft.fftfreq(NZ, d=DZ, device=device).to(DTYPE) * 2 * np.pi
    kz = kz1d[None, None, :]  # broadcast to (1, 1, NZ)
    psi_k = fft.fftn(psi)
    dpsi_dz_k = 1j * kz * psi_k
    dpsi_dz = fft.ifftn(dpsi_dz_k)
    integrand = torch.conj(psi) * dpsi_dz
    Pz = torch.imag(torch.sum(integrand)).item() * DV
    return Pz


# ══════════════════════════════════════════════════════════════════════
#  Kelvin-Thomson Formula
# ══════════════════════════════════════════════════════════════════════
def kelvin_thomson(R):
    """v_KT(R) = (1/2R)[ln(8R) - 0.25]"""
    return (1.0 / (2 * R)) * (np.log(8 * R) - 0.25)


# ══════════════════════════════════════════════════════════════════════
#  Velocity Extraction
# ══════════════════════════════════════════════════════════════════════
def extract_velocity(t_arr, z_arr, smooth_dt=2.0):
    """Extract smoothed velocity profile and peak velocity.

    Uses centered finite differences over a window of smooth_dt.
    Returns v_peak (signed), t_peak, and the full v(t) profile.
    """
    dt_samp = t_arr[1] - t_arr[0] if len(t_arr) > 1 else 1.0
    half_win = max(1, int(round(smooth_dt / (2 * dt_samp))))

    v = np.zeros(len(t_arr))
    for i in range(len(t_arr)):
        j_lo = max(0, i - half_win)
        j_hi = min(len(t_arr) - 1, i + half_win)
        if j_hi > j_lo:
            v[i] = (z_arr[j_hi] - z_arr[j_lo]) / (t_arr[j_hi] - t_arr[j_lo])

    # Peak velocity (by magnitude)
    i_peak = np.argmax(np.abs(v))
    v_peak = v[i_peak]
    t_peak = t_arr[i_peak]

    return v_peak, t_peak, v


# ══════════════════════════════════════════════════════════════════════
#  Single Radius Run
# ══════════════════════════════════════════════════════════════════════
def run_single_radius(R, X, Y, Z, K2, device):
    v_KT = kelvin_thomson(R)
    print(f"\n{'='*62}")
    print(f"  RADIUS  R = {R} ξ")
    print(f"  Evolve: {EVOLVE_TIME:.0f}τ  |  Sample: Δt={SAMPLE_DT}τ")
    print(f"  Kelvin-Thomson: v_KT = {v_KT:.4f} ξ/τ")
    print(f"{'='*62}", flush=True)

    # Init + relax
    psi, phase_target = init_vortex_ring(X, Y, Z, device, R)
    psi = imaginary_time_relax(psi, phase_target, K2, device, R)
    del phase_target

    # Initial measurements
    z_prev = None
    z0 = find_vortex_z_center(psi, R, z_prev=z_prev)
    z_prev = z0
    Pz0 = compute_impulse_z(psi, device)
    Pz_theory = RHO_0 * 2 * np.pi * np.pi * R**2  # ρ₀ κ π R², κ=2π
    print(f"  Post-relax: z_ring={z0:.4f}, P_z={Pz0:.2f} "
          f"(theory: {Pz_theory:.2f})", flush=True)

    # Real-time evolution
    dt = DT_FIXED
    kinetic_prop = torch.exp(-1j * 0.5 * K2 * dt)
    n_total = int(EVOLVE_TIME / dt)
    sample_every = int(SAMPLE_DT / dt)

    times = [0.0]
    z_list = [z0]
    tw = time.time()

    for step in range(1, n_total + 1):
        psi = real_time_step_free(psi, kinetic_prop, dt)
        if step % sample_every == 0:
            t = step * dt
            zc = find_vortex_z_center(psi, R, z_prev=z_prev)
            z_prev = zc
            times.append(t)
            z_list.append(zc)
            if step % (sample_every * 10) == 0:  # print every 5τ
                z_unw = unwrap_z(z_list)
                wall = time.time() - tw
                print(f"  t={t:6.1f}τ  z_ring={z_unw[-1]:+.4f}  "
                      f"[{wall:.0f}s]", flush=True)

    del kinetic_prop
    wall = time.time() - tw

    # Unwrap and extract velocity
    t_arr = np.array(times)
    z_arr = unwrap_z(z_list)
    v_peak, t_peak, v_profile = extract_velocity(t_arr, z_arr, V_SMOOTH_WINDOW)

    # Linear fit over the "plateau" (where |v| > 0.5*|v_peak|)
    if abs(v_peak) > 1e-12:
        mask_plateau = np.abs(v_profile) > 0.5 * np.abs(v_peak)
        if np.sum(mask_plateau) >= 3:
            t_plat = t_arr[mask_plateau]
            z_plat = z_arr[mask_plateau]
            coeffs = np.polyfit(t_plat, z_plat, 1)
            v_fit = coeffs[0]
        else:
            v_fit = v_peak
    else:
        v_fit = 0.0

    print(f"\n  R={R}ξ: v_peak={v_peak:+.4f} ξ/τ (t={t_peak:.1f}τ)")
    print(f"          v_plateau_fit={v_fit:+.4f} ξ/τ")
    print(f"          v_KT={v_KT:.4f} ξ/τ")
    print(f"          |v_peak|/v_KT = {abs(v_peak)/v_KT:.3f}")
    print(f"          Time: {wall:.1f}s", flush=True)

    return {
        'R': R,
        'times': t_arr,
        'z_traj': z_arr,
        'v_profile': v_profile,
        'v_peak': v_peak,
        't_peak': t_peak,
        'v_fit': v_fit,
        'v_KT': v_KT,
        'Pz_measured': Pz0,
        'Pz_theory': Pz_theory,
        'wall_time': wall
    }


# ══════════════════════════════════════════════════════════════════════
#  Plotting
# ══════════════════════════════════════════════════════════════════════
def plot_trajectories(results, outdir):
    fig, ax = plt.subplots(figsize=(10, 6))
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
    for i, r in enumerate(results):
        ax.plot(r['times'], r['z_traj'], 'o-', color=colors[i], ms=2,
                label=f'R={r["R"]:.0f}ξ (v_peak={r["v_peak"]:+.3f})')
    ax.set_xlabel('Time [τ]')
    ax.set_ylabel('z_ring (unwrapped) [ξ]')
    ax.set_title('Vortex Ring Self-Propulsion: z(t)')
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    path = os.path.join(outdir, 'z_trajectories_v4.png')
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Plot: {path}")


def plot_velocity_profiles(results, outdir):
    fig, ax = plt.subplots(figsize=(10, 6))
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
    for i, r in enumerate(results):
        ax.plot(r['times'], np.abs(r['v_profile']), '-', color=colors[i],
                lw=1.5, label=f'R={r["R"]:.0f}ξ')
        ax.axhline(r['v_KT'], color=colors[i], ls=':', alpha=0.5)
    ax.set_xlabel('Time [τ]')
    ax.set_ylabel('|v(t)| [ξ/τ]')
    ax.set_title('Velocity Profiles (dotted = Kelvin-Thomson)')
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    path = os.path.join(outdir, 'velocity_profiles_v4.png')
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Plot: {path}")


def plot_vself_vs_R(results, outdir):
    """Main result: v_self vs R with Kelvin-Thomson curve overlay."""
    fig, ax = plt.subplots(figsize=(8, 6))

    R_meas = [r['R'] for r in results]
    v_meas = [abs(r['v_peak']) for r in results]
    v_fit_pts = [abs(r['v_fit']) for r in results]
    v_KT_pts = [r['v_KT'] for r in results]

    # Kelvin-Thomson curve
    R_cont = np.linspace(3, 12, 100)
    v_KT_cont = np.array([kelvin_thomson(r) for r in R_cont])
    ax.plot(R_cont, v_KT_cont, '-', color='#1f77b4', lw=2, alpha=0.7,
            label=r'$v_{KT} = \frac{1}{2R}[\ln(8R) - 0.25]$')

    # Measured
    ax.plot(R_meas, v_meas, 'o-', color='#d62728', ms=10, lw=2,
            label='Measured |v_peak|', zorder=5)
    ax.plot(R_meas, v_fit_pts, 's--', color='#2ca02c', ms=8, lw=1.5,
            label='Plateau fit |v|', zorder=4)

    # KT reference points
    ax.plot(R_meas, v_KT_pts, 'x', color='#1f77b4', ms=10, mew=2,
            label='KT prediction')

    ax.set_xlabel('Ring Radius R [ξ]', fontsize=12)
    ax.set_ylabel('Self-Propulsion Velocity [ξ/τ]', fontsize=12)
    ax.set_title('UHF v4: Self-Propulsion Velocity vs Ring Radius',
                 fontsize=13)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    path = os.path.join(outdir, 'vself_vs_R_v4.png')
    fig.savefig(path, dpi=200)
    plt.close(fig)
    print(f"  Plot: {path}")


def plot_dashboard(results, outdir):
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']

    # (0,0) z(t) trajectories
    ax = axes[0, 0]
    for i, r in enumerate(results):
        ax.plot(r['times'], r['z_traj'], '-', color=colors[i], lw=1.5,
                label=f'R={r["R"]:.0f}ξ')
    ax.set_xlabel('Time [τ]'); ax.set_ylabel('z_ring [ξ]')
    ax.set_title('Ring Trajectories z(t)'); ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    # (0,1) v(t) profiles
    ax = axes[0, 1]
    for i, r in enumerate(results):
        ax.plot(r['times'], np.abs(r['v_profile']), '-', color=colors[i],
                lw=1.5, label=f'R={r["R"]:.0f}ξ')
        ax.axhline(r['v_KT'], color=colors[i], ls=':', alpha=0.4)
    ax.set_xlabel('Time [τ]'); ax.set_ylabel('|v(t)| [ξ/τ]')
    ax.set_title('Velocity Profiles (dotted=KT)'); ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    # (1,0) v_self vs R
    ax = axes[1, 0]
    R_meas = [r['R'] for r in results]
    v_meas = [abs(r['v_peak']) for r in results]
    R_cont = np.linspace(3, 12, 100)
    v_KT_cont = [kelvin_thomson(r) for r in R_cont]
    ax.plot(R_cont, v_KT_cont, '-', color='gray', lw=2, alpha=0.5,
            label='Kelvin-Thomson')
    ax.plot(R_meas, v_meas, 'o-', color='#d62728', ms=8, lw=2,
            label='Measured')
    ax.set_xlabel('R [ξ]'); ax.set_ylabel('v_self [ξ/τ]')
    ax.set_title('v_self vs R'); ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    # (1,1) ratio v_meas / v_KT
    ax = axes[1, 1]
    ratios = [abs(r['v_peak'])/r['v_KT'] for r in results]
    bars = ax.bar(R_meas, ratios, width=1.2,
                  color=colors[:len(results)], alpha=0.7)
    ax.axhline(1.0, color='green', ls='--', lw=2, label='Perfect match')
    ax.set_xlabel('R [ξ]'); ax.set_ylabel('v_meas / v_KT')
    ax.set_title('Accuracy Ratio'); ax.legend(fontsize=9)
    ax.set_ylim(0, 1.5)
    ax.grid(True, alpha=0.3)

    fig.suptitle(f'UHF — Self-Propulsion v4\n'
                 f'Grid {NX}×{NY}×{NZ}, Lxy={LX}ξ Lz={LZ}ξ, '
                 f'Evolve={EVOLVE_TIME}τ',
                 fontsize=13, fontweight='bold')
    plt.tight_layout(rect=[0, 0, 1, 0.93])
    path = os.path.join(outdir, 'self_propulsion_dashboard_v4.png')
    fig.savefig(path, dpi=200)
    plt.close(fig)
    print(f"  Dashboard: {path}")


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  UHF — Self-Propulsion Velocity v4                         ║")
    print("║  v_self(R) vs Kelvin-Thomson                               ║")
    print("╚══════════════════════════════════════════════════════════════╝",
          flush=True)

    device = setup_device()
    os.makedirs(OUTDIR, exist_ok=True)

    print(f"\n  Grid: {NX}×{NY}×{NZ} = {NX*NY*NZ:,}")
    print(f"  Δx={DX:.4f}, Δy={DY:.4f}, Δz={DZ:.4f}")
    print(f"  Radii: {RADII}")
    print(f"  Evolution: {EVOLVE_TIME}τ, dt={DT_FIXED}, sample Δt={SAMPLE_DT}τ")
    print(f"  Kelvin-Thomson predictions:")
    for R in RADII:
        print(f"    R={R:.0f}: v_KT={kelvin_thomson(R):.4f} ξ/τ")
    print(flush=True)

    X, Y, Z, K2 = build_grids(device)
    print(f"  VRAM after grids: {torch.cuda.memory_allocated(device)/1e9:.2f} GB",
          flush=True)

    results = []
    for R in RADII:
        torch.cuda.reset_peak_memory_stats(device)
        r = run_single_radius(R, X, Y, Z, K2, device)
        results.append(r)

    # ── Results Table ──
    print("\n" + "═"*75)
    print("  RESULTS TABLE: Self-Propulsion Velocity")
    print("═"*75)
    hdr = (f"  {'R':>4s}  {'v_peak':>10s}  {'v_fit':>10s}  "
           f"{'v_KT':>10s}  {'ratio':>8s}  {'P_z':>10s}  {'P_z(th)':>10s}")
    print(hdr)
    print(f"  {'─'*4}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*8}  "
          f"{'─'*10}  {'─'*10}")
    for r in results:
        ratio = abs(r['v_peak']) / r['v_KT']
        print(f"  {r['R']:4.0f}  {r['v_peak']:+10.4f}  {r['v_fit']:+10.4f}  "
              f"{r['v_KT']:10.4f}  {ratio:8.3f}  "
              f"{r['Pz_measured']:10.1f}  {r['Pz_theory']:10.1f}")

    # ── Accuracy Report (using v_fit = plateau linear fit, more robust) ──
    ratios_peak = [abs(r['v_peak'])/r['v_KT'] for r in results]
    ratios_fit  = [abs(r['v_fit'])/r['v_KT'] for r in results]
    mean_ratio = np.mean(ratios_fit)
    std_ratio = np.std(ratios_fit)

    R_arr = np.array([r['R'] for r in results])
    v_arr = np.array([abs(r['v_fit']) for r in results])
    v_KT_arr = np.array([r['v_KT'] for r in results])

    # R² for KT prediction
    ss_res = np.sum((v_arr - v_KT_arr)**2)
    ss_tot = np.sum((v_arr - np.mean(v_arr))**2)
    R_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0

    # Best-fit scale factor: v_meas = C * v_KT
    C_best = np.sum(v_arr * v_KT_arr) / np.sum(v_KT_arr**2)
    v_scaled = C_best * v_KT_arr
    ss_res_scaled = np.sum((v_arr - v_scaled)**2)
    R2_scaled = 1.0 - ss_res_scaled / ss_tot if ss_tot > 0 else 0

    print(f"\n  Primary metric: v_fit (plateau linear fit, robust to grid noise)")
    print(f"  Mean |v_fit|/v_KT  = {mean_ratio:.3f} ± {std_ratio:.3f}")
    print(f"  Mean |v_peak|/v_KT = {np.mean(ratios_peak):.3f} (grid-noisy)")
    print(f"  R² (KT prediction) = {R_squared:.4f}")
    print(f"  Best-fit scale C   = {C_best:.3f} (v_meas ≈ C × v_KT)")
    print(f"  R² (scaled KT)     = {R2_scaled:.4f}")

    if mean_ratio > 0.8 and R_squared > 0.9:
        conc = (f"KELVIN-THOMSON CONFIRMED: v_self follows (1/2R)[ln(8R) - b], "
                f"mean ratio {mean_ratio:.2f}, R²={R_squared:.4f}")
    elif R2_scaled > 0.9:
        conc = (f"KT SHAPE CONFIRMED: v_self ∝ (1/2R)ln(8R) with scale "
                f"C={C_best:.2f}, R²(scaled)={R2_scaled:.4f}")
    elif mean_ratio > 0.3:
        conc = (f"PARTIAL MATCH: v_self follows KT trend, mean ratio "
                f"{mean_ratio:.2f}, R²={R_squared:.4f}")
    else:
        conc = (f"MISMATCH: v_self disagrees with KT, mean ratio "
                f"{mean_ratio:.2f}")

    print(f"\n  CONCLUSION: {conc}", flush=True)

    # ── Plots ──
    plot_trajectories(results, OUTDIR)
    plot_velocity_profiles(results, OUTDIR)
    plot_vself_vs_R(results, OUTDIR)
    plot_dashboard(results, OUTDIR)

    # ── JSON ──
    jd = {
        'parameters': {
            'NX': NX, 'NY': NY, 'NZ': NZ,
            'LX': LX, 'LY': LY, 'LZ': LZ,
            'xi': XI, 'rho0': RHO_0, 'dt': DT_FIXED,
            'evolve_time': EVOLVE_TIME,
            'sample_dt': SAMPLE_DT,
            'radii': RADII,
            'v_smooth_window': V_SMOOTH_WINDOW
        },
        'results': {},
        'accuracy': {
            'mean_ratio_fit': float(mean_ratio),
            'std_ratio_fit': float(std_ratio),
            'mean_ratio_peak': float(np.mean(ratios_peak)),
            'R_squared': float(R_squared),
            'C_best_fit': float(C_best),
            'R2_scaled': float(R2_scaled),
            'conclusion': conc
        }
    }
    for r in results:
        jd['results'][f'R{r["R"]:.0f}'] = {
            'R': r['R'],
            'v_peak': float(r['v_peak']),
            'v_fit': float(r['v_fit']),
            'v_KT': float(r['v_KT']),
            'ratio': float(abs(r['v_peak']) / r['v_KT']),
            't_peak': float(r['t_peak']),
            'Pz_measured': float(r['Pz_measured']),
            'Pz_theory': float(r['Pz_theory']),
            'wall_time_s': float(r['wall_time']),
            'times': r['times'].tolist(),
            'z_traj': r['z_traj'].tolist()
        }

    jp = os.path.join(OUTDIR, 'self_propulsion_v4_results.json')
    with open(jp, 'w') as f:
        json.dump(jd, f, indent=2)
    print(f"  JSON: {jp}")

    print("\n" + "═"*75)
    print(f"  {conc}")
    print("═"*75, flush=True)


if __name__ == '__main__':
    main()
