#!/usr/bin/env python3
"""
UHF Hydrodynamic Relativity v3 — Relativistic Superfluid Engine
================================================================
1+1D real scalar phi^4 Klein-Gordon equation:
  phi_tt = c^2 phi_xx - lambda phi (phi^2 - eta^2)

Kink soliton: phi = tanh(gamma*(x-vt)/sqrt(2))
  - Width contracts as FWHM(v) = FWHM_0 / gamma   (Lorentz contraction)
  - Shape mode period dilates: T(v) = T_0 * gamma  (time dilation)
  - Energy: E(v) = gamma * E_0                     (relativistic energy)

GPU: RTX 3090, PyTorch, float64
"""
import torch
import numpy as np
import json, os, time
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ──── Physics constants (natural units) ────────────────────────
C       = 1.0
ETA     = 1.0
LAM     = 1.0

# ──── Grid & integration ───────────────────────────────────────
N        = 16384
L        = 400.0
DT       = 0.005
T_TOTAL  = 200.0
X0       = -100.0
A_SHAPE  = 0.02       # shape mode excitation amplitude

# ──── Sponge ───────────────────────────────────────────────────
SPONGE_W = 30.0
SPONGE_S = 5.0

# ──── Test velocities ─────────────────────────────────────────
VELOCITIES = [0.50, 0.70, 0.85, 0.95]
MEAS_DT    = 0.5       # measurement interval

# ──── Analytical reference values ──────────────────────────────
OMEGA_SHAPE = np.sqrt(1.5)                            # shape mode freq
T_SHAPE_0   = 2.0 * np.pi / OMEGA_SHAPE              # rest period ~ 5.13
E_REST      = 2.0 * np.sqrt(2.0) / 3.0               # rest energy ~ 0.943
FWHM_REST   = 2.0 * np.arccosh(np.sqrt(2.0)) * np.sqrt(2.0)  # ~ 2.493

OUT_DIR = "UHF_HydroRelativity_v3_results"


# ═══════════════════════════════════════════════════════════════
#  Setup
# ═══════════════════════════════════════════════════════════════

def setup_device():
    assert torch.cuda.is_available(), "CUDA required"
    dev = torch.device('cuda:0')
    p = torch.cuda.get_device_properties(dev)
    print(f"GPU: {p.name} ({p.total_memory / 1e9:.1f} GB)")
    return dev


def build_grid(dev):
    dx = L / N
    x = torch.linspace(-L / 2, L / 2 - dx, N,
                        dtype=torch.float64, device=dev)
    return x, dx


def gamma_factor(v):
    return 1.0 / np.sqrt(1.0 - v * v)


# ═══════════════════════════════════════════════════════════════
#  Initial conditions
# ═══════════════════════════════════════════════════════════════

def init_kink(x, v):
    """Exact boosted kink + small shape-mode excitation."""
    g   = gamma_factor(v)
    z   = g * (x - X0) / np.sqrt(2.0)          # contracted coord
    th  = torch.tanh(z)
    sch = 1.0 / torch.cosh(z)

    # field
    phi = th + A_SHAPE * sch * th               # kink + shape mode

    # conjugate momentum  pi = dphi/dt
    #   kink part:   -v*g/sqrt2 * sech^2(z)
    #   shape part:  -v * d/dx[ A*sech(z)*tanh(z) ]
    #              = -v * A * g/sqrt2 * sech(z)*(1 - 2*tanh^2(z))
    pi = (-v * g / np.sqrt(2.0)) * (
            sch**2 + A_SHAPE * sch * (1.0 - 2.0 * th**2))

    return phi, pi


# ═══════════════════════════════════════════════════════════════
#  Spatial derivative (FD Laplacian, Dirichlet BC)
# ═══════════════════════════════════════════════════════════════

def laplacian(phi, dx2_inv, phi_L=-1.0, phi_R=1.0):
    lap = torch.empty_like(phi)
    lap[1:-1] = (phi[2:] - 2.0 * phi[1:-1] + phi[:-2]) * dx2_inv
    lap[0]    = (phi[1]  - 2.0 * phi[0]    + phi_L)     * dx2_inv
    lap[-1]   = (phi_R   - 2.0 * phi[-1]   + phi[-2])   * dx2_inv
    return lap


def force(phi, dx2_inv):
    """F = c^2 lap(phi) - lam * phi * (phi^2 - eta^2)"""
    return C * C * laplacian(phi, dx2_inv) - LAM * phi * (phi * phi - ETA * ETA)


# ═══════════════════════════════════════════════════════════════
#  Sponge layer
# ═══════════════════════════════════════════════════════════════

def build_sponge(x):
    sigma = torch.zeros_like(x)
    x_lo = -L / 2 + SPONGE_W
    x_hi =  L / 2 - SPONGE_W
    ml = x < x_lo
    mr = x > x_hi
    sigma[ml] = SPONGE_S * ((x_lo - x[ml]) / SPONGE_W) ** 2
    sigma[mr] = SPONGE_S * ((x[mr] - x_hi) / SPONGE_W) ** 2
    return sigma


def apply_sponge(phi, pi, sigma, x):
    damp = torch.exp(-sigma * DT)
    # vacuum targets: -1 left, +1 right (sponge only active at edges)
    tgt = torch.where(x < 0,
                      torch.tensor(-1.0, dtype=x.dtype, device=x.device),
                      torch.tensor( 1.0, dtype=x.dtype, device=x.device))
    phi = tgt + damp * (phi - tgt)
    pi  = damp * pi
    return phi, pi


# ═══════════════════════════════════════════════════════════════
#  Diagnostics
# ═══════════════════════════════════════════════════════════════

def measure_kink(phi, x, dx):
    """Return (zero-crossing position, FWHM of sech^2 bump)."""
    p = phi.cpu().numpy()
    xc = x.cpu().numpy()

    # zero crossing (closest to expected)
    signs = np.sign(p)
    idx = np.where(np.diff(signs))[0]
    if len(idx) == 0:
        return np.nan, np.nan
    mid = len(p) // 2
    best = idx[np.argmin(np.abs(idx - mid))]
    denom = p[best + 1] - p[best]
    x_cross = xc[best] - p[best] * dx / denom if abs(denom) > 1e-30 else np.nan

    # FWHM of (1 - phi^2)
    prof = 1.0 - p * p
    pk   = np.max(prof)
    above = np.where(prof > 0.5 * pk)[0]
    fwhm = xc[above[-1]] - xc[above[0]] if len(above) > 1 else np.nan

    return x_cross, fwhm


def compute_energy(phi, pi, dx, dx2_inv):
    """E = integral [ 1/2 pi^2 + 1/2 (dphi/dx)^2 + V(phi) ] dx"""
    # gradient via central FD (same BC)
    gp = torch.empty_like(phi)
    gp[1:-1] = (phi[2:] - phi[:-2]) / (2.0 * dx)
    gp[0]    = (phi[1]  - (-1.0))   / (2.0 * dx)
    gp[-1]   = (1.0   - phi[-2])    / (2.0 * dx)

    e_density = 0.5 * pi * pi + 0.5 * gp * gp + 0.25 * (phi * phi - 1.0) ** 2
    return (torch.sum(e_density) * dx).item()


# ═══════════════════════════════════════════════════════════════
#  Single-velocity run
# ═══════════════════════════════════════════════════════════════

def run_single(v, x, dx, dev):
    g = gamma_factor(v)
    n_steps   = int(T_TOTAL / DT)
    meas_step = max(1, int(MEAS_DT / DT))
    dx2_inv   = 1.0 / (dx * dx)

    print(f"\n{'=' * 60}")
    print(f"  v/c = {v:.2f}   gamma = {g:.4f}")
    print(f"  FWHM_expected = {FWHM_REST / g:.4f}   T_shape_exp = {T_SHAPE_0 * g:.3f}"
          f"   E_exp = {E_REST * g:.4f}")
    print(f"  steps = {n_steps}   meas every {meas_step}")
    print(f"{'=' * 60}")

    phi, pi = init_kink(x, v)
    sigma   = build_sponge(x)

    times, centers, fwhms, energies = [], [], [], []
    t0 = time.time()
    quarter = n_steps // 4

    for s in range(n_steps):
        t_now = s * DT

        # ---- measurement ----
        if s % meas_step == 0:
            xc, fw = measure_kink(phi, x, dx)
            E = compute_energy(phi, pi, dx, dx2_inv)
            times.append(t_now)
            centers.append(xc)
            fwhms.append(fw)
            energies.append(E)

        # ---- leapfrog ----
        F = force(phi, dx2_inv)
        pi  = pi + 0.5 * DT * F
        phi = phi + DT * pi
        F = force(phi, dx2_inv)
        pi  = pi + 0.5 * DT * F

        # ---- sponge ----
        phi, pi = apply_sponge(phi, pi, sigma, x)

        # ---- progress ----
        if quarter > 0 and (s + 1) % quarter == 0:
            xc, fw = measure_kink(phi, x, dx)
            E = compute_energy(phi, pi, dx, dx2_inv)
            print(f"  t={t_now + DT:7.1f}: x_c={xc:+8.3f}  FWHM={fw:.4f}"
                  f"  E={E:.4f}  [{time.time() - t0:.0f}s]")

    wall = time.time() - t0
    print(f"  Done in {wall:.1f}s")
    return dict(v=v, gamma=g, wall=wall,
                times=np.asarray(times),
                centers=np.asarray(centers),
                fwhms=np.asarray(fwhms),
                energies=np.asarray(energies))


# ═══════════════════════════════════════════════════════════════
#  Period extraction
# ═══════════════════════════════════════════════════════════════

def extract_period(times, centers, v):
    """FFT the zero-crossing residual to get shape-mode period."""
    residual = centers - (v * times + X0)
    ok = ~np.isnan(residual)
    t, r = times[ok], residual[ok]
    if len(r) < 20:
        return np.nan

    # discard first 20 time-units (transient)
    keep = t > t[0] + 20.0
    t, r = t[keep], r[keep]
    if len(r) < 20:
        return np.nan

    # detrend
    c = np.polyfit(t, r, 1)
    r = r - np.polyval(c, t)

    # Hanning window + FFT
    win = np.hanning(len(r))
    spec = np.abs(np.fft.rfft(r * win)) ** 2
    dt_m = t[1] - t[0]
    freqs = np.fft.rfftfreq(len(r), d=dt_m)
    spec[0] = 0.0

    pk = np.argmax(spec)
    if pk == 0 or freqs[pk] == 0:
        return np.nan

    # parabolic interpolation for sub-bin accuracy
    if 0 < pk < len(spec) - 1:
        a, b, c_ = np.log(spec[pk - 1] + 1e-30), np.log(spec[pk] + 1e-30), np.log(spec[pk + 1] + 1e-30)
        delta = 0.5 * (a - c_) / (a - 2 * b + c_) if abs(a - 2 * b + c_) > 1e-30 else 0.0
        f_peak = freqs[pk] + delta * (freqs[1] - freqs[0])
    else:
        f_peak = freqs[pk]

    return 1.0 / f_peak if f_peak > 0 else np.nan


# ═══════════════════════════════════════════════════════════════
#  R² helper
# ═══════════════════════════════════════════════════════════════

def R2(obs, pred):
    m = ~np.isnan(obs) & ~np.isnan(pred)
    o, p = obs[m], pred[m]
    if len(o) < 2:
        return np.nan
    ss_r = np.sum((o - p) ** 2)
    ss_t = np.sum((o - o.mean()) ** 2)
    return 1.0 - ss_r / ss_t if ss_t > 0 else (1.0 if ss_r == 0 else -np.inf)


# ═══════════════════════════════════════════════════════════════
#  Plot
# ═══════════════════════════════════════════════════════════════

def make_plots(results):
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    vs   = [r['v'] for r in results]
    fr   = [r['fwhm_mean'] / FWHM_REST for r in results]
    tr   = [r['T_period']  / T_SHAPE_0  for r in results]
    er   = [r['E_mean']    / E_REST     for r in results]

    vt = np.linspace(0.01, 0.99, 300)
    gt = 1.0 / np.sqrt(1.0 - vt ** 2)

    # (a) Lorentz contraction
    ax = axes[0, 0]
    ax.plot(vt, 1.0 / gt, 'k-', lw=2, label=r'$1/\gamma$')
    ax.plot(vs, fr, 'ro', ms=10, label='Measured')
    ax.set(xlabel='v / c', ylabel=r'$L/L_0$',
           title='Lorentz Contraction', xlim=(0, 1), ylim=(0, 1.15))
    ax.legend(); ax.grid(alpha=0.3)

    # (b) Time dilation
    ax = axes[0, 1]
    ax.plot(vt, gt, 'k-', lw=2, label=r'$\gamma$')
    ax.plot(vs, tr, 'bs', ms=10, label='Measured')
    ax.set(xlabel='v / c', ylabel=r'$T/T_0$',
           title='Time Dilation', xlim=(0, 1))
    ax.legend(); ax.grid(alpha=0.3)

    # (c) Relativistic energy
    ax = axes[1, 0]
    ax.plot(vt, gt, 'k-', lw=2, label=r'$\gamma$')
    ax.plot(vs, er, 'g^', ms=10, label='Measured')
    ax.set(xlabel='v / c', ylabel=r'$E/E_0$',
           title='Relativistic Energy', xlim=(0, 1))
    ax.legend(); ax.grid(alpha=0.3)

    # (d) FWHM vs time
    ax = axes[1, 1]
    for r in results:
        ax.plot(r['times'], r['fwhms'], label=f"v={r['v']:.2f}", alpha=0.7)
    ax.set(xlabel='Time', ylabel='FWHM', title='FWHM Stability')
    ax.legend(); ax.grid(alpha=0.3)

    fig.suptitle('UHF Relativity v3 — Klein-Gordon Kink Soliton', fontsize=15, y=0.98)
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    path = os.path.join(OUT_DIR, 'hydro_relativity_v3.png')
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"Plot saved: {path}")
    return path


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  UHF Hydrodynamic Relativity v3 — Relativistic Superfluid Engine")
    print(f"  N={N}  L={L}  dt={DT}  T={T_TOTAL}  x0={X0}")
    print(f"  omega_shape = {OMEGA_SHAPE:.4f}   T_shape0 = {T_SHAPE_0:.4f}")
    print(f"  E0 = {E_REST:.4f}   FWHM0 = {FWHM_REST:.4f}")
    print(f"  Velocities: {VELOCITIES}")
    print("=" * 70)

    dev = setup_device()
    x, dx = build_grid(dev)
    print(f"  dx = {dx:.5f}   CFL = {DT / dx:.3f}")

    os.makedirs(OUT_DIR, exist_ok=True)

    all_res = []
    for v in VELOCITIES:
        d = run_single(v, x, dx, dev)

        good = d['fwhms'][~np.isnan(d['fwhms'])]
        d['fwhm_mean'] = np.mean(good) if len(good) else np.nan
        d['fwhm_std']  = np.std(good)  if len(good) else np.nan
        d['E_mean']    = np.mean(d['energies'])
        d['T_period']  = extract_period(d['times'], d['centers'], v)
        all_res.append(d)

    # ── table ──────────────────────────────────────────────────
    print("\n" + "=" * 100)
    hdr = f"{'v/c':>6} {'gamma':>8} {'FWHM':>8} {'L/L0':>8} {'1/g':>8} {'T_meas':>8} {'T/T0':>8} {'g_th':>8} {'E/E0':>8}"
    print(hdr)
    print("-" * 100)

    fwhm_r, fwhm_th = [], []
    per_r,  per_th  = [], []
    ene_r,  ene_th  = [], []

    for r in all_res:
        g = r['gamma']
        lr = r['fwhm_mean'] / FWHM_REST
        tp = r['T_period'] / T_SHAPE_0 if not np.isnan(r['T_period']) else np.nan
        er = r['E_mean'] / E_REST
        t_p = r['T_period'] if not np.isnan(r['T_period']) else 0.0

        print(f"{r['v']:6.2f} {g:8.4f} {r['fwhm_mean']:8.4f} {lr:8.4f} {1/g:8.4f} "
              f"{t_p:8.3f} {tp if not np.isnan(tp) else 0:8.4f} {g:8.4f} {er:8.4f}")

        fwhm_r.append(lr);  fwhm_th.append(1.0 / g)
        per_r.append(tp);   per_th.append(g)
        ene_r.append(er);   ene_th.append(g)

    fwhm_r  = np.array(fwhm_r);  fwhm_th = np.array(fwhm_th)
    per_r   = np.array(per_r);   per_th  = np.array(per_th)
    ene_r   = np.array(ene_r);   ene_th  = np.array(ene_th)

    R2_L = R2(fwhm_r, fwhm_th)
    R2_T = R2(per_r,  per_th)
    R2_E = R2(ene_r,  ene_th)

    print("-" * 100)
    print(f"R²(L) = {R2_L:.6f}")
    print(f"R²(T) = {R2_T:.6f}")
    print(f"R²(E) = {R2_E:.6f}")

    plot_path = make_plots(all_res)

    th = 0.99
    ok = R2_L > th and R2_E > th
    if ok:
        conclusion = (f"Relativistic Klein-Gordon superfluid yields Lorentz contraction "
                      f"and relativistic energy matching gamma to "
                      f"R²_L={R2_L:.4f}, R²_E={R2_E:.4f}"
                      + (f", R²_T={R2_T:.4f}" if not np.isnan(R2_T) else "")
                      + " → hydrodynamic special relativity confirmed")
    else:
        conclusion = (f"R²_L={R2_L:.4f}, R²_T={'%.4f'%R2_T if not np.isnan(R2_T) else 'N/A'}, "
                      f"R²_E={R2_E:.4f} → Deviation → hypothesis falsified")

    print(f"\n{conclusion}")

    with open(os.path.join(OUT_DIR, 'results_v3.json'), 'w') as f:
        json.dump(dict(
            velocities=VELOCITIES,
            fwhm_ratios=fwhm_r.tolist(),
            period_ratios=[float(x) if not np.isnan(x) else None for x in per_r],
            energy_ratios=ene_r.tolist(),
            R2_L=R2_L,
            R2_T=float(R2_T) if not np.isnan(R2_T) else None,
            R2_E=R2_E,
            conclusion=conclusion,
            plot=plot_path
        ), f, indent=2)
    print(f"JSON saved: {OUT_DIR}/results_v3.json")


if __name__ == '__main__':
    main()
