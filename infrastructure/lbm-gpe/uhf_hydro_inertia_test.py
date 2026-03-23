#!/usr/bin/env python3
"""
UHF — Hydrodynamic Inertia Theorem: 3D Split-Step Fourier GP Solver
=====================================================================
Tests whether a vortex ring in a Gross-Pitaevskii superfluid responds
to an external linear potential V_ext = -F·z with constant effective
mass  m_eff = F / a  independent of F.

Physics (healing-length units: ℏ = m = ξ = c_s = 1)
----------------------------------------------------
* GP equation:  i ∂ψ/∂t = [-½∇² + |ψ|² - 1 + V_ext] ψ
* Domain: L=40ξ periodic cube, 256³ grid (Δx ≈ 0.156ξ).
* Vortex ring: R=8ξ in x-y plane, phase-winding ansatz + tanh core.
* Phase-locked imaginary-time relaxation: freeze analytic phase,
  relax only amplitude → preserves vortex topology.
* External potential: V_ext = -F·z.  Linear ramp over 50 τ_heal.
* z_com tracking → quadratic fit → m_eff = F/a.

Hardware: RTX 3090, PyTorch 2.5.1+cu121.
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
#  Parameters (healing-length units: ℏ = m = ξ = c_s = 1)
# ══════════════════════════════════════════════════════════════════════
N       = 256
L       = 40.0          # domain side [ξ]
XI      = 1.0
RHO_0   = 1.0
G_NL    = 1.0           # g
MU      = G_NL * RHO_0  # μ = 1
C_S     = 1.0           # sound speed
R_RING  = 8.0           # vortex ring major radius [ξ]
DX      = L / N
DV      = DX**3
TAU_HEAL = 1.0          # ξ/c_s = 1

# Imaginary-time relaxation
IT_TIME    = 30.0       # total imaginary time [τ_heal] (converges by ~20τ)
IT_DT      = 0.05       # imaginary-time step

# Real-time evolution
RAMP_TIME      = 50.0   # linear ramp duration [τ_heal]
EVOLVE_TIME    = 400.0  # total evolution time [τ_heal]
SAMPLE_INTERVAL = 10.0  # diagnostic sampling [τ_heal]
# Fixed dt: Strang split-step is unconditionally unitary.
# Accuracy constraint: K²_max·dt/2 < π → dt < 4π/K²_max ≈ 0.031.
# Using dt=0.02 for good accuracy margin.
DT_FIXED       = 0.02

FORCES = [0.01, 0.05, 0.1, 0.2]

# Stability monitors — set generous for inherent phonon dynamics  
VOID_MASS_CHANGE_TOL = 0.30  # 30% change from settled baseline → flag
RADIATION_LOSS_TOL   = 0.20  # 20% loss → flag (vortex shedding normal up to ~15%)

# Pre-flight settling (F=0) to damp initial transients
SETTLE_TIME    = 100.0  # τ_heal of F=0 evolution before force runs

OUTDIR = "UHF_HydroInertia_results"
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
    x1d = torch.linspace(-L/2, L/2 - DX, N, device=device, dtype=DTYPE)
    X, Y, Z = torch.meshgrid(x1d, x1d, x1d, indexing='ij')
    kx1d = torch.fft.fftfreq(N, d=DX, device=device).to(DTYPE) * 2 * np.pi
    KX, KY, KZ = torch.meshgrid(kx1d, kx1d, kx1d, indexing='ij')
    K2 = KX**2 + KY**2 + KZ**2
    return X, Y, Z, K2


# ══════════════════════════════════════════════════════════════════════
#  Vortex Ring
# ══════════════════════════════════════════════════════════════════════
def vortex_ring_distance(X, Y, Z, R):
    """Distance from each grid point to nearest point on ring core."""
    rho_cyl = torch.sqrt(X**2 + Y**2)
    s = rho_cyl - R
    return torch.sqrt(s**2 + Z**2)


def init_vortex_ring(X, Y, Z, device):
    """
    ψ = √ρ₀ · tanh(d/ξ) · exp(i·φ_ring)
    Phase winding φ_ring = atan2(z, ρ_cyl - R) gives +1 circulation.
    """
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
    """
    Imaginary-time relaxation with PHASE LOCKING.

    After each split-step:
    1. Extract amplitude |ψ|
    2. Restore phase to the analytic vortex winding
    3. Renormalize particle number

    This relaxes the radial core profile to the GP ground state
    around the vortex, without allowing the ring to unwind.
    """
    print("\n  ┌─ Phase-Locked Imaginary-Time Relaxation", flush=True)
    n_steps = int(IT_TIME / IT_DT)
    dt = IT_DT
    print(f"  │  {n_steps} steps, dt={dt}", flush=True)

    # Kinetic propagator for imaginary time: exp(-K²·dt/2) (real, damping)
    kinetic_full = torch.exp(-0.5 * K2 * dt)

    # Analytic phase as complex unit vector
    phase_factor = torch.exp(1j * phase_target.to(CDTYPE))

    # Reference particle count
    N_particles = torch.sum(torch.abs(psi)**2).item() * DV

    t0 = time.time()
    report_every = max(n_steps // 10, 1)

    for step in range(n_steps):
        # Nonlinear half-step
        rho = torch.abs(psi)**2
        psi = psi * torch.exp(-dt / 2.0 * (G_NL * rho - MU))

        # Kinetic full step
        psi_k = fft.fftn(psi)
        psi_k *= kinetic_full
        psi = fft.ifftn(psi_k)

        # Nonlinear half-step
        rho = torch.abs(psi)**2
        psi = psi * torch.exp(-dt / 2.0 * (G_NL * rho - MU))

        # Phase lock
        amp = torch.abs(psi)
        psi = amp.to(CDTYPE) * phase_factor

        # Renormalize
        N_now = torch.sum(amp**2).item() * DV
        if N_now > 0:
            psi *= np.sqrt(N_particles / N_now)

        if (step + 1) % report_every == 0:
            rho = torch.abs(psi)**2
            # Core depletion: density at ring position (R, 0, 0)
            ix = int((R_RING + L/2) / DX)
            iy = iz = N // 2
            ix = min(ix, N-1)
            rho_core = rho[ix, iy, iz].item()
            elapsed = time.time() - t0
            print(f"  │  Step {step+1}/{n_steps}  ρ_min={rho.min().item():.6f}  "
                  f"ρ_max={rho.max().item():.6f}  ρ_core={rho_core:.6f}  "
                  f"[{elapsed:.1f}s]", flush=True)

    rho = torch.abs(psi)**2
    elapsed = time.time() - t0
    ix = min(int((R_RING + L/2) / DX), N-1)
    iy = iz = N // 2
    rho_core = rho[ix, iy, iz].item()

    print(f"  │  ρ_min={rho.min().item():.6f}, ρ_max={rho.max().item():.6f}")
    print(f"  │  ρ at core (R,0,0) = {rho_core:.6f}  "
          f"(depletion {(1-rho_core)*100:.1f}%)")
    print(f"  └─ {elapsed:.1f}s", flush=True)
    return psi


# ══════════════════════════════════════════════════════════════════════
#  Diagnostics
# ══════════════════════════════════════════════════════════════════════
def compute_z_com(psi, Z):
    """z_com of the density void."""
    rho = torch.abs(psi)**2
    void = torch.clamp(RHO_0 - rho, min=0.0)
    denom = torch.sum(void).item() * DV
    if abs(denom) < 1e-30:
        return 0.0
    return torch.sum(Z * void).item() * DV / denom


def compute_core_void_mass(psi, core_mask):
    """Void mass within 3ξ of ring core (mask precomputed)."""
    rho = torch.abs(psi)**2
    void = torch.clamp(RHO_0 - rho, min=0.0) * core_mask
    return torch.sum(void).item() * DV


def compute_total_void_mass(psi):
    rho = torch.abs(psi)**2
    return torch.sum(torch.clamp(RHO_0 - rho, min=0.0)).item() * DV


def compute_max_velocity_bulk(psi, K2, device):
    """Max velocity in bulk (density > 0.5). For monitoring only."""
    rho = torch.abs(psi)**2
    bulk = rho > 0.5
    psi_k = fft.fftn(psi)
    kx1d = torch.fft.fftfreq(N, d=DX, device=device).to(DTYPE) * 2 * np.pi
    vmax = 0.0
    for axis in range(3):
        sh = [1, 1, 1]; sh[axis] = N
        ki = kx1d.reshape(sh)
        dpsi = fft.ifftn(1j * ki * psi_k)
        vi = torch.imag(torch.conj(psi) * dpsi) / torch.clamp(rho, min=1e-10)
        vi_bulk = vi[bulk]
        if vi_bulk.numel() > 0:
            vmax = max(vmax, torch.max(torch.abs(vi_bulk)).item())
    return vmax


# ══════════════════════════════════════════════════════════════════════
#  Split-Step Real-Time Stepper
# ══════════════════════════════════════════════════════════════════════
def real_time_step(psi, kinetic_prop, dt, F_now, Z):
    """Strang split-step: N(dt/2) → K(dt) → N(dt/2)."""
    hdt = dt / 2.0
    V = G_NL * torch.abs(psi)**2 - MU - F_now * Z
    psi = psi * torch.exp(-1j * V * hdt)

    psi_k = fft.fftn(psi)
    psi_k *= kinetic_prop
    psi = fft.ifftn(psi_k)

    V = G_NL * torch.abs(psi)**2 - MU - F_now * Z
    psi = psi * torch.exp(-1j * V * hdt)
    return psi


def compute_adaptive_dt(psi, K2, device):
    """Unused - kept for reference. We use fixed dt."""
    pass


# ══════════════════════════════════════════════════════════════════════
#  Single Force Run
# ══════════════════════════════════════════════════════════════════════
def run_single_force(F_val, psi_init, X, Y, Z, K2, core_mask, device):
    print(f"\n{'='*62}")
    print(f"  FORCE  F = {F_val}")
    print(f"  Ramp: {RAMP_TIME:.0f}τ → Hold: {EVOLVE_TIME-RAMP_TIME:.0f}τ → "
          f"Total: {EVOLVE_TIME:.0f}τ")
    print(f"{'='*62}", flush=True)

    psi = psi_init.clone()
    z0 = compute_z_com(psi, Z)
    vm0 = compute_core_void_mass(psi, core_mask)
    print(f"  Init: z_com={z0:.6f}, core_void={vm0:.4f}", flush=True)

    # Precompute kinetic propagator for fixed dt
    dt = DT_FIXED
    kinetic_prop = torch.exp(-1j * 0.5 * K2 * dt)
    n_total = int(EVOLVE_TIME / dt)
    sample_every = int(SAMPLE_INTERVAL / dt)
    print(f"  dt={dt}, steps={n_total}, sample every {sample_every}", flush=True)

    times, z_coms, cvoids = [0.0], [z0], [vm0]
    status = "OK"
    tw = time.time()

    for step in range(1, n_total + 1):
        t = step * dt
        F_now = F_val * min(t / RAMP_TIME, 1.0)
        psi = real_time_step(psi, kinetic_prop, dt, F_now, Z)

        if step % sample_every == 0:
            zc = compute_z_com(psi, Z)
            vm = compute_core_void_mass(psi, core_mask)
            times.append(t); z_coms.append(zc); cvoids.append(vm)

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
            print(f"  t={t:7.1f}  z_com={zc:+.6f}  cvoid={vm:.4f}  "
                  f"[{wall:.0f}s]", flush=True)

    wall = time.time() - tw
    vram = torch.cuda.max_memory_allocated(device) / 1e9
    print(f"  {n_total} steps, {wall:.1f}s, VRAM {vram:.2f} GB", flush=True)
    return np.array(times), np.array(z_coms), np.array(cvoids), status


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
def plot_results(all_results, m_effs, outdir):
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
    ax.set_xlabel('Time [τ]'); ax.set_ylabel('z_com [ξ]')
    ax.set_title('Vortex Ring Centre-of-Mass'); ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    ax = axes[0, 1]
    for i, (F, r) in enumerate(all_results.items()):
        t, z = r['times'], r['z_coms']
        m = t >= RAMP_TIME
        if np.sum(m) > 1:
            tp = t[m] - RAMP_TIME
            ax.plot(tp**2, z[m], 'o-', color=colors[i], ms=3, label=f'F={F}')
    ax.set_xlabel('(t−t_ramp)² [τ²]'); ax.set_ylabel('z_com [ξ]')
    ax.set_title('z_com vs t² (linearity check)'); ax.legend(fontsize=9)
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
    ax.set_ylim(0.7, 1.2)

    fig.suptitle(f'UHF — Hydrodynamic Inertia Theorem\n'
                 f'Ring R={R_RING}ξ, Grid {N}³, L={L}ξ',
                 fontsize=14, fontweight='bold')
    plt.tight_layout(rect=[0, 0, 1, 0.94])
    path = os.path.join(outdir, 'hydro_inertia_dashboard.png')
    fig.savefig(path, dpi=200); plt.close(fig)
    print(f"\n  Dashboard: {path}", flush=True)
    return path


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  UHF — Hydrodynamic Inertia Theorem Test                    ║")
    print("║  3D Split-Step Fourier GP Solver (Phase-Locked Init)        ║")
    print("╚══════════════════════════════════════════════════════════════╝",
          flush=True)

    device = setup_device()
    os.makedirs(OUTDIR, exist_ok=True)

    print(f"\n  Grid: {N}³ = {N**3:,},  Δx={DX:.4f}ξ,  L={L}ξ", flush=True)
    X, Y, Z, K2 = build_grids(device)
    print(f"  VRAM: {torch.cuda.memory_allocated(device)/1e9:.2f} GB", flush=True)

    print(f"\n  Vortex ring: R={R_RING}ξ, x-y plane", flush=True)
    psi, phase_target = init_vortex_ring(X, Y, Z, device)
    rho = torch.abs(psi)**2
    d = vortex_ring_distance(X, Y, Z, R_RING)
    ix = min(int((R_RING + L/2) / DX), N-1)
    iy = iz = N // 2
    print(f"  ρ range: [{rho.min().item():.6f}, {rho.max().item():.6f}]")
    print(f"  ρ at core: {rho[ix,iy,iz].item():.6f}", flush=True)

    # Precompute core mask for diagnostics (within 3ξ of ring)
    core_mask = (d < 3.0 * XI).to(DTYPE)
    del d  # free memory

    psi = imaginary_time_relax(psi, phase_target, K2, device)
    torch.cuda.synchronize(device)

    zc0 = compute_z_com(psi, Z)
    vm0 = compute_core_void_mass(psi, core_mask)
    vmt = compute_total_void_mass(psi)
    print(f"\n  Post-relax: z_com={zc0:.6f}, core_void={vm0:.4f}, "
          f"total_void={vmt:.4f}", flush=True)

    # ── Pre-flight settling: real-time evolution with F=0  ──
    print(f"\n  ┌─ Pre-flight settling: {SETTLE_TIME}τ with F=0", flush=True)
    kinetic_prop_settle = torch.exp(-1j * 0.5 * K2 * DT_FIXED)
    n_settle = int(SETTLE_TIME / DT_FIXED)
    sample_s = int(SAMPLE_INTERVAL / DT_FIXED)
    t0w = time.time()
    for step in range(1, n_settle + 1):
        psi = real_time_step(psi, kinetic_prop_settle, DT_FIXED, 0.0, Z)
        if step % sample_s == 0:
            t_s = step * DT_FIXED
            zc = compute_z_com(psi, Z)
            vm = compute_core_void_mass(psi, core_mask)
            wall = time.time() - t0w
            print(f"  │  t={t_s:6.1f}  z_com={zc:+.6f}  cvoid={vm:.4f}  "
                  f"[{wall:.0f}s]", flush=True)
    del kinetic_prop_settle
    zc_settled = compute_z_com(psi, Z)
    vm_settled = compute_core_void_mass(psi, core_mask)
    vmt_settled = compute_total_void_mass(psi)
    wall = time.time() - t0w
    print(f"  └─ Settled: z_com={zc_settled:.6f}, core_void={vm_settled:.4f}, "
          f"total_void={vmt_settled:.4f}  [{wall:.0f}s]", flush=True)
    print(f"  VRAM: {torch.cuda.memory_allocated(device)/1e9:.2f} GB", flush=True)

    all_results = {}
    m_effs = {}

    for F_val in FORCES:
        torch.cuda.reset_peak_memory_stats(device)
        times, z_coms, cvoids, status = run_single_force(
            F_val, psi, X, Y, Z, K2, core_mask, device)

        res = {'times': times, 'z_coms': z_coms,
               'core_voids': cvoids, 'status': status, 'fit_info': {}}

        if status == "OK":
            _, _, af, fi = fit_trajectory(times, z_coms, RAMP_TIME)
            res['a_fit'] = af; res['fit_info'] = fi
            if af is not None and abs(af) > 1e-20:
                meff = F_val / af
                merr = F_val * fi.get('a_err', 0) / af**2
                m_effs[F_val] = {'m_eff': meff, 'm_eff_err': merr,
                                 'a': af, 'a_err': fi.get('a_err'),
                                 'r_squared': fi.get('r_squared')}
                print(f"\n  F={F_val}: a={af:.6e}, m_eff={meff:.6f}, "
                      f"R²={fi['r_squared']:.6f}", flush=True)
            else:
                m_effs[F_val] = {'m_eff': None, 'a': af}
                print(f"\n  F={F_val}: fit failed", flush=True)
        else:
            m_effs[F_val] = {'m_eff': None, 'a': None, 'status': status}
            print(f"\n  F={F_val}: {status}", flush=True)

        all_results[F_val] = res

    # ── Table ──
    print("\n" + "═"*70)
    print("  RESULTS TABLE")
    print("═"*70)
    hdr = f"  {'F':>8s}  {'a':>14s}  {'m_eff':>12s}  {'R²':>10s}  Status"
    print(hdr)
    print(f"  {'─'*8}  {'─'*14}  {'─'*12}  {'─'*10}  {'─'*20}")
    for F in FORCES:
        info = m_effs.get(F, {})
        a_s = f"{info['a']:.6e}" if info.get('a') is not None else "—"
        m_s = f"{info['m_eff']:.6f}" if info.get('m_eff') is not None else "—"
        r_s = f"{info['r_squared']:.6f}" if info.get('r_squared') is not None else "—"
        st = info.get('status', all_results[F]['status'])
        print(f"  {F:8.3f}  {a_s:>14s}  {m_s:>12s}  {r_s:>10s}  {st}")

    # ── Constancy ──
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

    # ── Plot ──
    plot_results(all_results, m_effs, OUTDIR)

    # ── JSON ──
    jd = {
        'parameters': {'N': N, 'L': L, 'xi': XI, 'rho0': RHO_0,
                        'R_ring': R_RING, 'dx': DX,
                        'ramp_time': RAMP_TIME, 'evolve_time': EVOLVE_TIME,
                        'forces': FORCES},
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

    jp = os.path.join(OUTDIR, 'hydro_inertia_results.json')
    with open(jp, 'w') as f:
        json.dump(jd, f, indent=2)
    print(f"  JSON: {jp}", flush=True)

    print("\n" + "═"*70)
    print(f"  {conc}")
    print("═"*70, flush=True)


if __name__ == '__main__':
    main()
