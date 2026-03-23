import torch
import torch.nn.functional as F
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import time
import os

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Device: {device} ({torch.cuda.get_device_name()})")

# ====================== PARAMETERS ======================
N = 64
L = 40.0
dx = L / N
dt = 0.005                   # Fixed CFL (stable for wave eq + GP + torsion)
G_grav = 1.0
m_scalar = 0.1               # Boson mass
lam = 200.0                  # GP repulsive self-interaction
kappa_torsion = 2.0           # Torsion coupling strength

# ====================== GRID ======================
x = torch.linspace(-L/2 + dx/2, L/2 - dx/2, N, device=device)
X, Y, Z = torch.meshgrid(x, x, x, indexing='ij')
R2 = X**2 + Y**2 + Z**2
R_grid = torch.sqrt(R2).clamp(min=dx)

# Laplacian stencil (scaled by 1/dx^2)
LAP_KERNEL = torch.tensor([[[[0,0,0],[0,-1,0],[0,0,0]],
                             [[0,-1,0],[-1,6,-1],[0,-1,0]],
                             [[0,0,0],[0,-1,0],[0,0,0]]]],
                          dtype=torch.float32, device=device).unsqueeze(0) / (dx**2)

# ====================== HELPERS ======================
def laplacian_3d(field_real):
    return F.conv3d(field_real.unsqueeze(0).unsqueeze(0), LAP_KERNEL, padding=1)[0, 0]

def laplacian_complex(field):
    return laplacian_3d(field.real) + 1j * laplacian_3d(field.imag)

def gradient_sq_magnitude(field):
    gx = (torch.roll(field, -1, 0) - torch.roll(field, 1, 0)) / (2 * dx)
    gy = (torch.roll(field, -1, 1) - torch.roll(field, 1, 1)) / (2 * dx)
    gz = (torch.roll(field, -1, 2) - torch.roll(field, 1, 2)) / (2 * dx)
    return torch.abs(gx)**2 + torch.abs(gy)**2 + torch.abs(gz)**2

def poisson_fft(rho):
    """Exact spectral Poisson solver: Lap(Phi) = 4*pi*G*rho."""
    rho_hat = torch.fft.fftn(rho)
    kx = torch.fft.fftfreq(N, d=dx, device=device) * 2 * torch.pi
    KX, KY, KZ = torch.meshgrid(kx, kx, kx, indexing='ij')
    K2 = KX**2 + KY**2 + KZ**2
    K2[0, 0, 0] = 1.0
    V_hat = -4.0 * torch.pi * G_grav * rho_hat / K2
    V_hat[0, 0, 0] = 0.0
    return torch.fft.ifftn(V_hat).real

# ====================== INITIAL CONDITIONS ======================
sigma = 5.0
amp = 0.15

# Scalar field: Gaussian wavepacket
phi = (amp * torch.exp(-R2 / (2 * sigma**2))).to(torch.complex64)
pi_field = torch.zeros_like(phi)

# Axial torsion field K5(x,y,z): vortex seed with angular structure
# K5 ~ circulation pattern in xy-plane, localized to the field region
K5 = kappa_torsion * torch.exp(-R2 / (2 * (sigma * 1.5)**2)) * (X * Y) / (R2 + 1.0)
# Torsion conjugate momentum
pi_K5 = torch.zeros_like(K5)

# Absorbing sponge layer
sponge_start = 0.7 * L / 2
sponge = torch.ones_like(R_grid)
mask = R_grid > sponge_start
sponge[mask] = torch.exp(-3.0 * ((R_grid[mask] - sponge_start) / (L/2 - sponge_start))**2)
sponge_c = sponge.to(torch.complex64)

# ====================== EVOLUTION ======================
def compute_rhs(phi_s, pi_s, K5_s, pi_K5_s):
    """Full EKG + torsion RHS. Returns derivatives of all 4 fields + diagnostics."""

    # --- 1. Torsion-covariant derivative: D_mu(phi) = nabla_mu(phi) + i*K5*phi ---
    lap_phi = laplacian_complex(phi_s)
    # Torsional modification to the Laplacian (minimal coupling)
    D2_phi = lap_phi + 2j * K5_s * lap_phi_correction(phi_s, K5_s) - K5_s**2 * phi_s

    # --- 2. Energy density for Poisson (kinetic + mass, no GP, no torsion stress) ---
    rho_grav = 0.5 * (torch.abs(pi_s)**2 + gradient_sq_magnitude(phi_s) +
                      m_scalar**2 * torch.abs(phi_s)**2)

    # --- 3. Torsion stress-energy contribution to gravity ---
    # Torsion kinetic + gradient energy (gravitates weakly)
    rho_torsion = 0.5 * pi_K5_s**2 + 0.5 * gradient_sq_real(K5_s)
    rho_total = rho_grav + 0.1 * rho_torsion  # torsion gravitates at 10% coupling

    # --- 4. Poisson solve ---
    V = poisson_fft(rho_total)

    # --- 5. Lapse: alpha = sqrt(1 + 2*Phi), Phi < 0 at center ---
    alpha = torch.sqrt(torch.clamp(1.0 + 2.0 * V, min=0.01))

    # --- 6. Scalar field evolution (torsion-coupled KG) ---
    dphi_dt = alpha * pi_s
    # GP repulsion + mass + torsion backreaction on phi
    dpi_dt = alpha * (lap_phi - m_scalar**2 * phi_s
                      - lam * torch.abs(phi_s)**2 * phi_s
                      - K5_s**2 * phi_s)  # torsion mass-like term

    # --- 7. Torsion field evolution ---
    # K5 obeys a wave equation with source from the scalar current
    # d(K5)/dt = pi_K5
    # d(pi_K5)/dt = Lap(K5) - m_torsion^2 * K5 + source
    lap_K5 = laplacian_3d(K5_s)
    m_torsion_sq = 1.0  # torsion mass (controls range)
    # Source: axial current J5 = Im(phi* D_t phi) ~ Im(phi* pi)
    J5 = torch.imag(torch.conj(phi_s) * pi_s)
    dK5_dt = alpha * pi_K5_s
    dpi_K5_dt = alpha * (lap_K5 - m_torsion_sq * K5_s + kappa_torsion * J5)

    return dphi_dt, dpi_dt, dK5_dt, dpi_K5_dt, V, alpha, rho_total

def lap_phi_correction(phi_s, K5_s):
    """First-order torsion correction to Laplacian: K5 * grad(phi) summed over axes."""
    gx = (torch.roll(phi_s, -1, 0) - torch.roll(phi_s, 1, 0)) / (2 * dx)
    gy = (torch.roll(phi_s, -1, 1) - torch.roll(phi_s, 1, 1)) / (2 * dx)
    gz = (torch.roll(phi_s, -1, 2) - torch.roll(phi_s, 1, 2)) / (2 * dx)
    # K5 is a scalar (axial component), couples to sum of gradients
    return gx + gy + gz

def gradient_sq_real(field):
    """Gradient squared magnitude for real field."""
    gx = (torch.roll(field, -1, 0) - torch.roll(field, 1, 0)) / (2 * dx)
    gy = (torch.roll(field, -1, 1) - torch.roll(field, 1, 1)) / (2 * dx)
    gz = (torch.roll(field, -1, 2) - torch.roll(field, 1, 2)) / (2 * dx)
    return gx**2 + gy**2 + gz**2

# ====================== MAIN LOOP ======================
n_steps = 3000
log_every = 50
mid = N // 2

history_t = []
history_central_rho = []
history_central_lapse = []
history_K5_max = []
history_2MR = []

t = 0.0
start_time = time.time()

print(f"[{time.strftime('%H:%M:%S')}] UHF TORSION-COUPLED 3D EKG (N={N}^3 = {N**3:,} cells)")
print(f"Grid: L={L}, dx={dx:.4f}, dt={dt}")
print(f"Physics: G={G_grav}, m={m_scalar}, lam={lam}, kappa={kappa_torsion}")
print(f"Torsion field K5 initial max: {float(K5.max()):.4f}\n")

for step in range(n_steps):
    # --- RK2 Midpoint ---
    d1_phi, d1_pi, d1_K5, d1_piK5, V, alpha, rho = compute_rhs(phi, pi_field, K5, pi_K5)

    phi_h = phi + 0.5 * dt * d1_phi
    pi_h = pi_field + 0.5 * dt * d1_pi
    K5_h = K5 + 0.5 * dt * d1_K5
    piK5_h = pi_K5 + 0.5 * dt * d1_piK5

    d2_phi, d2_pi, d2_K5, d2_piK5, V, alpha, rho = compute_rhs(phi_h, pi_h, K5_h, piK5_h)

    phi = phi + dt * d2_phi
    pi_field = pi_field + dt * d2_pi
    K5 = K5 + dt * d2_K5
    pi_K5 = pi_K5 + dt * d2_piK5

    # Sponge damping
    phi = phi * sponge_c
    pi_field = pi_field * sponge_c
    K5 = K5 * sponge
    pi_K5 = pi_K5 * sponge
    t += dt

    # --- Logging ---
    if step % log_every == 0:
        c_rho = float(torch.abs(phi[mid, mid, mid])**2)
        c_lapse = float(alpha[mid, mid, mid])
        k5_max = float(torch.max(torch.abs(K5)))
        c_V = float(-V[mid, mid, mid])
        comp = 2.0 * max(c_V, 0.0)

        history_t.append(t)
        history_central_rho.append(c_rho)
        history_central_lapse.append(c_lapse)
        history_K5_max.append(k5_max)
        history_2MR.append(comp)

        print(f"Step {step:05d} | t={t:.4f} | "
              f"rho={c_rho:.4e} | Lapse={c_lapse:.4f} | "
              f"K5max={k5_max:.4f} | 2M/R~{comp:.4f}")

        if c_lapse < 0.05:
            print("\n[!] APPARENT HORIZON: Lapse collapsed below 0.05.")
            break
        if torch.isnan(phi[mid, mid, mid]):
            print("\n[!] NaN DETECTED — simulation unstable.")
            break

elapsed = time.time() - start_time
print(f"\n[OK] TORSION EKG COMPLETE. {step+1} steps in {elapsed:.1f}s")

# ====================== PLOTTING ======================
cpu_t = np.array(history_t)
cpu_rho = np.array(history_central_rho)
cpu_lapse = np.array(history_central_lapse)
cpu_K5 = np.array(history_K5_max)

plt.style.use('dark_background')
fig, axes = plt.subplots(2, 2, figsize=(14, 10))

# Top-left: |phi| slice
slice_phi = torch.abs(phi[:, mid, :]).cpu().detach().numpy()
axes[0, 0].imshow(slice_phi, cmap='inferno', origin='lower',
                   extent=[-L/2, L/2, -L/2, L/2])
axes[0, 0].set_title(f'|phi| slice (y=0) at t={t:.2f}')
axes[0, 0].set_xlabel('x'); axes[0, 0].set_ylabel('z')

# Top-right: K5 torsion slice
slice_K5 = K5[:, mid, :].cpu().detach().numpy()
im = axes[0, 1].imshow(slice_K5, cmap='coolwarm', origin='lower',
                        extent=[-L/2, L/2, -L/2, L/2])
axes[0, 1].set_title(f'Torsion K5 slice (y=0)')
axes[0, 1].set_xlabel('x'); axes[0, 1].set_ylabel('z')
plt.colorbar(im, ax=axes[0, 1])

# Bottom-left: central density + lapse vs time
ax_rho = axes[1, 0]
ax_rho.plot(cpu_t, cpu_rho, color='cyan', lw=2, label='Central |phi|^2')
ax_rho.set_xlabel('Time'); ax_rho.set_ylabel('Central |phi|^2', color='cyan')
ax_rho.set_yscale('log')
ax_rho.grid(True, alpha=0.2)
ax_lapse = ax_rho.twinx()
ax_lapse.plot(cpu_t, cpu_lapse, color='magenta', lw=2, label='Lapse')
ax_lapse.set_ylabel('Lapse alpha', color='magenta')
ax_lapse.axhline(y=0.05, color='red', linestyle='--', alpha=0.5)
ax_rho.set_title('Density & Lapse Evolution')

# Bottom-right: torsion amplitude vs time
axes[1, 1].plot(cpu_t, cpu_K5, color='gold', lw=2)
axes[1, 1].set_xlabel('Time'); axes[1, 1].set_ylabel('max|K5|')
axes[1, 1].set_title('Torsion Field Amplitude')
axes[1, 1].grid(True, alpha=0.2)

plt.suptitle('UHF Torsion-Coupled 3D EKG: Singularity Avoidance', fontsize=14, y=1.01)
plt.tight_layout()
os.makedirs("UHF_results", exist_ok=True)
plt.savefig("UHF_results/3D_torsion_EKG_final.png", dpi=300)
print("Plot saved to UHF_results/3D_torsion_EKG_final.png")

# Final verdict
final_lapse = cpu_lapse[-1] if len(cpu_lapse) > 0 else 1.0
final_K5 = cpu_K5[-1] if len(cpu_K5) > 0 else 0.0
print(f"\n{'='*60}")
print(f"=== FINAL RESULTS ===")
print(f"Final central lapse     = {final_lapse:.6f}")
print(f"Final torsion max|K5|   = {final_K5:.6f}")
print(f"Apparent horizon formed = {final_lapse < 0.05}")
print(f"Torsion field survived  = {final_K5 > 0.001}")
print(f"{'='*60}")
