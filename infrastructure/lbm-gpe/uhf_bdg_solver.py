#!/usr/bin/env python3
"""UHF BdG Solver v5.0 — Bogoliubov-de Gennes spectral statistics
on a 1D ring with torsion flux.

Tests the Hilbert-Pólya conjecture: do the BdG excitation eigenvalues
of the UHF vacuum with a topological vortex exhibit GUE level repulsion?

Protocol:
  1. 1D ring, N=1024, dx=0.01, periodic BCs
  2. GP condensate with winding number 1: phi = sqrt(rho0) exp(i 2pi x/L)
  3. Torsion flux: K = 0.5 in central 20%, K = 0 outside
  4. Imaginary-time RK4 equilibration (5000 steps)
  5. Dense 2N x 2N BdG matrix on GPU (2048 x 2048)
  6. Full diagonalization: torch.linalg.eigvals
  7. Spectral statistics: NNS distribution (Control vs Vortex)

Key operator identity (from derivation):
  D = d/dx + iK,  D† = -d/dx - iK
  => D†D = -D²  (Hermitian)
  => -D² = -d²/dx² - i(dK/dx) - 2iK d/dx + K²

GPU: RTX 3090.
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
dx = 0.01
L = N * dx  # 10.24
rho0 = 1.0
lam = 1.0              # GP interaction strength
mu = lam * rho0         # chemical potential = 1.0
dt_imag = 5e-5          # imaginary time step (RK4 stable: dt*4/dx^2 = 2.0 < 2.785)
n_equil = 5000          # equilibration steps
n_extract = 50          # number of eigenvalues to analyze

xi_heal = 1.0 / np.sqrt(2 * lam * rho0)  # healing length
c_sound = np.sqrt(lam * rho0)             # sound speed

print(f"\n  BdG Solver Parameters:")
print(f"    N={N}, dx={dx}, L={L:.4f}")
print(f"    rho0={rho0}, lambda={lam}, mu={mu}")
print(f"    dt_imag={dt_imag}, n_equil={n_equil}")
print(f"    Healing length xi = {xi_heal:.4f} ({xi_heal/dx:.0f} grid pts)")
print(f"    Sound speed c = {c_sound:.4f}")
print(f"    BdG matrix: {2*N} x {2*N} = {(2*N)**2} elements")
print(f"    Memory: ~{(2*N)**2 * 8 / 1e6:.1f} MB (complex64)")

# ======================== GRID ========================
x = torch.linspace(0, L - dx, N, device=device, dtype=torch.float64)


# ======================== BdG ANALYSIS ========================
def run_bdg_analysis(K_profile, label):
    """Full BdG analysis for a given torsion profile K(x).

    Steps: equilibrate GP condensate, build BdG matrix, diagonalize,
    compute spectral statistics.
    """
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    t0 = time.time()

    # --- 1. Initialize winding-1 condensate ---
    theta = 2 * np.pi * x / L
    phi = (torch.sqrt(torch.tensor(rho0, device=device, dtype=torch.float64))
           * torch.exp(1j * theta.to(torch.complex128)))

    K = K_profile.to(torch.float64)

    # dK/dx with centered differences (periodic)
    dKdx = (torch.roll(K, -1) - torch.roll(K, 1)) / (2 * dx)

    n_nonzero_K = int((torch.abs(K) > 1e-10).sum())
    print(f"    K profile: min={float(K.min()):.2f}, max={float(K.max()):.2f}, "
          f"nonzero={n_nonzero_K}/{N}")
    print(f"    dK/dx: min={float(dKdx.min()):.2f}, max={float(dKdx.max()):.2f}")
    print(f"    Initial <|phi|^2> = {float(torch.abs(phi).pow(2).mean()):.6f}")

    # --- 2. Imaginary-time RK4 equilibration ---
    # GP in imaginary time: dphi/dtau = D^2 phi + mu*phi - lam*|phi|^2*phi
    # where D^2 phi = phi'' + 2iK phi' + i(K') phi - K^2 phi
    print(f"    Equilibrating ({n_equil} steps, dt={dt_imag})...")

    K_c = K.to(torch.complex128)
    dK_c = dKdx.to(torch.complex128)
    K2_c = K_c ** 2

    def gp_rhs(phi_s):
        phi_pp = (torch.roll(phi_s, -1) - 2 * phi_s + torch.roll(phi_s, 1)) / dx**2
        phi_p = (torch.roll(phi_s, -1) - torch.roll(phi_s, 1)) / (2 * dx)
        D2phi = phi_pp + 2j * K_c * phi_p + 1j * dK_c * phi_s - K2_c * phi_s
        return D2phi + mu * phi_s - lam * torch.abs(phi_s)**2 * phi_s

    for step in range(n_equil):
        k1 = gp_rhs(phi)
        k2 = gp_rhs(phi + 0.5 * dt_imag * k1)
        k3 = gp_rhs(phi + 0.5 * dt_imag * k2)
        k4 = gp_rhs(phi + dt_imag * k3)
        phi = phi + (dt_imag / 6.0) * (k1 + 2 * k2 + 2 * k3 + k4)

        # Renormalize to maintain target density
        current_mean_rho = torch.abs(phi).pow(2).mean()
        phi = phi * torch.sqrt(
            torch.tensor(rho0, device=device, dtype=torch.float64) / current_mean_rho
        )

        if step % 1000 == 0 or step == n_equil - 1:
            rho = torch.abs(phi).pow(2)
            print(f"      Step {step:5d}: <rho>={float(rho.mean()):.6f}, "
                  f"min={float(rho.min()):.6f}, max={float(rho.max()):.6f}")

    t_equil = time.time() - t0
    print(f"    Equilibration done ({t_equil:.1f}s)")

    # Save equilibrated state
    phi_equil = phi.clone()
    rho_equil = torch.abs(phi).pow(2)

    # --- 3. Build dense BdG matrix on GPU ---
    print(f"    Building {2*N}x{2*N} BdG matrix on GPU...")
    t_build = time.time()

    # Downcast to complex64 for eigenvalue computation
    phi_f = phi.to(torch.complex64)
    K_f = K.to(torch.complex64)

    # Build covariant derivative D = G_forward + i*diag(K)
    # using forward finite differences: D_jj = -1/dx + iK_j, D_{j,j+1} = 1/dx
    # Then D†D = D^H @ D is Hermitian by construction and equals -D²
    # in the continuum limit (recovers standard 3-point Laplacian for K=0).
    idx = torch.arange(N, device=device)
    D_mat = torch.zeros(N, N, device=device, dtype=torch.complex64)
    D_mat[idx, idx] = -1.0 / dx + 1j * K_f[idx]
    D_mat[idx, (idx + 1) % N] = 1.0 / dx

    neg_D2 = D_mat.conj().T @ D_mat  # D†D, Hermitian

    diag_rho = torch.diag(torch.abs(phi_f) ** 2)
    I_N = torch.eye(N, device=device, dtype=torch.complex64)

    # H_0 = D†D - mu + 2*lam*|phi|^2
    H0 = neg_D2 - mu * I_N + 2 * lam * diag_rho

    # -H_0* (element-wise conjugate, then negate)
    neg_H0_conj = -(H0.conj())

    # Off-diagonal pairing terms
    Delta = lam * torch.diag(phi_f ** 2)
    neg_Delta_conj = -lam * torch.diag(phi_f.conj() ** 2)

    # Assemble full 2N x 2N BdG matrix:
    # H_BdG = [[ H_0,       Delta      ],
    #          [-Delta*,    -H_0*       ]]
    H_bdg = torch.zeros(2 * N, 2 * N, device=device, dtype=torch.complex64)
    H_bdg[:N, :N] = H0
    H_bdg[:N, N:] = Delta
    H_bdg[N:, :N] = neg_Delta_conj
    H_bdg[N:, N:] = neg_H0_conj

    # Check Hermiticity of H_0 (should be Hermitian since -D^2 = D†D)
    herm_err = float(torch.max(torch.abs(H0 - H0.conj().T)))
    print(f"    H_0 Hermiticity check: max|H_0 - H_0†| = {herm_err:.2e}")

    t_built = time.time() - t_build
    print(f"    Matrix built ({t_built:.1f}s)")

    # Free intermediate matrices
    del D_mat, diag_rho, I_N, neg_D2
    del H0, neg_H0_conj, Delta, neg_Delta_conj
    torch.cuda.empty_cache()

    # --- 4. Diagonalize ---
    print(f"    Diagonalizing {2*N}x{2*N} matrix...")
    t_diag = time.time()

    try:
        evals = torch.linalg.eigvals(H_bdg)
        diag_device = "CUDA"
    except Exception as e:
        print(f"    CUDA eigvals failed ({e}), falling back to CPU...")
        evals = torch.linalg.eigvals(H_bdg.cpu())
        diag_device = "CPU"

    evals_np = evals.cpu().numpy()
    t_diaged = time.time() - t_diag
    print(f"    Diagonalization done ({t_diaged:.1f}s, {diag_device})")

    del H_bdg
    torch.cuda.empty_cache()

    # --- 5. Process eigenvalues ---
    max_imag = np.max(np.abs(evals_np.imag))
    mean_imag = np.mean(np.abs(evals_np.imag))
    print(f"    Eigenvalue quality:")
    print(f"      Max  |Im(omega)|: {max_imag:.6e}")
    print(f"      Mean |Im(omega)|: {mean_imag:.6e}")

    evals_real = evals_np.real

    # Positive eigenvalues (drop Goldstone zero mode at |omega| < 1e-5)
    evals_pos = np.sort(evals_real[evals_real > 1e-5])
    evals_neg = np.sort(evals_real[evals_real < -1e-5])
    n_zero = np.sum(np.abs(evals_real) <= 1e-5)
    n_pos = len(evals_pos)

    print(f"    Spectrum: {n_pos} positive, {len(evals_neg)} negative, {n_zero} near-zero")

    # Extract lowest positive eigenvalues
    n_use = min(n_extract, n_pos)
    lowest = evals_pos[:n_use]

    print(f"\n    Lowest 10 positive eigenvalues:")
    for i in range(min(10, n_use)):
        print(f"      omega_{i+1:2d} = {lowest[i]:.8f}")

    # --- 6. Spectral statistics ---
    if n_use >= 10:
        spacings = np.diff(lowest)
        mean_s = np.mean(spacings)
        s = spacings / mean_s  # unfolded NNS

        var_s = np.var(s)

        # Consecutive spacing ratio (doesn't require unfolding)
        r_vals = np.minimum(s[:-1], s[1:]) / np.maximum(s[:-1], s[1:])
        mean_r = np.mean(r_vals)

        frac_small = np.mean(s < 0.3)

        print(f"\n    NNS Statistics (n={len(s)} spacings):")
        print(f"      Mean spacing (raw):  {mean_s:.6f}")
        print(f"      Var(s):    {var_s:.4f}  [Poisson=1.00, GOE=0.286, GUE=0.178]")
        print(f"      <r>:       {mean_r:.4f}  [Poisson=0.386, GOE=0.536, GUE=0.603]")
        print(f"      P(s<0.3):  {frac_small:.4f}  [Poisson=0.259, GOE=0.013, GUE=0.000]")

        if var_s > 0.7:
            stat_class = "POISSON (no level repulsion)"
        elif var_s > 0.25:
            stat_class = "GOE-like (linear repulsion)"
        else:
            stat_class = "GUE-like (quadratic repulsion)"
        print(f"      => Classification: {stat_class}")
    else:
        s = np.array([])
        var_s = float('nan')
        mean_r = float('nan')
        frac_small = float('nan')
        stat_class = "INSUFFICIENT DATA"
        print(f"    Too few eigenvalues for statistics ({n_use})")

    total_time = time.time() - t0
    print(f"\n    Total time: {total_time:.1f}s")

    return {
        "label": label,
        "lowest": lowest,
        "nns": s,
        "all_pos_evals": evals_pos,
        "max_imag": max_imag,
        "n_positive": n_pos,
        "var_s": var_s,
        "mean_r": mean_r,
        "frac_small": frac_small,
        "stat_class": stat_class,
        "phi_final": phi_equil.cpu().numpy(),
        "rho_final": rho_equil.cpu().numpy(),
        "K_profile": K.cpu().numpy(),
        "herm_err": herm_err,
    }


# ======================== RUN BOTH CASES ========================
print("\n" + "=" * 60)
print("  UHF BdG SOLVER v5.0 — SPECTRAL STATISTICS")
print("  Testing Hilbert-Polya via Bogoliubov-de Gennes spectrum")
print("=" * 60)

# Control: K = 0 (pure superfluid with winding)
K_control = torch.zeros(N, device=device, dtype=torch.float64)

# Vortex: K = 0.5 in central 20% of the ring
K_vortex = torch.zeros(N, device=device, dtype=torch.float64)
center = L / 2
half_width = 0.1 * L  # 20% total = 10% on each side of center
mask_vortex = (x >= center - half_width) & (x < center + half_width)
K_vortex[mask_vortex] = 0.5
print(f"\n  Vortex K profile: {int(mask_vortex.sum())} / {N} points with K=0.5")

result_ctrl = run_bdg_analysis(K_control, "CONTROL (K=0, pure superfluid, winding=1)")
result_vort = run_bdg_analysis(K_vortex, "TORSION VORTEX (K=0.5, central 20%, winding=1)")


# ======================== COMPARISON ========================
print(f"\n\n{'='*60}")
print(f"  COMPARISON: CONTROL vs VORTEX")
print(f"{'='*60}")

n_compare = min(10, len(result_ctrl["lowest"]), len(result_vort["lowest"]))
print(f"\n  {'':>4s} | {'Control':>14s} | {'Vortex':>14s} | {'Delta':>10s}")
print(f"  {'-'*4} | {'-'*14} | {'-'*14} | {'-'*10}")
for i in range(n_compare):
    c = result_ctrl["lowest"][i]
    v = result_vort["lowest"][i]
    print(f"  w_{i+1:2d} | {c:14.8f} | {v:14.8f} | {v - c:+10.6f}")

print(f"\n  Spectral Statistics Summary:")
print(f"  {'Metric':>15s} | {'Control':>10s} | {'Vortex':>10s} | "
      f"{'Poisson':>8s} | {'GOE':>8s} | {'GUE':>8s}")
print(f"  {'-'*15} | {'-'*10} | {'-'*10} | {'-'*8} | {'-'*8} | {'-'*8}")
print(f"  {'Var(s)':>15s} | {result_ctrl['var_s']:10.4f} | {result_vort['var_s']:10.4f} | "
      f"{'1.000':>8s} | {'0.286':>8s} | {'0.178':>8s}")
print(f"  {'<r>':>15s} | {result_ctrl['mean_r']:10.4f} | {result_vort['mean_r']:10.4f} | "
      f"{'0.386':>8s} | {'0.536':>8s} | {'0.603':>8s}")
print(f"  {'P(s<0.3)':>15s} | {result_ctrl['frac_small']:10.4f} | {result_vort['frac_small']:10.4f} | "
      f"{'0.259':>8s} | {'0.013':>8s} | {'0.000':>8s}")

print(f"\n  Control classification:  {result_ctrl['stat_class']}")
print(f"  Vortex classification:   {result_vort['stat_class']}")

# ======================== SAVE JSON ========================
os.makedirs("UHF_BdG_results", exist_ok=True)

save_data = {
    "parameters": {
        "N": N, "dx": dx, "L": L, "rho0": rho0,
        "lambda": lam, "mu": mu,
        "dt_imag": dt_imag, "n_equil": n_equil,
        "xi_heal": xi_heal, "c_sound": c_sound,
    },
    "control": {
        "lowest_10": result_ctrl["lowest"][:10].tolist(),
        "var_s": float(result_ctrl["var_s"]),
        "mean_r": float(result_ctrl["mean_r"]),
        "frac_small": float(result_ctrl["frac_small"]),
        "stat_class": result_ctrl["stat_class"],
        "max_imag": float(result_ctrl["max_imag"]),
        "herm_err": float(result_ctrl["herm_err"]),
    },
    "vortex": {
        "lowest_10": result_vort["lowest"][:10].tolist(),
        "var_s": float(result_vort["var_s"]),
        "mean_r": float(result_vort["mean_r"]),
        "frac_small": float(result_vort["frac_small"]),
        "stat_class": result_vort["stat_class"],
        "max_imag": float(result_vort["max_imag"]),
        "herm_err": float(result_vort["herm_err"]),
    },
}

with open("UHF_BdG_results/bdg_results.json", "w") as f:
    json.dump(save_data, f, indent=2)

# ======================== PLOT ========================
plt.style.use('dark_background')
fig, axes = plt.subplots(2, 3, figsize=(18, 10))

x_plot = np.linspace(0, L, N, endpoint=False)
s_theory = np.linspace(0, 4, 200)
p_poisson = np.exp(-s_theory)
p_goe = (np.pi / 2) * s_theory * np.exp(-np.pi * s_theory**2 / 4)
p_gue = (32 / np.pi**2) * s_theory**2 * np.exp(-4 * s_theory**2 / np.pi)

# --- Row 0: Control ---
# Density profile
axes[0, 0].plot(x_plot, np.abs(result_ctrl["phi_final"])**2, 'cyan', lw=1)
axes[0, 0].set_xlabel("x")
axes[0, 0].set_ylabel("|φ|²")
axes[0, 0].set_title("Control: Density profile")
axes[0, 0].grid(True, alpha=0.2)

# Lowest eigenvalues
n_show = min(30, len(result_ctrl["lowest"]))
axes[0, 1].scatter(range(1, n_show + 1), result_ctrl["lowest"][:n_show],
                   c='cyan', s=30, zorder=5)
axes[0, 1].plot(range(1, n_show + 1), result_ctrl["lowest"][:n_show],
                'cyan', alpha=0.3, lw=1)
axes[0, 1].set_xlabel("Mode n")
axes[0, 1].set_ylabel("ω_n")
axes[0, 1].set_title(f"Control: Lowest {n_show} eigenvalues")
axes[0, 1].grid(True, alpha=0.2)

# NNS histogram
if len(result_ctrl["nns"]) > 5:
    axes[0, 2].hist(result_ctrl["nns"], bins=20, density=True,
                    color='cyan', alpha=0.7, edgecolor='white', lw=0.5)
    axes[0, 2].plot(s_theory, p_poisson, 'r--', lw=2, label='Poisson')
    axes[0, 2].plot(s_theory, p_goe, 'g--', lw=2, label='GOE')
    axes[0, 2].plot(s_theory, p_gue, 'm--', lw=2, label='GUE')
    axes[0, 2].set_xlabel("s (normalized spacing)")
    axes[0, 2].set_ylabel("P(s)")
    axes[0, 2].set_title(f"Control NNS: Var={result_ctrl['var_s']:.3f}")
    axes[0, 2].legend(fontsize=9)
    axes[0, 2].set_xlim(0, 4)
    axes[0, 2].grid(True, alpha=0.2)

# --- Row 1: Vortex ---
# Density + K profile
ax_rho = axes[1, 0]
ax_K = ax_rho.twinx()
ax_rho.plot(x_plot, np.abs(result_vort["phi_final"])**2, 'orange', lw=1, label='|φ|²')
ax_K.fill_between(x_plot, result_vort["K_profile"], color='yellow', alpha=0.15)
ax_K.plot(x_plot, result_vort["K_profile"], 'yellow', lw=1, alpha=0.5, label='K')
ax_rho.set_xlabel("x")
ax_rho.set_ylabel("|φ|²", color='orange')
ax_K.set_ylabel("K", color='yellow')
ax_rho.set_title("Vortex: Density (orange) + K (yellow)")
ax_rho.grid(True, alpha=0.2)

# Lowest eigenvalues
n_show_v = min(30, len(result_vort["lowest"]))
axes[1, 1].scatter(range(1, n_show_v + 1), result_vort["lowest"][:n_show_v],
                   c='orange', s=30, zorder=5)
axes[1, 1].plot(range(1, n_show_v + 1), result_vort["lowest"][:n_show_v],
                'orange', alpha=0.3, lw=1)
axes[1, 1].set_xlabel("Mode n")
axes[1, 1].set_ylabel("ω_n")
axes[1, 1].set_title(f"Vortex: Lowest {n_show_v} eigenvalues")
axes[1, 1].grid(True, alpha=0.2)

# NNS histogram
if len(result_vort["nns"]) > 5:
    axes[1, 2].hist(result_vort["nns"], bins=20, density=True,
                    color='orange', alpha=0.7, edgecolor='white', lw=0.5)
    axes[1, 2].plot(s_theory, p_poisson, 'r--', lw=2, label='Poisson')
    axes[1, 2].plot(s_theory, p_goe, 'g--', lw=2, label='GOE')
    axes[1, 2].plot(s_theory, p_gue, 'm--', lw=2, label='GUE')
    axes[1, 2].set_xlabel("s (normalized spacing)")
    axes[1, 2].set_ylabel("P(s)")
    axes[1, 2].set_title(f"Vortex NNS: Var={result_vort['var_s']:.3f}")
    axes[1, 2].legend(fontsize=9)
    axes[1, 2].set_xlim(0, 4)
    axes[1, 2].grid(True, alpha=0.2)

plt.suptitle("UHF BdG Spectral Statistics — Control vs Torsion Vortex\n"
             f"N={N}, λ={lam}, ρ₀={rho0}, ξ={xi_heal:.3f}",
             fontsize=14, y=1.02)
plt.tight_layout()
plt.savefig("UHF_BdG_results/bdg_spectral_stats.png", dpi=300, bbox_inches='tight')
print(f"\nPlot saved to UHF_BdG_results/bdg_spectral_stats.png")
print(f"Data saved to UHF_BdG_results/bdg_results.json")
