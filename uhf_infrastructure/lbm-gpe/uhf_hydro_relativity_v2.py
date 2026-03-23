#!/usr/bin/env python3
"""
UHF — Hydrodynamic Relativity v2 (Acoustic Metric)
Bogoliubov Phonon Wave-Packet: Length Contraction & Time Dilation

1D split-step Fourier GP solver on RTX 3090.
Tests whether a drifting phonon wave-packet shows Lorentz contraction
L(v) = L₀/γ and time dilation T(v) = T₀·γ with γ=1/√(1−v²/c_s²).

Units: ξ=1, ρ₀=1, c_s=1, ℏ=m=1, g=1, μ=1.

Physics: The perturbation δψ = A·gauss·exp(ik₀x) on a stationary condensate
creates a density modulation that decomposes into ±c_s Bogoliubov movers.
We track the RIGHT-moving density perturbation δρ = |ψ|² − ρ₀ via
centre-of-mass and RMS width, and extract the internal oscillation period
from the density time-series at the packet centre.
"""

import os, sys, json, time
import numpy as np
import torch
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
from scipy.signal import find_peaks

# ╔═══════════════════════════════════════════════════════════╗
# ║  SETUP                                                   ║
# ╚═══════════════════════════════════════════════════════════╝

def setup_device():
    assert torch.cuda.is_available(), "CUDA required"
    dev = torch.device('cuda:0')
    prop = torch.cuda.get_device_properties(dev)
    print(f"  Device : {prop.name}")
    print(f"  VRAM   : {prop.total_memory / 1e9:.1f} GB")
    return dev

# ╔═══════════════════════════════════════════════════════════╗
# ║  GRID                                                    ║
# ╚═══════════════════════════════════════════════════════════╝

def build_grid(N, L, dev):
    dx = L / N
    x = torch.linspace(-L/2, L/2 - dx, N, dtype=torch.float64, device=dev)
    k = torch.fft.fftfreq(N, d=dx, device=dev).to(torch.float64) * 2 * np.pi
    return x, k, dx

# ╔═══════════════════════════════════════════════════════════╗
# ║  SPONGE (absorbing boundary)                             ║
# ╚═══════════════════════════════════════════════════════════╝

def build_sponge(x, L, width=15.0, strength=0.05):
    xn = x.cpu().numpy()
    s = np.zeros_like(xn)
    h = L / 2
    ml = xn < -h + width
    s[ml] = strength * ((-h + width - xn[ml]) / width) ** 2
    mr = xn > h - width
    s[mr] = strength * ((xn[mr] - (h - width)) / width) ** 2
    return torch.from_numpy(s).to(x.device)

# ╔═══════════════════════════════════════════════════════════╗
# ║  BOGOLIUBOV DISPERSION                                   ║
# ╚═══════════════════════════════════════════════════════════╝

def omega_bog(k):
    """ω(k) = |k|√(1 + k²/2)  (c_s = ξ = 1)."""
    return np.abs(k) * np.sqrt(1.0 + 0.5 * k**2)

def group_vel(k):
    """v_g = dω/dk = k(1+k²)/√(k²(1+k²/2))."""
    k2 = k**2
    return k * (1.0 + k2) / (np.abs(k) * np.sqrt(1.0 + 0.5 * k2) + 1e-30)

# ╔═══════════════════════════════════════════════════════════╗
# ║  INITIALIZATION                                          ║
# ╚═══════════════════════════════════════════════════════════╝

def init_phonon_packet(x, k0, A=0.01, sigma=8.0, x0=-60.0):
    """
    ψ(x,0) = √ρ₀ + A · exp(−(x−x₀)²/2σ²) · exp(ik₀x)

    Per Red Team spec: Galilean boost on packet.
    k₀ sets the carrier wavenumber; the density perturbation
    cos(k₀x)·gauss splits into ±c_s Bogoliubov movers.
    The right-mover is tracked for measurements.
    """
    rho0_t = torch.tensor(1.0, dtype=torch.float64, device=x.device)
    envelope = A * torch.exp(-(x - x0)**2 / (2 * sigma**2))
    carrier = torch.exp(1j * k0 * x)
    psi = torch.sqrt(rho0_t) + (envelope * carrier).to(torch.complex128)
    return psi

# ╔═══════════════════════════════════════════════════════════╗
# ║  SPLIT-STEP GP PROPAGATOR                                ║
# ╚═══════════════════════════════════════════════════════════╝

def real_time_step(psi, kin_half, dt, g, mu):
    """Strang split-step: ½ nonlinear → kinetic → ½ nonlinear. No sponge (PBC)."""
    rho = torch.abs(psi)**2
    psi = psi * torch.exp(-0.5j * dt * (g * rho - mu))
    psi = torch.fft.ifft(torch.fft.fft(psi) * kin_half)
    rho = torch.abs(psi)**2
    psi = psi * torch.exp(-0.5j * dt * (g * rho - mu))
    return psi

# ╔═══════════════════════════════════════════════════════════╗
# ║  DENSITY-PERTURBATION TRACKING                           ║
# ╚═══════════════════════════════════════════════════════════╝

def measure_drho(x, psi, rho0=1.0, side='right', x0=-150.0):
    """
    Compute δρ = |ψ|² − ρ₀.
    Track the right-moving packet (x > -20) or left-moving.
    Returns: (centre_of_mass, rms_width, peak_amplitude, density_at_com).
    """
    xn = x.cpu().numpy()
    drho = (torch.abs(psi)**2 - rho0).cpu().numpy()

    # Isolate the right-moving packet within central measurement zone
    if side == 'right':
        mask = (xn > x0 + 5.0) & (xn < x0 + 180.0)
    else:
        mask = (xn < x0 - 5.0) & (xn > x0 - 180.0)

    xm = xn[mask]
    dm = drho[mask]

    # Use |δρ| for tracking (the perturbation oscillates +/-)
    adr = np.abs(dm)
    total = np.sum(adr)

    if total < 1e-15:
        return None, None, None, None

    # Centre of mass of |δρ|
    com = np.sum(xm * adr) / total

    # RMS width
    rms = np.sqrt(np.sum((xm - com)**2 * adr) / total)

    # Peak amplitude
    peak = np.max(np.abs(dm))

    # δρ at nearest grid point to COM
    idx = np.argmin(np.abs(xm - com))
    drho_at_com = dm[idx]

    return com, rms, peak, drho_at_com

# ╔═══════════════════════════════════════════════════════════╗
# ║  SINGLE VELOCITY RUN                                     ║
# ╚═══════════════════════════════════════════════════════════╝

def run_single(x, k, dx, dev, k0, dt=0.01, T_total=150.0,
               A=0.01, sigma=8.0, x0=-60.0, sample_dt=0.5):
    """
    Run GP evolution for one carrier wavenumber k₀.
    Returns dict with trajectory, widths, oscillation data.
    """
    g = 1.0
    mu = 1.0
    L = float(x[-1].item() - x[0].item()) + float(dx)

    # Kinetic propagator (FULL step, used in Strang with nonlinear half-steps)
    kin_half = torch.exp(-0.5j * k**2 * dt).to(torch.complex128)
    # No sponge — pure periodic BC

    # Init (NO imaginary-time relaxation — it kills the oscillatory packet)
    psi = init_phonon_packet(x, k0, A=A, sigma=sigma, x0=x0)

    N_steps = int(T_total / dt)
    sample_interval = max(1, int(sample_dt / dt))
    print_interval = max(1, int(30.0 / dt))  # print every 30τ

    # Storage
    times, coms, widths, peaks, drho_com = [], [], [], [], []

    t0_wall = time.time()

    for step in range(N_steps + 1):
        t_now = step * dt

        if step % sample_interval == 0:
            with torch.no_grad():
                c, w, p, d = measure_drho(x, psi, side='right', x0=x0)
                if c is not None:
                    times.append(t_now)
                    coms.append(c)
                    widths.append(w)
                    peaks.append(p)
                    drho_com.append(d)

        if step % print_interval == 0 and step > 0:
            with torch.no_grad():
                rho2 = torch.abs(psi)**2
                norm = torch.sum(rho2).item() * dx
                rho_max = torch.max(rho2).item()
                rho_min = torch.min(rho2).item()
            c_now = coms[-1] if coms else float('nan')
            p_now = peaks[-1] if peaks else 0
            print(f"    t={t_now:7.1f}τ  com={c_now:7.2f}  pk={p_now:.4e}  "
                  f"ρ=[{rho_min:.6f},{rho_max:.6f}]  "
                  f"norm={norm/L:.6f}  [{time.time()-t0_wall:.0f}s]",
                  flush=True)

        if step < N_steps:
            psi = real_time_step(psi, kin_half, dt, g, mu)

    wall = time.time() - t0_wall

    times = np.array(times)
    coms = np.array(coms)
    widths = np.array(widths)
    peaks = np.array(peaks)
    drho_com = np.array(drho_com)

    # ── Actual velocity from COM trajectory ──
    if len(times) > 20:
        # Use early segment (before dispersion or sponge effects)
        n_use = min(len(times), len(times) // 2)
        n_use = max(n_use, 20)
        p_fit = np.polyfit(times[:n_use], coms[:n_use], 1)
        v_actual = p_fit[0]
    else:
        v_actual = 0.0

    # ── Average RMS width (use early 50%) ──
    if len(widths) > 10:
        n_early = max(10, len(widths) // 3)
        width_avg = np.mean(widths[:n_early])
    else:
        width_avg = np.nan

    # ── Initial RMS width (first 5 samples) ──
    if len(widths) >= 5:
        width_init = np.mean(widths[:5])
    else:
        width_init = np.nan

    # ── Internal oscillation period from δρ at COM ──
    T_internal = extract_period(times, drho_com)

    # ── Predicted group velocity ──
    vg_theory = group_vel(k0) if k0 > 0 else 1.0

    print(f"    DONE: k₀={k0:.2f}  v_g(theory)={vg_theory:.4f}  "
          f"v_actual={v_actual:.4f}  W_rms={width_avg:.2f}  "
          f"T_int={T_internal if T_internal else 'N/A'}  [{wall:.0f}s]")

    return {
        'k0': k0,
        'vg_theory': vg_theory,
        'v_actual': v_actual,
        'width_avg': width_avg,
        'width_init': width_init,
        'T_internal': T_internal,
        'times': times.tolist(),
        'coms': coms.tolist(),
        'widths': widths.tolist(),
        'peaks': peaks.tolist(),
        'drho_com': drho_com.tolist(),
        'wall_time': wall,
    }

# ╔═══════════════════════════════════════════════════════════╗
# ║  PERIOD EXTRACTION                                       ║
# ╚═══════════════════════════════════════════════════════════╝

def extract_period(times, signal):
    """Extract dominant oscillation period from time-series via FFT."""
    if len(times) < 20 or np.all(np.abs(signal) < 1e-15):
        return None

    dt_avg = np.mean(np.diff(times))
    n = len(signal)

    # Detrend
    sig = signal - np.mean(signal)
    if np.max(np.abs(sig)) < 1e-15:
        return None

    # Window
    window = np.hanning(n)
    sig = sig * window

    fft_vals = np.fft.rfft(sig)
    freqs = np.fft.rfftfreq(n, d=dt_avg)

    # Skip DC and very low frequencies
    min_idx = max(1, int(2.0 / (freqs[-1] + 1e-30)))  # skip f < 2 cycles
    if min_idx >= len(freqs):
        return None

    power = np.abs(fft_vals[min_idx:])**2
    freqs_cut = freqs[min_idx:]

    if len(power) == 0 or np.max(power) < 1e-30:
        return None

    peak_idx = np.argmax(power)
    f_dom = freqs_cut[peak_idx]

    if f_dom < 1e-10:
        return None

    return 1.0 / f_dom

# ╔═══════════════════════════════════════════════════════════╗
# ║  LORENTZ FITS                                            ║
# ╚═══════════════════════════════════════════════════════════╝

def gamma_func(v, cs=1.0):
    return 1.0 / np.sqrt(np.maximum(1e-30, 1.0 - (v / cs)**2))

def compute_R2(y_data, y_pred):
    ss_res = np.sum((y_data - y_pred)**2)
    ss_tot = np.sum((y_data - np.mean(y_data))**2)
    if ss_tot < 1e-30:
        return 0.0
    return 1.0 - ss_res / ss_tot

# ╔═══════════════════════════════════════════════════════════╗
# ║  PLOTTING                                                ║
# ╚═══════════════════════════════════════════════════════════╝

def make_plots(results, outdir):
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle('UHF Hydrodynamic Relativity v2 — Acoustic Metric\n'
                 'Bogoliubov Phonon Wave-Packet', fontsize=13)

    v_smooth = np.linspace(0.01, 0.99, 200)
    gamma_smooth = gamma_func(v_smooth)

    # ── Panel 1: Packet trajectories ──
    ax = axes[0, 0]
    for r in results:
        t = np.array(r['times'])
        c = np.array(r['coms'])
        ax.plot(t, c, label=f"k₀={r['k0']:.2f}, v={r['v_actual']:.3f}")
    ax.set_xlabel('t (ξ/c_s)')
    ax.set_ylabel('COM position (ξ)')
    ax.set_title('Density Perturbation Trajectories')
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    # ── Panel 2: RMS width vs time ──
    ax = axes[0, 1]
    for r in results:
        t = np.array(r['times'])
        w = np.array(r['widths'])
        ax.plot(t, w, label=f"k₀={r['k0']:.2f}")
    ax.set_xlabel('t (ξ/c_s)')
    ax.set_ylabel('RMS width (ξ)')
    ax.set_title('Envelope Width vs Time')
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    # ── Panel 3: Width vs velocity (Lorentz test) ──
    ax = axes[1, 0]
    v_arr = np.array([r['v_actual'] for r in results])
    w_arr = np.array([r['width_avg'] for r in results])
    valid = ~np.isnan(w_arr) & (w_arr > 0) & (v_arr > 0)
    if np.sum(valid) > 0:
        W0 = w_arr[valid][0]
        ax.plot(v_arr[valid], w_arr[valid] / W0, 'ro', ms=8, label='Measured W/W₀')
    ax.plot(v_smooth, 1.0 / gamma_smooth, 'b-', lw=2,
            label='$1/\\gamma = \\sqrt{1-v^2/c_s^2}$')
    ax.set_xlabel('v_actual / c_s')
    ax.set_ylabel('W(v) / W₀')
    ax.set_title('Width vs Velocity (Lorentz Test)')
    ax.legend()
    ax.set_xlim(0, 2.0)
    ax.set_ylim(0, 1.5)
    ax.grid(True, alpha=0.3)

    # ── Panel 4: Dispersion relation ──
    ax = axes[1, 1]
    k_arr = np.array([r['k0'] for r in results])
    T_arr = np.array([r['T_internal'] for r in results])
    valid_T = np.array([t is not None and t > 0 for t in T_arr])
    if np.sum(valid_T) > 0:
        omega_meas = 2 * np.pi / T_arr[valid_T]
        k_valid = k_arr[valid_T]
        ax.plot(k_valid, omega_meas, 'ro', ms=8, label='Measured ω')
    k_th = np.linspace(0.01, max(k_arr) * 1.2, 200)
    omega_th = omega_bog(k_th)
    omega_lin = k_th  # ω = c_s k (phonon limit)
    ax.plot(k_th, omega_th, 'b-', lw=2, label='Bogoliubov: $|k|\\sqrt{1+k^2/2}$')
    ax.plot(k_th, omega_lin, 'g--', lw=1.5, label='Phonon: $c_s|k|$')
    ax.set_xlabel('k₀ (1/ξ)')
    ax.set_ylabel('ω (c_s/ξ)')
    ax.set_title('Dispersion Relation Verification')
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    fname = os.path.join(outdir, 'hydro_relativity_v2.png')
    plt.savefig(fname, dpi=150)
    plt.close()
    return fname

# ╔═══════════════════════════════════════════════════════════╗
# ║  MAIN                                                    ║
# ╚═══════════════════════════════════════════════════════════╝

def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  UHF — Hydrodynamic Relativity v2 (Acoustic Metric)       ║")
    print("║  Bogoliubov Phonon: Length Contraction & Time Dilation     ║")
    print("║  1D GP, Split-Step Fourier, N=4096, L=400ξ (PBC)          ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    dev = setup_device()

    N = 4096
    L = 400.0           # large domain for periodic BC
    dt = 0.001          # small dt to prevent split-step instability at k_max
    T_total = 120.0     # packets travel <130ξ at v_g≤1.6
    A = 0.01
    sigma = 8.0
    x0 = -150.0         # far left, room for rightward propagation
    # Red Team spec velocities → used as k₀ (Galilean boost wavenumber)
    k0_list = [0.1, 0.5, 0.8, 0.95]

    outdir = 'UHF_HydroRelativity_v2_results'
    os.makedirs(outdir, exist_ok=True)

    x, k, dx = build_grid(N, L, dev)
    print(f"\n  Grid: N={N}, L={L:.0f}ξ, Δx={dx:.4f}ξ")
    print(f"  Packet: A={A}, σ={sigma}ξ, x₀={x0}ξ")
    print(f"  Evolution: dt={dt}, T={T_total}ξ/c_s")
    print(f"  Carrier wavenumbers k₀: {k0_list}")
    print(f"  Predicted v_g: {[f'{group_vel(k0):.3f}' for k0 in k0_list]}")
    print()

    t0 = time.time()
    results = []

    for k0 in k0_list:
        vg = group_vel(k0)
        print(f"{'='*70}")
        print(f"  k₀ = {k0}  (v_g theory = {vg:.4f} c_s)")
        print(f"{'='*70}")

        r = run_single(x, k, dx, dev, k0, dt=dt, T_total=T_total,
                       A=A, sigma=sigma, x0=x0, sample_dt=0.5)
        results.append(r)
        torch.cuda.empty_cache()
        print()

    # ╔═══════════════════════════════════════════════════════╗
    # ║  AGGREGATE                                            ║
    # ╚═══════════════════════════════════════════════════════╝

    print(f"{'='*70}")
    print(f"  AGGREGATE RESULTS")
    print(f"{'='*70}\n")

    k_arr = np.array([r['k0'] for r in results])
    va_arr = np.array([r['v_actual'] for r in results])
    w_arr = np.array([r['width_avg'] for r in results])
    T_arr = np.array([r['T_internal'] for r in results])
    vg_arr = np.array([r['vg_theory'] for r in results])

    # Normalize widths to first
    valid_w = ~np.isnan(w_arr) & (w_arr > 0)
    W0 = w_arr[0] if valid_w[0] else 1.0

    # Normalize periods to first valid
    valid_T = np.array([t is not None and t > 0 for t in T_arr])
    T0 = T_arr[valid_T][0] if np.sum(valid_T) > 0 else 1.0

    print(f"  {'k₀':>6s}  {'v_g(th)':>8s}  {'v_act':>8s}  {'W/W₀':>8s}  "
          f"{'1/γ(v)':>8s}  {'T/T₀':>8s}  {'γ(v)':>8s}")
    print(f"  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")

    for i, r in enumerate(results):
        v = va_arr[i]
        w_ratio = w_arr[i] / W0 if valid_w[i] else float('nan')
        # γ with c_s=1 — note v_actual may be > c_s
        if abs(v) < 1.0:
            inv_g = np.sqrt(1 - v**2)
            g_v = 1.0 / inv_g
        else:
            inv_g = float('nan')
            g_v = float('nan')
        T_ratio = T_arr[i] / T0 if valid_T[i] else float('nan')
        print(f"  {k_arr[i]:6.2f}  {vg_arr[i]:8.4f}  {v:8.4f}  {w_ratio:8.4f}  "
              f"{inv_g:8.4f}  {T_ratio:8.4f}  {g_v:8.4f}")

    print()

    # ── Lorentz fit for width ──
    # Only use points where v < c_s
    sub_cs = va_arr < 1.0
    if np.sum(sub_cs & valid_w) >= 2:
        v_sub = va_arr[sub_cs & valid_w]
        w_sub = w_arr[sub_cs & valid_w] / W0
        lorentz_pred = np.sqrt(1 - v_sub**2)
        R2_L = compute_R2(w_sub, lorentz_pred)
    else:
        R2_L = None

    # ── Lorentz fit for period ──
    if np.sum(sub_cs & valid_T) >= 2:
        v_sub_T = va_arr[sub_cs & valid_T]
        T_sub = T_arr[sub_cs & valid_T] / T0
        gamma_pred = 1.0 / np.sqrt(1 - v_sub_T**2)
        R2_T = compute_R2(T_sub, gamma_pred)
    else:
        R2_T = None

    # ── Dispersion relation R² ──
    if np.sum(valid_T) >= 2:
        omega_meas = 2 * np.pi / T_arr[valid_T]
        omega_pred = omega_bog(k_arr[valid_T])
        R2_disp = compute_R2(omega_meas, omega_pred)
    else:
        R2_disp = None

    R2_L_s = f"{R2_L:.4f}" if R2_L is not None else "N/A"
    R2_T_s = f"{R2_T:.4f}" if R2_T is not None else "N/A"
    R2_d_s = f"{R2_disp:.4f}" if R2_disp is not None else "N/A"

    print(f"  R² (Length Contraction, W vs 1/γ):  {R2_L_s}")
    print(f"  R² (Time Dilation, T vs γ):         {R2_T_s}")
    print(f"  R² (Dispersion ω vs Bogoliubov):    {R2_d_s}")
    print()

    # ── Key physics finding ──
    all_v = va_arr[valid_w]
    if len(all_v) > 0 and np.all(all_v > 0.9):
        physics_note = ("All packets travel at v_g ≈ c_s regardless of k₀ "
                        "(phonon regime: v_g ≥ c_s). No sub-luminal packets exist "
                        "in Bogoliubov dispersion → Lorentz contraction test "
                        "inapplicable (v never < c_s).")
    elif len(all_v) > 0:
        physics_note = (f"Measured velocities: {[f'{v:.3f}' for v in va_arr]}. "
                        f"Width ratio W/W₀: {[f'{w/W0:.3f}' for w in w_arr if not np.isnan(w)]}.")
    else:
        physics_note = "Insufficient valid data."

    # ── Conclusion ──
    L_pass = R2_L is not None and R2_L > 0.8
    T_pass = R2_T is not None and R2_T > 0.8

    if L_pass and T_pass:
        conclusion = (f"Acoustic wave-packet shows Lorentz contraction and time "
                      f"dilation matching γ to R²_L={R2_L_s}, R²_T={R2_T_s} → "
                      f"hydrodynamic special relativity confirmed in phonon sector")
    elif L_pass:
        conclusion = (f"Partial: length contraction R²_L={R2_L_s} but time "
                      f"dilation R²_T={R2_T_s}")
    else:
        conclusion = (f"Deviation → hypothesis falsified "
                      f"(R²_L={R2_L_s}, R²_T={R2_T_s}). "
                      f"{physics_note}")

    print(f"  CONCLUSION: {conclusion}")
    print()

    # ── Plots ──
    print("  Generating plots...")
    plot_fname = make_plots(results, outdir)
    print(f"  Plot: {plot_fname}")

    # ── JSON ──
    json_data = {
        'version': 'v2_acoustic_metric',
        'grid': {'N': N, 'L': L, 'dx': float(dx)},
        'params': {'A': A, 'sigma': sigma, 'x0': x0, 'dt': dt, 'T_total': T_total},
        'results': [{
            'k0': r['k0'],
            'vg_theory': r['vg_theory'],
            'v_actual': r['v_actual'],
            'width_avg': float(r['width_avg']) if not np.isnan(r['width_avg']) else None,
            'T_internal': r['T_internal'],
            'wall_time': r['wall_time'],
        } for r in results],
        'R2_length': R2_L,
        'R2_time': R2_T,
        'R2_dispersion': R2_disp,
        'physics_note': physics_note,
        'conclusion': conclusion,
    }
    json_fname = os.path.join(outdir, 'results_v2.json')
    with open(json_fname, 'w') as f:
        json.dump(json_data, f, indent=2, default=str)
    print(f"  JSON: {json_fname}")

    total = time.time() - t0
    print(f"\n  Total wall time: {total:.0f}s")
    print(f"  CONCLUSION: {conclusion}")

if __name__ == '__main__':
    main()
