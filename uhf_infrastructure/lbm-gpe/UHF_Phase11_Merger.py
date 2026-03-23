#!/usr/bin/env python3
"""UHF Phase 11 Merger: Binary Torsion-Coupled 3D EKG
Two scalar cores with orbital momentum, torsion (K5) coupling,
FFT Poisson gravity, GW quadrupole extraction with polarization analysis.
Built on proven Phase 11 NightRun infrastructure.
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

# ====================== PARAMETERS ======================
N = 64
L = 40.0
dx = L / N          # 0.625
dt = 0.003          # Proven CFL from Phase 11
G_grav = 1.0
m_scalar = 0.1
lam = 200.0         # GP repulsive self-interaction
kappa_torsion = 0.8  # Torsion coupling (stronger for merger dynamics)
m_torsion_sq = 1.0
z4c_damping = 0.04   # Slightly less than Phase 11 — let K5 survive into ringdown
lam_K5_cubic = 0.3    # Cubic torsion self-interaction
K5_sat = 2.0          # Smooth tanh saturation
KO_sigma = 0.02       # Kreiss-Oliger dissipation
n_steps = 30000
log_every = 300
checkpoint_every = 5000
GW_extraction_radius = 15.0

# Binary IC parameters
d_sep = 7.0          # Half-separation: cores at (±d_sep, 0, 0)
sigma_core = 2.5     # Core Gaussian width
amp_core = 0.12      # Core amplitude
v_orbit = 0.5        # Orbital tangential velocity
k_boost = m_scalar * v_orbit  # Phase gradient for Galilean boost

# ====================== GRID ======================
x = torch.linspace(-L/2 + dx/2, L/2 - dx/2, N, device=device)
X, Y, Z = torch.meshgrid(x, x, x, indexing='ij')
R2 = X**2 + Y**2 + Z**2
R_grid = torch.sqrt(R2).clamp(min=dx)

# Laplacian stencil (7-point, 3D)
LAP_KERNEL = torch.tensor([[[[0,0,0],[0,-1,0],[0,0,0]],
                             [[0,-1,0],[-1,6,-1],[0,-1,0]],
                             [[0,0,0],[0,-1,0],[0,0,0]]]],
                          dtype=torch.float32, device=device).unsqueeze(0) / (dx**2)

# Precompute FFT k^2 grid for Poisson solver
kf = torch.fft.fftfreq(N, d=dx, device=device) * 2 * torch.pi
KX_f, KY_f, KZ_f = torch.meshgrid(kf, kf, kf, indexing='ij')
K2_fft = KX_f**2 + KY_f**2 + KZ_f**2
K2_fft[0, 0, 0] = 1.0  # avoid div/0

# Absorbing sponge layer
sponge_start = 0.7 * L / 2
sponge = torch.ones(N, N, N, device=device)
mask_s = R_grid > sponge_start
sponge[mask_s] = torch.exp(-3.0 * ((R_grid[mask_s] - sponge_start) / (L/2 - sponge_start))**2)
sponge_c = sponge.to(torch.complex64)

# GW extraction shell mask (thin shell at extraction radius)
gw_shell = (torch.abs(R_grid - GW_extraction_radius) < 1.5 * dx).float()
# Quadrupole tensor weight functions (STF components)
Qxx_w = (3 * X**2 - R2) * gw_shell   # h+ polarization
Qxy_w = 3 * X * Y * gw_shell          # h× polarization (chiral component)

# ====================== HELPERS ======================
def laplacian_3d(f):
    return F.conv3d(f.unsqueeze(0).unsqueeze(0), LAP_KERNEL, padding=1)[0, 0]

def laplacian_complex(f):
    return laplacian_3d(f.real) + 1j * laplacian_3d(f.imag)

def gradient_sq_magnitude(f):
    gx = (torch.roll(f, -1, 0) - torch.roll(f, 1, 0)) / (2 * dx)
    gy = (torch.roll(f, -1, 1) - torch.roll(f, 1, 1)) / (2 * dx)
    gz = (torch.roll(f, -1, 2) - torch.roll(f, 1, 2)) / (2 * dx)
    return torch.abs(gx)**2 + torch.abs(gy)**2 + torch.abs(gz)**2

def gradient_sq_real(f):
    gx = (torch.roll(f, -1, 0) - torch.roll(f, 1, 0)) / (2 * dx)
    gy = (torch.roll(f, -1, 1) - torch.roll(f, 1, 1)) / (2 * dx)
    gz = (torch.roll(f, -1, 2) - torch.roll(f, 1, 2)) / (2 * dx)
    return gx**2 + gy**2 + gz**2

def gradient_dot_complex(K, f):
    """K * (grad f) summed over directions — torsion gradient coupling."""
    gx = (torch.roll(f, -1, 0) - torch.roll(f, 1, 0)) / (2 * dx)
    gy = (torch.roll(f, -1, 1) - torch.roll(f, 1, 1)) / (2 * dx)
    gz = (torch.roll(f, -1, 2) - torch.roll(f, 1, 2)) / (2 * dx)
    return K * (gx + gy + gz)

def kreiss_oliger_diss(f):
    """4th-order Kreiss-Oliger dissipation to damp grid-scale modes."""
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
    """Quadrupole GW strain proxy at extraction radius."""
    h_plus = float(torch.sum(Qxx_w * rho) * dx**3)
    h_cross = float(torch.sum(Qxy_w * rho) * dx**3)
    return h_plus, h_cross

def compute_binary_separation(phi_abs_sq):
    """Center-of-mass separation between left (x<0) and right (x>0) halves."""
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
    """Orbital angular momentum L_z = int Im(phi* (x d_y phi - y d_x phi)) dV."""
    grad_x = (torch.roll(phi_f, -1, 0) - torch.roll(phi_f, 1, 0)) / (2 * dx_val)
    grad_y = (torch.roll(phi_f, -1, 1) - torch.roll(phi_f, 1, 1)) / (2 * dx_val)
    Lz_density = torch.imag(torch.conj(phi_f) * (X * grad_y - Y * grad_x))
    return float(Lz_density.sum() * dx_val**3)

# ====================== INITIAL CONDITIONS ======================
# Core 1 at (+d_sep, 0, 0) with tangential boost in +y
R1_sq = (X - d_sep)**2 + Y**2 + Z**2
phi1 = amp_core * torch.exp(-R1_sq / (2 * sigma_core**2))

# Core 2 at (-d_sep, 0, 0) with tangential boost in -y
R2_sq = (X + d_sep)**2 + Y**2 + Z**2
phi2 = amp_core * torch.exp(-R2_sq / (2 * sigma_core**2))

# Apply orbital boosts via phase gradients (Galilean boost)
phi = (phi1 * torch.exp(1j * k_boost * Y) +
       phi2 * torch.exp(-1j * k_boost * Y)).to(torch.complex64)
pi_field = torch.zeros_like(phi)

# Torsion: vortex seeds at each core location
K5_1 = kappa_torsion * torch.exp(-R1_sq / (2*(sigma_core*1.5)**2)) * (X - d_sep) * Y / (R1_sq + 1.0)
K5_2 = kappa_torsion * torch.exp(-R2_sq / (2*(sigma_core*1.5)**2)) * (X + d_sep) * Y / (R2_sq + 1.0)
K5 = K5_1 + K5_2
pi_K5 = torch.zeros_like(K5)

# ====================== EVOLUTION ======================
def compute_rhs(phi_s, pi_s, K5_s, pi_K5_s):
    lap_phi = laplacian_complex(phi_s)

    # Gravitational source: kinetic + gradient + mass (GP doesn't gravitate)
    rho_grav = 0.5 * (torch.abs(pi_s)**2 + gradient_sq_magnitude(phi_s) +
                      m_scalar**2 * torch.abs(phi_s)**2)
    rho_torsion = 0.5 * pi_K5_s**2 + 0.5 * gradient_sq_real(K5_s)
    rho_total = rho_grav + 0.1 * rho_torsion

    V = poisson_fft(rho_total)
    alpha = torch.sqrt(torch.clamp(1.0 + 2.0 * V, min=0.01))

    # Torsion-covariant Laplacian: D^2 phi = lap + 2i K5 (grad phi) - K5^2 phi
    torsion_grad = gradient_dot_complex(K5_s, phi_s)
    D2_phi = lap_phi + 2j * torsion_grad - K5_s**2 * phi_s

    # Scalar field evolution
    dphi_dt = alpha * pi_s
    dpi_dt = alpha * (D2_phi - m_scalar**2 * phi_s
                      - lam * torch.abs(phi_s)**2 * phi_s)
    dpi_dt = dpi_dt + kreiss_oliger_diss(pi_s)

    # Torsion field evolution
    lap_K5 = laplacian_3d(K5_s)
    J5 = torch.imag(torch.conj(phi_s) * pi_s)
    dK5_dt = alpha * pi_K5_s
    nonlin_damp = z4c_damping * (1.0 + (K5_s / K5_sat)**2)
    dpi_K5_dt = alpha * (lap_K5 - m_torsion_sq * K5_s
                        - lam_K5_cubic * K5_s**3
                        + kappa_torsion * J5) - nonlin_damp * pi_K5_s
    dpi_K5_dt = dpi_K5_dt + kreiss_oliger_diss(pi_K5_s)

    return dphi_dt, dpi_dt, dK5_dt, dpi_K5_dt, V, alpha, rho_total

# ====================== MAIN LOOP ======================
mid = N // 2
t = 0.0

history = {"t": [], "central_rho": [], "central_lapse": [],
           "K5_max": [], "h_plus": [], "h_cross": [], "total_mass": [],
           "binary_sep": [], "circ_pol": [], "Lz": []}

os.makedirs("UHF_results", exist_ok=True)
start_time = time.time()

init_sep = compute_binary_separation(torch.abs(phi)**2)
init_Lz = compute_Lz(phi, dx)

print(f"\n{'='*70}")
print(f"  UHF PHASE 11 MERGER: BINARY TORSION-COUPLED 3D EKG")
print(f"  Grid: {N}^3 = {N**3:,} cells | dt={dt} | {n_steps:,} steps")
print(f"  Binary: d_sep={d_sep}, sigma={sigma_core}, v_orbit={v_orbit}")
print(f"  Physics: G={G_grav}, m={m_scalar}, lam={lam}, kappa={kappa_torsion}")
print(f"  Z4c damping: {z4c_damping} | GW extraction r={GW_extraction_radius}")
print(f"  K5 initial max: {float(K5.abs().max()):.4f}")
print(f"  Initial separation: {init_sep:.2f}")
print(f"  Initial Lz: {init_Lz:.4f}")
print(f"{'='*70}\n")

for step in range(n_steps):
    # --- RK2 Midpoint ---
    d1 = compute_rhs(phi, pi_field, K5, pi_K5)
    d1_phi, d1_pi, d1_K5, d1_piK5 = d1[:4]

    phi_h = phi + 0.5 * dt * d1_phi
    pi_h = pi_field + 0.5 * dt * d1_pi
    K5_h = K5 + 0.5 * dt * d1_K5
    piK5_h = pi_K5 + 0.5 * dt * d1_piK5

    d2 = compute_rhs(phi_h, pi_h, K5_h, piK5_h)
    d2_phi, d2_pi, d2_K5, d2_piK5, V, alpha, rho = d2

    phi = phi + dt * d2_phi
    pi_field = pi_field + dt * d2_pi
    K5 = K5 + dt * d2_K5
    pi_K5 = pi_K5 + dt * d2_piK5

    # Sponge damping
    phi *= sponge_c
    pi_field *= sponge_c
    K5 *= sponge
    pi_K5 *= sponge

    # Smooth tanh saturation on K5
    K5 = K5_sat * torch.tanh(K5 / K5_sat)
    t += dt

    # --- Logging ---
    if step % log_every == 0:
        c_rho = float(torch.abs(phi[mid, mid, mid])**2)
        c_lapse = float(alpha[mid, mid, mid])
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

        elapsed = time.time() - start_time
        eta = elapsed / (step + 1) * (n_steps - step - 1) if step > 0 else 0
        print(f"[{step:06d}/{n_steps}] t={t:.3f} | "
              f"rho={c_rho:.3e} | alpha={c_lapse:.4f} | "
              f"K5={k5_max:.3f} | d={sep:.2f} | "
              f"h+={h_plus:.3e} | hx={h_cross:.3e} | "
              f"fc={f_circ:.3f} | Lz={Lz:.2f} | "
              f"M={M_total:.1f} | ETA {eta/60:.0f}m")

        if torch.isnan(phi[mid, mid, mid]):
            print("\n[!] NaN DETECTED — halting.")
            break

    # --- Checkpoint ---
    if step > 0 and step % checkpoint_every == 0:
        with open(f"UHF_results/merger_history_step{step}.json", "w") as f:
            json.dump(history, f)
        print(f"    [CHECKPOINT] Saved history at step {step}")

# ====================== FINAL SAVE ======================
elapsed = time.time() - start_time
print(f"\n{'='*70}")
print(f"  MERGER RUN COMPLETE: {step+1} steps in {elapsed:.1f}s ({elapsed/3600:.2f}h)")
print(f"{'='*70}")

with open("UHF_results/merger_history_final.json", "w") as f:
    json.dump(history, f)

# ====================== FINAL REPORT ======================
final_lapse = float(alpha[mid, mid, mid])
final_k5 = float(torch.max(torch.abs(K5)))
max_circ_pol = max(history["circ_pol"]) if history["circ_pol"] else 0
final_sep = history["binary_sep"][-1] if history["binary_sep"] else d_sep * 2
horizon_formed = final_lapse < 0.02

print(f"\n  Final central lapse      = {final_lapse:.6f}")
print(f"  Final torsion max|K5|    = {final_k5:.6f}")
print(f"  Final binary separation  = {final_sep:.3f}")
print(f"  Max circular pol frac    = {max_circ_pol:.4f}")
print(f"  Horizon formed           = {horizon_formed}")
print(f"  Merger occurred          = {final_sep < 2.0}")

# ====================== PLOTTING ======================
cpu = {k: np.array(v) for k, v in history.items()}

plt.style.use('dark_background')
fig, axes = plt.subplots(3, 3, figsize=(20, 16))

# (0,0) |phi| orbital plane (xy at z=0)
axes[0, 0].imshow(torch.abs(phi[:, :, mid]).cpu().detach().numpy(),
                   cmap='inferno', origin='lower', extent=[-L/2, L/2, -L/2, L/2])
axes[0, 0].set_title(f'|phi| orbital plane (z=0) t={t:.1f}')
axes[0, 0].set_xlabel('x'); axes[0, 0].set_ylabel('y')

# (0,1) K5 orbital plane
im = axes[0, 1].imshow(K5[:, :, mid].cpu().detach().numpy(),
                         cmap='coolwarm', origin='lower', extent=[-L/2, L/2, -L/2, L/2])
axes[0, 1].set_title('Torsion K5 (z=0)')
plt.colorbar(im, ax=axes[0, 1])

# (0,2) |phi| meridional plane (xz at y=0)
axes[0, 2].imshow(torch.abs(phi[:, mid, :]).cpu().detach().numpy(),
                   cmap='inferno', origin='lower', extent=[-L/2, L/2, -L/2, L/2])
axes[0, 2].set_title(f'|phi| meridional (y=0) t={t:.1f}')
axes[0, 2].set_xlabel('x'); axes[0, 2].set_ylabel('z')

# (1,0) Binary separation
axes[1, 0].plot(cpu["t"], cpu["binary_sep"], color='cyan', lw=1.5)
axes[1, 0].axhline(y=2.0, color='red', ls='--', alpha=0.5, label='Merger threshold')
axes[1, 0].set_xlabel('Time'); axes[1, 0].set_ylabel('Separation')
axes[1, 0].set_title('Binary Separation')
axes[1, 0].legend(); axes[1, 0].grid(True, alpha=0.2)

# (1,1) Central lapse
axes[1, 1].plot(cpu["t"], cpu["central_lapse"], color='magenta', lw=1.5)
axes[1, 1].axhline(y=0.05, color='red', ls='--', alpha=0.5, label='Horizon')
axes[1, 1].set_xlabel('Time'); axes[1, 1].set_ylabel('Lapse alpha')
axes[1, 1].set_title('Central Lapse (Merger Point)')
axes[1, 1].legend(); axes[1, 1].grid(True, alpha=0.2)

# (1,2) K5 max
axes[1, 2].plot(cpu["t"], cpu["K5_max"], color='gold', lw=1.5)
axes[1, 2].set_xlabel('Time'); axes[1, 2].set_ylabel('max|K5|')
axes[1, 2].set_title('Torsion Field Amplitude')
axes[1, 2].grid(True, alpha=0.2)

# (2,0) GW strain h+ and h×
axes[2, 0].plot(cpu["t"], cpu["h_plus"], color='lime', lw=1, label='h+ (linear)')
axes[2, 0].plot(cpu["t"], cpu["h_cross"], color='orange', lw=1, label='h× (chiral)')
axes[2, 0].set_xlabel('Time'); axes[2, 0].set_ylabel('GW Strain Proxy')
axes[2, 0].set_title('Gravitational Wave Polarizations')
axes[2, 0].legend(); axes[2, 0].grid(True, alpha=0.2)

# (2,1) Circular polarization fraction
axes[2, 1].plot(cpu["t"], cpu["circ_pol"], color='red', lw=1.5)
axes[2, 1].axhline(y=0.5, color='yellow', ls='--', alpha=0.3, label='50% circular')
axes[2, 1].set_xlabel('Time'); axes[2, 1].set_ylabel('f_circ = |h×|/(|h+|+|h×|)')
axes[2, 1].set_title('Circular Polarization Fraction (Chiral Signature)')
axes[2, 1].set_ylim(-0.05, 1.05)
axes[2, 1].legend(); axes[2, 1].grid(True, alpha=0.2)

# (2,2) Angular momentum
axes[2, 2].plot(cpu["t"], cpu["Lz"], color='white', lw=1.5)
axes[2, 2].set_xlabel('Time'); axes[2, 2].set_ylabel('L_z')
axes[2, 2].set_title('Orbital Angular Momentum')
axes[2, 2].grid(True, alpha=0.2)

plt.suptitle('UHF Phase 11 Merger: Binary Torsion-Coupled EKG', fontsize=15, y=1.01)
plt.tight_layout()
plt.savefig("UHF_results/phase11_merger_final.png", dpi=300, bbox_inches='tight')
print(f"\nPlot saved to UHF_results/phase11_merger_final.png")
