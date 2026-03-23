"""
Geometry Field Simulator v3.0 — Single RTX 3090
================================================
Split-step Gross-Pitaevskii evolution of a complex scalar field
on disk (polar) or sphere (Cartesian) geometry.

Outputs raw metrics: vortex count, symmetry, central B, rim gradient, ray ratio.
"""

import os
import numpy as np
import torch
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from tqdm import tqdm

# ================== CONFIGURATION ==================
MODE = "disk_geometry"       # change to "sphere_geometry" for comparison
GRID_SIZE = 1024
DT = 0.006
STEPS = 500
G_COUPLING = 1.0             # nonlinear coupling |psi|^2 coefficient

# ================== DEVICE ==================
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Device: {device}")
if device.type == "cuda":
    name = torch.cuda.get_device_name(0)
    vram = torch.cuda.get_device_properties(0).total_memory / 1e9
    print(f"  GPU: {name}  VRAM: {vram:.1f} GB")

# ================== GRID SETUP ==================
print(f"Mode: {MODE}  Grid: {GRID_SIZE}x{GRID_SIZE}  Steps: {STEPS}")

if MODE == "disk_geometry":
    r = torch.linspace(0.01, 1.0, GRID_SIZE, device=device)
    theta = torch.linspace(0, 2 * np.pi, GRID_SIZE, device=device)
    R, Theta = torch.meshgrid(r, theta, indexing='ij')
    X = R * torch.cos(Theta)
    Y = R * torch.sin(Theta)
else:   # sphere_geometry
    x = torch.linspace(-1, 1, GRID_SIZE, device=device)
    y = torch.linspace(-1, 1, GRID_SIZE, device=device)
    X, Y = torch.meshgrid(x, y, indexing='ij')

# Effective grid spacing for k-space
dx = 2.0 / GRID_SIZE

# k-space grid for split-step kinetic propagator
kx = torch.fft.fftfreq(GRID_SIZE, d=dx, device=device) * (2 * np.pi)
ky = torch.fft.fftfreq(GRID_SIZE, d=dx, device=device) * (2 * np.pi)
KX, KY = torch.meshgrid(kx, ky, indexing='ij')
K2 = KX ** 2 + KY ** 2

# exp(-i dt k^2 / 2) — precomputed kinetic propagator
kinetic_prop = torch.exp(-1j * DT * K2 / 2.0).to(torch.cfloat)

# ================== INITIAL FIELD ==================
psi = torch.ones(GRID_SIZE, GRID_SIZE, dtype=torch.cfloat, device=device)
psi += 0.01 * torch.randn(GRID_SIZE, GRID_SIZE, dtype=torch.cfloat,
                           device=device)
psi /= torch.abs(psi)   # unit density

# ================== B FIELD (static, geometry-dependent) ==================
if MODE == "disk_geometry":
    B = torch.exp(-(X ** 2 + Y ** 2) * 12) * (Y - X)
else:
    B = torch.zeros(GRID_SIZE, GRID_SIZE, device=device)


# ================== MEASUREMENT FUNCTIONS ==================
@torch.no_grad()
def count_vortices_and_symmetry(psi):
    """Detect vortices via phase curl, return (count, symmetry_score)."""
    phase = torch.angle(psi)
    # Mixed partial d^2(phase)/dxdy as curl proxy
    dphase_dy = torch.gradient(phase, dim=1)[0]
    curl = torch.gradient(dphase_dy, dim=0)[0]
    vort = int((torch.abs(curl) > 2.8).sum().item())
    symmetry = 1.0 if 11 <= vort <= 13 else 0.0
    return vort, symmetry


@torch.no_grad()
def compute_B_metrics(B):
    """Central field strength and rim gradient."""
    h, w = B.shape
    c = h // 2
    # Central 100x100 region
    central = B[c - 50:c + 50, c - 50:c + 50].abs().mean().item()
    # Outer 100-pixel border (all four edges)
    rim = torch.cat([
        B[:100, :].reshape(-1),
        B[-100:, :].reshape(-1),
        B[100:-100, :100].reshape(-1),
        B[100:-100, -100:].reshape(-1),
    ]).abs().mean().item()
    return central, rim


@torch.no_grad()
def compute_velocity(psi):
    """Superfluid velocity v = grad(phase)."""
    phase = torch.angle(psi)
    vx = torch.gradient(phase, dim=1)[0] / dx
    vy = torch.gradient(phase, dim=0)[0] / dx
    return vx, vy


@torch.no_grad()
def ray_march(B, n_rays=2048):
    """Trace rays through B field with magnetic bending. Returns hit fraction."""
    h, w = B.shape
    # Start rays from left edge, uniformly spaced in y
    y_pos = torch.linspace(0.0, h - 1.0, n_rays, device=B.device)
    x_pos = torch.zeros(n_rays, device=B.device)
    vx = torch.ones(n_rays, device=B.device)
    vy = torch.zeros(n_rays, device=B.device)

    step_size = 4.0
    max_steps = w // int(step_size)

    for _ in range(max_steps):
        ix = x_pos.long().clamp(0, w - 1)
        iy = y_pos.long().clamp(0, h - 1)
        b_local = B[iy, ix]

        # Magnetic bending
        vy = vy + 0.1 * b_local * step_size

        # Renormalize direction
        speed = torch.sqrt(vx ** 2 + vy ** 2).clamp(min=1e-6)
        vx = vx / speed
        vy = vy / speed

        x_pos = x_pos + vx * step_size
        y_pos = (y_pos + vy * step_size).clamp(0, h - 1)

    hits = int((x_pos >= w - 1).sum().item())
    return hits / n_rays


# ================== SPLIT-STEP GP INTEGRATOR ==================
@torch.no_grad()
def gp_step(psi):
    """One split-step Gross-Pitaevskii time step."""
    # Half-step potential
    V = G_COUPLING * torch.abs(psi) ** 2
    psi = psi * torch.exp(-1j * (DT / 2) * V)

    # Full kinetic step in k-space
    psi_k = torch.fft.fft2(psi)
    psi_k = psi_k * kinetic_prop
    psi = torch.fft.ifft2(psi_k)

    # Half-step potential
    V = G_COUPLING * torch.abs(psi) ** 2
    psi = psi * torch.exp(-1j * (DT / 2) * V)

    return psi


# ================== MAIN LOOP ==================
plt.ion()
fig, axs = plt.subplots(2, 3, figsize=(18, 10))
fig.suptitle(f"Geometry Field Simulator v3.0 — {MODE}", fontsize=14)

# Pre-compute static B metrics (B doesn't change)
central_B, rim_B = compute_B_metrics(B)
B_cpu = B.cpu().numpy()

final_vort = 0
final_sym = 0.0
final_ray = 0.0

for step in tqdm(range(STEPS), desc="Evolving"):
    psi = gp_step(psi)

    # Update display every 10 steps and on the final step
    if step % 10 == 0 or step == STEPS - 1:
        density = torch.abs(psi) ** 2
        phase = torch.angle(psi)
        vort, sym = count_vortices_and_symmetry(psi)
        ray_ratio = ray_march(B)

        final_vort = vort
        final_sym = sym
        final_ray = ray_ratio

        den_cpu = density.cpu().numpy()
        phase_cpu = phase.cpu().numpy()

        # (0,0) Density
        axs[0, 0].cla()
        axs[0, 0].imshow(den_cpu, cmap='viridis', origin='lower')
        axs[0, 0].set_title(f"Density |psi|^2  |  Vortices: {vort}")

        # (0,1) Phase
        axs[0, 1].cla()
        axs[0, 1].imshow(phase_cpu, cmap='twilight', origin='lower')
        axs[0, 1].set_title("Phase arg(psi)")

        # (0,2) B field
        axs[0, 2].cla()
        axs[0, 2].imshow(B_cpu, cmap='coolwarm', origin='lower')
        axs[0, 2].set_title(
            f"B field  |  Central: {central_B:.3f}  Rim: {rim_B:.3f}")

        # (1,0) Velocity quiver
        axs[1, 0].cla()
        vx, vy = compute_velocity(psi)
        skip = 32
        vx_sub = vx[::skip, ::skip].cpu().numpy()
        vy_sub = vy[::skip, ::skip].cpu().numpy()
        qx = np.arange(0, GRID_SIZE, skip)
        qy = np.arange(0, GRID_SIZE, skip)
        QX, QY = np.meshgrid(qx, qy, indexing='ij')
        axs[1, 0].quiver(QX, QY, vx_sub, vy_sub,
                         scale=300, alpha=0.7, color='white')
        axs[1, 0].imshow(den_cpu, cmap='inferno', origin='lower', alpha=0.5)
        axs[1, 0].set_title("Velocity field")

        # (1,1) Ray ratio bar
        axs[1, 1].cla()
        axs[1, 1].bar(["Ray Ratio"], [ray_ratio], color='steelblue')
        axs[1, 1].set_ylim(0, 1)
        axs[1, 1].set_title(f"Ray Ratio: {ray_ratio:.3f}")

        # (1,2) Metrics summary
        axs[1, 2].cla()
        axs[1, 2].axis('off')
        txt = (f"Step: {step + 1}/{STEPS}\n"
               f"Mode: {MODE}\n\n"
               f"Vortices:   {vort}\n"
               f"Symmetry:   {sym:.2f}\n"
               f"Central |B|: {central_B:.4f}\n"
               f"Rim grad:   {rim_B:.4f}\n"
               f"Ray ratio:  {ray_ratio:.3f}")
        axs[1, 2].text(0.1, 0.5, txt, fontsize=13, family='monospace',
                       va='center', transform=axs[1, 2].transAxes)
        axs[1, 2].set_title("Metrics")

        plt.tight_layout()
        try:
            plt.pause(0.001)
        except Exception:
            pass

# ================== FINAL OUTPUT ==================
print(f"\n{'=' * 50}")
print(f"  SIMULATION COMPLETE — {MODE}")
print(f"{'=' * 50}")
print(f"  Vortex count:      {final_vort}")
print(f"  Symmetry score:    {final_sym:.2f}")
print(f"  Central strength:  {central_B:.4f}")
print(f"  Rim gradient:      {rim_B:.4f}")
print(f"  Ray ratio:         {final_ray:.3f}")
print(f"{'=' * 50}")

os.makedirs("results", exist_ok=True)
out_path = f"results/{MODE}_final.png"
plt.savefig(out_path, dpi=400, bbox_inches='tight')
print(f"  Saved: {out_path}")
plt.close()
