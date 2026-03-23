#!/usr/bin/env python3
"""
UHF Phase 3 — Dynamical Simulation Suite
==========================================
Sim A: 512³ GP trefoil vortex reconnection (toroidal cross-section imprint)
Sim B: 128³ LISA acoustic echo (Gaussian pressure spike)
Sim C: N=7 Mermin Nuke (float64, expect 64.0 ± 10⁻¹⁴)
"""

import math
import sys
import time
import numpy as np

try:
    import cupy as cp
    from cupy.fft import fftn, ifftn
    CUPY = True
except ImportError:
    cp = None
    CUPY = False

# ═══════════════════════════════════════════════════════════════════
#  VRAM heartbeat
# ═══════════════════════════════════════════════════════════════════

def vram_gb():
    """Return (used_gb, total_gb) on device 0."""
    if not CUPY:
        return 0.0, 0.0
    free, total = cp.cuda.runtime.memGetInfo()
    used = (total - free) / 1e9
    return used, total / 1e9

def heartbeat(step, total, label=""):
    used, tot = vram_gb()
    print(f"  {label} Step {step:>4d}/{total} | VRAM: {used:.1f}/{tot:.1f} GB")


# ═══════════════════════════════════════════════════════════════════
#  Sim C — N=7 Mermin Nuke  (run first as environment check)
# ═══════════════════════════════════════════════════════════════════

def sim_c_mermin_nuke():
    """
    N=7 Mermin operator with vortex 3-6-9 kernel.
    M_7 = Im[(σ_x + iσ_y)^⊗7]
    |Ψ_V⟩ = (|0000000⟩ + i|1111111⟩)/√2
    Expect ⟨M_7⟩ = 2^6 = 64.0 ± 10⁻¹⁴
    """
    from itertools import product as iterproduct

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  Sim C — N=7 Mermin Nuke (float64)                         ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    N = 7
    dim = 2 ** N  # 128

    sigma_x = np.array([[0, 1], [1, 0]], dtype=np.complex128)
    sigma_y = np.array([[0, -1j], [1j, 0]], dtype=np.complex128)

    t0 = time.perf_counter()

    # Build M_7 = Im[(σ_x + iσ_y)^⊗7]
    M = np.zeros((dim, dim), dtype=np.complex128)
    for bits in iterproduct([0, 1], repeat=N):
        k = sum(bits)
        coeff = (1j) ** k
        if abs(coeff.imag) < 0.5:
            continue
        sign = coeff.imag

        op = sigma_y if bits[0] else sigma_x
        for b in bits[1:]:
            op = np.kron(op, sigma_y if b else sigma_x)
        M += sign * op

    M = (M + M.conj().T) / 2.0  # enforce Hermiticity

    # Vortex GHZ with Tesla phase
    psi = np.zeros(dim, dtype=np.complex128)
    psi[0] = 1.0 / np.sqrt(2)
    psi[dim - 1] = 1j / np.sqrt(2)

    # ⟨M_7⟩ = ψ† M ψ
    m_val = np.real(psi.conj() @ M @ psi)
    wall = time.perf_counter() - t0

    c_bound = float(2 ** (N // 2))   # 2^3 = 8
    q_bound = float(2 ** (N - 1))    # 2^6 = 64
    err = abs(m_val - 64.0)
    ratio = m_val / c_bound

    print(f"  N             = {N}")
    print(f"  dim(H)        = {dim}")
    print(f"  ⟨M_7⟩         = {m_val:.15f}")
    print(f"  Expected      = 64.000000000000000")
    print(f"  |Error|       = {err:.2e}")
    print(f"  LHV bound     = {c_bound:.1f}")
    print(f"  QM bound      = {q_bound:.1f}")
    print(f"  Violation     = {ratio:.4f}×")
    print(f"  Wall time     = {wall:.3f} s")

    passed = err < 1e-12
    print(f"  Status: {'PASS' if passed else 'FAIL'}  "
          f"(tol 10⁻¹²: {err:.2e})")
    print()
    return passed


# ═══════════════════════════════════════════════════════════════════
#  Sim A — 512³ Trefoil GP Reconnection
# ═══════════════════════════════════════════════════════════════════

def sim_a_trefoil_512():
    """
    512³ GP simulation with trefoil T(2,3) imprinted via
    toroidal cross-section method.

    In each poloidal plane at angle φ, imprint 3 phase vortices
    at θ = (2φ + 2πk)/3 for k=0,1,2.
    """
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  Sim A — 512³ Trefoil GP Reconnection                      ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    if not CUPY:
        print("  SKIP — CuPy required")
        return False

    xp = cp
    N = 512
    dx = 0.5
    dt = 0.002
    n_steps = 200
    measure_every = 50

    L = N * dx  # 256.0
    R_torus = L / 4       # major radius = 64
    r_torus = R_torus / 4  # minor radius = 16

    # Force-clear any leftover GPU allocations
    cp.get_default_memory_pool().free_all_blocks()
    cp.get_default_pinned_memory_pool().free_all_blocks()

    used, tot = vram_gb()
    print(f"  Grid: {N}³  |  dx={dx}  dt={dt}  L={L}")
    print(f"  Torus: R={R_torus:.1f}, r={r_torus:.1f}")
    print(f"  VRAM before alloc: {used:.1f}/{tot:.1f} GB")

    t_wall = time.perf_counter()

    # ── k-space setup (1D stored, 3D built only when needed) ──
    k1d = xp.fft.fftfreq(N, d=dx).astype(xp.float64) * 2 * xp.pi
    kx_1d = k1d.reshape(N, 1, 1)
    ky_1d = k1d.reshape(1, N, 1)
    kz_1d = k1d.reshape(1, 1, N)
    # Build k² incrementally (float64, 1.07 GB)
    k2 = xp.zeros((N, N, N), dtype=xp.float64)
    k2 += kx_1d**2
    k2 += ky_1d**2
    k2 += kz_1d**2
    # Build kinetic_half in-place to avoid complex temp spike
    kinetic_half = (-0.5j * dt / 2.0) * k2.astype(xp.complex128)
    xp.exp(kinetic_half, out=kinetic_half)

    heartbeat(0, n_steps, "alloc k-space")

    # ── Coordinate grid (process in slabs to limit peak VRAM) ──
    x1d = xp.linspace(0, L, N, endpoint=False, dtype=xp.float64)
    cx, cy, cz = L / 2, L / 2, L / 2

    # We'll build psi slab-by-slab (along x) to avoid 6 full 3D coord arrays
    psi = xp.ones((N, N, N), dtype=xp.complex128)
    core_w = dx * 2.0
    rv = r_torus * 0.5  # vortex orbit radius within tube

    slab_size = 32  # process 32 x-slices at a time
    n_slabs = N // slab_size

    for sl in range(n_slabs):
        i0 = sl * slab_size
        i1 = i0 + slab_size

        # Build local coordinates for this slab
        xx_s = x1d[i0:i1].reshape(slab_size, 1, 1) - cx
        yy_s = x1d.reshape(1, N, 1) - cy
        zz_s = x1d.reshape(1, 1, N) - cz

        phi_s = xp.arctan2(yy_s, xx_s)
        r_cyl_s = xp.sqrt(xx_s**2 + yy_s**2)
        delta_r_s = r_cyl_s - R_torus
        rho_perp_s = xp.sqrt(delta_r_s**2 + zz_s**2)
        theta_s = xp.arctan2(zz_s, delta_r_s)

        phase_s = xp.zeros((slab_size, N, N), dtype=xp.float64)
        density_s = xp.ones((slab_size, N, N), dtype=xp.float64)

        for k_idx in range(3):
            theta_k = (2.0 * phi_s + 2.0 * xp.pi * k_idx) / 3.0
            cos_diff = xp.cos(theta_s - theta_k)
            sin_diff = xp.sin(theta_s - theta_k)
            d_k = xp.sqrt((rho_perp_s * cos_diff - rv)**2 +
                           (rho_perp_s * sin_diff)**2)
            phase_s += xp.arctan2(rho_perp_s * sin_diff,
                                  rho_perp_s * cos_diff - rv)
            density_s *= d_k / xp.sqrt(d_k**2 + 2.0 * core_w**2)

        psi[i0:i1] = density_s.astype(xp.complex128) * xp.exp(1j * phase_s)

    # Cleanup slab temporaries
    del xx_s, yy_s, zz_s, phi_s, r_cyl_s, delta_r_s, rho_perp_s, theta_s
    del phase_s, density_s, cos_diff, sin_diff, d_k, theta_k
    cp.get_default_memory_pool().free_all_blocks()

    heartbeat(0, n_steps, "psi ready")

    # ── Lightweight Hamiltonian (no kx/ky/kz needed) ──
    def compute_hamiltonian(psi_field):
        """H = ½Σ k²|ψ_k|²/N³·dx³ + ½Σ(|ψ|²−1)²·dx³"""
        psi_k = fftn(psi_field)
        grad_sq = k2 * (xp.real(psi_k)**2 + xp.imag(psi_k)**2)
        H_kin = 0.5 * float(xp.sum(grad_sq).real) / N**3 * dx**3
        del psi_k, grad_sq
        rho = xp.real(psi_field * xp.conj(psi_field))
        H_int = 0.5 * float(xp.sum((rho - 1.0)**2)) * dx**3
        del rho
        return H_kin, H_int, H_kin + H_int

    # ── Full NAB decomposition (memory-careful, run only at start/end) ──
    def compute_nab(psi_field):
        """NAB energy decomposition using sequential gradient computation."""
        N3 = N**3
        dx3 = dx**3
        rho = xp.abs(psi_field)**2
        sqrt_rho = xp.sqrt(rho + 1e-30)
        psi_k = fftn(psi_field)
        psi_conj = xp.conj(psi_field)

        # Process one gradient component at a time → w_k components
        # Accumulate |w_k|² and k·w_k products in k-space
        # We need: kx*wx_k + ky*wy_k + kz*wz_k (Helmholtz)
        # Store each w_component_k, compute energy on the fly

        w_k_list = []
        k_components = [kx_1d, ky_1d, kz_1d]

        for dim_idx in range(3):
            k_comp = k_components[dim_idx]
            dpsi = ifftn(1j * k_comp * psi_k)
            j_comp = xp.imag(psi_conj * dpsi)
            del dpsi
            w_comp = j_comp / sqrt_rho
            del j_comp
            w_comp_k = fftn(w_comp)
            del w_comp
            w_k_list.append(w_comp_k)

        del psi_conj, sqrt_rho

        # E_kin = ½ Σ |w_k|² / N³ * dx³
        E_kin = 0.0
        for wk in w_k_list:
            E_kin += 0.5 * float(xp.sum(xp.real(wk)**2 + xp.imag(wk)**2).real) / N3 * dx3

        # Helmholtz: k·w
        k2_safe = k2.copy()
        k2_safe[0, 0, 0] = 1.0
        kdotw = k_components[0] * w_k_list[0] + k_components[1] * w_k_list[1] + k_components[2] * w_k_list[2]

        # Compressible energy
        E_comp = 0.0
        for dim_idx in range(3):
            w_c_k = k_components[dim_idx] * kdotw / k2_safe
            w_c_k.reshape(-1)[0] = 0  # zero DC
            E_comp += 0.5 * float(xp.sum(xp.real(w_c_k)**2 + xp.imag(w_c_k)**2).real) / N3 * dx3
            del w_c_k

        del kdotw, k2_safe
        for wk in w_k_list:
            del wk
        del w_k_list
        cp.get_default_memory_pool().free_all_blocks()

        E_incomp = E_kin - E_comp

        # Hamiltonian
        grad_sq = k2 * (xp.real(psi_k)**2 + xp.imag(psi_k)**2)
        H_kin = 0.5 * float(xp.sum(grad_sq).real) / N3 * dx3
        del grad_sq, psi_k
        H_int = 0.5 * float(xp.sum((rho - 1.0)**2).real) * dx3
        del rho
        cp.get_default_memory_pool().free_all_blocks()

        return E_kin, E_comp, E_incomp, H_kin + H_int

    # ── Strang split-step ──
    def step(psi_field):
        psi_k = fftn(psi_field)
        psi_k *= kinetic_half
        psi_field = ifftn(psi_k)

        rho = xp.abs(psi_field)**2
        psi_field *= xp.exp(-1j * (rho - 1.0) * dt)

        psi_k = fftn(psi_field)
        psi_k *= kinetic_half
        psi_field = ifftn(psi_k)
        return psi_field

    # ── Main evolution loop ──
    history = []

    # Initial NAB measurement
    print("  Computing initial NAB decomposition...")
    E_kin0, E_comp0, E_inc0, H0 = compute_nab(psi)
    history.append((0, E_kin0, E_comp0, E_inc0, H0))
    heartbeat(0, n_steps, "SimA")
    print(f"         H={H0:.6e}  E_kin={E_kin0:.4e}  "
          f"E_comp={E_comp0:.4e}  E_inc={E_inc0:.4e}")

    for s in range(1, n_steps + 1):
        psi = step(psi)

        if s % measure_every == 0:
            H_kin, H_int, H = compute_hamiltonian(psi)
            history.append((s, 0, 0, 0, H))
            heartbeat(s, n_steps, "SimA")
            print(f"         H={H:.6e}  H_kin={H_kin:.4e}  H_int={H_int:.4e}")

    # Final NAB measurement
    print("  Computing final NAB decomposition...")
    E_kinF, E_compF, E_incF, HF = compute_nab(psi)
    history[-1] = (n_steps, E_kinF, E_compF, E_incF, HF)
    print(f"         H={HF:.6e}  E_kin={E_kinF:.4e}  "
          f"E_comp={E_compF:.4e}  E_inc={E_incF:.4e}")

    wall = time.perf_counter() - t_wall

    # ── Results ──
    H_init = H0
    H_final = HF
    dH_pct = (H_final - H_init) / (abs(H_init) + 1e-30) * 100

    print(f"\n  ── Sim A Results ──")
    print(f"  Wall time:       {wall:.1f} s")
    print(f"  H conservation:  ΔH = {dH_pct:+.6f}%")
    print(f"  E_incomp (init): {E_inc0:.6e}")
    print(f"  E_incomp (final):{E_incF:.6e}")
    print(f"  E_comp (init):   {E_comp0:.6e}")
    print(f"  E_comp (final):  {E_compF:.6e}")
    print(f"  Incomp frac init:  {E_inc0/(E_kin0+1e-30):.6f}")
    print(f"  Incomp frac final: {E_incF/(E_kinF+1e-30):.6f}")

    h_ok = abs(dH_pct) < 1.0
    print(f"  Status: {'PASS' if h_ok else 'WARN'} (ΔH < 1%)")

    # Cleanup
    del psi, kinetic_half, k2
    cp.get_default_memory_pool().free_all_blocks()

    return h_ok, history


# ═══════════════════════════════════════════════════════════════════
#  Sim B — 128³ LISA Acoustic Echo
# ═══════════════════════════════════════════════════════════════════

def sim_b_lisa_echo():
    """
    128³ GP simulation with a Gaussian pressure spike.
    Measure echo reflections at the centre probe (periodic BEC box).

    Physics: sound speed c_s = 1 (dimensionless, ξ=1 units).
    Echo period = L/c_s (wave wraps around periodic box).
    Δt/R = L/(L/2) = 2.0 — the round-trip time in units of R.
    """
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  Sim B — 128³ LISA Acoustic Echo                           ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    if not CUPY:
        print("  SKIP — CuPy required")
        return False

    xp = cp
    cp.get_default_memory_pool().free_all_blocks()

    N = 128
    dx = 0.5
    dt = 0.01          # larger dt for speed (CFL OK: dt < dx²/2 = 0.125)
    n_steps = 25000     # t_final = 250 → ~3.9 echo periods
    L = N * dx          # 64.0

    c_s = 1.0
    R_box = L / 2       # 32.0
    t_echo = L / c_s    # 64.0 — echo period at centre

    print(f"  Grid: {N}³  |  dx={dx}  dt={dt}  L={L}")
    print(f"  c_s={c_s}  R_box={R_box}  t_echo(expected)={t_echo:.1f}")
    heartbeat(0, n_steps, "SimB")

    # k-space
    k1d = xp.fft.fftfreq(N, d=dx).astype(xp.float64) * 2 * xp.pi
    kx_1d = k1d.reshape(N, 1, 1)
    ky_1d = k1d.reshape(1, N, 1)
    kz_1d = k1d.reshape(1, 1, N)
    k2 = xp.zeros((N, N, N), dtype=xp.float64)
    k2 += kx_1d**2; k2 += ky_1d**2; k2 += kz_1d**2
    kinetic_half = (-0.5j * dt / 2.0) * k2.astype(xp.complex128)
    xp.exp(kinetic_half, out=kinetic_half)

    # Coordinate grid
    x1d = xp.linspace(0, L, N, endpoint=False, dtype=xp.float64)
    xx, yy, zz = xp.meshgrid(x1d, x1d, x1d, indexing='ij')

    # ── Uniform BEC + Gaussian pressure spike at centre ──
    cx, cy, cz = L / 2, L / 2, L / 2
    sigma = 3.0         # tight spike for clean wavefront
    amp = 0.5            # strong perturbation for visible echoes
    r2 = (xx - cx)**2 + (yy - cy)**2 + (zz - cz)**2
    rho0 = 1.0 + amp * xp.exp(-r2 / (2 * sigma**2))
    psi = xp.sqrt(rho0).astype(xp.complex128)

    del xx, yy, zz, r2, rho0
    cp.get_default_memory_pool().free_all_blocks()

    ic = N // 2  # centre index

    print(f"  Spike: amp={amp}, σ={sigma:.1f}")
    print(f"  Expected echo period at centre: {t_echo:.1f}")
    print(f"  Expected Δt/R = {t_echo/R_box:.4f}")

    # ── Step function ──
    def step_b(psi_field):
        psi_k = fftn(psi_field)
        psi_k *= kinetic_half
        psi_field = ifftn(psi_k)
        rho = xp.abs(psi_field)**2
        psi_field *= xp.exp(-1j * (rho - 1.0) * dt)
        psi_k = fftn(psi_field)
        psi_k *= kinetic_half
        psi_field = ifftn(psi_k)
        return psi_field

    # ── Evolve and record density at centre probe ──
    t_wall = time.perf_counter()
    # Sample every 10 steps to reduce host overhead
    sample_every = 10
    n_samples = n_steps // sample_every + 1
    rho_centre = np.zeros(n_samples)
    times = np.zeros(n_samples)
    si = 0

    for s in range(n_steps + 1):
        if s % sample_every == 0 and si < n_samples:
            rho_centre[si] = float(xp.abs(psi[ic, ic, ic])**2)
            times[si] = s * dt
            si += 1

        if s % 5000 == 0:
            heartbeat(s, n_steps, "SimB")

        if s < n_steps:
            psi = step_b(psi)

    wall = time.perf_counter() - t_wall
    rho_centre = rho_centre[:si]
    times = times[:si]

    # ── Find echo peaks at centre ──
    from scipy.signal import find_peaks

    # The centre density starts high, drops, then spikes periodically
    # Look for peaks above the equilibrium baseline (ρ≈1)
    baseline = 1.0
    signal = rho_centre - baseline

    # Minimum distance between peaks: ~t_echo / dt / sample_every * 0.5
    min_dist = int(t_echo / dt / sample_every * 0.5)
    # Height threshold: small fraction of initial perturbation
    peaks, props = find_peaks(signal, height=amp * 0.01, distance=max(min_dist, 10))

    # Skip peak at t=0 (the initial spike itself)
    if len(peaks) > 0 and times[peaks[0]] < 5.0:
        peaks = peaks[1:]

    echo_times = times[peaks[:6]]
    echo_deltas = np.diff(echo_times) if len(echo_times) > 1 else np.array([])

    print(f"\n  ── Sim B Results ──")
    print(f"  Wall time:       {wall:.1f} s")
    print(f"  ρ_centre(t=0):   {rho_centre[0]:.6f}")
    print(f"  ρ_centre(final): {rho_centre[-1]:.6f}")
    print(f"  Echo peaks found: {len(peaks)}")

    if len(echo_times) > 0:
        print(f"  Echo arrival times at centre:")
        for i, et in enumerate(echo_times):
            ratio = et / R_box
            print(f"    Echo {i+1}: t = {et:.2f}  →  Δt/R = {ratio:.4f}")

    if len(echo_deltas) > 0:
        print(f"  Inter-echo intervals:")
        for i, ed in enumerate(echo_deltas):
            ratio = ed / R_box
            print(f"    Δt_{i+1}→{i+2} = {ed:.2f}  →  Δt/R = {ratio:.4f}")
        mean_dt = np.mean(echo_deltas)
        mean_ratio = mean_dt / R_box
        print(f"  Mean echo period: {mean_dt:.2f}")
        print(f"  Mean Δt/R = {mean_ratio:.4f}  (expected {t_echo/R_box:.4f})")
        print(f"  LISA validation: Δt/R = {mean_ratio:.4f} "
              f"→ physical echo = 2R/c = {mean_ratio:.1f}× (R/c)")
    else:
        mean_ratio = 0.0

    # Diagnostics: dump the time series for debugging
    print(f"\n  Centre density time-series (sampled):")
    for i in range(0, min(len(rho_centre), 20)):
        t = times[i]
        r = rho_centre[i]
        print(f"    t={t:7.2f}  ρ={r:.6f}")
    if len(rho_centre) > 20:
        print(f"    ... ({len(rho_centre)} total samples)")

    passed = len(peaks) >= 2
    print(f"\n  Status: {'PASS' if passed else 'WARN'} "
          f"({len(peaks)} echoes detected, need ≥ 2)")

    # Cleanup
    del psi, kinetic_half, k2
    cp.get_default_memory_pool().free_all_blocks()

    return passed


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

def main():
    print("=" * 66)
    print("  UHF Phase 3 — Dynamical Simulation Suite")
    print("=" * 66)

    if CUPY:
        dev = cp.cuda.Device(0)
        props = cp.cuda.runtime.getDeviceProperties(0)
        name = props["name"]
        if isinstance(name, bytes):
            name = name.decode()
        free, total = cp.cuda.runtime.memGetInfo()
        print(f"  GPU: {name}")
        print(f"  VRAM: {free/1e9:.1f} / {total/1e9:.1f} GB free")
    else:
        print("  GPU: Not available (CPU mode)")

    # ── Sim C first (environment check) ──
    c_ok = sim_c_mermin_nuke()

    # Clear GPU between sims
    if CUPY:
        cp.get_default_memory_pool().free_all_blocks()
        cp.get_default_pinned_memory_pool().free_all_blocks()

    # ── Sim A ──
    a_ok = False
    a_hist = None
    try:
        a_ok, a_hist = sim_a_trefoil_512()
    except Exception as e:
        print(f"  Sim A FAILED: {e}")
        import traceback; traceback.print_exc()

    # Clear GPU between sims
    if CUPY:
        cp.get_default_memory_pool().free_all_blocks()
        cp.get_default_pinned_memory_pool().free_all_blocks()

    # ── Sim B ──
    b_ok = False
    try:
        b_ok = sim_b_lisa_echo()
    except Exception as e:
        print(f"  Sim B FAILED: {e}")
        import traceback; traceback.print_exc()

    # ── Summary ──
    print("\n" + "=" * 66)
    print("  PHASE 3 SUMMARY")
    print("=" * 66)
    status = {True: "✓ PASS", False: "✗ FAIL"}
    print(f"  Sim C (N=7 Mermin Nuke)       : {status[c_ok]}")
    print(f"  Sim A (512³ Trefoil GP)       : {status[a_ok]}")
    print(f"  Sim B (128³ LISA Echo)        : {status[b_ok]}")
    all_pass = c_ok and a_ok and b_ok
    print(f"\n  Overall: {'ALL PASS' if all_pass else 'PARTIAL'}")
    print("=" * 66)

    return all_pass


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
