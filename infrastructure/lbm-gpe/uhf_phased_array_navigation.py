"""
UHF Phased Array Navigation — Directional Thrust via Perturbation Network
==========================================================================
Prove that a network of localized perturbation nodes ("Pyramids") on the
surface of a macroscopic GP defect (Mass E) can generate directional thrust
by phase-coordinated driving, acting as a phased array that steers the
Bjerknes force vector away from the Sun–Earth axis.

Solver: RK4 + FD4 Laplacian (strictly local, no FFT).
"""

import os, sys, time, math
import numpy as np

os.environ["LD_LIBRARY_PATH"] = "/usr/lib/wsl/lib"

try:
    import cupy as cp
except ImportError:
    print("FATAL: CuPy required.")
    sys.exit(1)

# ==================================================================
# 1. NATURAL UNITS
# ==================================================================
HBAR  = 1.0
M     = 1.0
G_INT = 1.0       # interaction coupling g
RHO0  = 1.0       # background density
MU    = G_INT * RHO0   # chemical potential μ = g·ρ₀
CS    = 1.0        # speed of sound  c_s = sqrt(g·ρ₀/m)
XI    = 1.0 / np.sqrt(2.0)   # healing length ξ = ℏ/sqrt(2·m·g·ρ₀)

# ==================================================================
# 2. GRID
# ==================================================================
N      = 256
DX     = 0.5 * XI                     # grid spacing = 0.5 ξ
DT     = 0.15 * DX**2                 # time step (CFL-safe for RK4+FD4)
L      = 0.5 * N * DX                 # half-box length
X0     = -L                            # grid origin
N_TOTAL = N**3

# ==================================================================
# 3. DEFECT GEOMETRY — "The Solar System"
# ==================================================================
R_S    = 20.0 * XI         # Mass S (Sun) ring radius
R_E    = 10.0 * XI         # Mass E (Earth) ring radius
X_S    = -30.0 * XI        # Sun x-position
Y_S    =   0.0             # Sun y-position
Z_S    =   0.0             # Sun z-position
X_E    = +30.0 * XI        # Earth x-position
Y_E    =   0.0             # Earth y-position
Z_E    =   0.0             # Earth z-position
SEP    = X_E - X_S         # separation = 60 ξ

# Rings oriented in the y-z plane → axis along x
RING_AXIS = 'x'

# ==================================================================
# 4. PHASED ARRAY NODES ("Pyramids")
# ==================================================================
# Three perturbation nodes on Mass E's surface in the x-y plane,
# arranged in a triangle with ~120° angular separation.
NODE_1 = (X_E - 10.0 * XI,  Y_E,              Z_E)   # Front (Sun-facing)
NODE_2 = (X_E +  5.0 * XI,  Y_E + 8.66 * XI,  Z_E)   # Top-side
NODE_3 = (X_E +  5.0 * XI,  Y_E - 8.66 * XI,  Z_E)   # Bottom-side
NODES  = [NODE_1, NODE_2, NODE_3]
N_NODES = len(NODES)

# Perturbation envelope and amplitude per node
NODE_SIGMA = 2.0 * XI      # Gaussian envelope width for each node
NODE_AMP   = 0.05           # phase amplitude of each node's drive

# ==================================================================
# 5. EVOLUTION COUNTS
# ==================================================================
N_RELAX     = 300           # post-imprint settling
N_BASELINE  = 2000          # undriven baseline measurement
N_DRIVEN    = 3000          # driven evolution per steering test
N_TRANSIENT = 500           # discard first 500 of N_DRIVEN
N_AVERAGE   = N_DRIVEN - N_TRANSIENT   # average over last 2500

# ==================================================================
# 6. MEASUREMENT PROBES
# ==================================================================
N_PROBE     = 256           # sample points on each measurement shell
R_PROBE_S   = 5.0 * XI     # shell radius inside Sun
R_PROBE_E   = 5.0 * XI     # shell radius inside Earth (5 xi)

# ==================================================================
# 7. DERIVED QUANTITIES
# ==================================================================
VRAM_BYTES  = N_TOTAL * 4 * 10   # ~10 float32 arrays
T_ACOUSTIC  = SEP / CS           # acoustic crossing time

print("=" * 65)
print("  UHF Phased Array Navigation — Configuration")
print("=" * 65)
print(f"  Natural units: ℏ=m=g=ρ₀=1, c_s={CS}, ξ={XI:.4f}")
print(f"  Grid:   N={N}³ = {N_TOTAL:,}  dx={DX:.5f}  dt={DT:.6f}")
print(f"  Box:    [{X0:.2f}, {X0 + N*DX:.2f}]³")
print(f"  VRAM:   ~{VRAM_BYTES / 1e9:.2f} GB")
print(f"\n  Mass S (Sun):   R={R_S:.3f} ({R_S/XI:.0f} ξ)"
      f"  at ({X_S:.3f}, {Y_S:.1f}, {Z_S:.1f})")
print(f"  Mass E (Earth): R={R_E:.3f} ({R_E/XI:.0f} ξ)"
      f"  at ({X_E:.3f}, {Y_E:.1f}, {Z_E:.1f})")
print(f"  Separation: {SEP:.3f} ({SEP/XI:.0f} ξ)")
print(f"  t_acoustic = {T_ACOUSTIC:.3f}")
print(f"\n  Phased Array: {N_NODES} nodes")
for i, nd in enumerate(NODES):
    print(f"    Node {i+1}: ({nd[0]/XI:+.1f}, {nd[1]/XI:+.1f},"
          f" {nd[2]/XI:+.1f}) ξ")
print(f"  Node σ={NODE_SIGMA:.4f} ({NODE_SIGMA/XI:.1f} ξ)"
      f"  amp={NODE_AMP}")
print(f"\n  Relax={N_RELAX}  Baseline={N_BASELINE}  "
      f"Driven={N_DRIVEN} (transient={N_TRANSIENT}, avg={N_AVERAGE})")
print("=" * 65)


# ==================================================================
# 8. PHASED ARRAY DRIVE CONSTANTS
# ==================================================================
DRIVE_OMEGA = 2.0 * CS / XI   # fixed drive frequency
DRIVE_AMP   = 0.1             # fixed amplitude (overrides NODE_AMP for array)

# ==================================================================
# 9. CUDA KERNELS
# ==================================================================
_kernel_cache = None


def get_kernels():
    """Load GP kernels from uhf_gp_kernels.cu + custom phased-array kernel."""
    global _kernel_cache
    if _kernel_cache is not None:
        return _kernel_cache

    # --- Standard GP kernels ---
    kp = os.path.join(os.path.dirname(__file__) or '.', 'uhf_gp_kernels.cu')
    with open(kp) as f:
        src = f.read()
    mod = cp.RawModule(code=src)

    # --- Custom 3-node phased array kernel ---
    # Each node applies a dipolar (y-shear) phase kick with independent
    # phase offset.  Total rotation = sum of 3 contributions.
    phased_kernel = cp.RawKernel(r'''
    extern "C" __global__
    void apply_phased_array(
        float* __restrict__ psi_re,
        float* __restrict__ psi_im,
        /* Node 1 center */  float n1x, float n1y, float n1z,
        /* Node 2 center */  float n2x, float n2y, float n2z,
        /* Node 3 center */  float n3x, float n3y, float n3z,
        float sigma,
        /* A_drive * dt * sin(omega*t + phi_i) for each node */
        float drive1, float drive2, float drive3,
        float x0, float y0, float z0,
        float dx,
        int Nx, int Ny, int Nz)
    {
        int idx = blockIdx.x * blockDim.x + threadIdx.x;
        int total = Nx * Ny * Nz;
        if (idx >= total) return;

        int NxNy = Nx * Ny;
        int iz = idx / NxNy;
        int rem = idx - iz * NxNy;
        int iy = rem / Nx;
        int ix = rem - iy * Nx;

        float x = x0 + ix * dx;
        float y = y0 + iy * dx;
        float z = z0 + iz * dx;

        float inv2s2 = 1.0f / (2.0f * sigma * sigma);

        // --- Node 1 ---
        float rx1 = x - n1x;
        float ry1 = y - n1y;
        float rz1 = z - n1z;
        float d_theta  = drive1 * ry1 * expf(-(rx1*rx1 + ry1*ry1 + rz1*rz1) * inv2s2);

        // --- Node 2 ---
        float rx2 = x - n2x;
        float ry2 = y - n2y;
        float rz2 = z - n2z;
        d_theta   += drive2 * ry2 * expf(-(rx2*rx2 + ry2*ry2 + rz2*rz2) * inv2s2);

        // --- Node 3 ---
        float rx3 = x - n3x;
        float ry3 = y - n3y;
        float rz3 = z - n3z;
        d_theta   += drive3 * ry3 * expf(-(rx3*rx3 + ry3*ry3 + rz3*rz3) * inv2s2);

        // Apply combined phase rotation
        float c = cosf(d_theta);
        float s = sinf(d_theta);

        float re = psi_re[idx];
        float im = psi_im[idx];
        psi_re[idx] = re * c - im * s;
        psi_im[idx] = re * s + im * c;
    }
    ''', 'apply_phased_array')

    _kernel_cache = type('K', (), {
        'gp_rhs_fd4':            mod.get_function('gp_rhs_fd4'),
        'compute_density':       mod.get_function('compute_density'),
        'compute_phase':         mod.get_function('compute_phase'),
        'sample_sphere':         mod.get_function('sample_sphere'),
        'imprint_trefoil_kernel': mod.get_function('imprint_trefoil_kernel'),
        'apply_phased_array':    phased_kernel,
    })()

    # Warm-up
    _w = cp.zeros(1024, dtype=cp.float32)
    _kernel_cache.compute_density((4,), (256,), (_w, _w, _w, np.int32(1024)))
    cp.cuda.Stream.null.synchronize()
    return _kernel_cache


# ==================================================================
# 10. VORTEX RING IMPRINTING
# ==================================================================
def make_ring_curve(cx, cy, cz, R, axis='x', N_pts=256):
    """Generate ring centerline points for imprint kernel."""
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


def imprint_single_ring(psi_re, psi_im, K, cx, cy, cz, R, axis='x'):
    """Imprint one vortex ring using the trefoil kernel."""
    N_curve = 256
    xs, ys, zs = make_ring_curve(cx, cy, cz, R, axis, N_curve)
    d_cx = cp.asarray(xs)
    d_cy = cp.asarray(ys)
    d_cz = cp.asarray(zs)
    block, grid = 256, (N_TOTAL + 255) // 256

    for attempt in range(3):
        psi_re[:] = 0
        psi_im[:] = 0
        cp.cuda.Stream.null.synchronize()
        K.imprint_trefoil_kernel(
            (grid,), (block,),
            (psi_re, psi_im, d_cx, d_cy, d_cz, np.int32(N_curve),
             np.float32(X0), np.float32(DX),
             np.int32(N), np.int32(N), np.int32(N),
             np.float32(XI), np.float32(RHO0)))
        cp.cuda.Stream.null.synchronize()
        if not bool(cp.any(cp.isnan(psi_re))):
            return True
    return False


# ==================================================================
# 11. SHELL PROBES (golden-spiral on sphere)
# ==================================================================
def make_shell_probes(x_center, y_center, z_center, R, N_pts=256):
    """Golden-spiral points uniformly distributed on a sphere."""
    idx = np.arange(N_pts, dtype=np.float32)
    golden = (1 + np.sqrt(5)) / 2
    theta = np.arccos(1 - 2 * (idx + 0.5) / N_pts)
    phi = 2 * np.pi * idx / golden
    px = (x_center + R * np.sin(theta) * np.cos(phi)).astype(np.float32)
    py = (y_center + R * np.sin(theta) * np.sin(phi)).astype(np.float32)
    pz = (z_center + R * np.cos(theta)).astype(np.float32)
    return px, py, pz


# ==================================================================
# 12. RK4 SOLVER CLASS
# ==================================================================
class PhasedArraySolver:
    """RK4+FD4 GP solver with state save/restore for phased-array sweeps."""

    def __init__(self):
        self.K = get_kernels()

        # Primary field arrays
        z = lambda: cp.zeros(N_TOTAL, dtype=cp.float32)
        self.psi_re = z()
        self.psi_im = z()
        self.rho    = z()
        self.phase  = z()

        # RK4 temporaries
        self.rhs_re = z()
        self.rhs_im = z()
        self.tmp_re = z()
        self.tmp_im = z()
        self.acc_re = z()
        self.acc_im = z()

        # Precomputed constant
        self.inv_12dx2 = np.float32(1.0 / (12.0 * DX**2))

        # Pristine state storage
        self._saved_re = None
        self._saved_im = None

        # Probe GPU arrays
        self.probes = {}

    # --- State management ---
    def save_pristine_state(self):
        """Snapshot the current field as the pristine baseline."""
        self._saved_re = self.psi_re.copy()
        self._saved_im = self.psi_im.copy()

    def restore_pristine_state(self):
        """Reset fields to the saved pristine snapshot."""
        self.psi_re[:] = self._saved_re
        self.psi_im[:] = self._saved_im
        cp.cuda.Stream.null.synchronize()

    # --- Imprinting ---
    def imprint_two_rings(self):
        """Imprint Sun and Earth via Abrikosov product ansatz."""
        print("  Imprinting Mass S (Sun)...")
        ok = imprint_single_ring(self.psi_re, self.psi_im, self.K,
                                 X_S, Y_S, Z_S, R_S, RING_AXIS)
        if not ok:
            print("  FATAL: NaN in Sun imprint")
            return False
        save_re = self.psi_re.copy()
        save_im = self.psi_im.copy()

        print("  Imprinting Mass E (Earth)...")
        ok = imprint_single_ring(self.psi_re, self.psi_im, self.K,
                                 X_E, Y_E, Z_E, R_E, RING_AXIS)
        if not ok:
            print("  FATAL: NaN in Earth imprint")
            return False

        # Abrikosov product
        inv_sqrt_rho0 = np.float32(1.0 / np.sqrt(RHO0))
        new_re = (save_re * self.psi_re - save_im * self.psi_im) * inv_sqrt_rho0
        new_im = (save_re * self.psi_im + save_im * self.psi_re) * inv_sqrt_rho0
        self.psi_re[:] = new_re
        self.psi_im[:] = new_im
        del save_re, save_im, new_re, new_im
        cp.get_default_memory_pool().free_all_blocks()

        # Normalize mean density
        g = (N_TOTAL + 255) // 256
        self.K.compute_density(
            (g,), (256,),
            (self.psi_re, self.psi_im, self.rho, np.int32(N_TOTAL)))
        cp.cuda.Stream.null.synchronize()
        m = float(cp.mean(self.rho))
        if m > 0:
            s = np.float32(np.sqrt(RHO0 / m))
            self.psi_re *= s
            self.psi_im *= s
        print(f"  Two-ring product: <rho>={m:.6f} -> normalized")
        return True

    # --- Probe setup ---
    def setup_probes(self):
        """Create GPU probe arrays for shells around S and E."""
        for label, (cx, cy, cz, R) in [
            ('S', (X_S, Y_S, Z_S, R_PROBE_S)),
            ('E', (X_E, Y_E, Z_E, R_PROBE_E)),
        ]:
            px, py, pz = make_shell_probes(cx, cy, cz, R, N_PROBE)
            self.probes[label] = {
                'px': cp.asarray(px),
                'py': cp.asarray(py),
                'pz': cp.asarray(pz),
                'rho_out':   cp.zeros(N_PROBE, dtype=cp.float32),
                'phase_out': cp.zeros(N_PROBE, dtype=cp.float32),
                'N': N_PROBE,
            }

    # --- RK4 step ---
    def compute_rhs(self, in_re, in_im, out_re, out_im):
        """Evaluate GP RHS:  dpsi/dt = -i[(-1/2 nabla^2 + g|psi|^2 - mu) psi]."""
        g = (N_TOTAL + 255) // 256
        self.K.gp_rhs_fd4(
            (g,), (256,),
            (in_re, in_im, out_re, out_im,
             np.float32(G_INT), np.float32(MU),
             self.inv_12dx2,
             np.int32(N), np.int32(N), np.int32(N)))

    def rk4_step(self):
        """Standard 4-stage RK4 integration."""
        dt = np.float32(DT)
        dt2 = np.float32(DT * 0.5)
        dt6 = np.float32(DT / 6.0)

        # k1
        self.compute_rhs(self.psi_re, self.psi_im, self.rhs_re, self.rhs_im)
        self.acc_re[:] = self.rhs_re
        self.acc_im[:] = self.rhs_im
        self.tmp_re[:] = self.psi_re + dt2 * self.rhs_re
        self.tmp_im[:] = self.psi_im + dt2 * self.rhs_im

        # k2
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += 2.0 * self.rhs_re
        self.acc_im += 2.0 * self.rhs_im
        self.tmp_re[:] = self.psi_re + dt2 * self.rhs_re
        self.tmp_im[:] = self.psi_im + dt2 * self.rhs_im

        # k3
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += 2.0 * self.rhs_re
        self.acc_im += 2.0 * self.rhs_im
        self.tmp_re[:] = self.psi_re + dt * self.rhs_re
        self.tmp_im[:] = self.psi_im + dt * self.rhs_im

        # k4
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += self.rhs_re
        self.acc_im += self.rhs_im

        self.psi_re += dt6 * self.acc_re
        self.psi_im += dt6 * self.acc_im

    # --- Observation ---
    def observe_E(self):
        """Sample density on Earth's probe shell, return (D_x, D_y)."""
        P = self.probes['E']
        g = (N_TOTAL + 255) // 256

        # Compute density on full grid
        self.K.compute_density(
            (g,), (256,),
            (self.psi_re, self.psi_im, self.rho, np.int32(N_TOTAL)))
        self.K.compute_phase(
            (g,), (256,),
            (self.psi_re, self.psi_im, self.phase, np.int32(N_TOTAL)))

        # Sample on shell
        gs = (P['N'] + 255) // 256
        self.K.sample_sphere(
            (gs,), (256,),
            (self.rho, self.phase,
             P['px'], P['py'], P['pz'],
             P['rho_out'], P['phase_out'],
             np.float32(X0), np.float32(X0), np.float32(X0),
             np.float32(1.0 / DX),
             np.int32(N), np.int32(N), np.int32(N),
             np.int32(P['N'])))
        cp.cuda.Stream.null.synchronize()

        rho_shell = P['rho_out']

        # Unit normals on the shell
        nx = (P['px'] - np.float32(X_E)) / np.float32(R_PROBE_E)
        ny = (P['py'] - np.float32(Y_E)) / np.float32(R_PROBE_E)

        # Density dipole components
        D_x = float(cp.mean(rho_shell * nx))
        D_y = float(cp.mean(rho_shell * ny))
        return D_x, D_y

    # --- Phased-array drive ---
    def apply_phased_drive(self, t_current, phi1, phi2, phi3):
        """Apply the 3-node phased array perturbation."""
        drive1 = np.float32(
            DRIVE_AMP * DT * math.sin(DRIVE_OMEGA * t_current + phi1))
        drive2 = np.float32(
            DRIVE_AMP * DT * math.sin(DRIVE_OMEGA * t_current + phi2))
        drive3 = np.float32(
            DRIVE_AMP * DT * math.sin(DRIVE_OMEGA * t_current + phi3))
        g = (N_TOTAL + 255) // 256
        self.K.apply_phased_array(
            (g,), (256,),
            (self.psi_re, self.psi_im,
             np.float32(NODE_1[0]), np.float32(NODE_1[1]), np.float32(NODE_1[2]),
             np.float32(NODE_2[0]), np.float32(NODE_2[1]), np.float32(NODE_2[2]),
             np.float32(NODE_3[0]), np.float32(NODE_3[1]), np.float32(NODE_3[2]),
             np.float32(NODE_SIGMA),
             drive1, drive2, drive3,
             np.float32(X0), np.float32(X0), np.float32(X0),
             np.float32(DX),
             np.int32(N), np.int32(N), np.int32(N)))


# ==================================================================
# 13. STEERING CONFIGURATIONS
# ==================================================================
STEER_CONFIGS = [
    ('Symmetric',    0.0,          0.0,            0.0),
    ('Steer Up',     0.0,          math.pi,        0.0),
    ('Steer Down',   0.0,          0.0,            math.pi),
    ('Vortex Spin',  0.0,          2*math.pi/3,    4*math.pi/3),
]


# ==================================================================
# 14. MAIN EXECUTION
# ==================================================================
def main():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    t0_wall = time.time()
    solver = PhasedArraySolver()

    # --- Imprint ---
    print("\n[1/4] Imprinting defects...")
    if not solver.imprint_two_rings():
        sys.exit(1)
    solver.setup_probes()

    # --- Relaxation ---
    print(f"[2/4] Relaxing ({N_RELAX} steps)...")
    for step in range(N_RELAX):
        solver.rk4_step()
    cp.cuda.Stream.null.synchronize()
    solver.save_pristine_state()
    print("  Pristine state saved.")

    # --- Baseline (no drive) ---
    N_BASE_RUN = 1500
    N_BASE_SKIP = 0
    print(f"[3/4] Baseline ({N_BASE_RUN} steps, no drive)...")
    Fx_sum = 0.0
    Fy_sum = 0.0
    n_samples = 0
    for step in range(N_BASE_RUN):
        solver.rk4_step()
        Dx, Dy = solver.observe_E()
        Fx_sum += Dx
        Fy_sum += Dy
        n_samples += 1

    Fx_baseline = Fx_sum / n_samples
    Fy_baseline = Fy_sum / n_samples
    angle_baseline = math.degrees(math.atan2(Fy_baseline, Fx_baseline))
    print(f"  Baseline: F=({Fx_baseline:+.6e}, {Fy_baseline:+.6e})")
    print(f"  Angle = {angle_baseline:+.2f} deg")

    # --- Sweep steering configs ---
    print(f"[4/4] Sweeping {len(STEER_CONFIGS)} phase configurations...")
    N_STEER_RUN = 2000
    N_STEER_TRANSIENT = 500
    results = []

    for i_cfg, (name, phi1, phi2, phi3) in enumerate(STEER_CONFIGS):
        solver.restore_pristine_state()
        t_sim = 0.0
        Fx_sum = 0.0
        Fy_sum = 0.0
        n_avg = 0

        for step in range(N_STEER_RUN):
            solver.apply_phased_drive(t_sim, phi1, phi2, phi3)
            solver.rk4_step()
            t_sim += DT

            if step >= N_STEER_TRANSIENT:
                Dx, Dy = solver.observe_E()
                Fx_sum += Dx
                Fy_sum += Dy
                n_avg += 1

        Fx = Fx_sum / max(n_avg, 1)
        Fy = Fy_sum / max(n_avg, 1)
        angle = math.degrees(math.atan2(Fy, Fx))
        steer = angle - angle_baseline
        results.append((name, phi1, phi2, phi3, Fx, Fy, angle, steer))
        print(f"  [{i_cfg+1}/{len(STEER_CONFIGS)}] {name:14s}  "
              f"F=({Fx:+.4e}, {Fy:+.4e})  "
              f"angle={angle:+.2f} deg  steer={steer:+.2f} deg")

    # --- Results ---
    print("\n" + "=" * 70)
    print("  PHASED ARRAY NAVIGATION — RESULTS")
    print("=" * 70)
    print(f"  {'Config':<16s} {'phi1':>6s} {'phi2':>6s} {'phi3':>6s}"
          f"  {'F_x':>11s} {'F_y':>11s} {'Angle':>8s} {'Steer':>8s}")
    print("-" * 70)
    print(f"  {'Baseline':<16s} {'--':>6s} {'--':>6s} {'--':>6s}"
          f"  {Fx_baseline:+11.4e} {Fy_baseline:+11.4e}"
          f" {angle_baseline:+8.2f} {0.0:+8.2f}")
    for name, p1, p2, p3, Fx, Fy, ang, steer in results:
        print(f"  {name:<16s} {p1:6.2f} {p2:6.2f} {p3:6.2f}"
              f"  {Fx:+11.4e} {Fy:+11.4e} {ang:+8.2f} {steer:+8.2f}")
    print("=" * 70)

    dt_wall = time.time() - t0_wall
    print(f"  Wall time: {dt_wall:.1f} s")

    # --- Plot: 2D force vectors ---
    fig, ax = plt.subplots(figsize=(8, 8))

    # Baseline vector
    ax.quiver(0, 0, Fx_baseline, Fy_baseline,
              angles='xy', scale_units='xy', scale=1,
              color='gray', linewidth=2, label='Baseline', zorder=3)

    colors = ['#2066a0', '#c04020', '#20a060', '#8040c0']
    for idx, (name, p1, p2, p3, Fx, Fy, ang, steer) in enumerate(results):
        ax.quiver(0, 0, Fx, Fy,
                  angles='xy', scale_units='xy', scale=1,
                  color=colors[idx % len(colors)], linewidth=2,
                  label=f'{name} ({steer:+.1f} deg)', zorder=4)

    # Formatting
    all_Fx = [Fx_baseline] + [r[4] for r in results]
    all_Fy = [Fy_baseline] + [r[5] for r in results]
    mag_max = max(math.hypot(fx, fy) for fx, fy in zip(all_Fx, all_Fy))
    pad = mag_max * 1.5 if mag_max > 0 else 0.01
    ax.set_xlim(-pad, pad)
    ax.set_ylim(-pad, pad)
    ax.set_aspect('equal')
    ax.axhline(0, color='gray', ls=':', alpha=0.3)
    ax.axvline(0, color='gray', ls=':', alpha=0.3)
    ax.set_xlabel('F_x  (Sun-Earth axis)')
    ax.set_ylabel('F_y  (Lateral thrust)')
    ax.set_title('UHF Phased Array Navigation\nForce Vectors by Phase Config')
    ax.legend(loc='upper left', fontsize=9)
    ax.grid(True, alpha=0.2)

    out_path = os.path.join(os.path.dirname(__file__) or '.',
                            'uhf_phased_array_navigation.png')
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    print(f"  Plot saved: {out_path}")
    plt.close()


if __name__ == '__main__':
    main()
