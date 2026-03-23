#!/usr/bin/env python3
"""
UHF — Hydrodynamic Double-Slit v2
===================================
Born-Rule Emergence from Uniform Initial Noise.

2D Split-Step Fourier GP on RTX 3090 (2048×2048, L=80ξ).

Architecture:
  Single pilot wave (GP evolution) + 2000 Bohmian trajectories.
  Particles seeded at transmission peak (t=12τ) with UNIFORM y₀ within slit apertures,
  advected by pilot-wave velocity field v = Im(ψ*∇ψ)/|ψ|².
  Test: does the y_hit histogram converge to |ψ|² at the screen
  despite the flat initial y-distribution?

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
LX = LY = 80.0
XI      = 1.0
RHO_0   = 1.0
G_NL    = 1.0
MU      = G_NL * RHO_0

DX = LX / NX
DY = LY / NY
DA = DX * DY

# Timesteps
DT       = 0.02       # real-time step [τ]

# Wavepacket
WP_SIGMA_X = 4.0
WP_SIGMA_Y = 20.0
WP_X0      = -20.0
WP_KX      = 1.5
WP_AMP     = 1.0

# Double-slit barrier
BARRIER_X     = 0.0
BARRIER_THICK = 2.0
SLIT_WIDTH    = 2.0
SLIT_SEP      = 8.0
V_BARRIER     = 100.0
TANH_WIDTH    = 0.3    # sharp edges for clean slit openings

# Absorbing sponge
SPONGE_WIDTH  = 8.0
SPONGE_GAMMA  = 0.5

# Detection screen
SCREEN_X      = 15.0

# Ensemble
N_ENSEMBLE    = 2000
N_BINS        = 100

# Seeding
SEED_X = BARRIER_X + BARRIER_THICK / 2.0 + 1.0  # 2ξ past barrier centre

OUTDIR = "UHF_DoubleSlit_v2_results"
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
#  Barrier (v1-style: tanh × wall)
# ══════════════════════════════════════════════════════════════════════
def build_barrier(X, Y):
    w = TANH_WIDTH
    ht = BARRIER_THICK / 2.0
    x_profile = 0.5 * (torch.tanh((X - BARRIER_X + ht) / w)
                       - torch.tanh((X - BARRIER_X - ht) / w))
    sw = SLIT_WIDTH / 2.0
    y1c = +SLIT_SEP / 2.0
    y2c = -SLIT_SEP / 2.0
    slit1 = 0.5 * (torch.tanh((Y - y1c + sw) / w)
                   - torch.tanh((Y - y1c - sw) / w))
    slit2 = 0.5 * (torch.tanh((Y - y2c + sw) / w)
                   - torch.tanh((Y - y2c - sw) / w))
    y_wall = torch.clamp(1.0 - slit1 - slit2, 0.0, 1.0)
    return V_BARRIER * x_profile * y_wall


# ══════════════════════════════════════════════════════════════════════
#  Sponge
# ══════════════════════════════════════════════════════════════════════
def build_sponge(X, Y):
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
#  Real-Time Stepper (Strang split-step)
# ══════════════════════════════════════════════════════════════════════
def real_time_step(psi, kinetic_prop, V_ext, sponge_gamma):
    hdt = DT / 2.0
    V = G_NL * torch.abs(psi)**2 - MU + V_ext
    psi = psi * torch.exp(-1j * V * hdt)
    psi = fft.ifftn(fft.fftn(psi) * kinetic_prop)
    V = G_NL * torch.abs(psi)**2 - MU + V_ext
    psi = psi * torch.exp(-1j * V * hdt)
    psi = psi * torch.exp(-sponge_gamma * DT)
    return psi


# ══════════════════════════════════════════════════════════════════════
#  Velocity Field (spectral gradient)
# ══════════════════════════════════════════════════════════════════════
def compute_velocity_field(psi, KX, KY):
    psi_k = fft.fftn(psi)
    dpsi_dx = fft.ifftn(1j * KX * psi_k)
    dpsi_dy = fft.ifftn(1j * KY * psi_k)
    jx = torch.imag(psi.conj() * dpsi_dx)
    jy = torch.imag(psi.conj() * dpsi_dy)
    rho = torch.abs(psi)**2
    rho_safe = torch.clamp(rho, min=1e-30)
    vx = jx / rho_safe
    vy = jy / rho_safe
    mask = rho < 1e-20
    vx[mask] = 0.0
    vy[mask] = 0.0
    return vx, vy


# ══════════════════════════════════════════════════════════════════════
#  Batch Particle Advection (GPU-accelerated)
# ══════════════════════════════════════════════════════════════════════
def advect_particles_gpu(vx_field, vy_field, px, py, active, dt_eff):
    """Advect all active particles using bilinear interpolation on GPU."""
    if not torch.any(active):
        return px, py
    fi = (px + LX/2) / DX
    fj = (py + LY/2) / DY
    fi = torch.clamp(fi, 0.0, NX - 1.001)
    fj = torch.clamp(fj, 0.0, NY - 1.001)

    i0 = fi.long()
    j0 = fj.long()
    i1 = torch.clamp(i0 + 1, max=NX - 1)
    j1 = torch.clamp(j0 + 1, max=NY - 1)
    wx = fi - i0.double()
    wy = fj - j0.double()

    dvx = ((1-wx)*(1-wy)*vx_field[i0, j0] + wx*(1-wy)*vx_field[i1, j0] +
           (1-wx)*wy*vx_field[i0, j1] + wx*wy*vx_field[i1, j1])
    dvy = ((1-wx)*(1-wy)*vy_field[i0, j0] + wx*(1-wy)*vy_field[i1, j0] +
           (1-wx)*wy*vy_field[i0, j1] + wx*wy*vy_field[i1, j1])

    # Clamp extreme velocities
    v_max = 50.0
    dvx = torch.clamp(dvx, -v_max, v_max)
    dvy = torch.clamp(dvy, -v_max, v_max)

    px = px + torch.where(active, dvx * dt_eff, torch.zeros_like(px))
    py = py + torch.where(active, dvy * dt_eff, torch.zeros_like(py))
    return px, py


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  UHF — Hydrodynamic Double-Slit v2                        ║")
    print("║  Born-Rule Emergence from Uniform Initial Noise            ║")
    print("╚══════════════════════════════════════════════════════════════╝",
          flush=True)

    device = setup_device()
    os.makedirs(OUTDIR, exist_ok=True)

    de_broglie = 2 * np.pi / WP_KX
    L_screen = SCREEN_X - BARRIER_X
    theory_spacing = de_broglie * L_screen / SLIT_SEP

    print(f"\n  Grid: {NX}×{NY}, L={LX}ξ, Δx={DX:.4f}ξ")
    print(f"  Barrier: x={BARRIER_X}, thick={BARRIER_THICK}ξ, V={V_BARRIER}")
    print(f"  Slits: width={SLIT_WIDTH}ξ, separation={SLIT_SEP}ξ")
    print(f"  Screen: x={SCREEN_X}ξ,  Seed line: x={SEED_X}ξ")
    print(f"  Ensemble: N={N_ENSEMBLE}")
    print(f"  λ_dB = {de_broglie:.2f}ξ")
    print(f"  Theory fringe spacing = {theory_spacing:.2f}ξ")

    # ── Build grids & operators ──
    X, Y, K2, KX, KY = build_grids(device)
    kinetic_prop = torch.exp(-0.5j * K2.to(CDTYPE) * DT)
    V_barrier = build_barrier(X, Y)
    sponge = build_sponge(X, Y)

    vram = torch.cuda.memory_allocated() / 1e9
    print(f"  VRAM: {vram:.2f} GB")

    # ── Init wavepacket (no vortex — matching v1) ──
    dx_ = X - WP_X0
    envelope = WP_AMP * torch.exp(-dx_**2 / (2 * WP_SIGMA_X**2)
                                  - Y**2 / (2 * WP_SIGMA_Y**2))
    psi = (envelope * torch.exp(1j * WP_KX * X.to(CDTYPE))).to(CDTYPE)
    psi /= torch.sqrt(torch.sum(torch.abs(psi)**2) * DA)

    # ══════════════════════════════════════════════════════════════════
    #  Evolution parameters
    # ══════════════════════════════════════════════════════════════════
    barrier_arrival = (BARRIER_X - WP_X0) / WP_KX   # ~13.3τ

    # Single-shot seeding at transmission peak.
    # From diagnostics: transmitted wave at x=2 peaks at t≈12τ with forward vx.
    SEED_TIME = 12.0
    seed_step = int(SEED_TIME / DT)  # step 600

    # Time budget: particles need to travel from SEED_X to SCREEN_X
    post_travel = (SCREEN_X - SEED_X) / WP_KX  # ~8.7τ
    T_MAX = SEED_TIME + post_travel * 30.0      # generous margin
    n_steps = int(T_MAX / DT)

    vel_every = 5
    vel_dt = DT * vel_every
    flux_every = 10

    # Screen flux accumulation
    ix_screen = int((SCREEN_X + LX/2) / DX)
    ix_screen = min(ix_screen, NX - 1)
    ix_seed = int((SEED_X + LX/2) / DX)
    flux_accum = torch.zeros(NY, device=device, dtype=DTYPE)

    # Particle arrays
    rng = np.random.default_rng(seed=42)
    half = N_ENSEMBLE // 2
    y0_s1 = rng.uniform(SLIT_SEP/2 - SLIT_WIDTH/2,
                        SLIT_SEP/2 + SLIT_WIDTH/2, half)
    y0_s2 = rng.uniform(-SLIT_SEP/2 - SLIT_WIDTH/2,
                        -SLIT_SEP/2 + SLIT_WIDTH/2, N_ENSEMBLE - half)
    all_y0 = np.concatenate([y0_s1, y0_s2])
    rng.shuffle(all_y0)

    px = torch.zeros(N_ENSEMBLE, device=device, dtype=DTYPE)
    py = torch.zeros(N_ENSEMBLE, device=device, dtype=DTYPE)
    active = torch.zeros(N_ENSEMBLE, dtype=torch.bool, device=device)
    y_hit = torch.full((N_ENSEMBLE,), float('nan'), device=device, dtype=DTYPE)
    seeded = False

    n_traj = min(20, N_ENSEMBLE)
    traj_sample_every = 50
    traj_x_list = [[] for _ in range(n_traj)]
    traj_y_list = [[] for _ in range(n_traj)]

    psi_at_seed = None  # will save psi snapshot for control run

    print(f"\n  Barrier arrival: {barrier_arrival:.1f}τ")
    print(f"  Single-shot seed: t={SEED_TIME:.1f}τ, x={SEED_X:.1f}ξ")
    print(f"  Post-seed travel: {post_travel:.1f}τ")
    print(f"  T_MAX = {T_MAX:.1f}τ, n_steps = {n_steps}")
    print(f"  Evolving wavefunction + {N_ENSEMBLE} particles...", flush=True)

    t_start = time.time()

    # ══════════════════════════════════════════════════════════════════
    #  Main Evolution Loop
    # ══════════════════════════════════════════════════════════════════
    for step in range(1, n_steps + 1):
        psi = real_time_step(psi, kinetic_prop, V_barrier, sponge)

        # Accumulate flux at screen
        if step % flux_every == 0:
            if 0 < ix_screen < NX - 1:
                dpsi_dx = (psi[ix_screen+1, :] - psi[ix_screen-1, :]) / (2*DX)
                jx_scr = torch.imag(psi[ix_screen, :].conj() * dpsi_dx)
                flux_accum += torch.clamp(jx_scr, min=0.0) * DT * flux_every

        # Single-shot seed at transmission peak
        if step == seed_step and not seeded:
            psi_at_seed = psi.clone()  # save for control run
            px[:] = SEED_X
            py[:] = torch.tensor(all_y0, device=device, dtype=DTYPE)
            active[:] = True
            seeded = True
            print(f"  → Seeded {N_ENSEMBLE} particles at t={SEED_TIME:.1f}τ",
                  flush=True)

        # Advect particles
        if step % vel_every == 0 and seeded and torch.any(active):
            vx_f, vy_f = compute_velocity_field(psi, KX, KY)
            px, py = advect_particles_gpu(vx_f, vy_f, px, py, active, vel_dt)

            # Screen crossing
            crossed = active & (px >= SCREEN_X)
            if torch.any(crossed):
                y_hit[crossed] = py[crossed]
                active[crossed] = False

            # Out of bounds
            oob = active & ((torch.abs(px) > LX/2 - 1) |
                            (torch.abs(py) > LY/2 - 1))
            active[oob] = False

            # Backward-moving: kill if pushed far behind seed
            backward = active & (px < SEED_X - 5.0)
            active[backward] = False

            # Trajectories
            if step % traj_sample_every == 0:
                px_cpu = px[:n_traj].cpu()
                py_cpu = py[:n_traj].cpu()
                for i in range(n_traj):
                    if active[i] or not torch.isnan(y_hit[i]):
                        traj_x_list[i].append(px_cpu[i].item())
                        traj_y_list[i].append(py_cpu[i].item())

        # Progress
        if step % 2000 == 0:
            t_now = step * DT
            n_hit = int(torch.sum(~torch.isnan(y_hit)).item())
            n_act = int(torch.sum(active).item())
            elapsed = time.time() - t_start
            norm_now = torch.sum(torch.abs(psi)**2).item() * DA
            rho_seed = torch.abs(psi[ix_seed, :])**2
            rho_max_seed = rho_seed.max().item()
            print(f"  t={t_now:7.1f}τ  step={step:6d}  "
                  f"hits={n_hit:5d}  active={n_act:5d}  "
                  f"ρ_seed={rho_max_seed:.2e}  norm={norm_now:.2f}  "
                  f"[{elapsed:.0f}s]", flush=True)

        # Early termination
        if seeded and not torch.any(active):
            t_now = step * DT
            if t_now > SEED_TIME + post_travel:
                print(f"  All particles resolved at step {step} "
                      f"(t={t_now:.1f}τ)", flush=True)
                break

    total_time = time.time() - t_start

    # ══════════════════════════════════════════════════════════════════
    #  Results
    # ══════════════════════════════════════════════════════════════════
    y_hit_np = y_hit.cpu().numpy()
    hit_mask = ~np.isnan(y_hit_np)
    y_hits = y_hit_np[hit_mask]

    flux_np = flux_accum.cpu().numpy()
    y_1d = np.linspace(-LY/2, LY/2 - DY, NY)

    print(f"\n  ═══ RESULTS ═══")
    print(f"  Total seeded: {N_ENSEMBLE}")
    print(f"  Total hits: {len(y_hits)}/{N_ENSEMBLE}")
    print(f"  Wall time: {total_time:.0f}s")

    # ══════════════════════════════════════════════════════════════════
    #  Analysis
    # ══════════════════════════════════════════════════════════════════
    from scipy.ndimage import gaussian_filter1d
    from scipy.stats import pearsonr

    y_range = 25.0
    y_mask = np.abs(y_1d) < y_range
    flux_env = flux_np[y_mask]
    y_env = y_1d[y_mask]
    flux_smooth = gaussian_filter1d(flux_env, sigma=3)

    peaks_a = []
    for i in range(1, len(flux_smooth) - 1):
        if (flux_smooth[i] > flux_smooth[i-1] and
            flux_smooth[i] > flux_smooth[i+1] and
            flux_smooth[i] > 0.05 * np.max(flux_smooth)):
            peaks_a.append(y_env[i])

    if len(peaks_a) >= 2:
        peaks_a = np.array(sorted(peaks_a))
        spacings_a = np.diff(peaks_a)
        mean_spacing_a = np.mean(spacings_a)
    else:
        spacings_a = np.array([])
        mean_spacing_a = None

    if len(y_hits) >= 20:
        hist_vals, bin_edges = np.histogram(y_hits, bins=N_BINS,
                                            range=(-y_range, y_range))
        bin_centres = 0.5 * (bin_edges[:-1] + bin_edges[1:])

        flux_at_bins = np.interp(bin_centres, y_env, flux_smooth)

        hist_norm = hist_vals / np.max(hist_vals) if np.max(hist_vals) > 0 \
                    else hist_vals.astype(float)
        flux_norm = flux_at_bins / np.max(flux_at_bins) if np.max(flux_at_bins) > 0 \
                    else flux_at_bins

        sig_mask = flux_norm > 0.01
        if np.sum(sig_mask) > 5:
            r_val, p_val = pearsonr(hist_norm[sig_mask], flux_norm[sig_mask])
            r_sq = r_val**2
        else:
            r_val, r_sq, p_val = None, None, None

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
        r_val = r_sq = p_val = None
        peaks_b = []
        spacings_b = np.array([])
        mean_spacing_b = None
        hist_vals = np.zeros(N_BINS)
        bin_centres = np.linspace(-y_range, y_range, N_BINS)
        hist_norm = np.zeros(N_BINS)
        flux_norm = np.zeros(N_BINS)

    # ── Fringe Table ──
    print("\n" + "═"*70)
    print("  FRINGE SPACING & CORRELATION TABLE")
    print("═"*70)
    print(f"  {'Quantity':<40s} {'Value':>12s}")
    print(f"  {'-'*40:s} {'-'*12:s}")
    print(f"  {'N_hits (arrivals at screen)':<40s} {len(y_hits):>10d}")
    print(f"  {'λ_dB (de Broglie wavelength)':<40s} {de_broglie:>10.2f} ξ")
    print(f"  {'Theory: Δy = λ·L/d (Fraunhofer)':<40s} "
          f"{theory_spacing:>10.2f} ξ")
    if mean_spacing_a is not None:
        print(f"  {'|ψ|² envelope peak spacing':<40s} "
              f"{mean_spacing_a:>10.2f} ξ")
        print(f"  {'  → N peaks':<40s} {len(peaks_a):>10d}")
    if mean_spacing_b is not None:
        print(f"  {'Histogram peak spacing':<40s} "
              f"{mean_spacing_b:>10.2f} ξ")
        print(f"  {'  → N peaks':<40s} {len(peaks_b):>10d}")
    if r_val is not None:
        print(f"  {'Pearson r (hist vs |ψ|²)':<40s} {r_val:>10.4f}")
        print(f"  {'R² (hist vs |ψ|²)':<40s} {r_sq:>10.4f}")
        print(f"  {'p-value':<40s} {p_val:>10.2e}")

    # ══════════════════════════════════════════════════════════════════
    #  CONTROL: |ψ|²-seeded particles (equivariance check)
    # ══════════════════════════════════════════════════════════════════
    print("\n  Running control (|ψ|² seeding)...", flush=True)
    # Use saved psi snapshot from seed time (no need to re-evolve)
    psi_ctrl = psi_at_seed.clone()

    # Rejection-sample from |ψ|² at seed line
    rho_ctrl = torch.abs(psi_ctrl[ix_seed, :])**2
    rho_ctrl_np = rho_ctrl.cpu().numpy()
    rho_ctrl_max = rho_ctrl_np.max()
    y_all_np = np.linspace(-LY/2, LY/2 - DY, NY)

    rng_ctrl = np.random.default_rng(seed=123)
    ctrl_y0 = []
    while len(ctrl_y0) < N_ENSEMBLE:
        yt_batch = rng_ctrl.uniform(-LY/2, LY/2, N_ENSEMBLE * 10)
        for yt in yt_batch:
            jidx = min(int((yt + LY/2) / DY), NY - 1)
            if rng_ctrl.random() < rho_ctrl_np[jidx] / rho_ctrl_max:
                ctrl_y0.append(yt)
                if len(ctrl_y0) >= N_ENSEMBLE:
                    break
    ctrl_y0 = np.array(ctrl_y0[:N_ENSEMBLE])

    cpx = torch.full((N_ENSEMBLE,), SEED_X, device=device, dtype=DTYPE)
    cpy = torch.tensor(ctrl_y0, device=device, dtype=DTYPE)
    c_active = torch.ones(N_ENSEMBLE, dtype=torch.bool, device=device)
    c_yhit = torch.full((N_ENSEMBLE,), float('nan'), device=device, dtype=DTYPE)

    ctrl_nsteps = int(T_MAX / DT)
    for cstep in range(seed_step + 1, ctrl_nsteps + 1):
        psi_ctrl = real_time_step(psi_ctrl, kinetic_prop, V_barrier, sponge)
        if cstep % vel_every == 0 and torch.any(c_active):
            cvx, cvy = compute_velocity_field(psi_ctrl, KX, KY)
            cpx, cpy = advect_particles_gpu(cvx, cvy, cpx, cpy, c_active, vel_dt)
            crossed = c_active & (cpx >= SCREEN_X)
            c_yhit[crossed] = cpy[crossed]; c_active[crossed] = False
            oob = c_active & ((torch.abs(cpx) > LX/2-1) | (torch.abs(cpy) > LY/2-1))
            c_active[oob] = False
            backward = c_active & (cpx < SEED_X - 5.0)
            c_active[backward] = False
        if not torch.any(c_active) and cstep * DT > 30:
            break

    ctrl_hits = c_yhit[~torch.isnan(c_yhit)].cpu().numpy()
    if len(ctrl_hits) >= 20:
        ch_vals, ch_edges = np.histogram(ctrl_hits, bins=N_BINS,
                                          range=(-y_range, y_range))
        ch_centres = 0.5 * (ch_edges[:-1] + ch_edges[1:])
        ch_flux = np.interp(ch_centres, y_env, flux_smooth)
        ch_norm = ch_vals / max(ch_vals.max(), 1)
        cf_norm = ch_flux / max(ch_flux.max(), 1e-30)
        csig = cf_norm > 0.01
        if csig.sum() > 5:
            ctrl_r, ctrl_p = pearsonr(ch_norm[csig], cf_norm[csig])
            ctrl_r2 = ctrl_r**2
        else:
            ctrl_r, ctrl_r2, ctrl_p = None, None, None
    else:
        ctrl_r, ctrl_r2, ctrl_p = None, None, None

    print(f"  Control hits: {len(ctrl_hits)}/{N_ENSEMBLE}")
    if ctrl_r2 is not None:
        print(f"  Control R² (|ψ|²→|ψ|²): {ctrl_r2:.4f}")
        print(f"  {'Control Pearson r':<40s} {ctrl_r:>10.4f}")
        print(f"  {'Control R²':<40s} {ctrl_r2:>10.4f}")

    # ── Conclusion ──
    if r_sq is not None and r_sq > 0.5 and len(y_hits) >= 50:
        conclusion = (f"Born rule emerges from uniform initial noise via "
                      f"hydrodynamic Bohmian guidance "
                      f"(R² = {r_sq:.2f}, N_hits = {len(y_hits)})")
    else:
        ctrl_note = (f", control R²={ctrl_r2:.2f}"
                     if ctrl_r2 is not None else "")
        conclusion = (f"No Born-rule emergence from uniform noise "
                      f"(R²={r_sq:.4f}, N={len(y_hits)}{ctrl_note}); "
                      f"equivariance confirmed for |ψ|²-seeded control"
                      if r_sq is not None and ctrl_r2 is not None and ctrl_r2 > 0.5
                      else f"Hypothesis falsified (R²={r_sq:.4f})"
                      if r_sq is not None
                      else f"Insufficient hits ({len(y_hits)})")

    print(f"\n  CONCLUSION: {conclusion}", flush=True)

    # ══════════════════════════════════════════════════════════════════
    #  Plots
    # ══════════════════════════════════════════════════════════════════
    print("\n  Generating plots...", flush=True)

    y_cont = np.linspace(-y_range, y_range, 500)
    k = WP_KX
    d = SLIT_SEP
    a = SLIT_WIDTH
    L = L_screen
    alpha_arr = k * a * y_cont / (2 * L)
    beta_arr  = k * d * y_cont / (2 * L)
    sinc_sq = np.where(np.abs(alpha_arr) < 1e-10, 1.0,
                       (np.sin(alpha_arr) / alpha_arr)**2)
    fraunhofer = sinc_sq * np.cos(beta_arr)**2

    fig, axes = plt.subplots(4, 1, figsize=(14, 20))

    ax = axes[0]
    flux_plot = flux_env / np.max(flux_env) if np.max(flux_env) > 0 else flux_env
    ax.plot(y_env, flux_plot, 'b-', lw=1.5,
            label='GP time-integrated flux (|ψ|²)')
    fraunhofer_norm = fraunhofer / np.max(fraunhofer)
    ax.plot(y_cont, fraunhofer_norm, 'r--', lw=2, alpha=0.6,
            label='Fraunhofer prediction')
    ax.set_xlabel('y [ξ]', fontsize=12)
    ax.set_ylabel('Normalized intensity', fontsize=12)
    ax.set_title('Reference |ψ|² Interference Pattern at Screen',
                 fontsize=13, fontweight='bold')
    ax.legend(fontsize=11)
    ax.set_xlim(-y_range, y_range)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    if len(y_hits) > 0:
        ax.hist(y_hits, bins=N_BINS, range=(-y_range, y_range),
                color='#1f77b4', edgecolor='black', alpha=0.8, density=True,
                label=f'Uniform y₀ (N={len(y_hits)}/{N_ENSEMBLE})')
        hist_dens, _ = np.histogram(y_hits, bins=N_BINS,
                                     range=(-y_range, y_range), density=True)
        peak_hist = np.max(hist_dens) if np.max(hist_dens) > 0 else 1.0
        flux_overlay = flux_plot * peak_hist
        ax.plot(y_env, flux_overlay, 'r-', lw=2, alpha=0.7,
                label=f'|ψ|² envelope (R²={r_sq:.3f})' if r_sq else '|ψ|²')
    ax.set_xlabel('y on screen [ξ]', fontsize=12)
    ax.set_ylabel('Probability density', fontsize=12)
    ax.set_title(f'TEST: Uniform y₀ → Screen (R²={r_sq:.3f})'
                 if r_sq else 'TEST: Uniform y₀ → Screen',
                 fontsize=13, fontweight='bold')
    ax.set_xlim(-y_range, y_range)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)

    # Panel C: Control histogram (|ψ|²-seeded)
    ax = axes[2]
    if len(ctrl_hits) > 0:
        ax.hist(ctrl_hits, bins=N_BINS, range=(-y_range, y_range),
                color='#2ca02c', edgecolor='black', alpha=0.8, density=True,
                label=f'|ψ|²-seeded (N={len(ctrl_hits)}/{N_ENSEMBLE})')
        ch_dens, _ = np.histogram(ctrl_hits, bins=N_BINS,
                                   range=(-y_range, y_range), density=True)
        cpeak = np.max(ch_dens) if np.max(ch_dens) > 0 else 1.0
        ax.plot(y_env, flux_plot * cpeak, 'r-', lw=2, alpha=0.7,
                label=f'|ψ|² envelope (R²={ctrl_r2:.3f})'
                if ctrl_r2 else '|ψ|²')
    ax.set_xlabel('y on screen [ξ]', fontsize=12)
    ax.set_ylabel('Probability density', fontsize=12)
    ax.set_title(f'CONTROL: |ψ|²-seeded → Screen (R²={ctrl_r2:.3f})'
                 if ctrl_r2 else 'CONTROL: |ψ|²-seeded → Screen',
                 fontsize=13, fontweight='bold')
    ax.set_xlim(-y_range, y_range)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)

    ax = axes[3]
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

    colors = plt.cm.tab20(np.linspace(0, 1, n_traj))
    for i in range(n_traj):
        if len(traj_x_list[i]) > 2:
            ax.plot(traj_x_list[i], traj_y_list[i], '-', color=colors[i],
                    lw=0.7, alpha=0.7)

    ax.set_xlabel('x [ξ]', fontsize=12)
    ax.set_ylabel('y [ξ]', fontsize=12)
    ax.set_xlim(SEED_X - 2, SCREEN_X + 5)
    ax.set_ylim(-20, 20)
    ax.set_title('Example Bohmian Trajectories',
                 fontsize=13, fontweight='bold')
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    hist_path = os.path.join(OUTDIR, 'double_slit_ensemble_histogram_v2.png')
    fig.savefig(hist_path, dpi=200)
    plt.close(fig)
    print(f"  Plot: {hist_path}")

    fig2, ax2 = plt.subplots(figsize=(8, 4))
    ax2.hist(all_y0, bins=50, range=(-SLIT_SEP/2-SLIT_WIDTH, SLIT_SEP/2+SLIT_WIDTH),
             color='green', alpha=0.7, edgecolor='black',
             label=f'Initial y₀ (Uniform in slits, N={N_ENSEMBLE})')
    ax2.axhline(N_ENSEMBLE / 50, color='red', ls='--', lw=2,
                label='Expected (flat per slit)')
    ax2.set_xlabel('y₀ [ξ]', fontsize=12)
    ax2.set_ylabel('Count', fontsize=12)
    ax2.set_title('Initial y₀ Distribution (Uniform within slit apertures)',
                  fontsize=13, fontweight='bold')
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    init_path = os.path.join(OUTDIR, 'initial_y0_distribution_v2.png')
    fig2.savefig(init_path, dpi=150)
    plt.close(fig2)
    print(f"  Plot: {init_path}")

    jd = {
        'parameters': {
            'NX': NX, 'LX': LX, 'DT': DT,
            'WP_SIGMA_X': WP_SIGMA_X, 'WP_SIGMA_Y': WP_SIGMA_Y,
            'WP_X0': WP_X0, 'WP_KX': WP_KX,
            'SLIT_WIDTH': SLIT_WIDTH, 'SLIT_SEP': SLIT_SEP,
            'V_BARRIER': V_BARRIER, 'SCREEN_X': SCREEN_X,
            'N_ENSEMBLE': N_ENSEMBLE,
            'TANH_WIDTH': TANH_WIDTH,
            'SEED_X': SEED_X,
            'SEED_TIME': SEED_TIME,
        },
        'results': {
            'n_seeded': N_ENSEMBLE,
            'n_hits': int(len(y_hits)),
            'envelope_n_peaks': int(len(peaks_a)),
            'envelope_spacing': float(mean_spacing_a) if mean_spacing_a else None,
            'histogram_n_peaks': int(len(peaks_b)) if len(peaks_b) > 0 else 0,
            'histogram_spacing': float(mean_spacing_b) if mean_spacing_b else None,
            'theory_spacing': float(theory_spacing),
            'pearson_r': float(r_val) if r_val is not None else None,
            'r_squared': float(r_sq) if r_sq is not None else None,
            'p_value': float(p_val) if p_val is not None else None,
            'control_n_hits': int(len(ctrl_hits)),
            'control_r_squared': float(ctrl_r2) if ctrl_r2 is not None else None,
            'control_pearson_r': float(ctrl_r) if ctrl_r is not None else None,
        },
        'conclusion': conclusion,
        'wall_time_s': total_time,
    }
    jpath = os.path.join(OUTDIR, 'results_v2.json')
    with open(jpath, 'w') as f:
        json.dump(jd, f, indent=2)
    print(f"  JSON: {jpath}")
    print(f"\n  Wall time: {total_time:.0f}s", flush=True)


if __name__ == '__main__':
    main()
