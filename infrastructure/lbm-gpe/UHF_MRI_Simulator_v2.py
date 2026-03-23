#!/usr/bin/env python3
"""UHF-MRI Simulator v2: Torsion-Covariant MRI Physics
Proven infrastructure (RK2 midpoint, sponge, KO dissipation).
Vector covariant derivative: D_i phi = d_i phi + i K_i phi
  => D^2 phi = nabla^2 phi + i(div K)phi + 2i K.grad(phi) - |K|^2 phi
Bloch precession + superfluid phase-gradient T2* dephasing.
Spin-echo: equilibrate -> 90 deg -> dephase -> 180 deg -> echo.
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
import csv

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Device: {device} ({torch.cuda.get_device_name()})")

# ====================== PARAMETERS ======================
N = 64
dx = 0.1
L = N * dx        # 6.4
dt = 0.005
gamma_mri = 0.1   # gyromagnetic ratio (off-resonance from KG freq)
B0_strength = 1.0 # external B-field along z
m_scalar = 1.0    # scalar field mass (>> |K| to keep torsion perturbative)
lambda_gp = 100.0 # GP coupling (moderate — controls phi without over-constraining)
mu_chem = 1.0     # chemical potential — |phi|^2 relaxes toward mu
kappa_torsion = 0.0  # ZERO backreaction — no energy injection into K
dephase_strength = 18.0   # T2* from density-dependent Larmor: omega(r) = gamma*(B0 + g*(|phi|^2 - mu))
KO_sigma = 0.01
K_init = 1.0       # perturbative torsion — good MRI signal
friction_equil = 5.0   # strong pi damping during equilibration
friction_mri = 0.3     # moderate damping during MRI sequence (suppresses dynamic phi oscillations)

# Spin-echo sequence
n_equilibrate = 1000  # long equilibration to reach ground state
n_echo_half = 2000    # TE/2 = 10 time units
log_every = 10

# ====================== GRID ======================
x = torch.linspace(-L/2 + dx/2, L/2 - dx/2, N, device=device)
X, Y, Z = torch.meshgrid(x, x, x, indexing='ij')
R2 = X**2 + Y**2 + Z**2
R_grid = torch.sqrt(R2).clamp(min=dx)

LAP_KERNEL = torch.tensor([[[[0, 0, 0], [0, -1, 0], [0, 0, 0]],
                             [[0, -1, 0], [-1, 6, -1], [0, -1, 0]],
                             [[0, 0, 0], [0, -1, 0], [0, 0, 0]]]],
                          dtype=torch.float32, device=device).unsqueeze(0) / (dx**2)

# Sponge boundary (absorbing)
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
# Smooth Gaussian phantom: core + off-center lesion
sigma_phantom = 0.8
phi_bg = 0.3
phi_core = 1.0

phi_amp = phi_bg + (phi_core - phi_bg) * torch.exp(-R2 / (2 * sigma_phantom**2))

# Off-center lesion (lower density, mimics pathology)
R_lesion_sq = (X - 0.5)**2 + Y**2 + Z**2
phi_amp = phi_amp - 0.3 * torch.exp(-R_lesion_sq / (2 * 0.2**2))
phi_amp = phi_amp.clamp(min=0.05)

phi = phi_amp.to(torch.complex64)
pi_field = torch.zeros_like(phi)

# ====================== TORSION / MAGNETIZATION ======================
# K = (Kx, Ky, Kz) initially aligned with B0 = z-hat
K = torch.zeros(3, N, N, N, device=device)
K[2] = K_init  # equilibrium magnetization along B0

# K magnitude saturation (prevents runaway growth)
K_sat = 5.0

# ====================== EVOLUTION ======================
# Static B-field inhomogeneity map (set after equilibration)
B_inhom_static = None  # will be initialized after equilibration

def compute_rhs(phi_s, pi_s, K_s, friction=0.0, evolve_K=True):
    """RHS for KG + Bloch evolution."""
    global B_inhom_static

    # --- Complex spatial gradients of phi ---
    dphi = []
    for dim in range(3):
        g_r = grad_real(phi_s.real, dim)
        g_i = grad_real(phi_s.imag, dim)
        dphi.append(g_r + 1j * g_i)

    # --- Vector covariant Laplacian ---
    lap_phi = laplacian_complex(phi_s)

    # K . grad(phi)
    K_dot_grad_phi = K_s[0] * dphi[0] + K_s[1] * dphi[1] + K_s[2] * dphi[2]

    # |K|^2
    K_sq = K_s[0]**2 + K_s[1]**2 + K_s[2]**2

    # div(K)
    div_K = grad_real(K_s[0], 0) + grad_real(K_s[1], 1) + grad_real(K_s[2], 2)

    # D^2 phi = laplacian(phi) + i div(K) phi + 2i K.grad(phi) - |K|^2 phi
    D2_phi = lap_phi + 1j * div_K * phi_s + 2j * K_dot_grad_phi - K_sq * phi_s

    # KG evolution with chemical potential: lambda*(|phi|^2 - mu)*phi + friction
    dphi_dt = pi_s
    dpi_dt = (D2_phi - m_scalar**2 * phi_s
              - lambda_gp * (torch.abs(phi_s)**2 - mu_chem) * phi_s
              - friction * pi_s)
    dpi_dt = dpi_dt + (kreiss_oliger_diss(pi_s.real) + 1j * kreiss_oliger_diss(pi_s.imag))

    # --- Bloch evolution for K ---
    Kx, Ky, Kz = K_s[0], K_s[1], K_s[2]

    # Position-dependent Larmor frequency: omega(r) = gamma * (B0 + delta_B)
    # Use LIVE |phi|^2: static spatial variation -> T2* (refocusable)
    #                    temporal evolution -> T2 (irreversible, not refocusable)
    phi_sq = torch.abs(phi_s)**2
    B_eff = B0_strength + dephase_strength * (phi_sq - mu_chem)

    # Larmor precession with position-dependent frequency
    pre_x =  gamma_mri * Ky * B_eff
    pre_y = -gamma_mri * Kx * B_eff
    pre_z = torch.zeros_like(Kz)

    # Torsion backreaction: bare probability current J_i = Im(phi* d_i phi)
    Jx = torch.imag(torch.conj(phi_s) * dphi[0])
    Jy = torch.imag(torch.conj(phi_s) * dphi[1])
    Jz = torch.imag(torch.conj(phi_s) * dphi[2])

    dKx_dt = pre_x + kappa_torsion * Jx
    dKy_dt = pre_y + kappa_torsion * Jy
    dKz_dt = pre_z + kappa_torsion * Jz

    dK_dt = torch.stack([dKx_dt, dKy_dt, dKz_dt])
    if not evolve_K:
        dK_dt = torch.zeros_like(dK_dt)

    return dphi_dt, dpi_dt, dK_dt


def rk2_step(phi_s, pi_s, K_s, friction=0.0, evolve_K=True):
    """RK2 midpoint integration step."""
    d1_phi, d1_pi, d1_K = compute_rhs(phi_s, pi_s, K_s, friction, evolve_K)

    phi_h = phi_s + 0.5 * dt * d1_phi
    pi_h = pi_s + 0.5 * dt * d1_pi
    K_h = K_s + 0.5 * dt * d1_K

    d2_phi, d2_pi, d2_K = compute_rhs(phi_h, pi_h, K_h, friction, evolve_K)

    phi_n = phi_s + dt * d2_phi
    pi_n = pi_s + dt * d2_pi
    K_n = K_s + dt * d2_K

    # Sponge boundary
    phi_n *= sponge_c
    pi_n *= sponge_c
    K_n *= sponge.unsqueeze(0)

    # Preserve |K| per cell — Larmor precession is norm-preserving
    # (RK2 midpoint with time-varying B_eff introduces small norm drift)
    K_mag_pre = torch.sqrt((K_s**2).sum(dim=0)).clamp(min=1e-12)
    K_mag_post = torch.sqrt((K_n**2).sum(dim=0)).clamp(min=1e-12)
    # Only renormalize where K was nonzero before sponge damping
    # Sponge should still be able to damp K at boundaries
    K_n = K_n * (K_mag_pre * sponge / K_mag_post).unsqueeze(0)

    # K magnitude saturation: hard clip only when |K| > K_sat
    K_mag = torch.sqrt((K_n**2).sum(dim=0)).clamp(min=1e-12)
    clamp = torch.clamp(K_sat / K_mag, max=1.0)
    K_n = K_n * clamp.unsqueeze(0)

    return phi_n, pi_n, K_n


# ====================== MRI DIAGNOSTICS ======================
def measure_mri(K_s, phi_s, t_val, phase_label):
    """Compute MRI signal metrics."""
    Kx, Ky = K_s[0], K_s[1]
    # Coherent signal: |<Kx + iKy>| (phase-sensitive, like MRI receiver)
    Mxy_coh = float(torch.sqrt(Kx.mean()**2 + Ky.mean()**2))
    # Incoherent signal: <|Kx + iKy|> (total transverse mag)
    Mxy_inc = float(torch.sqrt(Kx**2 + Ky**2).mean())
    Mz_mean = float(K_s[2].mean())
    K_mag_mean = float(torch.sqrt((K_s**2).sum(dim=0)).mean())
    phi_max = float(torch.abs(phi_s).max())

    dphi_d = [(grad_real(phi_s.real, d) + 1j * grad_real(phi_s.imag, d)) for d in range(3)]
    J_sq_d = sum(torch.imag(torch.conj(phi_s) * dphi_d[d])**2 for d in range(3))
    gpg_max = float(torch.sqrt(J_sq_d).max())

    return {
        "t": t_val, "phase": phase_label,
        "Mxy_coh": Mxy_coh, "Mxy_inc": Mxy_inc,
        "Mz": Mz_mean, "K_mag": K_mag_mean,
        "phi_max": phi_max, "grad_phase_max": gpg_max,
    }


# ====================== MAIN LOOP ======================
mid = N // 2
t = 0.0
history = []

os.makedirs("UHF_MRI_results", exist_ok=True)
start_time = time.time()

TE = 2 * n_echo_half * dt  # echo time

print(f"\n{'='*70}")
print(f"  UHF-MRI SIMULATOR v2: TORSION-COVARIANT MRI")
print(f"  Grid: {N}^3 = {N**3:,} cells | dx={dx} | dt={dt}")
print(f"  MRI: gamma={gamma_mri} | B0={B0_strength} | TE={TE:.1f}")
print(f"  KG:  m={m_scalar} | lambda={lambda_gp} | kappa={kappa_torsion}")
print(f"  Dephase strength: {dephase_strength}")
print(f"  K_init={K_init} | K_sat={K_sat}")
print(f"  Phantom: Gaussian core sigma={sigma_phantom}, |phi|_peak={phi_core}")
print(f"           + lesion at (0.5,0,0), r=0.2")
print(f"  Sequence: equil({n_equilibrate}) -> 90deg -> dephase({n_echo_half})")
print(f"            -> 180deg -> echo({n_echo_half})")
print(f"  Total steps: {n_equilibrate + 2 * n_echo_half}")
print(f"{'='*70}\n")

# --------------------------------------------------------
# Phase 1: Equilibration
# --------------------------------------------------------
print("Phase 1: EQUILIBRATION — finding condensate ground state (K frozen)...")
for step in range(n_equilibrate):
    phi, pi_field, K = rk2_step(phi, pi_field, K, friction=friction_equil, evolve_K=False)
    t += dt
    if step % 50 == 0:
        m = measure_mri(K, phi, t, "equil")
        history.append(m)
        print(f"  [equil {step:4d}] t={t:.3f} | Mxy_coh={m['Mxy_coh']:.5f} | "
              f"Mz={m['Mz']:.4f} | K_mag={m['K_mag']:.4f} | "
              f"grad_phase={m['grad_phase_max']:.3f} | phi_max={m['phi_max']:.4f}")

equil_Kz = float(K[2].mean())
equil_Mxy = float(torch.sqrt(K[0]**2 + K[1]**2).mean())
print(f"  Equilibration done at t={t:.3f}: Mz={equil_Kz:.4f}, Mxy_inc={equil_Mxy:.6f}")

# Freeze the condensate density as a STATIC B-field inhomogeneity map
# Analogous to tissue being stationary during an MRI scan
B_inhom_static = dephase_strength * (torch.abs(phi)**2 - mu_chem)
print(f"  B_inhom range: [{float(B_inhom_static.min()):.4f}, {float(B_inhom_static.max()):.4f}]")

# Save phi phase map post-equilibration (for plotting)
phi_phase_equil = torch.angle(phi[:, :, mid]).cpu().numpy()
phi_amp_equil = torch.abs(phi[:, :, mid]).cpu().numpy()

# --------------------------------------------------------
# 90° Pulse (about y-axis: Kx -> Kz, Kz -> -Kx)
# --------------------------------------------------------
print(f"\n--- APPLYING 90° PULSE (about y-axis) ---")
Kx_old = K[0].clone()
K[0] = K[2].clone()
K[2] = -Kx_old

m = measure_mri(K, phi, t, "pulse90")
history.append(m)
initial_Mxy_coh = m["Mxy_coh"]
initial_Mxy_inc = m["Mxy_inc"]
print(f"  Post-90°: Mxy_coh={initial_Mxy_coh:.5f} | Mxy_inc={initial_Mxy_inc:.5f} | "
      f"Mz={m['Mz']:.4f}")

# --------------------------------------------------------
# Phase 2: Free Precession / T2* Dephasing
# --------------------------------------------------------
print(f"\nPhase 2: DEPHASING — free precession for {n_echo_half} steps (TE/2={n_echo_half*dt:.1f})...")
dephase_signal = []

for step in range(n_echo_half):
    phi, pi_field, K = rk2_step(phi, pi_field, K, friction=friction_mri)
    t += dt
    if step % log_every == 0:
        m = measure_mri(K, phi, t, "dephase")
        history.append(m)
        dephase_signal.append(m["Mxy_coh"])
        if step % 200 == 0:
            print(f"  [dephase {step:4d}] t={t:.3f} | Mxy_coh={m['Mxy_coh']:.5f} | "
                  f"Mxy_inc={m['Mxy_inc']:.5f} | Mz={m['Mz']:.4f} | "
                  f"K_mag={m['K_mag']:.4f}")

    if torch.isnan(phi[mid, mid, mid]):
        print("\n[!] NaN DETECTED — halting.")
        break

pre180_Mxy_coh = float(torch.sqrt(K[0].mean()**2 + K[1].mean()**2))
pre180_Mxy_inc = float(torch.sqrt(K[0]**2 + K[1]**2).mean())
print(f"  Pre-180° | Mxy_coh={pre180_Mxy_coh:.5f} | Mxy_inc={pre180_Mxy_inc:.5f}")

# --------------------------------------------------------
# 180° Refocusing Pulse (about x-axis: Ky -> -Ky, Kz -> -Kz)
# --------------------------------------------------------
print(f"\n--- APPLYING 180° REFOCUSING PULSE (about x-axis) ---")
K[1] = -K[1]
K[2] = -K[2]

m = measure_mri(K, phi, t, "pulse180")
history.append(m)
print(f"  Post-180°: Mxy_coh={m['Mxy_coh']:.5f} | Mxy_inc={m['Mxy_inc']:.5f} | "
      f"Mz={m['Mz']:.4f}")

# --------------------------------------------------------
# Phase 3: Echo Formation
# --------------------------------------------------------
print(f"\nPhase 3: ECHO FORMATION — refocusing for {n_echo_half} steps...")
echo_signal_coh = []
echo_signal_inc = []
echo_times = []

for step in range(n_echo_half):
    phi, pi_field, K = rk2_step(phi, pi_field, K, friction=friction_mri)
    t += dt
    if step % log_every == 0:
        m = measure_mri(K, phi, t, "echo")
        history.append(m)
        echo_signal_coh.append(m["Mxy_coh"])
        echo_signal_inc.append(m["Mxy_inc"])
        echo_times.append(m["t"])
        if step % 200 == 0:
            print(f"  [echo   {step:4d}] t={t:.3f} | Mxy_coh={m['Mxy_coh']:.5f} | "
                  f"Mxy_inc={m['Mxy_inc']:.5f} | Mz={m['Mz']:.4f} | "
                  f"K_mag={m['K_mag']:.4f}")

    if torch.isnan(phi[mid, mid, mid]):
        print("\n[!] NaN DETECTED — halting.")
        break

# ====================== RESULTS ======================
elapsed = time.time() - start_time

echo_peak_coh = max(echo_signal_coh) if echo_signal_coh else 0
echo_peak_idx = echo_signal_coh.index(echo_peak_coh) if echo_signal_coh else 0
echo_peak_time = echo_times[echo_peak_idx] if echo_times else 0
echo_peak_inc = max(echo_signal_inc) if echo_signal_inc else 0

# T2* estimate: decay from initial to pre-180
t_half = n_echo_half * dt
if pre180_Mxy_coh > 1e-30 and initial_Mxy_coh > 1e-30:
    ratio_decay = pre180_Mxy_coh / initial_Mxy_coh
    T2star_est = -t_half / np.log(ratio_decay + 1e-30) if 0 < ratio_decay < 1 else float('inf')
else:
    ratio_decay = 0.0
    T2star_est = float('inf')

# T2 estimate: echo_peak / initial
if echo_peak_coh > 1e-30 and initial_Mxy_coh > 1e-30:
    echo_ratio = echo_peak_coh / initial_Mxy_coh
    T2_est = -TE / np.log(echo_ratio + 1e-30) if 0 < echo_ratio < 1 else float('inf')
else:
    echo_ratio = 0.0
    T2_est = float('inf')

# Refocusing efficiency
if pre180_Mxy_coh > 1e-30:
    refocus_eff = echo_peak_coh / initial_Mxy_coh
else:
    refocus_eff = 0.0

print(f"\n{'='*70}")
print(f"  UHF-MRI v2 COMPLETE: {elapsed:.1f}s ({elapsed/60:.1f}m)")
print(f"{'='*70}")
print(f"  Initial Mxy (post-90°, coh)  = {initial_Mxy_coh:.6f}")
print(f"  Initial Mxy (post-90°, inc)  = {initial_Mxy_inc:.6f}")
print(f"  Pre-180° Mxy (coh)           = {pre180_Mxy_coh:.6f}")
print(f"  Pre-180° Mxy (inc)           = {pre180_Mxy_inc:.6f}")
print(f"  T2* decay ratio              = {ratio_decay:.4f}")
print(f"  Estimated T2*                = {T2star_est:.4f}")
print(f"  Echo peak Mxy (coherent)     = {echo_peak_coh:.6f}")
print(f"  Echo peak Mxy (incoherent)   = {echo_peak_inc:.6f}")
print(f"  Echo peak time               = {echo_peak_time:.3f}")
print(f"  Echo ratio (peak/initial)    = {echo_ratio:.4f}")
print(f"  Estimated T2                 = {T2_est:.4f}")
print(f"  Refocusing efficiency        = {refocus_eff:.4f}")
print(f"  Final phi max                = {float(torch.abs(phi).max()):.6f}")
print(f"  Final K magnitude mean       = {float(torch.sqrt((K**2).sum(dim=0)).mean()):.6f}")
print(f"  Final Kz mean                = {float(K[2].mean()):.6f}")

# ====================== SAVE ======================
# Echo signal CSV
with open("UHF_MRI_results/echo_signal.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["t", "Mxy_coherent", "Mxy_incoherent"])
    for i in range(len(echo_times)):
        w.writerow([echo_times[i], echo_signal_coh[i], echo_signal_inc[i]])

# Full history JSON
with open("UHF_MRI_results/mri_history.json", "w") as f:
    json.dump(history, f)

# Summary JSON
summary = {
    "initial_Mxy_coh": initial_Mxy_coh,
    "initial_Mxy_inc": initial_Mxy_inc,
    "pre180_Mxy_coh": pre180_Mxy_coh,
    "echo_peak_coh": echo_peak_coh,
    "echo_peak_inc": echo_peak_inc,
    "echo_peak_time": echo_peak_time,
    "echo_ratio": echo_ratio,
    "T2star_est": T2star_est,
    "T2_est": T2_est,
    "refocus_eff": refocus_eff,
    "TE": TE,
    "elapsed_s": elapsed,
}
with open("UHF_MRI_results/mri_summary.json", "w") as f:
    json.dump(summary, f, indent=2)

print(f"\n  CSV/JSON saved to UHF_MRI_results/")

# ====================== PLOTTING ======================
# Collect spatial maps at final time
Kxy_map = torch.sqrt(K[0]**2 + K[1]**2)[:, :, mid].cpu().numpy()
Kz_map = K[2][:, :, mid].cpu().numpy()
phi_amp_final = torch.abs(phi[:, :, mid]).cpu().numpy()
phi_phase_final = torch.angle(phi[:, :, mid]).cpu().numpy()

# Phase gradient magnitude (probability current, wrapping-free)
dphi_plot = [(grad_real(phi.real, d) + 1j * grad_real(phi.imag, d)) for d in range(3)]
J_sq_plot = sum(torch.imag(torch.conj(phi) * dphi_plot[d])**2 for d in range(3))
gpg_map = torch.sqrt(J_sq_plot[:, :, mid]).cpu().numpy()

extent = [-L/2, L/2, -L/2, L/2]

plt.style.use('dark_background')
fig, axes = plt.subplots(2, 3, figsize=(18, 11))

# (0,0) Phantom
im00 = axes[0, 0].imshow(phi_amp_equil.T, cmap='gray', origin='lower', extent=extent)
axes[0, 0].set_title('|phi| Phantom (pre-pulse)')
axes[0, 0].set_xlabel('x'); axes[0, 0].set_ylabel('y')
plt.colorbar(im00, ax=axes[0, 0])

# (0,1) Transverse magnetization at echo
im01 = axes[0, 1].imshow(Kxy_map.T, cmap='hot', origin='lower', extent=extent)
axes[0, 1].set_title('Mxy at Echo (transverse mag)')
axes[0, 1].set_xlabel('x'); axes[0, 1].set_ylabel('y')
plt.colorbar(im01, ax=axes[0, 1])

# (0,2) Phase gradient (dephasing source)
im02 = axes[0, 2].imshow(gpg_map.T, cmap='inferno', origin='lower', extent=extent)
axes[0, 2].set_title('|grad(phase)| (dephasing map)')
axes[0, 2].set_xlabel('x'); axes[0, 2].set_ylabel('y')
plt.colorbar(im02, ax=axes[0, 2])

# (1,0) Full signal timeline
all_coh = [h["Mxy_coh"] for h in history]
all_t = [h["t"] for h in history]
all_phase = [h["phase"] for h in history]

# Color-code by phase
for phase_name, color in [("equil", "gray"), ("dephase", "cyan"), ("echo", "lime")]:
    mask = [i for i, p in enumerate(all_phase) if p == phase_name]
    if mask:
        axes[1, 0].plot([all_t[i] for i in mask], [all_coh[i] for i in mask],
                       color=color, lw=1.5, label=phase_name)
# Mark pulses
pulse_times = [h["t"] for h in history if h["phase"] in ("pulse90", "pulse180")]
for pt in pulse_times:
    axes[1, 0].axvline(x=pt, color='red', ls='--', alpha=0.6)
axes[1, 0].set_xlabel('Time')
axes[1, 0].set_ylabel('Mxy (coherent)')
axes[1, 0].set_title('MRI Signal: Dephasing + Echo')
axes[1, 0].legend(fontsize=8)
axes[1, 0].grid(True, alpha=0.2)

# (1,1) Echo zoom
if echo_times:
    axes[1, 1].plot(echo_times, echo_signal_coh, color='lime', lw=2, label='Coherent')
    axes[1, 1].plot(echo_times, echo_signal_inc, color='orange', lw=1, alpha=0.7, label='Incoherent')
    if echo_peak_time > 0:
        axes[1, 1].axvline(x=echo_peak_time, color='red', ls='--', alpha=0.5, label=f'Peak t={echo_peak_time:.2f}')
    axes[1, 1].set_xlabel('Time')
    axes[1, 1].set_ylabel('Mxy')
    axes[1, 1].set_title(f'Echo Signal (peak={echo_peak_coh:.5f})')
    axes[1, 1].legend(fontsize=8)
    axes[1, 1].grid(True, alpha=0.2)

# (1,2) Kz map
im12 = axes[1, 2].imshow(Kz_map.T, cmap='coolwarm', origin='lower', extent=extent)
axes[1, 2].set_title('Kz (longitudinal magnetization)')
axes[1, 2].set_xlabel('x'); axes[1, 2].set_ylabel('y')
plt.colorbar(im12, ax=axes[1, 2])

plt.suptitle(f'UHF-MRI v2: Spin-Echo | TE={TE:.1f} | Echo Peak={echo_peak_coh:.5f} | T2*≈{T2star_est:.2f}',
             fontsize=13, y=1.01)
plt.tight_layout()
plt.savefig("UHF_MRI_results/uhf_mri_v2.png", dpi=300, bbox_inches='tight')
print(f"Plot saved to UHF_MRI_results/uhf_mri_v2.png")
