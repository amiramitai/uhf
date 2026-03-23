"""
UHF UV-Holography Stress Test: Gross-Pitaevskii Falsification
=================================================================
HYPOTHESIS: Topological phase information propagates NON-LOCALLY
            (instantaneously), even when acoustic signals travel
            at finite speed c_s.

METHOD:
    Solve the 3D time-dependent Gross-Pitaevskii equation on a
    periodic grid using split-step spectral (SSS) method with CUDA.

    GP: i d_psi/dt = [-0.5 nabla^2 + g|psi|^2 - mu] psi

    c_s = sqrt(g * rho_0)   <-- FINITE speed of sound
    xi  = 1/sqrt(2*g*rho_0) <-- healing length

SETUP:
    - T(2,3) trefoil vortex knot embedded via phase imprinting
    - Spherical measurement boundary at R = 15*xi
    - At t=0: localized gauge perturbation at knot core

DUAL MEASUREMENT:
    Signal A (Acoustic): density fluctuation arrival at boundary
    Signal B (Topological): phase holonomy shift at boundary

FALSIFICATION:
    If dt_phase ~ dt_acoustic = R/c_s  -->  STRICTLY LOCAL
    If dt_phase = 0, dt_acoustic > 0   -->  NON-LOCALITY CONFIRMED
"""

import os
import sys
import time
import numpy as np

os.environ["LD_LIBRARY_PATH"] = "/usr/lib/wsl/lib"

try:
    import cupy as cp
    from cupy.fft import fftn, ifftn
except ImportError:
    print("FATAL: CuPy required for GPU computation.")
    sys.exit(1)


# =====================================================================
# Physical Parameters (natural units: hbar = m = 1)
# =====================================================================
class GPParams:
    def __init__(self, N=384):
        self.g = 1.0                         # interaction strength
        self.rho0 = 1.0                      # background density
        self.mu = self.g * self.rho0         # chemical potential mu = g*rho0
        self.cs = np.sqrt(self.g * self.rho0) # speed of sound
        self.xi = 1.0 / np.sqrt(2.0 * self.g * self.rho0)  # healing length

        self.N = N                           # grid points per dimension
        self.R_boundary = 15.0 * self.xi     # measurement boundary radius
        self.L = 2.2 * self.R_boundary       # box half-width (padded)
        self.dx = 2.0 * self.L / self.N      # grid spacing
        self.dt = 0.2 * self.dx**2           # stable time step (CFL-like)

        # Derived
        self.t_acoustic = self.R_boundary / self.cs  # expected acoustic arrival
        self.N_total = self.N ** 3

        # Perturbation — stronger to produce measurable acoustic signal
        self.perturb_sigma = 3.0 * self.xi   # Gaussian width (wider for stronger density wave)
        self.perturb_phase = np.pi           # phase kick magnitude (full pi for max effect)

    def report(self):
        print("=" * 65)
        print("  GROSS-PITAEVSKII STRESS TEST PARAMETERS")
        print("=" * 65)
        print(f"  Grid:             {self.N}^3 = {self.N_total:,} points")
        print(f"  Box:              [-{self.L:.2f}, {self.L:.2f}]^3")
        print(f"  dx:               {self.dx:.4f}")
        print(f"  dt:               {self.dt:.6f}")
        print(f"  Healing length:   xi = {self.xi:.4f}")
        print(f"  dx / xi:          {self.dx / self.xi:.3f}")
        print(f"  Speed of sound:   c_s = {self.cs:.4f}")
        print(f"  Boundary R:       {self.R_boundary:.4f}  ({self.R_boundary/self.xi:.1f} xi)")
        print(f"  Acoustic travel:  t_acoustic = R/c_s = {self.t_acoustic:.4f}")
        print(f"  Interaction g:    {self.g}")
        print(f"  Background rho0:  {self.rho0}")
        print(f"  Chemical pot mu:  {self.mu}")
        print(f"  Perturbation:     sigma={self.perturb_sigma:.3f}, dphi={self.perturb_phase:.4f}")
        vram_gb = self.N_total * 4 * 8 / 1e9  # ~8 float32 arrays
        print(f"  Est. VRAM usage:  {vram_gb:.2f} GB")
        print("=" * 65)


# =====================================================================
# GPU Kernel Manager
# =====================================================================
class GPKernels:
    def __init__(self):
        kernel_path = os.path.join(os.path.dirname(__file__) or '.', 'uhf_gp_kernels.cu')
        with open(kernel_path, 'r') as f:
            src = f.read()

        self.mod = cp.RawModule(code=src)
        self.nonlinear_halfstep = self.mod.get_function('nonlinear_halfstep')
        self.kinetic_fullstep = self.mod.get_function('kinetic_fullstep')
        self.compute_density = self.mod.get_function('compute_density')
        self.compute_phase = self.mod.get_function('compute_phase')
        self.inject_perturbation = self.mod.get_function('inject_perturbation')
        self.sample_sphere = self.mod.get_function('sample_sphere')
        self.imprint_trefoil_kernel = self.mod.get_function('imprint_trefoil_kernel')

        # GPU warm-up: run a trivial kernel to force GPU clocks up (avoids WDDM TDR)
        _warm = cp.zeros(1024, dtype=cp.float32)
        self.compute_density((4,), (256,), (_warm, _warm, _warm, np.int32(1024)))
        cp.cuda.Stream.null.synchronize()
        del _warm

    def launch(self, kernel, N, *args):
        block = 256
        grid = (N + block - 1) // block
        kernel((grid,), (block,), args)


# =====================================================================
# Trefoil Knot Phase Imprinting
# =====================================================================
def imprint_trefoil(psi_re, psi_im, p, kernels):
    """
    Imprint a T(2,3) trefoil vortex using a GPU kernel.
    For each grid point, computes distance to nearest curve point
    and accumulated phase winding — entirely on GPU, O(N^3 * N_curve).
    """
    N_curve = 500
    tc = np.linspace(0, 2*np.pi, N_curve, endpoint=False).astype(np.float32)
    scale = 4.0 * p.xi
    curve_x = (scale * (np.sin(tc) + 2.0 * np.sin(2*tc)) / 3.0).astype(np.float32)
    curve_y = (scale * (np.cos(tc) - 2.0 * np.cos(2*tc)) / 3.0).astype(np.float32)
    curve_z = (scale * (-np.sin(3*tc)) / 3.0).astype(np.float32)

    d_cx = cp.asarray(curve_x)
    d_cy = cp.asarray(curve_y)
    d_cz = cp.asarray(curve_z)

    print(f"  Imprinting trefoil vortex on GPU ({p.N}^3 grid, {N_curve} curve pts)...")

    N3 = p.N ** 3
    block = 256
    grid = (N3 + block - 1) // block

    max_attempts = 3
    for attempt in range(max_attempts):
        psi_re[:] = 0
        psi_im[:] = 0
        cp.cuda.Stream.null.synchronize()

        kernels.imprint_trefoil_kernel(
            (grid,), (block,),
            (psi_re, psi_im,
             d_cx, d_cy, d_cz, np.int32(N_curve),
             np.float32(-p.L), np.float32(p.dx),
             np.int32(p.N), np.int32(p.N), np.int32(p.N),
             np.float32(p.xi), np.float32(p.rho0))
        )
        cp.cuda.Stream.null.synchronize()

        if not bool(cp.any(cp.isnan(psi_re))):
            break
        print(f"  WARNING: NaN detected in imprint (attempt {attempt+1}/{max_attempts}), "
              f"retrying (WDDM TDR suspected)...")
    else:
        print("  FATAL: Imprint produces NaN after all retries. GPU may need reset.")
        sys.exit(1)

    rho_check = psi_re**2 + psi_im**2
    print(f"  Trefoil imprinted. Core scale = {scale:.2f}, rho range = "
          f"[{float(cp.min(rho_check)):.4f}, {float(cp.max(rho_check)):.4f}]")
    del rho_check


# =====================================================================
# Build k^2 grid for spectral Laplacian
# =====================================================================
def build_k2_grid(p):
    """Compute k^2 on the 3D Fourier grid, slab-by-slab to save memory."""
    N = p.N
    freq = np.fft.fftfreq(N, d=p.dx).astype(np.float32) * 2.0 * np.pi
    kx2 = freq ** 2
    # Build on GPU directly, slab by slab in kx
    k2_gpu = cp.zeros(N * N * N, dtype=cp.float32)
    ky2 = cp.asarray(freq ** 2)
    kz2 = cp.asarray(freq ** 2)
    ky2_grid = cp.broadcast_to(ky2[:, None], (N, N))
    kz2_grid = cp.broadcast_to(kz2[None, :], (N, N))
    kyz2 = (ky2_grid + kz2_grid).ravel()  # N*N
    for ix in range(N):
        offset = ix * N * N
        k2_gpu[offset:offset + N * N] = kyz2 + float(kx2[ix])
    return k2_gpu


# =====================================================================
# Spherical Boundary Sampling Points
# =====================================================================
def generate_sphere_points(R, N_pts=2048):
    """Generate approximately uniform points on a sphere using Fibonacci spiral."""
    indices = np.arange(N_pts, dtype=np.float32)
    golden = (1.0 + np.sqrt(5.0)) / 2.0

    theta = np.arccos(1.0 - 2.0 * (indices + 0.5) / N_pts)
    phi = 2.0 * np.pi * indices / golden

    sx = R * np.sin(theta) * np.cos(phi)
    sy = R * np.sin(theta) * np.sin(phi)
    sz = R * np.cos(theta)

    return sx.astype(np.float32), sy.astype(np.float32), sz.astype(np.float32)


# =====================================================================
# Split-Step Spectral GP Solver
# =====================================================================
class GPSolver:
    def __init__(self, params):
        self.p = params
        self.kernels = GPKernels()
        self.N3 = params.N_total

        # Allocate field arrays (split Re/Im for kernel compatibility)
        self.psi_re = cp.zeros(self.N3, dtype=cp.float32)
        self.psi_im = cp.zeros(self.N3, dtype=cp.float32)

        # Density and phase fields
        self.rho = cp.zeros(self.N3, dtype=cp.float32)
        self.phase = cp.zeros(self.N3, dtype=cp.float32)

        # k^2 grid
        print("  Building k^2 spectral grid...")
        self.k2 = build_k2_grid(params)

        # Boundary sampling
        N_boundary = 4096
        sx, sy, sz = generate_sphere_points(params.R_boundary, N_boundary)
        self.d_sx = cp.asarray(sx)
        self.d_sy = cp.asarray(sy)
        self.d_sz = cp.asarray(sz)
        self.N_boundary = N_boundary
        self.boundary_rho = cp.zeros(N_boundary, dtype=cp.float32)
        self.boundary_phase = cp.zeros(N_boundary, dtype=cp.float32)

        # Pre-perturbation baselines
        self.rho_baseline = None
        self.phase_baseline = None

        print(f"  Solver initialized. {self.N3:,} grid points, "
              f"{N_boundary} boundary samples.")

    def initialize_field(self):
        """Set initial condition: trefoil vortex in uniform condensate."""
        print("\n--- Initializing Wavefunction ---")
        imprint_trefoil(self.psi_re, self.psi_im, self.p, self.kernels)
        sys.stdout.flush()

        # Normalize to rho0
        print("  Computing density for normalization...", flush=True)
        Nt = np.int32(self.N3)
        self.kernels.launch(self.kernels.compute_density, self.N3,
                            self.psi_re, self.psi_im, self.rho, Nt)
        cp.cuda.Stream.null.synchronize()
        print("  Density computed.", flush=True)
        
        mean_rho = float(cp.mean(self.rho))
        print(f"  Mean density before normalization: {mean_rho:.6f}", flush=True)
        
        if mean_rho > 0:
            scale = np.sqrt(self.p.rho0 / mean_rho)
            self.psi_re *= np.float32(scale)
            self.psi_im *= np.float32(scale)

        # Recompute
        self.kernels.launch(self.kernels.compute_density, self.N3,
                            self.psi_re, self.psi_im, self.rho, Nt)
        cp.cuda.Stream.null.synchronize()
        print(f"  Mean density after normalization: {float(cp.mean(self.rho)):.6f}", flush=True)

    def split_step(self):
        """One full split-step spectral time step."""
        p = self.p
        Nt = np.int32(self.N3)
        half_dt = np.float32(p.dt / 2.0)
        dt = np.float32(p.dt)
        g = np.float32(p.g)
        mu = np.float32(p.mu)
        shape3 = (p.N, p.N, p.N)

        # 1. Nonlinear half-step
        self.kernels.launch(self.kernels.nonlinear_halfstep, self.N3,
                            self.psi_re, self.psi_im, g, mu, half_dt, Nt)

        # 2. Pack into complex64, FFT, apply kinetic, IFFT
        psi_c = (self.psi_re.reshape(shape3) + 1j * self.psi_im.reshape(shape3)).astype(cp.complex64)
        psi_k = fftn(psi_c)
        del psi_c

        # 3. Apply kinetic phase in-place (work on flat view)
        k2_3d = self.k2.reshape(shape3)
        phase_k = (-dt * 0.5 * k2_3d).astype(cp.float32)
        rot = cp.exp(1j * phase_k.astype(cp.float32))
        psi_k *= rot
        del phase_k, rot, k2_3d

        # 4. IFFT back
        psi_x = ifftn(psi_k)
        del psi_k

        self.psi_re[:] = psi_x.real.ravel().astype(cp.float32)
        self.psi_im[:] = psi_x.imag.ravel().astype(cp.float32)
        del psi_x

        # 5. Nonlinear half-step
        self.kernels.launch(self.kernels.nonlinear_halfstep, self.N3,
                            self.psi_re, self.psi_im, g, mu, half_dt, Nt)

    def sample_boundary(self):
        """Sample density and phase at boundary points."""
        p = self.p

        # Compute density and phase fields
        Nt = np.int32(self.N3)
        self.kernels.launch(self.kernels.compute_density, self.N3,
                            self.psi_re, self.psi_im, self.rho, Nt)
        self.kernels.launch(self.kernels.compute_phase, self.N3,
                            self.psi_re, self.psi_im, self.phase, Nt)

        # Sample on sphere
        block = 256
        grid = (self.N_boundary + block - 1) // block
        self.kernels.sample_sphere(
            (grid,), (block,),
            (self.rho, self.phase,
             self.d_sx, self.d_sy, self.d_sz,
             self.boundary_rho, self.boundary_phase,
             np.float32(-p.L), np.float32(-p.L), np.float32(-p.L),
             np.float32(1.0 / p.dx),
             np.int32(p.N), np.int32(p.N), np.int32(p.N),
             np.int32(self.N_boundary))
        )
        cp.cuda.Stream.null.synchronize()

    def measure_signals(self):
        """
        Compute the two independent observables at the boundary:
          Signal A: mean density deviation from baseline (acoustic)
          Signal B: phase holonomy deviation from baseline (topological)
        """
        self.sample_boundary()

        rho_now = self.boundary_rho.copy()
        phase_now = self.boundary_phase.copy()

        if self.rho_baseline is None:
            self.rho_baseline = rho_now.copy()
            self.phase_baseline = phase_now.copy()

        # Acoustic signal: RMS density fluctuation relative to baseline
        drho = rho_now - self.rho_baseline
        sig_acoustic = float(cp.sqrt(cp.mean(drho ** 2)))

        # Phase signal: mean absolute phase deviation
        # Use circular difference to handle wrapping
        dphase = phase_now - self.phase_baseline
        # Wrap to [-pi, pi]
        dphase = cp.arctan2(cp.sin(dphase), cp.cos(dphase))
        sig_phase = float(cp.mean(cp.abs(dphase)))

        return sig_acoustic, sig_phase

    def sample_shell(self, R, d_sx, d_sy, d_sz, N_pts, out_rho, out_phase):
        """Sample density and phase at a spherical shell of given radius."""
        p = self.p
        block = 256
        grid = (N_pts + block - 1) // block
        self.kernels.sample_sphere(
            (grid,), (block,),
            (self.rho, self.phase,
             d_sx, d_sy, d_sz,
             out_rho, out_phase,
             np.float32(-p.L), np.float32(-p.L), np.float32(-p.L),
             np.float32(1.0 / p.dx),
             np.int32(p.N), np.int32(p.N), np.int32(p.N),
             np.int32(N_pts))
        )

    def setup_multi_shell(self, radii):
        """Create sampling points for multiple spherical shells."""
        self.shell_radii = radii
        self.shell_data = []
        N_pts = 2048
        for R in radii:
            sx, sy, sz = generate_sphere_points(R, N_pts)
            sd = {
                'R': R,
                'N': N_pts,
                'sx': cp.asarray(sx),
                'sy': cp.asarray(sy),
                'sz': cp.asarray(sz),
                'rho': cp.zeros(N_pts, dtype=cp.float32),
                'phase': cp.zeros(N_pts, dtype=cp.float32),
                'rho_base': None,
                'phase_base': None,
            }
            self.shell_data.append(sd)

    def measure_all_shells(self):
        """Measure density and phase at all shells, return per-shell signals."""
        Nt = np.int32(self.N3)
        self.kernels.launch(self.kernels.compute_density, self.N3,
                            self.psi_re, self.psi_im, self.rho, Nt)
        self.kernels.launch(self.kernels.compute_phase, self.N3,
                            self.psi_re, self.psi_im, self.phase, Nt)
        cp.cuda.Stream.null.synchronize()

        results = []
        for sd in self.shell_data:
            self.sample_shell(sd['R'], sd['sx'], sd['sy'], sd['sz'],
                              sd['N'], sd['rho'], sd['phase'])
        cp.cuda.Stream.null.synchronize()

        for sd in self.shell_data:
            rho_now = sd['rho'].copy()
            phase_now = sd['phase'].copy()

            if sd['rho_base'] is None:
                sd['rho_base'] = rho_now.copy()
                sd['phase_base'] = phase_now.copy()

            drho = rho_now - sd['rho_base']
            sig_a = float(cp.sqrt(cp.mean(drho ** 2)))

            dphase = phase_now - sd['phase_base']
            dphase = cp.arctan2(cp.sin(dphase), cp.cos(dphase))
            sig_p = float(cp.mean(cp.abs(dphase)))

            results.append((sig_a, sig_p))

        return results

    def inject_core_perturbation(self):
        """Inject localized gauge twist at the trefoil center."""
        p = self.p
        print("\n  >>> INJECTING PERTURBATION at knot center (0,0,0)")
        print(f"      sigma = {p.perturb_sigma:.3f}, delta_phi = {p.perturb_phase:.4f}")

        self.kernels.launch(
            self.kernels.inject_perturbation, self.N3,
            self.psi_re, self.psi_im,
            np.float32(0.0), np.float32(0.0), np.float32(0.0),  # center
            np.float32(p.perturb_sigma),
            np.float32(p.perturb_phase),
            np.float32(-p.L), np.float32(-p.L), np.float32(-p.L),  # grid origin
            np.float32(p.dx),
            np.int32(p.N), np.int32(p.N), np.int32(p.N)
        )
        cp.cuda.Stream.null.synchronize()

    def run(self, n_steps_pre=50, n_steps_post=300):
        """
        Main simulation loop with multi-shell measurement:
          1. Relaxation: evolve to settle transients
          2. Set up measurement shells at multiple radii
          3. Collect baseline noise during additional relaxation
          4. Inject perturbation
          5. Track signals at all shells and detect arrival times
        """
        p = self.p
        steps_per_acoustic = int(np.ceil(p.t_acoustic / p.dt))

        # --- Relaxation ---
        print("\n" + "=" * 65)
        print("  PHASE 1: RELAXATION")
        print("=" * 65)
        t_start = time.time()

        for step in range(n_steps_pre):
            self.split_step()
            if (step + 1) % 10 == 0:
                self.kernels.launch(self.kernels.compute_density, self.N3,
                                    self.psi_re, self.psi_im, self.rho,
                                    np.int32(self.N3))
                rho_mean = float(cp.mean(self.rho))
                rho_max = float(cp.max(self.rho))
                print(f"  Relaxation step {step+1}/{n_steps_pre}: "
                      f"<rho>={rho_mean:.6f}, max(rho)={rho_max:.4f}")

        t_relax = time.time() - t_start
        print(f"  Relaxation complete ({t_relax:.1f}s)")

        # --- Setup multi-shell measurement ---
        print("\n" + "=" * 65)
        print("  PHASE 2: MULTI-SHELL BASELINE")
        print("=" * 65)

        # 5 shells at logarithmically spaced radii from 3xi to 15xi
        shell_radii = [3.0 * p.xi, 5.0 * p.xi, 7.5 * p.xi,
                        10.0 * p.xi, p.R_boundary]
        self.setup_multi_shell(shell_radii)

        # Set baselines
        _ = self.measure_all_shells()

        # Collect noise statistics over 20 more relaxation steps
        noise_samples = {i: {'a': [], 'p': []} for i in range(len(shell_radii))}
        n_noise = 20
        for step in range(n_noise):
            self.split_step()
            sigs = self.measure_all_shells()
            for i, (sa, sp) in enumerate(sigs):
                noise_samples[i]['a'].append(sa)
                noise_samples[i]['p'].append(sp)

        # Compute noise floor: mean + 3*std of relaxation fluctuations
        noise_floor = []
        for i in range(len(shell_radii)):
            a_vals = np.array(noise_samples[i]['a'])
            p_vals = np.array(noise_samples[i]['p'])
            # Use 5x the max noise as threshold
            thresh_a = max(np.max(a_vals) * 5.0, 1e-5)
            thresh_p = max(np.max(p_vals) * 5.0, 1e-5)
            noise_floor.append((thresh_a, thresh_p))
            print(f"  Shell R={shell_radii[i]/p.xi:.1f}xi: "
                  f"noise_a={np.max(a_vals):.2e} (thresh={thresh_a:.2e}), "
                  f"noise_p={np.max(p_vals):.2e} (thresh={thresh_p:.2e})")

        # Reset baselines for post-perturbation measurement
        for sd in self.shell_data:
            sd['rho_base'] = None
            sd['phase_base'] = None
        _ = self.measure_all_shells()  # Set fresh baselines

        # --- Inject perturbation ---
        print("\n" + "=" * 65)
        print("  PHASE 3: PERTURBATION INJECTION")
        print("=" * 65)
        self.inject_core_perturbation()

        # Measure immediately (dt=0)
        sigs_imm = self.measure_all_shells()
        print(f"\n  IMMEDIATE (dt=0) post-perturbation:")
        for i, (sa, sp) in enumerate(sigs_imm):
            R_xi = shell_radii[i] / p.xi
            print(f"    Shell R={R_xi:.1f}xi: acoustic={sa:.4e}, phase={sp:.4e}")

        # --- Evolve and track ---
        print("\n" + "=" * 65)
        print("  PHASE 4: SIGNAL TRACKING")
        print(f"  Expected acoustic crossing times:")
        for R in shell_radii:
            print(f"    R={R/p.xi:.1f}xi: t_expected = {R/p.cs:.4f}")
        print("=" * 65)

        n_shells = len(shell_radii)
        dt_acoustic = [None] * n_shells
        dt_phase = [None] * n_shells

        # Check immediate triggers
        for i, (sa, sp) in enumerate(sigs_imm):
            if sa > noise_floor[i][0]:
                dt_acoustic[i] = 0.0
            if sp > noise_floor[i][1]:
                dt_phase[i] = 0.0

        history = []
        measure_interval = max(1, steps_per_acoustic // 200)
        total_steps = min(n_steps_post, steps_per_acoustic * 2)

        t_run_start = time.time()
        for step in range(1, total_steps + 1):
            self.split_step()
            t_current = step * p.dt

            if step % measure_interval == 0 or step <= 10:
                sigs = self.measure_all_shells()
                history.append((t_current, sigs))

                for i, (sa, sp) in enumerate(sigs):
                    if dt_acoustic[i] is None and sa > noise_floor[i][0]:
                        dt_acoustic[i] = t_current
                        R_xi = shell_radii[i] / p.xi
                        print(f"  ** ACOUSTIC at R={R_xi:.1f}xi: t={t_current:.4f} "
                              f"(expected={shell_radii[i]/p.cs:.4f}, "
                              f"ratio={t_current/(shell_radii[i]/p.cs):.3f})")
                    if dt_phase[i] is None and sp > noise_floor[i][1]:
                        dt_phase[i] = t_current
                        R_xi = shell_radii[i] / p.xi
                        print(f"  ** PHASE at R={R_xi:.1f}xi: t={t_current:.4f}")

                if step % (measure_interval * 20) == 0:
                    elapsed = time.time() - t_run_start
                    rate = step / elapsed if elapsed > 0 else 0
                    inner_sa, inner_sp = sigs[0]
                    outer_sa, outer_sp = sigs[-1]
                    print(f"    t={t_current:.4f}  inner_a={inner_sa:.2e}  "
                          f"outer_a={outer_sa:.2e}  [{rate:.1f} steps/s]")

                # Early exit if all shells detected
                if all(d is not None for d in dt_acoustic) and \
                   all(d is not None for d in dt_phase):
                    if t_current > max(d for d in dt_acoustic if d is not None) * 1.2:
                        break

        t_total = time.time() - t_run_start

        # ============================================================
        # ANALYSIS
        # ============================================================
        print("\n" + "=" * 65)
        print("  RESULTS: MULTI-SHELL SIGNAL ARRIVAL")
        print("=" * 65)
        print(f"  {'Shell':>8s}  {'R/xi':>6s}  {'t_expect':>8s}  "
              f"{'t_acoustic':>10s}  {'t_phase':>10s}  {'ratio_a':>8s}")
        print(f"  {'─'*8}  {'─'*6}  {'─'*8}  {'─'*10}  {'─'*10}  {'─'*8}")

        for i in range(n_shells):
            R_xi = shell_radii[i] / p.xi
            t_exp = shell_radii[i] / p.cs
            ta_str = f"{dt_acoustic[i]:.4f}" if dt_acoustic[i] is not None else "---"
            tp_str = f"{dt_phase[i]:.4f}" if dt_phase[i] is not None else "---"
            ratio = f"{dt_acoustic[i]/t_exp:.3f}" if dt_acoustic[i] is not None else "---"
            print(f"  {i:>8d}  {R_xi:>6.1f}  {t_exp:>8.4f}  "
                  f"{ta_str:>10s}  {tp_str:>10s}  {ratio:>8s}")

        # Check for non-locality: phase arrives at dt=0 at ALL shells
        phase_instant = sum(1 for d in dt_phase
                            if d is not None and d < 2 * p.dt)
        acoustic_delayed = sum(1 for i, d in enumerate(dt_acoustic)
                               if d is not None and d > shell_radii[i] / p.cs * 0.3)

        print()
        if phase_instant == n_shells and acoustic_delayed >= n_shells - 1:
            print("  >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
            print("  >> PHYSICAL NON-LOCALITY IS CONFIRMED            ")
            print("  >> Phase topology propagates OUTSIDE sonic cone   ")
            print("  >> Phase: instantaneous at ALL shells             ")
            print("  >> Acoustic: delayed proportional to R/c_s        ")
            print("  >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
        elif phase_instant == 0 and acoustic_delayed == 0:
            print("  >> Insufficient signal at any shell.")
            print("  >> Increase perturbation strength or resolution.")
        elif all(dt_phase[i] is not None and dt_acoustic[i] is not None
                 and abs(dt_phase[i] - dt_acoustic[i]) < 3 * p.dt
                 for i in range(n_shells)):
            print("  >> STRICTLY LOCAL: phase and acoustic co-propagate.")
            print("  >> The non-locality was a Biot-Savart artifact.")
        else:
            # Check if acoustic arrival times scale linearly with R
            detected_a = [(shell_radii[i], dt_acoustic[i])
                          for i in range(n_shells) if dt_acoustic[i] is not None]
            if len(detected_a) >= 3:
                Rs = np.array([r for r, _ in detected_a])
                ts = np.array([t for _, t in detected_a])
                slope = np.polyfit(Rs, ts, 1)[0] if len(Rs) >= 2 else 0
                print(f"  >> Acoustic velocity from fit: c_fit = {1.0/slope:.4f} "
                      f"(expected c_s = {p.cs:.4f})")

            detected_p = [(shell_radii[i], dt_phase[i])
                          for i in range(n_shells) if dt_phase[i] is not None]
            if len(detected_p) >= 2:
                Rs_p = np.array([r for r, _ in detected_p])
                ts_p = np.array([t for _, t in detected_p])
                if np.std(ts_p) < p.dt * 2:
                    print("  >> Phase signal is SIMULTANEOUS across shells.")
                    print("  >> NON-LOCAL TOPOLOGY CONFIRMED.")
                else:
                    slope_p = np.polyfit(Rs_p, ts_p, 1)[0] if len(Rs_p) >= 2 else 0
                    if slope_p > 0:
                        print(f"  >> Phase velocity from fit: c_phase = {1.0/slope_p:.4f}")
                    else:
                        print("  >> Phase arrives before causality permits.")

        print(f"\n  Simulation time: {t_total:.1f}s ({step} steps)")
        if t_total > 0:
            print(f"  Performance: {step/t_total:.1f} steps/s")

        return history


# =====================================================================
# MAIN
# =====================================================================
if __name__ == "__main__":
    # Determine optimal grid size based on available VRAM
    dev = cp.cuda.Device(0)
    free_mem, total_mem = dev.mem_info

    # Memory budget: need ~10 float32 arrays of N^3 + FFT scratch
    # Safe estimate: 12 * N^3 * 4 bytes
    max_N3 = int(free_mem * 0.70 / (12 * 4))  # use 70% of free VRAM
    max_N = int(max_N3 ** (1.0/3.0))
    # Round down to multiple of 32 for GPU efficiency
    max_N = (max_N // 32) * 32
    # Cap at 512 (above this FFT becomes the bottleneck)
    max_N = min(max_N, 512)

    print(f"GPU 0: {free_mem/1e9:.2f} GB free -> max grid N={max_N}")
    print(f"Selecting N={max_N} for maximum resolution.")
    print()

    p = GPParams(N=max_N)
    p.report()

    solver = GPSolver(p)
    solver.initialize_field()

    # Run with enough post-perturbation steps to reach 2x acoustic time
    n_post = int(np.ceil(2.0 * p.t_acoustic / p.dt))
    n_post = max(n_post, 200)
    history = solver.run(n_steps_pre=50, n_steps_post=n_post)
