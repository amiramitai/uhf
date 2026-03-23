"""
Field Evolution Simulator v4 — Analysis Edition (CuPy, Single RTX 3090)
========================================================================
Split-step Gross-Pitaevskii evolution with damping, 12 angular vortex
seeds (geometry_a) or 4 random seeds (geometry_b), plus post-run
stability analysis: energy conservation, angular momentum, morphology.
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
STEPS = 600
G_NL = 2.5                   # nonlinearity
GAMMA = 0.015                 # imaginary potential (damping strength)

# ================== DEVICE ==================
dev = cp.cuda.Device(0)
dev.use()
mem = dev.mem_info
print(f"Device: GPU 0  Free VRAM: {mem[0] / 1e9:.1f} / {mem[1] / 1e9:.1f} GB")
print(f"Mode: {MODE}  Grid: {GRID_SIZE}x{GRID_SIZE}  Steps: {STEPS}"
      f"  g: {G_NL}  gamma: {GAMMA}")

# ================== GRID SETUP ==================
if MODE == "geometry_a":
    r = cp.linspace(0.01, 1.0, GRID_SIZE, dtype=cp.float32)
    theta = cp.linspace(0, 2 * np.pi, GRID_SIZE, dtype=cp.float32)
    R, Theta = cp.meshgrid(r, theta, indexing='ij')
    X = R * cp.cos(Theta)
    Y = R * cp.sin(Theta)
else:
    x = cp.linspace(-1, 1, GRID_SIZE, dtype=cp.float32)
    y = cp.linspace(-1, 1, GRID_SIZE, dtype=cp.float32)
    X, Y = cp.meshgrid(x, y, indexing='ij')

dx = 2.0 / GRID_SIZE

# ================== K-SPACE PROPAGATOR ==================
kx = cp.fft.fftfreq(GRID_SIZE, d=dx).astype(cp.float32) * (2 * np.pi)
ky = cp.fft.fftfreq(GRID_SIZE, d=dx).astype(cp.float32) * (2 * np.pi)
KX, KY = cp.meshgrid(kx, ky, indexing='ij')
K2 = KX ** 2 + KY ** 2

# Kinetic propagator: exp(-i dt k^2 / 2)
kinetic_prop = cp.exp(-1j * DT * K2.astype(cp.float64) / 2.0).astype(cp.complex64)

# ================== INDEX ARRAYS (for vortex seeding) ==================
ix_arr = cp.arange(GRID_SIZE, dtype=cp.float32)
iy_arr = cp.arange(GRID_SIZE, dtype=cp.float32)
IX, IY = cp.meshgrid(ix_arr, iy_arr, indexing='ij')

# ================== INITIAL FIELD ==================
psi = cp.ones((GRID_SIZE, GRID_SIZE), dtype=cp.complex64)

if MODE == "geometry_a":
    # 12 vortex seeds at regular 30-degree angular positions
    # Place them at radius 0.5 in physical coords -> map to grid coords
    for k in range(12):
        angle = k * (np.pi / 6.0)   # 30 deg increments
        phys_x = 0.5 * np.cos(angle)
        phys_y = 0.5 * np.sin(angle)
        # Map physical [-1,1] to grid [0, GRID_SIZE]
        cx = (phys_x + 1.0) / 2.0 * GRID_SIZE
        cy = (phys_y + 1.0) / 2.0 * GRID_SIZE
        charge = 1 if k % 2 == 0 else -1

        vortex_phase = charge * cp.arctan2(IY - cp.float32(cy),
                                           IX - cp.float32(cx))
        r_from_center = cp.sqrt((IX - cp.float32(cx)) ** 2 +
                                (IY - cp.float32(cy)) ** 2)
        amplitude = cp.tanh(r_from_center / cp.float32(6.0))
        psi = psi * amplitude * cp.exp(1j * vortex_phase)
else:
    # 4 random vortex seeds
    rng = np.random.RandomState(42)
    for _ in range(4):
        cx = rng.uniform(0.2, 0.8) * GRID_SIZE
        cy = rng.uniform(0.2, 0.8) * GRID_SIZE
        charge = rng.choice([-1, +1])
        vortex_phase = charge * cp.arctan2(IY - cp.float32(cy),
                                           IX - cp.float32(cx))
        r_from_center = cp.sqrt((IX - cp.float32(cx)) ** 2 +
                                (IY - cp.float32(cy)) ** 2)
        amplitude = cp.tanh(r_from_center / cp.float32(8.0))
        psi = psi * amplitude * cp.exp(1j * vortex_phase)

# Small noise + normalize
psi += 0.01 * cp.random.randn(GRID_SIZE, GRID_SIZE, dtype=cp.float32)
mean_amp = float(cp.mean(cp.abs(psi) ** 2))
if mean_amp > 0:
    psi = psi * cp.float32(1.0 / np.sqrt(mean_amp))

# Free index arrays
del IX, IY, ix_arr, iy_arr
cp.get_default_memory_pool().free_all_blocks()

# ================== B FIELD (static) ==================
if MODE == "geometry_a":
    B = cp.exp(-(X ** 2 + Y ** 2) * 12) * (Y - X)
else:
    B = cp.zeros((GRID_SIZE, GRID_SIZE), dtype=cp.float32)


# ================== MEASUREMENT FUNCTIONS ==================
def count_vortices_and_symmetry(psi):
    """Detect vortices via phase curl, return (count, symmetry_score)."""
    phase = cp.angle(psi)
    dphase_dy = cp.roll(phase, -1, axis=1) - cp.roll(phase, 1, axis=1)
    dphase_dy = cp.arctan2(cp.sin(dphase_dy), cp.cos(dphase_dy))
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
    """Superfluid velocity via central difference with unwrapping."""
    phase = cp.angle(psi)
    dpx = cp.roll(phase, -1, axis=1) - cp.roll(phase, 1, axis=1)
    dpx = cp.arctan2(cp.sin(dpx), cp.cos(dpx)) / (2.0 * dx_val)
    dpy = cp.roll(phase, -1, axis=0) - cp.roll(phase, 1, axis=0)
    dpy = cp.arctan2(cp.sin(dpy), cp.cos(dpy)) / (2.0 * dx_val)
    return dpx, dpy


def ray_march(B, n_rays=2048):
    """Trace rays with magnetic bending, return hit fraction."""
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
        speed = cp.maximum(cp.sqrt(vx ** 2 + vy ** 2), cp.float32(1e-6))
        vx = vx / speed
        vy = vy / speed
        x_pos = x_pos + vx * step_size
        y_pos = cp.clip(y_pos + vy * step_size, 0, h - 1)
    return int((x_pos >= w - 1).sum()) / n_rays


# ================== ENERGY & ANGULAR MOMENTUM ==================
def compute_energy(psi, dx_val):
    """Total energy: kinetic (gradient) + interaction (|psi|^4)."""
    # Kinetic: 0.5 * sum |grad psi|^2 * dx^2
    dpsi_dx = (cp.roll(psi, -1, axis=1) - cp.roll(psi, 1, axis=1)) / (2.0 * dx_val)
    dpsi_dy = (cp.roll(psi, -1, axis=0) - cp.roll(psi, 1, axis=0)) / (2.0 * dx_val)
    E_kin = 0.5 * float(cp.sum(cp.abs(dpsi_dx) ** 2 +
                                cp.abs(dpsi_dy) ** 2)) * dx_val ** 2
    # Interaction: g/2 * sum |psi|^4 * dx^2
    rho = cp.abs(psi) ** 2
    E_int = 0.5 * G_NL * float(cp.sum(rho ** 2)) * dx_val ** 2
    return E_kin, E_int, E_kin + E_int


def compute_angular_momentum(psi, dx_val):
    """L_z = Im[sum psi* (x dp/dy - y dp/dx)] * dx^2."""
    # Reconstruct grid-index based x,y for angular momentum
    N = psi.shape[0]
    ix = cp.arange(N, dtype=cp.float32) * dx_val - 1.0
    iy = cp.arange(N, dtype=cp.float32) * dx_val - 1.0
    GX, GY = cp.meshgrid(ix, iy, indexing='ij')

    dpsi_dx = (cp.roll(psi, -1, axis=1) - cp.roll(psi, 1, axis=1)) / (2.0 * dx_val)
    dpsi_dy = (cp.roll(psi, -1, axis=0) - cp.roll(psi, 1, axis=0)) / (2.0 * dx_val)

    integrand = cp.conj(psi) * (GX * dpsi_dy - GY * dpsi_dx)
    Lz = float(cp.imag(cp.sum(integrand))) * dx_val ** 2
    return Lz


def classify_morphology(psi, dx_val):
    """Classify: collapsed, stable ring, or turbulent."""
    rho = cp.abs(psi) ** 2
    N = psi.shape[0]
    c = N // 2
    # Radial profile: mean density in concentric annuli
    ix = cp.arange(N, dtype=cp.float32) - c
    iy = cp.arange(N, dtype=cp.float32) - c
    GX, GY = cp.meshgrid(ix, iy, indexing='ij')
    R_grid = cp.sqrt(GX ** 2 + GY ** 2)

    # Central density (r < N/10)
    mask_center = R_grid < (N / 10)
    rho_center = float(cp.mean(rho[mask_center])) if int(mask_center.sum()) > 0 else 0.0

    # Ring region (N/5 < r < N/3)
    mask_ring = (R_grid > N / 5) & (R_grid < N / 3)
    rho_ring = float(cp.mean(rho[mask_ring])) if int(mask_ring.sum()) > 0 else 0.0

    # Density variance (turbulence indicator)
    rho_var = float(cp.var(rho))
    rho_mean = float(cp.mean(rho))

    if rho_center > 2.0 * rho_ring:
        return "collapsed", rho_center, rho_ring, rho_var
    elif rho_ring > 1.5 * rho_center:
        return "stable_ring", rho_center, rho_ring, rho_var
    elif rho_var > 0.1 * rho_mean ** 2:
        return "turbulent", rho_center, rho_ring, rho_var
    else:
        return "uniform", rho_center, rho_ring, rho_var


# ================== SPLIT-STEP GP WITH DAMPING ==================
def gp_step(psi):
    """One split-step GP time step with mild imaginary damping."""
    rho = cp.abs(psi) ** 2
    # Half-step: potential + damping  V_eff = g|psi|^2 - i*gamma
    psi = psi * cp.exp((-1j * G_NL * rho - GAMMA) * cp.float32(DT / 2))

    # Full kinetic step
    psi_k = cp.fft.fft2(psi)
    psi_k *= kinetic_prop
    psi = cp.fft.ifft2(psi_k)

    # Half-step potential + damping
    rho = cp.abs(psi) ** 2
    psi = psi * cp.exp((-1j * G_NL * rho - GAMMA) * cp.float32(DT / 2))

    return psi


# ================== RECORD INITIAL ENERGY & Lz ==================
E_kin_0, E_int_0, E_tot_0 = compute_energy(psi, dx)
Lz_0 = compute_angular_momentum(psi, dx)
print(f"Initial: E_tot={E_tot_0:.4f}  Lz={Lz_0:.4f}")

# ================== MAIN LOOP ==================
plt.ion()
fig, axs = plt.subplots(2, 3, figsize=(18, 10))
fig.suptitle(f"Field Evolution Simulator v4 — {MODE}", fontsize=14)

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

        # (1,2) Metrics
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

# ================== FINAL METRICS ==================
print(f"\n{'=' * 60}")
print(f"  RAW METRICS — {MODE}")
print(f"{'=' * 60}")
print(f"  Vortex count:      {final_vort}")
print(f"  Symmetry score:    {final_sym:.2f}")
print(f"  Central strength:  {central_B:.4f}")
print(f"  Rim gradient:      {rim_B:.4f}")
print(f"  Ray ratio:         {final_ray:.3f}")
print(f"{'=' * 60}")

# ================== STABILITY ANALYSIS ==================
E_kin_f, E_int_f, E_tot_f = compute_energy(psi, dx)
Lz_f = compute_angular_momentum(psi, dx)

E_drift_pct = 100.0 * abs(E_tot_f - E_tot_0) / max(abs(E_tot_0), 1e-15)
Lz_drift_pct = (100.0 * abs(Lz_f - Lz_0) / max(abs(Lz_0), 1e-15)
                if abs(Lz_0) > 1e-15 else 0.0)

morph_label, rho_center, rho_ring, rho_var = classify_morphology(psi, dx)

print(f"\n{'=' * 60}")
print(f"  STABILITY ANALYSIS — {MODE}")
print(f"{'=' * 60}")
print(f"  Energy:  initial={E_tot_0:.4f}  final={E_tot_f:.4f}"
      f"  drift={E_drift_pct:.2f}%")
print(f"    (kinetic: {E_kin_0:.4f} -> {E_kin_f:.4f},"
      f"  interaction: {E_int_0:.4f} -> {E_int_f:.4f})")
print(f"  Ang. momentum:  initial={Lz_0:.4f}  final={Lz_f:.4f}"
      f"  drift={Lz_drift_pct:.2f}%")
print(f"  Morphology:  {morph_label}")
print(f"    rho_center={rho_center:.6f}  rho_ring={rho_ring:.6f}"
      f"  rho_var={rho_var:.6e}")

# One-sentence summary
if morph_label == "stable_ring":
    summary = (f"{MODE} formed a stable ring structure with "
               f"{E_drift_pct:.1f}% energy drift — topologically robust.")
elif morph_label == "collapsed":
    summary = (f"{MODE} collapsed toward the center with "
               f"{E_drift_pct:.1f}% energy drift — unstable under damping.")
elif morph_label == "turbulent":
    summary = (f"{MODE} remained turbulent with {final_vort} vortices and "
               f"{E_drift_pct:.1f}% energy drift — no equilibrium reached.")
else:
    summary = (f"{MODE} relaxed to a near-uniform state with "
               f"{E_drift_pct:.1f}% energy drift — damping quenched all structure.")

print(f"\n  Summary: {summary}")
print(f"{'=' * 60}")

# ================== SAVE ==================
os.makedirs("results", exist_ok=True)
out_path = f"results/{MODE}_final.png"
plt.savefig(out_path, dpi=400, bbox_inches='tight')
print(f"  Saved: {out_path}")
plt.close()
