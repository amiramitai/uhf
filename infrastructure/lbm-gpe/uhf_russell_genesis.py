"""
UHF Russell Genesis — Twin Opposing Vortex Annihilation / Phase-Lock
=====================================================================
Test the "Twin Opposing Vortices" genesis hypothesis: do two
anti-parallel vortex rings with a slight impact parameter undergo
complete annihilation, or phase-lock into a stable topological
defect (knot or breather)?

Ring 1 (+1 charge): centripetal, normal along +x.
Ring 2 (-1 charge): centrifugal, normal along -x, offset in y-z.

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
MU    = G_INT * RHO0   # chemical potential mu = g * rho_0
CS    = 1.0        # speed of sound
XI    = 1.0 / np.sqrt(2.0)   # healing length

# ==================================================================
# 2. GRID
# ==================================================================
N      = 256
DX     = 0.5 * XI
DT     = 0.15 * DX**2
L      = 0.5 * N * DX
X0     = -L
N_TOTAL = N**3

# ==================================================================
# 3. DEFECT GEOMETRY — "The Twin Spirals"
# ==================================================================
R_RING  = 15.0 * XI        # both rings share the same radius

# Ring 1 (centripetal, +1 charge): normal along +x
X_R1    = -20.0 * XI
Y_R1    =   0.0
Z_R1    =   0.0
CHARGE_R1 = +1             # topological charge (circulation sign)

# Ring 2 (centrifugal, -1 charge): normal along -x, Russell offset
X_R2    = +20.0 * XI
Y_R2    =   5.0 * XI       # Russell offset in y
Z_R2    =   5.0 * XI       # Russell offset in z
CHARGE_R2 = -1             # opposite circulation

# Separation along x-axis
SEP_X   = X_R2 - X_R1      # 40 xi

# Impact parameter (transverse offset)
B_IMPACT = np.sqrt(Y_R2**2 + Z_R2**2)   # 5*sqrt(2) xi ~ 7.07 xi

# Ring axis: both lie in the y-z plane with normal along x
RING_AXIS = 'x'

# ==================================================================
# 4. EVOLUTION COUNTS
# ==================================================================
N_RELAX     = 200           # post-imprint settling (short, rings are moving)
N_EVOLVE    = 5000          # main evolution to observe collision dynamics
N_SNAPSHOT  = 100           # save observables every N_SNAPSHOT steps

# ==================================================================
# 5. MEASUREMENT PROBES
# ==================================================================
N_PROBE     = 256           # sample points on each measurement shell
R_PROBE     = 5.0 * XI     # probe shell radius (inside each ring)

# ==================================================================
# 6. DERIVED QUANTITIES
# ==================================================================
VRAM_BYTES  = N_TOTAL * 4 * 10
T_ACOUSTIC  = SEP_X / CS
T_COLLISION = SEP_X / (2.0 * CS)   # rough estimate: rings approach at ~c_s

print("=" * 65)
print("  UHF Russell Genesis — Configuration")
print("=" * 65)
print(f"  Natural units: hbar=m=g=rho_0=1, c_s={CS}, xi={XI:.4f}")
print(f"  Grid:   N={N}^3 = {N_TOTAL:,}  dx={DX:.5f}  dt={DT:.6f}")
print(f"  Box:    [{X0:.2f}, {X0 + N*DX:.2f}]^3")
print(f"  VRAM:   ~{VRAM_BYTES / 1e9:.2f} GB")
print(f"\n  Ring 1 (+1): R={R_RING:.3f} ({R_RING/XI:.0f} xi)"
      f"  at ({X_R1/XI:+.0f}, {Y_R1/XI:+.0f}, {Z_R1/XI:+.0f}) xi"
      f"  charge={CHARGE_R1:+d}")
print(f"  Ring 2 (-1): R={R_RING:.3f} ({R_RING/XI:.0f} xi)"
      f"  at ({X_R2/XI:+.0f}, {Y_R2/XI:+.0f}, {Z_R2/XI:+.0f}) xi"
      f"  charge={CHARGE_R2:+d}")
print(f"  Axial separation: {SEP_X:.3f} ({SEP_X/XI:.0f} xi)")
print(f"  Impact parameter: {B_IMPACT:.3f} ({B_IMPACT/XI:.2f} xi)")
print(f"  t_acoustic = {T_ACOUSTIC:.3f}  t_collision ~ {T_COLLISION:.3f}")
print(f"\n  Relax={N_RELAX}  Evolve={N_EVOLVE}  "
      f"Snapshot every {N_SNAPSHOT} steps")
print("=" * 65)


# ==================================================================
# 7. CUDA KERNELS
# ==================================================================
_kernel_cache = None


def get_kernels():
    """Load GP kernels from uhf_gp_kernels.cu."""
    global _kernel_cache
    if _kernel_cache is not None:
        return _kernel_cache

    kp = os.path.join(os.path.dirname(__file__) or '.', 'uhf_gp_kernels.cu')
    with open(kp) as f:
        src = f.read()
    mod = cp.RawModule(code=src)

    _kernel_cache = type('K', (), {
        'gp_rhs_fd4':            mod.get_function('gp_rhs_fd4'),
        'compute_density':       mod.get_function('compute_density'),
        'compute_phase':         mod.get_function('compute_phase'),
        'sample_sphere':         mod.get_function('sample_sphere'),
        'imprint_trefoil_kernel': mod.get_function('imprint_trefoil_kernel'),
    })()

    # Warm-up
    _w = cp.zeros(1024, dtype=cp.float32)
    _kernel_cache.compute_density((4,), (256,), (_w, _w, _w, np.int32(1024)))
    cp.cuda.Stream.null.synchronize()
    return _kernel_cache


# ==================================================================
# 8. VORTEX RING IMPRINTING
# ==================================================================
def make_ring_curve(cx, cy, cz, R, axis='x', charge=+1, N_pts=256):
    """Generate ring centerline points. charge=-1 reverses winding."""
    t = np.linspace(0, 2 * np.pi, N_pts, endpoint=False).astype(np.float32)
    if charge < 0:
        t = t[::-1].copy()   # reverse winding for opposite circulation
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


def imprint_single_ring(psi_re, psi_im, K, cx, cy, cz, R,
                        axis='x', charge=+1):
    """Imprint one vortex ring with given charge."""
    N_curve = 256
    xs, ys, zs = make_ring_curve(cx, cy, cz, R, axis, charge, N_curve)
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
# 9. RK4 SOLVER CLASS
# ==================================================================
class GenesisSolver:
    """RK4+FD4 GP solver for unforced twin-vortex evolution."""

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

    # --- RK4 step ---
    def compute_rhs(self, in_re, in_im, out_re, out_im):
        g = (N_TOTAL + 255) // 256
        self.K.gp_rhs_fd4(
            (g,), (256,),
            (in_re, in_im, out_re, out_im,
             np.float32(G_INT), np.float32(MU),
             self.inv_12dx2,
             np.int32(N), np.int32(N), np.int32(N)))

    def rk4_step(self):
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

    # --- Imprinting ---
    def imprint_twin_rings(self):
        """Imprint Ring 1 (+1) and Ring 2 (-1) via Abrikosov product."""
        print("  Imprinting Ring 1 (+1 charge)...")
        ok = imprint_single_ring(self.psi_re, self.psi_im, self.K,
                                 X_R1, Y_R1, Z_R1, R_RING,
                                 RING_AXIS, CHARGE_R1)
        if not ok:
            print("  FATAL: NaN in Ring 1 imprint")
            return False
        save_re = self.psi_re.copy()
        save_im = self.psi_im.copy()

        print("  Imprinting Ring 2 (-1 charge)...")
        ok = imprint_single_ring(self.psi_re, self.psi_im, self.K,
                                 X_R2, Y_R2, Z_R2, R_RING,
                                 RING_AXIS, CHARGE_R2)
        if not ok:
            print("  FATAL: NaN in Ring 2 imprint")
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
        print(f"  Twin-ring product: <rho>={m:.6f} -> normalized")
        return True

    # --- Measurement ---
    def observe(self):
        """Compute topology-tracking observables on the current field.

        Returns dict with:
          max_depletion : rho_0 - min(rho)  (0 = annihilated, >0 = defect lives)
          E_acoustic    : kinetic energy of density waves (compressible part)
          cm_x, cm_y, cm_z : center-of-mass of the defect (location of min rho)
          min_rho       : raw minimum density
        """
        g = (N_TOTAL + 255) // 256

        # Density
        self.K.compute_density(
            (g,), (256,),
            (self.psi_re, self.psi_im, self.rho, np.int32(N_TOTAL)))
        cp.cuda.Stream.null.synchronize()

        # --- Metric 1: Maximum density depletion ---
        min_rho = float(cp.min(self.rho))
        max_depletion = RHO0 - min_rho

        # --- Metric 3: Center of mass of defect ---
        # Location of global density minimum
        idx_min = int(cp.argmin(self.rho))
        iz = idx_min // (N * N)
        rem = idx_min - iz * N * N
        iy = rem // N
        ix = rem - iy * N
        cm_x = X0 + ix * DX
        cm_y = X0 + iy * DX
        cm_z = X0 + iz * DX

        # --- Metric 2: Acoustic (compressible kinetic) energy ---
        # E_kin = (hbar^2 / 2m) * integral |grad(sqrt(rho))|^2
        # In natural units: E_kin = 0.5 * sum |grad(sqrt(rho))|^2 * dx^3
        sqrt_rho = cp.sqrt(cp.maximum(self.rho, 1e-12))
        sqrt_rho_3d = sqrt_rho.reshape(N, N, N)

        # Central differences for gradient of sqrt(rho)
        gx = (cp.roll(sqrt_rho_3d, -1, 0) - cp.roll(sqrt_rho_3d, 1, 0)) \
             / (2.0 * DX)
        gy = (cp.roll(sqrt_rho_3d, -1, 1) - cp.roll(sqrt_rho_3d, 1, 1)) \
             / (2.0 * DX)
        gz = (cp.roll(sqrt_rho_3d, -1, 2) - cp.roll(sqrt_rho_3d, 1, 2)) \
             / (2.0 * DX)
        E_acoustic = 0.5 * float(cp.sum(gx**2 + gy**2 + gz**2)) * DX**3

        return {
            'max_depletion': max_depletion,
            'min_rho':       min_rho,
            'E_acoustic':    E_acoustic,
            'cm_x':          cm_x,
            'cm_y':          cm_y,
            'cm_z':          cm_z,
        }


# ==================================================================
# 10. MAIN EXECUTION
# ==================================================================
def main():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

    N_STEPS     = 4000
    SAMPLE_EVERY = 20

    t0_wall = time.time()
    solver = GenesisSolver()

    # --- Imprint ---
    print("\n[1/3] Imprinting twin opposing vortex rings...")
    if not solver.imprint_twin_rings():
        sys.exit(1)

    # --- Relaxation ---
    print(f"[2/3] Relaxing ({N_RELAX} steps)...")
    for step in range(N_RELAX):
        solver.rk4_step()
    cp.cuda.Stream.null.synchronize()
    print("  Relaxation complete.")

    # --- Main evolution ---
    print(f"[3/3] Evolving {N_STEPS} steps (sample every {SAMPLE_EVERY})...")
    times      = []
    depletion  = []
    E_acoustic = []
    traj_x     = []
    traj_y     = []
    traj_z     = []

    for step in range(N_STEPS):
        solver.rk4_step()

        if step % SAMPLE_EVERY == 0:
            obs = solver.observe()
            t_now = step * DT
            times.append(t_now)
            depletion.append(obs['max_depletion'])
            E_acoustic.append(obs['E_acoustic'])
            traj_x.append(obs['cm_x'])
            traj_y.append(obs['cm_y'])
            traj_z.append(obs['cm_z'])

            if step % (SAMPLE_EVERY * 10) == 0:
                print(f"  step {step:5d}  t={t_now:7.3f}  "
                      f"depl={obs['max_depletion']:.4f}  "
                      f"E_ac={obs['E_acoustic']:.4e}  "
                      f"core=({obs['cm_x']/XI:+.1f}, "
                      f"{obs['cm_y']/XI:+.1f}, "
                      f"{obs['cm_z']/XI:+.1f}) xi")

    cp.cuda.Stream.null.synchronize()

    times      = np.array(times)
    depletion  = np.array(depletion)
    E_acoustic = np.array(E_acoustic)
    traj_x     = np.array(traj_x)
    traj_y     = np.array(traj_y)
    traj_z     = np.array(traj_z)

    # --- Conclusion ---
    # Check if depletion survived past step 3000
    late_mask = times >= 3000 * DT
    if np.any(late_mask):
        late_depl = depletion[late_mask]
        mean_late = float(np.mean(late_depl))
        survived = mean_late > 0.05 * RHO0
    else:
        mean_late = float(depletion[-1])
        survived = mean_late > 0.05 * RHO0

    dt_wall = time.time() - t0_wall

    print("\n" + "=" * 65)
    print("  RUSSELL GENESIS — RESULTS")
    print("=" * 65)
    print(f"  Initial depletion:  {depletion[0]:.6f}")
    print(f"  Final depletion:    {depletion[-1]:.6f}")
    print(f"  Late-time mean:     {mean_late:.6f}")
    print(f"  Peak E_acoustic:    {np.max(E_acoustic):.4e} "
          f"at t={times[np.argmax(E_acoustic)]:.3f}")
    print(f"  Final core pos:     ({traj_x[-1]/XI:+.2f}, "
          f"{traj_y[-1]/XI:+.2f}, {traj_z[-1]/XI:+.2f}) xi")
    if survived:
        print("\n  >> CONCLUSION: STABLE TOPOLOGICAL DEFECT SURVIVES.")
        print("     Twin opposing vortices phase-locked into a "
              "persistent structure.")
    else:
        print("\n  >> CONCLUSION: ANNIHILATION.")
        print("     Twin opposing vortices destroyed each other — "
              "no surviving defect.")
    print(f"\n  Wall time: {dt_wall:.1f} s")
    print("=" * 65)

    # --- Plot 1: Depletion vs Time ---
    fig, axes = plt.subplots(1, 3, figsize=(18, 5))

    ax = axes[0]
    ax.plot(times, depletion, '-', color='#2066a0', linewidth=1.5)
    ax.axhline(0.05 * RHO0, color='red', ls='--', alpha=0.5,
               label='5% threshold')
    ax.set_xlabel('Time  [natural units]')
    ax.set_ylabel('Max Density Depletion  (rho_0 - min rho)')
    ax.set_title('Defect Survival')
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.2)

    # --- Plot 2: Acoustic Energy vs Time ---
    ax = axes[1]
    ax.plot(times, E_acoustic, '-', color='#c04020', linewidth=1.5)
    ax.set_xlabel('Time  [natural units]')
    ax.set_ylabel('Acoustic Energy  E_kin')
    ax.set_title('Acoustic Emission')
    ax.grid(True, alpha=0.2)

    # --- Plot 3: 3D Trajectory ---
    ax3 = fig.add_subplot(1, 3, 3, projection='3d')
    axes[2].remove()
    sc = ax3.scatter(np.array(traj_x) / XI,
                     np.array(traj_y) / XI,
                     np.array(traj_z) / XI,
                     c=times, cmap='viridis', s=4, alpha=0.7)
    ax3.set_xlabel('x / xi')
    ax3.set_ylabel('y / xi')
    ax3.set_zlabel('z / xi')
    ax3.set_title('Defect Core Trajectory')
    plt.colorbar(sc, ax=ax3, label='Time', shrink=0.6)

    plt.suptitle('UHF Russell Genesis — Twin Opposing Vortex Collision',
                 fontsize=13, y=1.02)
    plt.tight_layout()
    out_path = os.path.join(os.path.dirname(__file__) or '.',
                            'uhf_russell_genesis.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    print(f"  Plot saved: {out_path}")
    plt.close()


if __name__ == '__main__':
    main()
