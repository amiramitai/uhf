#!/usr/bin/env python3
"""Dirac-Torsion GUE Solver v4.1 — Integrability Diagnostic
1D massless Dirac fermion on S^1 + axial torsion K(x)

H = -i sigma_x d/dx + sigma_z K(x)

Tests 5 K profiles to isolate the GUE mechanism:
  1) Control:     K = 0
  2) Step vortex: K = step function (integrable scattering)
  3) Multi-step:  3 golden-ratio-spaced barriers (incommensurate)
  4) Random:      smooth random K(x) from 50 Fourier modes
  5) Chiral:      K_z sigma_z + K_y sigma_y (max symmetry breaking)

Spectrum has 4-fold near-degeneracy at each E=n (2 chirality x 2 L/R).
Resolved by clustering eigenvalues within threshold, then poly-unfolding.

Key classifier: use Var(s) primarily (robust for quasi-equidistant spectra).
  Var(s) targets: Picket fence=0, GUE=0.178, GOE=0.286, Poisson=1.0

Symmetry Class A (T-broken) -> GUE target.
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

N = 1024
dx = 2 * np.pi / N
L = 2 * np.pi
poly_deg = 5
MERGE_THRESHOLD = 0.05

print(f"\n  Dirac-Torsion v4.1 — Integrability Diagnostic")
print(f"    N={N}, dx={dx:.6f}, L={L:.6f}, Matrix: {2*N}x{2*N}")

x = torch.linspace(0, L - dx, N, device=device, dtype=torch.float64)


def polynomial_unfold(evals, degree=5):
    n = len(evals)
    cumulative = np.arange(1, n + 1, dtype=np.float64)
    with np.errstate(all='ignore'):
        coeffs = np.polyfit(evals, cumulative, degree)
    N_smooth = np.polyval(coeffs, evals)
    return np.diff(N_smooth)


def compute_stats(spacings):
    s = spacings[spacings > 0]
    if len(s) < 10:
        return float('nan'), float('nan'), float('nan'), np.array([])
    s_norm = s / np.mean(s)
    var_s = float(np.var(s_norm))
    r_vals = np.minimum(s_norm[:-1], s_norm[1:]) / np.maximum(s_norm[:-1], s_norm[1:])
    mean_r = float(np.mean(r_vals))
    frac_small = float(np.mean(s_norm < 0.3))
    return var_s, mean_r, frac_small, s_norm


def classify_var(var_s):
    """Classify by Var(s) — more robust than <r> for quasi-equidistant spectra.
    Var(s) targets: picket-fence≈0, GUE≈0.178, GOE≈0.286, Poisson≈1.0"""
    if np.isnan(var_s):
        return "N/A"
    d = {abs(var_s - 0.0): "EQUIDISTANT",
         abs(var_s - 0.178): "GUE",
         abs(var_s - 0.286): "GOE",
         abs(var_s - 1.0): "POISSON"}
    return d[min(d.keys())]


def resolve_degeneracies(evals_pos, threshold=MERGE_THRESHOLD):
    """Merge near-degenerate eigenvalues into resolved levels."""
    clusters = []
    current = [evals_pos[0]]
    for i in range(1, len(evals_pos)):
        if evals_pos[i] - current[-1] < threshold:
            current.append(evals_pos[i])
        else:
            clusters.append(np.mean(current))
            current = [evals_pos[i]]
    clusters.append(np.mean(current))
    return np.array(clusters)


def build_dirac(K_c, label=""):
    """Build standard Dirac Hamiltonian: H = [[ K, -iD ], [ -iD, -K ]]"""
    idx = torch.arange(N, device=device)
    D = torch.zeros(N, N, device=device, dtype=torch.complex64)
    D[idx, (idx + 1) % N] = 1.0 / (2 * dx)
    D[idx, (idx - 1) % N] = -1.0 / (2 * dx)
    neg_iD = -1j * D

    H = torch.zeros(2*N, 2*N, device=device, dtype=torch.complex64)
    H[:N, :N] = torch.diag(K_c)
    H[N:, N:] = torch.diag(-K_c)
    H[:N, N:] = neg_iD
    H[N:, :N] = neg_iD
    H = 0.5 * (H + H.conj().T)
    return H


def full_analysis(evals_np, label, K_np):
    """Complete spectral analysis with degeneracy resolution."""
    evals_pos = np.sort(evals_np[evals_np > 1e-5])
    n_pos = len(evals_pos)
    n_zero = int(np.sum(np.abs(evals_np) < 1e-5))

    print(f"    {n_pos} pos, {len(evals_np)-n_pos-n_zero} neg, {n_zero} zero")
    print(f"    Lowest 10:")
    for i in range(min(10, n_pos)):
        print(f"      E_{i+1:2d} = {evals_pos[i]:.10f}")

    # Resolve degeneracies
    resolved = resolve_degeneracies(evals_pos)
    n_res = len(resolved)
    print(f"    Resolved: {n_pos} -> {n_res} levels (avg cluster = {n_pos/n_res:.2f})")

    # Poly-unfolded bulk of resolved spectrum
    lo = max(5, n_res // 8)
    hi = min(n_res - 3, n_res * 7 // 8, lo + 300)
    bulk = resolved[lo:hi]
    if len(bulk) >= 20:
        s_poly = polynomial_unfold(bulk, degree=poly_deg)
        var_s, mean_r, frac_small, s_norm = compute_stats(s_poly)
    else:
        var_s = mean_r = frac_small = float('nan')
        s_norm = np.array([])

    cls = classify_var(var_s)
    print(f"    Resolved bulk (modes {lo}-{hi}):")
    print(f"      Var(s)={var_s:.4f}  <r>={mean_r:.4f}  P(s<0.3)={frac_small:.4f}")
    print(f"      => {cls}")

    return {
        "label": label,
        "n_pos": int(n_pos),
        "n_resolved": int(n_res),
        "lowest_10": [float(v) for v in evals_pos[:10]],
        "var_s": var_s, "mean_r": mean_r, "frac_small": frac_small,
        "classification": cls,
        "nns": s_norm,
        "K_np": K_np,
        "bulk_range": (int(lo), int(hi)),
    }


# ======================== RUN ALL PROFILES ========================
print("\n" + "="*60)
print("  DIRAC-TORSION v4.1 — INTEGRABILITY DIAGNOSTIC")
print("  GUE target: Var(s) ~ 0.178, <r> ~ 0.603")
print("="*60)

center = L / 2
half_width = 0.1 * L
mask = (x >= center - half_width) & (x < center + half_width)

results = {}

# --- 1. Control ---
print(f"\n{'='*60}\n  1. CONTROL (K=0)\n{'='*60}")
K = torch.zeros(N, device=device, dtype=torch.complex64)
H = build_dirac(K, "Control"); evals = torch.linalg.eigvalsh(H).cpu().numpy()
del H; torch.cuda.empty_cache()
results["Control"] = full_analysis(evals, "Control", np.zeros(N))

# --- 2. Step K=10 ---
print(f"\n{'='*60}\n  2. STEP VORTEX (K=10, central 20%)\n{'='*60}")
K = torch.zeros(N, device=device, dtype=torch.complex64)
K[mask] = 10.0
H = build_dirac(K); evals = torch.linalg.eigvalsh(H).cpu().numpy()
del H; torch.cuda.empty_cache()
results["Step K=10"] = full_analysis(evals, "Step K=10", K.real.cpu().numpy())

# --- 3. Multi-step (golden ratio) ---
print(f"\n{'='*60}\n  3. MULTI-STEP (3 golden-ratio barriers, K=10)\n{'='*60}")
K = torch.zeros(N, device=device, dtype=torch.complex64)
phi_gr = (1 + np.sqrt(5)) / 2
for pos_frac in [phi_gr/5, phi_gr**2/5, phi_gr**3/5]:
    pos = (L * pos_frac) % L
    barrier = (torch.abs(x - pos) < 0.08 * L)
    K[barrier] = 10.0
H = build_dirac(K); evals = torch.linalg.eigvalsh(H).cpu().numpy()
del H; torch.cuda.empty_cache()
results["Multi-step"] = full_analysis(evals, "Multi-step", K.real.cpu().numpy())

# --- 4. Random smooth K ---
print(f"\n{'='*60}\n  4. RANDOM K (50 Fourier modes, RMS=10)\n{'='*60}")
torch.manual_seed(42)
K_rand = torch.zeros(N, device=device, dtype=torch.float64)
for m in range(1, 51):
    amp = 10.0 / (1 + m**0.5)
    phase = 2 * np.pi * torch.rand(1, device=device, dtype=torch.float64)
    K_rand += amp * torch.sin(2 * np.pi * m * x / L + phase)
K_rand = K_rand * 10.0 / float(K_rand.std())
K_c = K_rand.to(torch.complex64)
H = build_dirac(K_c); evals = torch.linalg.eigvalsh(H).cpu().numpy()
del H; torch.cuda.empty_cache()
results["Random K"] = full_analysis(evals, "Random K", K_rand.cpu().numpy())

# --- 5. Chiral Kz+Ky ---
print(f"\n{'='*60}\n  5. CHIRAL (K_z sigma_z + K_y sigma_y, step profiles)\n{'='*60}")
idx = torch.arange(N, device=device)
D = torch.zeros(N, N, device=device, dtype=torch.complex64)
D[idx, (idx + 1) % N] = 1.0 / (2 * dx)
D[idx, (idx - 1) % N] = -1.0 / (2 * dx)
neg_iD = -1j * D

K_z = torch.zeros(N, device=device, dtype=torch.complex64)
K_z[mask] = 10.0
mask_y = (x >= center + 0.05*L) & (x < center + 0.25*L)
K_y = torch.zeros(N, device=device, dtype=torch.complex64)
K_y[mask_y] = 10.0

H = torch.zeros(2*N, 2*N, device=device, dtype=torch.complex64)
H[:N, :N] = torch.diag(K_z)
H[N:, N:] = torch.diag(-K_z)
H[:N, N:] = neg_iD - 1j * torch.diag(K_y)
H[N:, :N] = neg_iD + 1j * torch.diag(K_y)
H = 0.5 * (H + H.conj().T)

evals = torch.linalg.eigvalsh(H).cpu().numpy()
del H, D, neg_iD; torch.cuda.empty_cache()
K_zy = np.column_stack([K_z.real.cpu().numpy(), K_y.real.cpu().numpy()])
results["Chiral"] = full_analysis(evals, "Chiral Kz+Ky", K_z.real.cpu().numpy())


# ======================== SUMMARY ========================
print(f"\n\n{'='*70}")
print(f"  FINAL SUMMARY — INTEGRABILITY DIAGNOSTIC")
print(f"{'='*70}")
print(f"\n  Var(s) targets: Equidistant=0  GUE=0.178  GOE=0.286  Poisson=1.0")
print(f"  <r> targets:    (equidist~1)    GUE=0.603  GOE=0.536  Poisson=0.386")
print(f"  (For quasi-equidistant spectra, Var(s) is the primary classifier)")
print()
print(f"  {'Profile':>18s} | {'#Res':>5s} | {'Var(s)':>8s} | {'<r>':>8s} | {'P(s<.3)':>8s} | {'Class':>14s}")
print(f"  {'-'*18} | {'-'*5} | {'-'*8} | {'-'*8} | {'-'*8} | {'-'*14}")
for name in ["Control", "Step K=10", "Multi-step", "Random K", "Chiral"]:
    r = results[name]
    print(f"  {name:>18s} | {r['n_resolved']:5d} | {r['var_s']:8.4f} | "
          f"{r['mean_r']:8.4f} | {r['frac_small']:8.4f} | {r['classification']:>14s}")
print()

# Key diagnostic
best_name = min(["Multi-step", "Random K", "Chiral"],
                key=lambda n: abs(results[n]["var_s"] - 0.178))
best = results[best_name]
print(f"  ** Closest to GUE: {best_name} with Var(s)={best['var_s']:.4f} "
      f"(target=0.178, delta={abs(best['var_s']-0.178):.4f})")
print()
print(f"  Diagnosis:")
if results["Multi-step"]["var_s"] < 0.25:
    print(f"    Incommensurate multi-barrier breaks integrability -> approaches GUE")
if results["Random K"]["var_s"] > results["Step K=10"]["var_s"]:
    print(f"    Random K shows more disorder than step K -> integrability matters")
if results["Control"]["var_s"] < 0.01:
    print(f"    Control is near-equidistant (as expected for free Dirac on S^1)")

# ======================== SAVE ========================
os.makedirs("UHF_Dirac_results", exist_ok=True)
save_data = {
    "version": "4.1",
    "hamiltonian": "H = -i sigma_x d/dx + sigma_z K(x)",
    "parameters": {"N": N, "dx": float(dx), "L": float(L), "poly_deg": poly_deg,
                   "merge_threshold": MERGE_THRESHOLD},
}
for name in results:
    r = results[name]
    save_data[name] = {
        "lowest_10": r["lowest_10"],
        "n_resolved": r["n_resolved"],
        "var_s": r["var_s"], "mean_r": r["mean_r"],
        "frac_small": r["frac_small"],
        "classification": r["classification"],
    }
with open("UHF_Dirac_results/dirac_results_v4.json", "w") as f:
    json.dump(save_data, f, indent=2)

# ======================== PLOT ========================
plt.style.use('dark_background')
fig, axes = plt.subplots(2, 3, figsize=(18, 10))

x_plot = np.linspace(0, L, N, endpoint=False)
s_theory = np.linspace(0, 4, 200)
p_poisson = np.exp(-s_theory)
p_goe = (np.pi / 2) * s_theory * np.exp(-np.pi * s_theory**2 / 4)
p_gue = (32 / np.pi**2) * s_theory**2 * np.exp(-4 * s_theory**2 / np.pi)

# Top row: K profiles
names_plot = ["Step K=10", "Multi-step", "Random K"]
colors = ['cyan', 'lime', 'orange']
for i, (nm, col) in enumerate(zip(names_plot, colors)):
    axes[0, i].plot(x_plot, results[nm]["K_np"][:N], color=col, lw=1.5)
    axes[0, i].set_title(f"{nm}: K(x)")
    axes[0, i].set_xlabel("x"); axes[0, i].set_ylabel("K")
    axes[0, i].grid(True, alpha=0.2)

# Bottom row: NNS histograms
for i, (nm, col) in enumerate(zip(names_plot, colors)):
    r = results[nm]
    nns = r["nns"]
    if len(nns) > 5:
        axes[1, i].hist(nns, bins=25, density=True, range=(0, 4),
                        color=col, alpha=0.7, edgecolor='white', lw=0.5)
    axes[1, i].plot(s_theory, p_poisson, 'r--', lw=2, label='Poisson')
    axes[1, i].plot(s_theory, p_goe, 'g--', lw=2, label='GOE')
    axes[1, i].plot(s_theory, p_gue, 'm-', lw=2.5, label='GUE')
    axes[1, i].set_title(f"{nm}: Var(s)={r['var_s']:.3f} ({r['classification']})")
    axes[1, i].legend(fontsize=8)
    axes[1, i].set_xlim(0, 4); axes[1, i].set_xlabel("s"); axes[1, i].set_ylabel("P(s)")
    axes[1, i].grid(True, alpha=0.2)

plt.suptitle("Dirac-Torsion v4.1: Integrability Diagnostic\n"
             "Var(s) targets: GUE=0.178, GOE=0.286, Poisson=1.0",
             fontsize=13, y=1.02)
plt.tight_layout()
plt.savefig("UHF_Dirac_results/dirac_spectral_stats_v4.png", dpi=300, bbox_inches='tight')
print(f"\nPlot: UHF_Dirac_results/dirac_spectral_stats_v4.png")
print(f"Data: UHF_Dirac_results/dirac_results_v4.json")
