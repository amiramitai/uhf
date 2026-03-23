#!/usr/bin/env python3
"""
UHF — Hydrodynamic Inertia Theorem: 3D Split-Step Fourier GP Solver
=====================================================================
Tests whether a vortex ring in a Gross-Pitaevskii superfluid responds
to an external linear potential V_ext = -F·z with constant effective
mass  m_eff = F / a  independent of F.

Physics
-------
* Gross-Pitaevskii equation:  iℏ ∂ψ/∂t = [-ℏ²/(2m) ∇² + g|ψ|² - μ + V_ext] ψ
* Healing-length units: ℏ = m = ξ = c_s = 1,  g = 1, μ = 1, ρ₀ = 1.
* Domain:  L = 40ξ periodic cube, 256³ grid (Δx = L/N ≈ 0.156ξ).
* Vortex ring:  R = 8ξ in x-y plane, initialized by phase winding +
  imaginary-time relaxation (200 healing times).
* External potential:  V_ext(z) = -F·z.  Ramp F linearly over 50 τ_heal,
  then hold.  Test F = {0.01, 0.05, 0.1, 0.2}.
* Time integration: Strang split-step spectral (SSS) with adaptive dt.
* Diagnostics:  z_com of density void every 10 τ_heal.  Late-time
  quadratic fit → acceleration a → m_eff = F/a.

Hardware: RTX 3090, PyTorch 2.5.1+cu121.
"""

import os
import sys
import time
import json
import numpy as np
import torch
import torch.fft as fft
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit

# ══════════════════════════════════════════════════════════════════════
#  Constants (healing-length units)
# ══════════════════════════════════════════════════════════════════════
N       = 256           # grid points per axis
L       = 40.0          # domain side in units of ξ
XI      = 1.0           # healing length
RHO_0   = 1.0           # background density
G_NL    = 1.0           # nonlinear coupling (g = 4πaₛ/m in 3D → 1 in ξ-units)
MU      = G_NL * RHO_0  # chemical potential μ = gρ₀
C_S     = 1.0           # sound speed = √(gρ₀/m) = 1 in ξ-units
R_RING  = 8.0           # vortex ring major radius

DX      = L / N
DV      = DX**3         # voxel volume

# Time scales
TAU_HEAL = XI / C_S     # healing time = ξ/c_s = 1 in these units

# Imaginary-time relaxation
IT_STEPS   = 200        # in units of τ_heal
IT_DT      = 0.1        # imaginary-time step

# Real-time parameters
RAMP_TIME   = 50.0 * TAU_HEAL
EVOLVE_TIME = 400.0 * TAU_HEAL   # total real-time after ramp start
SAMPLE_INTERVAL = 10.0 * TAU_HEAL

# CFL: max dt such that max(v) < 0.5 c_s
MAX_DT = 0.05           # initial dt cap
CFL_FACTOR = 0.5        # v_max / c_s limit

# Force values to test
FORCES = [0.01, 0.05, 0.1, 0.2]

# Reconnection / radiation thresholds
RECONNECTION_CIRC_TOL = 0.10   # 10% change in circulation → flag
RADIATION_LOSS_TOL    = 0.05   # 5% acoustic energy loss → flag

OUTDIR = "UHF_HydroInertia_results"

# ══════════════════════════════════════════════════════════════════════
#  GPU Setup
# ══════════════════════════════════════════════════════════════════════
def setup_device():
    """Select GPU."""
    if not torch.cuda.is_available():
        print("ERROR: CUDA not available")
        sys.exit(1)
    dev = torch.device('cuda:0')
    props = torch.cuda.get_device_properties(dev)
    print(f"  Device : {props.name}")
    print(f"  VRAM   : {props.total_memory / 1e9:.1f} GB")
    return dev

# ══════════════════════════════════════════════════════════════════════
#  Grid Construction
# ══════════════════════════════════════════════════════════════════════
def build_grids(device):
    """Build real-space and k-space grids."""
    # Real-space coordinates centered at 0
    x1d = torch.linspace(-L/2, L/2 - DX, N, device=device, dtype=torch.float64)
    X, Y, Z = torch.meshgrid(x1d, x1d, x1d, indexing='ij')

    # k-space grid
    kx1d = torch.fft.fftfreq(N, d=DX, device=device).to(torch.float64) * 2 * np.pi
    KX, KY, KZ = torch.meshgrid(kx1d, kx1d, kx1d, indexing='ij')
    K2 = KX**2 + KY**2 + KZ**2

    return X, Y, Z, K2


# ══════════════════════════════════════════════════════════════════════
#  Vortex Ring Initialization
# ══════════════════════════════════════════════════════════════════════
def init_vortex_ring(X, Y, Z, device):
    """
    Initialize a vortex ring of radius R in the x-y plane (z=0) via
    standard phase-winding ansatz.

    Phase winding: the ring is parameterized as {(R cos φ, R sin φ, 0)}.
    For each grid point, compute the signed angle in the (s, z) plane
    where s = distance from ring axis minus R (cylindrical).
    Amplitude: tanh(d/ξ) where d = distance to ring core.
    """
    # Cylindrical distance from z-axis
    rho_cyl = torch.sqrt(X**2 + Y**2)

    # Distance from ring core in (s, z) plane
    s = rho_cyl - R_RING  # radial deviation from ring
    d = torch.sqrt(s**2 + Z**2)  # distance to ring core

    # Phase winding (unit vorticity): atan2(z, s)
    # This gives ±1 circulation around the ring core
    phase = torch.atan2(Z, s)

    # Amplitude: tanh profile for vortex core
    amp = torch.sqrt(torch.tensor(RHO_0, device=device, dtype=torch.float64)) * torch.tanh(d / XI)

    # Construct wavefunction
    psi = amp * torch.exp(1j * phase.to(torch.complex128))
    psi = psi.to(torch.complex128)

    return psi


def imaginary_time_relax(psi, K2, device):
    """
    Imaginary-time relaxation: evolve ∂ψ/∂τ = [∇²/2 - g|ψ|² + μ] ψ
    using split-step, renormalizing particle number at each step.

    This smooths the vortex core profile while preserving topology.
    """
    print("\n  ┌─ Imaginary-Time Relaxation")
    print(f"  │  Steps: {IT_STEPS} τ_heal / {IT_DT} = {int(IT_STEPS / IT_DT)} steps")
    print(f"  │  dt_imag = {IT_DT:.3f}")

    n_steps = int(IT_STEPS * TAU_HEAL / IT_DT)
    dt = IT_DT

    # Precompute kinetic propagator for imaginary time (real, damping)
    kinetic_full = torch.exp(-0.5 * K2 * dt)  # exp(-K²dt/2) for full kinetic step
    kinetic_half = torch.exp(-0.5 * K2 * dt * 0.5)  # half-step

    # Total particle number
    N_particles = torch.sum(torch.abs(psi)**2).item() * DV

    t0 = time.time()
    report_every = max(n_steps // 10, 1)
    for step in range(n_steps):
        # Nonlinear half-step (imaginary time: real exponential decay)
        rho = torch.abs(psi)**2
        psi = psi * torch.exp(-dt / 2.0 * (G_NL * rho - MU))

        # Kinetic full step in k-space
        psi_k = fft.fftn(psi)
        psi_k *= kinetic_full
        psi = fft.ifftn(psi_k)

        # Nonlinear half-step
        rho = torch.abs(psi)**2
        psi = psi * torch.exp(-dt / 2.0 * (G_NL * rho - MU))

        # Renormalize to preserve particle number
        N_now = torch.sum(torch.abs(psi)**2).item() * DV
        if N_now > 0:
            psi *= np.sqrt(N_particles / N_now)

        if (step + 1) % report_every == 0:
            rho = torch.abs(psi)**2
            rho_min = rho.min().item()
            rho_max = rho.max().item()
            elapsed = time.time() - t0
            print(f"  │  Step {step+1}/{n_steps}  ρ_min={rho_min:.4f}  "
                  f"ρ_max={rho_max:.4f}  [{elapsed:.1f}s]", flush=True)

    elapsed = time.time() - t0
    rho = torch.abs(psi)**2
    print(f"  │  Done. ρ_min={rho.min().item():.6f}, ρ_max={rho.max().item():.6f}")
    print(f"  └─ Relaxation time: {elapsed:.1f}s")

    return psi


# ══════════════════════════════════════════════════════════════════════
#  Diagnostics
# ══════════════════════════════════════════════════════════════════════
def compute_z_com(psi, Z):
    """
    Centre-of-mass of the density void:
    z_com = ∫ z·(ρ₀ - |ψ|²) dV / ∫ (ρ₀ - |ψ|²) dV
    """
    rho = torch.abs(psi)**2
    void = RHO_0 - rho
    # Clip negative values (overshoot from GP dynamics)
    void = torch.clamp(void, min=0.0)

    numerator = torch.sum(Z * void).item() * DV
    denominator = torch.sum(void).item() * DV

    if abs(denominator) < 1e-30:
        return 0.0
    return numerator / denominator


def compute_vortex_void_mass(psi):
    """
    Total displaced mass in void: M_void = ∫ (ρ₀ - |ψ|²) dV
    (for monitoring acoustic radiation loss).
    """
    rho = torch.abs(psi)**2
    void = torch.clamp(RHO_0 - rho, min=0.0)
    return torch.sum(void).item() * DV


def compute_max_velocity(psi, K2, device):
    """
    Estimate max velocity from gradient of phase.
    v = ∇S/m where ψ = √ρ exp(iS).
    In k-space: v_i = Im(∂_i ψ / ψ), but more robust is:
    j = Im(ψ* ∇ψ) / ρ
    """
    rho = torch.abs(psi)**2
    rho_safe = torch.clamp(rho, min=1e-10)

    # Compute gradient via FFT
    psi_k = fft.fftn(psi)
    kx1d = torch.fft.fftfreq(N, d=DX, device=device).to(torch.float64) * 2 * np.pi

    vmax = 0.0
    for axis in range(3):
        shape = [1, 1, 1]
        shape[axis] = N
        ki = kx1d.reshape(shape)
        dpsi_k = 1j * ki * psi_k
        dpsi = fft.ifftn(dpsi_k)
        # velocity component = Im(conj(psi) * dpsi) / rho
        vi = torch.imag(torch.conj(psi) * dpsi) / rho_safe
        vmax = max(vmax, torch.max(torch.abs(vi)).item())

    return vmax


def check_reconnection(void_mass_init, void_mass_now):
    """Check for vortex reconnection via void mass change."""
    if void_mass_init <= 0:
        return False
    change = abs(void_mass_now - void_mass_init) / void_mass_init
    return change > RECONNECTION_CIRC_TOL


def check_radiation_loss(void_mass_init, void_mass_now):
    """Check for acoustic radiation loss > 5%."""
    if void_mass_init <= 0:
        return False
    loss = (void_mass_init - void_mass_now) / void_mass_init
    return loss > RADIATION_LOSS_TOL


# ══════════════════════════════════════════════════════════════════════
#  Split-Step Spectral Time Stepper (Real Time)
# ══════════════════════════════════════════════════════════════════════
def real_time_step(psi, K2, dt, F_current, Z, device):
    """
    One Strang split-step spectral step for the GP equation:
      iℏ ∂ψ/∂t = [-∇²/2 + g|ψ|² - μ + V_ext] ψ

    V_ext(z) = -F·z

    Split: N(dt/2) → K(dt) → N(dt/2)
    where N = nonlinear + potential, K = kinetic.
    """
    half_dt = dt / 2.0

    # ── Nonlinear + potential half-step ──
    rho = torch.abs(psi)**2
    V_nl = G_NL * rho - MU - F_current * Z
    psi = psi * torch.exp(-1j * V_nl * half_dt)

    # ── Kinetic full step in k-space ──
    psi_k = fft.fftn(psi)
    psi_k = psi_k * torch.exp(-1j * 0.5 * K2 * dt)
    psi = fft.ifftn(psi_k)

    # ── Nonlinear + potential half-step ──
    rho = torch.abs(psi)**2
    V_nl = G_NL * rho - MU - F_current * Z
    psi = psi * torch.exp(-1j * V_nl * half_dt)

    return psi


def adaptive_dt(psi, K2, device):
    """
    Compute adaptive time step: max dt such that
    max velocity < CFL_FACTOR × c_s.
    Also ensure the nonlinear phase per step stays bounded.
    """
    vmax = compute_max_velocity(psi, K2, device)
    if vmax > 1e-10:
        dt_cfl = CFL_FACTOR * DX / vmax
    else:
        dt_cfl = MAX_DT

    # Also limit by nonlinear phase: g·ρ_max·dt < π/4
    rho_max = torch.max(torch.abs(psi)**2).item()
    dt_nl = 0.25 * np.pi / max(G_NL * rho_max, 1e-10)

    dt = min(dt_cfl, dt_nl, MAX_DT)
    return max(dt, 1e-6)  # floor


# ══════════════════════════════════════════════════════════════════════
#  Main Evolution for One Force Value
# ══════════════════════════════════════════════════════════════════════
def run_single_force(F_val, psi_relaxed, X, Y, Z, K2, device):
    """
    Run the full real-time evolution for one force value.
    Returns: times, z_com trajectory, void masses, status.
    """
    print(f"\n{'='*62}")
    print(f"  FORCE  F = {F_val}")
    print(f"  Ramp time: {RAMP_TIME:.0f} τ_heal")
    print(f"  Total time: {EVOLVE_TIME:.0f} τ_heal")
    print(f"{'='*62}")

    # Deep copy the relaxed state
    psi = psi_relaxed.clone()

    # Initial diagnostics
    z0 = compute_z_com(psi, Z)
    void_mass_init = compute_vortex_void_mass(psi)
    print(f"  Initial z_com = {z0:.6f}")
    print(f"  Initial void mass = {void_mass_init:.6f}")

    # Storage
    times = [0.0]
    z_coms = [z0]
    void_masses = [void_mass_init]

    t = 0.0
    step = 0
    next_sample = SAMPLE_INTERVAL
    status = "OK"
    t0_wall = time.time()

    while t < EVOLVE_TIME:
        # Current force (linear ramp)
        if t < RAMP_TIME:
            F_current = F_val * (t / RAMP_TIME)
        else:
            F_current = F_val

        # Adaptive time step
        dt = adaptive_dt(psi, K2, device)

        # Don't overshoot sample time or end time
        dt = min(dt, next_sample - t, EVOLVE_TIME - t)
        if dt < 1e-10:
            dt = 1e-6

        # Step
        psi = real_time_step(psi, K2, dt, F_current, Z, device)
        t += dt
        step += 1

        # Sample diagnostics
        if t >= next_sample - 1e-10:
            zc = compute_z_com(psi, Z)
            vm = compute_vortex_void_mass(psi)
            times.append(t)
            z_coms.append(zc)
            void_masses.append(vm)

            # Check for problems
            if check_reconnection(void_mass_init, vm):
                msg = (f"  ⚠ RECONNECTION DETECTED at t={t:.1f}: "
                       f"void mass changed {abs(vm-void_mass_init)/void_mass_init*100:.1f}%")
                print(msg)
                status = f"RECONNECTION at t={t:.1f}"
                break

            if check_radiation_loss(void_mass_init, vm):
                msg = (f"  ⚠ RADIATION LOSS >5% at t={t:.1f}: "
                       f"void mass loss {(void_mass_init-vm)/void_mass_init*100:.1f}%")
                print(msg)
                status = f"RADIATION_LOSS at t={t:.1f}"
                break

            elapsed = time.time() - t0_wall
            print(f"  t={t:7.1f}  z_com={zc:+.6f}  void_mass={vm:.4f}  "
                  f"dt={dt:.4f}  steps={step}  [{elapsed:.0f}s]")

            next_sample += SAMPLE_INTERVAL

    elapsed = time.time() - t0_wall
    print(f"  Total steps: {step}, wall time: {elapsed:.1f}s")
    vram_used = torch.cuda.max_memory_allocated(device) / 1e9
    print(f"  VRAM peak: {vram_used:.2f} GB")

    return np.array(times), np.array(z_coms), np.array(void_masses), status


# ══════════════════════════════════════════════════════════════════════
#  Fit & Analysis
# ══════════════════════════════════════════════════════════════════════
def quadratic_model(t, z0, v0, a):
    return z0 + v0 * t + 0.5 * a * t**2


def fit_trajectory(times, z_coms, ramp_time):
    """
    Fit late-time (post-ramp) trajectory to z = z0 + v0·t + ½·a·t².
    Returns: z0, v0, a, and fit quality.
    """
    # Select post-ramp data
    mask = times >= ramp_time
    t_fit = times[mask]
    z_fit = z_coms[mask]

    if len(t_fit) < 4:
        return None, None, None, None

    # Shift time origin to ramp end for numerical stability
    t_shifted = t_fit - t_fit[0]

    try:
        popt, pcov = curve_fit(quadratic_model, t_shifted, z_fit,
                               p0=[z_fit[0], 0.0, 0.0])
        z0, v0, a = popt
        perr = np.sqrt(np.diag(pcov))
        residuals = z_fit - quadratic_model(t_shifted, *popt)
        r_squared = 1 - np.sum(residuals**2) / np.sum((z_fit - np.mean(z_fit))**2)
        return z0, v0, a, {'a_err': perr[2], 'r_squared': r_squared,
                           't_fit': t_fit, 'z_fit': z_fit,
                           'z_model': quadratic_model(t_shifted, *popt)}
    except RuntimeError:
        return None, None, None, None


# ══════════════════════════════════════════════════════════════════════
#  Plotting
# ══════════════════════════════════════════════════════════════════════
def plot_results(all_results, m_effs, outdir):
    """Generate dashboard: z_com trajectories + m_eff table plot."""
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))

    # Top row: z_com trajectories
    ax = axes[0, 0]
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
    for i, (F_val, res) in enumerate(all_results.items()):
        times, z_coms = res['times'], res['z_coms']
        ax.plot(times, z_coms, 'o-', color=colors[i], markersize=3,
                label=f'F = {F_val}')
        if res.get('fit_info') and res['fit_info'].get('t_fit') is not None:
            ax.plot(res['fit_info']['t_fit'], res['fit_info']['z_model'],
                    '--', color=colors[i], linewidth=2, alpha=0.7)
    ax.axvline(RAMP_TIME, color='gray', linestyle=':', label='Ramp end')
    ax.set_xlabel('Time [τ_heal]')
    ax.set_ylabel('z_com [ξ]')
    ax.set_title('Vortex Ring Centre-of-Mass Trajectory')
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    # Top right: z_com vs t² (should be linear post-ramp)
    ax = axes[0, 1]
    for i, (F_val, res) in enumerate(all_results.items()):
        times, z_coms = res['times'], res['z_coms']
        mask = times >= RAMP_TIME
        if np.sum(mask) > 1:
            t_post = times[mask] - RAMP_TIME
            ax.plot(t_post**2, z_coms[mask], 'o-', color=colors[i],
                    markersize=3, label=f'F = {F_val}')
    ax.set_xlabel('(t - t_ramp)² [τ²_heal]')
    ax.set_ylabel('z_com [ξ]')
    ax.set_title('Quadratic Fit Check: z_com vs (t-t_ramp)²')
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    # Bottom left: m_eff vs F
    ax = axes[1, 0]
    F_vals = sorted(m_effs.keys())
    m_vals = [m_effs[F]['m_eff'] for F in F_vals]
    m_errs = [m_effs[F].get('m_eff_err', 0) for F in F_vals]
    ax.errorbar(F_vals, m_vals, yerr=m_errs, fmt='o-', color='#d62728',
                capsize=5, markersize=8, linewidth=2)
    if len(m_vals) > 0 and all(m is not None for m in m_vals):
        m_mean = np.mean(m_vals)
        ax.axhline(m_mean, color='green', linestyle='--', linewidth=1.5,
                   label=f'Mean m_eff = {m_mean:.4f}')
        ax.fill_between([min(F_vals)*0.5, max(F_vals)*1.5],
                        m_mean * 0.99, m_mean * 1.01,
                        alpha=0.2, color='green', label='±1% band')
    ax.set_xlabel('Force F [healing units]')
    ax.set_ylabel('m_eff = F / a')
    ax.set_title('Effective Mass vs Applied Force')
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    # Bottom right: void mass evolution (radiation check)
    ax = axes[1, 1]
    for i, (F_val, res) in enumerate(all_results.items()):
        times, vm = res['times'], res['void_masses']
        if len(vm) > 0:
            vm_norm = np.array(vm) / vm[0] if vm[0] > 0 else np.array(vm)
            ax.plot(times, vm_norm, 'o-', color=colors[i], markersize=3,
                    label=f'F = {F_val}')
    ax.axhline(0.95, color='red', linestyle=':', label='5% loss threshold')
    ax.set_xlabel('Time [τ_heal]')
    ax.set_ylabel('Void Mass / Initial Void Mass')
    ax.set_title('Void Mass Stability (Radiation Monitor)')
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_ylim(0.8, 1.1)

    fig.suptitle('UHF — Hydrodynamic Inertia Theorem Test\n'
                 f'Vortex Ring R={R_RING}ξ, Grid {N}³, L={L}ξ',
                 fontsize=14, fontweight='bold')
    plt.tight_layout(rect=[0, 0, 1, 0.94])
    path = os.path.join(outdir, 'hydro_inertia_dashboard.png')
    fig.savefig(path, dpi=200)
    plt.close(fig)
    print(f"\n  Dashboard saved: {path}")
    return path


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  UHF — Hydrodynamic Inertia Theorem Test                    ║")
    print("║  3D Split-Step Fourier GP Solver                            ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    device = setup_device()
    os.makedirs(OUTDIR, exist_ok=True)

    # ── Build grids ──
    print(f"\n  Grid: {N}³ = {N**3:,} points")
    print(f"  Δx = {DX:.4f} ξ  (need < 1ξ: {'✓' if DX < 1 else '✗'})")
    print(f"  L = {L} ξ")
    print(f"  VRAM estimate (complex128): {N**3 * 16 * 5 / 1e9:.2f} GB")

    X, Y, Z, K2 = build_grids(device)
    torch.cuda.synchronize(device)
    print(f"  Grids allocated. VRAM: {torch.cuda.memory_allocated(device)/1e9:.2f} GB")

    # ── Initialize vortex ring ──
    print(f"\n  Initializing vortex ring: R = {R_RING}ξ in x-y plane")
    psi = init_vortex_ring(X, Y, Z, device)
    torch.cuda.synchronize(device)
    rho = torch.abs(psi)**2
    print(f"  ρ range: [{rho.min().item():.4f}, {rho.max().item():.4f}]")
    print(f"  VRAM: {torch.cuda.memory_allocated(device)/1e9:.2f} GB")

    # ── Imaginary-time relaxation ──
    psi = imaginary_time_relax(psi, K2, device)
    torch.cuda.synchronize(device)

    # Verify ring survived
    zc = compute_z_com(psi, Z)
    vm = compute_vortex_void_mass(psi)
    print(f"\n  Post-relaxation: z_com = {zc:.6f}, void_mass = {vm:.6f}")
    print(f"  VRAM: {torch.cuda.memory_allocated(device)/1e9:.2f} GB")

    # ── Run for each force value ──
    all_results = {}
    m_effs = {}

    for F_val in FORCES:
        torch.cuda.reset_peak_memory_stats(device)

        times, z_coms, void_masses, status = run_single_force(
            F_val, psi, X, Y, Z, K2, device)

        result = {
            'times': times,
            'z_coms': z_coms,
            'void_masses': void_masses,
            'status': status
        }

        # Fit
        if status == "OK":
            z0_fit, v0_fit, a_fit, fit_info = fit_trajectory(times, z_coms, RAMP_TIME)
            result['z0_fit'] = z0_fit
            result['v0_fit'] = v0_fit
            result['a_fit'] = a_fit
            result['fit_info'] = fit_info

            if a_fit is not None and abs(a_fit) > 1e-20:
                m_eff = F_val / a_fit
                m_eff_err = F_val * fit_info['a_err'] / a_fit**2 if fit_info['a_err'] else 0
                m_effs[F_val] = {
                    'm_eff': m_eff,
                    'm_eff_err': m_eff_err,
                    'a': a_fit,
                    'a_err': fit_info['a_err'],
                    'r_squared': fit_info['r_squared']
                }
                print(f"\n  F={F_val}: a={a_fit:.6e}, m_eff={m_eff:.6f} "
                      f"(R²={fit_info['r_squared']:.6f})")
            else:
                print(f"\n  F={F_val}: fit failed or a≈0")
                m_effs[F_val] = {'m_eff': None, 'a': a_fit, 'r_squared': None}
        else:
            result['fit_info'] = {}
            m_effs[F_val] = {'m_eff': None, 'a': None, 'status': status}
            print(f"\n  F={F_val}: {status}")

        all_results[F_val] = result

    # ── Summary Table ──
    print("\n" + "═"*70)
    print("  RESULTS TABLE")
    print("═"*70)
    print(f"  {'F':>8s}  {'a':>14s}  {'m_eff':>12s}  {'R²':>10s}  {'Status':>12s}")
    print(f"  {'─'*8}  {'─'*14}  {'─'*12}  {'─'*10}  {'─'*12}")

    for F_val in FORCES:
        info = m_effs.get(F_val, {})
        a_str = f"{info.get('a', 0):.6e}" if info.get('a') is not None else "N/A"
        m_str = f"{info.get('m_eff', 0):.6f}" if info.get('m_eff') is not None else "N/A"
        r2_str = f"{info.get('r_squared', 0):.6f}" if info.get('r_squared') is not None else "N/A"
        st = info.get('status', all_results[F_val].get('status', 'OK'))
        print(f"  {F_val:8.3f}  {a_str:>14s}  {m_str:>12s}  {r2_str:>10s}  {st:>12s}")

    # ── Constancy check ──
    valid_m = [info['m_eff'] for info in m_effs.values()
               if info.get('m_eff') is not None]

    if len(valid_m) >= 2:
        m_mean = np.mean(valid_m)
        m_std = np.std(valid_m)
        m_variation = m_std / abs(m_mean) if abs(m_mean) > 1e-30 else float('inf')
        max_dev = max(abs(m - m_mean) / abs(m_mean) for m in valid_m)

        print(f"\n  m_eff mean   = {m_mean:.6f}")
        print(f"  m_eff std    = {m_std:.6f}")
        print(f"  Variation    = {m_variation*100:.4f}%")
        print(f"  Max deviation= {max_dev*100:.4f}%")

        if max_dev < 0.01:
            conclusion = ("✓ HYDRODYNAMIC INERTIA CONFIRMED: m_eff is constant "
                          f"within {max_dev*100:.2f}% (< 1% tolerance)")
        else:
            conclusion = (f"✗ m_eff varies by {max_dev*100:.2f}% "
                          f"(exceeds 1% tolerance)")
        print(f"\n  CONCLUSION: {conclusion}")
    else:
        conclusion = "Insufficient valid data points for constancy test"
        m_mean = valid_m[0] if valid_m else None
        m_std = 0
        m_variation = 0
        max_dev = 0
        print(f"\n  CONCLUSION: {conclusion}")

    # ── Plot ──
    plot_path = plot_results(all_results, m_effs, OUTDIR)

    # ── Save JSON ──
    json_data = {
        'parameters': {
            'N': N, 'L': L, 'xi': XI, 'rho0': RHO_0,
            'R_ring': R_RING, 'dx': DX,
            'ramp_time': RAMP_TIME, 'evolve_time': EVOLVE_TIME,
            'forces': FORCES
        },
        'results': {}
    }
    for F_val in FORCES:
        info = m_effs.get(F_val, {})
        json_data['results'][str(F_val)] = {
            'F': F_val,
            'acceleration': info.get('a'),
            'm_eff': info.get('m_eff'),
            'm_eff_err': info.get('m_eff_err'),
            'r_squared': info.get('r_squared'),
            'status': all_results[F_val]['status'],
            'times': all_results[F_val]['times'].tolist(),
            'z_coms': all_results[F_val]['z_coms'].tolist(),
        }

    json_data['conclusion'] = {
        'm_eff_mean': float(m_mean) if m_mean is not None else None,
        'm_eff_std': float(m_std),
        'variation_pct': float(m_variation * 100) if m_variation else None,
        'max_deviation_pct': float(max_dev * 100) if max_dev else None,
        'within_1pct': bool(max_dev < 0.01) if max_dev is not None else None,
        'text': conclusion
    }

    json_path = os.path.join(OUTDIR, 'hydro_inertia_results.json')
    with open(json_path, 'w') as f:
        json.dump(json_data, f, indent=2)
    print(f"  Results JSON: {json_path}")

    print("\n" + "═"*70)
    print(f"  {conclusion}")
    print("═"*70)


if __name__ == '__main__':
    main()
