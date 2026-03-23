#!/usr/bin/env python3
"""Dirac-Torsion GUE Solver v1.0
1D massless Dirac fermion on a ring coupled to axial torsion flux K(x).

Hamiltonian: H = -i σ_x ∂_x + σ_z K(x)

Block structure (2N × 2N):
  H = [[ +diag(K),   -i D  ],
       [ -i D,       -diag(K) ]]

where D is the central-difference derivative (anti-symmetric → -iD Hermitian).

Time-Reversal broken by axial coupling → Symmetry Class A → GUE.
Target: ⟨r⟩ ≈ 0.603 for the vortex case.
"""

import torch
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import time
import json
import os

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Device: {device} ({torch.cuda.get_device_name()})")

# ======================== PARAMETERS ========================
N = 1024
dx = 2 * np.pi / N
L = 2 * np.pi

print(f"\n  Dirac-Torsion Solver Parameters:")
print(f"    N={N}, dx={dx:.6f}, L={L:.6f}")
print(f"    Matrix: {2*N} x {2*N} = {(2*N)**2} elements")
print(f"    Memory: ~{(2*N)**2 * 8 / 1e6:.1f} MB (complex64)")

# ======================== GRID ========================
x = torch.linspace(0, L - dx, N, device=device, dtype=torch.float64)


def run_dirac_analysis(K_profile, label, n_extract=100):
    """Full Dirac eigenvalue analysis for a given torsion profile K(x)."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    t0 = time.time()
    K = K_profile.to(torch.float32)

    n_nonzero = int((torch.abs(K) > 1e-10).sum())
    print(f"    K: min={float(K.min()):.2f}, max={float(K.max()):.2f}, "
          f"nonzero={n_nonzero}/{N}")

    # --- Build 2N x 2N Dirac Hamiltonian on GPU ---
    print(f"    Building {2*N}x{2*N} Dirac matrix...")
    t_build = time.time()

    H_dirac = torch.zeros(2 * N, 2 * N, device=device, dtype=torch.complex64)
    idx = torch.arange(N, device=device)

    # Central-difference derivative matrix D (anti-symmetric, periodic)
    # D_{j,j+1} = +1/(2dx),  D_{j,j-1} = -1/(2dx)
    D = torch.zeros(N, N, device=device, dtype=torch.complex64)
    D[idx, (idx + 1) % N] = 1.0 / (2 * dx)
    D[idx, (idx - 1) % N] = -1.0 / (2 * dx)

    # -i D  (Hermitian: (-iD)† = iD^T = i(-D) = -iD  ✓)
    neg_iD = -1j * D

    # σ_z K(x) → diagonal blocks: +K(x) top-left, -K(x) bottom-right
    K_c = K.to(torch.complex64)

    # Assemble:
    # [[ +diag(K),   -iD     ],
    #  [ -iD,       -diag(K) ]]
    H_dirac[:N, :N] = torch.diag(K_c)          # +σ_z K  (top-left)
    H_dirac[N:, N:] = torch.diag(-K_c)         # -σ_z K  (bottom-right)
    H_dirac[:N, N:] = neg_iD                    # -iσ_x ∂  (top-right)
    H_dirac[N:, :N] = neg_iD                    # -iσ_x ∂  (bottom-left)

    # Hermiticity check before enforcement
    herm_err_raw = float(torch.max(torch.abs(H_dirac - H_dirac.conj().T)))
    print(f"    Hermiticity (raw):  max|H - H†| = {herm_err_raw:.2e}")

    # Enforce exact Hermiticity
    H_dirac = 0.5 * (H_dirac + H_dirac.conj().T)

    herm_err = float(torch.max(torch.abs(H_dirac - H_dirac.conj().T)))
    print(f"    Hermiticity (enforced): max|H - H†| = {herm_err:.2e}")

    t_built = time.time() - t_build
    print(f"    Matrix built ({t_built:.2f}s)")

    del D, neg_iD
    torch.cuda.empty_cache()

    # --- Diagonalize (eigvalsh: real eigenvalues, optimized for Hermitian) ---
    print(f"    Diagonalizing (eigvalsh)...")
    t_diag = time.time()

    evals = torch.linalg.eigvalsh(H_dirac)  # guaranteed real, sorted ascending
    evals_np = evals.cpu().numpy()

    t_diaged = time.time() - t_diag
    print(f"    Diagonalization done ({t_diaged:.2f}s)")

    del H_dirac
    torch.cuda.empty_cache()

    # --- Process eigenvalues ---
    n_total = len(evals_np)
    n_zero = int(np.sum(np.abs(evals_np) < 1e-5))
    evals_pos = np.sort(evals_np[evals_np > 1e-5])
    evals_neg = np.sort(evals_np[evals_np < -1e-5])
    n_pos = len(evals_pos)

    print(f"    Spectrum: {n_pos} positive, {len(evals_neg)} negative, {n_zero} near-zero")
    print(f"    E range: [{evals_np[0]:.4f}, {evals_np[-1]:.4f}]")

    n_use = min(n_extract, n_pos)
    lowest = evals_pos[:n_use]

    print(f"\n    Lowest 10 positive eigenvalues:")
    for i in range(min(10, n_use)):
        print(f"      E_{i+1:2d} = {lowest[i]:.10f}")

    # --- Spectral statistics ---
    if n_use >= 10:
        # Unfold: normalize spacings to mean 1
        spacings_raw = np.diff(lowest)
        mean_s = np.mean(spacings_raw)
        s = spacings_raw / mean_s  # unfolded NNS

        var_s = np.var(s)

        # ⟨r⟩ ratio (consecutive spacing ratio)
        r_vals = np.minimum(s[:-1], s[1:]) / np.maximum(s[:-1], s[1:])
        mean_r = np.mean(r_vals)

        # P(s < 0.3)
        frac_small = np.mean(s < 0.3)

        # Wigner surmise P(s) integrals for reference
        # Poisson: P(s) = exp(-s)        → ⟨r⟩ ≈ 0.386
        # GOE:     P(s) ~ s exp(-πs²/4)  → ⟨r⟩ ≈ 0.536
        # GUE:     P(s) ~ s² exp(-4s²/π) → ⟨r⟩ ≈ 0.603

        print(f"\n    NNS Statistics (n={len(s)} spacings from {n_use} eigenvalues):")
        print(f"      Mean spacing (raw):  {mean_s:.8f}")
        print(f"      Var(s):    {var_s:.4f}  [Poisson=1.000, GOE=0.286, GUE=0.178]")
        print(f"      ⟨r⟩:       {mean_r:.4f}  [Poisson=0.386, GOE=0.536, GUE=0.603]")
        print(f"      P(s<0.3):  {frac_small:.4f}  [Poisson=0.259, GOE=0.013, GUE=0.000]")

        # Classification
        dist_poi = abs(mean_r - 0.386)
        dist_goe = abs(mean_r - 0.536)
        dist_gue = abs(mean_r - 0.603)
        best = min(dist_poi, dist_goe, dist_gue)
        if best == dist_gue:
            stat_class = "GUE (quadratic repulsion, Class A)"
        elif best == dist_goe:
            stat_class = "GOE (linear repulsion, Class AI)"
        else:
            stat_class = "POISSON (no repulsion, integrable)"
        print(f"      => Classification: {stat_class}")
    else:
        s = np.array([])
        var_s = mean_r = frac_small = float('nan')
        stat_class = "INSUFFICIENT DATA"

    total_time = time.time() - t0
    print(f"\n    Total time: {total_time:.1f}s")

    return {
        "label": label,
        "lowest": lowest,
        "nns": s,
        "all_pos_evals": evals_pos,
        "n_positive": n_pos,
        "n_zero": n_zero,
        "var_s": float(var_s),
        "mean_r": float(mean_r),
        "frac_small": float(frac_small),
        "stat_class": stat_class,
        "K_profile": K.cpu().numpy(),
        "herm_err": herm_err,
    }


# ======================== RUN BOTH CASES ========================
print("\n" + "=" * 60)
print("  DIRAC-TORSION GUE SOLVER v1.0")
print("  H = -i σ_x ∂_x + σ_z K(x)  on S¹")
print("  Testing: Symmetry Class A → GUE level statistics")
print("=" * 60)

# Control: K = 0
K_control = torch.zeros(N, device=device, dtype=torch.float64)

# Vortex: K = 0.5 in central 20%
K_vortex = torch.zeros(N, device=device, dtype=torch.float64)
center = L / 2
half_width = 0.1 * L  # 20% total
mask = (x >= center - half_width) & (x < center + half_width)
K_vortex[mask] = 0.5
print(f"\n  Vortex K: {int(mask.sum())}/{N} points with K=0.5")

result_ctrl = run_dirac_analysis(K_control, "CONTROL (K=0, free Dirac on S¹)")
result_vort = run_dirac_analysis(K_vortex, "VORTEX (K=0.5, central 20%)")


# ======================== COMPARISON TABLE ========================
print(f"\n\n{'='*60}")
print(f"  COMPARISON: CONTROL vs VORTEX")
print(f"{'='*60}")

n_cmp = min(10, len(result_ctrl["lowest"]), len(result_vort["lowest"]))
print(f"\n  {'':>4s} | {'Control':>14s} | {'Vortex':>14s} | {'Delta':>10s}")
print(f"  {'-'*4} | {'-'*14} | {'-'*14} | {'-'*10}")
for i in range(n_cmp):
    c = result_ctrl["lowest"][i]
    v = result_vort["lowest"][i]
    print(f"  E_{i+1:2d} | {c:14.10f} | {v:14.10f} | {v - c:+10.6f}")

print(f"\n  {'Metric':>15s} | {'Control':>10s} | {'Vortex':>10s} | "
      f"{'Poisson':>8s} | {'GOE':>8s} | {'GUE':>8s}")
print(f"  {'-'*15} | {'-'*10} | {'-'*10} | {'-'*8} | {'-'*8} | {'-'*8}")
print(f"  {'Var(s)':>15s} | {result_ctrl['var_s']:10.4f} | {result_vort['var_s']:10.4f} | "
      f"{'1.000':>8s} | {'0.286':>8s} | {'0.178':>8s}")
print(f"  {'⟨r⟩':>15s} | {result_ctrl['mean_r']:10.4f} | {result_vort['mean_r']:10.4f} | "
      f"{'0.386':>8s} | {'0.536':>8s} | {'0.603':>8s}")
print(f"  {'P(s<0.3)':>15s} | {result_ctrl['frac_small']:10.4f} | {result_vort['frac_small']:10.4f} | "
      f"{'0.259':>8s} | {'0.013':>8s} | {'0.000':>8s}")

print(f"\n  Control: {result_ctrl['stat_class']}")
print(f"  Vortex:  {result_vort['stat_class']}")


# ======================== SAVE ========================
os.makedirs("UHF_Dirac_results", exist_ok=True)

save_data = {
    "hamiltonian": "H = -i sigma_x d/dx + sigma_z K(x)",
    "parameters": {"N": N, "dx": dx, "L": L},
    "control": {
        "lowest_10": result_ctrl["lowest"][:10].tolist(),
        "var_s": result_ctrl["var_s"],
        "mean_r": result_ctrl["mean_r"],
        "frac_small": result_ctrl["frac_small"],
        "stat_class": result_ctrl["stat_class"],
        "n_positive": result_ctrl["n_positive"],
        "n_zero": result_ctrl["n_zero"],
    },
    "vortex": {
        "K_value": 0.5, "K_region": "central 20%",
        "lowest_10": result_vort["lowest"][:10].tolist(),
        "var_s": result_vort["var_s"],
        "mean_r": result_vort["mean_r"],
        "frac_small": result_vort["frac_small"],
        "stat_class": result_vort["stat_class"],
        "n_positive": result_vort["n_positive"],
        "n_zero": result_vort["n_zero"],
    },
}

with open("UHF_Dirac_results/dirac_results.json", "w") as f:
    json.dump(save_data, f, indent=2)


# ======================== PLOT ========================
plt.style.use('dark_background')
fig, axes = plt.subplots(2, 3, figsize=(18, 10))

x_plot = np.linspace(0, L, N, endpoint=False)
s_theory = np.linspace(0, 4, 200)
p_poisson = np.exp(-s_theory)
p_goe = (np.pi / 2) * s_theory * np.exp(-np.pi * s_theory**2 / 4)
p_gue = (32 / np.pi**2) * s_theory**2 * np.exp(-4 * s_theory**2 / np.pi)

for row, (res, color, name) in enumerate([
    (result_ctrl, 'cyan', 'Control'),
    (result_vort, 'orange', 'Vortex'),
]):
    # Col 0: K profile
    axes[row, 0].fill_between(x_plot, res["K_profile"], color=color, alpha=0.3)
    axes[row, 0].plot(x_plot, res["K_profile"], color=color, lw=2)
    axes[row, 0].set_xlabel("x")
    axes[row, 0].set_ylabel("K(x)")
    axes[row, 0].set_title(f"{name}: Torsion profile")
    axes[row, 0].set_ylim(-0.1, 0.7)
    axes[row, 0].grid(True, alpha=0.2)

    # Col 1: Lowest eigenvalues
    n_show = min(50, len(res["lowest"]))
    axes[row, 1].scatter(range(1, n_show + 1), res["lowest"][:n_show],
                         c=color, s=20, zorder=5)
    axes[row, 1].plot(range(1, n_show + 1), res["lowest"][:n_show],
                      color=color, alpha=0.3, lw=1)
    axes[row, 1].set_xlabel("Mode n")
    axes[row, 1].set_ylabel("E_n")
    axes[row, 1].set_title(f"{name}: Lowest {n_show} positive eigenvalues")
    axes[row, 1].grid(True, alpha=0.2)

    # Col 2: NNS histogram
    if len(res["nns"]) > 5:
        axes[row, 2].hist(res["nns"], bins=25, density=True, range=(0, 4),
                          color=color, alpha=0.7, edgecolor='white', lw=0.5)
        axes[row, 2].plot(s_theory, p_poisson, 'r--', lw=2, label='Poisson')
        axes[row, 2].plot(s_theory, p_goe, 'g--', lw=2, label='GOE')
        axes[row, 2].plot(s_theory, p_gue, 'm-', lw=2.5, label='GUE')
        axes[row, 2].set_xlabel("s (normalized spacing)")
        axes[row, 2].set_ylabel("P(s)")
        axes[row, 2].set_title(f"{name} NNS: ⟨r⟩={res['mean_r']:.3f}, "
                               f"Var={res['var_s']:.3f}")
        axes[row, 2].legend(fontsize=9)
        axes[row, 2].set_xlim(0, 4)
        axes[row, 2].grid(True, alpha=0.2)

plt.suptitle("Dirac-Torsion Spectral Statistics — H = -iσ_x∂_x + σ_zK(x)\n"
             "Symmetry Class A (T-broken) → GUE target: ⟨r⟩ ≈ 0.603",
             fontsize=14, y=1.02)
plt.tight_layout()
plt.savefig("UHF_Dirac_results/dirac_spectral_stats.png", dpi=300, bbox_inches='tight')
print(f"\nPlot saved to UHF_Dirac_results/dirac_spectral_stats.png")
print(f"Data saved to UHF_Dirac_results/dirac_results.json")
