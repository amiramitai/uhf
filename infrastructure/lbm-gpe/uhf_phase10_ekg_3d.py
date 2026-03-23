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
N = 64                      # 64^3 grid — fits comfortably on single 3090
L = 40.0
dx = L / N
dt_fixed = 0.005  # Locked stable CFL for wave eq + GP nonlinearity
G_grav = 1.0
m_scalar = 0.1              # Small mass for boson star binding
lam = 200.0                 # GP repulsive self-interaction (quantum pressure)

# Laplacian stencil (scaled by 1/dx^2), shape [1,1,3,3,3]
LAP_KERNEL = torch.tensor([[[[0,0,0],[0,-1,0],[0,0,0]],
                             [[0,-1,0],[-1,6,-1],[0,-1,0]],
                             [[0,0,0],[0,-1,0],[0,0,0]]]],
                          dtype=torch.float32, device=device).unsqueeze(0) / (dx**2)

# ====================== HELPERS ======================
def laplacian_3d(field_real):
    """Apply 7-point Laplacian to a real-valued 3D field. Returns same shape."""
    # conv3d expects [B,C,D,H,W]
    out = F.conv3d(field_real.unsqueeze(0).unsqueeze(0), LAP_KERNEL, padding=1)
    return out[0, 0]

def laplacian_complex(field):
    """Laplacian of complex field via split real/imag."""
    return laplacian_3d(field.real) + 1j * laplacian_3d(field.imag)

def gradient_sq_magnitude(field):
    """Sum of |d(field)/dx_i|^2 via central differences."""
    # Shift-based central differences (periodic-like, clamped at edges)
    gx = (torch.roll(field, -1, 0) - torch.roll(field, 1, 0)) / (2 * dx)
    gy = (torch.roll(field, -1, 1) - torch.roll(field, 1, 1)) / (2 * dx)
    gz = (torch.roll(field, -1, 2) - torch.roll(field, 1, 2)) / (2 * dx)
    return torch.abs(gx)**2 + torch.abs(gy)**2 + torch.abs(gz)**2

def poisson_fft(rho):
    """Exact spectral Poisson solver: Lap(V) = 4*pi*G*rho (periodic BC)."""
    rho_hat = torch.fft.fftn(rho)
    # Build k^2 grid (wave numbers)
    kx = torch.fft.fftfreq(N, d=dx, device=device) * 2 * torch.pi
    KX, KY, KZ = torch.meshgrid(kx, kx, kx, indexing='ij')
    K2 = KX**2 + KY**2 + KZ**2
    K2[0, 0, 0] = 1.0  # avoid division by zero (DC mode)
    V_hat = -4.0 * torch.pi * G_grav * rho_hat / K2
    V_hat[0, 0, 0] = 0.0  # set mean potential to zero
    return torch.fft.ifftn(V_hat).real

# ====================== INITIAL CONDITIONS ======================
x = torch.linspace(-L/2 + dx/2, L/2 - dx/2, N, device=device)
X, Y, Z = torch.meshgrid(x, x, x, indexing='ij')
R2 = X**2 + Y**2 + Z**2

# Gaussian wavepacket (real-valued initial state, zero momentum)
sigma = 5.0
amp = 0.15
phi = (amp * torch.exp(-R2 / (2 * sigma**2))).to(torch.complex64)
pi_field = torch.zeros_like(phi)  # Start at rest

# Absorbing sponge layer: damp fields near boundaries to prevent periodic wrap
R_grid = torch.sqrt(R2)
sponge_start = 0.7 * L / 2  # sponge begins at 70% of box half-width
sponge = torch.ones_like(R_grid)
mask = R_grid > sponge_start
sponge[mask] = torch.exp(-3.0 * ((R_grid[mask] - sponge_start) / (L/2 - sponge_start))**2)
sponge = sponge.to(torch.complex64)

# ====================== EVOLUTION FUNCTIONS ======================
def compute_rhs(phi_s, pi_s):
    """EKG right-hand side: returns (dphi/dt, dpi/dt, V, alpha, rho)."""
    # Energy density for Poisson source (kinetic + mass only; GP pressure doesn't gravitate)
    rho = 0.5 * (torch.abs(pi_s)**2 + gradient_sq_magnitude(phi_s) +
                 m_scalar**2 * torch.abs(phi_s)**2)

    # Solve Poisson for gravitational potential (exact via FFT)
    # Solves ∇²Φ = 4πGρ → Φ < 0 at center (Newtonian: Φ = -GM/r)
    V_new = poisson_fft(rho)

    # Lapse function (weak-field GR): α = √(1 + 2Φ) where Φ < 0 at center
    # Clamp Φ > -0.495 so α > 0.1 (pre-horizon regime, never crosses)
    V_clamped = torch.clamp(V_new, min=-0.495)
    alpha = torch.sqrt(1.0 + 2.0 * V_clamped)

    # Klein-Gordon in curved spacetime:
    # Gravity enters ONLY through the lapse (time dilation) — no separate V*φ term
    # d(phi)/dt = α * π
    # d(π)/dt = α * [∇²φ - m²φ - λ|φ|²φ]
    lap_phi = laplacian_complex(phi_s)

    dphi_dt = alpha * pi_s
    dpi_dt = alpha * (lap_phi - m_scalar**2 * phi_s - lam * torch.abs(phi_s)**2 * phi_s)

    return dphi_dt, dpi_dt, V_new, alpha, rho

# ====================== MAIN LOOP ======================
n_steps = 3000
log_every = 50

history_t = []
history_central_rho = []
history_central_lapse = []

t = 0.0
mid = N // 2
start_time = time.time()

print(f"[{time.strftime('%H:%M:%S')}] IGNITING 3D EKG COLLAPSE (N={N}^3 = {N**3:,} cells)")
print(f"Grid: L={L}, dx={dx:.4f}, dt={dt_fixed:.4f}")
print(f"Physics: G={G_grav}, m={m_scalar}, lam={lam}, amp={amp}, dt={dt_fixed:.4f}\n")

for step in range(n_steps):
    # --- RK2 Midpoint Method (stable, 2 evaluations per step) ---
    dphi1, dpi1, V_grav, alpha, rho = compute_rhs(phi, pi_field)

    # Fixed CFL timestep (stable for dx=0.625)
    dt = dt_fixed

    # Half-step
    phi_h = phi + 0.5 * dt * dphi1
    pi_h = pi_field + 0.5 * dt * dpi1

    # Full-step evaluation at midpoint
    dphi2, dpi2, V_grav, alpha, rho = compute_rhs(phi_h, pi_h)

    phi = phi + dt * dphi2
    pi_field = pi_field + dt * dpi2

    # Apply sponge damping at boundaries
    phi = phi * sponge
    pi_field = pi_field * sponge
    t += dt

    # --- Logging ---
    if step % log_every == 0:
        c_rho = float(torch.abs(phi[mid, mid, mid])**2)
        c_lapse = float(alpha[mid, mid, mid])
        # Compactness from potential: 2|Φ| at center (Φ < 0 → |Φ| = -Φ)
        c_potential = float(-V_grav[mid, mid, mid])
        max_comp = 2.0 * c_potential  # = 1 - alpha^2 at center

        history_t.append(t)
        history_central_rho.append(c_rho)
        history_central_lapse.append(c_lapse)

        print(f"Step {step:05d} | t={t:.4f} | dt={dt:.2e} | "
              f"Central rho={c_rho:.4e} | Lapse={c_lapse:.4f} | 2M/R~{max_comp:.4f}")

        if c_lapse < 0.05:
            print("\n[!] APPARENT HORIZON: Lapse collapsed below 0.05.")
            break
        if torch.isnan(phi[mid, mid, mid]):
            print("\n[!] NaN DETECTED — simulation unstable.")
            break

elapsed = time.time() - start_time
print(f"\n[OK] 3D EKG COMPLETE. {step+1} steps in {elapsed:.1f}s")

# ====================== PLOTTING ======================
cpu_t = np.array(history_t)
cpu_rho = np.array(history_central_rho)
cpu_lapse = np.array(history_central_lapse)

plt.style.use('dark_background')
fig, axes = plt.subplots(2, 2, figsize=(14, 10))

# Top-left: field slice
slice_data = torch.abs(phi[:, mid, :]).cpu().detach().numpy()
axes[0, 0].imshow(slice_data, cmap='inferno', origin='lower',
                   extent=[-L/2, L/2, -L/2, L/2])
axes[0, 0].set_title(f'|phi| slice (y=0) at t={t:.2f}')
axes[0, 0].set_xlabel('x'); axes[0, 0].set_ylabel('z')

# Top-right: lapse slice
lapse_data = alpha[:, mid, :].cpu().detach().numpy()
axes[0, 1].imshow(lapse_data, cmap='coolwarm', origin='lower',
                   extent=[-L/2, L/2, -L/2, L/2], vmin=0, vmax=1)
axes[0, 1].set_title(f'Lapse alpha slice (y=0)')
axes[0, 1].set_xlabel('x'); axes[0, 1].set_ylabel('z')

# Bottom-left: central density vs time
axes[1, 0].plot(cpu_t, cpu_rho, color='cyan', lw=2)
axes[1, 0].set_xlabel('Time'); axes[1, 0].set_ylabel('Central |phi|^2')
axes[1, 0].set_title('Central Density Evolution')
axes[1, 0].set_yscale('log')
axes[1, 0].grid(True, alpha=0.2)

# Bottom-right: central lapse vs time
axes[1, 1].plot(cpu_t, cpu_lapse, color='magenta', lw=2)
axes[1, 1].axhline(y=0.05, color='red', linestyle='--', alpha=0.5, label='Horizon threshold')
axes[1, 1].set_xlabel('Time'); axes[1, 1].set_ylabel('Central Lapse')
axes[1, 1].set_title('Lapse Function (Time Dilation)')
axes[1, 1].legend()
axes[1, 1].grid(True, alpha=0.2)

plt.suptitle('UHF 3D Einstein-Klein-Gordon Collapse', fontsize=14, y=1.01)
plt.tight_layout()
os.makedirs("UHF_results", exist_ok=True)
plt.savefig("UHF_results/3D_EKG_collapse_final.png", dpi=300)
print("Plot saved to UHF_results/3D_EKG_collapse_final.png")

# Final verdict
final_lapse = cpu_lapse[-1] if len(cpu_lapse) > 0 else 1.0
print(f"\n=== FINAL RESULTS ===")
print(f"Final central lapse = {final_lapse:.6f}")
print(f"Apparent horizon formed: {final_lapse < 0.05}")