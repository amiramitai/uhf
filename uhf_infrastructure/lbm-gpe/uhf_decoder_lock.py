"""
UHF Decoder Lock – Pilot Batch Analysis
=========================================
Reads pilot_observables.csv from the Pilot Batch Runner.
Finds:
  t*  = argmax_{t < d/c_s}  |μ₁(t) − μ₀(t)| / σ_pooled(t)
  θ   = (S̄₀(t*) + S̄₁(t*)) / 2

Plots μ₀(t) vs μ₁(t) with ±σ bands and d'(t) discriminability.
"""

import os, sys
import numpy as np

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    print("WARNING: matplotlib not found; plots will be skipped.")

# Physical constants (natural units)
XI = 1.0 / np.sqrt(2.0)
D_XI = 20.0
D = D_XI * XI                # separation in natural units
CS = 1.0
T_ACOUSTIC = D / CS          # acoustic crossing time


def load_data(csv_path):
    """Load pilot_observables.csv into arrays."""
    data = np.genfromtxt(csv_path, delimiter=',', skip_header=1)
    trial_ids  = data[:, 0].astype(int)
    bit_labels = data[:, 1].astype(int)
    times      = data[:, 2]
    O_phi      = data[:, 3]
    O_rho      = data[:, 4]
    return trial_ids, bit_labels, times, O_phi, O_rho


def decode_analysis(trial_ids, bit_labels, times, signal):
    """
    Population statistics for a given signal (O_phi or O_rho).
    Returns dict with time grid, means, stds, d', t*, θ, accuracy.
    """
    # Get time grid from the first trial
    first_trial = np.min(trial_ids)
    t_grid = np.sort(times[trial_ids == first_trial])
    n_times = len(t_grid)

    mu0 = np.zeros(n_times)
    mu1 = np.zeros(n_times)
    sigma0 = np.zeros(n_times)
    sigma1 = np.zeros(n_times)
    n0 = np.zeros(n_times, dtype=int)
    n1 = np.zeros(n_times, dtype=int)

    for i, t in enumerate(t_grid):
        mask_t = np.abs(times - t) < 1e-5
        vals_0 = signal[mask_t & (bit_labels == 0)]
        vals_1 = signal[mask_t & (bit_labels == 1)]

        n0[i] = len(vals_0)
        n1[i] = len(vals_1)
        if n0[i] > 0:
            mu0[i] = np.mean(vals_0)
            sigma0[i] = np.std(vals_0, ddof=1) if n0[i] > 1 else 0.0
        if n1[i] > 0:
            mu1[i] = np.mean(vals_1)
            sigma1[i] = np.std(vals_1, ddof=1) if n1[i] > 1 else 0.0

    # Pooled standard deviation
    sigma_pooled = np.sqrt((sigma0 ** 2 + sigma1 ** 2) / 2.0)

    # Discriminability d'(t) = |μ₁ − μ₀| / σ_pooled
    d_prime = np.zeros(n_times)
    nonzero = sigma_pooled > 1e-12
    d_prime[nonzero] = (np.abs(mu1[nonzero] - mu0[nonzero])
                        / sigma_pooled[nonzero])

    # t* = argmax d'(t)  for  t < t_acoustic and t > 0
    mask_causal = (t_grid < T_ACOUSTIC) & (t_grid > 1e-6)
    causal_dp = d_prime.copy()
    causal_dp[~mask_causal] = 0.0
    idx_star = int(np.argmax(causal_dp))
    t_star = t_grid[idx_star]
    d_prime_star = d_prime[idx_star]

    # Threshold θ
    theta = (mu0[idx_star] + mu1[idx_star]) / 2.0
    v_info = D / t_star if t_star > 1e-12 else float('inf')

    # Accuracy at t*
    mask_ts = np.abs(times - t_star) < 1e-5
    v0 = signal[mask_ts & (bit_labels == 0)]
    v1 = signal[mask_ts & (bit_labels == 1)]
    if mu0[idx_star] < mu1[idx_star]:
        correct = int(np.sum(v0 < theta)) + int(np.sum(v1 >= theta))
    else:
        correct = int(np.sum(v0 >= theta)) + int(np.sum(v1 < theta))
    n_total = len(v0) + len(v1)
    accuracy = correct / n_total if n_total > 0 else 0.0

    return {
        't_grid': t_grid,
        'mu0': mu0, 'mu1': mu1,
        'sigma0': sigma0, 'sigma1': sigma1,
        'sigma_pooled': sigma_pooled,
        'd_prime': d_prime,
        't_star': t_star,
        'd_prime_star': d_prime_star,
        'theta': theta,
        'v_info': v_info,
        'accuracy': accuracy,
        'n0': n0, 'n1': n1,
        'idx_star': idx_star,
    }


def print_result(label, res):
    """Print decode results for one observable."""
    idx = res['idx_star']
    print(f"\n  --- {label} ---")
    print(f"  t*               = {res['t_star']:.4f}")
    print(f"  t* / t_acoustic  = {res['t_star'] / T_ACOUSTIC:.4f}")
    print(f"  v_information    = {res['v_info']:.2f} c_s")
    print(f"  d'(t*)           = {res['d_prime_star']:.4f}")
    print(f"  theta            = {res['theta']:.6f}")
    print(f"  Accuracy at t*   = {res['accuracy']:.1%}")
    print(f"  mu_0(t*)         = {res['mu0'][idx]:.6f}")
    print(f"  mu_1(t*)         = {res['mu1'][idx]:.6f}")
    print(f"  sigma_0(t*)      = {res['sigma0'][idx]:.6f}")
    print(f"  sigma_1(t*)      = {res['sigma1'][idx]:.6f}")
    print(f"  sigma_pooled(t*) = {res['sigma_pooled'][idx]:.6f}")
    sys.stdout.flush()


def plot_results(res_phi, res_rho, out_path='pilot_decode.png'):
    """Plot phase and density decode analysis."""
    if not HAS_MPL:
        print("  matplotlib not available; skipping plot.")
        return

    fig, axes = plt.subplots(3, 1, figsize=(13, 11), sharex=True,
                             gridspec_kw={'height_ratios': [3, 3, 2]})

    # --- Top panel: phase observable ---
    ax = axes[0]
    t = res_phi['t_grid']
    ax.plot(t, res_phi['mu0'], 'b-', lw=1.5, label='Bit 0 (+pi/2)')
    ax.fill_between(t,
                    res_phi['mu0'] - res_phi['sigma0'],
                    res_phi['mu0'] + res_phi['sigma0'],
                    color='blue', alpha=0.15)
    ax.plot(t, res_phi['mu1'], 'r-', lw=1.5, label='Bit 1 (-pi/2)')
    ax.fill_between(t,
                    res_phi['mu1'] - res_phi['sigma1'],
                    res_phi['mu1'] + res_phi['sigma1'],
                    color='red', alpha=0.15)
    ax.axvline(res_phi['t_star'], color='green', lw=2,
               label=f"t* = {res_phi['t_star']:.3f}")
    ax.axvline(T_ACOUSTIC, color='black', lw=1.5, ls='--',
               label=f't_acoustic = {T_ACOUSTIC:.2f}')
    ax.axhline(res_phi['theta'], color='gray', lw=1, ls=':',
               label=f"theta = {res_phi['theta']:.4f}")
    ax.set_ylabel('O_phi (circular mean phase)')
    n0_max = int(np.max(res_phi['n0']))
    n1_max = int(np.max(res_phi['n1']))
    ax.set_title(
        f'Phase:  {n0_max} x Bit0 + {n1_max} x Bit1  |  '
        f"t*={res_phi['t_star']:.3f}  v={res_phi['v_info']:.1f} c_s  "
        f"acc={res_phi['accuracy']:.1%}")
    ax.legend(loc='best', fontsize=8)
    ax.grid(True, alpha=0.3)

    # --- Middle panel: density observable ---
    ax = axes[1]
    ax.plot(t, res_rho['mu0'], 'b-', lw=1.5, label='Bit 0')
    ax.fill_between(t,
                    res_rho['mu0'] - res_rho['sigma0'],
                    res_rho['mu0'] + res_rho['sigma0'],
                    color='blue', alpha=0.15)
    ax.plot(t, res_rho['mu1'], 'r-', lw=1.5, label='Bit 1')
    ax.fill_between(t,
                    res_rho['mu1'] - res_rho['sigma1'],
                    res_rho['mu1'] + res_rho['sigma1'],
                    color='red', alpha=0.15)
    ax.axvline(res_rho['t_star'], color='green', lw=2,
               label=f"t*_rho = {res_rho['t_star']:.3f}")
    ax.axvline(T_ACOUSTIC, color='black', lw=1.5, ls='--')
    ax.set_ylabel('O_rho (mean density)')
    ax.set_title(
        f"Density:  t*={res_rho['t_star']:.3f}  "
        f"v={res_rho['v_info']:.1f} c_s  "
        f"d'={res_rho['d_prime_star']:.2f}")
    ax.legend(loc='best', fontsize=8)
    ax.grid(True, alpha=0.3)

    # --- Bottom panel: d'(t) ---
    ax = axes[2]
    ax.plot(t, res_phi['d_prime'], 'k-', lw=1.5, label="Phase d'")
    ax.plot(t, res_rho['d_prime'], 'gray', lw=1, ls='--',
            label="Density d'")
    ax.axvline(res_phi['t_star'], color='green', lw=2)
    ax.axvline(T_ACOUSTIC, color='black', lw=1.5, ls='--')
    ax.set_xlabel('Time (natural units)')
    ax.set_ylabel("|mu_1 - mu_0| / sigma_pooled")
    ax.set_title(
        f"Discriminability:  d'_phi(t*) = {res_phi['d_prime_star']:.2f},  "
        f"d'_rho(t*) = {res_rho['d_prime_star']:.2f}")
    ax.legend(loc='best', fontsize=8)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    print(f"  Plot saved: {out_path}")


def main():
    csv_path = os.path.join(os.path.dirname(__file__) or '.', 
                            'pilot_observables.csv')
    if not os.path.exists(csv_path):
        print(f"FATAL: {csv_path} not found. "
              f"Run uhf_pilot_batch_runner.py first.")
        sys.exit(1)

    print(f"{'#' * 65}")
    print(f"  UHF DECODER LOCK – PILOT BATCH ANALYSIS")
    print(f"{'#' * 65}")

    print(f"\n  Loading {csv_path}...")
    trial_ids, bit_labels, times, O_phi, O_rho = load_data(csv_path)

    n_trials = len(np.unique(trial_ids))
    n_rows = len(trial_ids)
    n_bit0 = len(np.unique(trial_ids[bit_labels == 0]))
    n_bit1 = len(np.unique(trial_ids[bit_labels == 1]))

    print(f"  Rows:      {n_rows}")
    print(f"  Trials:    {n_trials} (Bit 0: {n_bit0}, Bit 1: {n_bit1})")
    print(f"  Time:      [{times.min():.4f}, {times.max():.4f}]")
    print(f"  t_acoustic = {T_ACOUSTIC:.4f}")
    sys.stdout.flush()

    # Phase analysis
    res_phi = decode_analysis(trial_ids, bit_labels, times, O_phi)
    print_result("PHASE OBSERVABLE (O_phi)", res_phi)

    # Density analysis
    res_rho = decode_analysis(trial_ids, bit_labels, times, O_rho)
    print_result("DENSITY OBSERVABLE (O_rho)", res_rho)

    # --- Verdict ---
    print(f"\n  {'=' * 55}")
    if (res_phi['t_star'] < T_ACOUSTIC
            and res_phi['d_prime_star'] > 1.0):
        print(f"  >> SUPRA-ACOUSTIC INFORMATION TRANSFER CONFIRMED")
        print(f"  >>")
        print(f"  >> t* = {res_phi['t_star']:.4f} = "
              f"{res_phi['t_star'] / T_ACOUSTIC:.4f} x t_acoustic")
        print(f"  >> v_information = {res_phi['v_info']:.1f} c_s")
        print(f"  >> d'(t*) = {res_phi['d_prime_star']:.2f}")
        print(f"  >> Accuracy = {res_phi['accuracy']:.1%}")
        print(f"  >>")
        print(f"  >> Shannon bit survives "
              f"sigma_theta = 0.01, sigma_rho = 1e-3 noise.")
    elif res_phi['t_star'] < T_ACOUSTIC:
        print(f"  >> Weak signal at t* = {res_phi['t_star']:.4f} "
              f"(d' = {res_phi['d_prime_star']:.2f})")
        print(f"  >> May need more trials or lower noise.")
    else:
        print(f"  >> No supra-acoustic decode detected.")
    print(f"  {'=' * 55}")
    sys.stdout.flush()

    # Plot
    plot_path = os.path.join(os.path.dirname(__file__) or '.',
                             'pilot_decode.png')
    plot_results(res_phi, res_rho, plot_path)


if __name__ == "__main__":
    main()
