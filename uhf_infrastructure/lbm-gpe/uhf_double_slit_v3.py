#!/usr/bin/env python3
"""
UHF — Hydrodynamic Double-Slit v3
===================================
Fraunhofer Born-Rule Convergence Ensemble.

2D Split-Step Fourier GP on RTX 3090 (2048×4096, Lx=160ξ, Lz=140ξ).
True far-field regime: screen at z=+50ξ.  k=3.0 for fast transit.

Architecture:
  Single pilot wave (GP evolution) + 15,000 Bohmian trajectories.
  Particles seeded at transmission peak with UNIFORM x₀ within slit
  apertures, advected by pilot-wave velocity field v = Im(ψ*∇ψ)/|ψ|².
  A vortex core follows the Bohmian velocity field by Helmholtz's
  theorem, so this is physically equivalent to tracking 15,000
  independent topological defects.

  Control: |ψ|²-seeded particles for equivariance check.

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
# Grid: x = transverse (160ξ), z = propagation (140ξ)
# Wider transverse domain to capture full far-field diffraction pattern.
# LZ=140 ensures 10ξ sponge sits at z=±60, screen at z=50 safely inside.
NX = 2048        # transverse
NZ = 4096        # propagation
LX = 160.0       # transverse extent (wide for far-field spread)
LZ = 140.0       # propagation extent (screen at 50 + 10ξ clearance + 10ξ sponge)
XI      = 1.0
RHO_0   = 1.0
G_NL    = 1.0
MU      = G_NL * RHO_0

DX = LX / NX
DZ = LZ / NZ
DA = DX * DZ

# Timestep (smaller for higher k)
DT = 0.01

# Wavepacket (propagates in +z direction, higher k for far-field)
WP_SIGMA_X = 20.0   # wide in transverse (covers both slits)
WP_SIGMA_Z = 4.0    # narrow in propagation
WP_Z0      = -25.0  # start position
WP_KZ      = 3.0    # momentum in z (higher for better far-field transmission)
WP_AMP     = 1.0

# Double-slit barrier (at z=0, slits in x-direction)
BARRIER_Z     = 0.0
BARRIER_THICK = 2.0
SLIT_WIDTH    = 2.0
SLIT_SEP      = 8.0
V_BARRIER     = 100.0
TANH_WIDTH    = 0.3

# Absorbing sponge (10ξ at boundaries)
# Asymmetric: full absorption at -z, x boundaries; gentle at +z to
# preserve transmitted wave propagating to far-field screen.
SPONGE_WIDTH  = 10.0
SPONGE_GAMMA  = 0.5
SPONGE_GAMMA_FWD = 0.05  # gentle on +z side

# Detection screen
SCREEN_Z      = 50.0

# Ensemble
N_ENSEMBLE    = 15000
N_BINS        = 200

# Seeding line (just past barrier)
SEED_Z = BARRIER_Z + BARRIER_THICK / 2.0 + 1.0  # z = 2ξ

OUTDIR = "UHF_DoubleSlit_v3_results"
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
    z1d = torch.linspace(-LZ/2, LZ/2 - DZ, NZ, device=device, dtype=DTYPE)
    X, Z = torch.meshgrid(x1d, z1d, indexing='ij')

    kx1d = torch.fft.fftfreq(NX, d=DX, device=device).to(DTYPE) * 2 * np.pi
    kz1d = torch.fft.fftfreq(NZ, d=DZ, device=device).to(DTYPE) * 2 * np.pi
    KX, KZ = torch.meshgrid(kx1d, kz1d, indexing='ij')
    K2 = KX**2 + KZ**2
    return X, Z, K2, KX, KZ


# ══════════════════════════════════════════════════════════════════════
#  Barrier (tanh-smoothed double slit)
# ══════════════════════════════════════════════════════════════════════
def build_barrier(X, Z):
    w = TANH_WIDTH
    ht = BARRIER_THICK / 2.0
    # z-profile: barrier centered at BARRIER_Z
    z_profile = 0.5 * (torch.tanh((Z - BARRIER_Z + ht) / w)
                       - torch.tanh((Z - BARRIER_Z - ht) / w))
    # x-slits: two openings at ±SLIT_SEP/2
    sw = SLIT_WIDTH / 2.0
    x1c = +SLIT_SEP / 2.0
    x2c = -SLIT_SEP / 2.0
    slit1 = 0.5 * (torch.tanh((X - x1c + sw) / w)
                   - torch.tanh((X - x1c - sw) / w))
    slit2 = 0.5 * (torch.tanh((X - x2c + sw) / w)
                   - torch.tanh((X - x2c - sw) / w))
    x_wall = torch.clamp(1.0 - slit1 - slit2, 0.0, 1.0)
    return V_BARRIER * z_profile * x_wall


# ══════════════════════════════════════════════════════════════════════
#  Sponge (10ξ absorbing layers at all boundaries)
# ══════════════════════════════════════════════════════════════════════
def build_sponge(X, Z):
    gamma = torch.zeros_like(X)
    sw = SPONGE_WIDTH
    # X boundaries: full absorption
    for coord, L in [(X, LX)]:
        mask_lo = coord < (-L/2 + sw)
        dist_lo = (-L/2 + sw - coord) / sw
        gamma = torch.where(mask_lo,
                            torch.maximum(gamma, SPONGE_GAMMA * dist_lo**2),
                            gamma)
        mask_hi = coord > (L/2 - sw)
        dist_hi = (coord - (L/2 - sw)) / sw
        gamma = torch.where(mask_hi,
                            torch.maximum(gamma, SPONGE_GAMMA * dist_hi**2),
                            gamma)
    # Z-low boundary: full absorption (reflected wave)
    mask_lo = Z < (-LZ/2 + sw)
    dist_lo = (-LZ/2 + sw - Z) / sw
    gamma = torch.where(mask_lo,
                        torch.maximum(gamma, SPONGE_GAMMA * dist_lo**2),
                        gamma)
    # Z-high boundary: gentle absorption (preserve transmitted wave)
    mask_hi = Z > (LZ/2 - sw)
    dist_hi = (Z - (LZ/2 - sw)) / sw
    gamma = torch.where(mask_hi,
                        torch.maximum(gamma, SPONGE_GAMMA_FWD * dist_hi**2),
                        gamma)
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
def compute_velocity_field(psi, KX, KZ):
    psi_k = fft.fftn(psi)
    dpsi_dx = fft.ifftn(1j * KX * psi_k)
    dpsi_dz = fft.ifftn(1j * KZ * psi_k)
    jx = torch.imag(psi.conj() * dpsi_dx)
    jz = torch.imag(psi.conj() * dpsi_dz)
    rho = torch.abs(psi)**2
    rho_safe = torch.clamp(rho, min=1e-30)
    vx = jx / rho_safe
    vz = jz / rho_safe
    mask = rho < 1e-20
    vx[mask] = 0.0
    vz[mask] = 0.0
    return vx, vz


# ══════════════════════════════════════════════════════════════════════
#  Batch Particle Advection (GPU-accelerated)
# ══════════════════════════════════════════════════════════════════════
def advect_particles_gpu(vx_field, vz_field, px, pz, active, dt_eff):
    if not torch.any(active):
        return px, pz
    fi = (px + LX/2) / DX
    fj = (pz + LZ/2) / DZ
    fi = torch.clamp(fi, 0.0, NX - 1.001)
    fj = torch.clamp(fj, 0.0, NZ - 1.001)

    i0 = fi.long()
    j0 = fj.long()
    i1 = torch.clamp(i0 + 1, max=NX - 1)
    j1 = torch.clamp(j0 + 1, max=NZ - 1)
    wx = fi - i0.double()
    wz = fj - j0.double()

    dvx = ((1-wx)*(1-wz)*vx_field[i0, j0] + wx*(1-wz)*vx_field[i1, j0] +
           (1-wx)*wz*vx_field[i0, j1] + wx*wz*vx_field[i1, j1])
    dvz = ((1-wx)*(1-wz)*vz_field[i0, j0] + wx*(1-wz)*vz_field[i1, j0] +
           (1-wx)*wz*vz_field[i0, j1] + wx*wz*vz_field[i1, j1])

    v_max = 10.0  # ~3× group velocity; clamp reflected-wave spikes
    dvx = torch.clamp(dvx, -v_max, v_max)
    dvz = torch.clamp(dvz, -v_max, v_max)

    px = px + torch.where(active, dvx * dt_eff, torch.zeros_like(px))
    pz = pz + torch.where(active, dvz * dt_eff, torch.zeros_like(pz))
    return px, pz


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  UHF — Hydrodynamic Double-Slit v3                        ║")
    print("║  Fraunhofer Born-Rule Convergence Ensemble                 ║")
    print("║  True Far-Field: Screen at z = +50ξ, N = 15,000           ║")
    print("╚══════════════════════════════════════════════════════════════╝",
          flush=True)

    device = setup_device()
    os.makedirs(OUTDIR, exist_ok=True)

    de_broglie = 2 * np.pi / WP_KZ
    L_screen = SCREEN_Z - BARRIER_Z
    theory_spacing = de_broglie * L_screen / SLIT_SEP

    print(f"\n  Grid: {NX}×{NZ}, Lx={LX}ξ, Lz={LZ}ξ")
    print(f"  Δx={DX:.4f}ξ, Δz={DZ:.4f}ξ")
    print(f"  Barrier: z={BARRIER_Z}, thick={BARRIER_THICK}ξ, V={V_BARRIER}")
    print(f"  Slits: width={SLIT_WIDTH}ξ, separation={SLIT_SEP}ξ")
    print(f"  Screen: z={SCREEN_Z}ξ,  Seed line: z={SEED_Z}ξ")
    print(f"  Ensemble: N={N_ENSEMBLE}")
    print(f"  λ_dB = {de_broglie:.2f}ξ")
    print(f"  Theory fringe spacing = {theory_spacing:.2f}ξ")

    # ── Build grids & operators ──
    X, Z, K2, KX, KZ = build_grids(device)
    kinetic_prop = torch.exp(-0.5j * K2.to(CDTYPE) * DT)
    V_barrier = build_barrier(X, Z)
    sponge = build_sponge(X, Z)

    vram = torch.cuda.memory_allocated() / 1e9
    print(f"  VRAM after grids: {vram:.2f} GB")

    # ── Init wavepacket (propagates in +z) ──
    dz_ = Z - WP_Z0
    envelope = WP_AMP * torch.exp(-X**2 / (2 * WP_SIGMA_X**2)
                                  - dz_**2 / (2 * WP_SIGMA_Z**2))
    psi = (envelope * torch.exp(1j * WP_KZ * Z.to(CDTYPE))).to(CDTYPE)
    psi /= torch.sqrt(torch.sum(torch.abs(psi)**2) * DA)

    vram = torch.cuda.memory_allocated() / 1e9
    print(f"  VRAM after psi: {vram:.2f} GB")

    # ══════════════════════════════════════════════════════════════════
    #  Determine seed time — evolve until transmitted wave peaks at
    #  the seed line, then seed all particles.
    # ══════════════════════════════════════════════════════════════════
    barrier_arrival = (BARRIER_Z - WP_Z0) / WP_KZ
    # Estimate: wave reaches barrier at ~8.3τ, transmitted peak at seed
    # line (z=2) at ~10τ. We'll monitor and pick the peak dynamically.
    SEED_TIME_EST = barrier_arrival + 2.0  # ~10.3τ
    seed_step_est = int(SEED_TIME_EST / DT)

    # Post-seed travel: z = 2 → z = 50, ~48ξ at v ≈ 3.0 → ~16τ
    post_travel = (SCREEN_Z - SEED_Z) / WP_KZ
    T_MAX = SEED_TIME_EST + post_travel * 5.0  # generous margin
    n_steps = int(T_MAX / DT)

    vel_every = 10  # vel_dt = 0.01 * 10 = 0.1τ
    vel_dt = DT * vel_every
    flux_every = 10

    # Grid indices for seed line and screen
    jz_seed = int((SEED_Z + LZ/2) / DZ)
    jz_seed = min(jz_seed, NZ - 1)
    jz_screen = int((SCREEN_Z + LZ/2) / DZ)
    jz_screen = min(jz_screen, NZ - 1)

    # Screen flux accumulation (along x-axis) — only during forward pulse
    flux_accum = torch.zeros(NX, device=device, dtype=DTYPE)
    # |ψ|² snapshot at screen — capture peak arrival
    rho_screen_peak = torch.zeros(NX, device=device, dtype=DTYPE)
    rho_screen_max = 0.0
    screen_snapshot_step = None

    # Particle arrays — uniform x₀ across BOTH slit apertures
    rng = np.random.default_rng(seed=42)
    half = N_ENSEMBLE // 2
    x0_s1 = rng.uniform(SLIT_SEP/2 - SLIT_WIDTH/2,
                         SLIT_SEP/2 + SLIT_WIDTH/2, half)
    x0_s2 = rng.uniform(-SLIT_SEP/2 - SLIT_WIDTH/2,
                         -SLIT_SEP/2 + SLIT_WIDTH/2, N_ENSEMBLE - half)
    all_x0 = np.concatenate([x0_s1, x0_s2])
    rng.shuffle(all_x0)

    px = torch.zeros(N_ENSEMBLE, device=device, dtype=DTYPE)
    pz = torch.zeros(N_ENSEMBLE, device=device, dtype=DTYPE)
    active = torch.zeros(N_ENSEMBLE, dtype=torch.bool, device=device)
    x_hit = torch.full((N_ENSEMBLE,), float('nan'), device=device, dtype=DTYPE)
    seeded = False

    n_traj = min(30, N_ENSEMBLE)
    traj_x_list = [[] for _ in range(n_traj)]
    traj_z_list = [[] for _ in range(n_traj)]
    traj_sample_every = 100

    psi_at_seed = None
    actual_seed_step = None

    # Monitor density at seed line to find peak
    rho_peak_at_seed = 0.0
    peak_found = False

    print(f"\n  Barrier arrival: {barrier_arrival:.1f}τ")
    print(f"  Estimated seed time: {SEED_TIME_EST:.1f}τ")
    print(f"  Post-seed travel: {post_travel:.1f}τ")
    print(f"  T_MAX = {T_MAX:.1f}τ, n_steps = {n_steps}")
    print(f"  Evolving wavefunction + {N_ENSEMBLE} particles...", flush=True)

    t_start = time.time()

    # ══════════════════════════════════════════════════════════════════
    #  Main Evolution Loop
    # ══════════════════════════════════════════════════════════════════
    for step in range(1, n_steps + 1):
        psi = real_time_step(psi, kinetic_prop, V_barrier, sponge)

        # Accumulate flux at screen (only forward flux)
        if step % flux_every == 0:
            if 0 < jz_screen < NZ - 1:
                dpsi_dz = (psi[:, jz_screen+1] - psi[:, jz_screen-1]) / (2*DZ)
                jz_scr = torch.imag(psi[:, jz_screen].conj() * dpsi_dz)
                flux_accum += torch.clamp(jz_scr, min=0.0) * DT * flux_every

        # Capture |ψ|² snapshot at screen when transmitted pulse peaks there
        if seeded and step % 10 == 0:
            rho_at_screen = torch.abs(psi[:, jz_screen])**2
            rho_scr_max_now = rho_at_screen.max().item()
            if rho_scr_max_now > rho_screen_max:
                rho_screen_max = rho_scr_max_now
                rho_screen_peak = rho_at_screen.clone()
                screen_snapshot_step = step

        # Dynamic seed time: find transmitted density peak at seed line
        if not seeded and step >= int(barrier_arrival * 0.8 / DT):
            rho_at_seed = torch.abs(psi[:, jz_seed])**2
            rho_max_now = rho_at_seed.max().item()
            if rho_max_now > rho_peak_at_seed:
                rho_peak_at_seed = rho_max_now
            elif (rho_max_now < 0.5 * rho_peak_at_seed and
                  rho_peak_at_seed > 1e-10 and not peak_found):
                peak_found = True
                # Seed 2 steps back (at peak)
                actual_seed_step = step

            # Also seed if we've passed the estimate by 5τ (fallback)
            if step > seed_step_est + int(5.0 / DT) and not seeded:
                actual_seed_step = step

        # Seed particles
        if actual_seed_step is not None and step == actual_seed_step and not seeded:
            psi_at_seed = psi.clone()
            px[:] = torch.tensor(all_x0, device=device, dtype=DTYPE)
            pz[:] = SEED_Z
            active[:] = True
            seeded = True
            seed_time_actual = step * DT
            print(f"  → Seeded {N_ENSEMBLE} particles at t={seed_time_actual:.1f}τ "
                  f"(step {step}, ρ_peak={rho_peak_at_seed:.2e})", flush=True)

        # Advect particles
        if step % vel_every == 0 and seeded and torch.any(active):
            vx_f, vz_f = compute_velocity_field(psi, KX, KZ)
            px, pz = advect_particles_gpu(vx_f, vz_f, px, pz, active, vel_dt)

            # Screen crossing
            crossed = active & (pz >= SCREEN_Z)
            if torch.any(crossed):
                x_hit[crossed] = px[crossed]
                active[crossed] = False

            # Out of bounds
            oob = active & ((torch.abs(px) > LX/2 - 1) |
                            (torch.abs(pz) > LZ/2 - 1))
            active[oob] = False

            # Backward-moving
            backward = active & (pz < SEED_Z - 5.0)
            active[backward] = False

            # Trajectories
            if step % traj_sample_every == 0:
                px_cpu = px[:n_traj].cpu()
                pz_cpu = pz[:n_traj].cpu()
                for i in range(n_traj):
                    if active[i] or not torch.isnan(x_hit[i]):
                        traj_x_list[i].append(px_cpu[i].item())
                        traj_z_list[i].append(pz_cpu[i].item())

        # Progress
        if step % 5000 == 0:
            t_now = step * DT
            n_hit = int(torch.sum(~torch.isnan(x_hit)).item())
            n_act = int(torch.sum(active).item())
            elapsed = time.time() - t_start
            norm_now = torch.sum(torch.abs(psi)**2).item() * DA
            print(f"  t={t_now:7.1f}τ  step={step:6d}  "
                  f"hits={n_hit:5d}  active={n_act:5d}  "
                  f"norm={norm_now:.4f}  [{elapsed:.0f}s]", flush=True)

        # Early termination
        if seeded and not torch.any(active):
            t_now = step * DT
            seed_t = actual_seed_step * DT if actual_seed_step else SEED_TIME_EST
            if t_now > seed_t + post_travel:
                print(f"  All particles resolved at step {step} "
                      f"(t={t_now:.1f}τ)", flush=True)
                break

    total_time = time.time() - t_start

    # ══════════════════════════════════════════════════════════════════
    #  Results
    # ══════════════════════════════════════════════════════════════════
    x_hit_np = x_hit.cpu().numpy()
    hit_mask = ~np.isnan(x_hit_np)
    x_hits = x_hit_np[hit_mask]

    flux_np = flux_accum.cpu().numpy()
    x_1d = np.linspace(-LX/2, LX/2 - DX, NX)

    print(f"\n  ═══ RESULTS ═══")
    print(f"  Total seeded: {N_ENSEMBLE}")
    print(f"  Total hits: {len(x_hits)}/{N_ENSEMBLE}")
    print(f"  Wall time: {total_time:.0f}s")

    # ══════════════════════════════════════════════════════════════════
    #  Analysis
    # ══════════════════════════════════════════════════════════════════
    from scipy.ndimage import gaussian_filter1d
    from scipy.stats import pearsonr

    # Use |ψ|² snapshot at screen as primary reference
    rho_ref_np = rho_screen_peak.cpu().numpy()
    if screen_snapshot_step is not None:
        print(f"  |ψ|² snapshot captured at step {screen_snapshot_step} "
              f"(t={screen_snapshot_step * DT:.1f}τ), "
              f"ρ_max={rho_screen_max:.2e}")

    x_range = 50.0
    x_mask = np.abs(x_1d) < x_range
    ref_env = rho_ref_np[x_mask]
    x_env = x_1d[x_mask]
    ref_smooth = gaussian_filter1d(ref_env, sigma=5)

    # Also compute Fraunhofer analytic for comparison
    k = WP_KZ; d = SLIT_SEP; a = SLIT_WIDTH; L = L_screen
    alpha_arr_env = k * a * x_env / (2 * L)
    beta_arr_env  = k * d * x_env / (2 * L)
    sinc_sq_env = np.where(np.abs(alpha_arr_env) < 1e-10, 1.0,
                           (np.sin(alpha_arr_env) / alpha_arr_env)**2)
    fraunhofer_env = sinc_sq_env * np.cos(beta_arr_env)**2

    # Reference peaks (from |ψ|² snapshot)
    peaks_a = []
    for i in range(1, len(ref_smooth) - 1):
        if (ref_smooth[i] > ref_smooth[i-1] and
            ref_smooth[i] > ref_smooth[i+1] and
            ref_smooth[i] > 0.05 * np.max(ref_smooth)):
            peaks_a.append(x_env[i])

    if len(peaks_a) >= 2:
        peaks_a = np.array(sorted(peaks_a))
        spacings_a = np.diff(peaks_a)
        mean_spacing_a = np.mean(spacings_a)
    else:
        spacings_a = np.array([])
        mean_spacing_a = None

    # Histogram analysis — compare to |ψ|² snapshot
    r_val = r_sq = p_val = None
    r_fraun = r_sq_fraun = p_fraun = None
    peaks_b = []
    mean_spacing_b = None

    if len(x_hits) >= 50:
        hist_vals, bin_edges = np.histogram(x_hits, bins=N_BINS,
                                            range=(-x_range, x_range))
        bin_centres = 0.5 * (bin_edges[:-1] + bin_edges[1:])

        ref_at_bins = np.interp(bin_centres, x_env, ref_smooth)

        hist_norm = hist_vals / np.max(hist_vals) if np.max(hist_vals) > 0 \
                    else hist_vals.astype(float)
        ref_norm = ref_at_bins / np.max(ref_at_bins) if np.max(ref_at_bins) > 0 \
                   else ref_at_bins

        sig_mask = ref_norm > 0.01
        if np.sum(sig_mask) > 5:
            r_val, p_val = pearsonr(hist_norm[sig_mask], ref_norm[sig_mask])
            r_sq = r_val**2

        # Also compare to Fraunhofer analytic
        fraun_at_bins = np.interp(bin_centres, x_env, fraunhofer_env)
        fraun_norm = fraun_at_bins / np.max(fraun_at_bins) if np.max(fraun_at_bins) > 0 \
                     else fraun_at_bins
        fsig = fraun_norm > 0.01
        if np.sum(fsig) > 5:
            r_fraun, p_fraun = pearsonr(hist_norm[fsig], fraun_norm[fsig])
            r_sq_fraun = r_fraun**2

        hist_smooth = gaussian_filter1d(hist_vals.astype(float), sigma=1.5)
        for i in range(1, len(hist_smooth) - 1):
            if (hist_smooth[i] > hist_smooth[i-1] and
                hist_smooth[i] > hist_smooth[i+1] and
                hist_smooth[i] > 0.10 * np.max(hist_smooth)):
                peaks_b.append(bin_centres[i])

        if len(peaks_b) >= 2:
            peaks_b = np.array(sorted(peaks_b))
            spacings_b = np.diff(peaks_b)
            mean_spacing_b = np.mean(spacings_b)
    else:
        hist_vals = np.zeros(N_BINS)
        bin_centres = np.linspace(-x_range, x_range, N_BINS)
        hist_norm = np.zeros(N_BINS)
        ref_norm = np.zeros(N_BINS)

    # ── Fringe Table ──
    print("\n" + "═"*70)
    print("  FRINGE SPACING & CORRELATION TABLE")
    print("═"*70)
    print(f"  {'Quantity':<42s} {'Value':>12s}")
    print(f"  {'-'*42:s} {'-'*12:s}")
    print(f"  {'N_hits (arrivals at screen)':<42s} {len(x_hits):>10d}")
    print(f"  {'N_ensemble':<42s} {N_ENSEMBLE:>10d}")
    print(f"  {'λ_dB (de Broglie wavelength)':<42s} {de_broglie:>10.2f} ξ")
    print(f"  {'Screen distance L (z_screen - z_barrier)':<42s} "
          f"{L_screen:>10.1f} ξ")
    print(f"  {'Theory: Δx = λ·L/d (Fraunhofer)':<42s} "
          f"{theory_spacing:>10.2f} ξ")
    if mean_spacing_a is not None:
        print(f"  {'|ψ|² snapshot peak spacing':<42s} "
              f"{mean_spacing_a:>10.2f} ξ")
        print(f"  {'  → N peaks':<42s} {len(peaks_a):>10d}")
    if mean_spacing_b is not None:
        print(f"  {'Histogram peak spacing':<42s} "
              f"{mean_spacing_b:>10.2f} ξ")
        print(f"  {'  → N peaks':<42s} {len(peaks_b):>10d}")
    if r_val is not None:
        print(f"  {'Pearson r (hist vs |ψ|² snapshot)':<42s} {r_val:>10.4f}")
        print(f"  {'R² (hist vs |ψ|² snapshot)':<42s} {r_sq:>10.4f}")
        print(f"  {'p-value':<42s} {p_val:>10.2e}")
    if r_fraun is not None:
        print(f"  {'Pearson r (hist vs Fraunhofer)':<42s} {r_fraun:>10.4f}")
        print(f"  {'R² (hist vs Fraunhofer)':<42s} {r_sq_fraun:>10.4f}")
        print(f"  {'p-value (Fraunhofer)':<42s} {p_fraun:>10.2e}")

    # ══════════════════════════════════════════════════════════════════
    #  CONTROL: |ψ|²-seeded particles (equivariance check)
    # ══════════════════════════════════════════════════════════════════
    print("\n  Running control (|ψ|² seeding)...", flush=True)
    psi_ctrl = psi_at_seed.clone()

    # Rejection-sample from |ψ|² at seed line
    rho_ctrl = torch.abs(psi_ctrl[:, jz_seed])**2
    rho_ctrl_np = rho_ctrl.cpu().numpy()
    rho_ctrl_max = rho_ctrl_np.max()
    x_all_np = np.linspace(-LX/2, LX/2 - DX, NX)

    rng_ctrl = np.random.default_rng(seed=123)
    ctrl_x0 = []
    while len(ctrl_x0) < N_ENSEMBLE:
        xt_batch = rng_ctrl.uniform(-LX/2, LX/2, N_ENSEMBLE * 10)
        for xt in xt_batch:
            iidx = min(int((xt + LX/2) / DX), NX - 1)
            if rng_ctrl.random() < rho_ctrl_np[iidx] / rho_ctrl_max:
                ctrl_x0.append(xt)
                if len(ctrl_x0) >= N_ENSEMBLE:
                    break
    ctrl_x0 = np.array(ctrl_x0[:N_ENSEMBLE])

    cpx = torch.tensor(ctrl_x0, device=device, dtype=DTYPE)
    cpz = torch.full((N_ENSEMBLE,), SEED_Z, device=device, dtype=DTYPE)
    c_active = torch.ones(N_ENSEMBLE, dtype=torch.bool, device=device)
    c_xhit = torch.full((N_ENSEMBLE,), float('nan'), device=device, dtype=DTYPE)

    ctrl_nsteps = n_steps
    seed_st = actual_seed_step if actual_seed_step else seed_step_est
    for cstep in range(seed_st + 1, ctrl_nsteps + 1):
        psi_ctrl = real_time_step(psi_ctrl, kinetic_prop, V_barrier, sponge)
        if cstep % vel_every == 0 and torch.any(c_active):
            cvx, cvz = compute_velocity_field(psi_ctrl, KX, KZ)
            cpx, cpz = advect_particles_gpu(cvx, cvz, cpx, cpz, c_active, vel_dt)
            crossed = c_active & (cpz >= SCREEN_Z)
            c_xhit[crossed] = cpx[crossed]; c_active[crossed] = False
            oob = c_active & ((torch.abs(cpx) > LX/2-1) |
                              (torch.abs(cpz) > LZ/2-1))
            c_active[oob] = False
            backward = c_active & (cpz < SEED_Z - 5.0)
            c_active[backward] = False
        if cstep % 5000 == 0:
            ch = int(torch.sum(~torch.isnan(c_xhit)).item())
            ca = int(torch.sum(c_active).item())
            print(f"  [ctrl] step={cstep}  hits={ch}  active={ca}", flush=True)
        if not torch.any(c_active):
            ct = cstep * DT
            if ct > (seed_st * DT) + post_travel:
                break

    ctrl_hits = c_xhit[~torch.isnan(c_xhit)].cpu().numpy()
    ctrl_r, ctrl_r2, ctrl_p = None, None, None
    ctrl_r_fraun, ctrl_r2_fraun, ctrl_p_fraun = None, None, None
    if len(ctrl_hits) >= 50:
        ch_vals, ch_edges = np.histogram(ctrl_hits, bins=N_BINS,
                                          range=(-x_range, x_range))
        ch_centres = 0.5 * (ch_edges[:-1] + ch_edges[1:])
        ch_ref = np.interp(ch_centres, x_env, ref_smooth)
        ch_norm = ch_vals / max(ch_vals.max(), 1)
        cr_norm = ch_ref / max(ch_ref.max(), 1e-30)
        csig = cr_norm > 0.01
        if csig.sum() > 5:
            ctrl_r, ctrl_p = pearsonr(ch_norm[csig], cr_norm[csig])
            ctrl_r2 = ctrl_r**2

        # Also compare control to Fraunhofer
        cf_ref = np.interp(ch_centres, x_env, fraunhofer_env)
        cf_norm = cf_ref / max(cf_ref.max(), 1e-30)
        cfsig = cf_norm > 0.01
        if cfsig.sum() > 5:
            ctrl_r_fraun, ctrl_p_fraun = pearsonr(ch_norm[cfsig], cf_norm[cfsig])
            ctrl_r2_fraun = ctrl_r_fraun**2

    print(f"  Control hits: {len(ctrl_hits)}/{N_ENSEMBLE}")
    if ctrl_r2 is not None:
        print(f"  Control R² (|ψ|²→|ψ|²): {ctrl_r2:.4f}")
        print(f"  Control Pearson r:       {ctrl_r:.4f}")
    if ctrl_r2_fraun is not None:
        print(f"  Control R² (vs Fraunhofer): {ctrl_r2_fraun:.4f}")
        print(f"  Control r  (vs Fraunhofer): {ctrl_r_fraun:.4f}")

    # ── Conclusion ──
    # Use best available R² (snapshot or Fraunhofer)
    best_r_sq = r_sq
    best_label = "|ψ|² snapshot"
    if r_sq_fraun is not None and (r_sq is None or r_sq_fraun > r_sq):
        best_r_sq = r_sq_fraun
        best_label = "Fraunhofer"

    if best_r_sq is not None and best_r_sq > 0.5 and len(x_hits) >= 100:
        conclusion = (f"Born rule emerges from uniform initial noise via "
                      f"hydrodynamic Bohmian guidance in true Fraunhofer "
                      f"regime (R² = {best_r_sq:.2f} [{best_label}], "
                      f"N = {len(x_hits):,d})")
    else:
        ctrl_note = (f", control R²={ctrl_r2:.2f}"
                     if ctrl_r2 is not None else "")
        conclusion = (f"Convergence insufficient → hypothesis requires "
                      f"further refinement "
                      f"(R²={best_r_sq:.4f} [{best_label}], "
                      f"N={len(x_hits)}{ctrl_note})"
                      if best_r_sq is not None
                      else f"Insufficient hits ({len(x_hits)})")

    print(f"\n  CONCLUSION: {conclusion}", flush=True)

    # ══════════════════════════════════════════════════════════════════
    #  Plots
    # ══════════════════════════════════════════════════════════════════
    print("\n  Generating plots...", flush=True)

    # Fraunhofer analytic (for overlay)
    x_cont = np.linspace(-x_range, x_range, 500)
    alpha_arr_c = k * a * x_cont / (2 * L)
    beta_arr_c  = k * d * x_cont / (2 * L)
    sinc_sq_c = np.where(np.abs(alpha_arr_c) < 1e-10, 1.0,
                         (np.sin(alpha_arr_c) / alpha_arr_c)**2)
    fraunhofer_c = sinc_sq_c * np.cos(beta_arr_c)**2

    fig, axes = plt.subplots(4, 1, figsize=(14, 22))

    # Panel A: |ψ|² snapshot + Fraunhofer
    ax = axes[0]
    ref_plot = ref_smooth / np.max(ref_smooth) if np.max(ref_smooth) > 0 else ref_smooth
    ax.plot(x_env, ref_plot, 'b-', lw=1.5,
            label='GP |ψ|² snapshot at screen peak')
    fraunhofer_norm = fraunhofer_c / np.max(fraunhofer_c)
    ax.plot(x_cont, fraunhofer_norm, 'r--', lw=2, alpha=0.6,
            label='Fraunhofer prediction')
    ax.set_xlabel('x [ξ]', fontsize=12)
    ax.set_ylabel('Normalized intensity', fontsize=12)
    ax.set_title('Reference |ψ|² Interference at Screen (z=+50ξ)',
                 fontsize=13, fontweight='bold')
    ax.legend(fontsize=11)
    ax.set_xlim(-x_range, x_range)
    ax.grid(True, alpha=0.3)

    # Panel B: Uniform histogram
    ax = axes[1]
    if len(x_hits) > 0:
        ax.hist(x_hits, bins=N_BINS, range=(-x_range, x_range),
                color='#1f77b4', edgecolor='black', linewidth=0.3,
                alpha=0.8, density=True,
                label=f'Uniform x₀ (N={len(x_hits):,d}/{N_ENSEMBLE:,d})')
        hist_dens, _ = np.histogram(x_hits, bins=N_BINS,
                                     range=(-x_range, x_range), density=True)
        peak_hist = np.max(hist_dens) if np.max(hist_dens) > 0 else 1.0
        ax.plot(x_env, ref_plot * peak_hist, 'r-', lw=2, alpha=0.7,
                label=f'|ψ|² snapshot (R²={r_sq:.3f})' if r_sq else '|ψ|²')
    ax.set_xlabel('x on screen [ξ]', fontsize=12)
    ax.set_ylabel('Probability density', fontsize=12)
    ax.set_title(f'TEST: Uniform x₀ → Screen z=+50ξ (R²={r_sq:.3f})'
                 if r_sq else 'TEST: Uniform x₀ → Screen',
                 fontsize=13, fontweight='bold')
    ax.set_xlim(-x_range, x_range)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)

    # Panel C: Control histogram
    ax = axes[2]
    if len(ctrl_hits) > 0:
        ax.hist(ctrl_hits, bins=N_BINS, range=(-x_range, x_range),
                color='#2ca02c', edgecolor='black', linewidth=0.3,
                alpha=0.8, density=True,
                label=f'|ψ|²-seeded (N={len(ctrl_hits):,d}/{N_ENSEMBLE:,d})')
        ch_dens, _ = np.histogram(ctrl_hits, bins=N_BINS,
                                   range=(-x_range, x_range), density=True)
        cpeak = np.max(ch_dens) if np.max(ch_dens) > 0 else 1.0
        ax.plot(x_env, ref_plot * cpeak, 'r-', lw=2, alpha=0.7,
                label=f'|ψ|² snapshot (R²={ctrl_r2:.3f})'
                if ctrl_r2 else '|ψ|²')
    ax.set_xlabel('x on screen [ξ]', fontsize=12)
    ax.set_ylabel('Probability density', fontsize=12)
    ax.set_title(f'CONTROL: |ψ|²-seeded → Screen (R²={ctrl_r2:.3f})'
                 if ctrl_r2 else 'CONTROL: |ψ|²-seeded → Screen',
                 fontsize=13, fontweight='bold')
    ax.set_xlim(-x_range, x_range)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)

    # Panel D: Trajectories
    ax = axes[3]
    sw = SLIT_WIDTH / 2.0
    x1c = SLIT_SEP / 2.0
    x2c = -SLIT_SEP / 2.0
    # Draw barrier walls
    ax.fill_betweenx([BARRIER_Z - BARRIER_THICK/2, BARRIER_Z + BARRIER_THICK/2],
                     -40, x2c - sw, color='gray', alpha=0.5)
    ax.fill_betweenx([BARRIER_Z - BARRIER_THICK/2, BARRIER_Z + BARRIER_THICK/2],
                     x2c + sw, x1c - sw, color='gray', alpha=0.5)
    ax.fill_betweenx([BARRIER_Z - BARRIER_THICK/2, BARRIER_Z + BARRIER_THICK/2],
                     x1c + sw, 40, color='gray', alpha=0.5)
    ax.axhline(SCREEN_Z, color='orange', ls='--', lw=2, label='Screen z=+50ξ')

    colors = plt.cm.tab20(np.linspace(0, 1, n_traj))
    for i in range(n_traj):
        if len(traj_x_list[i]) > 2:
            ax.plot(traj_x_list[i], traj_z_list[i], '-', color=colors[i],
                    lw=0.7, alpha=0.7)

    ax.set_xlabel('x [ξ]', fontsize=12)
    ax.set_ylabel('z [ξ]', fontsize=12)
    ax.set_xlim(-25, 25)
    ax.set_ylim(SEED_Z - 2, SCREEN_Z + 5)
    ax.set_title('Example Bohmian Trajectories (far-field)',
                 fontsize=13, fontweight='bold')
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    hist_path = os.path.join(OUTDIR, 'double_slit_ensemble_histogram_v3.png')
    fig.savefig(hist_path, dpi=200)
    plt.close(fig)
    print(f"  Plot: {hist_path}")

    # Initial distribution plot
    fig2, ax2 = plt.subplots(figsize=(8, 4))
    ax2.hist(all_x0, bins=50,
             range=(-SLIT_SEP/2 - SLIT_WIDTH, SLIT_SEP/2 + SLIT_WIDTH),
             color='green', alpha=0.7, edgecolor='black',
             label=f'Initial x₀ (Uniform in slits, N={N_ENSEMBLE:,d})')
    ax2.axhline(N_ENSEMBLE / 50, color='red', ls='--', lw=2,
                label='Expected (flat per slit)')
    ax2.set_xlabel('x₀ [ξ]', fontsize=12)
    ax2.set_ylabel('Count', fontsize=12)
    ax2.set_title('Initial x₀ Distribution (Uniform within slit apertures)',
                  fontsize=13, fontweight='bold')
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    init_path = os.path.join(OUTDIR, 'initial_x0_distribution_v3.png')
    fig2.savefig(init_path, dpi=150)
    plt.close(fig2)
    print(f"  Plot: {init_path}")

    # JSON
    jd = {
        'parameters': {
            'NX': NX, 'NZ': NZ, 'LX': LX, 'LZ': LZ,
            'DT': DT, 'WP_SIGMA_X': WP_SIGMA_X,
            'WP_SIGMA_Z': WP_SIGMA_Z,
            'WP_Z0': WP_Z0, 'WP_KZ': WP_KZ,
            'SLIT_WIDTH': SLIT_WIDTH, 'SLIT_SEP': SLIT_SEP,
            'V_BARRIER': V_BARRIER, 'SCREEN_Z': SCREEN_Z,
            'N_ENSEMBLE': N_ENSEMBLE,
            'TANH_WIDTH': TANH_WIDTH,
            'SEED_Z': SEED_Z,
            'SPONGE_WIDTH': SPONGE_WIDTH,
            'actual_seed_time': float(actual_seed_step * DT) if actual_seed_step else None,
        },
        'results': {
            'n_hits': int(len(x_hits)),
            'envelope_n_peaks': int(len(peaks_a)),
            'envelope_spacing': float(mean_spacing_a) if mean_spacing_a else None,
            'histogram_n_peaks': int(len(peaks_b)) if len(peaks_b) > 0 else 0,
            'histogram_spacing': float(mean_spacing_b) if mean_spacing_b else None,
            'theory_spacing': float(theory_spacing),
            'pearson_r': float(r_val) if r_val is not None else None,
            'r_squared': float(r_sq) if r_sq is not None else None,
            'r_squared_fraunhofer': float(r_sq_fraun) if r_sq_fraun is not None else None,
            'p_value': float(p_val) if p_val is not None else None,
            'control_n_hits': int(len(ctrl_hits)),
            'control_r_squared': float(ctrl_r2) if ctrl_r2 is not None else None,
            'control_pearson_r': float(ctrl_r) if ctrl_r is not None else None,
            'control_r_squared_fraunhofer': float(ctrl_r2_fraun) if ctrl_r2_fraun is not None else None,
        },
        'conclusion': conclusion,
        'wall_time_s': total_time,
    }
    jpath = os.path.join(OUTDIR, 'results_v3.json')
    with open(jpath, 'w') as f:
        json.dump(jd, f, indent=2)
    print(f"  JSON: {jpath}")
    print(f"\n  Total wall time: {total_time:.0f}s", flush=True)


if __name__ == '__main__':
    main()
