"""
Field Evolution Simulator v3 (Improved) — CuPy Single RTX 3090
================================================================
Split-step Gross-Pitaevskii evolution of a complex scalar field
on polar (geometry_a) or Cartesian (geometry_b) grid.

4 initial vortex seeds. Outputs raw metrics: vortex count,
symmetry score, central B, rim gradient, ray ratio.
"""

import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from tqdm import tqdm

os.environ["LD_LIBRARY_PATH"] = "/usr/lib/wsl/lib"

import cupy as cp

# ================== CONFIGURATION ==================
MODE = "geometry_a"          # change to "geometry_b" for Cartesian
GRID_SIZE = 1024
DT = 0.006
STEPS = 500
G_NL = 10.0                  # nonlinearity g

# ================== DEVICE ==================
dev = cp.cuda.Device(0)
dev.use()
mem = dev.mem_info
print(f"Device: GPU 0  Free VRAM: {mem[0] / 1e9:.1f} / {mem[1] / 1e9:.1f} GB")
print(f"Mode: {MODE}  Grid: {GRID_SIZE}x{GRID_SIZE}  Steps: {STEPS}  g: {G_NL}")

# ================== GRID SETUP ==================
if MODE == "geometry_a":
    r = cp.linspace(0.01, 1.0, GRID_SIZE, dtype=cp.float32)
    theta = cp.linspace(0, 2 * np.pi, GRID_SIZE, dtype=cp.float32)
    R, Theta = cp.meshgrid(r, theta, indexing='ij')
    X = R * cp.cos(Theta)
    Y = R * cp.sin(Theta)
else:   # geometry_b
    x = cp.linspace(-1, 1, GRID_SIZE, dtype=cp.float32)
    y = cp.linspace(-1, 1, GRID_SIZE, dtype=cp.float32)
    X, Y = cp.meshgrid(x, y, indexing='ij')

dx = 2.0 / GRID_SIZE

# ================== K-SPACE PROPAGATOR ==================
kx = cp.fft.fftfreq(GRID_SIZE, d=dx).astype(cp.float32) * (2 * np.pi)
ky = cp.fft.fftfreq(GRID_SIZE, d=dx).astype(cp.float32) * (2 * np.pi)
KX, KY = cp.meshgrid(kx, ky, indexing='ij')
K2 = KX ** 2 + KY ** 2

# exp(-i dt k^2 / 2) kinetic propagator
kinetic_prop = cp.exp(-1j * DT * K2.astype(cp.float64) / 2.0).astype(cp.complex64)

# ================== INITIAL FIELD WITH 4 VORTEX SEEDS ==================
psi = cp.ones((GRID_SIZE, GRID_SIZE), dtype=cp.complex64)

# Seed 4 vortex singularities at random positions inside the grid
rng = np.random.RandomState(42)
for _ in range(4):
    # Random center in grid coordinates
    cx = rng.uniform(0.2, 0.8) * GRID_SIZE
    cy = rng.uniform(0.2, 0.8) * GRID_SIZE
    charge = rng.choice([-1, +1])

    # Grid index arrays
    ix = cp.arange(GRID_SIZE, dtype=cp.float32)
    iy = cp.arange(GRID_SIZE, dtype=cp.float32)
    IX, IY = cp.meshgrid(ix, iy, indexing='ij')

    # Phase ramp: charge * atan2(y - cy, x - cx)
    vortex_phase = charge * cp.arctan2(IY - cp.float32(cy),
                                       IX - cp.float32(cx))
    # Amplitude envelope: tanh(r / core_size) to create density depletion
    r_from_center = cp.sqrt((IX - cp.float32(cx)) ** 2 +
                            (IY - cp.float32(cy)) ** 2)
    core_size = 8.0  # pixels
    amplitude = cp.tanh(r_from_center / cp.float32(core_size))

    psi = psi * amplitude.astype(cp.float32) * cp.exp(
        1j * vortex_phase.astype(cp.float32))

# Small noise + normalize to unit mean density
psi += 0.01 * cp.random.randn(GRID_SIZE, GRID_SIZE, dtype=cp.float32)
mean_amp = float(cp.mean(cp.abs(psi) ** 2))
if mean_amp > 0:
    psi = psi * cp.float32(1.0 / np.sqrt(mean_amp))

# ================== B FIELD ==================
if MODE == "geometry_a":
    B = cp.exp(-(X ** 2 + Y ** 2) * 12) * (Y - X)
else:
    B = cp.zeros((GRID_SIZE, GRID_SIZE), dtype=cp.float32)


# ================== MEASUREMENT FUNCTIONS ==================
def count_vortices_and_symmetry(psi):
    """Detect vortices via phase curl, return (count, symmetry_score)."""
    phase = cp.angle(psi)
    # d(phase)/dy
    dphase_dy = cp.roll(phase, -1, axis=1) - cp.roll(phase, 1, axis=1)
    # Unwrap
    dphase_dy = cp.arctan2(cp.sin(dphase_dy), cp.cos(dphase_dy))
    # d/dx of dphase_dy  (curl proxy)
    curl = cp.roll(dphase_dy, -1, axis=0) - cp.roll(dphase_dy, 1, axis=0)
    curl = cp.arctan2(cp.sin(curl), cp.cos(curl))

    vort = int((cp.abs(curl) > 2.8).sum())
    symmetry = 1.0 if 11 <= vort <= 13 else 0.0
    return vort, symmetry


def compute_B_metrics(B):
    """Central field strength and rim gradient."""
    h, w = B.shape
    c = h // 2
    central = float(cp.mean(cp.abs(B[c - 50:c + 50, c - 50:c + 50])))
    rim_parts = cp.concatenate([
        B[:100, :].ravel(),
        B[-100:, :].ravel(),
        B[100:-100, :100].ravel(),
        B[100:-100, -100:].ravel(),
    ])
    rim = float(cp.mean(cp.abs(rim_parts)))
    return central, rim


def compute_velocity(psi, dx_val):
    """Superfluid velocity v = grad(phase) via central difference."""
    phase = cp.angle(psi)
    # Central differences with unwrapping
    dpx = cp.roll(phase, -1, axis=1) - cp.roll(phase, 1, axis=1)
    dpx = cp.arctan2(cp.sin(dpx), cp.cos(dpx)) / (2.0 * dx_val)
    dpy = cp.roll(phase, -1, axis=0) - cp.roll(phase, 1, axis=0)
    dpy = cp.arctan2(cp.sin(dpy), cp.cos(dpy)) / (2.0 * dx_val)
    return dpx, dpy


def ray_march(B, n_rays=2048):
    """Trace rays through B field with magnetic bending. Returns hit fraction."""
    h, w = B.shape
    y_pos = cp.linspace(0.0, h - 1.0, n_rays, dtype=cp.float32)
    x_pos = cp.zeros(n_rays, dtype=cp.float32)
    vx = cp.ones(n_rays, dtype=cp.float32)
    vy = cp.zeros(n_rays, dtype=cp.float32)

    step_size = cp.float32(4.0)
    max_steps = w // 4

    for _ in range(max_steps):
        ix = cp.clip(x_pos.astype(cp.int32), 0, w - 1)
        iy = cp.clip(y_pos.astype(cp.int32), 0, h - 1)
        b_local = B[iy, ix]

        vy = vy + 0.1 * b_local * step_size
        speed = cp.sqrt(vx ** 2 + vy ** 2)
        speed = cp.maximum(speed, cp.float32(1e-6))
        vx = vx / speed
        vy = vy / speed

        x_pos = x_pos + vx * step_size
        y_pos = cp.clip(y_pos + vy * step_size, 0, h - 1)

    hits = int((x_pos >= w - 1).sum())
    return hits / n_rays


# ================== SPLIT-STEP GP INTEGRATOR ==================
def gp_step(psi):
    """One split-step Gross-Pitaevskii time step."""
    # Half-step potential
    rho = cp.abs(psi) ** 2
    psi = psi * cp.exp(cp.float32(-DT / 2) * 1j * G_NL * rho)

    # Full kinetic step in k-space
    psi_k = cp.fft.fft2(psi)
    psi_k *= kinetic_prop
    psi = cp.fft.ifft2(psi_k)

    # Half-step potential
    rho = cp.abs(psi) ** 2
    psi = psi * cp.exp(cp.float32(-DT / 2) * 1j * G_NL * rho)

    return psi


# ================== MAIN LOOP ==================
plt.ion()
fig, axs = plt.subplots(2, 3, figsize=(18, 10))
fig.suptitle(f"Field Evolution Simulator v3 — {MODE}", fontsize=14)

# Static B metrics
central_B, rim_B = compute_B_metrics(B)
B_cpu = cp.asnumpy(B)

final_vort = 0
final_sym = 0.0
final_ray = 0.0

for step in tqdm(range(STEPS), desc="Evolving"):
    psi = gp_step(psi)

    if step % 10 == 0 or step == STEPS - 1:
        density = cp.abs(psi) ** 2
        phase = cp.angle(psi)
        vort, sym = count_vortices_and_symmetry(psi)
        ray_ratio = ray_march(B)

        final_vort = vort
        final_sym = sym
        final_ray = ray_ratio

        den_cpu = cp.asnumpy(density)
        phase_cpu = cp.asnumpy(phase)

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
            f"B field  |  Central: {central_B:.4f}  Rim: {rim_B:.4f}")

        # (1,0) Velocity quiver
        axs[1, 0].cla()
        vx, vy = compute_velocity(psi, dx)
        skip = 32
        vx_sub = cp.asnumpy(vx[::skip, ::skip])
        vy_sub = cp.asnumpy(vy[::skip, ::skip])
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
               f"Vortices:    {vort}\n"
               f"Symmetry:    {sym:.2f}\n"
               f"Central |B|: {central_B:.4f}\n"
               f"Rim grad:    {rim_B:.4f}\n"
               f"Ray ratio:   {ray_ratio:.3f}")
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
