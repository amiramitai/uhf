"""
UHF Hypervisor Resonance — Kuramoto Decoupling Sweep
=====================================================
Sweep a transverse shear perturbation frequency across a GP fluid
containing two vortex-ring defects (Mass A, Mass B) to find the
resonant frequency ω* that inverts the Bjerknes force and/or
alters the local topological clock rate of Mass B.

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
# 3. DEFECT GEOMETRY
# ==================================================================
R_A    = 50.0 * XI         # Mass A ring radius (large)
R_B    =  5.0 * XI         # Mass B ring radius (small)
X_A    = -20.0 * XI        # Mass A x-position
X_B    = +20.0 * XI        # Mass B x-position
SEP    = X_B - X_A         # separation = 40 ξ

# Rings oriented in the y-z plane → axis along x
RING_AXIS = 'x'

# ==================================================================
# 4. SWEEP PARAMETERS
# ==================================================================
N_FREQ      = 30                              # frequency steps
OMEGA_MIN   = 0.3 * CS / XI                   # lower bound
OMEGA_MAX   = 10.0 * CS / XI                  # upper bound
OMEGA_SWEEP = np.logspace(
    np.log10(OMEGA_MIN),
    np.log10(OMEGA_MAX),
    N_FREQ
)

# ==================================================================
# 5. EVOLUTION COUNTS
# ==================================================================
N_RELAX     = 300          # initial relaxation (post-imprint settling)
N_BASELINE  = 2000         # undriven baseline measurement
N_DRIVEN    = 2000         # driven evolution per frequency step
N_TRANSIENT = 500          # discard first 500 of N_DRIVEN
N_AVERAGE   = N_DRIVEN - N_TRANSIENT   # average over last 1500

# ==================================================================
# 6. PERTURBATION GEOMETRY
# ==================================================================
# Transverse shear applied strictly at Mass B location
DRIVE_SIGMA  = 2.0 * XI    # Gaussian envelope width (localized to B)
DRIVE_AMP    = 0.05         # phase amplitude of shear perturbation

# ==================================================================
# 7. MEASUREMENT PROBES
# ==================================================================
N_PROBE     = 256          # sample points on each measurement shell
R_PROBE_A   = 5.0 * XI    # shell radius around A for force measurement
R_PROBE_B   = 3.0 * XI    # shell radius around B for clock measurement

# ==================================================================
# 8. DERIVED QUANTITIES (for reference printout)
# ==================================================================
VRAM_BYTES  = N_TOTAL * 4 * 10   # ~10 float32 arrays
T_ACOUSTIC  = SEP / CS            # acoustic crossing time

print("=" * 65)
print("  UHF Hypervisor Resonance — Configuration")
print("=" * 65)
print(f"  Natural units: ℏ=m=g=ρ₀=1, c_s={CS}, ξ={XI:.4f}")
print(f"  Grid:   N={N}³ = {N_TOTAL:,}  dx={DX:.5f}  dt={DT:.6f}")
print(f"  Box:    [{X0:.2f}, {X0 + N*DX:.2f}]³")
print(f"  VRAM:   ~{VRAM_BYTES / 1e9:.2f} GB")
print(f"\n  Defect A: R={R_A:.3f} ({R_A/XI:.0f} ξ)  at x={X_A:.3f}")
print(f"  Defect B: R={R_B:.3f} ({R_B/XI:.0f} ξ)  at x={X_B:.3f}")
print(f"  Separation: {SEP:.3f} ({SEP/XI:.0f} ξ)")
print(f"  t_acoustic = {T_ACOUSTIC:.3f}")
print(f"\n  Sweep: {N_FREQ} frequencies  [{OMEGA_MIN:.3f}, {OMEGA_MAX:.3f}] c_s/ξ")
print(f"  Relax={N_RELAX}  Baseline={N_BASELINE}  "
      f"Driven={N_DRIVEN} (transient={N_TRANSIENT}, avg={N_AVERAGE})")
print(f"  Drive: σ={DRIVE_SIGMA:.4f} ({DRIVE_SIGMA/XI:.1f} ξ)  "
      f"amp={DRIVE_AMP}")
print("=" * 65)


# ==================================================================
# 9. CUDA KERNELS
# ==================================================================
_kernel_cache = None


def get_kernels():
    """Load GP kernels from uhf_gp_kernels.cu + custom shear kernel."""
    global _kernel_cache
    if _kernel_cache is not None:
        return _kernel_cache

    # --- Standard GP kernels ---
    kp = os.path.join(os.path.dirname(__file__) or '.', 'uhf_gp_kernels.cu')
    with open(kp) as f:
        src = f.read()
    mod = cp.RawModule(code=src)

    # --- Custom dipolar transverse shear kernel ---
    shear_kernel = cp.RawKernel(r'''
    extern "C" __global__
    void apply_transverse_shear(
        float* __restrict__ psi_re,
        float* __restrict__ psi_im,
        float cx, float cy, float cz,
        float sigma,
        float drive_term,          // A_drive * dt * sin(omega * t)
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

        float rx = x - cx;
        float ry = y - cy;
        float rz = z - cz;
        float r2 = rx * rx + ry * ry + rz * rz;
        float sigma2 = sigma * sigma;

        float envelope = expf(-r2 / (2.0f * sigma2));

        // Dipolar phase kick: transverse (y-direction) shear
        //   d_theta = drive_term * (y - cy) * envelope
        float d_theta = drive_term * ry * envelope;

        float c = cosf(d_theta);
        float s = sinf(d_theta);

        float re = psi_re[idx];
        float im = psi_im[idx];
        psi_re[idx] = re * c - im * s;
        psi_im[idx] = re * s + im * c;
    }
    ''', 'apply_transverse_shear')

    _kernel_cache = type('K', (), {
        'gp_rhs_fd4':           mod.get_function('gp_rhs_fd4'),
        'compute_density':      mod.get_function('compute_density'),
        'compute_phase':        mod.get_function('compute_phase'),
        'sample_sphere':        mod.get_function('sample_sphere'),
        'imprint_trefoil_kernel': mod.get_function('imprint_trefoil_kernel'),
        'apply_transverse_shear': shear_kernel,
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
    """Imprint one vortex ring using the trefoil kernel (single-loop = ring)."""
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
class ResonanceSolver:
    """RK4+FD4 GP solver with state save/restore for frequency sweeps."""

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

        # Pristine state storage (set after relaxation)
        self._saved_re = None
        self._saved_im = None

        # Probe GPU arrays (set up by setup_probes)
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
        """Imprint Mass A and Mass B via Abrikosov product ansatz."""
        print("  Imprinting Mass A (large ring)...")
        ok = imprint_single_ring(self.psi_re, self.psi_im, self.K,
                                 X_A, 0.0, 0.0, R_A, RING_AXIS)
        if not ok:
            print("  FATAL: NaN in ring A imprint")
            return False
        save_re = self.psi_re.copy()
        save_im = self.psi_im.copy()

        print("  Imprinting Mass B (small ring)...")
        ok = imprint_single_ring(self.psi_re, self.psi_im, self.K,
                                 X_B, 0.0, 0.0, R_B, RING_AXIS)
        if not ok:
            print("  FATAL: NaN in ring B imprint")
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
        print(f"  Two-ring product: <ρ>={m:.6f} → normalized")
        return True

    # --- Probe setup ---
    def setup_probes(self):
        """Create GPU probe arrays for shells around A and B."""
        for label, (cx, cy, cz, R) in [
            ('A', (X_A, 0.0, 0.0, R_PROBE_A)),
            ('B', (X_B, 0.0, 0.0, R_PROBE_B)),
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
        """Evaluate GP RHS:  dψ/dt = -i[(-½∇² + g|ψ|² − μ)ψ]."""
        g = (N_TOTAL + 255) // 256
        self.K.gp_rhs_fd4(
            (g,), (256,),
            (in_re, in_im, out_re, out_im,
             np.float32(G_INT), np.float32(MU),
             self.inv_12dx2,
             np.int32(N), np.int32(N), np.int32(N)))

    def rk4_step(self):
        """Standard 4-stage RK4 integration of the GP equation."""
        dt = np.float32(DT)
        dt2 = np.float32(DT * 0.5)
        dt6 = np.float32(DT / 6.0)

        # k1 = f(psi)
        self.compute_rhs(self.psi_re, self.psi_im, self.rhs_re, self.rhs_im)
        self.acc_re[:] = self.rhs_re
        self.acc_im[:] = self.rhs_im
        self.tmp_re[:] = self.psi_re + dt2 * self.rhs_re
        self.tmp_im[:] = self.psi_im + dt2 * self.rhs_im

        # k2 = f(psi + dt/2 * k1)
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += 2.0 * self.rhs_re
        self.acc_im += 2.0 * self.rhs_im
        self.tmp_re[:] = self.psi_re + dt2 * self.rhs_re
        self.tmp_im[:] = self.psi_im + dt2 * self.rhs_im

        # k3 = f(psi + dt/2 * k2)
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += 2.0 * self.rhs_re
        self.acc_im += 2.0 * self.rhs_im
        self.tmp_re[:] = self.psi_re + dt * self.rhs_re
        self.tmp_im[:] = self.psi_im + dt * self.rhs_im

        # k4 = f(psi + dt * k3)
        self.compute_rhs(self.tmp_re, self.tmp_im, self.rhs_re, self.rhs_im)
        self.acc_re += self.rhs_re
        self.acc_im += self.rhs_im

        # psi += dt/6 * (k1 + 2k2 + 2k3 + k4)
        self.psi_re += dt6 * self.acc_re
        self.psi_im += dt6 * self.acc_im

    # --- Observation ---
    def observe(self, label):
        """Sample density and phase on shell `label`.

        Returns (rho_array, phase_array) as CuPy device arrays.
        """
        P = self.probes[label]
        g = (N_TOTAL + 255) // 256

        # Compute density & phase on full grid (shared across probes)
        self.K.compute_density(
            (g,), (256,),
            (self.psi_re, self.psi_im, self.rho, np.int32(N_TOTAL)))
        self.K.compute_phase(
            (g,), (256,),
            (self.psi_re, self.psi_im, self.phase, np.int32(N_TOTAL)))

        # Trilinear-interpolated sample on the shell
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
        return P['rho_out'], P['phase_out']

    def measure_B(self, prev_phase_B):
        """Measure Bjerknes-proxy dipole D_x and clock rate on shell B.

        Returns (D_x, mean_phase, d_theta_dt).
        """
        rho_B, phase_B = self.observe('B')
        P = self.probes['B']

        # Unit normals nˆx = (x_i - x_B) / R_PROBE_B
        nx = (P['px'] - np.float32(X_B)) / np.float32(R_PROBE_B)

        # Density dipole D_x = <ρ_i · nx_i>  (Bjerknes force proxy)
        D_x = float(cp.mean(rho_B * nx))

        # Circular mean of phase on shell
        sin_mean = float(cp.mean(cp.sin(phase_B)))
        cos_mean = float(cp.mean(cp.cos(phase_B)))
        mean_phase = math.atan2(sin_mean, cos_mean)

        # Clock rate dθ/dt (unwrapped)
        if prev_phase_B is not None:
            dp = mean_phase - prev_phase_B
            # Unwrap
            if dp > math.pi:
                dp -= 2 * math.pi
            elif dp < -math.pi:
                dp += 2 * math.pi
            d_theta_dt = dp / DT
        else:
            d_theta_dt = 0.0

        return D_x, mean_phase, d_theta_dt

    # --- Shear drive ---
    def apply_shear(self, t_current, omega_ext):
        """Apply dipolar transverse shear perturbation at Mass B."""
        drive_term = np.float32(
            DRIVE_AMP * DT * math.sin(omega_ext * t_current))
        g = (N_TOTAL + 255) // 256
        self.K.apply_transverse_shear(
            (g,), (256,),
            (self.psi_re, self.psi_im,
             np.float32(X_B), np.float32(0.0), np.float32(0.0),
             np.float32(DRIVE_SIGMA),
             drive_term,
             np.float32(X0), np.float32(X0), np.float32(X0),
             np.float32(DX),
             np.int32(N), np.int32(N), np.int32(N)))


# ==================================================================
# 13. MAIN EXECUTION
# ==================================================================
def main():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    t0_wall = time.time()
    solver = ResonanceSolver()

    # --- Imprint two vortex rings ---
    print("\n[1/5] Imprinting defects...")
    if not solver.imprint_two_rings():
        sys.exit(1)
    solver.setup_probes()

    # --- Relaxation ---
    print(f"[2/5] Relaxing ({N_RELAX} steps)...")
    for step in range(N_RELAX):
        solver.rk4_step()
    cp.cuda.Stream.null.synchronize()
    solver.save_pristine_state()
    print("  Pristine state saved.")

    # --- Baseline measurement (undriven) ---
    print(f"[3/5] Baseline ({N_BASELINE} steps)...")
    F_sum = 0.0
    clock_sum = 0.0
    prev_phase = None
    n_samples = 0
    for step in range(N_BASELINE):
        solver.rk4_step()
        D_x, mean_ph, dth_dt = solver.measure_B(prev_phase)
        prev_phase = mean_ph
        if step > 0:  # skip first (no dθ/dt yet)
            F_sum += D_x
            clock_sum += dth_dt
            n_samples += 1

    F_baseline = F_sum / max(n_samples, 1)
    clock_baseline = clock_sum / max(n_samples, 1)
    print(f"  F_baseline  = {F_baseline:+.6e}")
    print(f"  ω_clock_baseline = {clock_baseline:+.6e} rad/dt")

    # --- Frequency sweep ---
    print(f"[4/5] Sweeping {N_FREQ} frequencies...")
    F_net_arr = np.zeros(N_FREQ)
    clock_arr = np.zeros(N_FREQ)

    for i_freq, omega_ext in enumerate(OMEGA_SWEEP):
        solver.restore_pristine_state()
        t_sim = 0.0
        prev_phase = None
        F_sum = 0.0
        clock_sum = 0.0
        n_avg = 0

        for step in range(N_DRIVEN):
            solver.apply_shear(t_sim, omega_ext)
            solver.rk4_step()
            t_sim += DT

            D_x, mean_ph, dth_dt = solver.measure_B(prev_phase)
            prev_phase = mean_ph

            # Discard transient
            if step >= N_TRANSIENT and step > 0:
                F_sum += D_x
                clock_sum += dth_dt
                n_avg += 1

        F_net_arr[i_freq] = F_sum / max(n_avg, 1)
        clock_arr[i_freq] = clock_sum / max(n_avg, 1)

        status = "NEG" if F_net_arr[i_freq] < 0 else "pos"
        print(f"  [{i_freq+1:2d}/{N_FREQ}] ω={omega_ext:8.4f}  "
              f"F={F_net_arr[i_freq]:+.4e}  "
              f"clock={clock_arr[i_freq]:+.4e}  [{status}]")

    # --- Analysis ---
    print("\n" + "=" * 65)
    print("  RESULTS")
    print("=" * 65)

    # Find ω* where F_net crosses zero or goes most negative
    zero_crossings = []
    for i in range(len(F_net_arr) - 1):
        if F_net_arr[i] * F_net_arr[i + 1] < 0:
            # Linear interpolation for crossing
            w1, w2 = OMEGA_SWEEP[i], OMEGA_SWEEP[i + 1]
            f1, f2 = F_net_arr[i], F_net_arr[i + 1]
            omega_star = w1 - f1 * (w2 - w1) / (f2 - f1)
            # Interpolate clock rate at crossing
            frac = (omega_star - w1) / (w2 - w1)
            clock_star = clock_arr[i] + frac * (clock_arr[i + 1] - clock_arr[i])
            zero_crossings.append((omega_star, clock_star))

    i_min = int(np.argmin(F_net_arr))
    omega_min = OMEGA_SWEEP[i_min]

    if zero_crossings:
        for j, (ws, cs) in enumerate(zero_crossings):
            print(f"  Zero-crossing #{j+1}: ω* = {ws:.6f} c_s/ξ")
            print(f"    Clock rate at ω* = {cs:+.6e} rad/dt")
            print(f"    Tesla Time-Metric: τ*/τ_baseline = "
                  f"{cs / clock_baseline:.6f}"
                  if abs(clock_baseline) > 1e-15
                  else "    Tesla Time-Metric: baseline ≈ 0 (undetermined)")
    else:
        print(f"  No zero-crossing found in sweep range.")
        print(f"  Most negative F at ω = {omega_min:.6f} c_s/ξ  "
              f"F = {F_net_arr[i_min]:+.4e}")
        print(f"  Clock rate there = {clock_arr[i_min]:+.6e} rad/dt")

    print(f"\n  Baseline: F={F_baseline:+.4e}  clock={clock_baseline:+.4e}")
    dt_wall = time.time() - t0_wall
    print(f"  Wall time: {dt_wall:.1f} s")
    print("=" * 65)

    # --- Plot ---
    fig, ax1 = plt.subplots(figsize=(10, 6))
    ax1.set_xscale('log')
    color1 = '#2066a0'
    ax1.set_xlabel('ω_ext  [c_s / ξ]')
    ax1.set_ylabel('F_net  (Bjerknes dipole D_x)', color=color1)
    ax1.plot(OMEGA_SWEEP, F_net_arr, 'o-', color=color1, markersize=4,
             label='F_net')
    ax1.axhline(F_baseline, color=color1, ls='--', alpha=0.5,
                label=f'F_baseline={F_baseline:.2e}')
    ax1.axhline(0, color='gray', ls=':', alpha=0.4)
    for ws, _ in zero_crossings:
        ax1.axvline(ws, color='red', ls='--', alpha=0.7, label=f'ω*={ws:.3f}')
    ax1.tick_params(axis='y', labelcolor=color1)
    ax1.legend(loc='upper left', fontsize=8)

    ax2 = ax1.twinx()
    color2 = '#c04020'
    ax2.set_ylabel('dθ_B/dt  (clock rate)', color=color2)
    ax2.plot(OMEGA_SWEEP, clock_arr, 's-', color=color2, markersize=4,
             label='clock rate')
    ax2.axhline(clock_baseline, color=color2, ls='--', alpha=0.5,
                label=f'ω_clock_baseline={clock_baseline:.2e}')
    ax2.tick_params(axis='y', labelcolor=color2)
    ax2.legend(loc='upper right', fontsize=8)

    plt.title('UHF Hypervisor Resonance — Kuramoto Decoupling Sweep')
    plt.tight_layout()
    out_path = os.path.join(os.path.dirname(__file__) or '.',
                            'uhf_hypervisor_resonance.png')
    plt.savefig(out_path, dpi=150)
    print(f"  Plot saved: {out_path}")
    plt.close()


if __name__ == '__main__':
    main()
