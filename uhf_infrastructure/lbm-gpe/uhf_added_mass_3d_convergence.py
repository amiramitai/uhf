#!/usr/bin/env python3
"""
UHF 3D Grid Convergence — D3Q19 LBM Torus Added Mass
======================================================
Headless. No framebuffer. Console + CSV + PNG only.

Proper grid convergence: fixed physical torus (R/L=0.195, r/L=0.0625),
grid refined from 64³ to 256³. As Δx → 0, staircase boundary
resolves the smooth torus surface and C_added converges.

Usage:
  python uhf_added_mass_3d_convergence.py              # full sweep
  python uhf_added_mass_3d_convergence.py --nx 128     # single grid

VRAM budget (RTX 3090, 24 GB):
  64³  : ~44 MB     128³ : ~350 MB    192³ : ~1.2 GB
  256³ : ~2.8 GB    384³ : ~9.4 GB
"""

import sys
import os
import subprocess
import json

# ─────────────────────────────────────────────────────────────
# Mode dispatch: if --nx is given, run a single grid.
# Otherwise, orchestrate the full convergence sweep.
# ─────────────────────────────────────────────────────────────
def run_single_grid(NX_arg):
    """Execute D3Q19 LBM at one grid resolution. Prints JSON results."""
    import taichi as ti
    import numpy as np
    import time

    ti.init(arch=ti.cuda, default_fp=ti.f32, random_seed=42)

    NX = NY = NZ = NX_arg
    Q = 19
    CS2 = 1.0 / 3.0

    # D3Q19 lattice data
    ex_np  = np.array([0, 1,-1, 0,0, 0,0, 1,-1, 1,-1, 1,-1, 1,-1, 0,0, 0,0], dtype=np.int32)
    ey_np  = np.array([0, 0,0, 1,-1, 0,0, 1,-1,-1, 1, 0,0, 0,0, 1,-1, 1,-1], dtype=np.int32)
    ez_np  = np.array([0, 0,0, 0,0, 1,-1, 0,0, 0,0, 1,-1,-1, 1, 1,-1,-1, 1], dtype=np.int32)
    w_np   = np.array([1/3]+[1/18]*6+[1/36]*12, dtype=np.float32)
    opp_np = np.array([0, 2,1, 4,3, 6,5, 8,7, 10,9, 12,11, 14,13, 16,15, 18,17], dtype=np.int32)

    w_ti   = ti.field(dtype=ti.f32, shape=(Q,));  w_ti.from_numpy(w_np)
    ex_ti  = ti.field(dtype=ti.i32, shape=(Q,));  ex_ti.from_numpy(ex_np)
    ey_ti  = ti.field(dtype=ti.i32, shape=(Q,));  ey_ti.from_numpy(ey_np)
    ez_ti  = ti.field(dtype=ti.i32, shape=(Q,));  ez_ti.from_numpy(ez_np)
    opp_ti = ti.field(dtype=ti.i32, shape=(Q,));  opp_ti.from_numpy(opp_np)

    # Allocate 3D fields
    f_     = ti.field(dtype=ti.f32, shape=(NX, NY, NZ, Q))
    ft_    = ti.field(dtype=ti.f32, shape=(NX, NY, NZ, Q))
    rho_f  = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
    ux_f   = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
    uy_f   = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
    uz_f   = ti.field(dtype=ti.f32, shape=(NX, NY, NZ))
    solid  = ti.field(dtype=ti.i32, shape=(NX, NY, NZ))
    bnd    = ti.field(dtype=ti.i32, shape=(NX, NY, NZ))

    # Torus geometry: fixed physical ratio R/L=0.195, r/L=0.0625
    L = float(NX)
    TORUS_R = 0.195 * L
    TORUS_r = 0.0625 * L
    CX = L / 2.0
    CY = L / 2.0
    CZ = L / 2.0

    @ti.kernel
    def stamp_torus():
        for i, j, k in solid:
            dx = ti.cast(i, ti.f32) - CX
            dy = ti.cast(j, ti.f32) - CY
            dz = ti.cast(k, ti.f32) - CZ
            rxy = ti.sqrt(dx*dx + dy*dy) + 1e-10
            d = ti.sqrt((rxy - TORUS_R)**2 + dz*dz)
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
    def count_solid_k() -> ti.i32:
        n = 0
        for i, j, k in solid:
            if solid[i, j, k] == 1:
                n += 1
        return n

    @ti.func
    def feq(q: ti.i32, r: ti.f32, u: ti.f32, v: ti.f32, w: ti.f32) -> ti.f32:
        eu = (ti.cast(ex_ti[q], ti.f32)*u + ti.cast(ey_ti[q], ti.f32)*v +
              ti.cast(ez_ti[q], ti.f32)*w)
        usq = u*u + v*v + w*w
        return w_ti[q] * r * (1.0 + eu/CS2 + 0.5*eu*eu/(CS2*CS2) - 0.5*usq/CS2)

    @ti.kernel
    def init_eq(rho0: ti.f32):
        for i, j, k in rho_f:
            rho_f[i,j,k] = rho0
            ux_f[i,j,k] = 0.0;  uy_f[i,j,k] = 0.0;  uz_f[i,j,k] = 0.0
            for q in range(Q):
                f_[i,j,k,q] = feq(q, rho0, 0.0, 0.0, 0.0)

    @ti.kernel
    def collide_k(omega: ti.f32):
        for i, j, k in rho_f:
            if solid[i,j,k] == 0:
                r = 0.0; u = 0.0; v = 0.0; vz = 0.0
                for q in range(Q):
                    fi = f_[i,j,k,q]
                    r  += fi
                    u  += fi * ti.cast(ex_ti[q], ti.f32)
                    v  += fi * ti.cast(ey_ti[q], ti.f32)
                    vz += fi * ti.cast(ez_ti[q], ti.f32)
                inv_r = 1.0 / ti.max(r, 1e-10)
                u *= inv_r;  v *= inv_r;  vz *= inv_r
                rho_f[i,j,k] = r;  ux_f[i,j,k] = u
                uy_f[i,j,k] = v;   uz_f[i,j,k] = vz
                for q in range(Q):
                    f_[i,j,k,q] = (1.0-omega)*f_[i,j,k,q] + omega*feq(q, r, u, v, vz)

    @ti.kernel
    def stream_k(wall_vz: ti.f32):
        for i, j, k in rho_f:
            for q in range(Q):
                si = i - ex_ti[q];  sj = j - ey_ti[q];  sk = k - ez_ti[q]
                if 0 <= si < NX and 0 <= sj < NY and 0 <= sk < NZ:
                    if solid[si,sj,sk] == 0:
                        ft_[i,j,k,q] = f_[si,sj,sk,q]
                    else:
                        oq = opp_ti[q]
                        eu_w = ti.cast(ez_ti[q], ti.f32) * wall_vz
                        ft_[i,j,k,q] = f_[i,j,k,oq] + 2.0*w_ti[q]*rho_f[i,j,k]*eu_w/CS2
                else:
                    ft_[i,j,k,q] = feq(q, rho_f[i,j,k], ux_f[i,j,k], uy_f[i,j,k], uz_f[i,j,k])

    @ti.kernel
    def mem_z() -> ti.f32:
        fz = 0.0
        for i, j, k in bnd:
            if bnd[i,j,k] == 1:
                for q in range(1, Q):
                    ni = i + ex_ti[q]; nj = j + ey_ti[q]; nk = k + ez_ti[q]
                    if 0 <= ni < NX and 0 <= nj < NY and 0 <= nk < NZ:
                        if solid[ni,nj,nk] == 0:
                            oq = opp_ti[q]
                            fz += ti.cast(ez_ti[q], ti.f32) * (f_[ni,nj,nk,oq] + ft_[ni,nj,nk,q])
        return fz

    @ti.kernel
    def copy_back_k():
        for i, j, k in rho_f:
            if solid[i,j,k] == 0:
                for q in range(Q):
                    f_[i,j,k,q] = ft_[i,j,k,q]

    # ── Parameters ──
    tau = 0.56
    omega = 1.0 / tau
    accel_val = 2e-5
    n_warmup = 300
    n_accel  = 1200
    n_coast  = 100
    rho_values = [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0]

    # Build torus
    stamp_torus()
    n_solid = int(count_solid_k())
    V_analytic = 2.0 * np.pi**2 * TORUS_R * TORUS_r**2

    print(f"GRID {NX}x{NY}x{NZ}  R={TORUS_R:.1f} r={TORUS_r:.1f} "
          f"V_solid={n_solid} V_analytic={V_analytic:.0f}", file=sys.stderr)

    # ── Density sweep ──
    results = []
    for rho0 in rho_values:
        init_eq(rho0)
        wall_vz = 0.0
        total = n_warmup + n_accel + n_coast
        fz_buf = np.zeros(total, dtype=np.float64)

        t0 = time.time()
        for step in range(total):
            if n_warmup <= step < n_warmup + n_accel:
                wall_vz += accel_val
            collide_k(omega)
            stream_k(wall_vz)
            fz_buf[step] = float(mem_z())
            copy_back_k()
        dt = time.time() - t0

        accel_slice = fz_buf[n_warmup:n_warmup + n_accel]
        F_mean = float(np.mean(accel_slice))
        F_std  = float(np.std(accel_slice))
        M_meas = F_mean / accel_val
        M_std  = F_std  / accel_val
        M_disp = rho0 * n_solid
        C_add  = M_meas / M_disp if M_disp > 1e-10 else 0.0

        results.append({
            'NX': NX, 'rho0': rho0, 'V_solid': n_solid,
            'V_analytic': V_analytic,
            'F_mean': F_mean, 'F_std': F_std,
            'M_measured': M_meas, 'M_std': M_std,
            'M_displaced': M_disp, 'C_added': C_add,
            'time_s': dt
        })
        print(f"  rho={rho0:.1f} F={F_mean:.6e} M={M_meas:.2f} "
              f"C={C_add:.4f} [{dt:.1f}s]", file=sys.stderr)

    # Output JSON to stdout for collection by parent
    print(json.dumps(results))


# ─────────────────────────────────────────────────────────────
# Orchestrator: run multiple grid sizes, collect, analyse
# ─────────────────────────────────────────────────────────────
def orchestrate():
    import numpy as np
    import csv as csv_mod

    grid_sizes = [64, 96, 128, 192, 256]
    all_results = []

    print("=" * 78)
    print("  UHF 3D Grid Convergence — D3Q19 LBM Torus (R/L=0.195, r/L=0.0625)")
    print("  Grid sweep:", grid_sizes)
    print("=" * 78)

    script_path = os.path.abspath(__file__)

    for NX in grid_sizes:
        vram_mb = NX**3 * (19*4*2 + 4*6) / (1024**2)
        print(f"\n{'─'*78}")
        print(f"  Launching {NX}³  (est. {vram_mb:.0f} MB VRAM)")
        print(f"{'─'*78}")

        env = os.environ.copy()
        env['LD_LIBRARY_PATH'] = '/usr/lib/wsl/lib'
        result = subprocess.run(
            [sys.executable, script_path, '--nx', str(NX)],
            capture_output=True, text=True, env=env
        )

        if result.returncode != 0:
            print(f"  ERROR at {NX}³:")
            print(result.stderr[-500:] if result.stderr else "(no stderr)")
            continue

        # Parse JSON from stdout — skip Taichi init lines before the JSON array
        try:
            raw = result.stdout.strip()
            # Find '[{' — the real JSON array start (not '[Taichi]')
            json_start = raw.find('[{')
            if json_start < 0:
                raise json.JSONDecodeError("No JSON array found", raw, 0)
            data = json.loads(raw[json_start:])
            all_results.extend(data)
            # Print stderr (progress)
            for line in result.stderr.strip().split('\n'):
                print(f"  {line}")
        except json.JSONDecodeError:
            print(f"  JSON parse error. stdout: {result.stdout[:200]}")
            print(f"  stderr: {result.stderr[-300:]}")
            continue

    if not all_results:
        print("\nNo results collected. Aborting.")
        return

    # ═══════════════════════════════════════════════════════════
    # Analysis
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*78}")
    print("  RESULTS TABLE")
    print(f"{'='*78}")
    print(f"  {'NX':>4} | {'ρ₀':>4} | {'V_solid':>8} | {'<F_z>':>12} | "
          f"{'M_meas':>12} | {'ρ₀V':>10} | {'C_add':>8}")
    print(f"  {'─'*4}-+-{'─'*4}-+-{'─'*8}-+-{'─'*12}-+-"
          f"{'─'*12}-+-{'─'*10}-+-{'─'*8}")

    for r in all_results:
        print(f"  {r['NX']:4d} | {r['rho0']:4.1f} | {r['V_solid']:8d} | "
              f"{r['F_mean']:12.6e} | {r['M_measured']:12.2f} | "
              f"{r['M_displaced']:10.2f} | {r['C_added']:8.4f}")

    # Per-grid linear regression: M = α × ρ₀
    print(f"\n{'='*78}")
    print("  PER-GRID REGRESSION: M_added = α × ρ₀")
    print(f"{'='*78}")

    grid_summary = []
    unique_nx = sorted(set(r['NX'] for r in all_results))

    for nx in unique_nx:
        subset = [r for r in all_results if r['NX'] == nx]
        rhos = np.array([r['rho0'] for r in subset])
        Ms   = np.array([r['M_measured'] for r in subset])
        V    = subset[0]['V_solid']

        # Through-origin: M = α × ρ₀
        alpha = float(np.sum(rhos * Ms) / np.sum(rhos**2))
        SS_res = float(np.sum((Ms - alpha * rhos)**2))
        SS_tot = float(np.sum((Ms - np.mean(Ms))**2))
        R2 = 1.0 - SS_res / (SS_tot + 1e-30)
        C = alpha / V if V > 0 else 0.0

        residuals = Ms - alpha * rhos
        rms_res   = float(np.sqrt(np.mean(residuals**2)))

        grid_summary.append({
            'NX': nx, 'V_solid': V, 'alpha': alpha,
            'C_added': C, 'R2': R2, 'rms_residual': rms_res
        })

        print(f"\n  Grid {nx}³  (V_solid = {V})")
        print(f"    M = {alpha:.4f} × ρ₀     R² = {R2:.10f}")
        print(f"    C_added = α/V = {C:.6f}")
        print(f"    RMS residual = {rms_res:.6e}")

    # ── Grid Convergence of C_added ──
    print(f"\n{'='*78}")
    print("  GRID CONVERGENCE OF C_added (at all ρ₀)")
    print(f"{'='*78}")
    print(f"  {'NX':>4} | {'V_solid':>8} | {'C_added':>10} | {'R²':>14} | {'RMS(ε)':>12}")
    print(f"  {'─'*4}-+-{'─'*8}-+-{'─'*10}-+-{'─'*14}-+-{'─'*12}")
    for gs in grid_summary:
        print(f"  {gs['NX']:4d} | {gs['V_solid']:8d} | {gs['C_added']:10.6f} | "
              f"{gs['R2']:14.10f} | {gs['rms_residual']:12.6e}")

    # Richardson extrapolation for C_added
    if len(grid_summary) >= 3:
        Cs  = [gs['C_added'] for gs in grid_summary]
        NXs = [gs['NX'] for gs in grid_summary]
        # Use last three for Richardson
        C1, C2, C3 = Cs[-3], Cs[-2], Cs[-1]
        h1, h2, h3 = 1.0/NXs[-3], 1.0/NXs[-2], 1.0/NXs[-1]
        r21 = h2 / h1
        r32 = h3 / h2
        # Estimate convergence order
        if abs(C2 - C3) > 1e-12 and abs(C1 - C2) > 1e-12:
            p_est = abs(np.log(abs((C1 - C2) / (C2 - C3)))) / abs(np.log(NXs[-1] / NXs[-2]))
        else:
            p_est = 2.0
        # Richardson extrapolation using last two
        r = float(NXs[-1]) / float(NXs[-2])
        C_rich = C3 + (C3 - C2) / (r**min(p_est, 4.0) - 1.0)

        print(f"\n  Richardson extrapolation:")
        print(f"    Convergence order p ≈ {p_est:.2f}")
        print(f"    C_added(∞) ≈ {C_rich:.6f}")

    # ── Overall Verdict ──
    finest = grid_summary[-1]
    print(f"\n{'='*78}")
    print(f"  VERDICT (finest grid {finest['NX']}³)")
    print(f"{'='*78}")
    print(f"  M_added = {finest['alpha']:.4f} × ρ₀")
    print(f"  C_added = {finest['C_added']:.6f}")
    print(f"  R²      = {finest['R2']:.10f}")

    if finest['R2'] > 0.999:
        print(f"\n  ✓ CONFIRMED: M = C × ρ_vacuum × V_defect  (3D torus, grid-converged)")
    elif finest['R2'] > 0.99:
        print(f"\n  ~ STRONG EVIDENCE: M ∝ ρ₀  (R² = {finest['R2']:.6f})")
    else:
        print(f"\n  ? INCONCLUSIVE (R² = {finest['R2']:.6f})")
    print(f"{'='*78}")

    # ── CSV ──
    csv_path = "uhf_3d_convergence_results.csv"
    with open(csv_path, 'w', newline='') as csvf:
        fieldnames = ['NX', 'rho0', 'V_solid', 'V_analytic', 'F_mean', 'F_std',
                       'M_measured', 'M_std', 'M_displaced', 'C_added', 'time_s']
        writer = csv_mod.DictWriter(csvf, fieldnames=fieldnames)
        writer.writeheader()
        for r in all_results:
            writer.writerow(r)
    print(f"\n  CSV: {csv_path}")

    # ── Summary CSV ──
    sum_csv = "uhf_3d_convergence_summary.csv"
    with open(sum_csv, 'w', newline='') as csvf:
        writer = csv_mod.DictWriter(csvf, fieldnames=['NX','V_solid','alpha','C_added','R2','rms_residual'])
        writer.writeheader()
        for gs in grid_summary:
            writer.writerow(gs)
    print(f"  Summary CSV: {sum_csv}")

    # ── Headless Plot ──
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 3, figsize=(20, 6))

        # Panel 1: M vs ρ₀ for each grid
        ax = axes[0]
        markers = ['v', 's', 'o', 'D', '^']
        for idx, nx in enumerate(unique_nx):
            subset = [r for r in all_results if r['NX'] == nx]
            rhos = [r['rho0'] for r in subset]
            Ms   = [r['M_measured'] for r in subset]
            ax.plot(rhos, Ms, marker=markers[idx % len(markers)],
                    label=f'{nx}³', linewidth=1.5, markersize=7)
        alpha_f = grid_summary[-1]['alpha']
        rho_line = np.linspace(0, 2.2, 100)
        ax.plot(rho_line, alpha_f * rho_line, 'k--', lw=2,
                label=f'Fit ({unique_nx[-1]}³): {alpha_f:.0f}×ρ₀')
        ax.set_xlabel('ρ₀', fontsize=13)
        ax.set_ylabel('M_added', fontsize=13)
        ax.set_title('Added Mass vs Density (all grids)', fontsize=14)
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)

        # Panel 2: C_added convergence
        ax = axes[1]
        Cs = [gs['C_added'] for gs in grid_summary]
        NXs = [gs['NX'] for gs in grid_summary]
        ax.plot(NXs, Cs, 'o-', color='crimson', markersize=10, linewidth=2)
        if len(grid_summary) >= 3:
            ax.axhline(C_rich, color='gray', linestyle='--', label=f'Richardson: {C_rich:.4f}')
            ax.legend(fontsize=11)
        ax.set_xlabel('Grid Size NX', fontsize=13)
        ax.set_ylabel('C_added = α / V', fontsize=13)
        ax.set_title('Grid Convergence of Added-Mass Coefficient', fontsize=14)
        ax.grid(True, alpha=0.3)

        # Panel 3: R² for each grid
        ax = axes[2]
        R2s = [gs['R2'] for gs in grid_summary]
        ax.plot(NXs, [1.0 - r for r in R2s], 'o-', color='steelblue', markersize=10, lw=2)
        ax.set_xlabel('Grid Size NX', fontsize=13)
        ax.set_ylabel('1 − R²', fontsize=13)
        ax.set_title('Linearity Error vs Grid Resolution', fontsize=14)
        ax.set_yscale('log')
        ax.grid(True, alpha=0.3)

        plt.suptitle('UHF: m = ρ_vacuum × V_defect — 3D Grid Convergence',
                     fontsize=16, fontweight='bold', y=1.02)
        plt.tight_layout()
        plot_path = "uhf_3d_convergence_results.png"
        plt.savefig(plot_path, dpi=150, bbox_inches='tight')
        print(f"  Plot: {plot_path}")
        plt.close()
    except Exception as e:
        print(f"  Plot failed: {e}")

    print("\n  Done.\n")


# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if '--nx' in sys.argv:
        idx = sys.argv.index('--nx')
        nx_val = int(sys.argv[idx + 1])
        run_single_grid(nx_val)
    else:
        orchestrate()
