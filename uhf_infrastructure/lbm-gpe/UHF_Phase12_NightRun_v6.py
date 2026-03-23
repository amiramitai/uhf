#!/usr/bin/env python3
"""UHF Phase 12 Night Run v6: True Plunge Merger
Proven v5b physics engine (gauge driver + rest-mass source + grad_alpha force).
True plunge tuning:
  - d_sep = 0.75 (deeply overlapping cores)
  - v_radial = 0.3 (inward radial kick via pi_field)
  - v_orbit = 0.15 (minimal, chirality measurement only)
  - G = 2.0, amp = 0.20 (stronger gravitational binding)
  - lambda = 0.5 (minimal GP repulsion)
  - sigma = 1.2 (tighter cores)
  - tau_lapse = 3.0 (faster lapse response for stronger gravity)
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
N = 80
L = 40.0
dx = L / N            # 0.5
dt = 0.0025
G_grav = 2.0          # v6: stronger gravity for binding
m_scalar = 0.1
lam = 0.5             # v6: minimal GP repulsion
kappa_torsion = 0.8
m_torsion_sq = 1.0
z4c_damping = 0.05
lam_K5_cubic = 0.3
K5_sat = 2.0
KO_sigma = 0.02
n_steps = 50000
log_every = 500
checkpoint_every = 5000
GW_extraction_radius = 15.0

# Dynamic lapse parameters
tau_lapse = 3.0        # v6: faster response for stronger gravity
alpha_target_floor = 0.1
alpha_hard_min = 0.01
alpha_hard_max = 2.0

# Binary IC — TRUE PLUNGE
d_sep = 0.75           # v6: deeply overlapping (< sigma)
sigma_core = 1.2       # v6: tighter cores
amp_core = 0.20        # v6: stronger gravity
v_orbit = 0.15         # v6: reduced — just chirality measurement
v_radial = 0.3         # v6: NEW — inward radial velocity
k_boost = m_scalar * v_orbit

# ====================== GRID ======================
x = torch.linspace(-L/2 + dx/2, L/2 - dx/2, N, device=device)
X, Y, Z = torch.meshgrid(x, x, x, indexing='ij')
R2 = X**2 + Y**2 + Z**2
R_grid = torch.sqrt(R2).clamp(min=dx)

LAP_KERNEL = torch.tensor([[[[0,0,0],[0,-1,0],[0,0,0]],
                             [[0,-1,0],[-1,6,-1],[0,-1,0]],
                             [[0,0,0],[0,-1,0],[0,0,0]]]],
                          dtype=torch.float32, device=device).unsqueeze(0) / (dx**2)

kf = torch.fft.fftfreq(N, d=dx, device=device) * 2 * torch.pi
KX_f, KY_f, KZ_f = torch.meshgrid(kf, kf, kf, indexing='ij')
K2_fft = KX_f**2 + KY_f**2 + KZ_f**2
K2_fft[0, 0, 0] = 1.0

sponge_start = 0.7 * L / 2
sponge = torch.ones(N, N, N, device=device)
mask_s = R_grid > sponge_start
sponge[mask_s] = torch.exp(-3.0 * ((R_grid[mask_s] - sponge_start) / (L/2 - sponge_start))**2)
sponge_c = sponge.to(torch.complex64)

gw_shell = (torch.abs(R_grid - GW_extraction_radius) < 1.5 * dx).float()
Qxx_w = (3 * X**2 - R2) * gw_shell
Qxy_w = 3 * X * Y * gw_shell

# ====================== HELPERS ======================
def laplacian_3d(f):
    return F.conv3d(f.unsqueeze(0).unsqueeze(0), LAP_KERNEL, padding=1)[0, 0]

def laplacian_complex(f):
    return laplacian_3d(f.real) + 1j * laplacian_3d(f.imag)

def gradient_sq_real(f):
    gx = (torch.roll(f, -1, 0) - torch.roll(f, 1, 0)) / (2 * dx)
    gy = (torch.roll(f, -1, 1) - torch.roll(f, 1, 1)) / (2 * dx)
    gz = (torch.roll(f, -1, 2) - torch.roll(f, 1, 2)) / (2 * dx)
    return gx**2 + gy**2 + gz**2

def gradient_dot_complex(K, f):
    gx = (torch.roll(f, -1, 0) - torch.roll(f, 1, 0)) / (2 * dx)
    gy = (torch.roll(f, -1, 1) - torch.roll(f, 1, 1)) / (2 * dx)
    gz = (torch.roll(f, -1, 2) - torch.roll(f, 1, 2)) / (2 * dx)
    return K * (gx + gy + gz)

def grad_alpha_dot_grad_phi(alpha_f, phi_f):
    """Gravitational force: nabla(alpha) . nabla(phi)."""
    result_r = torch.zeros(N, N, N, device=device)
    result_i = torch.zeros(N, N, N, device=device)
    for dim in range(3):
        da = (torch.roll(alpha_f, -1, dim) - torch.roll(alpha_f, 1, dim)) / (2 * dx)
        dp_r = (torch.roll(phi_f.real, -1, dim) - torch.roll(phi_f.real, 1, dim)) / (2 * dx)
        dp_i = (torch.roll(phi_f.imag, -1, dim) - torch.roll(phi_f.imag, 1, dim)) / (2 * dx)
        result_r += da * dp_r
        result_i += da * dp_i
    return result_r + 1j * result_i

def kreiss_oliger_diss(f):
    d = torch.zeros_like(f)
    for dim in range(3):
        fp2 = torch.roll(f, -2, dim)
        fp1 = torch.roll(f, -1, dim)
        fm1 = torch.roll(f, 1, dim)
        fm2 = torch.roll(f, 2, dim)
        d += (fp2 - 4*fp1 + 6*f - 4*fm1 + fm2)
    return -KO_sigma / (16.0 * dt) * d

def poisson_fft(rho):
    rho_hat = torch.fft.fftn(rho)
    V_hat = -4.0 * torch.pi * G_grav * rho_hat / K2_fft
    V_hat[0, 0, 0] = 0.0
    return torch.fft.ifftn(V_hat).real

def extract_gw_strain(rho):
    h_plus = float(torch.sum(Qxx_w * rho) * dx**3)
    h_cross = float(torch.sum(Qxy_w * rho) * dx**3)
    return h_plus, h_cross

def compute_binary_separation(phi_abs_sq):
    rho = phi_abs_sq.clamp(min=0)
    left_mask = (X < 0)
    right_mask = (X > 0)
    rho_left = rho * left_mask
    rho_right = rho * right_mask
    m_left = rho_left.sum()
    m_right = rho_right.sum()
    if m_left < 1e-10 or m_right < 1e-10:
        return 0.0
    x_cm_l = (X * rho_left).sum() / m_left
    y_cm_l = (Y * rho_left).sum() / m_left
    x_cm_r = (X * rho_right).sum() / m_right
    y_cm_r = (Y * rho_right).sum() / m_right
    return float(torch.sqrt((x_cm_r - x_cm_l)**2 + (y_cm_r - y_cm_l)**2))

def compute_Lz(phi_f, dx_val):
    grad_x = (torch.roll(phi_f, -1, 0) - torch.roll(phi_f, 1, 0)) / (2 * dx_val)
    grad_y = (torch.roll(phi_f, -1, 1) - torch.roll(phi_f, 1, 1)) / (2 * dx_val)
    Lz_density = torch.imag(torch.conj(phi_f) * (X * grad_y - Y * grad_x))
    return float(Lz_density.sum() * dx_val**3)

# ====================== INITIAL CONDITIONS ======================
R1_sq = (X - d_sep)**2 + Y**2 + Z**2
phi1 = amp_core * torch.exp(-R1_sq / (2 * sigma_core**2))
R2_sq = (X + d_sep)**2 + Y**2 + Z**2
phi2 = amp_core * torch.exp(-R2_sq / (2 * sigma_core**2))

# Complex phi with orbital phase kicks
phi = (phi1 * torch.exp(1j * k_boost * Y) +
       phi2 * torch.exp(-1j * k_boost * Y)).to(torch.complex64)

# v6 NEW: Initialize pi_field with inward radial velocity
# Core 1 at +d_sep: moving in -x direction.  d_t phi1 ~ -v_rad * d_x phi1
#   d_x phi1 = phi1 * (-(x-d_sep)/sigma^2)
#   => d_t phi1 = +v_rad * phi1 * (x-d_sep)/sigma^2
# Core 2 at -d_sep: moving in +x direction.  d_t phi2 ~ +v_rad * d_x phi2
#   d_x phi2 = phi2 * (-(x+d_sep)/sigma^2)
#   => d_t phi2 = -v_rad * phi2 * (x+d_sep)/sigma^2
pi_radial_1 = v_radial / sigma_core**2 * (X - d_sep) * phi1 * torch.exp(1j * k_boost * Y)
pi_radial_2 = -v_radial / sigma_core**2 * (X + d_sep) * phi2 * torch.exp(-1j * k_boost * Y)
pi_field = (pi_radial_1 + pi_radial_2).to(torch.complex64)

K5_1 = kappa_torsion * torch.exp(-R1_sq / (2*(sigma_core*1.5)**2)) * (X - d_sep) * Y / (R1_sq + 1.0)
K5_2 = kappa_torsion * torch.exp(-R2_sq / (2*(sigma_core*1.5)**2)) * (X + d_sep) * Y / (R2_sq + 1.0)
K5 = K5_1 + K5_2
pi_K5 = torch.zeros_like(K5)

# Initialize alpha from initial potential
rho_init = torch.abs(phi)**2
V_init = poisson_fft(rho_init)
alpha = torch.clamp(1.0 + 2.0 * V_init, min=0.2, max=alpha_hard_max)

# ====================== EVOLUTION ======================
def compute_rhs(phi_s, pi_s, K5_s, pi_K5_s, alpha_s):
    lap_phi = laplacian_complex(phi_s)

    # Rest-mass only source
    rho_grav = torch.abs(phi_s)**2
    rho_torsion = 0.5 * pi_K5_s**2 + 0.5 * gradient_sq_real(K5_s)
    rho_total = rho_grav + 0.1 * rho_torsion

    V = poisson_fft(rho_total)

    # Gauge driver target
    alpha_target = torch.clamp(1.0 + 2.0 * V, min=alpha_target_floor)

    # Dynamic lapse ODE
    dalpha_dt = -(alpha_s / tau_lapse) * (alpha_s - alpha_target)
    dalpha_dt = dalpha_dt + kreiss_oliger_diss(alpha_s)

    # Torsion-covariant Laplacian
    torsion_grad = gradient_dot_complex(K5_s, phi_s)
    D2_phi = lap_phi + 2j * torsion_grad - K5_s**2 * phi_s

    # ADM KG evolution with gravitational force
    grav_force = grad_alpha_dot_grad_phi(alpha_s, phi_s)

    dphi_dt = alpha_s * pi_s
    dpi_dt = (alpha_s * (D2_phi - m_scalar**2 * phi_s
                         - lam * torch.abs(phi_s)**2 * phi_s)
              + grav_force)
    dpi_dt = dpi_dt + kreiss_oliger_diss(pi_s)

    # Torsion K5 evolution
    lap_K5 = laplacian_3d(K5_s)
    J5 = torch.imag(torch.conj(phi_s) * pi_s)
    dK5_dt = alpha_s * pi_K5_s
    nonlin_damp = z4c_damping * (1.0 + (K5_s / K5_sat)**2)
    dpi_K5_dt = alpha_s * (lap_K5 - m_torsion_sq * K5_s
                          - lam_K5_cubic * K5_s**3
                          + kappa_torsion * J5) - nonlin_damp * pi_K5_s
    dpi_K5_dt = dpi_K5_dt + kreiss_oliger_diss(pi_K5_s)

    return dphi_dt, dpi_dt, dK5_dt, dpi_K5_dt, dalpha_dt, V, alpha_s, rho_total

# ====================== MAIN LOOP ======================
mid = N // 2
t = 0.0

history = {"t": [], "central_rho": [], "central_lapse": [],
           "K5_max": [], "h_plus": [], "h_cross": [], "total_mass": [],
           "binary_sep": [], "circ_pol": [], "Lz": [], "alpha_min": []}

os.makedirs("UHF_results", exist_ok=True)
start_time = time.time()

init_sep = compute_binary_separation(torch.abs(phi)**2)
init_Lz = compute_Lz(phi, dx)
init_pi_max = float(torch.abs(pi_field).max())

print(f"\n{'='*70}")
print(f"  UHF PHASE 12 v6: TRUE PLUNGE MERGER")
print(f"  Gauge driver ODE (tau={tau_lapse}) + rest-mass source + grad_alpha force")
print(f"  Grid: {N}^3 = {N**3:,} cells | dx={dx:.3f} | dt={dt} | {n_steps:,} steps")
print(f"  Binary: d_sep={d_sep}, sigma={sigma_core}, amp={amp_core}")
print(f"  Plunge: v_radial={v_radial}, v_orbit={v_orbit}")
print(f"  Physics: G={G_grav}, m={m_scalar}, lam={lam}, kappa={kappa_torsion}")
print(f"  Z4c damping: {z4c_damping} | KO sigma: {KO_sigma}")
print(f"  GW extraction r={GW_extraction_radius}")
print(f"  K5 initial max: {float(K5.abs().max()):.4f}")
print(f"  Initial separation: {init_sep:.2f}")
print(f"  Initial max|pi|: {init_pi_max:.4f}")
print(f"  Initial Lz: {init_Lz:.4f}")
print(f"  Initial central lapse: {float(alpha[mid,mid,mid]):.4f}")
print(f"  Initial min lapse: {float(alpha.min()):.4f}")
print(f"{'='*70}\n")

for step in range(n_steps):
    # --- RK2 Midpoint ---
    d1 = compute_rhs(phi, pi_field, K5, pi_K5, alpha)
    d1_phi, d1_pi, d1_K5, d1_piK5, d1_alpha = d1[:5]

    phi_h = phi + 0.5 * dt * d1_phi
    pi_h = pi_field + 0.5 * dt * d1_pi
    K5_h = K5 + 0.5 * dt * d1_K5
    piK5_h = pi_K5 + 0.5 * dt * d1_piK5
    alpha_h = torch.clamp(alpha + 0.5 * dt * d1_alpha, min=alpha_hard_min, max=alpha_hard_max)

    d2 = compute_rhs(phi_h, pi_h, K5_h, piK5_h, alpha_h)
    d2_phi, d2_pi, d2_K5, d2_piK5, d2_alpha, V, alpha_used, rho = d2

    phi = phi + dt * d2_phi
    pi_field = pi_field + dt * d2_pi
    K5 = K5 + dt * d2_K5
    pi_K5 = pi_K5 + dt * d2_piK5
    alpha = torch.clamp(alpha + dt * d2_alpha, min=alpha_hard_min, max=alpha_hard_max)

    # Sponge damping
    phi *= sponge_c
    pi_field *= sponge_c
    K5 *= sponge
    pi_K5 *= sponge
    alpha = alpha * sponge + (1.0 - sponge)

    # Smooth tanh saturation on K5
    K5 = K5_sat * torch.tanh(K5 / K5_sat)
    t += dt

    # --- Logging ---
    if step % log_every == 0:
        c_rho = float(torch.abs(phi[mid, mid, mid])**2)
        c_lapse = float(alpha[mid, mid, mid])
        min_lapse = float(alpha.min())
        k5_max = float(torch.max(torch.abs(K5)))
        h_plus, h_cross = extract_gw_strain(rho)
        M_total = float(torch.sum(rho) * dx**3)
        sep = compute_binary_separation(torch.abs(phi)**2)
        Lz = compute_Lz(phi, dx)
        f_circ = abs(h_cross) / (abs(h_plus) + abs(h_cross) + 1e-30)

        history["t"].append(t)
        history["central_rho"].append(c_rho)
        history["central_lapse"].append(c_lapse)
        history["K5_max"].append(k5_max)
        history["h_plus"].append(h_plus)
        history["h_cross"].append(h_cross)
        history["total_mass"].append(M_total)
        history["binary_sep"].append(sep)
        history["circ_pol"].append(f_circ)
        history["Lz"].append(Lz)
        history["alpha_min"].append(min_lapse)

        elapsed = time.time() - start_time
        eta = elapsed / (step + 1) * (n_steps - step - 1) if step > 0 else 0
        print(f"[{step:06d}/{n_steps}] t={t:.3f} | "
              f"rho={c_rho:.3e} | alpha_c={c_lapse:.4f} | alpha_min={min_lapse:.4f} | "
              f"K5={k5_max:.3f} | d={sep:.2f} | "
              f"h+={h_plus:.3e} | hx={h_cross:.3e} | "
              f"fc={f_circ:.3f} | Lz={Lz:.2f} | "
              f"M={M_total:.2f} | ETA {eta/60:.0f}m")

        if torch.isnan(phi[mid, mid, mid]):
            print("\n[!] NaN DETECTED — halting.")
            break

    if step > 0 and step % checkpoint_every == 0:
        with open(f"UHF_results/phase12v6_history_step{step}.json", "w") as f:
            json.dump(history, f)
        print(f"    [CHECKPOINT] Saved history at step {step}")

# ====================== FINAL SAVE ======================
elapsed = time.time() - start_time
print(f"\n{'='*70}")
print(f"  PHASE 12 v6 COMPLETE: {step+1} steps in {elapsed:.1f}s ({elapsed/3600:.2f}h)")
print(f"{'='*70}")

with open("UHF_results/phase12v6_history_final.json", "w") as f:
    json.dump(history, f)

for name, key in [("h_plus","h_plus"),("h_cross","h_cross"),("polarization","circ_pol"),
                  ("separation","binary_sep"),("lapse","central_lapse"),("alpha_min","alpha_min"),
                  ("total_mass","total_mass")]:
    with open(f"UHF_results/phase12v6_{name}.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["t", key])
        for ti, val in zip(history["t"], history[key]): w.writerow([ti, val])

final_lapse = float(alpha[mid, mid, mid])
final_k5 = float(torch.max(torch.abs(K5)))
max_circ_pol = max(history["circ_pol"]) if history["circ_pol"] else 0
final_sep = history["binary_sep"][-1] if history["binary_sep"] else d_sep * 2
min_sep = min(history["binary_sep"]) if history["binary_sep"] else d_sep * 2
min_lapse_ever = min(history["central_lapse"]) if history["central_lapse"] else 1.0
min_alpha_global = min(history["alpha_min"]) if history["alpha_min"] else 1.0

n_late = max(1, len(history["circ_pol"]) // 5)
late_fc = history["circ_pol"][-n_late:]
late_fc_mean = np.mean(late_fc) if late_fc else 0
late_fc_max = max(late_fc) if late_fc else 0

lapse_dynamic = min_alpha_global > 0.05
merged = min_sep < 2.0

print(f"\n  Final central lapse      = {final_lapse:.6f}")
print(f"  Min central lapse (ever) = {min_lapse_ever:.6f}")
print(f"  Min global lapse (ever)  = {min_alpha_global:.6f}")
print(f"  Lapse stayed dynamic     = {lapse_dynamic}")
print(f"  Final torsion max|K5|    = {final_k5:.6f}")
print(f"  Final binary separation  = {final_sep:.3f}")
print(f"  Min binary separation    = {min_sep:.3f}")
print(f"  Max circular pol frac    = {max_circ_pol:.4f}")
print(f"  Late ringdown fc (mean)  = {late_fc_mean:.4f}")
print(f"  Late ringdown fc (max)   = {late_fc_max:.4f}")
print(f"  Final total mass         = {history['total_mass'][-1]:.4f}")
print(f"  Merger occurred (d<2)    = {merged}")
print(f"\n  CSV saved: phase12v6_*.csv")

# ====================== PLOTTING ======================
cpu = {k: np.array(v) for k, v in history.items()}

plt.style.use('dark_background')
fig, axes = plt.subplots(3, 3, figsize=(20, 16))

axes[0, 0].imshow(torch.abs(phi[:, :, mid]).cpu().detach().numpy(),
                   cmap='inferno', origin='lower', extent=[-L/2, L/2, -L/2, L/2])
axes[0, 0].set_title(f'|phi| orbital plane (z=0) t={t:.1f}')
axes[0, 0].set_xlabel('x'); axes[0, 0].set_ylabel('y')

im = axes[0, 1].imshow(K5[:, :, mid].cpu().detach().numpy(),
                         cmap='coolwarm', origin='lower', extent=[-L/2, L/2, -L/2, L/2])
axes[0, 1].set_title('Torsion K5 (z=0)')
plt.colorbar(im, ax=axes[0, 1])

axes[0, 2].imshow(alpha[:, :, mid].cpu().detach().numpy(),
                   cmap='viridis', origin='lower', extent=[-L/2, L/2, -L/2, L/2])
axes[0, 2].set_title(f'Lapse alpha (z=0) t={t:.1f}')
axes[0, 2].set_xlabel('x'); axes[0, 2].set_ylabel('y')

axes[1, 0].plot(cpu["t"], cpu["binary_sep"], color='cyan', lw=1.5)
axes[1, 0].axhline(y=2.0, color='red', ls='--', alpha=0.5, label='Merger threshold')
axes[1, 0].set_xlabel('Time'); axes[1, 0].set_ylabel('Separation')
axes[1, 0].set_title('Binary Separation')
axes[1, 0].legend(); axes[1, 0].grid(True, alpha=0.2)

axes[1, 1].plot(cpu["t"], cpu["central_lapse"], color='magenta', lw=1.5, label='Central')
axes[1, 1].plot(cpu["t"], cpu["alpha_min"], color='cyan', lw=1, alpha=0.7, label='Global min')
axes[1, 1].axhline(y=0.10, color='red', ls='--', alpha=0.5, label='Target floor')
axes[1, 1].set_xlabel('Time'); axes[1, 1].set_ylabel('Lapse alpha')
axes[1, 1].set_title('Dynamic Lapse Evolution')
axes[1, 1].legend(); axes[1, 1].grid(True, alpha=0.2)

axes[1, 2].plot(cpu["t"], cpu["total_mass"], color='gold', lw=1.5)
axes[1, 2].set_xlabel('Time'); axes[1, 2].set_ylabel('Total Mass')
axes[1, 2].set_title('Total Mass')
axes[1, 2].grid(True, alpha=0.2)

axes[2, 0].plot(cpu["t"], cpu["h_plus"], color='lime', lw=1, label='h+ (linear)')
axes[2, 0].plot(cpu["t"], cpu["h_cross"], color='orange', lw=1, label='hx (chiral)')
axes[2, 0].set_xlabel('Time'); axes[2, 0].set_ylabel('GW Strain Proxy')
axes[2, 0].set_title('Gravitational Wave Polarizations')
axes[2, 0].legend(); axes[2, 0].grid(True, alpha=0.2)

axes[2, 1].plot(cpu["t"], cpu["circ_pol"], color='red', lw=1.5)
axes[2, 1].axhline(y=0.5, color='yellow', ls='--', alpha=0.3, label='50% circular')
axes[2, 1].set_xlabel('Time'); axes[2, 1].set_ylabel('f_circ = |hx|/(|h+|+|hx|)')
axes[2, 1].set_title('Circular Polarization Fraction')
axes[2, 1].set_ylim(-0.05, 1.05)
axes[2, 1].legend(); axes[2, 1].grid(True, alpha=0.2)

axes[2, 2].plot(cpu["t"], cpu["Lz"], color='white', lw=1.5)
axes[2, 2].set_xlabel('Time'); axes[2, 2].set_ylabel('L_z')
axes[2, 2].set_title('Orbital Angular Momentum')
axes[2, 2].grid(True, alpha=0.2)

plt.suptitle(f'UHF Phase 12 v6: True Plunge (G={G_grav}, lam={lam}, d={d_sep}, v_rad={v_radial})',
             fontsize=14, y=1.01)
plt.tight_layout()
plt.savefig("UHF_results/phase12v6_merger_final.png", dpi=300, bbox_inches='tight')
print(f"Plot saved to UHF_results/phase12v6_merger_final.png")
