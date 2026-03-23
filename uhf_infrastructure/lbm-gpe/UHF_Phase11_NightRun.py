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
dx = L / N
dt = 0.003                    # Conservative fixed CFL for long-run stability
G_grav = 1.0
m_scalar = 0.1
lam = 200.0                   # GP repulsive self-interaction
kappa_torsion = 0.5            # Torsion coupling strength (reduced to prevent runaway)
m_torsion_sq = 1.0             # Torsion mass (range control)
z4c_damping = 0.05             # Z4c-style constraint damping on pi_K5
lam_K5_cubic = 0.5             # Cubic torsion self-interaction (prevents runaway)
K5_sat = 2.0                   # Smooth tanh saturation scale for K5
KO_sigma = 0.02                # Kreiss-Oliger dissipation coefficient
n_steps = 50000
log_every = 500
checkpoint_every = 5000
GW_extraction_radius = 12.0   # Radius for quadrupole GW extraction

# ====================== GRID ======================
x = torch.linspace(-L/2 + dx/2, L/2 - dx/2, N, device=device)
X, Y, Z = torch.meshgrid(x, x, x, indexing='ij')
R2 = X**2 + Y**2 + Z**2
R_grid = torch.sqrt(R2).clamp(min=dx)

# Laplacian stencil
LAP_KERNEL = torch.tensor([[[[0,0,0],[0,-1,0],[0,0,0]],
                             [[0,-1,0],[-1,6,-1],[0,-1,0]],
                             [[0,0,0],[0,-1,0],[0,0,0]]]],
                          dtype=torch.float32, device=device).unsqueeze(0) / (dx**2)

# Precompute FFT k^2 grid
kx = torch.fft.fftfreq(N, d=dx, device=device) * 2 * torch.pi
KX_f, KY_f, KZ_f = torch.meshgrid(kx, kx, kx, indexing='ij')
K2_fft = KX_f**2 + KY_f**2 + KZ_f**2
K2_fft[0, 0, 0] = 1.0  # avoid div/0

# Absorbing sponge layer
sponge_start = 0.7 * L / 2
sponge = torch.ones(N, N, N, device=device)
mask = R_grid > sponge_start
sponge[mask] = torch.exp(-3.0 * ((R_grid[mask] - sponge_start) / (L/2 - sponge_start))**2)
sponge_c = sponge.to(torch.complex64)

# GW extraction shell mask (thin shell at extraction radius)
gw_shell = (torch.abs(R_grid - GW_extraction_radius) < 1.5 * dx).float()
gw_shell_norm = gw_shell.sum() * dx**3
# Quadrupole tensor weight functions (STF components)
Qxx_w = (3 * X**2 - R2) * gw_shell  # I_xx - 1/3 delta_xx I
Qxy_w = 3 * X * Y * gw_shell
Qxz_w = 3 * X * Z * gw_shell

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
    """Compute K * (grad f) summed over directions — scalar K times grad of complex f."""
    gx = (torch.roll(f, -1, 0) - torch.roll(f, 1, 0)) / (2 * dx)
    gy = (torch.roll(f, -1, 1) - torch.roll(f, 1, 1)) / (2 * dx)
    gz = (torch.roll(f, -1, 2) - torch.roll(f, 1, 2)) / (2 * dx)
    # K is scalar (axial), couples uniformly to all gradient components
    return K * (gx + gy + gz)

def kreiss_oliger_diss(f):
    """4th-order Kreiss-Oliger dissipation to damp grid-scale modes."""
    d = torch.zeros_like(f)
    for dim in range(3):
        fp2 = torch.roll(f, -2, dim)
        fp1 = torch.roll(f, -1, dim)
        fm1 = torch.roll(f, 1, dim)
        fm2 = torch.roll(f, 2, dim)
        d += (fp2 - 4*fp1 + 6*f - 4*fm1 + fm2)  # 4th-order stencil
    return -KO_sigma / (16.0 * dt) * d

def poisson_fft(rho):
    rho_hat = torch.fft.fftn(rho)
    V_hat = -4.0 * torch.pi * G_grav * rho_hat / K2_fft
    V_hat[0, 0, 0] = 0.0
    return torch.fft.ifftn(V_hat).real

# ====================== INITIAL CONDITIONS ======================
sigma = 5.0
amp = 0.15

phi = (amp * torch.exp(-R2 / (2 * sigma**2))).to(torch.complex64)
pi_field = torch.zeros_like(phi)

# Torsion: vortex seed with angular structure
K5 = kappa_torsion * torch.exp(-R2 / (2 * (sigma * 1.5)**2)) * (X * Y) / (R2 + 1.0)
pi_K5 = torch.zeros_like(K5)

# ====================== EVOLUTION ======================
def compute_rhs(phi_s, pi_s, K5_s, pi_K5_s):
    lap_phi = laplacian_complex(phi_s)

    # Gravitational source: kinetic + mass (GP pressure doesn't gravitate)
    rho_grav = 0.5 * (torch.abs(pi_s)**2 + gradient_sq_magnitude(phi_s) +
                      m_scalar**2 * torch.abs(phi_s)**2)
    rho_torsion = 0.5 * pi_K5_s**2 + 0.5 * gradient_sq_real(K5_s)
    rho_total = rho_grav + 0.1 * rho_torsion

    V = poisson_fft(rho_total)
    alpha = torch.sqrt(torch.clamp(1.0 + 2.0 * V, min=0.01))

    # Torsion-covariant Laplacian: D^2 phi = nabla^2 phi + 2i K5 (nabla phi) - K5^2 phi
    torsion_grad = gradient_dot_complex(K5_s, phi_s)
    D2_phi = lap_phi + 2j * torsion_grad - K5_s**2 * phi_s

    # Scalar field: GP repulsion + full torsion-covariant Laplacian
    dphi_dt = alpha * pi_s
    dpi_dt = alpha * (D2_phi - m_scalar**2 * phi_s
                      - lam * torch.abs(phi_s)**2 * phi_s)

    # Kreiss-Oliger dissipation on scalar momentum
    dpi_dt = dpi_dt + kreiss_oliger_diss(pi_s)

    # Torsion field: wave eq + axial current source + Z4c damping
    lap_K5 = laplacian_3d(K5_s)
    J5 = torch.imag(torch.conj(phi_s) * pi_s)
    dK5_dt = alpha * pi_K5_s
    # Amplitude-dependent Z4c damping: stronger when K5 is large
    nonlin_damp = z4c_damping * (1.0 + (K5_s / K5_sat)**2)
    dpi_K5_dt = alpha * (lap_K5 - m_torsion_sq * K5_s
                        - lam_K5_cubic * K5_s**3
                        + kappa_torsion * J5) - nonlin_damp * pi_K5_s
    dpi_K5_dt = dpi_K5_dt + kreiss_oliger_diss(pi_K5_s)

    return dphi_dt, dpi_dt, dK5_dt, dpi_K5_dt, V, alpha, rho_total

def extract_gw_strain(rho):
    """Quadrupole GW strain proxy: h+ ~ d^2 Q_ij / dt^2 at extraction radius."""
    h_plus = float(torch.sum(Qxx_w * rho) * dx**3)
    h_cross = float(torch.sum(Qxy_w * rho) * dx**3)
    return h_plus, h_cross

# ====================== MAIN LOOP ======================
mid = N // 2
t = 0.0

# History arrays
history = {"t": [], "central_rho": [], "central_lapse": [],
           "K5_max": [], "h_plus": [], "h_cross": [], "total_mass": []}

os.makedirs("UHF_results", exist_ok=True)
start_time = time.time()

print(f"\n{'='*70}")
print(f"  UHF PHASE 11: TORSION-COUPLED 3D EKG NIGHT RUN")
print(f"  Grid: {N}^3 = {N**3:,} cells | dt={dt} | {n_steps:,} steps")
print(f"  Physics: G={G_grav}, m={m_scalar}, lam={lam}, kappa={kappa_torsion}")
print(f"  Z4c damping: {z4c_damping} | GW extraction r={GW_extraction_radius}")
print(f"  K5 initial max: {float(K5.max()):.4f}")
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

    # Smooth tanh saturation on K5 (no gradient discontinuity)
    K5 = K5_sat * torch.tanh(K5 / K5_sat)
    t += dt

    # --- Logging ---
    if step % log_every == 0:
        c_rho = float(torch.abs(phi[mid, mid, mid])**2)
        c_lapse = float(alpha[mid, mid, mid])
        k5_max = float(torch.max(torch.abs(K5)))
        h_plus, h_cross = extract_gw_strain(rho)
        M_total = float(torch.sum(rho) * dx**3)

        history["t"].append(t)
        history["central_rho"].append(c_rho)
        history["central_lapse"].append(c_lapse)
        history["K5_max"].append(k5_max)
        history["h_plus"].append(h_plus)
        history["h_cross"].append(h_cross)
        history["total_mass"].append(M_total)

        elapsed = time.time() - start_time
        eta = elapsed / (step + 1) * (n_steps - step - 1) if step > 0 else 0
        print(f"[{step:06d}/{n_steps}] t={t:.3f} | "
              f"rho={c_rho:.3e} | alpha={c_lapse:.4f} | "
              f"K5={k5_max:.3f} | h+={h_plus:.3e} | "
              f"M={M_total:.2f} | ETA {eta/60:.0f}m")

        if torch.isnan(phi[mid, mid, mid]):
            print("\n[!] NaN DETECTED — halting.")
            break

    # --- Checkpoint ---
    if step > 0 and step % checkpoint_every == 0:
        with open(f"UHF_results/phase11_history_step{step}.json", "w") as f:
            json.dump(history, f)
        print(f"    [CHECKPOINT] Saved history at step {step}")

# ====================== FINAL SAVE ======================
elapsed = time.time() - start_time
print(f"\n{'='*70}")
print(f"  NIGHT RUN COMPLETE: {step+1} steps in {elapsed:.1f}s ({elapsed/3600:.2f}h)")
print(f"{'='*70}")

with open("UHF_results/phase11_history_final.json", "w") as f:
    json.dump(history, f)

# ====================== PLOTTING ======================
cpu = {k: np.array(v) for k, v in history.items()}

plt.style.use('dark_background')
fig, axes = plt.subplots(3, 2, figsize=(16, 14))

# (0,0) |phi| slice
axes[0, 0].imshow(torch.abs(phi[:, mid, :]).cpu().detach().numpy(),
                   cmap='inferno', origin='lower', extent=[-L/2, L/2, -L/2, L/2])
axes[0, 0].set_title(f'|phi| slice (y=0) t={t:.1f}')
axes[0, 0].set_xlabel('x'); axes[0, 0].set_ylabel('z')

# (0,1) K5 slice
im = axes[0, 1].imshow(K5[:, mid, :].cpu().detach().numpy(),
                         cmap='coolwarm', origin='lower', extent=[-L/2, L/2, -L/2, L/2])
axes[0, 1].set_title('Torsion K5 slice (y=0)')
plt.colorbar(im, ax=axes[0, 1])

# (1,0) Central density
axes[1, 0].plot(cpu["t"], cpu["central_rho"], color='cyan', lw=1.5)
axes[1, 0].set_xlabel('Time'); axes[1, 0].set_ylabel('Central |phi|^2')
axes[1, 0].set_title('Central Density Evolution')
axes[1, 0].set_yscale('log')
axes[1, 0].grid(True, alpha=0.2)

# (1,1) Lapse
axes[1, 1].plot(cpu["t"], cpu["central_lapse"], color='magenta', lw=1.5)
axes[1, 1].axhline(y=0.05, color='red', ls='--', alpha=0.5, label='Horizon')
axes[1, 1].set_xlabel('Time'); axes[1, 1].set_ylabel('Lapse alpha')
axes[1, 1].set_title('Lapse (Gravitational Time Dilation)')
axes[1, 1].legend(); axes[1, 1].grid(True, alpha=0.2)

# (2,0) K5 max
axes[2, 0].plot(cpu["t"], cpu["K5_max"], color='gold', lw=1.5)
axes[2, 0].set_xlabel('Time'); axes[2, 0].set_ylabel('max|K5|')
axes[2, 0].set_title('Torsion Field Amplitude')
axes[2, 0].grid(True, alpha=0.2)

# (2,1) GW strain
axes[2, 1].plot(cpu["t"], cpu["h_plus"], color='lime', lw=1, label='h+')
axes[2, 1].plot(cpu["t"], cpu["h_cross"], color='orange', lw=1, label='hx', alpha=0.7)
axes[2, 1].set_xlabel('Time'); axes[2, 1].set_ylabel('GW Strain Proxy')
axes[2, 1].set_title('Gravitational Wave Extraction (Quadrupole)')
axes[2, 1].legend(); axes[2, 1].grid(True, alpha=0.2)

plt.suptitle('UHF Phase 11: Torsion-Coupled EKG Night Run', fontsize=15, y=1.01)
plt.tight_layout()
plt.savefig("UHF_results/phase11_night_run_final.png", dpi=300)
print("Plot saved to UHF_results/phase11_night_run_final.png")

# Final verdict
fl = cpu["central_lapse"][-1]
fk = cpu["K5_max"][-1]
print(f"\n{'='*60}")
print(f"  Final central lapse      = {fl:.6f}")
print(f"  Final torsion max|K5|    = {fk:.6f}")
print(f"  Apparent horizon formed  = {fl < 0.05}")
print(f"  Torsion field survived   = {fk > 0.001}")
print(f"  Total simulation time    = {t:.2f}")
print(f"  Wall clock               = {elapsed:.1f}s ({elapsed/3600:.2f}h)")
print(f"{'='*60}")
