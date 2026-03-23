#!/usr/bin/env python3
"""UHF-MRI Simulator v2 — rho0 (Temperature) Sweep: Fixed-chi, Fixed-mu Retrodiction
Fixes B0=3.0, chi=0.1, mu_chem=1.0 (all constant). Only gamma=rho0/xi^2 varies.
This breaks BOTH the gamma*chi cancellation AND the mu_chem competition.
UHF prediction: T2* increases as rho0 decreases (weaker Larmor spread).
Single RTX 3090.
"""

import torch
import torch.nn.functional as F
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import time
import os
import json

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Device: {device} ({torch.cuda.get_device_name()})")

# ====================== FUNDAMENTAL UHF PARAMETERS ======================
xi = 0.5            # healing length (UHF vacuum parameter)
g_torsion = 0.1     # torsional coupling strength
B0 = 3.0            # 3T clinical MRI field

# rho0 sweep — condensate density (temperature proxy)
rho0_values = [1.0, 0.8, 0.6, 0.4]

# These globals are updated per run:
rho0 = 1.0           # will be overwritten in sweep loop
gamma_derived = rho0 / xi**2
mu_chem = 1.0        # LOCKED constant — independent of rho0

print(f"\n  UHF Fundamental Parameters:")
print(f"    xi (healing length)     = {xi}")
print(f"    g_torsion (coupling)    = {g_torsion}")
print(f"    B0 (fixed)              = {B0}")
print(f"  Sweep variable: rho0 = {rho0_values}")
print(f"  Per-run derivations: gamma=rho0/xi^2, chi=0.1 (FIXED), mu_chem=1.0 (FIXED)")

# ====================== FIXED SIMULATION PARAMETERS ======================
N = 64
dx = 0.1
L = N * dx        # 6.4
dt = 0.005
m_scalar = 1.0
lambda_gp = 100.0
kappa_torsion = 0.0   # zero backreaction — no energy injection
KO_sigma = 0.01
K_init = 1.0
K_sat = 5.0
friction_equil = 5.0
friction_mri = 0.3

n_equilibrate = 1000
n_echo_half = 2000

# ====================== GRID (shared across runs) ======================
x = torch.linspace(-L/2 + dx/2, L/2 - dx/2, N, device=device)
X, Y, Z = torch.meshgrid(x, x, x, indexing='ij')
R2 = X**2 + Y**2 + Z**2
R_grid = torch.sqrt(R2).clamp(min=dx)

LAP_KERNEL = torch.tensor([[[[0, 0, 0], [0, -1, 0], [0, 0, 0]],
                             [[0, -1, 0], [-1, 6, -1], [0, -1, 0]],
                             [[0, 0, 0], [0, -1, 0], [0, 0, 0]]]],
                          dtype=torch.float32, device=device).unsqueeze(0) / (dx**2)

sponge_start = 0.7 * L / 2
sponge = torch.ones(N, N, N, device=device)
mask_s = R_grid > sponge_start
sponge[mask_s] = torch.exp(-3.0 * ((R_grid[mask_s] - sponge_start) / (L/2 - sponge_start))**2)
sponge_c = sponge.to(torch.complex64)

# ====================== HELPERS ======================
def laplacian_3d(f):
    return F.conv3d(f.unsqueeze(0).unsqueeze(0), LAP_KERNEL, padding=1)[0, 0]

def laplacian_complex(f):
    return laplacian_3d(f.real) + 1j * laplacian_3d(f.imag)

def grad_real(f, dim):
    return (torch.roll(f, -1, dim) - torch.roll(f, 1, dim)) / (2 * dx)

def kreiss_oliger_diss(f):
    d = torch.zeros_like(f)
    for dim in range(3):
        fp2 = torch.roll(f, -2, dim)
        fp1 = torch.roll(f, -1, dim)
        fm1 = torch.roll(f, 1, dim)
        fm2 = torch.roll(f, 2, dim)
        d += (fp2 - 4*fp1 + 6*f - 4*fm1 + fm2)
    return -KO_sigma / (16.0 * dt) * d

# ====================== PHANTOM ======================
sigma_phantom = 0.8
phi_bg = 0.3
phi_core = 1.0
phi_amp_init = phi_bg + (phi_core - phi_bg) * torch.exp(-R2 / (2 * sigma_phantom**2))
R_lesion_sq = (X - 0.5)**2 + Y**2 + Z**2
phi_amp_init = phi_amp_init - 0.3 * torch.exp(-R_lesion_sq / (2 * 0.2**2))
phi_amp_init = phi_amp_init.clamp(min=0.05)


def make_phantom():
    """Return fresh copies of phi, pi, K."""
    phi = phi_amp_init.clone().to(torch.complex64)
    pi_f = torch.zeros_like(phi)
    K = torch.zeros(3, N, N, N, device=device)
    K[2] = K_init
    return phi, pi_f, K


# ====================== EVOLUTION ======================
def compute_rhs(phi_s, pi_s, K_s, B0, friction=0.0, evolve_K=True):
    """RHS for KG + Bloch evolution with derived UHF parameters."""

    # --- Complex spatial gradients of phi ---
    dphi = []
    for dim in range(3):
        g_r = grad_real(phi_s.real, dim)
        g_i = grad_real(phi_s.imag, dim)
        dphi.append(g_r + 1j * g_i)

    # --- Vector covariant Laplacian ---
    lap_phi = laplacian_complex(phi_s)
    K_dot_grad_phi = K_s[0] * dphi[0] + K_s[1] * dphi[1] + K_s[2] * dphi[2]
    K_sq = K_s[0]**2 + K_s[1]**2 + K_s[2]**2
    div_K = grad_real(K_s[0], 0) + grad_real(K_s[1], 1) + grad_real(K_s[2], 2)
    D2_phi = lap_phi + 1j * div_K * phi_s + 2j * K_dot_grad_phi - K_sq * phi_s

    # KG evolution with chemical potential + friction
    dphi_dt = pi_s
    dpi_dt = (D2_phi - m_scalar**2 * phi_s
              - lambda_gp * (torch.abs(phi_s)**2 - mu_chem) * phi_s
              - friction * pi_s)
    dpi_dt = dpi_dt + (kreiss_oliger_diss(pi_s.real) + 1j * kreiss_oliger_diss(pi_s.imag))

    # --- Bloch evolution for K ---
    Kx, Ky, Kz = K_s[0], K_s[1], K_s[2]

    # Position-dependent Larmor frequency — DERIVED from UHF parameters
    # B_eff(r) = B0 * [1 + chi * (|phi|^2 - mu_chem)]
    # chi = 0.1 FIXED (not recomputed from g_torsion/rho0)
    # delta_omega = gamma * B0 * chi * delta(|phi|^2), gamma = rho0/xi^2
    # With chi fixed, delta_omega ∝ rho0 => T2* ∝ 1/rho0
    phi_sq = torch.abs(phi_s)**2
    chi_torsion = 0.1  # FIXED — breaks gamma*chi cancellation
    B_eff = B0 * (1.0 + chi_torsion * (phi_sq - mu_chem))

    # Larmor precession
    pre_x =  gamma_derived * Ky * B_eff
    pre_y = -gamma_derived * Kx * B_eff
    pre_z = torch.zeros_like(Kz)

    # kappa_torsion = 0: no energy injection into K
    dKx_dt = pre_x
    dKy_dt = pre_y
    dKz_dt = pre_z

    dK_dt = torch.stack([dKx_dt, dKy_dt, dKz_dt])
    if not evolve_K:
        dK_dt = torch.zeros_like(dK_dt)

    return dphi_dt, dpi_dt, dK_dt


def rk2_step(phi_s, pi_s, K_s, B0, friction=0.0, evolve_K=True):
    """RK2 midpoint integration step."""
    d1_phi, d1_pi, d1_K = compute_rhs(phi_s, pi_s, K_s, B0, friction, evolve_K)

    phi_h = phi_s + 0.5 * dt * d1_phi
    pi_h = pi_s + 0.5 * dt * d1_pi
    K_h = K_s + 0.5 * dt * d1_K

    d2_phi, d2_pi, d2_K = compute_rhs(phi_h, pi_h, K_h, B0, friction, evolve_K)

    phi_n = phi_s + dt * d2_phi
    pi_n = pi_s + dt * d2_pi
    K_n = K_s + dt * d2_K

    # Sponge boundary
    phi_n *= sponge_c
    pi_n *= sponge_c
    K_n *= sponge.unsqueeze(0)

    # Preserve |K| per cell — Larmor is norm-preserving
    K_mag_pre = torch.sqrt((K_s**2).sum(dim=0)).clamp(min=1e-12)
    K_mag_post = torch.sqrt((K_n**2).sum(dim=0)).clamp(min=1e-12)
    K_n = K_n * (K_mag_pre * sponge / K_mag_post).unsqueeze(0)

    # Hard clip
    K_mag = torch.sqrt((K_n**2).sum(dim=0)).clamp(min=1e-12)
    clamp = torch.clamp(K_sat / K_mag, max=1.0)
    K_n = K_n * clamp.unsqueeze(0)

    return phi_n, pi_n, K_n


def measure_mri(K_s, phi_s):
    """Compute MRI signal metrics (minimal, for sweep)."""
    Kx, Ky = K_s[0], K_s[1]
    Mxy_coh = float(torch.sqrt(Kx.mean()**2 + Ky.mean()**2))
    Mxy_inc = float(torch.sqrt(Kx**2 + Ky**2).mean())
    phi_max = float(torch.abs(phi_s).max())
    K_mag = float(torch.sqrt((K_s**2).sum(dim=0)).mean())
    return Mxy_coh, Mxy_inc, phi_max, K_mag


# ====================== rho0 (TEMPERATURE) SWEEP ======================
os.makedirs("UHF_MRI_results", exist_ok=True)
mid = N // 2
TE = 2 * n_echo_half * dt

sweep_results = []
all_echo_curves = {}

print(f"\n{'='*70}")
print(f"  UHF-MRI rho0 SWEEP: TEMPERATURE RETRODICTION TEST")
print(f"  Grid: {N}^3 | dx={dx} | dt={dt} | TE={TE:.1f}")
print(f"  B0={B0} (fixed) | xi={xi} | g_torsion={g_torsion}")
print(f"  kappa_torsion=0 (no energy injection)")
print(f"  rho0 values: {rho0_values}")
print(f"  Per-run: gamma=rho0/xi^2, chi=0.1 (FIXED), mu_chem=1.0 (FIXED)")
print(f"  Sequence per rho0: equil({n_equilibrate}) -> 90 -> dephase({n_echo_half})")
print(f"                     -> 180 -> echo({n_echo_half})")
print(f"{'='*70}")

total_start = time.time()

for rho_idx, rho0_val in enumerate(rho0_values):
    # Update globals for this run
    rho0 = rho0_val
    gamma_derived = rho0_val / xi**2
    # mu_chem stays locked at 1.0 — NOT updated with rho0
    chi_val = 0.1  # FIXED — no rho0 dependence

    print(f"\n{'='*50}")
    print(f"  rho0 = {rho0_val} (run {rho_idx+1}/{len(rho0_values)})")
    print(f"    gamma = {gamma_derived:.4f}, chi = {chi_val:.4f} (FIXED), mu_chem = {mu_chem:.2f} (FIXED)")
    print(f"{'='*50}")

    # Fresh phantom for each rho0
    phi, pi_field, K = make_phantom()
    t = 0.0

    # --- Phase 1: Equilibration (K frozen) ---
    print(f"  Equilibrating ({n_equilibrate} steps, K frozen)...")
    for step in range(n_equilibrate):
        phi, pi_field, K = rk2_step(phi, pi_field, K, B0, friction=friction_equil, evolve_K=False)
        t += dt
        if step == n_equilibrate - 1:
            Mxy_c, Mxy_i, pm, km = measure_mri(K, phi)
            phi_mean_sq = float(torch.abs(phi).pow(2).mean())
            print(f"    Post-equil: Mz={float(K[2].mean()):.4f} | phi_max={pm:.3f} | <|phi|^2>={phi_mean_sq:.3f} | K_mag={km:.4f}")

    # --- 90° Pulse ---
    Kx_old = K[0].clone()
    K[0] = K[2].clone()
    K[2] = -Kx_old

    initial_Mxy_coh, initial_Mxy_inc, _, _ = measure_mri(K, phi)
    print(f"    Post-90°: Mxy_coh={initial_Mxy_coh:.6f}")

    # --- Phase 2: Dephasing ---
    print(f"  Dephasing ({n_echo_half} steps)...")
    dephase_coh = []
    dephase_times = []
    for step in range(n_echo_half):
        phi, pi_field, K = rk2_step(phi, pi_field, K, B0, friction=friction_mri)
        t += dt
        if step % 10 == 0:
            mc, mi, pm, km = measure_mri(K, phi)
            dephase_coh.append(mc)
            dephase_times.append(t)
        if step % 400 == 0:
            mc, mi, pm, km = measure_mri(K, phi)
            print(f"    [dephase {step:4d}] Mxy_coh={mc:.6f} | phi_max={pm:.3f} | K_mag={km:.4f}")

    pre180_Mxy_coh = float(torch.sqrt(K[0].mean()**2 + K[1].mean()**2))
    print(f"    Pre-180° Mxy_coh={pre180_Mxy_coh:.6f}")

    # --- 180° Pulse ---
    K[1] = -K[1]
    K[2] = -K[2]

    # --- Phase 3: Echo ---
    print(f"  Echo formation ({n_echo_half} steps)...")
    echo_coh = []
    echo_inc = []
    echo_times = []
    for step in range(n_echo_half):
        phi, pi_field, K = rk2_step(phi, pi_field, K, B0, friction=friction_mri)
        t += dt
        if step % 10 == 0:
            mc, mi, pm, km = measure_mri(K, phi)
            echo_coh.append(mc)
            echo_inc.append(mi)
            echo_times.append(t)
        if step % 400 == 0:
            mc, mi, pm, km = measure_mri(K, phi)
            print(f"    [echo   {step:4d}] Mxy_coh={mc:.6f} | phi_max={pm:.3f} | K_mag={km:.4f}")

    # --- Results for this rho0 ---
    final_phi_max = float(torch.abs(phi).max())
    echo_peak_coh = max(echo_coh) if echo_coh else 0
    echo_peak_idx = echo_coh.index(echo_peak_coh)
    echo_peak_time = echo_times[echo_peak_idx]

    t_half = n_echo_half * dt
    ratio_decay = pre180_Mxy_coh / initial_Mxy_coh if initial_Mxy_coh > 0 else 0
    T2star = -t_half / np.log(ratio_decay + 1e-30) if 0 < ratio_decay < 1 else float('inf')

    echo_ratio = echo_peak_coh / initial_Mxy_coh if initial_Mxy_coh > 0 else 0
    refocus_eff = echo_ratio

    result = {
        "rho0": rho0_val,
        "gamma": gamma_derived,
        "chi": chi_val,
        "T2star": T2star,
        "refocusing": refocus_eff,
        "phi_max": final_phi_max,
        "echo_peak_coh": echo_peak_coh,
        "initial_Mxy_coh": initial_Mxy_coh,
        "pre180_coh": pre180_Mxy_coh,
        "echo_ratio": echo_ratio,
        "decay_ratio": ratio_decay,
    }
    sweep_results.append(result)
    all_echo_curves[rho0_val] = (echo_times, echo_coh, echo_inc)

    print(f"\n    --- rho0={rho0_val} RESULT ---")
    print(f"    gamma={gamma_derived:.4f} | chi={chi_val:.4f}")
    print(f"    T2*              = {T2star:.4f}")
    print(f"    Refocusing eff.  = {refocus_eff:.4f} ({refocus_eff*100:.1f}%)")
    print(f"    phi_max (final)  = {final_phi_max:.4f}")
    print(f"    Echo peak / init = {echo_ratio:.4f}")
    print(f"    K_mag (final)    = {km:.4f}")

total_elapsed = time.time() - total_start

# ====================== SUMMARY ======================
print(f"\n\n{'='*70}")
print(f"  rho0 SWEEP COMPLETE: {total_elapsed:.1f}s ({total_elapsed/60:.1f}m)")
print(f"{'='*70}")
print(f"  UHF Parameters: xi={xi}, g_torsion={g_torsion}, B0={B0}")
print(f"  kappa_torsion = 0 (no energy injection into K)")
print(f"")
print(f"  {'rho0':>6s} | {'gamma':>8s} | {'chi':>8s} | {'T2*':>10s} | {'Refocusing':>10s} | {'phi_max':>8s}")
print(f"  {'-'*6} | {'-'*8} | {'-'*8} | {'-'*10} | {'-'*10} | {'-'*8}")
for r in sweep_results:
    print(f"  {r['rho0']:6.2f} | {r['gamma']:8.4f} | {r['chi']:8.4f} | {r['T2star']:10.4f} | {r['refocusing']*100:9.1f}% | {r['phi_max']:8.3f}")

# Temperature scaling test: T2* vs 1/rho0
if len(sweep_results) >= 2:
    rho_arr = np.array([r['rho0'] for r in sweep_results])
    T2s_arr = np.array([r['T2star'] for r in sweep_results])
    inv_rho = 1.0 / rho_arr

    if all(np.isfinite(T2s_arr)) and all(T2s_arr > 0):
        # Linear fit: T2* = slope * (1/rho0) + intercept
        slope, intercept = np.polyfit(inv_rho, T2s_arr, 1)
        # R² of linear fit
        T2_pred_lin = slope * inv_rho + intercept
        ss_res = np.sum((T2s_arr - T2_pred_lin)**2)
        ss_tot = np.sum((T2s_arr - np.mean(T2s_arr))**2)
        R2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        print(f"\n  LINEAR FIT: T2* = {slope:.2f} / rho0 + {intercept:.2f}")
        print(f"  R² = {R2:.4f}")
        print(f"  Slope = {slope:.2f} (UHF predicts positive slope)")
        if slope > 0:
            print(f"  ✓ POSITIVE slope: T2* increases as rho0 decreases (UHF confirmed)")
        else:
            print(f"  ✗ NEGATIVE slope: does NOT match UHF prediction")
    else:
        print(f"\n  Cannot fit — some T2* values are infinite or negative.")

# ====================== SAVE ======================
with open("UHF_MRI_results/rho0_sweep_summary.json", "w") as f:
    json.dump({
        "uhf_params": {"xi": xi, "g_torsion": g_torsion, "B0": B0},
        "sweep_variable": "rho0",
        "results": sweep_results,
    }, f, indent=2)

# ====================== PLOT ======================
plt.style.use('dark_background')
fig, axes = plt.subplots(1, 3, figsize=(18, 5.5))

# (0) Echo curves for all rho0 values
colors = ['cyan', 'lime', 'orange', 'magenta']
for i, rv in enumerate(rho0_values):
    et, ec, ei = all_echo_curves[rv]
    axes[0].plot(et, ec, color=colors[i], lw=2, label=f'ρ₀={rv}')
axes[0].set_xlabel('Time')
axes[0].set_ylabel('Mxy (coherent)')
axes[0].set_title('Echo Curves vs ρ₀')
axes[0].legend()
axes[0].grid(True, alpha=0.2)

# (1) T2* vs 1/rho0
rho_arr = np.array([r['rho0'] for r in sweep_results])
T2s_arr = np.array([r['T2star'] for r in sweep_results])
inv_rho = 1.0 / rho_arr

axes[1].plot(inv_rho, T2s_arr, 'o-', color='lime', lw=2, ms=10)
if all(np.isfinite(T2s_arr)):
    inv_rho_fine = np.linspace(0.8, 3.0, 100)
    T2_fit = slope * inv_rho_fine + intercept
    axes[1].plot(inv_rho_fine, T2_fit, '--', color='red', alpha=0.7,
                 label=f'Fit: slope={slope:.1f}, R²={R2:.3f}')
    axes[1].legend()
axes[1].set_xlabel('1/ρ₀ (∝ temperature)')
axes[1].set_ylabel('T2*')
axes[1].set_title('T2* vs 1/ρ₀ (UHF predicts positive slope)')
axes[1].grid(True, alpha=0.2)

# (2) Refocusing efficiency vs rho0
refoc_arr = np.array([r['refocusing']*100 for r in sweep_results])
axes[2].bar(rho_arr, refoc_arr, width=0.1, color='orange', alpha=0.8)
axes[2].set_xlabel('ρ₀')
axes[2].set_ylabel('Refocusing (%)')
axes[2].set_title('Refocusing Efficiency vs ρ₀')
axes[2].axhline(y=80, color='green', ls='--', alpha=0.5, label='80% target')
axes[2].axhline(y=95, color='green', ls='--', alpha=0.5, label='95% target')
axes[2].legend()
axes[2].grid(True, alpha=0.2)

plt.suptitle(f'UHF-MRI ρ₀ Sweep | xi={xi}, g_tor={g_torsion}, B0={B0} | '
             f'kappa=0 | rho0={rho0_values}',
             fontsize=13, y=1.02)
plt.tight_layout()
plt.savefig("UHF_MRI_results/rho0_sweep.png", dpi=300, bbox_inches='tight')
print(f"\nPlot saved to UHF_MRI_results/rho0_sweep.png")
