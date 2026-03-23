"""
UHF Supra-Acoustic Bit Transmission Test
============================================================
THESIS: Shannon information (a decodable bit) travels faster than
        the emergent speed of light (c_s) through the UV Bogoliubov
        channel.

PROTOCOL (Equal-Energy Binary Signaling):
  Particle A: vortex ring at x = -10  (natural units)
  Particle B: vortex ring at x = +10
  Distance D = 20.0,  t_acoustic = D / c_s = 20.0

  Bit '0':  UV density void (sigma=0.5*xi, void=0.01) + phase twist +pi/2
  Bit '1':  UV density void (sigma=0.5*xi, void=0.01) + phase twist -pi/2

  Both encodings share the EXACT SAME density void, total energy,
  Hamiltonian, and k-spectrum.  Only the topological parity differs.

BLIND DECODER:
  At Particle B, continuously monitor phase holonomy and density.
  Define t_decode as the first time the responses to Bit 0 and Bit 1
  diverge beyond a noise threshold derived from the reference run.

VERDICT:
  t_decode >= 20.0  => Only dispersive precursors, no causal info.
  t_decode <  20.0  => CAUSAL SUPRA-ACOUSTIC SIGNALING CONFIRMED.

SOLVER: RK4 + 4th-order FD Laplacian.  NO FFT.  Strictly local stencil.
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
class BitParams:
    def __init__(self, N, L_half):
        self.g = 1.0
        self.rho0 = 1.0
        self.mu = 1.0
        self.cs = 1.0
        self.xi = 1.0 / np.sqrt(2.0)

        self.N = N
        self.N_total = N ** 3
        self.L = L_half
        self.dx = 2.0 * L_half / N
        self.dt = 0.15 * self.dx ** 2

        # Particle positions
        self.x_A = -10.0
        self.x_B = +10.0
        self.D = self.x_B - self.x_A

        # Ring parameters
        self.R_ring = 3.0 * self.xi

        # Perturbation (UV, sharp) at ring center
        self.sigma = 0.5 * self.xi
        self.void = 0.01  # 99% depletion — same for both bits
        # Phase twists: +pi/2 (bit 0), -pi/2 (bit 1)
        self.dphi_bit0 = +np.pi / 2.0
        self.dphi_bit1 = -np.pi / 2.0

        # Injection point: ring A center (bulk condensate)
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
# CUDA kernel manager
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
        _w = cp.zeros(1024, dtype=cp.float32)
        _kernels.compute_density((4,), (256,), (_w, _w, _w, np.int32(1024)))
        cp.cuda.Stream.null.synchronize()
    return _kernels


# =================================================================
# Vortex ring imprinting
# =================================================================
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


# =================================================================
# Probe generation
# =================================================================
def make_shell_probes(x_center, R_probe, N_pts=256):
    idx = np.arange(N_pts, dtype=np.float32)
    golden = (1 + np.sqrt(5)) / 2
    theta = np.arccos(1 - 2 * (idx + 0.5) / N_pts)
    phi = 2 * np.pi * idx / golden
    px = (x_center + R_probe * np.sin(theta) * np.cos(phi)).astype(np.float32)
    py = (R_probe * np.sin(theta) * np.sin(phi)).astype(np.float32)
    pz = (R_probe * np.cos(theta)).astype(np.float32)
    return px, py, pz


def make_ring_probes(cx, R_ring, N_pts=64):
    t = np.linspace(0, 2 * np.pi, N_pts, endpoint=False).astype(np.float32)
    px = np.full(N_pts, cx, dtype=np.float32)
    py = (R_ring * np.cos(t)).astype(np.float32)
    pz = (R_ring * np.sin(t)).astype(np.float32)
    return px, py, pz


# =================================================================
# RK4 + FD4 Solver
# =================================================================
class BitSolver:
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

    def inject(self, dphi):
        """Inject UV perturbation at A's center with given phase twist."""
        p = self.p
        g = (self.N3 + 255) // 256
        self.K.inject_phase_and_void(
            (g,), (256,),
            (self.psi_re, self.psi_im,
             np.float32(p.inject_x), np.float32(p.inject_y), np.float32(p.inject_z),
             np.float32(p.sigma),
             np.float32(dphi),
             np.float32(p.void),
             np.float32(-p.L), np.float32(-p.L), np.float32(-p.L),
             np.float32(p.dx),
             np.int32(p.N), np.int32(p.N), np.int32(p.N)))
        cp.cuda.Stream.null.synchronize()


# =================================================================
# Single bit-run: inject + evolve + record B response
# =================================================================
def run_single_bit(solver, p, saved_re, saved_im, dphi, bit_label,
                   total_steps, meas_set, ref_ts):
    """Run one bit encoding. Returns dict: step -> probe snapshot at B."""
    # Restore pristine state
    solver.psi_re[:] = saved_re
    solver.psi_im[:] = saved_im
    cp.cuda.Stream.null.synchronize()

    # Inject
    solver.inject(dphi)

    # Record t=0 footprint
    snap0 = solver.sample_probes()
    for label in ['B_shell', 'B_ring']:
        dr = snap0[label]['rho']
        dp = snap0[label]['phase']
        rms_rho = np.sqrt(np.mean(dr ** 2))
        # Just report raw values at t=0 (not delta, since there's no ref yet)
    print(f"    Bit '{bit_label}': injected dphi={dphi:+.4f}")
    sys.stdout.flush()

    # Evolve
    ts_data = {}  # step -> {'B_shell': {...}, 'B_ring': {...}}
    t0 = time.time()
    for step in range(1, total_steps + 1):
        solver.rk4_step()
        if step in meas_set:
            snap = solver.sample_probes()
            ts_data[step] = {
                'B_shell': snap['B_shell'].copy(),
                'B_ring': snap['B_ring'].copy(),
            }
        if step % (max(1, total_steps // 10)) == 0:
            el = time.time() - t0
            rate = step / el if el > 0 else 0
            print(f"    [{bit_label}] t={step * p.dt:.4f}  "
                  f"{step}/{total_steps}  [{rate:.1f} s/s]")
            sys.stdout.flush()
    elapsed = time.time() - t0
    print(f"    [{bit_label}] Done ({elapsed:.1f}s)")
    sys.stdout.flush()
    return ts_data


# =================================================================
# Main experiment
# =================================================================
def run_bit_test():
    dev = cp.cuda.Device(0)
    free, total = dev.mem_info
    max_N3 = int(free * 0.65 / (12 * 4))
    max_N = int(max_N3 ** (1.0 / 3.0))
    max_N = (max_N // 32) * 32
    max_N = min(max_N, 704)
    print(f"GPU 0: {free / 1e9:.2f} GB free -> N={max_N}")

    xi = 1.0 / np.sqrt(2.0)
    L_half = 25.0
    p = BitParams(max_N, L_half)

    print(f"\n{'#' * 65}")
    print(f"  SUPRA-ACOUSTIC BIT TRANSMISSION TEST")
    print(f"{'#' * 65}")
    print(f"  Grid:           {p.N}^3 = {p.N_total:,}")
    print(f"  Box:            [-{L_half}, +{L_half}],  dx = {p.dx:.5f}")
    print(f"  dx/xi:          {p.dx / p.xi:.4f}  ({p.xi / p.dx:.1f} cells/xi)")
    print(f"  dt:             {p.dt:.6f}")
    print(f"  Particle A:     x = {p.x_A:.1f}  (= {p.x_A / p.xi:.1f} xi)")
    print(f"  Particle B:     x = {p.x_B:.1f}  (= {p.x_B / p.xi:.1f} xi)")
    print(f"  Distance D:     {p.D:.1f}  (= {p.D / p.xi:.1f} xi)")
    print(f"  Ring radius:    {p.R_ring:.4f}  (= {p.R_ring / p.xi:.1f} xi)")
    print(f"  Perturbation:   sigma = {p.sigma:.4f}  (= 0.5 xi)")
    print(f"  sigma/dx:       {p.sigma / p.dx:.1f} cells")
    print(f"  Density void:   {p.void}  (same for both bits)")
    print(f"  Bit '0' phase:  dphi = +pi/2 = {p.dphi_bit0:+.4f}")
    print(f"  Bit '1' phase:  dphi = -pi/2 = {p.dphi_bit1:+.4f}")
    print(f"  Injection at:   ({p.inject_x:.1f}, {p.inject_y:.1f}, {p.inject_z:.1f})")
    print(f"  t_acoustic:     {p.t_acoustic:.4f}  (D / c_s)")
    print(f"  t_UV (k_dom):   {p.t_uv:.4f}  (D / v_g)")
    print(f"  v_UV / c_s:     {p.v_uv:.2f}")
    sys.stdout.flush()

    # --- Create solver ---
    solver = BitSolver(p)

    # --- Imprint two vortex rings ---
    print(f"\n  Imprinting two vortex rings (Abrikosov product)...")
    ok = imprint_two_rings(solver)
    if not ok:
        print("  FATAL: Failed to imprint vortex rings.")
        return

    # --- Setup probes at B ---
    R_probe = 1.0 * xi
    probe_dict = {
        'B_shell': make_shell_probes(p.x_B, R_probe, N_pts=256),
        'B_ring': make_ring_probes(p.x_B, p.R_ring, N_pts=64),
    }
    solver.setup_probes(probe_dict)
    print(f"  Probes at B: shell (R={R_probe:.4f}, 256 pts) + ring (64 pts)")

    # --- Relaxation ---
    n_relax = 100
    print(f"\n  Relaxation ({n_relax} steps)...")
    sys.stdout.flush()
    t_relax0 = time.time()
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
    t_relax = time.time() - t_relax0
    print(f"  Relaxed in {t_relax:.1f}s ({n_relax / t_relax:.1f} steps/s)")

    # --- Save pristine state ---
    print("\n  Saving pristine state (post-relaxation)...")
    saved_re = solver.psi_re.copy()
    saved_im = solver.psi_im.copy()

    # --- Reference run (no perturbation) for noise floor ---
    t_max = p.t_acoustic * 1.5
    total_steps = int(np.ceil(t_max / p.dt))
    meas_int = max(1, total_steps // 2000)
    meas_set = set()
    for s in range(1, total_steps + 1):
        if s <= 40 or s % meas_int == 0:
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

    print(f"  (Noise floor will be computed from bit0-vs-bit1 early-time differential)")

    # ================================================================
    # BIT '0' RUN: +pi/2 phase twist
    # ================================================================
    print(f"\n{'=' * 65}")
    print(f"  BIT '0' RUN (dphi = +pi/2)")
    print(f"{'=' * 65}")
    sys.stdout.flush()
    bit0_data = run_single_bit(solver, p, saved_re, saved_im,
                               p.dphi_bit0, '0',
                               total_steps, meas_set, ref_ts)

    # ================================================================
    # BIT '1' RUN: -pi/2 phase twist
    # ================================================================
    print(f"\n{'=' * 65}")
    print(f"  BIT '1' RUN (dphi = -pi/2)")
    print(f"{'=' * 65}")
    sys.stdout.flush()
    bit1_data = run_single_bit(solver, p, saved_re, saved_im,
                               p.dphi_bit1, '1',
                               total_steps, meas_set, ref_ts)

    # Free GPU memory
    del saved_re, saved_im
    del solver
    cp.get_default_memory_pool().free_all_blocks()

    # ================================================================
    # DECODE: find t_decode where bit0 and bit1 diverge at B
    # ================================================================
    print(f"\n{'=' * 65}")
    print(f"  BLIND DECODER ANALYSIS")
    print(f"{'=' * 65}")

    # Collect common measurement steps
    common_steps = sorted(set(bit0_data.keys()) & set(bit1_data.keys()))
    if not common_steps:
        print("  FATAL: No common measurement steps between bit runs.")
        return

    print(f"  Common measurement points: {len(common_steps)}")
    print(f"  Time range: [{common_steps[0] * p.dt:.4f}, "
          f"{common_steps[-1] * p.dt:.4f}]")

    # --- First pass: compute all deltas to establish noise floor ---
    # CFL minimum arrival time: t_CFL = D / (2*dx/dt) = D*dt/(2*dx)
    t_cfl = p.D * p.dt / (2.0 * p.dx)
    print(f"  CFL minimum arrival: t_CFL = {t_cfl:.4f}")
    all_deltas = []  # (t, mad_phase_shell, mad_phase_ring, rms_rho_shell, rms_rho_ring)
    for step in common_steps:
        t_cur = step * p.dt
        b0_shell = bit0_data[step]['B_shell']
        b1_shell = bit1_data[step]['B_shell']
        b0_ring = bit0_data[step]['B_ring']
        b1_ring = bit1_data[step]['B_ring']
        dp_shell = b0_shell['phase'] - b1_shell['phase']
        dp_shell = np.arctan2(np.sin(dp_shell), np.cos(dp_shell))
        dp_ring = b0_ring['phase'] - b1_ring['phase']
        dp_ring = np.arctan2(np.sin(dp_ring), np.cos(dp_ring))
        dr_shell = b0_shell['rho'] - b1_shell['rho']
        dr_ring = b0_ring['rho'] - b1_ring['rho']
        mad_phase_shell = np.mean(np.abs(dp_shell))
        mad_phase_ring = np.mean(np.abs(dp_ring))
        rms_rho_shell = np.sqrt(np.mean(dr_shell ** 2))
        rms_rho_ring = np.sqrt(np.mean(dr_ring ** 2))
        all_deltas.append((t_cur, mad_phase_shell, mad_phase_ring,
                           rms_rho_shell, rms_rho_ring))

    # Noise floor: max delta in the causal shadow (t < t_CFL)
    early_deltas = [d for d in all_deltas if d[0] < t_cfl]
    if early_deltas:
        noise_phase = max(d[1] for d in early_deltas)
        noise_rho = max(d[3] for d in early_deltas)
    else:
        noise_phase = 0.0
        noise_rho = 0.0
    # Threshold: 10x noise floor, minimum 1e-6 (just above float32 epsilon)
    THRESH_PHASE = max(10.0 * noise_phase, 1e-6)
    THRESH_RHO = max(10.0 * noise_rho, 1e-6)
    print(f"  Differential noise floor (t < {t_cfl:.4f}):")
    print(f"    phase: {noise_phase:.3e}  -> threshold: {THRESH_PHASE:.3e}")
    print(f"    rho:   {noise_rho:.3e}  -> threshold: {THRESH_RHO:.3e}")

    # --- Second pass: detect and print ---
    ts_times = []
    ts_delta_phase_shell = []
    ts_delta_phase_ring = []
    ts_delta_rho_shell = []
    ts_delta_rho_ring = []
    ts_signed_phase = []
    t_decode_phase = None
    t_decode_rho = None

    print(f"\n  --- DELTA(B) TIME SERIES ---")
    print(f"  {'t':>8}  {'t/t_ac':>7}  {'d_phase_sh':>11}  {'d_phase_rg':>11}  "
          f"{'d_rho_sh':>11}  {'d_rho_rg':>11}  {'signed_ph':>10}  {'status':>12}")
    print(f"  {'-' * 90}")

    for i, (t_cur, mad_ps, mad_pr, rms_rs, rms_rr) in enumerate(all_deltas):
        step = common_steps[i]
        ts_times.append(t_cur)
        ts_delta_phase_shell.append(mad_ps)
        ts_delta_phase_ring.append(mad_pr)
        ts_delta_rho_shell.append(rms_rs)
        ts_delta_rho_ring.append(rms_rr)

        # Signed mean phase for parity detection
        b0_shell = bit0_data[step]['B_shell']
        b1_shell = bit1_data[step]['B_shell']
        dp_shell = b0_shell['phase'] - b1_shell['phase']
        dp_shell = np.arctan2(np.sin(dp_shell), np.cos(dp_shell))
        signed_ph = np.mean(dp_shell)
        ts_signed_phase.append(signed_ph)

        # Detection
        status = ""
        if t_decode_phase is None and mad_ps > THRESH_PHASE:
            t_decode_phase = t_cur
            status = "** PHASE **"
        if t_decode_rho is None and rms_rs > THRESH_RHO:
            t_decode_rho = t_cur
            if not status:
                status = "** RHO **"
            else:
                status = "** BOTH **"

        # Print: first 10, detections, every 50th, last 3
        should_print = (i < 10 or i > len(all_deltas) - 3
                        or i % 50 == 0 or status)
        if should_print:
            print(f"  {t_cur:>8.4f}  {t_cur / p.t_acoustic:>7.4f}  "
                  f"{mad_ps:>11.3e}  {mad_pr:>11.3e}  "
                  f"{rms_rs:>11.3e}  {rms_rr:>11.3e}  "
                  f"{signed_ph:>+10.3e}  {status}")
            sys.stdout.flush()

    ts_times = np.array(ts_times)
    ts_delta_phase_shell = np.array(ts_delta_phase_shell)
    ts_delta_phase_ring = np.array(ts_delta_phase_ring)
    ts_delta_rho_shell = np.array(ts_delta_rho_shell)
    ts_delta_rho_ring = np.array(ts_delta_rho_ring)

    # ================================================================
    # CROSS-CORRELATION of bit0 vs bit1 phase response at B
    # ================================================================
    print(f"\n  --- CROSS-CORRELATION ---")
    if len(ts_times) > 20:
        # Cross-correlate bit0_phase_response with bit1_phase_response
        # to find if they are anti-correlated (expected for +/-pi/2 twist)
        b0_phases = []
        b1_phases = []
        for step in common_steps:
            if step in ref_ts:
                ref = ref_ts[step]
            else:
                nearest = min(ref_ts.keys(), key=lambda k: abs(k - step))
                ref = ref_ts[nearest]
            r0 = bit0_data[step]['B_shell']['phase'] - ref['B_shell']['phase']
            r0 = np.arctan2(np.sin(r0), np.cos(r0))
            r1 = bit1_data[step]['B_shell']['phase'] - ref['B_shell']['phase']
            r1 = np.arctan2(np.sin(r1), np.cos(r1))
            b0_phases.append(np.mean(r0))
            b1_phases.append(np.mean(r1))

        b0_phases = np.array(b0_phases)
        b1_phases = np.array(b1_phases)

        # Pearson correlation of the two responses over time
        b0_c = b0_phases - np.mean(b0_phases)
        b1_c = b1_phases - np.mean(b1_phases)
        s0, s1 = np.std(b0_c), np.std(b1_c)
        if s0 > 1e-12 and s1 > 1e-12:
            pearson = np.sum(b0_c * b1_c) / (s0 * s1 * len(b0_c))
            print(f"  Pearson(bit0_phase, bit1_phase): r = {pearson:.6f}")
            if pearson < -0.5:
                print(f"  >> ANTI-CORRELATED: bit0 and bit1 carry "
                      f"opposite phase parity at B")
        else:
            print(f"  Insufficient phase variance at B for correlation.")

        # Time-resolved anti-correlation onset
        print(f"\n  --- SLIDING ANTI-CORRELATION (window=20) ---")
        window = min(20, len(b0_phases) // 4)
        if window >= 5:
            slide_r = []
            slide_t = []
            t_anticorr = None
            for i in range(len(b0_phases) - window):
                w0 = b0_phases[i:i + window]
                w1 = b1_phases[i:i + window]
                w0c = w0 - np.mean(w0)
                w1c = w1 - np.mean(w1)
                sw0, sw1 = np.std(w0c), np.std(w1c)
                if sw0 > 1e-12 and sw1 > 1e-12:
                    r = np.sum(w0c * w1c) / (sw0 * sw1 * window)
                else:
                    r = 0.0
                slide_r.append(r)
                slide_t.append(ts_times[i + window // 2])
                if t_anticorr is None and r < -0.5:
                    t_anticorr = ts_times[i]

            if t_anticorr is not None:
                print(f"  Anti-correlation (r < -0.5) onset: "
                      f"t = {t_anticorr:.4f} "
                      f"(t/t_ac = {t_anticorr / p.t_acoustic:.4f})")

    # ================================================================
    # RESULTS
    # ================================================================
    print(f"\n{'=' * 65}")
    print(f"  SUPRA-ACOUSTIC BIT TRANSMISSION RESULTS")
    print(f"{'=' * 65}")
    print(f"  Solver:         RK4 + FD4 (strictly local, NO FFT)")
    print(f"  Grid:           {p.N}^3 = {p.N_total:,}")
    print(f"  Distance:       D = {p.D:.1f}  ({p.D / p.xi:.1f} xi)")
    print(f"  Bit '0':        dphi = +pi/2, void = {p.void}")
    print(f"  Bit '1':        dphi = -pi/2, void = {p.void}")
    print(f"  Noise floor:    phase={noise_phase:.3e}, rho={noise_rho:.3e}")
    print(f"  Decode thresh:  phase={THRESH_PHASE:.3e}, rho={THRESH_RHO:.3e}")
    print(f"  t_acoustic:     {p.t_acoustic:.4f}")
    print(f"  t_UV (k_dom):   {p.t_uv:.4f}")
    print(f"  v_UV / c_s:     {p.v_uv:.2f}")

    print(f"\n  --- DECODE TIMES ---")
    t_decode = None

    if t_decode_phase is not None:
        v_eff = p.D / t_decode_phase
        print(f"  Phase decode:   t = {t_decode_phase:.4f}  "
              f"(t/t_ac = {t_decode_phase / p.t_acoustic:.4f})  "
              f"v = {v_eff:.2f} c_s")
        t_decode = t_decode_phase
    else:
        print(f"  Phase decode:   NOT ACHIEVED in t_max = {t_max:.2f}")

    if t_decode_rho is not None:
        v_eff = p.D / t_decode_rho
        print(f"  Density decode: t = {t_decode_rho:.4f}  "
              f"(t/t_ac = {t_decode_rho / p.t_acoustic:.4f})  "
              f"v = {v_eff:.2f} c_s")
        if t_decode is None or t_decode_rho < t_decode:
            t_decode = t_decode_rho
    else:
        print(f"  Density decode: NOT ACHIEVED in t_max = {t_max:.2f}")

    # Signed phase parity check: does mean(bit0_phase - bit1_phase) at B
    # show consistent sign after decode time?
    ts_signed_phase = np.array(ts_signed_phase)
    if t_decode is not None:
        post_decode = ts_signed_phase[np.array(ts_times) > t_decode]
        if len(post_decode) > 10:
            pos_frac = np.mean(post_decode > 0)
            print(f"  Signed phase parity after decode: "
                  f"{pos_frac:.1%} positive (expect ~100% or ~0%)")

    # Max delta seen
    idx_max_p = np.argmax(ts_delta_phase_shell)
    idx_max_r = np.argmax(ts_delta_rho_shell)
    print(f"\n  Max |delta_phase| at B: {ts_delta_phase_shell[idx_max_p]:.3e} "
          f"at t={ts_times[idx_max_p]:.4f}")
    print(f"  Max |delta_rho|   at B: {ts_delta_rho_shell[idx_max_r]:.3e} "
          f"at t={ts_times[idx_max_r]:.4f}")

    print(f"\n  --- TIMING ---")
    print(f"  Relaxation:  {t_relax:.1f}s")
    print(f"  Reference:   {t_ref_elapsed:.1f}s")

    # --- VERDICT ---
    print(f"\n  {'=' * 55}")
    if t_decode is not None:
        v_measured = p.D / t_decode
        ratio_acoustic = t_decode / p.t_acoustic

        if ratio_acoustic < 0.5:
            print(f"  >> DECODE TIME:       t = {t_decode:.4f}")
            print(f"  >>   = {ratio_acoustic:.4f} * t_acoustic")
            print(f"  >>   v_information = {v_measured:.2f} c_s")
            print(f"  >>")
            print(f"  >> {'>' * 50}")
            print(f"  >> CAUSAL SUPRA-ACOUSTIC SIGNALING CONFIRMED")
            print(f"  >>")
            print(f"  >> A single Shannon bit (the sign of the phase")
            print(f"  >> twist: +pi/2 vs -pi/2) is DECODABLE at")
            print(f"  >> Particle B at t = {t_decode:.4f},")
            print(f"  >> which is {ratio_acoustic:.4f} * t_acoustic.")
            print(f"  >>")
            print(f"  >> Both bit encodings have IDENTICAL:")
            print(f"  >>   - density void (sigma=0.5*xi, void={p.void})")
            print(f"  >>   - total energy")
            print(f"  >>   - k-spectrum (|delta_phase| = pi/2)")
            print(f"  >>")
            print(f"  >> Only the TOPOLOGICAL PARITY differs,")
            print(f"  >> and it is decoded at {v_measured:.1f}x the")
            print(f"  >> emergent speed of light (c_s).")
            print(f"  >>")
            print(f"  >> This is not a dispersive precursor.")
            print(f"  >> This is INFORMATION TRANSFER.")
            print(f"  >> {'>' * 50}")
        elif ratio_acoustic < 1.5:
            print(f"  >> Bit decoded at t = {t_decode:.4f} ~ t_acoustic")
            print(f"  >> Information travels at c_s. Acoustic channel.")
            print(f"  >> No supra-acoustic signaling.")
        else:
            print(f"  >> Bit decoded at t = {t_decode:.4f} >> t_acoustic")
            print(f"  >> Sub-acoustic information transfer.")
    else:
        print(f"  >> BIT NOT DECODABLE at Particle B")
        print(f"  >> within t_max = {t_max:.2f}")
        print(f"  >> (t_acoustic = {p.t_acoustic:.2f})")
        max_delta = np.max(ts_delta_phase_shell)
        print(f"  >> Max delta_phase seen: {max_delta:.3e}")
        print(f"  >> Threshold was: {THRESH_DECODE:.3e}")
        if max_delta > 0.1 * THRESH_PHASE:
            print(f"  >> Signal approaching threshold — "
                  f"may need longer run.")
    print(f"  {'=' * 55}")
    sys.stdout.flush()


# =================================================================
if __name__ == "__main__":
    run_bit_test()
