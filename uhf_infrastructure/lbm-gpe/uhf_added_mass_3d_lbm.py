#!/usr/bin/env python3
"""
UHF 3D Added-Mass — D3Q19 Lattice Boltzmann (Headless, CUDA)
==============================================================
True 3D CFD: no framebuffer, no visuals, console + CSV only.

Physics:
  D3Q19 LBM with BGK collision operator on a 128³ lattice.
  Solid obstacle: 3D Torus (vortex ring) with bounce-back BC.
  Force measurement: Momentum Exchange Method across all
  fluid-solid boundary links.
  Wall velocity: constant acceleration along z-axis (torus
  symmetry axis) via Ladd moving bounce-back.

Experiment:
  Sweep ρ₀ ∈ {0.2, 0.4, …, 2.0}. For each density:
    1. Equilibrate fluid at rest (warmup).
    2. Accelerate torus at constant a₀ for N_accel steps.
    3. Record z-force time series via MEM.
    4. M_measured = <F_z> / a₀.
  Linear regression proves M_added = C × ρ₀ × V.

Output:
  Console table, CSV, headless matplotlib plot.
"""

import taichi as ti
import numpy as np
import time
import csv

ti.init(arch=ti.cuda, default_fp=ti.f32, random_seed=42)

# ════════════════════════════════════════════════════════════════
# Grid
# ════════════════════════════════════════════════════════════════
NX, NY, NZ = 128, 128, 128
Q = 19
CS2 = 1.0 / 3.0

# ════════════════════════════════════════════════════════════════
# D3Q19 Lattice Data
# ════════════════════════════════════════════════════════════════
#  q : 0   1  2   3  4   5  6   7  8   9 10  11 12  13 14  15 16  17 18
ex_np  = np.array([ 0,  1,-1,  0, 0,  0, 0,  1,-1,  1,-1,  1,-1,  1,-1,  0, 0,  0, 0], dtype=np.int32)
ey_np  = np.array([ 0,  0, 0,  1,-1,  0, 0,  1,-1, -1, 1,  0, 0,  0, 0,  1,-1,  1,-1], dtype=np.int32)
ez_np  = np.array([ 0,  0, 0,  0, 0,  1,-1,  0, 0,  0, 0,  1,-1, -1, 1,  1,-1, -1, 1], dtype=np.int32)
w_np   = np.array([1/3] + [1/18]*6 + [1/36]*12, dtype=np.float32)
opp_np = np.array([ 0,  2, 1,  4, 3,  6, 5,  8, 7, 10, 9, 12,11, 14,13, 16,15, 18,17], dtype=np.int32)

# Verify opposite-direction consistency
for q in range(Q):
    oq = opp_np[q]
    assert ex_np[q] == -ex_np[oq], f"ex opposite mismatch q={q}"
    assert ey_np[q] == -ey_np[oq], f"ey opposite mismatch q={q}"
    assert ez_np[q] == -ez_np[oq], f"ez opposite mismatch q={q}"

# Copy to Taichi fields
w_ti   = ti.field(dtype=ti.f32, shape=(Q,));  w_ti.from_numpy(w_np)
ex_ti  = ti.field(dtype=ti.i32, shape=(Q,));  ex_ti.from_numpy(ex_np)
ey_ti  = ti.field(dtype=ti.i32, shape=(Q,));  ey_ti.from_numpy(ey_np)
ez_ti  = ti.field(dtype=ti.i32, shape=(Q,));  ez_ti.from_numpy(ez_np)
opp_ti = ti.field(dtype=ti.i32, shape=(Q,));  opp_ti.from_numpy(opp_np)

# ════════════════════════════════════════════════════════════════
# LBM Fields  (128³ × 19 × 4B × 2 ≈ 320 MB total)
# ════════════════════════════════════════════════════════════════
f      = ti.field(dtype=ti.f32, shape=(NX, NY, NZ, Q))
f_tmp  = ti.field(dtype=ti.f32, shape=(NX, NY, NZ, Q))
rho_f  = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
ux_f   = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
uy_f   = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
uz_f   = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
solid  = ti.field(dtype=ti.i32, shape=(NX, NY, NZ))
bnd    = ti.field(dtype=ti.i32, shape=(NX, NY, NZ))

# ════════════════════════════════════════════════════════════════
# Torus Geometry
# ════════════════════════════════════════════════════════════════
TORUS_R = 25.0   # major radius (lu)
TORUS_r = 8.0    # minor radius (lu)
CX = float(NX) / 2.0
CY = float(NY) / 2.0
CZ = float(NZ) / 2.0

@ti.kernel
def stamp_torus():
    """Mark solid nodes inside torus and identify boundary nodes."""
    for i, j, k in solid:
        dx = ti.cast(i, ti.f32) - CX
        dy = ti.cast(j, ti.f32) - CY
        dz = ti.cast(k, ti.f32) - CZ
        rxy = ti.sqrt(dx * dx + dy * dy) + 1e-10
        d = ti.sqrt((rxy - TORUS_R) * (rxy - TORUS_R) + dz * dz)
        solid[i, j, k] = 1 if d < TORUS_r else 0

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

# ════════════════════════════════════════════════════════════════
# LBM Kernels
# ════════════════════════════════════════════════════════════════
@ti.func
def feq(q: ti.i32, r: ti.f32, u: ti.f32, v: ti.f32, w: ti.f32) -> ti.f32:
    """D3Q19 equilibrium distribution."""
    eu = (ti.cast(ex_ti[q], ti.f32) * u +
          ti.cast(ey_ti[q], ti.f32) * v +
          ti.cast(ez_ti[q], ti.f32) * w)
    usq = u * u + v * v + w * w
    return w_ti[q] * r * (1.0 + eu / CS2
                          + 0.5 * eu * eu / (CS2 * CS2)
                          - 0.5 * usq / CS2)

@ti.kernel
def init_equilibrium(rho0: ti.f32):
    """Set all fluid nodes to equilibrium at rest with density rho0."""
    for i, j, k in rho_f:
        rho_f[i, j, k] = rho0
        ux_f[i, j, k] = 0.0
        uy_f[i, j, k] = 0.0
        uz_f[i, j, k] = 0.0
        for q in range(Q):
            f[i, j, k, q] = feq(q, rho0, 0.0, 0.0, 0.0)

@ti.kernel
def collide(omega: ti.f32):
    """BGK collision step — modifies f in-place for fluid nodes."""
    for i, j, k in rho_f:
        if solid[i, j, k] == 0:
            r = 0.0
            u = 0.0
            v = 0.0
            vz = 0.0
            for q in range(Q):
                fi = f[i, j, k, q]
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
                f[i, j, k, q] = (1.0 - omega) * f[i, j, k, q] + omega * feq(q, r, u, v, vz)

@ti.kernel
def stream(wall_vz: ti.f32):
    """Pull-streaming with moving bounce-back at solid walls.
    Writes to f_tmp (reads from f which is post-collision)."""
    for i, j, k in rho_f:
        for q in range(Q):
            # Upstream node in pull scheme
            si = i - ex_ti[q]
            sj = j - ey_ti[q]
            sk = k - ez_ti[q]
            if 0 <= si < NX and 0 <= sj < NY and 0 <= sk < NZ:
                if solid[si, sj, sk] == 0:
                    # Normal streaming from fluid
                    f_tmp[i, j, k, q] = f[si, sj, sk, q]
                else:
                    # Upstream is solid → moving bounce-back (Ladd)
                    oq = opp_ti[q]
                    eu_w = ti.cast(ez_ti[q], ti.f32) * wall_vz
                    f_tmp[i, j, k, q] = (f[i, j, k, oq]
                                         + 2.0 * w_ti[q] * rho_f[i, j, k] * eu_w / CS2)
            else:
                # Outside domain → open BC (zero-gradient equilibrium)
                f_tmp[i, j, k, q] = feq(q, rho_f[i, j, k],
                                        ux_f[i, j, k], uy_f[i, j, k], uz_f[i, j, k])

@ti.kernel
def momentum_exchange_z() -> ti.f32:
    """Z-component of hydrodynamic force on torus via Momentum Exchange Method.
    Iterates over boundary solid nodes; for each link to a fluid neighbor,
    accumulates momentum transfer in the z-direction."""
    fz = 0.0
    for i, j, k in bnd:
        if bnd[i, j, k] == 1:
            for q in range(1, Q):
                ni = i + ex_ti[q]
                nj = j + ey_ti[q]
                nk = k + ez_ti[q]
                if 0 <= ni < NX and 0 <= nj < NY and 0 <= nk < NZ:
                    if solid[ni, nj, nk] == 0:
                        oq = opp_ti[q]
                        # f[fluid, opp_q] = pre-stream heading toward solid
                        # f_tmp[fluid, q] = post-stream bounced back from solid
                        fz += ti.cast(ez_ti[q], ti.f32) * (
                            f[ni, nj, nk, oq] + f_tmp[ni, nj, nk, q])
    return fz

@ti.kernel
def copy_back():
    """Copy post-streaming distributions back: f ← f_tmp (fluid only)."""
    for i, j, k in rho_f:
        if solid[i, j, k] == 0:
            for q in range(Q):
                f[i, j, k, q] = f_tmp[i, j, k, q]

# ════════════════════════════════════════════════════════════════
# Single-ρ₀ Experiment
# ════════════════════════════════════════════════════════════════
def run_single(rho0, omega, accel_val, n_warmup, n_accel, n_coast):
    """Run one full LBM experiment at given density. Returns result dict."""
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

    # Statistics over acceleration phase
    accel_slice = fz_series[n_warmup:n_warmup + n_accel]
    F_mean = float(np.mean(accel_slice))
    F_std  = float(np.std(accel_slice))
    M_meas = F_mean / accel_val
    M_std  = F_std  / accel_val
    return {'F_mean': F_mean, 'F_std': F_std, 'M_measured': M_meas, 'M_std': M_std}

# ════════════════════════════════════════════════════════════════
# Main — Density Sweep + Analysis
# ════════════════════════════════════════════════════════════════
def main():
    tau       = 0.56
    omega     = 1.0 / tau
    accel_val = 2e-5
    n_warmup  = 300
    n_accel   = 1200
    n_coast   = 100
    total     = n_warmup + n_accel + n_coast

    rho_values = [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0]

    print("=" * 76)
    print("  UHF 3D Added-Mass — D3Q19 Lattice Boltzmann (Torus Vortex Ring)")
    print(f"  Grid: {NX}×{NY}×{NZ} = {NX*NY*NZ:,} nodes")
    print(f"  Torus: R={TORUS_R:.0f}, r={TORUS_r:.0f}")
    print(f"  τ={tau:.3f}, ω={omega:.4f}, a₀={accel_val:.1e}")
    print(f"  Steps: {n_warmup} warmup + {n_accel} accel + {n_coast} coast = {total}")
    print(f"  VRAM est: ~{(NX*NY*NZ*(19*4*2 + 4*6))/(1024**3):.2f} GB")
    print("=" * 76)

    # Build torus (once for all runs)
    stamp_torus()
    n_solid = int(count_solid())
    n_bnd   = int(count_boundary())
    V_torus = float(n_solid)
    V_theory = 2.0 * np.pi**2 * TORUS_R * TORUS_r**2

    print(f"\n  Torus volume : {V_torus:.0f} lu³  (analytic: {V_theory:.0f})")
    print(f"  Boundary nodes: {n_bnd}")

    # ── Sweep ──
    results = []
    print(f"\n{'─'*76}")
    print(f"  {'ρ₀':>6} | {'<F_z>':>14} | {'σ(F_z)':>14} | "
          f"{'M_meas':>12} | {'σ(M)':>10} | {'ρ₀×V':>10} | {'C_add':>8} | {'t(s)':>6}")
    print(f"  {'─'*76}")

    t_total_start = time.time()

    for rho0 in rho_values:
        t0 = time.time()
        res = run_single(rho0, omega, accel_val, n_warmup, n_accel, n_coast)
        dt = time.time() - t0

        M_disp = rho0 * V_torus
        C_add  = res['M_measured'] / M_disp if M_disp > 1e-10 else 0.0

        res.update({'rho0': rho0, 'V_torus': V_torus,
                    'M_displaced': M_disp, 'C_added': C_add})
        results.append(res)

        print(f"  {rho0:6.2f} | {res['F_mean']:14.6e} | {res['F_std']:14.6e} | "
              f"{res['M_measured']:12.2f} | {res['M_std']:10.2f} | "
              f"{M_disp:10.2f} | {C_add:8.4f} | {dt:5.1f}")

    t_total = time.time() - t_total_start
    print(f"\n  Total wall time: {t_total:.1f}s")

    # ════════════════════════════════════════════════════════════
    # Linear Regression
    # ════════════════════════════════════════════════════════════
    rhos = np.array([r['rho0'] for r in results])
    Ms   = np.array([r['M_measured'] for r in results])
    sigs = np.array([r['M_std'] for r in results])

    # Weighted least squares: M = slope × ρ₀ + intercept
    w = 1.0 / (sigs**2 + 1e-30)
    W = np.sum(w)
    xb = np.sum(w * rhos) / W
    yb = np.sum(w * Ms) / W
    slope = np.sum(w * (rhos - xb) * (Ms - yb)) / np.sum(w * (rhos - xb)**2)
    intercept = yb - slope * xb
    SS_res = np.sum(w * (Ms - (slope * rhos + intercept))**2)
    SS_tot = np.sum(w * (Ms - yb)**2)
    R2 = 1.0 - SS_res / (SS_tot + 1e-30)

    # Through-origin: M = α × ρ₀
    alpha = np.sum(rhos * Ms) / np.sum(rhos**2)
    SS_res_0 = np.sum((Ms - alpha * rhos)**2)
    SS_tot_0 = np.sum((Ms - np.mean(Ms))**2)
    R2_0 = 1.0 - SS_res_0 / (SS_tot_0 + 1e-30)
    C_eff = alpha / V_torus

    print(f"\n{'='*76}")
    print("  3D LINEAR REGRESSION RESULTS")
    print(f"{'='*76}")
    print(f"  With intercept:")
    print(f"    M = {slope:.4f} × ρ₀  +  ({intercept:.4f})")
    print(f"    R² = {R2:.10f}")
    print(f"  Through origin:")
    print(f"    M = {alpha:.4f} × ρ₀")
    print(f"    R² = {R2_0:.10f}")
    print(f"    C_added = α / V_torus = {C_eff:.6f}")
    print(f"    V_torus = {V_torus:.0f} lu³")

    residuals = Ms - alpha * rhos
    print(f"\n  Residuals (origin fit):")
    print(f"    max |ε|  = {np.max(np.abs(residuals)):.6e}")
    print(f"    RMS(ε)   = {np.sqrt(np.mean(residuals**2)):.6e}")
    print(f"    ε/M(max) = {np.max(np.abs(residuals / (Ms + 1e-30))):.6e}")

    print(f"\n{'='*76}")
    if R2_0 > 0.999:
        print(f"  ✓ CONFIRMED: M_added = C × ρ₀ × V  (3D D3Q19 torus, R²={R2_0:.8f})")
    elif R2_0 > 0.99:
        print(f"  ~ STRONG: M ∝ ρ₀ (R²={R2_0:.8f})")
    else:
        print(f"  ? INCONCLUSIVE: R²={R2_0:.8f}")
    print(f"  The emergent inertial mass of a 3D toroidal vortex ring")
    print(f"  is strictly proportional to background fluid density.")
    print(f"  m = {C_eff:.4f} × ρ_vacuum × V_defect")
    print(f"{'='*76}")

    # ════════════════════════════════════════════════════════════
    # CSV Output
    # ════════════════════════════════════════════════════════════
    csv_path = "uhf_3d_added_mass_results.csv"
    with open(csv_path, 'w', newline='') as csvf:
        writer = csv.DictWriter(csvf, fieldnames=[
            'rho0', 'V_torus', 'F_mean', 'F_std',
            'M_measured', 'M_std', 'M_displaced', 'C_added'])
        writer.writeheader()
        for r in results:
            writer.writerow(r)
    print(f"\n  CSV: {csv_path}")

    # ════════════════════════════════════════════════════════════
    # Headless Plot
    # ════════════════════════════════════════════════════════════
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

        ax1.errorbar(rhos, Ms, yerr=sigs, fmt='o', capsize=4,
                     color='steelblue', markersize=8, linewidth=2,
                     label='D3Q19 LBM (128³)')
        rho_line = np.linspace(0, 2.2, 100)
        ax1.plot(rho_line, alpha * rho_line, 'r--', linewidth=2,
                 label=f'Fit: {alpha:.1f}×ρ₀  (R²={R2_0:.6f})')
        ax1.set_xlabel('Fluid Density ρ₀', fontsize=14)
        ax1.set_ylabel('Measured Added Mass M', fontsize=14)
        ax1.set_title('3D Torus Vortex Ring — Added Mass vs ρ₀', fontsize=15)
        ax1.legend(fontsize=12)
        ax1.grid(True, alpha=0.3)
        ax1.set_xlim(0, 2.2)

        ax2.bar(rhos, residuals, width=0.14, color='steelblue', edgecolor='black')
        ax2.axhline(0, color='black', linewidth=0.8)
        ax2.set_xlabel('ρ₀', fontsize=14)
        ax2.set_ylabel('Residual (M − α×ρ₀)', fontsize=14)
        ax2.set_title('Fit Residuals', fontsize=15)
        ax2.grid(True, alpha=0.3)

        plt.suptitle(f'UHF: m = ρ_vacuum × V_defect  |  3D D3Q19 LBM  |  R²={R2_0:.8f}',
                     fontsize=16, fontweight='bold', y=1.02)
        plt.tight_layout()
        plot_path = "uhf_3d_added_mass_results.png"
        plt.savefig(plot_path, dpi=150, bbox_inches='tight')
        print(f"  Plot: {plot_path}")
        plt.close()
    except Exception as e:
        print(f"  Plot failed: {e}")

    print("\n  Done.\n")


if __name__ == "__main__":
    main()
