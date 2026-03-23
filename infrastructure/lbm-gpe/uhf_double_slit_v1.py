#!/usr/bin/env python3
"""
UHF — Hydrodynamic Double-Slit v1
===================================
Emergent Quantum Interference from Pilot-Wave Guidance.

2D Split-Step Fourier GP on RTX 3090 (2048×2048, L=80ξ).

Method A (Wave Intensity):
  Evolve a wide Gaussian wavepacket through a double-slit barrier.
  Measure |ψ(x_screen, y)|² — the diffraction+interference pattern.

Method B (Bohmian Trajectories):
  Run N=300 deterministic Bohmian trajectories:
  - Each particle starts at (x=WP_X0, y₀) with y₀ ~ Uniform(-6ξ, +6ξ)
  - Particle velocity = probability current / density: v = Im(ψ*∇ψ) / |ψ|²
  - All particles share the SAME evolving wavefunction (pilot wave)
  - Track until screen crossing; record y_hit

Output: histogram of screen arrivals with Fraunhofer prediction overlay.

Units: ℏ = m = ξ = c_s = 1, κ = 2π, ρ₀ = 1, g = 1.
"""

import os, sys, time, json
import numpy as np
import torch
import torch.fft as fft
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ══════════════════════════════════════════════════════════════════════
#  Parameters
# ══════════════════════════════════════════════════════════════════════
NX = NY = 2048
LX = LY = 80.0        # domain [ξ]
XI      = 1.0
RHO_0   = 1.0
G_NL    = 1.0
MU      = G_NL * RHO_0

DX = LX / NX
DY = LY / NY
DA = DX * DY

# Real-time
DT       = 0.02       # time step [τ]

# Wavepacket (wide Gaussian ≈ plane wave through both slits)
WP_SIGMA_X = 4.0      # width in x (propagation) [ξ]
WP_SIGMA_Y = 20.0     # width in y (transverse) [ξ] — wide → uniform across slits
WP_X0      = -20.0    # launch x-position [ξ]
WP_KX      = 1.5      # launch momentum (v_group ≈ 1.5)
WP_AMP     = 1.0      # peak amplitude

# Double-slit barrier
BARRIER_X     = 0.0           # barrier centre x [ξ]
BARRIER_THICK = 2.0           # barrier thickness in x [ξ]
SLIT_WIDTH    = 2.0           # each slit opening [ξ]
SLIT_SEP      = 8.0           # centre-to-centre slit separation [ξ]
V_BARRIER     = 100.0         # barrier height
TANH_WIDTH    = 0.3           # smooth edge width [ξ] — sharp for clean slits

# Absorbing sponge layer
SPONGE_WIDTH  = 8.0           # width of absorbing boundary [ξ]
SPONGE_GAMMA  = 0.5           # max absorption rate

# Detection screen
SCREEN_X      = 10.0          # detection screen x-position [ξ]

# Bohmian ensemble
N_PARTICLES   = 1000          # number of Bohmian trajectories
JITTER_RANGE  = 6.0           # y₀ ~ Uniform(−6, +6) [ξ]

# Histogram
N_BINS        = 80

OUTDIR = "UHF_DoubleSlit_v1_results"
DTYPE  = torch.float64
CDTYPE = torch.complex128


# ══════════════════════════════════════════════════════════════════════
#  GPU
# ══════════════════════════════════════════════════════════════════════
def setup_device():
    assert torch.cuda.is_available(), "CUDA required"
    dev = torch.device('cuda:0')
    p = torch.cuda.get_device_properties(dev)
    print(f"  Device : {p.name}")
    print(f"  VRAM   : {p.total_memory / 1e9:.1f} GB")
    return dev


# ══════════════════════════════════════════════════════════════════════
#  Grids
# ══════════════════════════════════════════════════════════════════════
def build_grids(device):
    x1d = torch.linspace(-LX/2, LX/2 - DX, NX, device=device, dtype=DTYPE)
    y1d = torch.linspace(-LY/2, LY/2 - DY, NY, device=device, dtype=DTYPE)
    X, Y = torch.meshgrid(x1d, y1d, indexing='ij')

    kx1d = torch.fft.fftfreq(NX, d=DX, device=device).to(DTYPE) * 2 * np.pi
    ky1d = torch.fft.fftfreq(NY, d=DY, device=device).to(DTYPE) * 2 * np.pi
    KX, KY = torch.meshgrid(kx1d, ky1d, indexing='ij')
    K2 = KX**2 + KY**2
    return X, Y, K2, KX, KY


# ══════════════════════════════════════════════════════════════════════
#  Barrier Potential (Double Slit)
# ══════════════════════════════════════════════════════════════════════
def build_barrier(X, Y):
    """Double-slit barrier with smooth tanh edges."""
    w = TANH_WIDTH
    ht = BARRIER_THICK / 2.0

    # x-profile: wall region
    x_profile = 0.5 * (torch.tanh((X - BARRIER_X + ht) / w)
                       - torch.tanh((X - BARRIER_X - ht) / w))

    # Slit openings
    sw = SLIT_WIDTH / 2.0
    y1_center = +SLIT_SEP / 2.0
    y2_center = -SLIT_SEP / 2.0

    slit1 = 0.5 * (torch.tanh((Y - y1_center + sw) / w)
                   - torch.tanh((Y - y1_center - sw) / w))
    slit2 = 0.5 * (torch.tanh((Y - y2_center + sw) / w)
                   - torch.tanh((Y - y2_center - sw) / w))

    y_wall = torch.clamp(1.0 - slit1 - slit2, 0.0, 1.0)

    return V_BARRIER * x_profile * y_wall


# ══════════════════════════════════════════════════════════════════════
#  Absorbing Boundary Sponge
# ══════════════════════════════════════════════════════════════════════
def build_sponge(X, Y):
    """Smooth absorbing layer at domain edges."""
    gamma = torch.zeros_like(X)
    sw = SPONGE_WIDTH

    for coord, L in [(X, LX), (Y, LY)]:
        mask_lo = coord < (-L/2 + sw)
        dist_lo = (-L/2 + sw - coord) / sw
        gamma = torch.where(mask_lo, torch.maximum(gamma, SPONGE_GAMMA * dist_lo**2), gamma)

        mask_hi = coord > (L/2 - sw)
        dist_hi = (coord - (L/2 - sw)) / sw
        gamma = torch.where(mask_hi, torch.maximum(gamma, SPONGE_GAMMA * dist_hi**2), gamma)

    return gamma


# ══════════════════════════════════════════════════════════════════════
#  Wavepacket Initialization
# ══════════════════════════════════════════════════════════════════════
def init_wavepacket(X, Y, device):
    """Wide Gaussian wavepacket with momentum kick e^{ik·x}."""
    dx = X - WP_X0
    dy = Y  # centred at y=0
    envelope = WP_AMP * torch.exp(-dx**2 / (2.0 * WP_SIGMA_X**2)
                                  -dy**2 / (2.0 * WP_SIGMA_Y**2))
    phase = WP_KX * X
    return (envelope * torch.exp(1j * phase.to(CDTYPE))).to(CDTYPE)


# ══════════════════════════════════════════════════════════════════════
#  Real-Time Stepper (Strang split-step)
# ══════════════════════════════════════════════════════════════════════
def real_time_step(psi, kinetic_prop, dt, V_ext, sponge_gamma):
    hdt = dt / 2.0

    V = G_NL * torch.abs(psi)**2 - MU + V_ext
    psi = psi * torch.exp(-1j * V * hdt)

    psi = fft.ifftn(fft.fftn(psi) * kinetic_prop)

    V = G_NL * torch.abs(psi)**2 - MU + V_ext
    psi = psi * torch.exp(-1j * V * hdt)

    # Absorbing sponge
    psi = psi * torch.exp(-sponge_gamma * dt)

    return psi


# ══════════════════════════════════════════════════════════════════════
#  Compute Velocity Field (spectral gradient)
# ══════════════════════════════════════════════════════════════════════
def compute_velocity_field(psi, KX, KY):
    """Compute Bohmian velocity field v = j/ρ on the full grid.
    j = Im(ψ* ∇ψ), ρ = |ψ|².  Returns (vx, vy)."""
    psi_k = fft.fftn(psi)
    dpsi_dx = fft.ifftn(1j * KX * psi_k)
    dpsi_dy = fft.ifftn(1j * KY * psi_k)

    jx = torch.imag(psi.conj() * dpsi_dx)
    jy = torch.imag(psi.conj() * dpsi_dy)
    rho = torch.abs(psi)**2

    # Avoid division by zero in vacuum regions
    rho_safe = torch.clamp(rho, min=1e-30)
    vx = jx / rho_safe
    vy = jy / rho_safe

    # Zero out velocity where there's no wavefunction
    mask = rho < 1e-20
    vx[mask] = 0.0
    vy[mask] = 0.0

    return vx, vy


def interp_velocity(vx_field, vy_field, x_arr, y_arr, active):
    """Bilinear interpolation of velocity for all active particles.
    x_arr, y_arr: numpy arrays of particle positions.
    active: boolean mask.
    Returns (dvx, dvy) arrays."""
    n = len(x_arr)
    dvx = np.zeros(n)
    dvy = np.zeros(n)

    for i in range(n):
        if not active[i]:
            continue
        fi = (x_arr[i] + LX/2) / DX
        fj = (y_arr[i] + LY/2) / DY

        fi = max(0.0, min(fi, NX - 1.001))
        fj = max(0.0, min(fj, NY - 1.001))

        i0 = int(fi)
        j0 = int(fj)
        i1 = min(i0 + 1, NX - 1)
        j1 = min(j0 + 1, NY - 1)

        wx = fi - i0
        wy = fj - j0

        dvx[i] = ((1-wx)*(1-wy)*vx_field[i0,j0] + wx*(1-wy)*vx_field[i1,j0] +
                   (1-wx)*wy*vx_field[i0,j1] + wx*wy*vx_field[i1,j1]).item()
        dvy[i] = ((1-wx)*(1-wy)*vy_field[i0,j0] + wx*(1-wy)*vy_field[i1,j0] +
                   (1-wx)*wy*vy_field[i0,j1] + wx*wy*vy_field[i1,j1]).item()

    return dvx, dvy


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  UHF — Hydrodynamic Double-Slit v1                        ║")
    print("║  Emergent Quantum Interference from Pilot-Wave Guidance    ║")
    print("╚══════════════════════════════════════════════════════════════╝",
          flush=True)

    device = setup_device()
    os.makedirs(OUTDIR, exist_ok=True)

    print(f"\n  Grid: {NX}×{NY}, L={LX}ξ, Δx={DX:.4f}ξ")
    print(f"  Barrier: x={BARRIER_X}, thick={BARRIER_THICK}ξ, V={V_BARRIER}")
    print(f"  Slits: width={SLIT_WIDTH}ξ, separation={SLIT_SEP}ξ")
    print(f"  Wavepacket: x₀={WP_X0}ξ, σ_x={WP_SIGMA_X}ξ, "
          f"σ_y={WP_SIGMA_Y}ξ, k={WP_KX}")
    print(f"  Screen: x={SCREEN_X}ξ")
    print(f"  Particles: N={N_PARTICLES}, y₀ ∈ [−{JITTER_RANGE}, "
          f"+{JITTER_RANGE}]ξ")

    de_broglie = 2 * np.pi / WP_KX
    L_screen = SCREEN_X - BARRIER_X
    theory_spacing = de_broglie * L_screen / SLIT_SEP
    print(f"  λ_dB = {de_broglie:.2f}ξ")
    print(f"  Theory fringe spacing = {theory_spacing:.2f}ξ", flush=True)

    X, Y, K2, KX, KY = build_grids(device)
    V_barrier = build_barrier(X, Y)
    sponge = build_sponge(X, Y)
    kinetic_prop = torch.exp(-1j * 0.5 * K2 * DT)

    print(f"  VRAM: {torch.cuda.memory_allocated(device)/1e9:.2f} GB",
          flush=True)

    # ── Initialize wavepacket ──
    psi = init_wavepacket(X, Y, device)
    norm0 = torch.sum(torch.abs(psi)**2).item() * DA
    print(f"  Initial norm = {norm0:.2f}", flush=True)

    # ── Time evolution parameters ──
    travel_time = (SCREEN_X - WP_X0) / WP_KX
    T_MAX = travel_time * 8.0
    n_steps = int(T_MAX / DT)
    vel_every = 5
    sample_every = 25
    flux_every = 10

    # Time-integrated probability flux at screen
    ix_screen = int((SCREEN_X + LX/2) / DX)
    ix_screen = min(ix_screen, NX - 1)
    flux_accum = torch.zeros(NY, device=device, dtype=DTYPE)

    # Bohmian particle seeding: seed AFTER transmitted wave establishes
    # Seed line just past barrier
    SEED_X = BARRIER_X + BARRIER_THICK / 2.0 + 1.0  # 1ξ past barrier edge
    seed_time = (BARRIER_X - WP_X0) / WP_KX + 5.0   # barrier arrival + 5τ
    seed_step = int(seed_time / DT)
    bohmian_seeded = False

    rng = np.random.default_rng(seed=42)
    part_x = np.zeros(N_PARTICLES)
    part_y = np.zeros(N_PARTICLES)
    part_active = np.zeros(N_PARTICLES, dtype=bool)
    part_y_hit = np.full(N_PARTICLES, np.nan)

    n_traj = min(15, N_PARTICLES)
    traj_x = [[] for _ in range(n_traj)]
    traj_y = [[] for _ in range(n_traj)]

    print(f"\n  Travel estimate: {travel_time:.1f}τ, T_MAX={T_MAX:.1f}τ, "
          f"n_steps={n_steps}")
    print(f"  Bohmian seed at t={seed_time:.1f}τ (step {seed_step}), "
          f"x={SEED_X:.1f}ξ")
    print(f"  Evolving wavefunction + {N_PARTICLES} Bohmian particles...",
          flush=True)

    t_start = time.time()
    vx_field = vy_field = None

    for step in range(1, n_steps + 1):
        psi = real_time_step(psi, kinetic_prop, DT, V_barrier, sponge)

        # Accumulate probability flux at screen
        if step % flux_every == 0:
            if ix_screen > 0 and ix_screen < NX - 1:
                dpsi_dx = (psi[ix_screen + 1, :] - psi[ix_screen - 1, :]) / (2 * DX)
            else:
                dpsi_dx = torch.zeros(NY, device=device, dtype=CDTYPE)
            jx_screen = torch.imag(psi[ix_screen, :].conj() * dpsi_dx)
            flux_accum += torch.clamp(jx_screen, min=0.0) * DT * flux_every

        # Seed Bohmian particles from transmitted |ψ|² at seed line
        if step == seed_step and not bohmian_seeded:
            ix_seed = int((SEED_X + LX/2) / DX)
            ix_seed = min(ix_seed, NX - 2)
            rho_seed = (torch.abs(psi[ix_seed, :])**2).cpu().numpy()
            y_seed_1d = np.linspace(-LY/2, LY/2 - DY, NY)
            # Sample from |ψ|² distribution (rejection sampling)
            rho_max = rho_seed.max()
            if rho_max > 0:
                sampled = 0
                attempts = 0
                while sampled < N_PARTICLES and attempts < N_PARTICLES * 200:
                    y_trial = rng.uniform(-LY/2, LY/2)
                    iy_trial = int((y_trial + LY/2) / DY)
                    iy_trial = max(0, min(iy_trial, NY - 1))
                    if rng.uniform() < rho_seed[iy_trial] / rho_max:
                        part_x[sampled] = SEED_X
                        part_y[sampled] = y_trial
                        part_active[sampled] = True
                        sampled += 1
                    attempts += 1
                n_seeded = sampled
            else:
                n_seeded = 0
            bohmian_seeded = True
            # Initialize trajectories
            for i in range(n_traj):
                if i < n_seeded:
                    traj_x[i].append(part_x[i])
                    traj_y[i].append(part_y[i])
            print(f"  Seeded {n_seeded} Bohmian particles from transmitted "
                  f"|ψ|² at x={SEED_X:.1f}ξ, t={seed_time:.1f}τ", flush=True)

        # Update velocity field periodically
        if step % vel_every == 0 and np.any(part_active):
            vx_field, vy_field = compute_velocity_field(psi, KX, KY)

            dvx, dvy = interp_velocity(vx_field, vy_field,
                                       part_x, part_y, part_active)
            part_x[part_active] += dvx[part_active] * DT * vel_every
            part_y[part_active] += dvy[part_active] * DT * vel_every

            # Check screen crossing
            crossed = part_active & (part_x >= SCREEN_X)
            if np.any(crossed):
                part_y_hit[crossed] = part_y[crossed]
                part_active[crossed] = False

            # Check out of bounds
            oob = part_active & ((np.abs(part_x) > LX/2 - 1) |
                                 (np.abs(part_y) > LY/2 - 1))
            part_active[oob] = False

            # Record trajectories
            if step % sample_every == 0:
                for i in range(n_traj):
                    if part_active[i] or not np.isnan(part_y_hit[i]):
                        traj_x[i].append(part_x[i])
                        traj_y[i].append(part_y[i])

        # Progress
        if step % 500 == 0:
            t_now = step * DT
            n_hit = np.sum(~np.isnan(part_y_hit))
            n_act = np.sum(part_active)
            elapsed = time.time() - t_start
            norm_now = torch.sum(torch.abs(psi)**2).item() * DA
            print(f"  t={t_now:6.1f}τ  step={step:5d}  "
                  f"hits={n_hit:3.0f}  active={n_act:3d}  "
                  f"norm={norm_now:.2f}  [{elapsed:.0f}s]", flush=True)

        # Early termination (only after seeding)
        if bohmian_seeded and not np.any(part_active):
            print(f"  All particles stopped at step {step} "
                  f"(t={step*DT:.1f}τ)", flush=True)
            break

    total_time = time.time() - t_start

    # ── Method A: Time-integrated probability flux at screen ──
    flux_np = flux_accum.cpu().numpy()
    y_1d = np.linspace(-LY/2, LY/2 - DY, NY)

    # ── Method B: Bohmian arrivals ──
    hit_mask = ~np.isnan(part_y_hit)
    y_hits = part_y_hit[hit_mask]

    print(f"\n  ═══ RESULTS ═══")
    print(f"  Method A: Time-integrated flux at screen x={SCREEN_X}ξ")
    print(f"  Method B: {len(y_hits)}/{N_PARTICLES} Bohmian particles "
          f"hit screen")
    print(f"  Wall time: {total_time:.0f}s")

    # ══════════════════════════════════════════════════════════════════
    #  Fringe Analysis
    # ══════════════════════════════════════════════════════════════════
    from scipy.ndimage import gaussian_filter1d

    # --- Method A peaks ---
    y_mask = np.abs(y_1d) < 20
    rho_a = flux_np[y_mask]
    y_a = y_1d[y_mask]
    rho_smooth = gaussian_filter1d(rho_a, sigma=5)

    peaks_a = []
    for i in range(1, len(rho_smooth) - 1):
        if (rho_smooth[i] > rho_smooth[i-1] and
            rho_smooth[i] > rho_smooth[i+1] and
            rho_smooth[i] > 0.05 * np.max(rho_smooth)):
            peaks_a.append(y_a[i])

    if len(peaks_a) >= 2:
        peaks_a = np.array(sorted(peaks_a))
        spacings_a = np.diff(peaks_a)
        mean_spacing_a = np.mean(spacings_a)
    else:
        spacings_a = np.array([])
        mean_spacing_a = None

    # --- Method B peaks ---
    if len(y_hits) >= 20:
        hist_vals, bin_edges = np.histogram(y_hits, bins=N_BINS,
                                            range=(-20, 20))
        bin_centres = 0.5 * (bin_edges[:-1] + bin_edges[1:])
        hist_smooth = gaussian_filter1d(hist_vals.astype(float), sigma=1.5)

        peaks_b = []
        for i in range(1, len(hist_smooth) - 1):
            if (hist_smooth[i] > hist_smooth[i-1] and
                hist_smooth[i] > hist_smooth[i+1] and
                hist_smooth[i] > 0.15 * np.max(hist_smooth)):
                peaks_b.append(bin_centres[i])

        if len(peaks_b) >= 2:
            peaks_b = np.array(sorted(peaks_b))
            spacings_b = np.diff(peaks_b)
            mean_spacing_b = np.mean(spacings_b)
        else:
            spacings_b = np.array([])
            mean_spacing_b = None
    else:
        peaks_b = []
        spacings_b = np.array([])
        mean_spacing_b = None

    # ── Fringe Table ──
    print("\n" + "═"*70)
    print("  FRINGE SPACING TABLE")
    print("═"*70)
    print(f"  {'Quantity':<35s} {'Value':>12s}")
    print(f"  {'-'*35:s} {'-'*12:s}")
    print(f"  {'λ_dB (de Broglie wavelength)':<35s} {de_broglie:>10.2f} ξ")
    print(f"  {'Theory: Δy = λ·L/d':<35s} {theory_spacing:>10.2f} ξ")
    if mean_spacing_a is not None:
        print(f"  {'Method A (|ψ|² peaks) spacing':<35s} "
              f"{mean_spacing_a:>10.2f} ξ")
        print(f"  {'  → ratio A/theory':<35s} "
              f"{mean_spacing_a/theory_spacing:>10.3f}")
        print(f"  {'  → N peaks':<35s} {len(peaks_a):>10d}")
    else:
        print(f"  {'Method A: insufficient peaks':<35s} {'N/A':>12s}")
    if mean_spacing_b is not None:
        print(f"  {'Method B (Bohmian hist) spacing':<35s} "
              f"{mean_spacing_b:>10.2f} ξ")
        print(f"  {'  → ratio B/theory':<35s} "
              f"{mean_spacing_b/theory_spacing:>10.3f}")
        print(f"  {'  → N peaks':<35s} {len(peaks_b):>10d}")
    else:
        print(f"  {'Method B: insufficient peaks/hits':<35s} "
              f"{'N/A':>12s}")

    # ── Conclusion ──
    confirmed = False
    if mean_spacing_a is not None and len(peaks_a) >= 3:
        ratio_a = mean_spacing_a / theory_spacing
        # Fresnel near-field (L/d≈2) compresses fringes vs far-field formula;
        # accept ratio 0.5–1.3
        if 0.5 < ratio_a < 1.3:
            confirmed = True

    if confirmed:
        conclusion = (f"GP wavefunction produces {len(peaks_a)}-fringe "
                      f"interference pattern at screen, mean spacing "
                      f"{mean_spacing_a:.2f}ξ vs theory {theory_spacing:.2f}ξ "
                      f"(ratio {mean_spacing_a/theory_spacing:.3f}) — "
                      f"emergent quantum interference confirmed from "
                      f"superfluid dynamics.")
    else:
        conclusion = ("Interference pattern not clearly resolved at "
                      "current parameters — hypothesis not yet confirmed.")

    print(f"\n  CONCLUSION: {conclusion}", flush=True)

    # ══════════════════════════════════════════════════════════════════
    #  Plots
    # ══════════════════════════════════════════════════════════════════
    print("\n  Generating plots...", flush=True)

    # Fraunhofer prediction
    y_cont = np.linspace(-20, 20, 500)
    k = WP_KX
    d = SLIT_SEP
    a = SLIT_WIDTH
    L = L_screen

    alpha_arr = k * a * y_cont / (2 * L)
    beta_arr  = k * d * y_cont / (2 * L)
    sinc_sq = np.where(np.abs(alpha_arr) < 1e-10, 1.0,
                       (np.sin(alpha_arr) / alpha_arr)**2)
    fraunhofer = sinc_sq * np.cos(beta_arr)**2

    # --- Plot 1: Combined figure ---
    fig, axes = plt.subplots(3, 1, figsize=(12, 14))

    # Panel A: wave intensity at screen
    ax = axes[0]
    rho_a_norm = rho_a / np.max(rho_a) if np.max(rho_a) > 0 else rho_a
    ax.plot(y_a, rho_a_norm, 'b-', lw=1.5,
            label='GP time-integrated flux')
    fraunhofer_a = fraunhofer / np.max(fraunhofer)
    ax.plot(y_cont, fraunhofer_a, 'r--', lw=2, alpha=0.6,
            label='Fraunhofer prediction')
    ax.set_xlabel('y [ξ]', fontsize=12)
    ax.set_ylabel('Normalized intensity', fontsize=12)
    ax.set_title('Method A: Time-Integrated Probability Flux at Screen',
                 fontsize=13, fontweight='bold')
    ax.legend(fontsize=11)
    ax.set_xlim(-20, 20)
    ax.grid(True, alpha=0.3)

    # Panel B: Bohmian histogram
    ax = axes[1]
    if len(y_hits) > 0:
        ax.hist(y_hits, bins=N_BINS, range=(-20, 20), color='#1f77b4',
                edgecolor='black', alpha=0.8, density=True,
                label=f'Bohmian arrivals (N={len(y_hits)})')
        hist_vals_plot, _ = np.histogram(y_hits, bins=N_BINS,
                                          range=(-20, 20), density=True)
        if np.max(hist_vals_plot) > 0:
            fraunhofer_b = fraunhofer / np.max(fraunhofer) * np.max(hist_vals_plot)
            ax.plot(y_cont, fraunhofer_b, 'r--', lw=2, alpha=0.6,
                    label='Fraunhofer')
    else:
        ax.text(0.5, 0.5, 'No Bohmian hits', transform=ax.transAxes,
                ha='center', fontsize=14)
    ax.set_xlabel('y on screen [ξ]', fontsize=12)
    ax.set_ylabel('Probability density', fontsize=12)
    ax.set_title('Method B: Bohmian Trajectory Arrivals',
                 fontsize=13, fontweight='bold')
    ax.set_xlim(-20, 20)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)

    # Panel C: Example trajectories
    ax = axes[2]
    sw = SLIT_WIDTH / 2.0
    y1c = SLIT_SEP / 2.0
    y2c = -SLIT_SEP / 2.0
    ax.fill_betweenx([-40, y2c - sw], BARRIER_X - BARRIER_THICK/2,
                     BARRIER_X + BARRIER_THICK/2, color='gray', alpha=0.5)
    ax.fill_betweenx([y2c + sw, y1c - sw], BARRIER_X - BARRIER_THICK/2,
                     BARRIER_X + BARRIER_THICK/2, color='gray', alpha=0.5)
    ax.fill_betweenx([y1c + sw, 40], BARRIER_X - BARRIER_THICK/2,
                     BARRIER_X + BARRIER_THICK/2, color='gray', alpha=0.5)
    ax.axvline(SCREEN_X, color='orange', ls='--', lw=2, label='Screen')

    colors = plt.cm.tab10(np.linspace(0, 1, n_traj))
    for i in range(n_traj):
        if len(traj_x[i]) > 2:
            ax.plot(traj_x[i], traj_y[i], '-', color=colors[i],
                    lw=0.8, alpha=0.7)

    ax.set_xlabel('x [ξ]', fontsize=12)
    ax.set_ylabel('y [ξ]', fontsize=12)
    ax.set_xlim(-5, 20)
    ax.set_ylim(-15, 15)
    ax.set_title('Bohmian Trajectories through Double Slit',
                 fontsize=13, fontweight='bold')
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_aspect('equal')

    plt.tight_layout()
    hist_path = os.path.join(OUTDIR, 'double_slit_histogram_v1.png')
    fig.savefig(hist_path, dpi=200)
    plt.close(fig)
    print(f"  Plot: {hist_path}")

    # ── JSON ──
    jd = {
        'parameters': {
            'NX': NX, 'LX': LX, 'DT': DT,
            'WP_SIGMA_X': WP_SIGMA_X, 'WP_SIGMA_Y': WP_SIGMA_Y,
            'WP_X0': WP_X0, 'WP_KX': WP_KX,
            'SLIT_WIDTH': SLIT_WIDTH, 'SLIT_SEP': SLIT_SEP,
            'V_BARRIER': V_BARRIER, 'SCREEN_X': SCREEN_X,
            'N_PARTICLES': N_PARTICLES,
        },
        'results': {
            'n_bohmian_hits': int(len(y_hits)),
            'method_a_n_peaks': int(len(peaks_a)) if len(peaks_a) > 0 else 0,
            'method_a_spacing': float(mean_spacing_a) if mean_spacing_a else None,
            'method_b_n_peaks': int(len(peaks_b)) if len(peaks_b) > 0 else 0,
            'method_b_spacing': float(mean_spacing_b) if mean_spacing_b else None,
            'theory_spacing': float(theory_spacing),
        },
        'conclusion': conclusion,
        'wall_time_s': total_time,
    }
    jpath = os.path.join(OUTDIR, 'results_v1.json')
    with open(jpath, 'w') as f:
        json.dump(jd, f, indent=2)
    print(f"  JSON: {jpath}")
    print(f"\n  Wall time: {total_time:.0f}s", flush=True)


if __name__ == '__main__':
    main()
