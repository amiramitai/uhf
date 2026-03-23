#!/usr/bin/env python3
"""
UHF Hydrodynamic General Relativity v2 — 1/r Gravitational Lensing
====================================================================
2D Klein-Gordon with space-dependent wave speed c(r)²:
    φ_tt = c(r)² ∇²φ - λφ(φ²-η²)

c(r)² = c₀²[1 - V₀/(r + ε)]   — 1/r acoustic metric (Schwarzschild-like)

Slower speed near centre → rays bend inward (gravitational attraction).
Weak-field limit:  Δθ ≈ V₀/b   for b >> ε   → predicts 1/b law.

Background φ=η is exact equilibrium since ∇²η = 0 and η(η²−η²) = 0.

GPU: RTX 3090, PyTorch, float64, leapfrog + spectral Laplacian (rFFT2)
"""
import torch
import numpy as np
import json, os, time
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ──── Physics ──────────────────────────────────────────────────
C0    = 1.0           # background wave speed
ETA   = 1.0           # vacuum value
LAM   = 1.0           # quartic coupling

# 1/r acoustic lens
V0    = 3.0           # lens strength  (controls deflection magnitude)
EPS   = 5.0           # regularisation  (c²_min = 1 - V0/EPS = 0.40)

# ──── Grid ─────────────────────────────────────────────────────
NX     = 1024
NY     = 1024
LX     = 120.0
LY     = 120.0

# ──── Integration ──────────────────────────────────────────────
DT     = 0.005
T_MAX  = 90.0

# ──── Wave packet ──────────────────────────────────────────────
K0      = 15.0
W_PKT   = 3.0
A_PKT   = 0.02
X_START = -40.0
OMEGA   = float(np.sqrt(C0**2 * K0**2 + 2 * LAM * ETA**2))

# ──── Sponge ──────────────────────────────────────────────────
SPONGE_W = 10.0
SPONGE_S = 5.0

# ──── Impact parameters ───────────────────────────────────────
B_VALUES = [8.0, 10.0, 12.0, 14.0, 16.0, 18.0,
            20.0, 22.0, 24.0, 26.0, 28.0, 30.0]

# ──── Measurement ─────────────────────────────────────────────
MEAS_DT  = 0.5
X_IN_LO  = -38.0
X_IN_HI  = -18.0
X_OUT_LO =  18.0
X_OUT_HI =  38.0

OUT_DIR = "UHF_HydroGR_Lensing_v2_results"


# ═══════════════════════════════════════════════════════════════
def setup_device():
    assert torch.cuda.is_available(), "CUDA required"
    dev = torch.device('cuda:0')
    p = torch.cuda.get_device_properties(dev)
    print(f"GPU: {p.name} ({p.total_mem / 1e9:.1f} GB)"
          if hasattr(p, 'total_mem') else
          f"GPU: {p.name} ({p.total_memory / 1e9:.1f} GB)")
    return dev


def build_grid(dev):
    dx = LX / NX
    dy = LY / NY
    x1d = torch.linspace(-LX/2, LX/2 - dx, NX, dtype=torch.float64, device=dev)
    y1d = torch.linspace(-LY/2, LY/2 - dy, NY, dtype=torch.float64, device=dev)
    yy, xx = torch.meshgrid(y1d, x1d, indexing='ij')
    kx_r = torch.fft.rfftfreq(NX, d=dx, device=dev).to(torch.float64) * 2 * np.pi
    ky   = torch.fft.fftfreq(NY, d=dy, device=dev).to(torch.float64) * 2 * np.pi
    ky2d, kx2d = torch.meshgrid(ky, kx_r, indexing='ij')
    neg_k2 = -(kx2d**2 + ky2d**2)
    return xx, yy, dx, dy, neg_k2


def build_c2(xx, yy):
    """c²(r) = c₀²[1 - V₀/(r + ε)]  — 1/r acoustic metric."""
    r = torch.sqrt(xx**2 + yy**2)
    c2 = C0**2 * (1.0 - V0 / (r + EPS))
    c2_min = c2.min().item()
    print(f"  c²(r) 1/r profile: V₀={V0}, ε={EPS}, c²_min={c2_min:.4f}")
    assert c2_min > 0, f"c² went negative! c²_min={c2_min}"
    return c2


def build_sponge(xx, yy):
    s = torch.zeros_like(xx)
    x_lo = -LX/2 + SPONGE_W
    x_hi =  LX/2 - SPONGE_W
    y_lo = -LY/2 + SPONGE_W
    y_hi =  LY/2 - SPONGE_W
    ml, mr = xx < x_lo, xx > x_hi
    mb, mt = yy < y_lo, yy > y_hi
    s[ml] = ((x_lo - xx[ml]) / SPONGE_W).clamp(0, 1)**2 * SPONGE_S
    s[mr] = ((xx[mr] - x_hi) / SPONGE_W).clamp(0, 1)**2 * SPONGE_S
    s[mb] = torch.maximum(s[mb], ((y_lo - yy[mb]) / SPONGE_W).clamp(0, 1)**2 * SPONGE_S)
    s[mt] = torch.maximum(s[mt], ((yy[mt] - y_hi) / SPONGE_W).clamp(0, 1)**2 * SPONGE_S)
    return s


# ═══════════════════════════════════════════════════════════════
def laplacian(phi, neg_k2):
    return torch.fft.irfft2(neg_k2 * torch.fft.rfft2(phi),
                            s=(phi.shape[0], phi.shape[1]))


def force(phi, c2, neg_k2):
    return c2 * laplacian(phi, neg_k2) - LAM * phi * (phi * phi - ETA * ETA)


# ═══════════════════════════════════════════════════════════════
def init_packet(xx, yy, b):
    env = A_PKT * torch.exp(-((xx - X_START)**2 + (yy - b)**2) / (2 * W_PKT**2))
    phi = ETA + env * torch.cos(K0 * xx)
    pi  = OMEGA * env * torch.sin(K0 * xx)
    return phi, pi


def track_com(phi, xx, yy):
    dphi2 = (phi - ETA)**2
    total = dphi2.sum()
    if total.item() < 1e-30:
        return np.nan, np.nan
    return ((dphi2 * xx).sum() / total).item(), ((dphi2 * yy).sum() / total).item()


# ═══════════════════════════════════════════════════════════════
def run_single(b, xx, yy, c2, sponge, neg_k2):
    n_steps   = int(T_MAX / DT)
    meas_step = max(1, int(MEAS_DT / DT))
    damp      = torch.exp(-sponge * DT)

    phi, pi = init_packet(xx, yy, b)
    times, cxs, cys = [], [], []
    t0 = time.time()

    for s in range(n_steps):
        if s % meas_step == 0:
            cx, cy = track_com(phi, xx, yy)
            times.append(s * DT)
            cxs.append(cx)
            cys.append(cy)

        F = force(phi, c2, neg_k2)
        pi  = pi + 0.5 * DT * F
        phi = phi + DT * pi
        F = force(phi, c2, neg_k2)
        pi  = pi + 0.5 * DT * F

        phi = ETA + damp * (phi - ETA)
        pi  = damp * pi

    wall = time.time() - t0
    print(f"  b={b:5.1f}: {wall:.1f}s  ({len(times)} pts)")
    return np.asarray(times), np.asarray(cxs), np.asarray(cys)


# ═══════════════════════════════════════════════════════════════
def compute_deflection(times, cxs, cys):
    ok = ~(np.isnan(cxs) | np.isnan(cys))
    x, y = cxs[ok], cys[ok]
    m_in  = (x >= X_IN_LO) & (x <= X_IN_HI)
    m_out = (x >= X_OUT_LO) & (x <= X_OUT_HI)
    if m_in.sum() < 3 or m_out.sum() < 3:
        return np.nan, np.nan, np.nan
    c_in  = np.polyfit(x[m_in],  y[m_in],  1)
    c_out = np.polyfit(x[m_out], y[m_out], 1)
    return np.arctan(c_out[0]) - np.arctan(c_in[0]), np.arctan(c_in[0]), np.arctan(c_out[0])


def R2(obs, pred):
    m = ~np.isnan(obs) & ~np.isnan(pred)
    o, p = obs[m], pred[m]
    if len(o) < 2:
        return np.nan
    ss_r = np.sum((o - p)**2)
    ss_t = np.sum((o - o.mean())**2)
    return 1.0 - ss_r / ss_t if ss_t > 0 else (1.0 if ss_r == 0 else -np.inf)


# ═══════════════════════════════════════════════════════════════
def make_plots(bs, dthetas, fit_A, fit_alpha, r2_val, traj_data):
    fig, axes = plt.subplots(1, 3, figsize=(18, 5.5))
    bs_a = np.asarray(bs)
    dt_a = np.asarray(dthetas)
    ok = ~np.isnan(dt_a) & (np.abs(dt_a) > 1e-12)

    b_fit = np.linspace(bs_a[ok].min(), bs_a[ok].max(), 200)

    ax = axes[0]
    ax.plot(bs_a[ok], np.abs(dt_a[ok]), 'ro', ms=8, label='|Δθ| measured')
    ax.plot(b_fit, fit_A * b_fit**fit_alpha, 'k-', lw=2,
            label=f'{fit_A:.4f}·b^{{{fit_alpha:.3f}}}')
    ax.set(xlabel='b [ξ]', ylabel='|Δθ| [rad]',
           title=f'Deflection (R²={r2_val:.4f})')
    ax.legend(); ax.grid(alpha=0.3)

    ax = axes[1]
    ax.plot(np.log(bs_a[ok]), np.log(np.abs(dt_a[ok])), 'ro', ms=8)
    ax.plot(np.log(b_fit), np.log(fit_A) + fit_alpha * np.log(b_fit), 'k-', lw=2)
    ax.set(xlabel='ln(b)', ylabel='ln(|Δθ|)',
           title=f'Log-log slope = {fit_alpha:.3f}')
    ax.grid(alpha=0.3)

    ax = axes[2]
    for bv, (t, cx, cy) in traj_data.items():
        ok2 = ~(np.isnan(cx) | np.isnan(cy))
        ax.plot(cx[ok2], cy[ok2], '-', alpha=0.7, label=f'b={bv:.0f}')
    # draw the ε circle
    th = np.linspace(0, 2 * np.pi, 100)
    ax.plot(EPS * np.cos(th), EPS * np.sin(th), 'k--', alpha=0.4, label=f'ε={EPS}')
    ax.set(xlabel='x', ylabel='y', title='Trajectories',
           xlim=(-45, 45), ylim=(-5, 35))
    ax.legend(fontsize=6, ncol=3); ax.grid(alpha=0.3)
    ax.set_aspect('equal')

    fig.suptitle('UHF GR v2 — Phonon Lensing (1/r acoustic metric)', fontsize=14, y=1.02)
    plt.tight_layout()
    path = os.path.join(OUT_DIR, 'gr_lensing_v2.png')
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"Plot: {path}")
    return path


# ═══════════════════════════════════════════════════════════════
def main():
    print("=" * 70)
    print("  UHF Hydrodynamic GR v2 — 1/r Gravitational Lensing")
    print(f"  {NX}x{NY}  L={LX}  dx={LX/NX:.4f}  dt={DT}")
    print(f"  1/r lens: V₀={V0}  ε={EPS}  k₀={K0}  w={W_PKT}")
    print(f"  ω={OMEGA:.4f}  v_group={C0**2 * K0 / OMEGA:.4f}")
    print(f"  b: {B_VALUES}")
    print("=" * 70)

    dev = setup_device()
    xx, yy, dx, dy, neg_k2 = build_grid(dev)
    c2     = build_c2(xx, yy)
    sponge = build_sponge(xx, yy)
    print(f"  CFL: dt·c_max/dx = {DT * C0 / (LX / NX):.3f}")

    os.makedirs(OUT_DIR, exist_ok=True)

    bs_done, dthetas = [], []
    traj_data = {}

    for b in B_VALUES:
        t, cx, cy = run_single(b, xx, yy, c2, sponge, neg_k2)
        dth, th_in, th_out = compute_deflection(t, cx, cy)
        bs_done.append(b)
        dthetas.append(dth)
        traj_data[b] = (t, cx, cy)
        s = "NaN" if np.isnan(dth) else f"{dth:+.6f}"
        print(f"    Δθ={s}")

    bs_a = np.asarray(bs_done)
    dt_a = np.asarray(dthetas)
    ok = ~np.isnan(dt_a) & (np.abs(dt_a) > 1e-12)

    if ok.sum() >= 2:
        log_b  = np.log(bs_a[ok])
        log_dt = np.log(np.abs(dt_a[ok]))
        coeffs = np.polyfit(log_b, log_dt, 1)
        fit_alpha = coeffs[0]
        fit_A     = np.exp(coeffs[1])
        r2_val    = R2(log_dt, coeffs[0] * log_b + coeffs[1])
        r2_lin    = R2(np.abs(dt_a[ok]), fit_A * bs_a[ok]**fit_alpha)
    else:
        fit_alpha = fit_A = r2_val = r2_lin = np.nan

    # ── Table ──────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print(f"{'b':>6} {'Δθ':>12} {'|Δθ|':>12} {'1/b':>10}")
    print("-" * 55)
    for b, dth in zip(bs_done, dthetas):
        print(f"{b:6.1f} {dth:+12.6f} {abs(dth):12.6f} {1/b:10.6f}")
    print("-" * 55)
    print(f"Fit: |Δθ| = {fit_A:.6f}·b^({fit_alpha:.4f})")
    print(f"R²(log) = {r2_val:.6f}   R²(lin) = {r2_lin:.6f}")

    plot_path = make_plots(bs_done, dthetas, fit_A, fit_alpha, r2_val, traj_data)

    tol = 0.15
    if not np.isnan(fit_alpha) and abs(fit_alpha - (-1.0)) < tol and r2_val > 0.98:
        conclusion = (f"CONFIRMED — deflection scales as 1/b: exponent {fit_alpha:.3f} "
                      f"(target -1.0±{tol}), R²={r2_val:.4f}")
    else:
        conclusion = (f"FALSIFIED — exponent = {fit_alpha:.3f}, R²={r2_val:.4f} "
                      f"(needed α=-1.0±{tol}, R²>0.98)")
    print(f"\n{conclusion}")

    with open(os.path.join(OUT_DIR, 'results_v2.json'), 'w') as f:
        json.dump(dict(
            b_values=bs_done, deflections=dthetas,
            fit_alpha=fit_alpha, fit_A=fit_A,
            R2_loglog=r2_val, R2_linear=r2_lin,
            V0=V0, epsilon=EPS,
            conclusion=conclusion, plot=plot_path
        ), f, indent=2, default=float)
    print(f"JSON: {OUT_DIR}/results_v2.json")


if __name__ == '__main__':
    main()
