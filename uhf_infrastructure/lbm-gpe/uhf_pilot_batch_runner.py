"""
UHF Pilot Batch Runner – Blind Communication Protocol (Phase 1)
================================================================
200 trials (100 × Bit '0', 100 × Bit '1'), alternating order.

Null-A noise model:
  σ_θ = 0.01 rad   (phase noise on full grid before each trial)
  σ_ρ = 1e-3 ρ₀    (density noise on full grid before each trial)

Solver:  RK4 + 4th-order FD Laplacian.  NO FFT.  Strictly local stencil.
Grid:    N = 320,  Δx = 0.25 ξ,  d = 20 ξ,  boundary = 30 ξ.

Output:  pilot_observables.csv
  columns: trial_id, bit_label, time, O_phi, O_rho
"""

import os, sys, time
import numpy as np

os.environ["LD_LIBRARY_PATH"] = "/usr/lib/wsl/lib"

try:
    import cupy as cp
except ImportError:
    print("FATAL: CuPy required.")
    sys.exit(1)

# ==================================================================
# Protocol constants
# ==================================================================
N_TRIALS = 200
MEAS_EVERY = 10          # measure every 10 RK4 steps
CSV_FILENAME = "pilot_observables.csv"


# ==================================================================
# Physical parameters (natural units: ℏ = m = g = ρ₀ = 1)
# ==================================================================
class PilotParams:
    def __init__(self):
        self.g = 1.0
        self.rho0 = 1.0
        self.mu = 1.0
        self.cs = 1.0
        self.xi = 1.0 / np.sqrt(2.0)

        # Spec in units of ξ
        d_xi = 20.0
        clearance_xi = 30.0
        dx_xi = 0.25
        L_half_xi = d_xi / 2.0 + clearance_xi  # 40 ξ

        # Grid
        self.N = int(round(2.0 * L_half_xi / dx_xi))   # 320
        self.N = (self.N // 32) * 32                    # GPU-friendly
        self.N_total = self.N ** 3
        self.L = L_half_xi * self.xi                    # half-box (natural)
        self.dx = 2.0 * self.L / self.N
        self.dt = 0.15 * self.dx ** 2

        # Defect positions (natural units)
        self.x_A = -(d_xi / 2.0) * self.xi
        self.x_B = +(d_xi / 2.0) * self.xi
        self.D = d_xi * self.xi

        # Vortex ring
        self.R_ring = 3.0 * self.xi

        # Perturbation
        self.sigma = 0.5 * self.xi
        self.void = 0.01
        self.dphi_bit0 = +np.pi / 2.0
        self.dphi_bit1 = -np.pi / 2.0

        # Injection at ring A center
        self.inject_x = self.x_A
        self.inject_y = 0.0
        self.inject_z = 0.0

        # Noise model
        self.sigma_theta = 0.01     # rad
        self.sigma_rho = 1e-3       # ρ₀ = 1

        # Probe at B
        self.R_B = 1.5 * self.xi
        self.N_probe = 256

        # Timescales
        self.t_acoustic = self.D / self.cs
        k_dom = 1.0 / self.sigma
        eps_dom = np.sqrt(k_dom ** 2 + k_dom ** 4 / 4.0)
        self.v_uv = (k_dom + k_dom ** 3) / eps_dom
        self.t_uv = self.D / self.v_uv


# ==================================================================
# CUDA kernel manager
# ==================================================================
_kernels = None


def get_kernels():
    global _kernels
    if _kernels is None:
        kp = os.path.join(os.path.dirname(__file__) or '.', 'uhf_gp_kernels.cu')
        with open(kp) as f:
            src = f.read()
        mod = cp.RawModule(code=src)
        _kernels = type('K', (), {
            'compute_density':      mod.get_function('compute_density'),
            'compute_phase':        mod.get_function('compute_phase'),
            'sample_sphere':        mod.get_function('sample_sphere'),
            'imprint_trefoil_kernel': mod.get_function('imprint_trefoil_kernel'),
            'gp_rhs_fd4':          mod.get_function('gp_rhs_fd4'),
            'inject_phase_and_void': mod.get_function('inject_phase_and_void'),
        })()
        _w = cp.zeros(1024, dtype=cp.float32)
        _kernels.compute_density((4,), (256,), (_w, _w, _w, np.int32(1024)))
        cp.cuda.Stream.null.synchronize()
    return _kernels


# ==================================================================
# Vortex ring imprinting (Abrikosov product ansatz)
# ==================================================================
def make_ring_curve(cx, cy, cz, R, axis='x', N_pts=256):
    t = np.linspace(0, 2 * np.pi, N_pts, endpoint=False).astype(np.float32)
    if axis == 'x':
        xs = np.full(N_pts, cx, dtype=np.float32)
        ys = (cy + R * np.cos(t)).astype(np.float32)
        zs = (cz + R * np.sin(t)).astype(np.float32)
    elif axis == 'y':
        xs = (cx + R * np.cos(t)).astype(np.float32)
        ys = np.full(N_pts, cy, dtype=np.float32)
        zs = (cz + R * np.sin(t)).astype(np.float32)
    else:
        xs = (cx + R * np.cos(t)).astype(np.float32)
        ys = (cy + R * np.sin(t)).astype(np.float32)
        zs = np.full(N_pts, cz, dtype=np.float32)
    return xs, ys, zs


def imprint_single_ring(psi_re, psi_im, p, K, cx, cy, cz, R, axis='x'):
    N_curve = 256
    xs, ys, zs = make_ring_curve(cx, cy, cz, R, axis, N_curve)
    d_cx = cp.asarray(xs)
    d_cy = cp.asarray(ys)
    d_cz = cp.asarray(zs)
    N3 = p.N_total
    block, grid = 256, (N3 + 255) // 256

    for _ in range(3):
        psi_re[:] = 0
        psi_im[:] = 0
        cp.cuda.Stream.null.synchronize()
        K.imprint_trefoil_kernel(
            (grid,), (block,),
            (psi_re, psi_im, d_cx, d_cy, d_cz, np.int32(N_curve),
             np.float32(-p.L), np.float32(p.dx),
             np.int32(p.N), np.int32(p.N), np.int32(p.N),
             np.float32(p.xi), np.float32(p.rho0)))
        cp.cuda.Stream.null.synchronize()
        if not bool(cp.any(cp.isnan(psi_re))):
            break
    else:
        print("  WARNING: NaN in ring imprint")
        return False

    rho = psi_re ** 2 + psi_im ** 2
    rmin, rmax = float(cp.min(rho)), float(cp.max(rho))
    print(f"    Ring at ({cx:.3f},{cy:.1f},{cz:.1f}) R={R:.4f}: "
          f"rho=[{rmin:.4f}, {rmax:.4f}]")
    return True


def imprint_two_rings(solver):
    p = solver.p
    K = solver.K
    N3 = p.N_total

    print("  Imprinting Ring A...")
    ok = imprint_single_ring(solver.psi_re, solver.psi_im, p, K,
                             p.x_A, 0.0, 0.0, p.R_ring, 'x')
    if not ok:
        return False

    save_re = solver.psi_re.copy()
    save_im = solver.psi_im.copy()

    print("  Imprinting Ring B...")
    ok = imprint_single_ring(solver.psi_re, solver.psi_im, p, K,
                             p.x_B, 0.0, 0.0, p.R_ring, 'x')
    if not ok:
        return False

    # Abrikosov product
    inv_sqrt_rho0 = np.float32(1.0 / np.sqrt(p.rho0))
    new_re = (save_re * solver.psi_re - save_im * solver.psi_im) * inv_sqrt_rho0
    new_im = (save_re * solver.psi_im + save_im * solver.psi_re) * inv_sqrt_rho0
    solver.psi_re[:] = new_re
    solver.psi_im[:] = new_im
    del save_re, save_im, new_re, new_im
    cp.get_default_memory_pool().free_all_blocks()

    # Normalize mean density
    K.compute_density(((N3 + 255) // 256,), (256,),
                      (solver.psi_re, solver.psi_im, solver.rho, np.int32(N3)))
    cp.cuda.Stream.null.synchronize()
    m = float(cp.mean(solver.rho))
    if m > 0:
        s = np.float32(np.sqrt(p.rho0 / m))
        solver.psi_re *= s
        solver.psi_im *= s
    rmin = float(cp.min(solver.rho))
    rmax = float(cp.max(solver.rho))
    print(f"  Two-ring product: <rho>={m:.6f} -> normalized")
    print(f"  Combined rho: [{rmin:.4f}, {rmax:.4f}]")
    return True


# ==================================================================
# Shell probes (golden spiral on sphere)
# ==================================================================
def make_shell_probes(x_center, y_center, z_center, R, N_pts=256):
    idx = np.arange(N_pts, dtype=np.float32)
    golden = (1 + np.sqrt(5)) / 2
    theta = np.arccos(1 - 2 * (idx + 0.5) / N_pts)
    phi = 2 * np.pi * idx / golden
    px = (x_center + R * np.sin(theta) * np.cos(phi)).astype(np.float32)
    py = (y_center + R * np.sin(theta) * np.sin(phi)).astype(np.float32)
    pz = (z_center + R * np.cos(theta)).astype(np.float32)
    return px, py, pz


# ==================================================================
# RK4 + FD4 Solver
# ==================================================================
class PilotSolver:
    def __init__(self, params):
        self.p = params
        self.K = get_kernels()
        self.N3 = params.N_total
        z = lambda: cp.zeros(self.N3, dtype=cp.float32)
        self.psi_re = z()
        self.psi_im = z()
        self.rho = z()
        self.phase = z()
        self.rhs_re = z()
        self.rhs_im = z()
        self.tmp_re = z()
        self.tmp_im = z()
        self.acc_re = z()
        self.acc_im = z()
        self.inv_12dx2 = np.float32(1.0 / (12.0 * params.dx ** 2))
        self.probes = {}

    def compute_rhs(self, in_re, in_im, out_re, out_im):
        p = self.p
        self.K.gp_rhs_fd4(
            ((self.N3 + 255) // 256,), (256,),
            (in_re, in_im, out_re, out_im,
             np.float32(p.g), np.float32(p.mu), self.inv_12dx2,
             np.int32(p.N), np.int32(p.N), np.int32(p.N)))

    def rk4_step(self):
        dt = np.float32(self.p.dt)
        dt2 = np.float32(self.p.dt * 0.5)
        dt6 = np.float32(self.p.dt / 6.0)
        dt3 = np.float32(self.p.dt / 3.0)
        # k1
        self.compute_rhs(self.psi_re, self.psi_im, self.rhs_re, self.rhs_im)
        cp.multiply(self.rhs_re, dt6, out=self.acc_re)
        cp.multiply(self.rhs_im, dt6, out=self.acc_im)
        cp.add(self.psi_re, self.rhs_re * dt2, out=self.tmp_re)
        cp.add(self.psi_im, self.rhs_im * dt2, out=self.tmp_im)
        # k2
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += self.rhs_re * dt3
        self.acc_im += self.rhs_im * dt3
        cp.add(self.psi_re, self.rhs_re * dt2, out=self.tmp_re)
        cp.add(self.psi_im, self.rhs_im * dt2, out=self.tmp_im)
        # k3
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += self.rhs_re * dt3
        self.acc_im += self.rhs_im * dt3
        cp.add(self.psi_re, self.rhs_re * dt, out=self.tmp_re)
        cp.add(self.psi_im, self.rhs_im * dt, out=self.tmp_im)
        # k4
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += self.rhs_re * dt6
        self.acc_im += self.rhs_im * dt6
        # Accumulate
        self.psi_re += self.acc_re
        self.psi_im += self.acc_im

    def setup_probes(self, probe_dict):
        self.probes = {}
        for label, (px, py, pz) in probe_dict.items():
            N_pts = len(px)
            self.probes[label] = {
                'px': cp.asarray(px),
                'py': cp.asarray(py),
                'pz': cp.asarray(pz),
                'rho_out': cp.zeros(N_pts, dtype=cp.float32),
                'phase_out': cp.zeros(N_pts, dtype=cp.float32),
                'N': N_pts,
            }

    def observe_B(self):
        """Compute circular-mean phase and mean density at B probe sphere."""
        Nt = np.int32(self.N3)
        g = (self.N3 + 255) // 256
        self.K.compute_density((g,), (256,),
                               (self.psi_re, self.psi_im, self.rho, Nt))
        self.K.compute_phase((g,), (256,),
                             (self.psi_re, self.psi_im, self.phase, Nt))
        cp.cuda.Stream.null.synchronize()

        pr = self.probes['B']
        g2 = (pr['N'] + 255) // 256
        p = self.p
        self.K.sample_sphere(
            (g2,), (256,),
            (self.rho, self.phase,
             pr['px'], pr['py'], pr['pz'],
             pr['rho_out'], pr['phase_out'],
             np.float32(-p.L), np.float32(-p.L), np.float32(-p.L),
             np.float32(1.0 / p.dx),
             np.int32(p.N), np.int32(p.N), np.int32(p.N),
             np.int32(pr['N'])))
        cp.cuda.Stream.null.synchronize()

        rho_vals = pr['rho_out'].get()
        phase_vals = pr['phase_out'].get()
        O_rho = float(np.mean(rho_vals))
        O_phi = float(np.arctan2(np.mean(np.sin(phase_vals)),
                                  np.mean(np.cos(phase_vals))))
        return O_phi, O_rho

    def inject(self, dphi):
        """Inject UV perturbation (phase twist + density void) at A."""
        p = self.p
        g = (self.N3 + 255) // 256
        self.K.inject_phase_and_void(
            (g,), (256,),
            (self.psi_re, self.psi_im,
             np.float32(p.inject_x), np.float32(p.inject_y),
             np.float32(p.inject_z),
             np.float32(p.sigma),
             np.float32(dphi),
             np.float32(p.void),
             np.float32(-p.L), np.float32(-p.L), np.float32(-p.L),
             np.float32(p.dx),
             np.int32(p.N), np.int32(p.N), np.int32(p.N)))
        cp.cuda.Stream.null.synchronize()

    def inject_noise(self, trial_id):
        """Add stochastic IC noise (full-grid phase + density) via CuPy."""
        cp.random.seed(trial_id * 7919 + 42)
        N3 = self.N3
        p = self.p
        g = (N3 + 255) // 256

        # Decompose ψ → (ρ, θ)
        self.K.compute_density((g,), (256,),
                               (self.psi_re, self.psi_im, self.rho,
                                np.int32(N3)))
        self.K.compute_phase((g,), (256,),
                             (self.psi_re, self.psi_im, self.phase,
                              np.int32(N3)))
        cp.cuda.Stream.null.synchronize()

        # ρ_new = ρ + δρ,  clamped > 0
        self.rho += cp.random.normal(0, p.sigma_rho, N3, dtype=cp.float32)
        cp.maximum(self.rho, cp.float32(1e-10), out=self.rho)

        # θ_new = θ + δθ
        self.phase += cp.random.normal(0, p.sigma_theta, N3, dtype=cp.float32)

        # Reconstruct  ψ = √ρ · exp(iθ)  — all in-place
        cp.sqrt(self.rho, out=self.rho)            # rho → amplitude
        cp.cos(self.phase, out=self.psi_re)
        cp.sin(self.phase, out=self.psi_im)
        self.psi_re *= self.rho                    # amp · cos(θ)
        self.psi_im *= self.rho                    # amp · sin(θ)

        cp.get_default_memory_pool().free_all_blocks()


# ==================================================================
# Main pilot batch
# ==================================================================
def run_pilot():
    dev = cp.cuda.Device(0)
    free, total = dev.mem_info
    print(f"GPU 0: {free / 1e9:.2f} GB free / {total / 1e9:.2f} GB total")

    p = PilotParams()

    vram_needed = 12 * 4 * p.N_total
    if vram_needed > free * 0.9:
        print(f"  FATAL: Need {vram_needed / 1e9:.2f} GB, "
              f"only {free / 1e9:.2f} GB free")
        sys.exit(1)

    print(f"\n{'#' * 65}")
    print(f"  PILOT BATCH RUNNER – BLIND COMMUNICATION PROTOCOL")
    print(f"{'#' * 65}")
    print(f"  Solver:         RK4 + FD4 (strictly local, NO FFT)")
    print(f"  Grid:           {p.N}^3 = {p.N_total:,}")
    print(f"  Box:            [-{p.L:.4f}, +{p.L:.4f}]")
    print(f"  dx:             {p.dx:.5f}  "
          f"({p.dx / p.xi:.4f} xi, {p.xi / p.dx:.1f} cells/xi)")
    print(f"  dt:             {p.dt:.6f}")
    print(f"  Defect A:       x = {p.x_A:.4f}  ({p.x_A / p.xi:.1f} xi)")
    print(f"  Defect B:       x = {p.x_B:.4f}  ({p.x_B / p.xi:.1f} xi)")
    print(f"  Separation d:   {p.D:.4f}  (20.0 xi)")
    print(f"  Boundary:       {(p.L - abs(p.x_B)) / p.xi:.1f} xi from B")
    print(f"  Ring radius:    {p.R_ring:.4f}  (3.0 xi)")
    print(f"  Perturbation:   sigma={p.sigma:.4f} (0.5 xi), void={p.void}")
    print(f"  Bit '0':        dphi = +pi/2 = {p.dphi_bit0:+.4f}")
    print(f"  Bit '1':        dphi = -pi/2 = {p.dphi_bit1:+.4f}")
    print(f"  Inject at:      ({p.inject_x:.4f}, 0.0, 0.0)")
    print(f"  Noise:          sigma_theta={p.sigma_theta}, "
          f"sigma_rho={p.sigma_rho}")
    print(f"  Probe B:        R_B = {p.R_B:.4f} (1.5 xi), "
          f"{p.N_probe} pts")
    print(f"  t_acoustic:     {p.t_acoustic:.4f}")
    print(f"  t_UV (k_dom):   {p.t_uv:.4f}  (v_UV = {p.v_uv:.2f} c_s)")
    print(f"  Trials:         {N_TRIALS} (alternating bit 0/1)")
    print(f"  VRAM:           {vram_needed / 1e9:.2f} GB")
    sys.stdout.flush()

    # --- Create solver ---
    solver = PilotSolver(p)

    # --- Imprint two vortex rings ---
    print(f"\n  Imprinting two vortex rings (Abrikosov product)...")
    sys.stdout.flush()
    ok = imprint_two_rings(solver)
    if not ok:
        print("  FATAL: Failed to imprint vortex rings.")
        return

    # --- Setup probes at B ---
    bpx, bpy, bpz = make_shell_probes(p.x_B, 0.0, 0.0, p.R_B, p.N_probe)
    solver.setup_probes({'B': (bpx, bpy, bpz)})
    print(f"  Probe at B: shell R={p.R_B:.4f}, {p.N_probe} pts")

    # --- Relaxation ---
    n_relax = 100
    print(f"\n  Relaxation ({n_relax} steps)...")
    sys.stdout.flush()
    t_r0 = time.time()
    for s in range(n_relax):
        solver.rk4_step()
        if (s + 1) % 25 == 0:
            solver.K.compute_density(
                ((solver.N3 + 255) // 256,), (256,),
                (solver.psi_re, solver.psi_im, solver.rho,
                 np.int32(solver.N3)))
            rm = float(cp.mean(solver.rho))
            nc = int(cp.sum(cp.isnan(solver.psi_re)))
            print(f"    step {s + 1}: <rho>={rm:.6f}"
                  f"{'  *** NaN ***' if nc else ''}")
            sys.stdout.flush()
            if nc:
                print("  FATAL: NaN during relaxation.")
                return
    t_relax = time.time() - t_r0
    print(f"  Relaxed in {t_relax:.1f}s ({n_relax / t_relax:.1f} s/s)")
    sys.stdout.flush()

    # --- Save pristine (relaxed) state ---
    saved_re = solver.psi_re.copy()
    saved_im = solver.psi_im.copy()
    print("  Pristine state saved.")

    O_phi_0, O_rho_0 = solver.observe_B()
    print(f"  Pristine B: O_phi={O_phi_0:.6f}, O_rho={O_rho_0:.6f}")
    sys.stdout.flush()

    # --- Compute trial parameters ---
    total_steps = int(np.ceil(p.t_acoustic / p.dt))
    n_meas = 1 + total_steps // MEAS_EVERY
    if total_steps % MEAS_EVERY != 0:
        n_meas += 1   # final step gets measured too
    print(f"\n  Evolution: {total_steps} steps/trial "
          f"(t_max = {total_steps * p.dt:.4f})")
    print(f"  Measurements/trial: {n_meas}")
    print(f"  Expected CSV rows:  ~{N_TRIALS * n_meas}")
    sys.stdout.flush()

    # --- Trial loop ---
    print(f"\n  Starting {N_TRIALS} trials...")
    print(f"  {'=' * 60}")
    sys.stdout.flush()

    csv_path = os.path.join(os.path.dirname(__file__) or '.', CSV_FILENAME)
    n_good = 0
    n_nan = 0
    t_batch_start = time.time()

    with open(csv_path, 'w') as csvf:
        csvf.write("trial_id,bit_label,time,O_phi,O_rho\n")

        for trial_id in range(N_TRIALS):
            bit = trial_id % 2
            dphi = p.dphi_bit0 if bit == 0 else p.dphi_bit1
            t_trial_start = time.time()

            # 1. Restore pristine
            solver.psi_re[:] = saved_re
            solver.psi_im[:] = saved_im
            cp.cuda.Stream.null.synchronize()

            # 2. Inject IC noise
            solver.inject_noise(trial_id)

            # 3. Inject bit perturbation at A
            solver.inject(dphi)

            # 4. Measure at t=0
            O_phi, O_rho = solver.observe_B()
            lines = [f"{trial_id},{bit},0.000000,{O_phi:.8e},{O_rho:.8e}"]

            # 5. Evolve + measure
            nan_hit = False
            for step in range(1, total_steps + 1):
                solver.rk4_step()
                if step % MEAS_EVERY == 0 or step == total_steps:
                    O_phi, O_rho = solver.observe_B()
                    if np.isnan(O_phi) or np.isnan(O_rho):
                        nan_hit = True
                        break
                    t_cur = step * p.dt
                    lines.append(
                        f"{trial_id},{bit},{t_cur:.6f},"
                        f"{O_phi:.8e},{O_rho:.8e}")

            if nan_hit:
                n_nan += 1
                print(f"  [{trial_id + 1:03d}/{N_TRIALS}] Bit {bit}: "
                      f"*** NaN at step {step} — SKIPPED ***")
                sys.stdout.flush()
                continue

            # 6. Write trial data
            csvf.write('\n'.join(lines) + '\n')
            csvf.flush()
            n_good += 1

            # 7. Progress
            t_trial = time.time() - t_trial_start
            rate = total_steps / t_trial if t_trial > 0 else 0
            elapsed_total = time.time() - t_batch_start
            avg_per_trial = elapsed_total / (trial_id + 1)
            remaining = (N_TRIALS - trial_id - 1) * avg_per_trial
            print(f"  [{trial_id + 1:03d}/{N_TRIALS}] Bit {bit}: "
                  f"{t_trial:.1f}s [{rate:.0f} s/s] | "
                  f"Elapsed {elapsed_total / 60:.1f}m  "
                  f"ETA {remaining / 3600:.2f}h")
            sys.stdout.flush()

    # --- Summary ---
    t_batch = time.time() - t_batch_start
    print(f"\n  {'=' * 60}")
    print(f"  PILOT BATCH COMPLETE")
    print(f"  {'=' * 60}")
    print(f"  Good trials:  {n_good}/{N_TRIALS}")
    print(f"  NaN skipped:  {n_nan}")
    print(f"  Total time:   {t_batch:.1f}s = {t_batch / 3600:.2f}h")
    print(f"  Output:       {csv_path}")
    print(f"  CSV rows:     ~{n_good * n_meas}")
    print(f"\n  Next: python uhf_decoder_lock.py")
    sys.stdout.flush()

    del saved_re, saved_im, solver
    cp.get_default_memory_pool().free_all_blocks()


if __name__ == "__main__":
    run_pilot()
