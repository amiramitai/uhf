#!/usr/bin/env python3
"""
UHF Topological Drag Mass — D3Q19 LBM (Headless, CUDA)
========================================================
Extends the proven D3Q19 added-mass framework to three distinct
topological knot boundaries, testing whether knot complexity
(crossing number) drives a non-linear mass hierarchy.

Topology 1 (Electron proxy): Simple Torus — unknot, crossing # = 0
Topology 2 (Muon proxy):     Trefoil Knot T(2,3), crossing # = 3
Topology 3 (Tau proxy):      Figure-Eight Knot 4_1, crossing # = 4

Same physics as uhf_added_mass_3d_lbm.py:
  D3Q19 LBM with BGK collision, moving bounce-back, MEM force.
  Constant acceleration along z-axis, sweep ρ₀.
  M_added = <F_z> / a₀, then C_added = M / (ρ₀ × V).

Output: Console table, CSV, headless PNG.
"""

import taichi as ti
import numpy as np
import time
import csv
import sys
import json

ti.init(arch=ti.cuda, default_fp=ti.f32, random_seed=42)

# ════════════════════════════════════════════════════════════════
# Grid — 128³
# ════════════════════════════════════════════════════════════════
NX, NY, NZ = 128, 128, 128
Q = 19
CS2 = 1.0 / 3.0

# ════════════════════════════════════════════════════════════════
# D3Q19 Lattice Data
# ════════════════════════════════════════════════════════════════
ex_np  = np.array([ 0,  1,-1,  0, 0,  0, 0,  1,-1,  1,-1,  1,-1,  1,-1,  0, 0,  0, 0], dtype=np.int32)
ey_np  = np.array([ 0,  0, 0,  1,-1,  0, 0,  1,-1, -1, 1,  0, 0,  0, 0,  1,-1,  1,-1], dtype=np.int32)
ez_np  = np.array([ 0,  0, 0,  0, 0,  1,-1,  0, 0,  0, 0,  1,-1, -1, 1,  1,-1, -1, 1], dtype=np.int32)
w_np   = np.array([1/3] + [1/18]*6 + [1/36]*12, dtype=np.float32)
opp_np = np.array([ 0,  2, 1,  4, 3,  6, 5,  8, 7, 10, 9, 12,11, 14,13, 16,15, 18,17], dtype=np.int32)

w_ti   = ti.field(dtype=ti.f32, shape=(Q,));  w_ti.from_numpy(w_np)
ex_ti  = ti.field(dtype=ti.i32, shape=(Q,));  ex_ti.from_numpy(ex_np)
ey_ti  = ti.field(dtype=ti.i32, shape=(Q,));  ey_ti.from_numpy(ey_np)
ez_ti  = ti.field(dtype=ti.i32, shape=(Q,));  ez_ti.from_numpy(ez_np)
opp_ti = ti.field(dtype=ti.i32, shape=(Q,));  opp_ti.from_numpy(opp_np)

# ════════════════════════════════════════════════════════════════
# LBM Fields
# ════════════════════════════════════════════════════════════════
f_f      = ti.field(dtype=ti.f32, shape=(NX, NY, NZ, Q))
f_tmp    = ti.field(dtype=ti.f32, shape=(NX, NY, NZ, Q))
rho_f    = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
ux_f     = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
uy_f     = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
uz_f     = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
solid    = ti.field(dtype=ti.i32, shape=(NX, NY, NZ))
bnd      = ti.field(dtype=ti.i32, shape=(NX, NY, NZ))

# Knot geometry is stamped from NumPy — upload via this array
solid_np = np.zeros((NX, NY, NZ), dtype=np.int32)

CX = float(NX) / 2.0
CY = float(NY) / 2.0
CZ = float(NZ) / 2.0

# ════════════════════════════════════════════════════════════════
# Knot Geometry Generators (CPU / NumPy)
# ════════════════════════════════════════════════════════════════
# All knots are generated as parametric tubes: sample the knot curve
# at high resolution, then voxelize all grid points within tube_r
# of the curve. This gives consistent tube cross-section thickness
# across all topologies.

TUBE_R = 6.0     # tube minor radius (lattice units) — same for all knots
N_CURVE = 4096   # parametric samples for curve

def voxelize_tube(curve_xyz, tube_r):
    """Given a (N,3) curve in lattice coordinates, mark all grid
    voxels within tube_r of the curve as solid."""
    s = np.zeros((NX, NY, NZ), dtype=np.int32)
    # Build KD-tree for fast lookup
    from scipy.spatial import cKDTree
    tree = cKDTree(curve_xyz)
    
    # Only check voxels in the bounding box + tube_r margin
    cmin = curve_xyz.min(axis=0) - tube_r - 1
    cmax = curve_xyz.max(axis=0) + tube_r + 1
    imin = max(0, int(np.floor(cmin[0])))
    jmin = max(0, int(np.floor(cmin[1])))
    kmin = max(0, int(np.floor(cmin[2])))
    imax = min(NX-1, int(np.ceil(cmax[0])))
    jmax = min(NY-1, int(np.ceil(cmax[1])))
    kmax = min(NZ-1, int(np.ceil(cmax[2])))
    
    # Vectorized: generate all candidate voxel centers
    ii = np.arange(imin, imax+1)
    jj = np.arange(jmin, jmax+1)
    kk = np.arange(kmin, kmax+1)
    grid = np.array(np.meshgrid(ii, jj, kk, indexing='ij')).reshape(3, -1).T  # (M, 3)
    
    dists, _ = tree.query(grid)
    mask = dists < tube_r
    
    gi = grid[mask, 0].astype(int)
    gj = grid[mask, 1].astype(int)
    gk = grid[mask, 2].astype(int)
    s[gi, gj, gk] = 1
    return s


def make_torus_curve():
    """Unknot (simple torus) — parametric circle in xy-plane."""
    R_major = 25.0  # major radius
    t = np.linspace(0, 2*np.pi, N_CURVE, endpoint=False)
    x = CX + R_major * np.cos(t)
    y = CY + R_major * np.sin(t)
    z = CZ + np.zeros_like(t)
    return np.column_stack([x, y, z])


def make_trefoil_curve():
    """Trefoil knot T(2,3) — torus knot parameterization.
    x = (R + r cos(3t)) cos(2t)
    y = (R + r cos(3t)) sin(2t)
    z = r sin(3t)
    Scaled to fit within ~50 lu diameter."""
    R_ = 22.0  # governs overall size
    r_ = 10.0  # governs knot amplitude
    t = np.linspace(0, 2*np.pi, N_CURVE, endpoint=False)
    x = CX + (R_ + r_ * np.cos(3*t)) * np.cos(2*t)
    y = CY + (R_ + r_ * np.cos(3*t)) * np.sin(2*t)
    z = CZ + r_ * np.sin(3*t)
    return np.column_stack([x, y, z])


def make_figure_eight_curve():
    """Figure-eight knot (4_1) parameterization.
    Standard parametric form:
      x = (2 + cos(2t)) cos(3t)
      y = (2 + cos(2t)) sin(3t)
      z = sin(4t)
    Scaled to fit within ~50 lu diameter."""
    scale = 12.0
    t = np.linspace(0, 2*np.pi, N_CURVE, endpoint=False)
    x = CX + scale * (2.0 + np.cos(2*t)) * np.cos(3*t)
    y = CY + scale * (2.0 + np.cos(2*t)) * np.sin(3*t)
    z = CZ + scale * np.sin(4*t)
    return np.column_stack([x, y, z])


# ════════════════════════════════════════════════════════════════
# Upload geometry to Taichi + compute boundary
# ════════════════════════════════════════════════════════════════
@ti.kernel
def compute_boundary():
    """Identify boundary solid nodes (adjacent to at least one fluid)."""
    for i, j, k in bnd:
        bnd[i, j, k] = 0
        if solid[i, j, k] == 1:
            for q in range(1, Q):
                ni = i + ex_ti[q]
                nj = j + ey_ti[q]
                nk = k + ez_ti[q]
                if 0 <= ni < NX and 0 <= nj < NY and 0 <= nk < NZ:
                    if solid[ni, nj, nk] == 0:
                        bnd[i, j, k] = 1

@ti.kernel
def count_solid() -> ti.i32:
    n = 0
    for i, j, k in solid:
        if solid[i, j, k] == 1:
            n += 1
    return n

@ti.kernel
def count_boundary() -> ti.i32:
    n = 0
    for i, j, k in bnd:
        if bnd[i, j, k] == 1:
            n += 1
    return n

def stamp_geometry(solid_array):
    """Upload a numpy solid array to the Taichi field."""
    solid.from_numpy(solid_array)
    compute_boundary()

# ════════════════════════════════════════════════════════════════
# LBM Kernels (identical to uhf_added_mass_3d_lbm.py)
# ════════════════════════════════════════════════════════════════
@ti.func
def feq(q: ti.i32, r: ti.f32, u: ti.f32, v: ti.f32, w: ti.f32) -> ti.f32:
    eu = (ti.cast(ex_ti[q], ti.f32) * u +
          ti.cast(ey_ti[q], ti.f32) * v +
          ti.cast(ez_ti[q], ti.f32) * w)
    usq = u * u + v * v + w * w
    return w_ti[q] * r * (1.0 + eu / CS2
                          + 0.5 * eu * eu / (CS2 * CS2)
                          - 0.5 * usq / CS2)

@ti.kernel
def init_equilibrium(rho0: ti.f32):
    for i, j, k in rho_f:
        rho_f[i, j, k] = rho0
        ux_f[i, j, k] = 0.0
        uy_f[i, j, k] = 0.0
        uz_f[i, j, k] = 0.0
        for q in range(Q):
            f_f[i, j, k, q] = feq(q, rho0, 0.0, 0.0, 0.0)

@ti.kernel
def collide(omega: ti.f32):
    for i, j, k in rho_f:
        if solid[i, j, k] == 0:
            r = ti.f32(0.0)
            u = ti.f32(0.0)
            v = ti.f32(0.0)
            vz = ti.f32(0.0)
            for q in range(Q):
                fi = f_f[i, j, k, q]
                r += fi
                u  += fi * ti.cast(ex_ti[q], ti.f32)
                v  += fi * ti.cast(ey_ti[q], ti.f32)
                vz += fi * ti.cast(ez_ti[q], ti.f32)
            inv_r = 1.0 / ti.max(r, 1e-10)
            u  *= inv_r
            v  *= inv_r
            vz *= inv_r
            rho_f[i, j, k] = r
            ux_f[i, j, k]  = u
            uy_f[i, j, k]  = v
            uz_f[i, j, k]  = vz
            for q in range(Q):
                f_f[i, j, k, q] = (1.0 - omega) * f_f[i, j, k, q] + omega * feq(q, r, u, v, vz)

@ti.kernel
def stream(wall_vz: ti.f32):
    for i, j, k in rho_f:
        for q in range(Q):
            si = i - ex_ti[q]
            sj = j - ey_ti[q]
            sk = k - ez_ti[q]
            if 0 <= si < NX and 0 <= sj < NY and 0 <= sk < NZ:
                if solid[si, sj, sk] == 0:
                    f_tmp[i, j, k, q] = f_f[si, sj, sk, q]
                else:
                    oq = opp_ti[q]
                    eu_w = ti.cast(ez_ti[q], ti.f32) * wall_vz
                    f_tmp[i, j, k, q] = (f_f[i, j, k, oq]
                                         + 2.0 * w_ti[q] * rho_f[i, j, k] * eu_w / CS2)
            else:
                f_tmp[i, j, k, q] = feq(q, rho_f[i, j, k],
                                        ux_f[i, j, k], uy_f[i, j, k], uz_f[i, j, k])

@ti.kernel
def momentum_exchange_z() -> ti.f32:
    fz = ti.f32(0.0)
    for i, j, k in bnd:
        if bnd[i, j, k] == 1:
            for q in range(1, Q):
                ni = i + ex_ti[q]
                nj = j + ey_ti[q]
                nk = k + ez_ti[q]
                if 0 <= ni < NX and 0 <= nj < NY and 0 <= nk < NZ:
                    if solid[ni, nj, nk] == 0:
                        oq = opp_ti[q]
                        fz += ti.cast(ez_ti[q], ti.f32) * (
                            f_f[ni, nj, nk, oq] + f_tmp[ni, nj, nk, q])
    return fz

@ti.kernel
def copy_back():
    for i, j, k in rho_f:
        if solid[i, j, k] == 0:
            for q in range(Q):
                f_f[i, j, k, q] = f_tmp[i, j, k, q]

# ════════════════════════════════════════════════════════════════
# Single-ρ₀ Run
# ════════════════════════════════════════════════════════════════
def run_single(rho0, omega, accel_val, n_warmup, n_accel, n_coast):
    total = n_warmup + n_accel + n_coast
    init_equilibrium(rho0)
    wall_vz = 0.0
    fz_series = np.zeros(total, dtype=np.float64)

    for step in range(total):
        if n_warmup <= step < n_warmup + n_accel:
            wall_vz += accel_val
        collide(omega)
        stream(wall_vz)
        fz = momentum_exchange_z()
        fz_series[step] = float(fz)
        copy_back()

    accel_slice = fz_series[n_warmup:n_warmup + n_accel]
    F_mean = float(np.mean(accel_slice))
    F_std  = float(np.std(accel_slice))
    M_meas = F_mean / accel_val
    M_std  = F_std  / accel_val
    return {'F_mean': F_mean, 'F_std': F_std, 'M_measured': M_meas, 'M_std': M_std}

# ════════════════════════════════════════════════════════════════
# Density Sweep for one topology
# ════════════════════════════════════════════════════════════════
def run_topology(name, crossing_num, curve_fn, rho_values, tau, accel_val,
                 n_warmup, n_accel, n_coast):
    print(f"\n{'═'*76}")
    print(f"  TOPOLOGY: {name}  (crossing number = {crossing_num})")
    print(f"{'═'*76}", flush=True)

    # Generate curve + voxelize
    curve = curve_fn()
    print(f"  Generating voxelized tube (tube_r={TUBE_R})...", flush=True)
    s = voxelize_tube(curve, TUBE_R)
    stamp_geometry(s)

    n_solid = int(count_solid())
    n_bnd   = int(count_boundary())
    V_solid = float(n_solid)
    omega   = 1.0 / tau

    print(f"  Solid volume : {n_solid:,} lu³")
    print(f"  Boundary nodes: {n_bnd:,}")
    print(f"  τ={tau}, ω={omega:.4f}, a₀={accel_val:.1e}", flush=True)

    print(f"\n  {'ρ₀':>6} | {'<F_z>':>14} | {'M_meas':>12} | "
          f"{'ρ₀×V':>12} | {'C_add':>8} | {'t(s)':>6}")
    print(f"  {'─'*76}")

    results = []
    for rho0 in rho_values:
        t0 = time.time()
        res = run_single(rho0, omega, accel_val, n_warmup, n_accel, n_coast)
        dt = time.time() - t0

        M_disp = rho0 * V_solid
        C_add  = res['M_measured'] / M_disp if M_disp > 1e-10 else 0.0
        res.update({'rho0': rho0, 'V_solid': V_solid,
                    'M_displaced': M_disp, 'C_added': C_add})
        results.append(res)

        print(f"  {rho0:6.2f} | {res['F_mean']:14.6e} | "
              f"{res['M_measured']:12.2f} | {M_disp:12.2f} | "
              f"{C_add:8.4f} | {dt:5.1f}", flush=True)

    # Linear regression through origin: M = α × ρ₀
    rhos = np.array([r['rho0'] for r in results])
    Ms   = np.array([r['M_measured'] for r in results])
    alpha = np.sum(rhos * Ms) / np.sum(rhos**2)
    SS_res = np.sum((Ms - alpha * rhos)**2)
    SS_tot = np.sum((Ms - np.mean(Ms))**2)
    R2 = 1.0 - SS_res / (SS_tot + 1e-30)
    C_eff = alpha / V_solid

    # Mean C_added across density points
    C_mean = np.mean([r['C_added'] for r in results])
    C_std  = np.std([r['C_added'] for r in results])

    print(f"\n  Regression: M = {alpha:.2f} × ρ₀,  R² = {R2:.10f}")
    print(f"  C_added = {C_eff:.6f}  (mean={C_mean:.6f} ± {C_std:.6f})")
    print(f"  V_solid = {V_solid:.0f} lu³")

    return {
        'name': name,
        'crossing_number': crossing_num,
        'V_solid': V_solid,
        'n_boundary': n_bnd,
        'alpha': float(alpha),
        'R2': float(R2),
        'C_added': float(C_eff),
        'C_mean': float(C_mean),
        'C_std': float(C_std),
        'M_at_rho1': float(alpha),  # M(ρ₀=1) = α
        'per_point': results,
    }

# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════
def main():
    tau       = 0.56
    accel_val = 2e-5
    n_warmup  = 300
    n_accel   = 1200
    n_coast   = 100

    rho_values = [0.4, 0.8, 1.0, 1.2, 1.6, 2.0]

    print("=" * 76)
    print("  UHF TOPOLOGICAL DRAG MASS — D3Q19 LBM")
    print(f"  Grid: {NX}×{NY}×{NZ}, tube_r={TUBE_R}")
    print(f"  Densities: {rho_values}")
    print(f"  Steps: {n_warmup} warmup + {n_accel} accel + {n_coast} coast")
    print("=" * 76)
    sys.stdout.flush()

    topologies = [
        ("Unknot (Torus)",      0, make_torus_curve),
        ("Trefoil T(2,3)",      3, make_trefoil_curve),
        ("Figure-Eight 4_1",    4, make_figure_eight_curve),
    ]

    all_results = []
    t_total = time.time()

    for name, cross_num, curve_fn in topologies:
        res = run_topology(name, cross_num, curve_fn, rho_values,
                           tau, accel_val, n_warmup, n_accel, n_coast)
        all_results.append(res)

    t_total = time.time() - t_total

    # ═══════════════════════════════════════════════════════════
    # COMPARATIVE TABLE
    # ═══════════════════════════════════════════════════════════
    print(f"\n\n{'═'*76}")
    print("  COMPARATIVE TOPOLOGICAL MASS TABLE")
    print(f"{'═'*76}")
    print(f"  {'Topology':<22} | {'Cross#':>6} | {'V_solid':>8} | "
          f"{'C_added':>8} | {'M(ρ=1)':>10} | {'R²':>12}")
    print(f"  {'─'*76}")

    for r in all_results:
        print(f"  {r['name']:<22} | {r['crossing_number']:>6} | "
              f"{r['V_solid']:>8.0f} | {r['C_added']:>8.4f} | "
              f"{r['M_at_rho1']:>10.2f} | {r['R2']:>12.8f}")

    # Mass ratios
    if len(all_results) == 3:
        m_e = all_results[0]['M_at_rho1']
        m_mu = all_results[1]['M_at_rho1']
        m_tau = all_results[2]['M_at_rho1']

        print(f"\n  MASS RATIOS (normalized to Unknot = 1):")
        print(f"    Unknot:       {m_e/m_e:10.4f}  (electron proxy)")
        print(f"    Trefoil:      {m_mu/m_e:10.4f}  (muon proxy)")
        print(f"    Figure-Eight: {m_tau/m_e:10.4f}  (tau proxy)")
        print(f"\n  Physical lepton mass ratios for reference:")
        print(f"    m_μ / m_e  = {105.658/0.511:.1f}")
        print(f"    m_τ / m_e  = {1776.86/0.511:.1f}")

        # Non-linearity check
        print(f"\n  NON-LINEARITY OF MASS vs CROSSING NUMBER:")
        print(f"    cross# 0→3: mass ratio = {m_mu/m_e:.4f}")
        print(f"    cross# 3→4: mass ratio = {m_tau/m_mu:.4f}")
        if m_mu > m_e and m_tau > m_mu:
            # Check if super-linear
            if m_tau/m_mu > (m_mu/m_e)**(1.0/3.0):
                print(f"    → NON-LINEAR mass hierarchy CONFIRMED")
            else:
                print(f"    → Mass increases with topology but sub-linearly")
        elif m_mu > m_e:
            print(f"    → Partial hierarchy (trefoil > unknot)")
        else:
            print(f"    → No clear hierarchy detected")

    print(f"\n  Total wall time: {t_total:.1f}s")
    print(f"{'═'*76}")

    # ═══════════════════════════════════════════════════════════
    # CSV
    # ═══════════════════════════════════════════════════════════
    csv_path = "uhf_topological_drag_mass.csv"
    with open(csv_path, 'w', newline='') as csvf:
        writer = csv.writer(csvf)
        writer.writerow(['topology', 'crossing_number', 'V_solid',
                         'C_added', 'R2', 'M_at_rho1', 'alpha',
                         'C_mean', 'C_std'])
        for r in all_results:
            writer.writerow([r['name'], r['crossing_number'],
                             f"{r['V_solid']:.0f}",
                             f"{r['C_added']:.6f}",
                             f"{r['R2']:.10f}",
                             f"{r['M_at_rho1']:.4f}",
                             f"{r['alpha']:.4f}",
                             f"{r['C_mean']:.6f}",
                             f"{r['C_std']:.6f}"])
    print(f"\n  CSV: {csv_path}")

    # Per-point detail CSV
    csv_detail = "uhf_topological_drag_mass_detail.csv"
    with open(csv_detail, 'w', newline='') as csvf:
        writer = csv.writer(csvf)
        writer.writerow(['topology', 'crossing_number', 'rho0',
                         'F_mean', 'F_std', 'M_measured', 'M_std',
                         'V_solid', 'C_added'])
        for r in all_results:
            for p in r['per_point']:
                writer.writerow([r['name'], r['crossing_number'],
                                 f"{p['rho0']:.2f}",
                                 f"{p['F_mean']:.8e}",
                                 f"{p['F_std']:.8e}",
                                 f"{p['M_measured']:.4f}",
                                 f"{p['M_std']:.4f}",
                                 f"{p['V_solid']:.0f}",
                                 f"{p['C_added']:.6f}"])
    print(f"  CSV: {csv_detail}")

    # JSON
    json_path = "uhf_topological_drag_mass.json"
    json_out = []
    for r in all_results:
        entry = {k: v for k, v in r.items() if k != 'per_point'}
        json_out.append(entry)
    with open(json_path, 'w') as jf:
        json.dump(json_out, jf, indent=2)
    print(f"  JSON: {json_path}")

    # ═══════════════════════════════════════════════════════════
    # PLOT
    # ═══════════════════════════════════════════════════════════
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 3, figsize=(20, 6))

        # 1. M vs ρ₀ for each topology
        ax = axes[0]
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c']
        markers = ['o', 's', '^']
        for idx, r in enumerate(all_results):
            rhos = [p['rho0'] for p in r['per_point']]
            Ms = [p['M_measured'] for p in r['per_point']]
            ax.scatter(rhos, Ms, color=colors[idx], marker=markers[idx],
                      s=60, zorder=3, label=f"{r['name']} (C#={r['crossing_number']})")
            rho_fit = np.linspace(0, max(rhos)*1.1, 50)
            ax.plot(rho_fit, r['alpha'] * rho_fit, '--', color=colors[idx], alpha=0.7)
        ax.set_xlabel('ρ₀ (background density)', fontsize=12)
        ax.set_ylabel('M_added (lattice units)', fontsize=12)
        ax.set_title('Added Mass vs Density', fontsize=14)
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)

        # 2. C_added vs crossing number
        ax = axes[1]
        cross_nums = [r['crossing_number'] for r in all_results]
        c_vals = [r['C_added'] for r in all_results]
        ax.bar(cross_nums, c_vals, width=0.6, color=colors[:len(all_results)],
               edgecolor='black', linewidth=1.5)
        for i, r in enumerate(all_results):
            ax.text(r['crossing_number'], r['C_added'] * 1.02,
                    f"C={r['C_added']:.4f}", ha='center', fontsize=10, fontweight='bold')
        ax.set_xlabel('Crossing Number', fontsize=12)
        ax.set_ylabel('C_added', fontsize=12)
        ax.set_title('Added Mass Coefficient vs Knot Complexity', fontsize=14)
        ax.set_xticks(cross_nums)
        ax.grid(True, alpha=0.3, axis='y')

        # 3. Total M(ρ=1) as bar chart
        ax = axes[2]
        names = [r['name'] for r in all_results]
        m_vals = [r['M_at_rho1'] for r in all_results]
        bars = ax.bar(range(len(names)), m_vals, color=colors[:len(all_results)],
                     edgecolor='black', linewidth=1.5)
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels(names, rotation=15, fontsize=10)
        ax.set_ylabel('M_added(ρ₀=1)', fontsize=12)
        ax.set_title('Topological Mass Hierarchy', fontsize=14)
        for i, (bar, m) in enumerate(zip(bars, m_vals)):
            ax.text(bar.get_x() + bar.get_width()/2, m * 1.02,
                    f"{m:.1f}", ha='center', fontsize=11, fontweight='bold')
        ax.grid(True, alpha=0.3, axis='y')

        plt.suptitle('UHF Topological Drag Mass — D3Q19 LBM (128³)',
                     fontsize=16, fontweight='bold', y=1.02)
        plt.tight_layout()
        plt.savefig("uhf_topological_drag_mass.png", dpi=150, bbox_inches='tight')
        print(f"  Plot: uhf_topological_drag_mass.png")
        plt.close()
    except Exception as e:
        print(f"  Plot failed: {e}")

    print(f"\n  Done.\n")


if __name__ == "__main__":
    main()
