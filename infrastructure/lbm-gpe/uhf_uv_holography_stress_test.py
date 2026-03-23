"""
UHF IR vs UV Bogoliubov Dispersion Test
============================================================
THESIS: The Bogoliubov dispersion  eps(k) = sqrt(c_s^2 k^2 + k^4/4)
        unifies emergent GR (phonon regime) with emergent QM
        (free-particle regime) in a single superfluid.

RUN A -- IR LIMIT (Gaussian sigma = 15 xi):
    Spectral content strictly phononic: exp(-sigma^2 k^2/2) -> 0
    for k > 1/xi.  Shells at R >= 75xi where Gaussian spatial
    tails are at machine precision.
    PREDICTION: wavefront at v_g ~ c_s.

RUN B -- UV LIMIT (compact bump R_cut = 1 xi):
    C^inf compact perturbation with sub-exponential spectral tail
    extending deep into k^4 regime.  Exactly zero beyond R_cut.
    PREDICTION: wavefront at v_g >> c_s.

SOLVER: RK4 + 4th-order FD Laplacian. NO FFT. Strictly local stencil.
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
# Bogoliubov dispersion theory
# =================================================================
def bogoliubov_vg(k):
    """Group velocity from eps = sqrt(k^2 + k^4/4) (natural units)."""
    if k < 1e-10:
        return 1.0
    eps = np.sqrt(k**2 + k**4 / 4.0)
    return (k + k**3) / eps


# =================================================================
# Physical parameters
# =================================================================
class GPParams:
    def __init__(self, N, R_boundary_xi, perturb_width_xi, compact=False,
                 void=0.1, dphi=np.pi):
        self.g = 1.0
        self.rho0 = 1.0
        self.mu = 1.0
        self.cs = 1.0
        self.xi = 1.0 / np.sqrt(2.0)

        self.N = N
        self.N_total = N ** 3
        self.R_boundary_xi = R_boundary_xi
        self.R_boundary = R_boundary_xi * self.xi
        self.L = 2.2 * self.R_boundary
        self.dx = 2.0 * self.L / N
        self.dt = 0.15 * self.dx ** 2

        self.compact = compact
        self.perturb_width_xi = perturb_width_xi
        self.perturb_width = perturb_width_xi * self.xi
        self.perturb_phase = dphi
        self.perturb_void = void


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
            'mod': mod,
            'compute_density': mod.get_function('compute_density'),
            'compute_phase': mod.get_function('compute_phase'),
            'sample_sphere': mod.get_function('sample_sphere'),
            'imprint_trefoil_kernel': mod.get_function('imprint_trefoil_kernel'),
            'gp_rhs_fd4': mod.get_function('gp_rhs_fd4'),
            'inject_phase_and_void': mod.get_function('inject_phase_and_void'),
            'inject_compact_void': mod.get_function('inject_compact_void'),
        })()
        _w = cp.zeros(1024, dtype=cp.float32)
        _kernels.compute_density((4,), (256,), (_w, _w, _w, np.int32(1024)))
        cp.cuda.Stream.null.synchronize()
    return _kernels


# =================================================================
# Trefoil imprint + sphere sampling
# =================================================================
def imprint_trefoil(psi_re, psi_im, p, K):
    N_curve = 500
    tc = np.linspace(0, 2 * np.pi, N_curve, endpoint=False).astype(np.float32)
    scale = 4.0 * p.xi
    cx = (scale * (np.sin(tc) + 2 * np.sin(2 * tc)) / 3).astype(np.float32)
    cy = (scale * (np.cos(tc) - 2 * np.cos(2 * tc)) / 3).astype(np.float32)
    cz = (scale * (-np.sin(3 * tc)) / 3).astype(np.float32)
    d_cx, d_cy, d_cz = cp.asarray(cx), cp.asarray(cy), cp.asarray(cz)
    N3 = p.N ** 3
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
        sys.exit("FATAL: imprint NaN")
    rho = psi_re ** 2 + psi_im ** 2
    print(f"  Trefoil: scale={scale:.2f}, "
          f"rho=[{float(cp.min(rho)):.4f}, {float(cp.max(rho)):.4f}]")


def sphere_points(R, N_pts=2048):
    idx = np.arange(N_pts, dtype=np.float32)
    golden = (1 + np.sqrt(5)) / 2
    theta = np.arccos(1 - 2 * (idx + 0.5) / N_pts)
    phi = 2 * np.pi * idx / golden
    return ((R * np.sin(theta) * np.cos(phi)).astype(np.float32),
            (R * np.sin(theta) * np.sin(phi)).astype(np.float32),
            (R * np.cos(theta)).astype(np.float32))


# =================================================================
# RK4 + FD4 solver
# =================================================================
class GPSolver:
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

    def initialize_field(self):
        p = self.p
        if getattr(p, 'uniform', False):
            self.psi_re[:] = cp.float32(1.0)
            self.psi_im[:] = cp.float32(0.0)
            print(f"  Uniform condensate: psi = 1.0")
        else:
            imprint_trefoil(self.psi_re, self.psi_im, p, self.K)
        Nt = np.int32(self.N3)
        self.K.compute_density(((self.N3+255)//256,), (256,),
                               (self.psi_re, self.psi_im, self.rho, Nt))
        cp.cuda.Stream.null.synchronize()
        m = float(cp.mean(self.rho))
        if m > 0:
            s = np.sqrt(p.rho0 / m)
            self.psi_re *= np.float32(s)
            self.psi_im *= np.float32(s)
        print(f"  <rho> = {m:.6f} -> normalized")

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

    def setup_shells(self, radii):
        self.shell_radii = radii
        self.shell_data = []
        N_pts = 2048
        for R in radii:
            sx, sy, sz = sphere_points(R, N_pts)
            self.shell_data.append({
                'R': R, 'N': N_pts,
                'sx': cp.asarray(sx), 'sy': cp.asarray(sy), 'sz': cp.asarray(sz),
                'rho': cp.zeros(N_pts, dtype=cp.float32),
                'phase': cp.zeros(N_pts, dtype=cp.float32),
            })

    def sample_shells(self):
        Nt = np.int32(self.N3)
        g = (self.N3 + 255) // 256
        self.K.compute_density((g,), (256,),
                               (self.psi_re, self.psi_im, self.rho, Nt))
        self.K.compute_phase((g,), (256,),
                             (self.psi_re, self.psi_im, self.phase, Nt))
        cp.cuda.Stream.null.synchronize()
        p = self.p
        for sd in self.shell_data:
            g2 = (sd['N'] + 255) // 256
            self.K.sample_sphere(
                (g2,), (256,),
                (self.rho, self.phase,
                 sd['sx'], sd['sy'], sd['sz'],
                 sd['rho'], sd['phase'],
                 np.float32(-p.L), np.float32(-p.L), np.float32(-p.L),
                 np.float32(1.0 / p.dx),
                 np.int32(p.N), np.int32(p.N), np.int32(p.N),
                 np.int32(sd['N'])))
        cp.cuda.Stream.null.synchronize()
        return [(sd['rho'].get().copy(), sd['phase'].get().copy())
                for sd in self.shell_data]

    def inject_perturbation(self):
        p = self.p
        g = (self.N3 + 255) // 256
        if p.compact:
            self.K.inject_compact_void(
                (g,), (256,),
                (self.psi_re, self.psi_im,
                 np.float32(0.0), np.float32(0.0), np.float32(0.0),
                 np.float32(p.perturb_width),
                 np.float32(p.perturb_phase),
                 np.float32(p.perturb_void),
                 np.float32(-p.L), np.float32(-p.L), np.float32(-p.L),
                 np.float32(p.dx),
                 np.int32(p.N), np.int32(p.N), np.int32(p.N)))
        else:
            self.K.inject_phase_and_void(
                (g,), (256,),
                (self.psi_re, self.psi_im,
                 np.float32(0.0), np.float32(0.0), np.float32(0.0),
                 np.float32(p.perturb_width),
                 np.float32(p.perturb_phase),
                 np.float32(p.perturb_void),
                 np.float32(-p.L), np.float32(-p.L), np.float32(-p.L),
                 np.float32(p.dx),
                 np.int32(p.N), np.int32(p.N), np.int32(p.N)))
        cp.cuda.Stream.null.synchronize()


# =================================================================
# Run one A/B experiment
# =================================================================
def run_experiment(label, N, R_bnd_xi, perturb_width_xi, compact,
                   shell_radii_xi, n_relax=50, void=0.1, dphi=np.pi,
                   thresh=0.005, uniform=False):
    p = GPParams(N, R_bnd_xi, perturb_width_xi, compact=compact,
                 void=void, dphi=dphi)
    p.uniform = uniform

    # Dispersion predictions
    if compact:
        sigma_eff = 0.54 * perturb_width_xi * p.xi
        ptype = f"compact bump R_cut={perturb_width_xi}xi"
    else:
        sigma_eff = perturb_width_xi * p.xi
        ptype = f"Gaussian sigma={perturb_width_xi}xi"
    k_dom = 1.0 / sigma_eff
    v_dom = bogoliubov_vg(k_dom)
    if compact:
        k_edge = 3.0 * k_dom
    else:
        k_edge = np.sqrt(2.0 * np.log(1000.0)) / sigma_eff
    v_edge = bogoliubov_vg(k_edge)

    print(f"\n{'#' * 65}")
    print(f"  {label}")
    print(f"{'#' * 65}")
    print(f"  Grid:         {N}^3 = {p.N_total:,}")
    print(f"  Box:          L = {p.L:.2f}   dx = {p.dx:.4f}  (dx/xi = {p.dx/p.xi:.3f})")
    print(f"  dt:           {p.dt:.6f}")
    print(f"  Perturbation: {ptype}")
    print(f"  Width/dx:     {p.perturb_width/p.dx:.1f} cells")
    print(f"  k_dom:        {k_dom:.4f}   v_g(k_dom) = {v_dom:.4f} c_s")
    print(f"  k_edge:       {k_edge:.4f}   v_g(k_edge) = {v_edge:.4f} c_s")
    print(f"  CFL speed:    {2*p.dx/p.dt:.1f}")
    if not compact:
        # Show Gaussian tail at innermost shell
        R_inner = min(shell_radii_xi)
        tail = np.exp(-(R_inner / perturb_width_xi)**2 / 2.0)
        print(f"  Gaussian at R={R_inner}xi: exp(-{(R_inner/perturb_width_xi)**2/2:.1f}) = {tail:.2e}")
    else:
        print(f"  EXACTLY ZERO beyond R_cut = {perturb_width_xi} xi")
    sys.stdout.flush()

    shell_radii = [r * p.xi for r in shell_radii_xi]
    n_s = len(shell_radii)

    solver = GPSolver(p)
    solver.initialize_field()

    # --- Relaxation ---
    print(f"\n  Relaxation ({n_relax} steps)...")
    t0 = time.time()
    for s in range(n_relax):
        solver.rk4_step()
        if (s + 1) % 10 == 0:
            solver.K.compute_density(
                ((solver.N3 + 255) // 256,), (256,),
                (solver.psi_re, solver.psi_im, solver.rho, np.int32(solver.N3)))
            rm = float(cp.mean(solver.rho))
            nc = int(cp.sum(cp.isnan(solver.psi_re)))
            print(f"    step {s+1}: <rho>={rm:.6f}"
                  f"{'  *** NaN ***' if nc else ''}")
            if nc:
                print("  FATAL: NaN in relaxation. Skipping this run.")
                del solver
                cp.get_default_memory_pool().free_all_blocks()
                return None
    t_relax = time.time() - t0
    print(f"  Relaxed in {t_relax:.1f}s ({n_relax/t_relax:.1f} steps/s)")

    # --- Setup shells ---
    solver.setup_shells(shell_radii)
    for i, R in enumerate(shell_radii):
        t_cs = R / p.cs
        t_edge = R / p.cs / v_edge if v_edge > 0 else t_cs
        print(f"  Shell {i}: R={shell_radii_xi[i]}xi  t_cs={t_cs:.4f}  t_edge={t_edge:.4f}")

    # --- Save state ---
    print("\n  Saving state...")
    saved_re = solver.psi_re.copy()
    saved_im = solver.psi_im.copy()
    ref_t0 = solver.sample_shells()

    # --- Reference run ---
    max_R = max(shell_radii)
    steps_per_ac = int(np.ceil(max_R / p.cs / p.dt))
    total_steps = steps_per_ac * 2
    meas_int = max(1, steps_per_ac // 200)
    meas_set = set()
    for s in range(1, total_steps + 1):
        if s <= 10 or s % meas_int == 0:
            meas_set.add(s)

    print(f"\n  REFERENCE RUN ({total_steps} steps, ~{total_steps/13:.0f}s)...")
    sys.stdout.flush()
    ref_data = {}
    t_ref0 = time.time()
    for step in range(1, total_steps + 1):
        solver.rk4_step()
        if step in meas_set:
            ref_data[step] = solver.sample_shells()
        if step % (meas_int * 40) == 0:
            el = time.time() - t_ref0
            print(f"    t={step*p.dt:.4f}  {step}/{total_steps}  "
                  f"[{step/el:.1f} s/s]")
            sys.stdout.flush()
    t_ref = time.time() - t_ref0
    print(f"  Reference done ({t_ref:.1f}s)")

    # --- Restore + inject ---
    solver.psi_re[:] = saved_re
    solver.psi_im[:] = saved_im
    del saved_re, saved_im
    cp.cuda.Stream.null.synchronize()

    print(f"\n  INJECTING: {ptype}  dphi={p.perturb_phase:.4f}  void={p.perturb_void}")
    solver.inject_perturbation()

    # --- Baseline ---
    pert_t0 = solver.sample_shells()
    baseline_rho = []
    baseline_phase = []
    print(f"  Baseline footprint:")
    for i in range(n_s):
        dr = pert_t0[i][0] - ref_t0[i][0]
        dp = pert_t0[i][1] - ref_t0[i][1]
        dp = np.arctan2(np.sin(dp), np.cos(dp))
        baseline_rho.append(dr)
        baseline_phase.append(dp)
        print(f"    R={shell_radii_xi[i]}xi: "
              f"rho_rms={np.sqrt(np.mean(dr**2)):.3e}  "
              f"phase_mad={np.mean(np.abs(dp)):.3e}")

    # --- Perturbed run ---
    THRESH_A = thresh
    THRESH_P = thresh
    dt_acoustic = [None] * n_s
    dt_phase = [None] * n_s

    print(f"\n  PERTURBED RUN (max {total_steps} steps, thresh={THRESH_A})...")
    sys.stdout.flush()
    t_pert0 = time.time()
    final_step = total_steps

    for step in range(1, total_steps + 1):
        solver.rk4_step()

        if step in meas_set:
            pert_raw = solver.sample_shells()
            ref_raw = ref_data[step]
            t_cur = step * p.dt

            for i in range(n_s):
                drho = pert_raw[i][0] - ref_raw[i][0] - baseline_rho[i]
                sig_a = float(np.sqrt(np.mean(drho ** 2)))

                dphi = pert_raw[i][1] - ref_raw[i][1]
                dphi = np.arctan2(np.sin(dphi), np.cos(dphi))
                prop_phi = dphi - baseline_phase[i]
                prop_phi = np.arctan2(np.sin(prop_phi), np.cos(prop_phi))
                sig_p = float(np.mean(np.abs(prop_phi)))

                if dt_acoustic[i] is None and sig_a > THRESH_A:
                    dt_acoustic[i] = t_cur
                    t_exp = shell_radii[i] / p.cs
                    print(f"  ** ACOUSTIC R={shell_radii_xi[i]}xi: t={t_cur:.4f} "
                          f"(t/t_cs={t_cur/t_exp:.3f}) sig={sig_a:.3e}")
                if dt_phase[i] is None and sig_p > THRESH_P:
                    dt_phase[i] = t_cur
                    print(f"  ** PHASE    R={shell_radii_xi[i]}xi: t={t_cur:.4f} "
                          f"sig={sig_p:.3e}")

            if step % (meas_int * 20) == 0:
                el = time.time() - t_pert0
                print(f"    t={t_cur:.4f}  [{step/el:.1f} s/s]")
                sys.stdout.flush()

            if (all(d is not None for d in dt_acoustic) and
                    all(d is not None for d in dt_phase)):
                latest = max(max(dt_acoustic), max(dt_phase))
                if t_cur > latest * 2.0:
                    print(f"  Early exit at t={t_cur:.4f}")
                    final_step = step
                    break

    t_pert = time.time() - t_pert0

    # --- Analysis ---
    print(f"\n  {'='*60}")
    print(f"  RESULTS: {label}")
    print(f"  Signal = [pert(t)-ref(t)] - baseline")
    print(f"  {'='*60}")
    hdr = f"  {'Shell':>5}  {'R/xi':>5}  {'t_cs':>8}  {'t_acou':>8}  " \
          f"{'t_phase':>8}  {'v/c_s':>6}"
    print(hdr)
    print("  " + "-" * 55)
    for i in range(n_s):
        t_exp = shell_radii[i] / p.cs
        ta = f"{dt_acoustic[i]:.4f}" if dt_acoustic[i] else "---"
        tp = f"{dt_phase[i]:.4f}" if dt_phase[i] else "---"
        vs = f"{shell_radii[i]/dt_acoustic[i]/p.cs:.2f}" if dt_acoustic[i] else "---"
        print(f"  {i:>5}  {shell_radii_xi[i]:>5.1f}  {t_exp:>8.4f}  "
              f"{ta:>8}  {tp:>8}  {vs:>6}")

    det_a = [(shell_radii[i], dt_acoustic[i])
             for i in range(n_s) if dt_acoustic[i] is not None]
    c_fit = None
    if len(det_a) >= 2:
        Rs = np.array([r for r, _ in det_a])
        ts = np.array([t for _, t in det_a])
        slope = np.polyfit(Rs, ts, 1)[0]
        if slope > 0:
            c_fit = 1.0 / slope / p.cs
            print(f"\n  >> Fitted wavefront speed:  c_fit = {c_fit:.4f} c_s")
            print(f"  >> Bogoliubov prediction:   v_dom = {v_dom:.4f} c_s  "
                  f"v_edge = {v_edge:.4f} c_s")
        order_ok = all(ts[j] < ts[j + 1] for j in range(len(ts) - 1))
        print(f"  >> Inner->outer ordering:  {'CORRECT' if order_ok else 'VIOLATED'}")

    n_a = sum(1 for d in dt_acoustic if d is not None)
    n_p = sum(1 for d in dt_phase if d is not None)
    print(f"  >> Detected: acoustic {n_a}/{n_s}, phase {n_p}/{n_s}")
    print(f"  >> Ref: {t_ref:.1f}s  Pert: {t_pert:.1f}s  Total: {t_relax+t_ref+t_pert:.1f}s")
    sys.stdout.flush()

    results = {
        'label': label, 'perturb_width_xi': perturb_width_xi,
        'compact': compact,
        'shell_radii_xi': shell_radii_xi,
        'shell_radii': shell_radii,
        'dt_acoustic': dt_acoustic, 'dt_phase': dt_phase,
        'v_dom': v_dom, 'v_edge': v_edge,
        'c_fit': c_fit, 'p': p,
    }

    del solver
    cp.get_default_memory_pool().free_all_blocks()
    return results


# =================================================================
# Combined comparison
# =================================================================
def print_comparison(res_a, res_b):
    print(f"\n{'=' * 65}")
    print("  COMBINED VERDICT: IR vs UV BOGOLIUBOV DISPERSION")
    print(f"{'=' * 65}")

    for tag, res in [("IR", res_a), ("UV", res_b)]:
        if res is None:
            print(f"  {tag}: SKIPPED")
            continue
        c_s = f"{res['c_fit']:.4f}" if res['c_fit'] else "N/A"
        ct = "compact" if res['compact'] else "Gaussian"
        print(f"  {tag} ({ct}, width={res['perturb_width_xi']}xi): "
              f"c_fit = {c_s} c_s  "
              f"(v_dom={res['v_dom']:.4f}, v_edge={res['v_edge']:.4f})")

    if res_a is None or res_b is None:
        print("\n  >> One or both runs failed.")
        return

    ca = res_a['c_fit']
    cb = res_b['c_fit']
    if ca and cb:
        print(f"\n  >> SPEED RATIO:  UV / IR = {cb/ca:.2f}")
        print(f"  >> Predicted ratio (edge):  {res_b['v_edge']/res_a['v_edge']:.2f}")

        ir_causal = 0.5 < ca < 2.0
        uv_superluminal = cb > 3.0

        if ir_causal and uv_superluminal:
            print(f"\n  {'>'*55}")
            print(f"  >> DUAL-REGIME EMERGENCE CONFIRMED")
            print(f"  >>")
            print(f"  >> IR (Gaussian sigma={res_a['perturb_width_xi']}xi): "
                  f"c_fit = {ca:.4f} c_s")
            print(f"  >>   -> Phononic: Gaussian FT kills k > 1/xi")
            print(f"  >>   -> Wavefront respects acoustic light-cone")
            print(f"  >>   -> EMERGENT LORENTZ INVARIANCE (General Relativity)")
            print(f"  >>")
            print(f"  >> UV (compact R_cut={res_b['perturb_width_xi']}xi): "
                  f"c_fit = {cb:.4f} c_s")
            print(f"  >>   -> Free-particle: k^4 tail gives v_g >> c_s")
            print(f"  >>   -> Wavefront breaks acoustic light-cone")
            print(f"  >>   -> EMERGENT NON-LOCALITY (Quantum Mechanics)")
            print(f"  >>")
            print(f"  >> eps(k) = sqrt(c_s^2 k^2 + k^4/4) unifies both.")
            print(f"  >> Solver: RK4+FD4 (strictly local, no FFT)")
            print(f"  {'>'*55}")
        elif ir_causal and not uv_superluminal:
            print("\n  >> IR is causal but UV is also causal.")
        elif not ir_causal:
            print(f"\n  >> IR wavefront at {ca:.2f} c_s -- not phononic.")
    else:
        n_a_a = sum(1 for d in res_a['dt_acoustic'] if d is not None)
        n_a_b = sum(1 for d in res_b['dt_acoustic'] if d is not None)
        print(f"\n  >> Insufficient detections.")
        print(f"  >> IR: {n_a_a} shells, UV: {n_a_b} shells")


# =================================================================
# MAIN
# =================================================================
if __name__ == "__main__":
    dev = cp.cuda.Device(0)
    free, total = dev.mem_info
    max_N3 = int(free * 0.50 / (12 * 4))
    max_N = int(max_N3 ** (1.0 / 3.0))
    max_N = (max_N // 32) * 32
    max_N = min(max_N, 512)
    print(f"GPU 0: {free/1e9:.2f} GB free -> N={max_N}")

    xi = 1.0 / np.sqrt(2.0)
    print(f"\nBogoliubov: eps(k) = sqrt(k^2 + k^4/4),  xi={xi:.4f},  c_s=1.0")

    # ============================================================
    #  RUN A0: IR LIMIT - UNIFORM CONDENSATE (no trefoil)
    #  Gaussian sigma=15xi, void=0.02, dphi=0.0
    #  Pure acoustic test: no vortex, no phase kick
    #  PREDICTION: phononic wavefront at c_s
    # ============================================================
    res_A0 = run_experiment(
        label="RUN A0: IR UNIFORM (Gaussian sigma=15xi, no trefoil) -- Pure Acoustic",
        N=max_N, R_bnd_xi=100, perturb_width_xi=15.0, compact=False,
        shell_radii_xi=[75, 80, 85, 90, 100],
        n_relax=50, void=0.02, dphi=0.0, thresh=0.0005, uniform=True)

    # ============================================================
    #  RUN A0b: same but WITH phase kick dphi=0.02
    # ============================================================
    res_A0b = run_experiment(
        label="RUN A0b: IR UNIFORM+PHASE (Gaussian sigma=15xi, no trefoil, dphi=0.02)",
        N=max_N, R_bnd_xi=100, perturb_width_xi=15.0, compact=False,
        shell_radii_xi=[75, 80, 85, 90, 100],
        n_relax=50, void=0.02, dphi=0.02, thresh=0.0005, uniform=True)

    # ============================================================
    #  Quick summary
    # ============================================================
    print(f"\n{'=' * 65}")
    print("  UNIFORM CONDENSATE IR RESULTS")
    print(f"{'=' * 65}")
    for tag, res in [("UNIFORM void-only", res_A0),
                     ("UNIFORM void+phase", res_A0b)]:
        if res is None:
            print(f"  {tag}: SKIPPED")
            continue
        c_s = f"{res['c_fit']:.4f}" if res['c_fit'] else "N/A"
        print(f"  {tag}: c_fit = {c_s} c_s  "
              f"(void={res['p'].perturb_void}, dphi={res['p'].perturb_phase:.4f})")
    print(f"  Previous IR-WEAK w/ trefoil: c_fit = 3.2939 c_s")
    print(f"  Previous UV:                 c_fit = 10.7678 c_s")
