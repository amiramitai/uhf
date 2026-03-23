"""
UHF Phase 2: Blind Evaluation — 800 Blinded Trials + Statistical Validation
=============================================================================
Locked decoder from Pilot Phase.  Decoder parameters are IMMUTABLE.

Protocol:
  1. Run 800 blind trials with randomized bit assignment
  2. Decode blindly using locked t* and θ from Pilot Phase
  3. Unblind and compute: Accuracy, Binomial p-value, ROC/AUC, MI
  4. Label-Shuffled Null: scramble labels, recompute metrics
  5. Phase-Randomized Null: 100 trials with uniform random phase
  6. Output statistical summary

Solver: RK4 + 4th-order FD Laplacian.  NO FFT.  Strictly local stencil.
Grid:   320^3, Δx = 0.25 ξ, d = 20 ξ, boundary = 30 ξ.
"""

import os, sys, time, math
import numpy as np

os.environ["LD_LIBRARY_PATH"] = "/usr/lib/wsl/lib"

try:
    import cupy as cp
except ImportError:
    print("FATAL: CuPy required.")
    sys.exit(1)


# =====================================================================
#  LOCKED DECODER PARAMETERS (Pilot Phase — DO NOT ALTER)
# =====================================================================
STEP_PHI  = 2650       # t*_phi = 12.421875 natural units
STEP_RHO  = 2380       # t*_rho = 11.156250 natural units
THETA_PHI = 0.205264   # (μ₀ + μ₁)/2 for phase at t*_phi
THETA_RHO = 0.909564   # (μ₀ + μ₁)/2 for density at t*_rho
# Phase:   μ₀(t*) = 0.1955 < μ₁(t*) = 0.2150
#          predict 0 if S < θ,  predict 1 if S ≥ θ
# Density: μ₀(t*) = 0.9165 > μ₁(t*) = 0.9026
#          predict 0 if S ≥ θ,  predict 1 if S < θ

N_BLIND = 800
N_NULL_PHASE = 100
N_LABEL_SHUFFLES = 10000
BIT_RNG_SEED = 20260312


# =====================================================================
#  Physical parameters (identical to Pilot Phase)
# =====================================================================
class EvalParams:
    def __init__(self):
        self.g = 1.0
        self.rho0 = 1.0
        self.mu = 1.0
        self.cs = 1.0
        self.xi = 1.0 / np.sqrt(2.0)

        d_xi = 20.0
        clearance_xi = 30.0
        dx_xi = 0.25
        L_half_xi = d_xi / 2.0 + clearance_xi   # 40 ξ

        self.N = int(round(2.0 * L_half_xi / dx_xi))   # 320
        self.N = (self.N // 32) * 32
        self.N_total = self.N ** 3
        self.L = L_half_xi * self.xi
        self.dx = 2.0 * self.L / self.N
        self.dt = 0.15 * self.dx ** 2

        self.x_A = -(d_xi / 2.0) * self.xi
        self.x_B = +(d_xi / 2.0) * self.xi
        self.D = d_xi * self.xi

        self.R_ring = 3.0 * self.xi
        self.sigma = 0.5 * self.xi
        self.void = 0.01
        self.dphi_bit0 = +np.pi / 2.0
        self.dphi_bit1 = -np.pi / 2.0

        self.inject_x = self.x_A
        self.inject_y = 0.0
        self.inject_z = 0.0

        self.sigma_theta = 0.01
        self.sigma_rho = 1e-3

        self.R_B = 1.5 * self.xi
        self.N_probe = 256

        self.t_acoustic = self.D / self.cs


# =====================================================================
#  CUDA kernel manager
# =====================================================================
_kernels = None


def get_kernels():
    global _kernels
    if _kernels is None:
        kp = os.path.join(os.path.dirname(__file__) or '.', 'uhf_gp_kernels.cu')
        with open(kp) as f:
            src = f.read()
        mod = cp.RawModule(code=src)
        _kernels = type('K', (), {
            'compute_density':       mod.get_function('compute_density'),
            'compute_phase':         mod.get_function('compute_phase'),
            'sample_sphere':         mod.get_function('sample_sphere'),
            'imprint_trefoil_kernel': mod.get_function('imprint_trefoil_kernel'),
            'gp_rhs_fd4':           mod.get_function('gp_rhs_fd4'),
            'inject_phase_and_void': mod.get_function('inject_phase_and_void'),
        })()
        _w = cp.zeros(1024, dtype=cp.float32)
        _kernels.compute_density((4,), (256,), (_w, _w, _w, np.int32(1024)))
        cp.cuda.Stream.null.synchronize()
    return _kernels


# =====================================================================
#  Vortex ring imprinting (Abrikosov product ansatz)
# =====================================================================
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

    inv_sqrt_rho0 = np.float32(1.0 / np.sqrt(p.rho0))
    new_re = (save_re * solver.psi_re - save_im * solver.psi_im) * inv_sqrt_rho0
    new_im = (save_re * solver.psi_im + save_im * solver.psi_re) * inv_sqrt_rho0
    solver.psi_re[:] = new_re
    solver.psi_im[:] = new_im
    del save_re, save_im, new_re, new_im
    cp.get_default_memory_pool().free_all_blocks()

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


# =====================================================================
#  Shell probes (golden spiral on sphere)
# =====================================================================
def make_shell_probes(x_center, y_center, z_center, R, N_pts=256):
    idx = np.arange(N_pts, dtype=np.float32)
    golden = (1 + np.sqrt(5)) / 2
    theta = np.arccos(1 - 2 * (idx + 0.5) / N_pts)
    phi = 2 * np.pi * idx / golden
    px = (x_center + R * np.sin(theta) * np.cos(phi)).astype(np.float32)
    py = (y_center + R * np.sin(theta) * np.sin(phi)).astype(np.float32)
    pz = (z_center + R * np.cos(theta)).astype(np.float32)
    return px, py, pz


# =====================================================================
#  RK4 + FD4 Solver
# =====================================================================
class EvalSolver:
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

    def inject_noise(self, seed):
        """Add stochastic IC noise (full-grid phase + density)."""
        cp.random.seed(seed)
        N3 = self.N3
        p = self.p
        g = (N3 + 255) // 256

        self.K.compute_density((g,), (256,),
                               (self.psi_re, self.psi_im, self.rho,
                                np.int32(N3)))
        self.K.compute_phase((g,), (256,),
                             (self.psi_re, self.psi_im, self.phase,
                              np.int32(N3)))
        cp.cuda.Stream.null.synchronize()

        self.rho += cp.random.normal(0, p.sigma_rho, N3, dtype=cp.float32)
        cp.maximum(self.rho, cp.float32(1e-10), out=self.rho)

        self.phase += cp.random.normal(0, p.sigma_theta, N3, dtype=cp.float32)

        cp.sqrt(self.rho, out=self.rho)      # amplitude
        cp.cos(self.phase, out=self.psi_re)
        cp.sin(self.phase, out=self.psi_im)
        self.psi_re *= self.rho
        self.psi_im *= self.rho

        cp.get_default_memory_pool().free_all_blocks()


# =====================================================================
#  Pure-numpy statistics (no scipy/sklearn dependency)
# =====================================================================
def binomial_log10_pvalue(k, n, p0=0.5):
    """log₁₀ P(X ≥ k) for X ~ Binomial(n, p0), normal approximation."""
    mu = n * p0
    sigma = math.sqrt(n * p0 * (1 - p0))
    z = (k - 0.5 - mu) / sigma
    x = z / math.sqrt(2)
    if x > 26:
        # Asymptotic: erfc(x) ~ exp(-x²)/(x√π)
        log10e = math.log10(math.e)
        return (math.log10(0.5) - x * x * log10e
                - math.log10(x * math.sqrt(math.pi)))
    val = 0.5 * math.erfc(x)
    if val <= 0:
        return -300.0
    return math.log10(val)


def compute_auc(labels, scores):
    """AUC via Mann-Whitney U.  Higher score → more likely label=1."""
    labels = np.asarray(labels)
    scores = np.asarray(scores)
    pos = scores[labels == 1]
    neg = scores[labels == 0]
    n1, n0 = len(pos), len(neg)
    if n0 == 0 or n1 == 0:
        return 0.5
    neg_sorted = np.sort(neg)
    concordant = 0.0
    tied = 0.0
    for s in pos:
        left = int(np.searchsorted(neg_sorted, s, side='left'))
        right = int(np.searchsorted(neg_sorted, s, side='right'))
        concordant += left
        tied += right - left
    return (concordant + 0.5 * tied) / (n0 * n1)


def mutual_info(true_bits, pred_bits):
    """MI(true; pred) in bits from confusion matrix."""
    n = len(true_bits)
    true_bits = np.asarray(true_bits)
    pred_bits = np.asarray(pred_bits)
    tn = int(np.sum((true_bits == 0) & (pred_bits == 0)))
    fp = int(np.sum((true_bits == 0) & (pred_bits == 1)))
    fn = int(np.sum((true_bits == 1) & (pred_bits == 0)))
    tp = int(np.sum((true_bits == 1) & (pred_bits == 1)))
    joint = np.array([[tn, fp], [fn, tp]], dtype=float) / n
    mt = np.array([tn + fp, fn + tp], dtype=float) / n
    mp = np.array([tn + fn, fp + tp], dtype=float) / n
    mi = 0.0
    for i in range(2):
        for j in range(2):
            if joint[i, j] > 0 and mt[i] > 0 and mp[j] > 0:
                mi += joint[i, j] * math.log2(
                    joint[i, j] / (mt[i] * mp[j]))
    return mi


# =====================================================================
#  Main evaluation
# =====================================================================
def run_evaluation():
    dev = cp.cuda.Device(0)
    free, total = dev.mem_info
    print(f"GPU 0: {free / 1e9:.2f} GB free / {total / 1e9:.2f} GB total")

    p = EvalParams()
    dt = p.dt

    print(f"\n{'#' * 65}")
    print(f"  PHASE 2: BLIND EVALUATION — {N_BLIND} TRIALS + NULL CONTROLS")
    print(f"{'#' * 65}")
    print(f"  Solver:         RK4 + FD4 (strictly local, NO FFT)")
    print(f"  Grid:           {p.N}^3 = {p.N_total:,}")
    print(f"  dx:             {p.dx:.5f}  ({p.dx / p.xi:.4f} xi)")
    print(f"  dt:             {dt:.6f}")
    print(f"  Defect A:       x = {p.x_A:.4f}")
    print(f"  Defect B:       x = {p.x_B:.4f}")
    print(f"  d = {p.D:.4f}   t_acoustic = {p.t_acoustic:.4f}")
    print(f"  Noise:          σ_θ = {p.sigma_theta},  σ_ρ = {p.sigma_rho}")
    print(f"  LOCKED DECODER:")
    print(f"    STEP_RHO = {STEP_RHO}  (t = {STEP_RHO * dt:.6f})")
    print(f"    STEP_PHI = {STEP_PHI}  (t = {STEP_PHI * dt:.6f})")
    print(f"    THETA_PHI = {THETA_PHI}")
    print(f"    THETA_RHO = {THETA_RHO}")
    print(f"  Blind trials:   {N_BLIND}")
    print(f"  Null trials:    {N_NULL_PHASE} (phase-randomized)")
    print(f"  Label shuffles: {N_LABEL_SHUFFLES}")
    sys.stdout.flush()

    # --- Create solver ---
    solver = EvalSolver(p)

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
    print(f"  Relaxed in {t_relax:.1f}s")

    saved_re = solver.psi_re.copy()
    saved_im = solver.psi_im.copy()
    print("  Pristine state saved.")
    sys.stdout.flush()

    # --- Randomize bit assignments ---
    rng = np.random.RandomState(BIT_RNG_SEED)
    true_bits = rng.randint(0, 2, N_BLIND)
    n_bit0 = int(np.sum(true_bits == 0))
    n_bit1 = int(np.sum(true_bits == 1))
    print(f"  Bit assignment (RNG {BIT_RNG_SEED}): "
          f"{n_bit0} x Bit 0, {n_bit1} x Bit 1")
    sys.stdout.flush()

    # =================================================================
    #  PHASE A: 800 BLIND TRIALS
    # =================================================================
    print(f"\n  {'=' * 55}")
    print(f"  PHASE A: {N_BLIND} BLIND TRIALS")
    print(f"  {'=' * 55}")
    sys.stdout.flush()

    S_phi = np.zeros(N_BLIND)
    S_rho = np.zeros(N_BLIND)
    valid = np.ones(N_BLIND, dtype=bool)

    csv_path = os.path.join(os.path.dirname(__file__) or '.', 'blind_scores.csv')
    t_batch_start = time.time()

    with open(csv_path, 'w') as csvf:
        csvf.write("trial_id,true_bit,S_phi,S_rho\n")

        for trial_id in range(N_BLIND):
            bit = int(true_bits[trial_id])
            dphi = p.dphi_bit0 if bit == 0 else p.dphi_bit1
            t0 = time.time()

            # 1. Restore pristine
            solver.psi_re[:] = saved_re
            solver.psi_im[:] = saved_im
            cp.cuda.Stream.null.synchronize()

            # 2. Inject IC noise (unique seed, disjoint from Pilot)
            solver.inject_noise(trial_id * 7919 + 500042)

            # 3. Inject bit perturbation at A
            solver.inject(dphi)

            # 4. Evolve to STEP_RHO → measure density
            for _ in range(STEP_RHO):
                solver.rk4_step()
            _, o_rho = solver.observe_B()

            # 5. Continue to STEP_PHI → measure phase
            for _ in range(STEP_PHI - STEP_RHO):
                solver.rk4_step()
            o_phi, _ = solver.observe_B()

            if np.isnan(o_phi) or np.isnan(o_rho):
                valid[trial_id] = False
                print(f"  [{trial_id + 1:04d}/{N_BLIND}] "
                      f"*** NaN — SKIPPED ***")
                sys.stdout.flush()
                continue

            S_phi[trial_id] = o_phi
            S_rho[trial_id] = o_rho
            csvf.write(f"{trial_id},{bit},{o_phi:.8e},{o_rho:.8e}\n")

            t_trial = time.time() - t0
            elapsed = time.time() - t_batch_start
            avg = elapsed / (trial_id + 1)
            remaining = (N_BLIND + N_NULL_PHASE - trial_id - 1) * avg

            if trial_id < 3 or (trial_id + 1) % 20 == 0:
                print(f"  [{trial_id + 1:04d}/{N_BLIND}] Bit {bit}: "
                      f"{t_trial:.1f}s | "
                      f"Elapsed {elapsed / 60:.1f}m  "
                      f"ETA {remaining / 3600:.2f}h")
                csvf.flush()
                sys.stdout.flush()

    n_valid = int(np.sum(valid))
    t_blind = time.time() - t_batch_start
    print(f"\n  Blind trials complete: {n_valid}/{N_BLIND} valid "
          f"({t_blind / 3600:.2f}h)")
    sys.stdout.flush()

    # =================================================================
    #  PHASE B: BLIND DECODE (locked decoder, labels still hidden)
    # =================================================================
    print(f"\n  {'=' * 55}")
    print(f"  PHASE B: BLIND DECODE (locked thresholds)")
    print(f"  {'=' * 55}")

    # Phase channel: predict 1 if S_phi ≥ θ, else 0
    pred_phi = (S_phi >= THETA_PHI).astype(int)
    # Density channel: predict 1 if S_rho < θ, else 0
    pred_rho = (S_rho < THETA_RHO).astype(int)

    # Report prediction distribution (still blind — no labels)
    p1_phi = int(np.sum(pred_phi[valid] == 1))
    p0_phi = int(np.sum(pred_phi[valid] == 0))
    p1_rho = int(np.sum(pred_rho[valid] == 1))
    p0_rho = int(np.sum(pred_rho[valid] == 0))
    print(f"  Phase predictions:   {p0_phi} x '0',  {p1_phi} x '1'")
    print(f"  Density predictions: {p0_rho} x '0',  {p1_rho} x '1'")
    sys.stdout.flush()

    # =================================================================
    #  PHASE C: UNBLIND + PRIMARY METRICS
    # =================================================================
    print(f"\n  {'=' * 55}")
    print(f"  PHASE C: UNBLIND — PRIMARY METRICS")
    print(f"  {'=' * 55}")

    v = valid
    tb = true_bits[v]
    pp = pred_phi[v]
    pr_arr = pred_rho[v]
    sp = S_phi[v]
    sr = S_rho[v]
    n = len(tb)

    # Accuracy
    acc_phi = float(np.mean(pp == tb))
    acc_rho = float(np.mean(pr_arr == tb))
    k_phi = int(np.sum(pp == tb))
    k_rho = int(np.sum(pr_arr == tb))

    # Binomial p-value
    log10p_phi = binomial_log10_pvalue(k_phi, n)
    log10p_rho = binomial_log10_pvalue(k_rho, n)

    # ROC AUC
    # Phase: higher S_phi → more likely bit 1
    auc_phi = compute_auc(tb, sp)
    # Density: lower S_rho → more likely bit 1, so negate
    auc_rho = compute_auc(tb, -sr)

    # Mutual Information
    mi_phi = mutual_info(tb, pp)
    mi_rho = mutual_info(tb, pr_arr)

    print(f"\n  --- PHASE CHANNEL ---")
    print(f"  Accuracy:     {acc_phi:.4f}  ({k_phi}/{n})")
    print(f"  p-value:      10^({log10p_phi:.1f})")
    print(f"  ROC AUC:      {auc_phi:.6f}")
    print(f"  MI (bits):    {mi_phi:.6f}")

    print(f"\n  --- DENSITY CHANNEL ---")
    print(f"  Accuracy:     {acc_rho:.4f}  ({k_rho}/{n})")
    print(f"  p-value:      10^({log10p_rho:.1f})")
    print(f"  ROC AUC:      {auc_rho:.6f}")
    print(f"  MI (bits):    {mi_rho:.6f}")
    sys.stdout.flush()

    # =================================================================
    #  PHASE D: LABEL-SHUFFLED NULL
    # =================================================================
    print(f"\n  {'=' * 55}")
    print(f"  PHASE D: LABEL-SHUFFLED NULL ({N_LABEL_SHUFFLES:,} shuffles)")
    print(f"  {'=' * 55}")

    rng_shuf = np.random.RandomState(42)
    shuf_accs_phi = np.zeros(N_LABEL_SHUFFLES)
    shuf_accs_rho = np.zeros(N_LABEL_SHUFFLES)
    shuf_aucs_phi = np.zeros(N_LABEL_SHUFFLES)
    shuf_mis_phi = np.zeros(N_LABEL_SHUFFLES)

    for i in range(N_LABEL_SHUFFLES):
        shuf = tb.copy()
        rng_shuf.shuffle(shuf)
        shuf_accs_phi[i] = float(np.mean(pp == shuf))
        shuf_accs_rho[i] = float(np.mean(pr_arr == shuf))
        shuf_aucs_phi[i] = compute_auc(shuf, sp)
        shuf_mis_phi[i] = mutual_info(shuf, pp)

    print(f"  Shuffled accuracy (phase):")
    print(f"    mean = {np.mean(shuf_accs_phi):.4f} "
          f"± {np.std(shuf_accs_phi):.4f}  "
          f"max = {np.max(shuf_accs_phi):.4f}")
    print(f"  Shuffled accuracy (density):")
    print(f"    mean = {np.mean(shuf_accs_rho):.4f} "
          f"± {np.std(shuf_accs_rho):.4f}  "
          f"max = {np.max(shuf_accs_rho):.4f}")
    print(f"  Shuffled AUC (phase):")
    print(f"    mean = {np.mean(shuf_aucs_phi):.4f} "
          f"± {np.std(shuf_aucs_phi):.4f}")
    print(f"  Shuffled MI (phase):")
    print(f"    mean = {np.mean(shuf_mis_phi):.6f} "
          f"± {np.std(shuf_mis_phi):.6f}")
    sys.stdout.flush()

    # =================================================================
    #  PHASE E: PHASE-RANDOMIZED NULL (100 GPU trials)
    # =================================================================
    print(f"\n  {'=' * 55}")
    print(f"  PHASE E: PHASE-RANDOMIZED NULL ({N_NULL_PHASE} GPU trials)")
    print(f"  {'=' * 55}")
    sys.stdout.flush()

    null_rng = np.random.RandomState(999)
    null_dphis = null_rng.uniform(-np.pi, np.pi, N_NULL_PHASE)
    null_S_phi = np.zeros(N_NULL_PHASE)
    null_S_rho = np.zeros(N_NULL_PHASE)
    null_valid = np.ones(N_NULL_PHASE, dtype=bool)

    t_null_start = time.time()
    for trial_id in range(N_NULL_PHASE):
        dphi = float(null_dphis[trial_id])
        t0 = time.time()

        solver.psi_re[:] = saved_re
        solver.psi_im[:] = saved_im
        cp.cuda.Stream.null.synchronize()
        solver.inject_noise(trial_id * 7919 + 700042)
        solver.inject(dphi)

        for _ in range(STEP_RHO):
            solver.rk4_step()
        _, o_rho = solver.observe_B()

        for _ in range(STEP_PHI - STEP_RHO):
            solver.rk4_step()
        o_phi, _ = solver.observe_B()

        if np.isnan(o_phi) or np.isnan(o_rho):
            null_valid[trial_id] = False
            print(f"  [NULL {trial_id + 1:03d}/{N_NULL_PHASE}] *** NaN ***")
            sys.stdout.flush()
            continue

        null_S_phi[trial_id] = o_phi
        null_S_rho[trial_id] = o_rho

        t_trial = time.time() - t0
        elapsed = time.time() - t_null_start
        if trial_id < 3 or (trial_id + 1) % 20 == 0:
            print(f"  [NULL {trial_id + 1:03d}/{N_NULL_PHASE}] "
                  f"dphi={dphi:+.3f}  {t_trial:.1f}s | "
                  f"Elapsed {elapsed / 60:.1f}m")
            sys.stdout.flush()

    t_null_elapsed = time.time() - t_null_start

    nv = null_valid
    n_null_valid = int(np.sum(nv))
    # "True bit" for null: 0 if dphi >= 0, 1 if dphi < 0
    null_true = (null_dphis[nv] < 0).astype(int)
    null_pred_phi = (null_S_phi[nv] >= THETA_PHI).astype(int)
    null_pred_rho = (null_S_rho[nv] < THETA_RHO).astype(int)

    null_acc_phi = (float(np.mean(null_pred_phi == null_true))
                    if n_null_valid > 0 else 0.0)
    null_acc_rho = (float(np.mean(null_pred_rho == null_true))
                    if n_null_valid > 0 else 0.0)

    print(f"\n  Phase-randomized null ({n_null_valid} valid, "
          f"{t_null_elapsed / 60:.1f}m):")
    print(f"    Phase accuracy:   {null_acc_phi:.4f}  (expect ~0.50)")
    print(f"    Density accuracy: {null_acc_rho:.4f}  (expect ~0.50)")
    sys.stdout.flush()

    # --- Free GPU ---
    del saved_re, saved_im, solver
    cp.get_default_memory_pool().free_all_blocks()

    # =================================================================
    #  FINAL STATISTICAL SUMMARY
    # =================================================================
    t_total = time.time() - t_batch_start

    print(f"\n\n{'#' * 65}")
    print(f"  PHASE 2 — FINAL STATISTICAL SUMMARY")
    print(f"{'#' * 65}")
    print(f"\n  Valid trials:     {n} / {N_BLIND}")
    print(f"  Bit distribution: {int(np.sum(tb == 0))} x Bit 0, "
          f"{int(np.sum(tb == 1))} x Bit 1")
    print(f"  Total runtime:    {t_total / 3600:.2f}h")

    print(f"\n  ┌─────────────────┬────────────────┬────────────────┬──────────┐")
    print(f"  │ Metric          │ Phase Channel  │ Density Chan.  │ Target   │")
    print(f"  ├─────────────────┼────────────────┼────────────────┼──────────┤")
    print(f"  │ Accuracy        │ {acc_phi:>13.4f} │ {acc_rho:>13.4f} │ > 0.65   │")
    print(f"  │ p-value (log₁₀) │ {log10p_phi:>13.1f} │ {log10p_rho:>13.1f} │ < -8     │")
    print(f"  │ ROC AUC         │ {auc_phi:>13.6f} │ {auc_rho:>13.6f} │ > 0.70   │")
    print(f"  │ MI (bits)       │ {mi_phi:>13.6f} │ {mi_rho:>13.6f} │ > 0.03   │")
    print(f"  └─────────────────┴────────────────┴────────────────┴──────────┘")

    print(f"\n  ┌──────────────────┬────────────────┬────────────────────────┐")
    print(f"  │ Null Control     │ Accuracy       │ Comment                │")
    print(f"  ├──────────────────┼────────────────┼────────────────────────┤")
    print(f"  │ Label-shuffled   │ {np.mean(shuf_accs_phi):>13.4f} │"
          f" ± {np.std(shuf_accs_phi):.4f}  (10k shuffles) │")
    print(f"  │ Phase-randomized │ {null_acc_phi:>13.4f} │"
          f" expect ~0.50           │")
    print(f"  └──────────────────┴────────────────┴────────────────────────┘")

    # --- PASS / FAIL ---
    pass_acc = acc_phi > 0.65
    pass_pval = log10p_phi < -8
    pass_auc = auc_phi > 0.70
    pass_mi = mi_phi > 0.03
    pass_shuf = np.mean(shuf_accs_phi) < 0.55
    pass_null = abs(null_acc_phi - 0.50) < 0.15

    all_pass = (pass_acc and pass_pval and pass_auc and pass_mi
                and pass_shuf and pass_null)

    print(f"\n  ┌─ PASS / FAIL ──────────────────────────────────────────┐")
    print(f"  │ Accuracy > 0.65:       "
          f"{'PASS' if pass_acc else 'FAIL':>4}   ({acc_phi:.4f})"
          f"{'':>15}│")
    print(f"  │ p-value < 10⁻⁸:       "
          f"{'PASS' if pass_pval else 'FAIL':>4}   (10^{log10p_phi:.1f})"
          f"{'':>11}│")
    print(f"  │ AUC > 0.70:            "
          f"{'PASS' if pass_auc else 'FAIL':>4}   ({auc_phi:.6f})"
          f"{'':>9}│")
    print(f"  │ MI > 0.03 bits:        "
          f"{'PASS' if pass_mi else 'FAIL':>4}   ({mi_phi:.6f})"
          f"{'':>9}│")
    print(f"  │ Shuffled null ~ 0.50:  "
          f"{'PASS' if pass_shuf else 'FAIL':>4}   "
          f"({np.mean(shuf_accs_phi):.4f})"
          f"{'':>13}│")
    print(f"  │ Phase-rand null ~ 0.50:"
          f"{'PASS' if pass_null else 'FAIL':>4}   ({null_acc_phi:.4f})"
          f"{'':>13}│")
    print(f"  ├─────────────────────────────────────────────────────────┤")
    if all_pass:
        print(f"  │  >> ALL CRITERIA MET — PUBLICATION READY              │")
    else:
        fails = []
        if not pass_acc: fails.append("Accuracy")
        if not pass_pval: fails.append("p-value")
        if not pass_auc: fails.append("AUC")
        if not pass_mi: fails.append("MI")
        if not pass_shuf: fails.append("Shuf-null")
        if not pass_null: fails.append("Phase-null")
        print(f"  │  >> FAILED: {', '.join(fails):<44}│")
    print(f"  └─────────────────────────────────────────────────────────┘")

    print(f"\n  Output CSV: {csv_path}")
    sys.stdout.flush()


if __name__ == "__main__":
    run_evaluation()
