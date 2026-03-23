"""
UHF Hydrodynamic EPR Entanglement Test
============================================================
THESIS: The UV Bogoliubov channel (v_g >> c_s) can physically entangle
        two spatially separated topological defects.

SETUP:
  Particle A: vortex ring at x = -10  (natural units)
  Particle B: vortex ring at x = +10
  Distance D = 20.0,  t_acoustic = D / c_s = 20.0

PERTURBATION:
  At t = 0, inject sharp UV Gaussian (sigma = 0.5 xi) at Particle A's
  vortex core.  Phase kick = pi, void = 0.1 (massive density cavity).

MEASUREMENT:
  Track density & phase at Particle B's vortex core.
  If B responds at t ~ D/v_UV ~ 1.8  -> HYDRODYNAMIC EPR CONFIRMED
  If B responds at t ~ D/c_s  = 20   -> Classical acoustic, no entanglement

SOLVER: RK4 + 4th-order FD Laplacian.  NO FFT.  Strictly local stencil.
METHOD: A/B differential + baseline subtraction.
"""

import os, sys, time
import numpy as np

os.environ["LD_LIBRARY_PATH"] = "/usr/lib/wsl/lib"

try:
    import cupy as cp
except ImportError:
    print("FATAL: CuPy required.")
    sys.exit(1)


# =================================================================
# Physical parameters
# =================================================================
class EPRParams:
    def __init__(self, N, L_half):
        self.g = 1.0
        self.rho0 = 1.0
        self.mu = 1.0
        self.cs = 1.0
        self.xi = 1.0 / np.sqrt(2.0)

        self.N = N
        self.N_total = N ** 3
        self.L = L_half                     # box from -L to +L
        self.dx = 2.0 * L_half / N
        self.dt = 0.15 * self.dx ** 2

        # Particle positions (natural units)
        self.x_A = -10.0
        self.x_B = +10.0
        self.D = self.x_B - self.x_A        # = 20.0

        # Ring parameters
        self.R_ring = 3.0 * self.xi          # ring radius in natural units

        # Perturbation (UV, sharp) — inject at RING CENTER where rho=1
        self.sigma = 0.5 * self.xi           # Gaussian width
        self.dphi = np.pi                    # phase kick
        self.void = 0.01                     # density void (99% depletion)
        # Injection point: ring A center (bulk condensate, rho ≈ 1)
        self.inject_x = self.x_A
        self.inject_y = 0.0
        self.inject_z = 0.0

        # Timescales
        self.t_acoustic = self.D / self.cs
        k_dom = 1.0 / self.sigma
        eps_dom = np.sqrt(k_dom**2 + k_dom**4 / 4.0)
        self.v_uv = (k_dom + k_dom**3) / eps_dom
        self.t_uv = self.D / self.v_uv


# =================================================================
# CUDA kernel manager (reuse uhf_gp_kernels.cu)
# =================================================================
_kernels = None

def get_kernels():
    global _kernels
    if _kernels is None:
        kp = os.path.join(os.path.dirname(__file__) or '.', 'uhf_gp_kernels.cu')
        with open(kp) as f:
            src = f.read()
        mod = cp.RawModule(code=src)
        _kernels = type('K', (), {
            'compute_density': mod.get_function('compute_density'),
            'compute_phase': mod.get_function('compute_phase'),
            'sample_sphere': mod.get_function('sample_sphere'),
            'imprint_trefoil_kernel': mod.get_function('imprint_trefoil_kernel'),
            'gp_rhs_fd4': mod.get_function('gp_rhs_fd4'),
            'inject_phase_and_void': mod.get_function('inject_phase_and_void'),
        })()
        # warm up
        _w = cp.zeros(1024, dtype=cp.float32)
        _kernels.compute_density((4,), (256,), (_w, _w, _w, np.int32(1024)))
        cp.cuda.Stream.null.synchronize()
    return _kernels


# =================================================================
# Vortex ring imprinting (reuse trefoil kernel with circular curve)
# =================================================================
def make_ring_curve(cx, cy, cz, R, axis='x', N_pts=256):
    """Generate curve points for a vortex ring centered at (cx,cy,cz)
    with radius R in the plane perpendicular to the given axis."""
    t = np.linspace(0, 2 * np.pi, N_pts, endpoint=False).astype(np.float32)
    if axis == 'x':
        # ring in y-z plane
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
    """Imprint a single vortex ring using the trefoil kernel."""
    N_curve = 256
    xs, ys, zs = make_ring_curve(cx, cy, cz, R, axis, N_curve)
    d_cx, d_cy, d_cz = cp.asarray(xs), cp.asarray(ys), cp.asarray(zs)
    N3 = p.N_total
    block, grid = 256, (N3 + 255) // 256

    for attempt in range(3):
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
    print(f"    Ring at ({cx:.1f},{cy:.1f},{cz:.1f}) R={R:.3f}: "
          f"rho=[{rmin:.4f}, {rmax:.4f}]")
    return True


def imprint_two_rings(solver):
    """Imprint two vortex rings using Abrikosov product ansatz.
    psi_total = psi_A * psi_B / sqrt(rho0)"""
    p = solver.p
    K = solver.K
    N3 = p.N_total

    print("  Imprinting Ring A...")
    ok = imprint_single_ring(solver.psi_re, solver.psi_im, p, K,
                             p.x_A, 0.0, 0.0, p.R_ring, 'x')
    if not ok:
        return False

    # Save ring A
    save_re = solver.psi_re.copy()
    save_im = solver.psi_im.copy()

    print("  Imprinting Ring B...")
    ok = imprint_single_ring(solver.psi_re, solver.psi_im, p, K,
                             p.x_B, 0.0, 0.0, p.R_ring, 'x')
    if not ok:
        return False

    # Abrikosov product: psi = psi_A * psi_B / sqrt(rho0)
    # Complex multiply: (a+bi)(c+di) = (ac-bd) + (ad+bc)i
    inv_sqrt_rho0 = np.float32(1.0 / np.sqrt(p.rho0))
    new_re = (save_re * solver.psi_re - save_im * solver.psi_im) * inv_sqrt_rho0
    new_im = (save_re * solver.psi_im + save_im * solver.psi_re) * inv_sqrt_rho0
    solver.psi_re[:] = new_re
    solver.psi_im[:] = new_im
    del save_re, save_im, new_re, new_im
    cp.get_default_memory_pool().free_all_blocks()

    # Normalize
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


# =================================================================
# Probe generation — detection shells along x-axis
# =================================================================
def make_shell_probes(x_center, R_probe, N_pts=256):
    """Create probe points: sphere of radius R_probe at (x_center, 0, 0)."""
    idx = np.arange(N_pts, dtype=np.float32)
    golden = (1 + np.sqrt(5)) / 2
    theta = np.arccos(1 - 2 * (idx + 0.5) / N_pts)
    phi = 2 * np.pi * idx / golden
    px = (x_center + R_probe * np.sin(theta) * np.cos(phi)).astype(np.float32)
    py = (R_probe * np.sin(theta) * np.sin(phi)).astype(np.float32)
    pz = (R_probe * np.cos(theta)).astype(np.float32)
    return px, py, pz


def make_ring_probes(cx, R_ring, N_pts=64):
    """Points on vortex ring filament at x=cx in y-z plane."""
    t = np.linspace(0, 2 * np.pi, N_pts, endpoint=False).astype(np.float32)
    px = np.full(N_pts, cx, dtype=np.float32)
    py = (R_ring * np.cos(t)).astype(np.float32)
    pz = (R_ring * np.sin(t)).astype(np.float32)
    return px, py, pz


# =================================================================
# RK4 + FD4 Solver
# =================================================================
class EPRSolver:
    def __init__(self, params):
        self.p = params
        self.K = get_kernels()
        self.N3 = params.N_total
        z = lambda: cp.zeros(self.N3, dtype=cp.float32)
        self.psi_re, self.psi_im = z(), z()
        self.rho, self.phase = z(), z()
        self.rhs_re, self.rhs_im = z(), z()
        self.tmp_re, self.tmp_im = z(), z()
        self.acc_re, self.acc_im = z(), z()
        self.inv_12dx2 = np.float32(1.0 / (12.0 * params.dx ** 2))

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
        self.compute_rhs(self.psi_re, self.psi_im, self.rhs_re, self.rhs_im)
        cp.multiply(self.rhs_re, dt6, out=self.acc_re)
        cp.multiply(self.rhs_im, dt6, out=self.acc_im)
        cp.add(self.psi_re, self.rhs_re * dt2, out=self.tmp_re)
        cp.add(self.psi_im, self.rhs_im * dt2, out=self.tmp_im)
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += self.rhs_re * dt3
        self.acc_im += self.rhs_im * dt3
        cp.add(self.psi_re, self.rhs_re * dt2, out=self.tmp_re)
        cp.add(self.psi_im, self.rhs_im * dt2, out=self.tmp_im)
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += self.rhs_re * dt3
        self.acc_im += self.rhs_im * dt3
        cp.add(self.psi_re, self.rhs_re * dt, out=self.tmp_re)
        cp.add(self.psi_im, self.rhs_im * dt, out=self.tmp_im)
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += self.rhs_re * dt6
        self.acc_im += self.rhs_im * dt6
        self.psi_re += self.acc_re
        self.psi_im += self.acc_im

    def setup_probes(self, probe_dict):
        """probe_dict: name -> (px, py, pz)"""
        self.probes = {}
        for label, (px, py, pz) in probe_dict.items():
            N_pts = len(px)
            self.probes[label] = {
                'px': cp.asarray(px), 'py': cp.asarray(py), 'pz': cp.asarray(pz),
                'rho_out': cp.zeros(N_pts, dtype=cp.float32),
                'phase_out': cp.zeros(N_pts, dtype=cp.float32),
                'N': N_pts,
            }

    def sample_probes(self):
        """Sample density & phase at all probe points. Returns dict of numpy arrays."""
        Nt = np.int32(self.N3)
        g = (self.N3 + 255) // 256
        self.K.compute_density((g,), (256,),
                               (self.psi_re, self.psi_im, self.rho, Nt))
        self.K.compute_phase((g,), (256,),
                             (self.psi_re, self.psi_im, self.phase, Nt))
        cp.cuda.Stream.null.synchronize()

        p = self.p
        results = {}
        for label, pr in self.probes.items():
            g2 = (pr['N'] + 255) // 256
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
            results[label] = {
                'rho': pr['rho_out'].get().copy(),
                'phase': pr['phase_out'].get().copy(),
            }
        return results

    def inject_at_A(self):
        """Inject sharp UV perturbation at Particle A's ring CENTER (bulk condensate)."""
        p = self.p
        g = (self.N3 + 255) // 256
        self.K.inject_phase_and_void(
            (g,), (256,),
            (self.psi_re, self.psi_im,
             np.float32(p.inject_x), np.float32(p.inject_y), np.float32(p.inject_z),
             np.float32(p.sigma),
             np.float32(p.dphi),
             np.float32(p.void),
             np.float32(-p.L), np.float32(-p.L), np.float32(-p.L),
             np.float32(p.dx),
             np.int32(p.N), np.int32(p.N), np.int32(p.N)))
        cp.cuda.Stream.null.synchronize()


# =================================================================
# Main experiment
# =================================================================
def run_epr():
    dev = cp.cuda.Device(0)
    free, total = dev.mem_info
    # Push VRAM -- use 70% for arrays (12 float32 arrays + temporaries)
    max_N3 = int(free * 0.65 / (12 * 4))
    max_N = int(max_N3 ** (1.0 / 3.0))
    max_N = (max_N // 32) * 32
    max_N = min(max_N, 704)
    print(f"GPU 0: {free / 1e9:.2f} GB free -> N={max_N}")

    xi = 1.0 / np.sqrt(2.0)

    # Box: particles at x=+-10, need wrap margin for UV signal
    # L_half = 25 gives L_full = 50, wrap = 30 vs direct = 20 → safe
    L_half = 25.0
    p = EPRParams(max_N, L_half)

    print(f"\n{'#' * 65}")
    print(f"  HYDRODYNAMIC EPR ENTANGLEMENT TEST")
    print(f"{'#' * 65}")
    print(f"  Grid:           {p.N}^3 = {p.N_total:,}")
    print(f"  Box:            [-{L_half}, +{L_half}],  dx = {p.dx:.5f}")
    print(f"  dx/xi:          {p.dx / p.xi:.4f}  ({p.xi / p.dx:.1f} cells/xi)")
    print(f"  dt:             {p.dt:.6f}")
    print(f"  Particle A:     x = {p.x_A:.1f}  (= {p.x_A / p.xi:.1f} xi)")
    print(f"  Particle B:     x = {p.x_B:.1f}  (= {p.x_B / p.xi:.1f} xi)")
    print(f"  Distance D:     {p.D:.1f}  (= {p.D / p.xi:.1f} xi)")
    print(f"  Ring radius:    {p.R_ring:.4f}  (= {p.R_ring / p.xi:.1f} xi)")
    print(f"  Perturbation:   Gaussian sigma = {p.sigma:.4f}  (= 0.5 xi)")
    print(f"  sigma/dx:       {p.sigma / p.dx:.1f} cells")
    print(f"  Phase kick:     {p.dphi:.4f},  void = {p.void}")
    print(f"  t_acoustic:     {p.t_acoustic:.4f}  (D / c_s)")
    print(f"  t_UV (k_dom):   {p.t_uv:.4f}  (D / v_g)")
    print(f"  v_UV / c_s:     {p.v_uv:.2f}")
    print(f"  Injection at:   ({p.inject_x:.1f}, {p.inject_y:.1f}, {p.inject_z:.1f}) "
          f"[ring A center, rho~1]")
    print(f"  CFL speed:      {2 * p.dx / p.dt:.1f}")
    sys.stdout.flush()

    # --- Create solver ---
    solver = EPRSolver(p)

    # --- Imprint two vortex rings ---
    print(f"\n  Imprinting two vortex rings (Abrikosov product)...")
    ok = imprint_two_rings(solver)
    if not ok:
        print("  FATAL: Failed to imprint vortex rings.")
        return

    # --- Setup detection shells along x-axis ---
    # Shell probes: spheres of radius R_probe at various distances from A
    R_probe = 1.0 * xi   # 1ξ sphere for clean, localized sampling
    # Detection points at x = -5, 0, +5, +10 (distances 5, 10, 15, 20 from A)
    shell_x = [-5.0, 0.0, 5.0, 10.0]
    shell_d = [x - p.x_A for x in shell_x]  # distance from A: 5, 10, 15, 20
    shell_labels = [f"d{d:.0f}" for d in shell_d]

    probe_dict = {}
    # Perturbation probe: small sphere at injection site
    probe_dict['A'] = make_shell_probes(p.inject_x, R_probe, N_pts=256)
    # Detection shells
    for label, x_pos in zip(shell_labels, shell_x):
        probe_dict[label] = make_shell_probes(x_pos, R_probe, N_pts=256)
    # B ring filament probes (topological response)
    probe_dict['B_ring'] = make_ring_probes(p.x_B, p.R_ring, N_pts=64)

    solver.setup_probes(probe_dict)
    all_labels = ['A'] + shell_labels + ['B_ring']
    print(f"  Detection shells (distance from A):")
    for label, d in zip(shell_labels, shell_d):
        t_cs = d / p.cs
        t_uv = d / p.v_uv
        print(f"    {label}: x={shell_x[shell_labels.index(label)]:.0f}, "
              f"d={d:.0f}, t_cs={t_cs:.2f}, t_UV={t_uv:.2f}")
    print(f"  B ring filament: 64 points on ring at x={p.x_B}")
    print(f"  Probe radius: {R_probe:.4f} ({R_probe / xi:.1f} xi), 256 pts/shell")

    # --- Relaxation ---
    n_relax = 100
    print(f"\n  Relaxation ({n_relax} steps)...")
    sys.stdout.flush()
    t0 = time.time()
    for s in range(n_relax):
        solver.rk4_step()
        if (s + 1) % 20 == 0:
            solver.K.compute_density(
                ((solver.N3 + 255) // 256,), (256,),
                (solver.psi_re, solver.psi_im, solver.rho, np.int32(solver.N3)))
            rm = float(cp.mean(solver.rho))
            nc = int(cp.sum(cp.isnan(solver.psi_re)))
            print(f"    step {s + 1}: <rho>={rm:.6f}"
                  f"{'  *** NaN ***' if nc else ''}")
            sys.stdout.flush()
            if nc:
                print("  FATAL: NaN during relaxation.")
                return
    t_relax = time.time() - t0
    print(f"  Relaxed in {t_relax:.1f}s ({n_relax / t_relax:.1f} steps/s)")

    # --- Save state ---
    print("\n  Saving state...")
    saved_re = solver.psi_re.copy()
    saved_im = solver.psi_im.copy()
    ref_t0 = solver.sample_probes()

    # --- Reference run ---
    # Run until 1.5 * t_acoustic (capture past acoustic arrival)
    t_max = p.t_acoustic * 1.5
    total_steps = int(np.ceil(t_max / p.dt))
    meas_int = max(1, total_steps // 2000)
    meas_set = set()
    for s in range(1, total_steps + 1):
        if s <= 20 or s % meas_int == 0:
            meas_set.add(s)

    print(f"\n  REFERENCE RUN ({total_steps} steps, t_max={t_max:.2f}, "
          f"~{total_steps / 6:.0f}s)...")
    sys.stdout.flush()

    ref_ts = {}
    t_ref0 = time.time()
    for step in range(1, total_steps + 1):
        solver.rk4_step()
        if step in meas_set:
            ref_ts[step] = solver.sample_probes()
        if step % (max(1, total_steps // 10)) == 0:
            el = time.time() - t_ref0
            rate = step / el if el > 0 else 0
            print(f"    t={step * p.dt:.4f}  {step}/{total_steps}  [{rate:.1f} s/s]")
            sys.stdout.flush()
    t_ref_elapsed = time.time() - t_ref0
    print(f"  Reference done ({t_ref_elapsed:.1f}s)")

    # --- Restore + inject ---
    solver.psi_re[:] = saved_re
    solver.psi_im[:] = saved_im
    del saved_re, saved_im
    cp.cuda.Stream.null.synchronize()

    print(f"\n  INJECTING UV PERTURBATION AT PARTICLE A's CENTER")
    print(f"    Location: ({p.inject_x:.1f}, {p.inject_y:.1f}, {p.inject_z:.1f})")
    print(f"    sigma={p.sigma:.4f}, dphi={p.dphi:.4f}, void={p.void}")
    solver.inject_at_A()

    # --- Baseline footprint ---
    pert_t0 = solver.sample_probes()
    baselines = {}
    print(f"  Baseline footprint (perturbation at t=0):")
    for label in all_labels:
        dr = pert_t0[label]['rho'] - ref_t0[label]['rho']
        dp = pert_t0[label]['phase'] - ref_t0[label]['phase']
        dp = np.arctan2(np.sin(dp), np.cos(dp))
        baselines[label] = {'rho': dr, 'phase': dp}
        rms_rho = np.sqrt(np.mean(dr ** 2))
        mad_phase = np.mean(np.abs(dp))
        print(f"    {label:>6}: rho_rms={rms_rho:.3e}, phase_mad={mad_phase:.3e}")

    # --- Perturbed run ---
    # Adaptive threshold: signal drops ~1/d from A
    THRESH_BASE = 0.001
    dt_detect = {label: {'rho': None, 'phase': None} for label in shell_labels + ['B_ring']}
    t_detect_A = None

    # Time series for cross-correlation
    ts_times = []
    ts_signal_A = []
    ts_signals = {label: {'rho': [], 'phase': []} for label in shell_labels + ['B_ring']}

    print(f"\n  PERTURBED RUN ({total_steps} steps, base_thresh={THRESH_BASE})...")
    sys.stdout.flush()
    t_pert0 = time.time()
    final_step = total_steps

    for step in range(1, total_steps + 1):
        solver.rk4_step()

        if step in meas_set:
            t_cur = step * p.dt
            pert = solver.sample_probes()

            if step in ref_ts:
                ref = ref_ts[step]
            else:
                nearest = min(ref_ts.keys(), key=lambda k: abs(k - step))
                ref = ref_ts[nearest]

            # Signal at A (injection site)
            dA = pert['A']['rho'] - ref['A']['rho'] - baselines['A']['rho']
            sig_A = np.sqrt(np.mean(dA ** 2))
            ts_times.append(t_cur)
            ts_signal_A.append(sig_A)

            if t_detect_A is None and sig_A > THRESH_BASE:
                t_detect_A = t_cur
                print(f"  ** A RESPONSE: t={t_cur:.4f}  sig={sig_A:.3e}")
                sys.stdout.flush()

            # Signal at each detection shell
            for label in shell_labels + ['B_ring']:
                dr = pert[label]['rho'] - ref[label]['rho'] - baselines[label]['rho']
                dp = pert[label]['phase'] - ref[label]['phase'] - baselines[label]['phase']
                dp = np.arctan2(np.sin(dp), np.cos(dp))
                sig_rho = np.sqrt(np.mean(dr ** 2))
                sig_phase = np.mean(np.abs(dp))
                ts_signals[label]['rho'].append(sig_rho)
                ts_signals[label]['phase'].append(sig_phase)

                if dt_detect[label]['rho'] is None and sig_rho > THRESH_BASE:
                    dt_detect[label]['rho'] = t_cur
                    d_str = label
                    print(f"  ** {d_str} DENSITY:  t={t_cur:.4f}  "
                          f"(t/t_ac={t_cur / p.t_acoustic:.3f})  "
                          f"sig={sig_rho:.3e}")
                    sys.stdout.flush()
                if dt_detect[label]['phase'] is None and sig_phase > THRESH_BASE:
                    dt_detect[label]['phase'] = t_cur
                    print(f"  ** {label} PHASE:    t={t_cur:.4f}  "
                          f"(t/t_ac={t_cur / p.t_acoustic:.3f})  "
                          f"sig={sig_phase:.3e}")
                    sys.stdout.flush()

        # Progress
        if step % (max(1, total_steps // 10)) == 0:
            el = time.time() - t_pert0
            rate = step / el if el > 0 else 0
            print(f"    t={step * p.dt:.4f}  {step}/{total_steps}  [{rate:.1f} s/s]")
            sys.stdout.flush()

        # Early exit: all shells detected both signals
        all_found = all(dt_detect[l]['rho'] is not None for l in shell_labels)
        if all_found:
            t_cur = step * p.dt
            latest = max(dt_detect[l]['rho'] for l in shell_labels)
            if t_cur > latest * 2.5:
                print(f"  Early exit at t={t_cur:.4f}")
                final_step = step
                break

    t_pert_elapsed = time.time() - t_pert0

    # ================================================================
    # RESULTS
    # ================================================================
    print(f"\n{'=' * 65}")
    print(f"  EPR ENTANGLEMENT TEST RESULTS")
    print(f"{'=' * 65}")
    print(f"  Solver:         RK4 + FD4 (strictly local, NO FFT)")
    print(f"  Grid:           {p.N}^3 = {p.N_total:,}")
    print(f"  Particle A:     x = {p.x_A:.1f}  ({p.x_A / p.xi:.1f} xi)")
    print(f"  Particle B:     x = {p.x_B:.1f}  ({p.x_B / p.xi:.1f} xi)")
    print(f"  Distance:       D = {p.D:.1f}  ({p.D / p.xi:.1f} xi)")
    print(f"  Injection:      at ({p.inject_x:.1f},{p.inject_y:.1f},{p.inject_z:.1f})")
    print(f"  Perturbation:   sigma=0.5xi, dphi=pi, void={p.void}")
    print(f"  Threshold:      {THRESH_BASE}")
    print(f"  t_acoustic:     {p.t_acoustic:.4f}")
    print(f"  t_UV (k_dom):   {p.t_uv:.4f}")
    print(f"  v_UV / c_s:     {p.v_uv:.2f}")

    print(f"\n  --- ARRIVAL TABLE ---")
    print(f"  {'Shell':>7}  {'d':>5}  {'t_cs':>8}  {'t_UV':>8}  "
          f"{'t_rho':>8}  {'t_phase':>8}  {'v/c_s':>6}")
    print(f"  {'-' * 60}")
    det_points = []
    for label, d in zip(shell_labels, shell_d):
        t_cs_exp = d / p.cs
        t_uv_exp = d / p.v_uv
        t_rho = dt_detect[label]['rho']
        t_phase = dt_detect[label]['phase']
        tr = f"{t_rho:.4f}" if t_rho else "---"
        tp = f"{t_phase:.4f}" if t_phase else "---"
        vr = f"{d / t_rho:.2f}" if t_rho else "---"
        print(f"  {label:>7}  {d:>5.0f}  {t_cs_exp:>8.2f}  {t_uv_exp:>8.2f}  "
              f"{tr:>8}  {tp:>8}  {vr:>6}")
        if t_rho is not None:
            det_points.append((d, t_rho))

    # B ring filament
    t_Br_rho = dt_detect['B_ring']['rho']
    t_Br_phase = dt_detect['B_ring']['phase']
    print(f"  {'B_ring':>7}  {p.D:>5.0f}  {p.t_acoustic:>8.2f}  {p.t_uv:>8.2f}  "
          f"{t_Br_rho if t_Br_rho else '---':>8}  "
          f"{t_Br_phase if t_Br_phase else '---':>8}  "
          f"{p.D / t_Br_rho:.2f}" if t_Br_rho else "---")

    # Fit wavefront speed from detected shells
    c_fit = None
    if len(det_points) >= 2:
        ds = np.array([d for d, _ in det_points])
        ts = np.array([t for _, t in det_points])
        slope = np.polyfit(ds, ts, 1)[0]
        if slope > 0:
            c_fit = 1.0 / slope / p.cs
            print(f"\n  >> Fitted wavefront speed: c_fit = {c_fit:.4f} c_s")
            print(f"  >> Bogoliubov prediction:  v_dom = {p.v_uv:.4f} c_s")
            order_ok = all(ts[j] < ts[j + 1] for j in range(len(ts) - 1))
            print(f"  >> Inner->outer ordering: {'CORRECT' if order_ok else 'VIOLATED'}")

    print(f"\n  --- TIMING ---")
    print(f"  Relaxation:  {t_relax:.1f}s")
    print(f"  Reference:   {t_ref_elapsed:.1f}s")
    print(f"  Perturbed:   {t_pert_elapsed:.1f}s")
    print(f"  Total:       {t_relax + t_ref_elapsed + t_pert_elapsed:.1f}s")

    # --- Cross-correlation ---
    ts_times = np.array(ts_times)
    ts_signal_A = np.array(ts_signal_A)
    # Use d20 (B location) for cross-correlation
    B_label = shell_labels[-1]  # d20
    ts_B_rho = np.array(ts_signals[B_label]['rho'])

    if len(ts_times) > 10 and np.std(ts_signal_A) > 1e-12 and np.std(ts_B_rho) > 1e-12:
        print(f"\n  --- CROSS-CORRELATION (A vs B @ d={shell_d[-1]:.0f}) ---")
        A_norm = ts_signal_A - np.mean(ts_signal_A)
        B_norm = ts_B_rho - np.mean(ts_B_rho)
        A_std, B_std = np.std(A_norm), np.std(B_norm)
        n = len(A_norm)
        cc = np.correlate(A_norm, B_norm, mode='full')
        cc /= (A_std * B_std * n)
        lags = np.arange(-n + 1, n)
        dt_meas = np.mean(np.diff(ts_times)) if len(ts_times) > 1 else p.dt
        lag_times = lags * dt_meas

        pos_mask = lag_times > 0
        if np.any(pos_mask):
            cc_pos = cc[pos_mask]
            lag_pos = lag_times[pos_mask]
            peak_idx = np.argmax(cc_pos)
            peak_lag = lag_pos[peak_idx]
            peak_cc = cc_pos[peak_idx]
            print(f"  Peak correlation:  CC = {peak_cc:.4f} at lag = {peak_lag:.4f}")
            if peak_lag > 0:
                v_cc = p.D / peak_lag
                print(f"  Implied speed:     v = {v_cc:.2f} c_s")
                print(f"  t_lag / t_acoustic = {peak_lag / p.t_acoustic:.4f}")
    else:
        print(f"\n  Cross-correlation: insufficient signal.")

    # --- VERDICT ---
    print(f"\n  {'=' * 55}")
    # Use the B-location shell (d20) for the primary verdict
    t_B = dt_detect[shell_labels[-1]]['rho']  # d20 density detection
    if t_B is None:
        t_B = dt_detect[shell_labels[-1]]['phase']
    if t_B is None and t_Br_rho is not None:
        t_B = t_Br_rho

    if t_B is not None:
        v_measured = p.D / t_B
        ratio_acoustic = t_B / p.t_acoustic

        if ratio_acoustic < 0.5:
            print(f"  >> B RESPONSE TIME:  t = {t_B:.4f}")
            print(f"  >>   = {ratio_acoustic:.4f} * t_acoustic")
            print(f"  >>   v_effective = {v_measured:.2f} c_s")
            print(f"  >>")
            print(f"  >> {'>' * 50}")
            print(f"  >> HYDRODYNAMIC EPR ENTANGLEMENT CONFIRMED")
            print(f"  >>")
            print(f"  >> The UV Bogoliubov channel (v_g = {p.v_uv:.1f} c_s)")
            print(f"  >> transmitted a perturbation from Particle A to")
            print(f"  >> Particle B at {v_measured:.1f}x the emergent light")
            print(f"  >> speed (c_s), using a STRICTLY LOCAL solver.")
            print(f"  >>")
            print(f"  >> eps(k) = sqrt(k^2 + k^4/4) enables")
            print(f"  >> superluminal information transfer between")
            print(f"  >> spatially separated topological defects.")
            print(f"  >> {'>' * 50}")
        elif ratio_acoustic < 1.5:
            print(f"  >> B responds at t = {t_B:.4f} ~ t_acoustic = {p.t_acoustic:.2f}")
            print(f"  >> Signal propagated at c_s. Classical acoustic.")
        else:
            print(f"  >> B responds at t = {t_B:.4f} >> t_acoustic")
    else:
        print(f"  >> NO RESPONSE at Particle B within t = {final_step * p.dt:.2f}")
        if c_fit is not None and len(det_points) >= 2:
            # Extrapolate arrival at B
            d_furthest, t_furthest = det_points[-1]
            t_extrap = p.D / (p.D / p.t_acoustic) if c_fit else None
            print(f"  >> Fitted speed from shells: c_fit = {c_fit:.2f} c_s")
            print(f"  >> Extrapolated B arrival: t ~ {p.D / c_fit:.2f}")
    print(f"  {'=' * 55}")
    sys.stdout.flush()

    del solver
    cp.get_default_memory_pool().free_all_blocks()


# =================================================================
if __name__ == "__main__":
    run_epr()
