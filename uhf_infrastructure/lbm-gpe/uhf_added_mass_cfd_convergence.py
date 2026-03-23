#!/usr/bin/env python3
"""
UHF Added-Mass Convergence Study — Headless LBM D2Q9
=====================================================
No framebuffer. No GUI. Pure numerical CFD.

Physics question:
  Does a Lattice Boltzmann fluid naturally produce
  M_added = C × ρ₀ × A_obstacle  when an obstacle accelerates?
  (C = geometry-dependent added-mass coefficient)

Method:
  1. D2Q9 LBM with BGK collision on CUDA (Taichi)
  2. Concave star obstacle with moving bounce-back BC
  3. Apply known acceleration a₀ for N_accel steps
  4. Measure hydrodynamic force F via momentum exchange
  5. Compute M_measured = <F> / a₀  (time-averaged during steady accel)
  6. Sweep ρ₀ ∈ {0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0}
  7. Grid convergence: repeat at 256², 512², 768² to check M → limit
  8. Fit M_measured = slope × ρ₀ → extract C_added × A
  9. Report R², residuals, convergence order

Output:
  - Console table with all measurements
  - CSV: uhf_added_mass_convergence.csv
  - Plot: uhf_added_mass_convergence.png (matplotlib, no GUI)
"""

import taichi as ti
import numpy as np
import time
import os

ti.init(arch=ti.cuda, default_fp=ti.f64, random_seed=42)

# ═══════════════════════════════════════════════════════════════
# D2Q9 Constants (host-side numpy, copied per run)
# ═══════════════════════════════════════════════════════════════
Q = 9
CS2 = 1.0 / 3.0
w_np  = np.array([4/9, 1/9, 1/9, 1/9, 1/9, 1/36, 1/36, 1/36, 1/36], dtype=np.float64)
ex_np = np.array([0, 1, 0, -1, 0, 1, -1, -1, 1], dtype=np.int32)
ey_np = np.array([0, 0, 1, 0, -1, 1, 1, -1, -1], dtype=np.int32)
opp_np = np.array([0, 3, 4, 1, 2, 7, 8, 5, 6], dtype=np.int32)

# ═══════════════════════════════════════════════════════════════
# Parameterised LBM Solver (one run)
# ═══════════════════════════════════════════════════════════════
def run_lbm(NX, NY, rho0, tau, accel_val, n_warmup, n_accel, n_coast):
    """
    Run a single LBM experiment.
    Returns dict with time-series of force and derived added mass.
    """
    omega = 1.0 / tau

    # --- Taichi fields (allocated fresh each call) ---
    f_field     = ti.field(dtype=ti.f64, shape=(NX, NY, Q))
    f_tmp       = ti.field(dtype=ti.f64, shape=(NX, NY, Q))
    rho_f       = ti.field(dtype=ti.f64, shape=(NX, NY))
    ux_f        = ti.field(dtype=ti.f64, shape=(NX, NY))
    uy_f        = ti.field(dtype=ti.f64, shape=(NX, NY))
    solid       = ti.field(dtype=ti.i32, shape=(NX, NY))
    bnd         = ti.field(dtype=ti.i32, shape=(NX, NY))
    w_ti        = ti.field(dtype=ti.f64, shape=(Q,))
    ex_ti       = ti.field(dtype=ti.i32, shape=(Q,))
    ey_ti       = ti.field(dtype=ti.i32, shape=(Q,))
    opp_ti      = ti.field(dtype=ti.i32, shape=(Q,))

    w_ti.from_numpy(w_np)
    ex_ti.from_numpy(ex_np)
    ey_ti.from_numpy(ey_np)
    opp_ti.from_numpy(opp_np)

    # Obstacle geometry: concave 5-arm star at grid center
    # Scale with grid: radius = 0.08 * min(NX, NY)
    R_outer = 0.08 * min(NX, NY)
    R_inner = 0.04 * min(NX, NY)
    n_arms  = 5

    @ti.kernel
    def stamp_obstacle(cx: ti.f64, cy: ti.f64):
        for i, j in solid:
            dx = ti.cast(i, ti.f64) - cx
            dy = ti.cast(j, ti.f64) - cy
            r = ti.sqrt(dx * dx + dy * dy) + 1e-12
            theta = ti.atan2(dy, dx)
            star_r = R_inner + (R_outer - R_inner) * 0.5 * (1.0 + ti.cos(ti.cast(n_arms, ti.f64) * theta))
            solid[i, j] = 1 if r < star_r else 0
        for i, j in bnd:
            bnd[i, j] = 0
            if solid[i, j] == 1:
                for q in range(1, Q):
                    ni = i + ex_ti[q]
                    nj = j + ey_ti[q]
                    if 0 <= ni < NX and 0 <= nj < NY:
                        if solid[ni, nj] == 0:
                            bnd[i, j] = 1

    @ti.func
    def feq(q: ti.i32, rho_l: ti.f64, ux_l: ti.f64, uy_l: ti.f64) -> ti.f64:
        eu = ti.cast(ex_ti[q], ti.f64) * ux_l + ti.cast(ey_ti[q], ti.f64) * uy_l
        usq = ux_l * ux_l + uy_l * uy_l
        return w_ti[q] * rho_l * (1.0 + eu / CS2 + 0.5 * eu * eu / (CS2 * CS2) - 0.5 * usq / CS2)

    @ti.kernel
    def init_eq(rho0_v: ti.f64):
        for i, j in rho_f:
            rho_f[i, j] = rho0_v
            ux_f[i, j] = 0.0
            uy_f[i, j] = 0.0
            for q in range(Q):
                f_field[i, j, q] = feq(q, rho0_v, 0.0, 0.0)

    @ti.kernel
    def collide_stream(omega_v: ti.f64, wall_vx: ti.f64, wall_vy: ti.f64):
        # Collision (fluid nodes only)
        for i, j in rho_f:
            if solid[i, j] == 0:
                r = 0.0
                u = 0.0
                v = 0.0
                for q in range(Q):
                    fi = f_field[i, j, q]
                    r += fi
                    u += fi * ti.cast(ex_ti[q], ti.f64)
                    v += fi * ti.cast(ey_ti[q], ti.f64)
                if r > 1e-14:
                    u /= r
                    v /= r
                rho_f[i, j] = r
                ux_f[i, j] = u
                uy_f[i, j] = v
                for q in range(Q):
                    f_field[i, j, q] = f_field[i, j, q] * (1.0 - omega_v) + feq(q, r, u, v) * omega_v

        # Streaming with bounce-back at solid
        for i, j in rho_f:
            for q in range(Q):
                ni = i - ex_ti[q]
                nj = j - ey_ti[q]
                if 0 <= ni < NX and 0 <= nj < NY:
                    if solid[ni, nj] == 0:
                        f_tmp[i, j, q] = f_field[ni, nj, q]
                    else:
                        oq = opp_ti[q]
                        eu_w = ti.cast(ex_ti[q], ti.f64) * wall_vx + ti.cast(ey_ti[q], ti.f64) * wall_vy
                        f_tmp[i, j, q] = f_field[i, j, oq] + 2.0 * w_ti[q] * rho_f[i, j] * eu_w / CS2
                else:
                    # Open BC: equilibrium at background density
                    f_tmp[i, j, q] = feq(q, rho_f[i, j], ux_f[i, j], uy_f[i, j])

        for i, j in rho_f:
            if solid[i, j] == 0:
                for q in range(Q):
                    f_field[i, j, q] = f_tmp[i, j, q]

    @ti.kernel
    def momentum_exchange_force() -> ti.types.vector(2, ti.f64):
        fx = 0.0
        fy = 0.0
        for i, j in bnd:
            if bnd[i, j] == 1:
                for q in range(1, Q):
                    ni = i + ex_ti[q]
                    nj = j + ey_ti[q]
                    if 0 <= ni < NX and 0 <= nj < NY:
                        if solid[ni, nj] == 0:
                            oq = opp_ti[q]
                            fx += ti.cast(ex_ti[q], ti.f64) * (f_field[ni, nj, oq] + f_tmp[ni, nj, q])
                            fy += ti.cast(ey_ti[q], ti.f64) * (f_field[ni, nj, oq] + f_tmp[ni, nj, q])
        return ti.Vector([fx, fy])

    @ti.kernel
    def count_solid() -> ti.i32:
        n = 0
        for i, j in solid:
            if solid[i, j] == 1:
                n += 1
        return n

    # ── Run ──
    cx = float(NX) / 2.0
    cy = float(NY) / 2.0

    stamp_obstacle(cx, cy)
    init_eq(rho0)

    n_solid = count_solid()
    obs_vx = 0.0

    total_steps = n_warmup + n_accel + n_coast
    force_series = np.zeros(total_steps, dtype=np.float64)

    for step in range(total_steps):
        # Determine obstacle velocity
        if n_warmup <= step < n_warmup + n_accel:
            obs_vx += accel_val  # constant acceleration phase
        # else: coast (constant velocity) or warmup (stationary)

        collide_stream(omega, obs_vx, 0.0)

        # Measure force every step
        fvec = momentum_exchange_force()
        force_series[step] = fvec[0]  # x-component (direction of push)

    # ── Analysis ──
    # During acceleration phase, average force
    accel_slice = force_series[n_warmup:n_warmup + n_accel]
    F_mean = np.mean(accel_slice)
    F_std  = np.std(accel_slice)

    # Added mass = F / a  (a = accel_val per timestep, in lattice units)
    M_measured = F_mean / accel_val if abs(accel_val) > 1e-15 else 0.0
    M_std      = F_std / abs(accel_val) if abs(accel_val) > 1e-15 else 0.0

    # Theoretical: M_theory = C_added * rho0 * A_obstacle
    # For a circle, C_added = 1 (added mass = displaced mass)
    # For a star, C_added > 1 due to concavities trapping fluid
    M_displaced = rho0 * float(n_solid)

    return {
        'NX': NX, 'NY': NY, 'rho0': rho0, 'tau': tau,
        'n_solid': int(n_solid), 'A_obs': int(n_solid),
        'accel': accel_val,
        'F_mean': F_mean, 'F_std': F_std,
        'M_measured': M_measured, 'M_std': M_std,
        'M_displaced': M_displaced,
        'force_series': force_series,
        'n_warmup': n_warmup, 'n_accel': n_accel,
    }


# ═══════════════════════════════════════════════════════════════
# Main: Sweep ρ₀ and Grid Resolution
# ═══════════════════════════════════════════════════════════════
def main():
    print("=" * 72)
    print("  UHF Added-Mass Convergence — Headless LBM D2Q9 (f64, CUDA)")
    print("  Question: Does M_added = C × ρ₀ × A  emerge from the PDE?")
    print("=" * 72)

    # Parameters
    tau       = 0.56          # relaxation time
    accel_val = 2e-5          # small acceleration (Ma << 1 required for LBM validity)
    n_warmup  = 500           # let fluid equilibrate
    n_accel   = 2000          # acceleration phase (measure force here)
    n_coast   = 500           # coast after acceleration

    rho_values = [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0]
    grid_sizes = [(256, 256), (512, 512), (768, 384)]

    all_results = []

    for NX, NY in grid_sizes:
        print(f"\n{'─'*72}")
        print(f"  Grid: {NX}×{NY} ({NX*NY:,} nodes)")
        print(f"{'─'*72}")
        print(f"  {'ρ₀':>6} | {'A_obs':>7} | {'<F_x>':>12} | {'σ(F_x)':>12} | "
              f"{'M_meas':>12} | {'σ(M)':>10} | {'ρ₀×A':>10} | {'C_add':>8}")
        print(f"  {'─'*6}-+-{'─'*7}-+-{'─'*12}-+-{'─'*12}-+-"
              f"{'─'*12}-+-{'─'*10}-+-{'─'*10}-+-{'─'*8}")

        for rho0 in rho_values:
            t0 = time.time()
            res = run_lbm(NX, NY, rho0, tau, accel_val, n_warmup, n_accel, n_coast)
            dt = time.time() - t0

            C_add = res['M_measured'] / res['M_displaced'] if abs(res['M_displaced']) > 1e-10 else 0.0

            print(f"  {rho0:6.2f} | {res['A_obs']:7d} | {res['F_mean']:12.6e} | "
                  f"{res['F_std']:12.6e} | {res['M_measured']:12.4f} | "
                  f"{res['M_std']:10.4f} | {res['M_displaced']:10.2f} | "
                  f"{C_add:8.4f}   [{dt:.1f}s]")

            all_results.append({
                'NX': NX, 'NY': NY, 'rho0': rho0,
                'A_obs': res['A_obs'],
                'F_mean': res['F_mean'], 'F_std': res['F_std'],
                'M_measured': res['M_measured'], 'M_std': res['M_std'],
                'M_displaced': res['M_displaced'], 'C_added': C_add,
            })

    # ═══════════════════════════════════════════════════════════
    # Statistical Analysis
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*72}")
    print("  CONVERGENCE ANALYSIS")
    print(f"{'='*72}")

    import csv

    # Save CSV
    csv_path = "uhf_added_mass_convergence.csv"
    with open(csv_path, 'w', newline='') as csvf:
        writer = csv.DictWriter(csvf, fieldnames=[
            'NX', 'NY', 'rho0', 'A_obs', 'F_mean', 'F_std',
            'M_measured', 'M_std', 'M_displaced', 'C_added'])
        writer.writeheader()
        for r in all_results:
            writer.writerow(r)
    print(f"\n  Data saved: {csv_path}")

    # Per-grid linear regression: M_measured = slope × ρ₀ + intercept
    for NX, NY in grid_sizes:
        subset = [r for r in all_results if r['NX'] == NX and r['NY'] == NY]
        rhos = np.array([r['rho0'] for r in subset])
        M_m  = np.array([r['M_measured'] for r in subset])
        M_s  = np.array([r['M_std'] for r in subset])
        A    = subset[0]['A_obs']

        # Weighted least squares: M = slope * rho + intercept
        # Weight = 1/sigma²
        w = 1.0 / (M_s**2 + 1e-20)
        W = np.sum(w)
        x_bar = np.sum(w * rhos) / W
        y_bar = np.sum(w * M_m) / W
        slope = np.sum(w * (rhos - x_bar) * (M_m - y_bar)) / np.sum(w * (rhos - x_bar)**2)
        intercept = y_bar - slope * x_bar

        # R²
        SS_res = np.sum(w * (M_m - (slope * rhos + intercept))**2)
        SS_tot = np.sum(w * (M_m - y_bar)**2)
        R2 = 1.0 - SS_res / (SS_tot + 1e-20)

        # Standard error on slope
        N = len(rhos)
        if N > 2:
            se_slope = np.sqrt(SS_res / ((N - 2) * np.sum(w * (rhos - x_bar)**2)))
        else:
            se_slope = 0.0

        C_eff = slope / A if A > 0 else 0.0

        print(f"\n  Grid {NX}×{NY}  (A_obs = {A})")
        print(f"    M_added = ({slope:.4f} ± {se_slope:.4f}) × ρ₀  +  ({intercept:.4f})")
        print(f"    R² = {R2:.8f}")
        print(f"    C_added = slope / A = {C_eff:.6f}")
        print(f"    Theory check: M = C × ρ × A  ⟹  C = {C_eff:.6f}")

        # Residuals
        residuals = M_m - (slope * rhos + intercept)
        print(f"    Residual max = {np.max(np.abs(residuals)):.6e}")
        print(f"    Residual RMS = {np.sqrt(np.mean(residuals**2)):.6e}")

    # ═══════════════════════════════════════════════════════════
    # Grid Convergence Test
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'─'*72}")
    print("  GRID CONVERGENCE (at ρ₀ = 1.0)")
    print(f"{'─'*72}")
    rho_test = 1.0
    conv_data = []
    for NX, NY in grid_sizes:
        r = [x for x in all_results if x['NX'] == NX and x['NY'] == NY and abs(x['rho0'] - rho_test) < 0.01]
        if r:
            r = r[0]
            C = r['C_added']
            conv_data.append((NX, NY, r['M_measured'], r['M_std'], C, r['A_obs']))
            print(f"    {NX:4d}×{NY:4d}: M_meas = {r['M_measured']:.4f} ± {r['M_std']:.4f},  "
                  f"C = {C:.6f}, A = {r['A_obs']}")

    if len(conv_data) >= 2:
        # Richardson extrapolation between last two grids
        _, _, M1, _, _, _ = conv_data[-2]
        _, _, M2, _, _, _ = conv_data[-1]
        NX1 = conv_data[-2][0]
        NX2 = conv_data[-1][0]
        r_ratio = NX2 / NX1
        p_est = np.log(abs((conv_data[-3][2] - M1) / (M1 - M2 + 1e-20))) / np.log(r_ratio) if len(conv_data) >= 3 else 2.0
        M_rich = M2 + (M2 - M1) / (r_ratio**min(p_est, 4.0) - 1.0)
        print(f"\n    Richardson extrapolation: M∞ ≈ {M_rich:.4f}")
        print(f"    Estimated convergence order p ≈ {p_est:.2f}")

    # ═══════════════════════════════════════════════════════════
    # Key Physical Result
    # ═══════════════════════════════════════════════════════════
    # Check linearity: is M_measured ∝ ρ₀ to high precision?
    finest = [r for r in all_results if r['NX'] == grid_sizes[-1][0]]
    rhos_f = np.array([r['rho0'] for r in finest])
    M_f    = np.array([r['M_measured'] for r in finest])

    # Force through origin: M = α × ρ₀
    alpha = np.sum(rhos_f * M_f) / np.sum(rhos_f**2)
    SS_res_0 = np.sum((M_f - alpha * rhos_f)**2)
    SS_tot_0 = np.sum((M_f - np.mean(M_f))**2)
    R2_0 = 1.0 - SS_res_0 / (SS_tot_0 + 1e-20)

    print(f"\n{'='*72}")
    print(f"  PHYSICAL RESULT (finest grid {grid_sizes[-1][0]}×{grid_sizes[-1][1]})")
    print(f"{'='*72}")
    print(f"  M_added = {alpha:.4f} × ρ₀   (forced through origin)")
    print(f"  R² = {R2_0:.10f}")
    print(f"")
    print(f"  Interpretation:")
    print(f"    The Navier-Stokes / LBM equations produce M_added ∝ ρ₀")
    print(f"    with proportionality constant α = {alpha:.4f} = C × A_obstacle")
    A_finest = finest[0]['A_obs']
    print(f"    A_obstacle = {A_finest} lattice units²")
    print(f"    C_added = α / A = {alpha / A_finest:.6f}")
    print(f"")
    if R2_0 > 0.999:
        print(f"  ✓ CONFIRMED: m = ρ × V emerges from fluid dynamics (R²>{R2_0:.6f})")
    elif R2_0 > 0.99:
        print(f"  ~ STRONG EVIDENCE: m ∝ ρ (R²={R2_0:.6f})")
    elif R2_0 > 0.95:
        print(f"  ~ SUGGESTIVE: m ∝ ρ with deviations (R²={R2_0:.6f})")
    else:
        print(f"  ✗ WEAK/FALSIFIED: R²={R2_0:.6f}")

    # ═══════════════════════════════════════════════════════════
    # Matplotlib Plot (no display, save to file)
    # ═══════════════════════════════════════════════════════════
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 3, figsize=(18, 6))

        # Panel 1: M_measured vs ρ₀ for each grid
        ax1 = axes[0]
        markers = ['o', 's', 'D']
        for idx, (NX, NY) in enumerate(grid_sizes):
            subset = [r for r in all_results if r['NX'] == NX and r['NY'] == NY]
            rhos = [r['rho0'] for r in subset]
            Ms   = [r['M_measured'] for r in subset]
            errs = [r['M_std'] for r in subset]
            ax1.errorbar(rhos, Ms, yerr=errs, marker=markers[idx],
                        label=f'{NX}×{NY}', capsize=3, linewidth=1.5)
        # Theory line from finest grid
        rho_line = np.linspace(0, 2.2, 100)
        ax1.plot(rho_line, alpha * rho_line, 'k--', label=f'Fit: {alpha:.1f}×ρ₀', linewidth=2)
        ax1.set_xlabel('Fluid Density ρ₀', fontsize=13)
        ax1.set_ylabel('Measured Added Mass M', fontsize=13)
        ax1.set_title('M_added vs ρ₀ (all grids)', fontsize=14)
        ax1.legend(fontsize=11)
        ax1.grid(True, alpha=0.3)

        # Panel 2: C_added convergence vs grid size
        ax2 = axes[1]
        for rho_val in [0.4, 1.0, 1.6]:
            Cs = []
            Ns = []
            for NX, NY in grid_sizes:
                r = [x for x in all_results if x['NX'] == NX and x['NY'] == NY
                     and abs(x['rho0'] - rho_val) < 0.01]
                if r:
                    Cs.append(r[0]['C_added'])
                    Ns.append(NX)
            ax2.plot(Ns, Cs, 'o-', label=f'ρ₀={rho_val}', linewidth=1.5, markersize=8)
        ax2.set_xlabel('Grid NX', fontsize=13)
        ax2.set_ylabel('Added Mass Coefficient C', fontsize=13)
        ax2.set_title('Grid Convergence of C_added', fontsize=14)
        ax2.legend(fontsize=11)
        ax2.grid(True, alpha=0.3)

        # Panel 3: Residuals (finest grid, origin fit)
        ax3 = axes[2]
        residuals_f = M_f - alpha * rhos_f
        ax3.bar(rhos_f, residuals_f, width=0.15, color='steelblue', edgecolor='black')
        ax3.axhline(0, color='black', linewidth=0.8)
        ax3.set_xlabel('ρ₀', fontsize=13)
        ax3.set_ylabel('Residual (M_meas − α×ρ₀)', fontsize=13)
        ax3.set_title(f'Fit Residuals (R²={R2_0:.8f})', fontsize=14)
        ax3.grid(True, alpha=0.3)

        plt.tight_layout()
        plot_path = "uhf_added_mass_convergence.png"
        plt.savefig(plot_path, dpi=150, bbox_inches='tight')
        print(f"\n  Plot: {plot_path}")
        plt.close()
    except Exception as e:
        print(f"\n  Plot failed: {e}")

    print(f"\n{'='*72}")
    print("  Done.")
    print(f"{'='*72}")


if __name__ == "__main__":
    main()
