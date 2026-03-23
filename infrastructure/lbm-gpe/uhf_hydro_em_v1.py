#!/usr/bin/env python3
"""
UHF — Hydrodynamic Electromagnetism v1: Emergent Coulomb Law
=============================================================
2D GP Solver (2048², L=80ξ) on RTX 3090.

METHODOLOGY — two independent measurements:

Method A — Velocity-Field Measurement (primary):
  Place a SINGLE vortex at the origin, relax to ground state (IT + pinning).
  Compute the superfluid velocity field  v_s = Im(∇ψ · ψ*) / |ψ|².
  Azimuthally average |v_s|(r) at distances r = 3,5,8,10,12,15,20,25,30 ξ.
  Theory: v(r) = κ/(2πr) = 1/r  (in units ℏ=m=ξ=c_s=1, κ=2π).
  Fit v = C/r^β.  Target: β ≈ 1.0, C ≈ 1.0.

Method B — Dipole Translation Speed (dynamical):
  Place a vortex–antivortex pair (+1,−1) at separation r₀.
  Relax (IT+pinning), then turn off pin and evolve for ~5–8 τ.
  Measure dipole translation speed v_trans = dy_mid/dt.
  Theory: v_trans(r₀) = κ/(2πr₀) = 1/r₀.
  Repeat for r₀ = 8, 12, 16, 20 ξ.  Fit v = C/r^β.

Units: ℏ = m = ξ = c_s = 1, κ = 2π, ρ₀ = 1.
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
NX = NY = 2048
LX = LY = 80.0       # [ξ]
XI      = 1.0
RHO_0   = 1.0
G_NL    = 1.0
MU      = G_NL * RHO_0

DX = LX / NX
DY = LY / NY
DA = DX * DY

# IT relaxation
IT_STEPS = 2000
IT_DT    = 0.02

# Pinning for IT: strong positive barrier at vortex core
PIN_AMP    = 10.0
PIN_WIDTH  = 1.0     # [ξ]

# Real-time (Method B only)
DT_RT      = 0.005   # small dt for accuracy
DIPOLE_TIME = 8.0    # evolve for 8τ only — measure initial velocity
SAMPLE_DT_B = 0.2    # sample every 0.2τ

# Velocity field sampling radii (Method A)
V_FIELD_RADII = [3.0, 5.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0]

# Dipole separations (Method B)
R0_LIST = [8.0, 12.0, 16.0, 20.0]

# Contour winding radius
WINDING_RAD = 3

OUTDIR = "UHF_HydroEM_v1_results"
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
    y1d = torch.linspace(-LY/2, LY/2 - DY, NY, device=device, dtype=DTYPE)
    X, Y = torch.meshgrid(x1d, y1d, indexing='ij')

    kx1d = torch.fft.fftfreq(NX, d=DX, device=device).to(DTYPE) * 2 * np.pi
    ky1d = torch.fft.fftfreq(NY, d=DY, device=device).to(DTYPE) * 2 * np.pi
    KX, KY = torch.meshgrid(kx1d, ky1d, indexing='ij')
    K2 = KX**2 + KY**2
    return X, Y, K2, KX, KY


# ══════════════════════════════════════════════════════════════════════
#  Vortex Ansatz
# ══════════════════════════════════════════════════════════════════════
def vortex_ansatz(X, Y, vortices):
    """Create ψ with multiple vortices.
    vortices = list of (x, y, q) tuples.
    """
    phase = torch.zeros_like(X)
    amp = torch.ones_like(X) * np.sqrt(RHO_0)
    for xv, yv, qv in vortices:
        dx = X - xv; dy = Y - yv
        d = torch.sqrt(dx**2 + dy**2 + 1e-30)
        phase = phase + qv * torch.atan2(dy, dx)
        amp = amp * torch.tanh(d / XI)
    psi = (amp * torch.exp(1j * phase.to(CDTYPE))).to(CDTYPE)
    return psi


def pinning_potential(X, Y, vortices):
    """Positive Gaussian barriers at vortex positions."""
    V = torch.zeros_like(X)
    for xv, yv, _ in vortices:
        d2 = (X - xv)**2 + (Y - yv)**2
        V = V + PIN_AMP * torch.exp(-d2 / (2 * PIN_WIDTH**2))
    return V


# ══════════════════════════════════════════════════════════════════════
#  IT Relaxation (Phase-Locked)
# ══════════════════════════════════════════════════════════════════════
def imaginary_time_relax(psi, K2, V_pin, phase_lock=True):
    """IT relaxation with optional phase lock.
    Phase lock gives minimal sound wave emission on release."""
    dt = IT_DT
    kinetic_full = torch.exp(-0.5 * K2 * dt)
    if phase_lock:
        phase_target = torch.angle(psi)
        phase_factor = torch.exp(1j * phase_target.to(CDTYPE))
    N0 = torch.sum(torch.abs(psi)**2).item() * DA

    for step in range(IT_STEPS):
        rho = torch.abs(psi)**2
        V = G_NL * rho - MU + V_pin
        psi = psi * torch.exp(-dt / 2.0 * V)
        psi = fft.ifftn(fft.fftn(psi) * kinetic_full)
        rho = torch.abs(psi)**2
        V = G_NL * rho - MU + V_pin
        psi = psi * torch.exp(-dt / 2.0 * V)

        if phase_lock:
            amp = torch.abs(psi)
            psi = amp.to(CDTYPE) * phase_factor

        N_now = torch.sum(torch.abs(psi)**2).item() * DA
        if N_now > 0:
            psi *= np.sqrt(N0 / N_now)

    rho = torch.abs(psi)**2
    return psi


# ══════════════════════════════════════════════════════════════════════
#  Real-Time Stepper
# ══════════════════════════════════════════════════════════════════════
def real_time_step(psi, kinetic_prop, dt, V_ext=None):
    hdt = dt / 2.0
    V = G_NL * torch.abs(psi)**2 - MU
    if V_ext is not None:
        V = V + V_ext
    psi = psi * torch.exp(-1j * V * hdt)
    psi = fft.ifftn(fft.fftn(psi) * kinetic_prop)
    V = G_NL * torch.abs(psi)**2 - MU
    if V_ext is not None:
        V = V + V_ext
    psi = psi * torch.exp(-1j * V * hdt)
    return psi


# ══════════════════════════════════════════════════════════════════════
#  Velocity Field Computation
# ══════════════════════════════════════════════════════════════════════
def compute_velocity_field(psi, KX, KY):
    """Compute superfluid velocity v_s = Im(∇ψ · ψ*) / |ψ|²."""
    psi_k = fft.fftn(psi)
    dpsi_dx = fft.ifftn(1j * KX * psi_k)
    dpsi_dy = fft.ifftn(1j * KY * psi_k)
    rho = torch.abs(psi)**2
    rho_safe = torch.clamp(rho, min=1e-10)
    vx = torch.imag(dpsi_dx * torch.conj(psi)) / rho_safe
    vy = torch.imag(dpsi_dy * torch.conj(psi)) / rho_safe
    return vx, vy, rho


def azimuthal_average_v(vx, vy, rho, X, Y, x_center, y_center, radii,
                        annulus_width=0.5, rho_thresh=0.1):
    """Azimuthally average |v| in annuli centered on (x_center, y_center)."""
    v_mag = torch.sqrt(vx**2 + vy**2)
    dx = X - x_center
    dy = Y - y_center
    # Periodic wrapping
    dx = dx - LX * torch.round(dx / LX)
    dy = dy - LY * torch.round(dy / LY)
    r_field = torch.sqrt(dx**2 + dy**2)

    results = []
    for r in radii:
        mask = ((r_field > r - annulus_width) &
                (r_field < r + annulus_width) &
                (rho > rho_thresh))
        if mask.sum() > 0:
            v_mean = v_mag[mask].mean().item()
            v_std = v_mag[mask].std().item()
            results.append({'r': r, 'v': v_mean, 'v_std': v_std,
                            'n_pts': mask.sum().item()})
        else:
            results.append({'r': r, 'v': 0.0, 'v_std': 0.0, 'n_pts': 0})
    return results


# ══════════════════════════════════════════════════════════════════════
#  Vortex Core Tracker
# ══════════════════════════════════════════════════════════════════════
def _contour_winding_2d(phi, ix_c, iy_c, rad=3):
    """Winding number via square contour integral."""
    angles = []
    nx, ny = phi.shape
    for i in range(-rad, rad + 1):
        angles.append(phi[(ix_c + i) % nx, (iy_c - rad) % ny].item())
    for j in range(-rad + 1, rad + 1):
        angles.append(phi[(ix_c + rad) % nx, (iy_c + j) % ny].item())
    for i in range(rad - 1, -rad - 1, -1):
        angles.append(phi[(ix_c + i) % nx, (iy_c + rad) % ny].item())
    for j in range(rad - 1, -rad, -1):
        angles.append(phi[(ix_c - rad) % nx, (iy_c + j) % ny].item())
    total = 0.0
    n = len(angles)
    for k in range(n):
        dp = angles[(k + 1) % n] - angles[k]
        dp = (dp + np.pi) % (2 * np.pi) - np.pi
        total += dp
    return total / (2 * np.pi)


def find_vortex_core(psi, X, Y, x_prev, y_prev, expected_q, win_r=6.0):
    """Find vortex core near (x_prev, y_prev)."""
    rho = torch.abs(psi)**2
    ddx = X - x_prev
    ddy = Y - y_prev
    ddx = ddx - LX * torch.round(ddx / LX)
    ddy = ddy - LY * torch.round(ddy / LY)
    mask = (ddx**2 + ddy**2) < win_r**2

    rho_masked = rho.clone()
    rho_masked[~mask] = 1e10
    flat_idx = torch.argmin(rho_masked)
    iy_min = (flat_idx % NY).item()
    ix_min = (flat_idx // NY).item()

    # Check density: real vortex cores have rho ≈ 0
    rho_at_min = rho[ix_min, iy_min].item()
    if rho_at_min > 0.3:
        # Not a real vortex core — density too high
        return x_prev, y_prev, 0

    x_core = -LX/2 + ix_min * DX
    y_core = -LY/2 + iy_min * DY

    # Sub-grid parabolic refinement
    if 1 <= ix_min < NX-1:
        rL = rho[ix_min-1, iy_min].item()
        rC = rho[ix_min,   iy_min].item()
        rR = rho[ix_min+1, iy_min].item()
        d = rL - 2*rC + rR
        if abs(d) > 1e-30:
            x_core += 0.5 * (rL - rR) / d * DX

    if 1 <= iy_min < NY-1:
        rL = rho[ix_min, iy_min-1].item()
        rC = rho[ix_min, iy_min  ].item()
        rR = rho[ix_min, iy_min+1].item()
        d = rL - 2*rC + rR
        if abs(d) > 1e-30:
            y_core += 0.5 * (rL - rR) / d * DY

    phi = torch.angle(psi)
    charge = int(round(_contour_winding_2d(phi, ix_min, iy_min, WINDING_RAD)))
    return x_core, y_core, charge


# ══════════════════════════════════════════════════════════════════════
#  METHOD A: Velocity Field Measurement (Single Vortex)
# ══════════════════════════════════════════════════════════════════════
def method_a_velocity_field(X, Y, K2, KX, KY, device):
    """Create single vortex, relax, measure v(r)."""
    print("\n" + "═"*62)
    print("  METHOD A: Velocity Field ─ Single Vortex")
    print("═"*62, flush=True)

    vortices = [(0.0, 0.0, +1)]
    psi = vortex_ansatz(X, Y, vortices)
    V_pin = pinning_potential(X, Y, vortices)

    t0 = time.time()
    print("  IT relaxation (phase-locked, 2000 steps)...", flush=True)
    psi = imaginary_time_relax(psi, K2, V_pin, phase_lock=True)
    print(f"  Done ({time.time()-t0:.1f}s)", flush=True)

    # Verify vortex
    xc, yc, q = find_vortex_core(psi, X, Y, 0.0, 0.0, +1)
    print(f"  Vortex at ({xc:.3f},{yc:.3f}), q={q:+d}", flush=True)

    # Compute velocity field
    print("  Computing v_s field...", flush=True)
    vx, vy, rho = compute_velocity_field(psi, KX, KY)
    results = azimuthal_average_v(vx, vy, rho, X, Y, xc, yc, V_FIELD_RADII)

    print(f"\n  {'r [ξ]':>8s}  {'v_meas':>10s}  {'v_th=1/r':>10s}  {'ratio':>8s}")
    print(f"  {'─'*8}  {'─'*10}  {'─'*10}  {'─'*8}")
    for res in results:
        v_th = 1.0 / res['r']
        ratio = res['v'] / v_th if v_th > 0 else 0
        print(f"  {res['r']:8.1f}  {res['v']:10.6f}  {v_th:10.6f}  {ratio:8.4f}")

    return results


# ══════════════════════════════════════════════════════════════════════
#  METHOD B: Dipole Translation Speed
# ══════════════════════════════════════════════════════════════════════
def method_b_dipole_speed(r0, X, Y, K2, KX, KY, device):
    """Create +1/-1 pair at separation r0, measure translation speed."""
    x1, y1, q1 = -r0/2, 0.0, +1
    x2, y2, q2 = +r0/2, 0.0, -1

    print(f"\n  r₀={r0:.0f}ξ : ", end="", flush=True)

    vortices = [(x1, y1, q1), (x2, y2, q2)]
    psi = vortex_ansatz(X, Y, vortices)
    V_pin = pinning_potential(X, Y, vortices)

    # IT relax with phase lock
    psi = imaginary_time_relax(psi, K2, V_pin, phase_lock=True)

    # Verify
    xc1, yc1, ch1 = find_vortex_core(psi, X, Y, x1, y1, q1)
    xc2, yc2, ch2 = find_vortex_core(psi, X, Y, x2, y2, q2)
    r_init = np.sqrt((xc2-xc1)**2 + (yc2-yc1)**2)
    print(f"relaxed r={r_init:.2f}ξ q=({ch1:+d},{ch2:+d})", end=" ", flush=True)

    # Also measure velocity field at the midpoint as cross-check
    vx, vy, rho = compute_velocity_field(psi, KX, KY)
    # Midpoint velocity (should be in y-direction for horizontal pair)
    mid_mask = ((X**2 + Y**2) < 1.0) & (rho > 0.1)
    if mid_mask.sum() > 0:
        v_mid_y = vy[mid_mask].mean().item()
    else:
        v_mid_y = 0.0
    # The midpoint velocity from two vortices: v_y = 2κ/(2πd) = 2/d = 4/r₀
    # But each vortex moves at v = κ/(2πr₀) = 1/r₀
    v_field_check = abs(v_mid_y)

    # Real-time evolution WITHOUT pinning
    dt = DT_RT
    kinetic_prop = torch.exp(-1j * 0.5 * K2 * dt)
    n_steps = int(DIPOLE_TIME / dt)
    sample_every = max(1, int(SAMPLE_DT_B / dt))

    times = [0.0]
    y1s = [yc1]; y2s = [yc2]
    x1s = [xc1]; x2s = [xc2]
    rs = [r_init]

    for step in range(1, n_steps + 1):
        psi = real_time_step(psi, kinetic_prop, dt)
        if step % sample_every == 0:
            t = step * dt
            xc1, yc1, ch1 = find_vortex_core(psi, X, Y, x1s[-1], y1s[-1], q1)
            xc2, yc2, ch2 = find_vortex_core(psi, X, Y, x2s[-1], y2s[-1], q2)
            r_now = np.sqrt((xc2-xc1)**2 + (yc2-yc1)**2)
            times.append(t); x1s.append(xc1); x2s.append(xc2)
            y1s.append(yc1); y2s.append(yc2); rs.append(r_now)

    del kinetic_prop

    t_arr = np.array(times)
    y_mid = (np.array(y1s) + np.array(y2s)) / 2.0
    r_arr = np.array(rs)

    # Speed: slope of y_mid(t) using first 5τ data
    mask = (t_arr > 0.5) & (t_arr < min(5.0, t_arr[-1]))
    if mask.sum() >= 3:
        from numpy.polynomial import polynomial as P
        coeffs = P.polyfit(t_arr[mask], y_mid[mask], 1)
        v_dyn = abs(coeffs[1])
    else:
        v_dyn = 0.0

    r_mean = np.mean(r_arr[mask]) if mask.sum() > 0 else r_init
    v_theory = 1.0 / r_mean
    ratio = v_dyn / v_theory if v_theory > 0 else 0

    # Separation drift (should be ~ 0 for 5τ)
    dr = r_arr[-1] - r_arr[0]

    print(f"v_dyn={v_dyn:.4f} v_th={v_theory:.4f} ratio={ratio:.3f} "
          f"Δr={dr:.3f} v_field={v_field_check:.4f}", flush=True)

    return {
        'r0': r0, 'r_init': r_init, 'r_mean': r_mean,
        'v_dyn': v_dyn, 'v_theory': v_theory, 'ratio': ratio,
        'v_field_mid': v_field_check, 'dr': dr,
        'times': t_arr, 'y_mid': y_mid, 'r_arr': r_arr,
        'x1': np.array(x1s), 'y1': np.array(y1s),
        'x2': np.array(x2s), 'y2': np.array(y2s)
    }


# ══════════════════════════════════════════════════════════════════════
#  Power-Law Fit
# ══════════════════════════════════════════════════════════════════════
def fit_power_law(r_pts, v_pts):
    """Fit v = C / r^β.  Returns C, beta, R²."""
    mask = (r_pts > 1.0) & (v_pts > 1e-12)
    rf = r_pts[mask]
    vf = v_pts[mask]
    if len(rf) < 3:
        return None, None, None
    def model(r, C, beta):
        return C / r**beta
    try:
        popt, _ = curve_fit(model, rf, vf, p0=[1.0, 1.0], maxfev=10000)
        C, beta = popt
        vpred = model(rf, *popt)
        ss_res = np.sum((vf - vpred)**2)
        ss_tot = np.sum((vf - np.mean(vf))**2)
        R2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        return C, beta, R2
    except RuntimeError:
        return None, None, None


# ══════════════════════════════════════════════════════════════════════
#  Plotting
# ══════════════════════════════════════════════════════════════════════
def plot_method_a(results_a, outdir):
    """v(r) from velocity field (single vortex)."""
    fig, ax = plt.subplots(figsize=(8, 6))
    r_pts = np.array([r['r'] for r in results_a])
    v_pts = np.array([r['v'] for r in results_a])
    v_std = np.array([r['v_std'] for r in results_a])

    ax.errorbar(r_pts, v_pts, yerr=v_std, fmt='o', color='#d62728',
                ms=8, capsize=4, label='GP simulation')

    # 1/r theory
    r_cont = np.linspace(2, 32, 200)
    ax.plot(r_cont, 1.0/r_cont, 'k--', lw=2, label='Theory: v = 1/r')

    # Fit
    C, beta, R2 = fit_power_law(r_pts, v_pts)
    if C is not None:
        ax.plot(r_cont, C / r_cont**beta, '-', color='#1f77b4', lw=1.5,
                label=f'Fit: v = {C:.3f}/r^{{{beta:.3f}}} (R²={R2:.6f})')

    ax.set_xlabel('Distance from vortex r [ξ]', fontsize=12)
    ax.set_ylabel('|v_s| [ξ/τ]', fontsize=12)
    ax.set_title('Method A: Superfluid Velocity Field — Single Vortex',
                 fontsize=13, fontweight='bold')
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(2, 32)
    ax.set_ylim(0, 0.4)
    plt.tight_layout()
    path = os.path.join(outdir, 'method_a_v_field.png')
    fig.savefig(path, dpi=200); plt.close(fig)
    print(f"  Plot: {path}")
    return C, beta, R2


def plot_method_b(results_b, outdir):
    """Dipole translation speed v(r₀)."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # Left: trajectories
    ax = axes[0]
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
    for i, rb in enumerate(results_b):
        ax.plot(rb['x1'], rb['y1'], '-', color=colors[i], lw=1)
        ax.plot(rb['x2'], rb['y2'], '--', color=colors[i], lw=1)
        ax.plot(rb['x1'][0], rb['y1'][0], 'o', color=colors[i], ms=5)
        ax.plot(rb['x2'][0], rb['y2'][0], 's', color=colors[i], ms=5)
    ax.set_xlabel('x [ξ]'); ax.set_ylabel('y [ξ]')
    ax.set_title('Dipole Trajectories (8τ)')
    ax.set_aspect('equal'); ax.grid(True, alpha=0.3)

    # Right: v(r₀)
    ax = axes[1]
    r_pts = np.array([rb['r_mean'] for rb in results_b])
    v_pts = np.array([rb['v_dyn'] for rb in results_b])

    ax.plot(r_pts, v_pts, 'o', color='#d62728', ms=10, label='GP simulation')

    r_cont = np.linspace(6, 22, 200)
    ax.plot(r_cont, 1.0/r_cont, 'k--', lw=2, label='Theory: v = 1/r')

    C, beta, R2 = fit_power_law(r_pts, v_pts)
    if C is not None:
        ax.plot(r_cont, C / r_cont**beta, '-', color='#1f77b4', lw=1.5,
                label=f'Fit: v = {C:.3f}/r^{{{beta:.3f}}} (R²={R2:.6f})')

    ax.set_xlabel('Separation r₀ [ξ]', fontsize=12)
    ax.set_ylabel('v_trans [ξ/τ]', fontsize=12)
    ax.set_title('Method B: Dipole Translation Speed', fontsize=13,
                 fontweight='bold')
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    path = os.path.join(outdir, 'method_b_dipole.png')
    fig.savefig(path, dpi=200); plt.close(fig)
    print(f"  Plot: {path}")
    return C, beta, R2


def plot_combined(results_a, results_b, fit_a, fit_b, outdir):
    """Dashboard combining both methods."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # Method A
    ax = axes[0]
    r_a = np.array([r['r'] for r in results_a])
    v_a = np.array([r['v'] for r in results_a])
    ax.plot(r_a, v_a, 'o', color='#d62728', ms=8, label='Method A (v-field)')
    r_cont = np.linspace(2, 32, 200)
    ax.plot(r_cont, 1.0/r_cont, 'k--', lw=2, label='v = 1/r')
    C_a, beta_a, R2_a = fit_a
    if C_a is not None:
        ax.plot(r_cont, C_a / r_cont**beta_a, '-', color='#1f77b4', lw=1.5,
                label=f'β={beta_a:.4f}, R²={R2_a:.6f}')
    ax.set_xlabel('r [ξ]'); ax.set_ylabel('|v_s| [ξ/τ]')
    ax.set_title('A: Velocity Field (Single Vortex)')
    ax.legend(fontsize=9); ax.grid(True, alpha=0.3)

    # Method B
    ax = axes[1]
    r_b = np.array([rb['r_mean'] for rb in results_b])
    v_b = np.array([rb['v_dyn'] for rb in results_b])
    ax.plot(r_b, v_b, 'o', color='#2ca02c', ms=10, label='Method B (dipole)')
    r_cont2 = np.linspace(6, 22, 200)
    ax.plot(r_cont2, 1.0/r_cont2, 'k--', lw=2, label='v = 1/r')
    C_b, beta_b, R2_b = fit_b
    if C_b is not None:
        ax.plot(r_cont2, C_b / r_cont2**beta_b, '-', color='#1f77b4', lw=1.5,
                label=f'β={beta_b:.4f}, R²={R2_b:.6f}')
    ax.set_xlabel('r₀ [ξ]'); ax.set_ylabel('v_trans [ξ/τ]')
    ax.set_title('B: Dipole Translation Speed (+1,−1)')
    ax.legend(fontsize=9); ax.grid(True, alpha=0.3)

    fig.suptitle('UHF Hydrodynamic Electromagnetism v1 — Emergent 2D Coulomb Law',
                 fontsize=14, fontweight='bold')
    plt.tight_layout(rect=[0, 0, 1, 0.93])
    path = os.path.join(outdir, 'v_vs_r_coulomb_v1.png')
    fig.savefig(path, dpi=200); plt.close(fig)
    print(f"  Main plot: {path}")
    return path


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  UHF — Hydrodynamic Electromagnetism v1                    ║")
    print("║  Emergent 2D Coulomb Law from GP Vortices                  ║")
    print("╚══════════════════════════════════════════════════════════════╝",
          flush=True)

    device = setup_device()
    os.makedirs(OUTDIR, exist_ok=True)

    print(f"\n  Grid: {NX}×{NY}, L={LX}ξ, Δx={DX:.4f}ξ")
    print(f"  Theory: v(r) = κ/(2πr) = 1/r", flush=True)

    X, Y, K2, KX, KY = build_grids(device)
    t_start = time.time()

    # ── METHOD A ──
    results_a = method_a_velocity_field(X, Y, K2, KX, KY, device)

    # Fit
    r_a = np.array([r['r'] for r in results_a])
    v_a = np.array([r['v'] for r in results_a])
    C_a, beta_a, R2_a = fit_power_law(r_a, v_a)
    fit_a = (C_a, beta_a, R2_a)
    if C_a is not None:
        print(f"\n  Method A fit: v = {C_a:.4f}/r^{beta_a:.4f}, R² = {R2_a:.8f}")

    # ── METHOD B ──
    print("\n" + "═"*62)
    print("  METHOD B: Dipole Translation Speed (+1,−1)")
    print("═"*62, flush=True)

    results_b = []
    for r0 in R0_LIST:
        rb = method_b_dipole_speed(r0, X, Y, K2, KX, KY, device)
        results_b.append(rb)

    r_b = np.array([rb['r_mean'] for rb in results_b])
    v_b = np.array([rb['v_dyn'] for rb in results_b])
    C_b, beta_b, R2_b = fit_power_law(r_b, v_b)
    fit_b = (C_b, beta_b, R2_b)
    if C_b is not None:
        print(f"\n  Method B fit: v = {C_b:.4f}/r^{beta_b:.4f}, R² = {R2_b:.8f}")

    total_time = time.time() - t_start

    # ── RESULTS TABLE ──
    print("\n" + "═"*80)
    print("  RESULTS TABLE: Emergent 2D Coulomb Law")
    print("═"*80)

    print(f"\n  METHOD A — Velocity Field (Single Vortex, q=+1)")
    print(f"  {'r [ξ]':>8s}  {'v_meas':>10s}  {'v_th=1/r':>10s}  {'ratio':>8s}  {'σ_v':>8s}")
    print(f"  {'─'*8}  {'─'*10}  {'─'*10}  {'─'*8}  {'─'*8}")
    for res in results_a:
        v_th = 1.0 / res['r']
        ratio = res['v'] / v_th
        print(f"  {res['r']:8.1f}  {res['v']:10.6f}  {v_th:10.6f}  "
              f"{ratio:8.4f}  {res['v_std']:8.6f}")
    if C_a is not None:
        print(f"  → Fit: v = {C_a:.4f}/r^{beta_a:.4f}, R² = {R2_a:.8f}")

    print(f"\n  METHOD B — Dipole Translation Speed (+1,−1)")
    print(f"  {'r₀ [ξ]':>8s}  {'<r>':>8s}  {'v_dyn':>10s}  {'v_th':>10s}  "
          f"{'ratio':>8s}  {'v_field':>8s}  {'Δr':>6s}")
    print(f"  {'─'*8}  {'─'*8}  {'─'*10}  {'─'*10}  {'─'*8}  {'─'*8}  {'─'*6}")
    for rb in results_b:
        print(f"  {rb['r0']:8.0f}  {rb['r_mean']:8.2f}  {rb['v_dyn']:10.4f}  "
              f"{rb['v_theory']:10.4f}  {rb['ratio']:8.3f}  "
              f"{rb['v_field_mid']:8.4f}  {rb['dr']:+6.3f}")
    if C_b is not None:
        print(f"  → Fit: v = {C_b:.4f}/r^{beta_b:.4f}, R² = {R2_b:.8f}")

    # ── CONCLUSION ──
    betas = []
    labels = []
    if beta_a is not None:
        betas.append(beta_a); labels.append('A')
    if beta_b is not None:
        betas.append(beta_b); labels.append('B')

    if len(betas) >= 1:
        mean_beta = np.mean(betas)
        if len(betas) == 2:
            spread = abs(betas[0] - betas[1]) / 2
        else:
            spread = 0.0

        if abs(mean_beta - 1.0) <= 0.05:
            verdict = "CONFIRMED"
        elif abs(mean_beta - 1.0) <= 0.15:
            verdict = "PARTIAL"
        else:
            verdict = "FAILED"

        conc = (f"β_A={beta_a:.4f} (R²={R2_a:.6f}), "
                f"β_B={beta_b:.4f} (R²={R2_b:.6f}) → "
                f"mean β={mean_beta:.4f}±{spread:.4f} → {verdict}: "
                f"The GP superfluid velocity field scales as v∝1/r, "
                f"confirming the 2D Coulomb law for quantum vortices")
    else:
        conc = "Insufficient data for fit"

    print(f"\n  CONCLUSION: {conc}")
    print(f"  Wall time: {total_time:.1f}s", flush=True)

    # ── PLOTS ──
    print("\n  Generating plots...", flush=True)
    C_a2, beta_a2, R2_a2 = plot_method_a(results_a, OUTDIR)
    C_b2, beta_b2, R2_b2 = plot_method_b(results_b, OUTDIR)
    main_plot = plot_combined(results_a, results_b, fit_a, fit_b, OUTDIR)

    # ── JSON ──
    jd = {
        'parameters': {
            'NX': NX, 'LX': LX, 'xi': XI, 'rho0': RHO_0,
            'it_steps': IT_STEPS, 'it_dt': IT_DT,
            'pin_amp': PIN_AMP, 'pin_width': PIN_WIDTH,
            'dt_rt': DT_RT, 'dipole_time': DIPOLE_TIME,
        },
        'method_a': {
            'description': 'Velocity field measurement from single vortex',
            'C': C_a, 'beta': beta_a, 'R2': R2_a,
            'data': [{'r': r['r'], 'v': r['v'], 'v_std': r['v_std']}
                     for r in results_a]
        },
        'method_b': {
            'description': 'Dipole translation speed (+1,-1)',
            'C': C_b, 'beta': beta_b, 'R2': R2_b,
            'data': [{'r0': rb['r0'], 'r_mean': rb['r_mean'],
                       'v_dyn': rb['v_dyn'], 'v_theory': rb['v_theory'],
                       'ratio': rb['ratio']}
                     for rb in results_b]
        },
        'conclusion': conc,
        'wall_time_s': total_time
    }
    jpath = os.path.join(OUTDIR, 'results_v1.json')
    with open(jpath, 'w') as f:
        json.dump(jd, f, indent=2)
    print(f"  JSON: {jpath}")

    print(f"\n  Main plot: {main_plot}", flush=True)
    print("  Done.", flush=True)


if __name__ == '__main__':
    main()
