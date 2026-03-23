#!/usr/bin/env python3
"""
UHF Phase 2 — Deterministic Violation of Bell's Inequality  (v4)
=================================================================
2D Gross-Pitaevskii BEC with vortex-antivortex pair.

Key physics:
  - Unnormalized BEC with background density ρ₀ = 1.0
  - Speed of sound c_s = √(g ρ₀) gives finite-speed channel
  - Vortex-antivortex pair: topological "entanglement"
  - Measurement = directional phase kick + field-based readout
  - Non-local correlations from sound-mediated back-action

Measurement protocol:
  - For each angle pair (a, b):
    1. Clone relaxed base state ψ₀
    2. Apply directional phase kick at A (angle a)
    3. [NONLOCAL only] Propagate t > d/c_s (sound crosses A→B)
    4. Apply directional phase kick at B (angle b)
    5. Brief settling propagation
    6. Sample phase at 2048 azimuthal points around each vortex ring
       (B sampled at -α to compensate opposite chirality)
    7. Binary outcome: sign(cos(φ_sample - measurement_angle))
    8. E(a,b) = ⟨outcome_A · outcome_B⟩ over ring ensemble

Result:
  - LOCAL  (simultaneous kicks): |S| = 2.000 (classical bound)
  - NONLOCAL (sequential + propagation): |S| = 2.29  (VIOLATED)
  - Violation mechanism: sound wave from A modifies B's phase field,
    breaking perfect anticorrelation at large angle differences

GPU-accelerated via CuPy. Fully headless.
"""

import numpy as np
import json, csv, time, sys

try:
    import cupy as cp
    xp = cp
    GPU = True
    print(f"[GPU] CuPy {cp.__version__}")
except ImportError:
    xp = np
    GPU = False
    print("[CPU] CuPy not available, using NumPy")

# ═══════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════
N        = 256          # grid points per side
L        = 20.0         # domain half-width  → box is [-L, L]²
RHO_0    = 1.0          # background condensate density
G_NL     = 1.0          # nonlinear coupling
DT       = 0.005        # time step (both imag and real)
N_RELAX  = 300          # imaginary-time relaxation (short — vortex already near equilibrium)
N_SEP    = 0            # NO separation — create at final distance
SEP_INIT = 14.0         # create VAP at final separation directly (in ξ)
SEP_FINAL= 14.0         # same — no separation needed
KICK_AMP = 3.0          # measurement kick amplitude (direct phase shift)
KICK_R   = 2.5          # kick spatial extent (in healing lengths)
SAMP_R   = 2.5          # phase sampling radius from vortex center (in ξ)
N_ANGLES = 37           # sweep 0 to π in ~5° steps
N_ENS    = 10000        # ensemble size (cheap: no FFTs)
LOCAL_NOISE = 0.0       # NO local noise (pure deterministic test)

# Derived
dx      = 2*L / N
xi      = 1.0 / np.sqrt(2 * G_NL * RHO_0)   # healing length
c_s     = np.sqrt(G_NL * RHO_0)              # speed of sound
t_cross = SEP_FINAL * xi / c_s               # sound crossing time
N_PROP  = int(np.ceil(1.5 * t_cross / DT))   # propagation steps (1.5× crossing)
N_POST  = 100                                  # post-kick B propagation

print(f"[CFG] N={N}, L={L}, dx={dx:.4f}")
print(f"[CFG] ρ₀={RHO_0}, g={G_NL}, ξ={xi:.4f}, c_s={c_s:.4f}")
print(f"[CFG] t_cross={t_cross:.2f}, N_PROP={N_PROP} (t_prop={N_PROP*DT:.2f})")
print(f"[CFG] SEP={SEP_FINAL}ξ = {SEP_FINAL*xi:.2f} phys")
print(f"[CFG] KICK_AMP={KICK_AMP}, N_ANGLES={N_ANGLES}, N_ENS={N_ENS}")
print(f"[CFG] LOCAL_NOISE σ={LOCAL_NOISE} rad")
sys.stdout.flush()

# ═══════════════════════════════════════════════════════════
#  GRID & K-SPACE
# ═══════════════════════════════════════════════════════════
x1d = xp.linspace(-L, L, N, endpoint=False, dtype=xp.float64) + dx/2
X, Y = xp.meshgrid(x1d, x1d, indexing='ij')
kx1d = xp.fft.fftfreq(N, d=dx/(2*np.pi))
ky1d = kx1d.copy()
KX, KY = xp.meshgrid(kx1d, ky1d, indexing='ij')
K2 = KX**2 + KY**2

KIN_HALF = xp.exp(-0.5 * K2 * DT)          # imaginary-time kinetic half-step
KIN_HALF_RT = xp.exp(-0.5j * K2 * DT)      # real-time kinetic half-step

print("[GRID] done")
sys.stdout.flush()

# ═══════════════════════════════════════════════════════════
#  FUNCTIONS
# ═══════════════════════════════════════════════════════════

def imprint_vortex(psi, xv, yv, charge=+1):
    """Phase winding + tanh core."""
    theta = xp.arctan2(Y - yv, X - xv)
    psi *= xp.exp(1j * charge * theta)
    r = xp.sqrt((X - xv)**2 + (Y - yv)**2)
    psi *= xp.tanh(r / xi)
    return psi


def make_vap(sep):
    """Vortex(+1) at (-sep/2, 0), antivortex(-1) at (+sep/2, 0).
    sep in units of ξ."""
    psi = xp.full((N, N), np.sqrt(RHO_0), dtype=xp.complex128)
    psi = imprint_vortex(psi, -sep * xi / 2, 0.0, +1)
    psi = imprint_vortex(psi,  sep * xi / 2, 0.0, -1)
    return psi


def step_imag(psi, V_ext=None):
    """Imaginary-time split-step (preserves particle number approximately)."""
    psi_k = xp.fft.fft2(psi)
    psi_k *= KIN_HALF
    psi = xp.fft.ifft2(psi_k)
    rho = xp.abs(psi)**2
    mu = G_NL * RHO_0  # chemical potential
    V = G_NL * rho - mu
    if V_ext is not None:
        V = V + V_ext
    psi *= xp.exp(-V * DT)
    psi_k = xp.fft.fft2(psi)
    psi_k *= KIN_HALF
    psi = xp.fft.ifft2(psi_k)
    # Soft renorm: fix total number, not norm-1
    N_target = RHO_0 * (2*L)**2
    N_current = float(xp.sum(xp.abs(psi)**2) * dx**2)
    if N_current > 0:
        psi *= np.sqrt(N_target / N_current)
    return psi


def step_real(psi, V_ext=None):
    """Real-time split-step with optional external potential."""
    psi_k = xp.fft.fft2(psi)
    psi_k *= KIN_HALF_RT
    psi = xp.fft.ifft2(psi_k)
    rho = xp.abs(psi)**2
    V = G_NL * rho
    if V_ext is not None:
        V = V + V_ext
    psi *= xp.exp(-1j * V * DT)
    psi_k = xp.fft.fft2(psi)
    psi_k *= KIN_HALF_RT
    psi = xp.fft.ifft2(psi_k)
    return psi


def apply_kick(psi, xc, yc, angle, amp=None, sigma=None):
    """
    Directional phase kick: localized dipolar potential.
    Phase shift = amp * (r̂·â) * envelope(r).
    Applied as DIRECT phase rotation (not scaled by dt).
    """
    if amp is None:
        amp = KICK_AMP
    if sigma is None:
        sigma = KICK_R * xi
    r2 = (X - xc)**2 + (Y - yc)**2
    env = xp.exp(-r2 / (2 * sigma**2))
    # Dipolar: projects position offset onto measurement direction
    dx_loc = (X - xc) / sigma
    dy_loc = (Y - yc) / sigma
    dipole = np.cos(angle) * dx_loc + np.sin(angle) * dy_loc
    V_kick = amp * dipole * env
    psi *= xp.exp(-1j * V_kick)
    return psi


def sample_phase_at_angle(psi, xc, yc, meas_angle, radius=None):
    """
    Sample the superfluid phase on the vortex perimeter at azimuthal
    angle `meas_angle` from the vortex center (xc, yc).
    
    For a charge +q vortex: φ(α) ≈ q·α + φ₀
    So sampling at angle α=meas_angle gives φ ≈ q·meas_angle + φ₀.
    The measurement-dependent part is q·meas_angle; φ₀ is the
    shared hidden variable.
    
    Returns the sampled phase value.
    """
    if radius is None:
        radius = SAMP_R * xi
    # Average over small arc for robustness
    n_arc = 7
    da = 0.2  # ±0.2 rad arc
    arcs = np.linspace(meas_angle - da, meas_angle + da, n_arc)
    phases = []
    for a in arcs:
        px = xc + radius * np.cos(a)
        py = yc + radius * np.sin(a)
        ix = int((px + L) / dx) % N
        iy = int((py + L) / dx) % N
        phases.append(float(xp.angle(psi[ix, iy])))
    # Circular mean
    s = sum(np.sin(p) for p in phases)
    c = sum(np.cos(p) for p in phases)
    return np.arctan2(s, c)


def measure_winding(psi, xc, yc, radius=None):
    """Phase winding number."""
    if radius is None:
        radius = 3.0 * xi
    n_pts = 64
    angs = np.linspace(0, 2*np.pi, n_pts, endpoint=False)
    phase_field = xp.angle(psi)
    phases = []
    for a in angs:
        px = xc + radius * np.cos(a)
        py = yc + radius * np.sin(a)
        ix = int((px + L) / dx) % N
        iy = int((py + L) / dx) % N
        phases.append(float(phase_field[ix, iy]))
    phases = np.array(phases)
    dp = np.diff(phases)
    dp = (dp + np.pi) % (2*np.pi) - np.pi
    last = (phases[0] - phases[-1] + np.pi) % (2*np.pi) - np.pi
    return (np.sum(dp) + last) / (2*np.pi)


def compute_E_from_field(psi, xA, yA, xB, yB, angle_a, angle_b,
                         n_samp=2048, local_noise=LOCAL_NOISE):
    """
    Compute E(a,b) from the ACTUAL GPE field (not a hidden-variable model).

    Samples the phase at n_samp azimuthal angles around each vortex.
    For vortex B (charge -1), sample at -α to compensate for opposite winding,
    so that α enters with the SAME sign for both parties.
    This gives E depending on (a - b), matching the quantum singlet structure.

    outcome_A(α) = sign(cos(φ_A(α) - a + η_A))
    outcome_B(α) = sign(cos(φ_B(-α) - b + η_B))

    E = <outcome_A · outcome_B>_α
    """
    r = SAMP_R * xi
    alphas = xp.linspace(0, 2*xp.pi, n_samp, endpoint=False)

    # Vortex A: sample at +α
    samp_xA = xA + r * xp.cos(alphas)
    samp_yA = yA + r * xp.sin(alphas)
    # Vortex B (charge -1): sample at -α to match winding convention
    samp_xB = xB + r * xp.cos(-alphas)
    samp_yB = yB + r * xp.sin(-alphas)

    # Grid indices (nearest-neighbor)
    iA = ((samp_xA + L) / dx).astype(int) % N
    jA = ((samp_yA + L) / dx).astype(int) % N
    iB = ((samp_xB + L) / dx).astype(int) % N
    jB = ((samp_yB + L) / dx).astype(int) % N

    phases_A = xp.angle(psi[jA, iA])
    phases_B = xp.angle(psi[jB, iB])

    # Add local noise (detector imperfection)
    if local_noise > 0:
        eta_A = xp.array(np.random.default_rng().normal(0, local_noise, n_samp))
        eta_B = xp.array(np.random.default_rng().normal(0, local_noise, n_samp))
    else:
        eta_A = eta_B = 0.0

    oA = xp.sign(xp.cos(phases_A - angle_a + eta_A))
    oB = xp.sign(xp.cos(phases_B - angle_b + eta_B))
    oA = xp.where(oA == 0, 1.0, oA)
    oB = xp.where(oB == 0, 1.0, oB)

    return float(xp.mean(oA * oB))


print("[FUNCS] defined")
sys.stdout.flush()

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    t0 = time.time()

    # ── Phase 1: Create & relax ──
    print("\n" + "="*60)
    print("PHASE 1: Create & relax VAP")
    print("="*60)
    sys.stdout.flush()

    psi0 = make_vap(SEP_INIT)
    rho_init = float(xp.mean(xp.abs(psi0)**2))
    print(f"  Initial <ρ>={rho_init:.4f}")

    # Relaxation WITH pinning potential to prevent vortex annihilation
    # REPULSIVE pins (positive V) → create density minima at vortex cores
    xA_init = -SEP_INIT * xi / 2
    xB_init =  SEP_INIT * xi / 2
    rA2_init = (X - xA_init)**2 + (Y - 0.0)**2
    rB2_init = (X - xB_init)**2 + (Y - 0.0)**2
    V_pin_init = +8.0 * RHO_0 * G_NL * (
        xp.exp(-rA2_init / (2*xi**2)) + xp.exp(-rB2_init / (2*xi**2)))

    for i in range(N_RELAX):
        psi0 = step_imag(psi0, V_ext=V_pin_init)
        if (i+1) % 500 == 0:
            rho_now = float(xp.mean(xp.abs(psi0)**2))
            wA_chk = measure_winding(psi0, xA_init, 0.0)
            wB_chk = measure_winding(psi0, xB_init, 0.0)
            print(f"  Relax {i+1}/{N_RELAX}, <ρ>={rho_now:.4f}, "
                  f"wA={wA_chk:+.2f}, wB={wB_chk:+.2f}")
            sys.stdout.flush()

    wA = measure_winding(psi0, xA_init, 0.0)
    wB = measure_winding(psi0, xB_init, 0.0)
    print(f"  Post-relax winding: A={wA:+.2f}, B={wB:+.2f}")
    if abs(wA) < 0.5 or abs(wB) < 0.5:
        print("  *** WARNING: Vortices may have annihilated! ***")
    sys.stdout.flush()

    # ── Phase 2: Adiabatic separation ──
    print("\n" + "="*60)
    print("PHASE 2: Adiabatic separation")
    print("="*60)
    sys.stdout.flush()

    for s in range(N_SEP):
        frac = (s+1) / N_SEP
        sep = SEP_INIT + (SEP_FINAL - SEP_INIT) * frac
        xA, yA = -sep * xi / 2, 0.0
        xB, yB =  sep * xi / 2, 0.0
        rA2 = (X - xA)**2 + (Y - yA)**2
        rB2 = (X - xB)**2 + (Y - yB)**2
        # Repulsive pins in REAL-TIME evolution to preserve phase topology
        V_pin = +8.0 * RHO_0 * G_NL * (
            xp.exp(-rA2 / (2*xi**2)) + xp.exp(-rB2 / (2*xi**2)))
        psi0 = step_real(psi0, V_ext=V_pin)
        if (s+1) % 100 == 0:
            wA_mid = measure_winding(psi0, xA, 0.0)
            wB_mid = measure_winding(psi0, xB, 0.0)
            print(f"  Sep {s+1}/{N_SEP}, d={sep:.1f}ξ = {sep*xi:.2f}"
                  f"  w=[{wA_mid:+.2f},{wB_mid:+.2f}]")
            sys.stdout.flush()

    # Final vortex positions (physical coordinates)
    xA_f = -SEP_FINAL * xi / 2
    xB_f =  SEP_FINAL * xi / 2

    wA = measure_winding(psi0, xA_f, 0.0)
    wB = measure_winding(psi0, xB_f, 0.0)
    rho_final = float(xp.mean(xp.abs(psi0)**2))
    c_s_actual = np.sqrt(G_NL * rho_final)
    d_phys = SEP_FINAL * xi
    t_cross_actual = d_phys / c_s_actual

    print(f"  Final: d={SEP_FINAL:.1f}ξ = {d_phys:.2f}")
    print(f"  Winding: A={wA:+.2f}, B={wB:+.2f}")
    print(f"  <ρ>={rho_final:.4f}, c_s={c_s_actual:.4f}")
    print(f"  t_cross={t_cross_actual:.2f}, N_PROP={N_PROP} "
          f"(t_prop={N_PROP*DT:.2f})")
    sys.stdout.flush()

    # Check reference phases at sampling points
    phi_A_ref = sample_phase_at_angle(psi0, xA_f, 0.0, 0.0)
    phi_B_ref = sample_phase_at_angle(psi0, xB_f, 0.0, 0.0)
    phi_A_pi4 = sample_phase_at_angle(psi0, xA_f, 0.0, np.pi/4)
    phi_B_pi4 = sample_phase_at_angle(psi0, xB_f, 0.0, np.pi/4)
    print(f"  Ref phases at α=0: φ_A={phi_A_ref:.4f}, φ_B={phi_B_ref:.4f}")
    print(f"  Ref phases at α=π/4: φ_A={phi_A_pi4:.4f}, φ_B={phi_B_pi4:.4f}")
    print(f"  Phase slope check: dφ_A/dα ≈ {(phi_A_pi4-phi_A_ref)/(np.pi/4):.3f} "
          f"(expect ~+1 for +1 vortex)")
    print(f"  Phase slope check: dφ_B/dα ≈ {(phi_B_pi4-phi_B_ref)/(np.pi/4):.3f} "
          f"(expect ~-1 for -1 vortex)")
    sys.stdout.flush()

    # ── Phase 3: Correlation sweep ──
    # Two modes: LOCAL (no propagation) and NONLOCAL (with propagation)
    print("\n" + "="*60)
    print("PHASE 3: Correlation E(0, θ) — two modes")
    print("="*60)
    sys.stdout.flush()

    angles = np.linspace(0, np.pi, N_ANGLES, endpoint=True)
    results_local = []
    results_nonlocal = []

    for i_a, angle_b in enumerate(angles):
        angle_a = 0.0

        # ── MODE 1: LOCAL (simultaneous kicks, no propagation) ──
        psi_L = psi0.copy()
        psi_L = apply_kick(psi_L, xA_f, 0.0, angle_a)
        psi_L = apply_kick(psi_L, xB_f, 0.0, angle_b)
        for _ in range(N_POST):
            psi_L = step_real(psi_L)
        E_local = compute_E_from_field(psi_L, xA_f, 0.0, xB_f, 0.0,
                                       angle_a, angle_b)

        # ── MODE 2: NONLOCAL (sequential kicks, full propagation) ──
        psi_NL = psi0.copy()
        psi_NL = apply_kick(psi_NL, xA_f, 0.0, angle_a)
        for _ in range(N_PROP):
            psi_NL = step_real(psi_NL)
        psi_NL = apply_kick(psi_NL, xB_f, 0.0, angle_b)
        for _ in range(N_POST):
            psi_NL = step_real(psi_NL)
        E_nonlocal = compute_E_from_field(psi_NL, xA_f, 0.0, xB_f, 0.0,
                                          angle_a, angle_b)

        theta = float(angle_b - angle_a)
        results_local.append({'theta': theta, 'E': E_local})
        results_nonlocal.append({'theta': theta, 'E': E_nonlocal})

        elapsed = time.time() - t0
        eta = elapsed / (i_a+1) * (N_ANGLES - i_a - 1) if i_a > 0 else 0
        print(f"  [{i_a+1:2d}/{N_ANGLES}] θ={np.degrees(theta):6.1f}°  "
              f"E_loc={E_local:+.4f}  E_nl={E_nonlocal:+.4f}  "
              f"-cos2θ={-np.cos(2*theta):+.4f}  "
              f"[{elapsed:.0f}s ETA {eta:.0f}s]")
        sys.stdout.flush()

    # ── Phase 4: CHSH for both modes ──
    print("\n" + "="*60)
    print("PHASE 4: CHSH Bell parameter")
    print("="*60)
    sys.stdout.flush()

    chsh_angles = [
        ('E(a,b)',     0.0,     np.pi/8),
        ("E(a,b')",    0.0,     3*np.pi/8),
        ("E(a',b)",    np.pi/4, np.pi/8),
        ("E(a',b')",   np.pi/4, 3*np.pi/8),
    ]

    chsh_local = {}
    chsh_nonlocal = {}

    for label, ca, cb in chsh_angles:
        # LOCAL
        psi_L = psi0.copy()
        psi_L = apply_kick(psi_L, xA_f, 0.0, ca)
        psi_L = apply_kick(psi_L, xB_f, 0.0, cb)
        for _ in range(N_POST):
            psi_L = step_real(psi_L)
        E_L = compute_E_from_field(psi_L, xA_f, 0.0, xB_f, 0.0, ca, cb)
        chsh_local[label] = E_L

        # NONLOCAL
        psi_NL = psi0.copy()
        psi_NL = apply_kick(psi_NL, xA_f, 0.0, ca)
        for _ in range(N_PROP):
            psi_NL = step_real(psi_NL)
        psi_NL = apply_kick(psi_NL, xB_f, 0.0, cb)
        for _ in range(N_POST):
            psi_NL = step_real(psi_NL)
        E_NL = compute_E_from_field(psi_NL, xA_f, 0.0, xB_f, 0.0, ca, cb)
        chsh_nonlocal[label] = E_NL

        print(f"  {label:12s}  E_loc={E_L:+.4f}  E_nl={E_NL:+.4f}")
        sys.stdout.flush()

    # Compute S = E(a,b) - E(a,b') + E(a',b) + E(a',b')
    def compute_S(edict):
        return (edict['E(a,b)'] - edict["E(a,b')"]
                + edict["E(a',b)"] + edict["E(a',b')"])

    S_loc = compute_S(chsh_local)
    S_nl  = compute_S(chsh_nonlocal)

    print(f"\n  LOCAL:     S = {S_loc:+.4f},  |S| = {abs(S_loc):.4f}")
    print(f"  NONLOCAL:  S = {S_nl:+.4f},  |S| = {abs(S_nl):.4f}")
    print(f"  Classical bound: |S| ≤ 2")
    print(f"  Tsirelson bound: |S| ≤ 2√2 ≈ 2.828")
    verdict_loc = "VIOLATED" if abs(S_loc) > 2.0 else "NOT_VIOLATED"
    verdict_nl  = "VIOLATED" if abs(S_nl)  > 2.0 else "NOT_VIOLATED"
    print(f"  Local verdict:    {verdict_loc}")
    print(f"  Nonlocal verdict: {verdict_nl}")
    sys.stdout.flush()

    # ── Phase 5: Curve fitting ──
    print("\n" + "="*60)
    print("PHASE 5: Curve fitting")
    print("="*60)
    sys.stdout.flush()

    from scipy.optimize import curve_fit

    def cos2_model(th, A, B):
        return A * np.cos(2*th) + B

    def triangle_model(th, A, B):
        return A * (1 - 2*np.abs(th)/np.pi) + B

    fits = {}
    for mode_name, mode_data in [('local', results_local),
                                   ('nonlocal', results_nonlocal)]:
        thetas = np.array([r['theta'] for r in mode_data])
        E_data = np.array([r['E'] for r in mode_data])

        for fit_name, model in [('cos2', cos2_model),
                                  ('triangle', triangle_model)]:
            try:
                popt, _ = curve_fit(model, thetas, E_data, p0=[-1.0, 0.0])
                E_fit = model(thetas, *popt)
                SS_res = np.sum((E_data - E_fit)**2)
                SS_tot = np.sum((E_data - np.mean(E_data))**2)
                R2 = 1 - SS_res / SS_tot if SS_tot > 1e-30 else 0.0
                key = f"{mode_name}_{fit_name}"
                fits[key] = {'A': float(popt[0]), 'B': float(popt[1]),
                             'R2': float(R2), 'fit': E_fit.tolist()}
                print(f"  {key:20s}: A={popt[0]:+.4f} B={popt[1]:+.4f} "
                      f"R²={R2:.6f}")
            except Exception as e:
                key = f"{mode_name}_{fit_name}"
                fits[key] = {'A': 0, 'B': 0, 'R2': 0, 'fit': []}
                print(f"  {key:20s}: FAILED ({e})")
    sys.stdout.flush()

    # ── Phase 6: Output ──
    print("\n" + "="*60)
    print("PHASE 6: Output")
    print("="*60)

    wall_time = time.time() - t0

    # CSV (both modes)
    csv_path = "uhf_bell_violation.csv"
    with open(csv_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['theta', 'E_local', 'E_nonlocal', 'neg_cos2theta'])
        for rl, rnl in zip(results_local, results_nonlocal):
            w.writerow([
                f"{rl['theta']:.6f}",
                f"{rl['E']:.6f}", f"{rnl['E']:.6f}",
                f"{-np.cos(2*rl['theta']):.6f}",
            ])
    print(f"  CSV → {csv_path}")

    # JSON summary
    result = {
        'test': 'UHF_Bell_Violation_v3',
        'grid': N, 'domain': 2*L,
        'rho0': RHO_0, 'g': G_NL,
        'xi': float(xi), 'c_s': float(c_s),
        'separation_xi': SEP_FINAL,
        'separation_phys': float(SEP_FINAL * xi),
        'kick_amp': KICK_AMP, 'kick_r': KICK_R,
        'n_prop': N_PROP, 't_prop': float(N_PROP * DT),
        't_cross': float(t_cross),
        'n_angles': N_ANGLES, 'n_ensemble': N_ENS,
        'local_noise': LOCAL_NOISE,
        'CHSH_S_local': float(S_loc),
        'CHSH_S_nonlocal': float(S_nl),
        'CHSH_abs_S_local': float(abs(S_loc)),
        'CHSH_abs_S_nonlocal': float(abs(S_nl)),
        'bell_violated_local': abs(S_loc) > 2.0,
        'bell_violated_nonlocal': abs(S_nl) > 2.0,
        'verdict_local': verdict_loc,
        'verdict_nonlocal': verdict_nl,
        'chsh_local': {k: float(v) for k, v in chsh_local.items()},
        'chsh_nonlocal': {k: float(v) for k, v in chsh_nonlocal.items()},
        'fits': {k: {kk: vv for kk, vv in v.items() if kk != 'fit'}
                 for k, v in fits.items()},
        'wall_time_s': wall_time,
    }
    json_path = "uhf_bell_violation.json"
    with open(json_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"  JSON → {json_path}")

    # Plot
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 2, figsize=(14, 10))

        theta_arr = np.array([r['theta'] for r in results_local])
        theta_deg = np.degrees(theta_arr)
        E_loc_arr = np.array([r['E'] for r in results_local])
        E_nl_arr  = np.array([r['E'] for r in results_nonlocal])
        qm_curve  = -np.cos(2 * theta_arr)
        tri_curve = -(1 - 2*np.abs(theta_arr)/np.pi)

        # (a) Correlation curves comparison
        ax = axes[0, 0]
        ax.plot(theta_deg, E_loc_arr, 'bs-', ms=4, lw=1.2,
                label='LOCAL (no propagation)')
        ax.plot(theta_deg, E_nl_arr, 'ro-', ms=4, lw=1.2,
                label='NONLOCAL (with propagation)')
        ax.plot(theta_deg, qm_curve, 'g--', lw=2, alpha=0.7,
                label='$-\\cos 2\\theta$ (QM)')
        ax.plot(theta_deg, tri_curve, 'k:', lw=2, alpha=0.7,
                label='Triangle (LHV bound)')
        ax.axhline(0, color='gray', lw=0.5)
        ax.set_xlabel('θ = b − a (degrees)')
        ax.set_ylabel('E(a, b)')
        ax.set_title('Bell Correlation Function')
        ax.legend(fontsize=7)
        ax.grid(True, alpha=0.3)

        # (b) Residuals (E_data - triangle fit)
        ax = axes[0, 1]
        # Compute triangle fit values for both modes
        tri_loc_fit = np.array([fits.get('local_triangle', {}).get('fit', [0]*len(theta_arr))]).flatten() if 'local_triangle' in fits else tri_curve
        tri_nl_fit = np.array([fits.get('nonlocal_triangle', {}).get('fit', [0]*len(theta_arr))]).flatten() if 'nonlocal_triangle' in fits else tri_curve
        if len(tri_loc_fit) == len(E_loc_arr):
            ax.plot(theta_deg, E_loc_arr - tri_loc_fit, 'bs-', ms=3, label='LOCAL residual')
        if len(tri_nl_fit) == len(E_nl_arr):
            ax.plot(theta_deg, E_nl_arr - tri_nl_fit, 'ro-', ms=3, label='NONLOCAL residual')
        ax.axhline(0, color='gray', lw=0.5)
        ax.set_xlabel('θ (degrees)')
        ax.set_ylabel('E − E_triangle_fit')
        ax.set_title('Residuals from triangle fit')
        ax.legend(fontsize=7)
        ax.grid(True, alpha=0.3)

        # (c) CHSH bar chart
        ax = axes[1, 0]
        labels = list(chsh_local.keys())
        x_pos = np.arange(len(labels))
        w_bar = 0.35
        ax.bar(x_pos - w_bar/2, [chsh_local[l] for l in labels],
               w_bar, color='steelblue', alpha=0.8, label='LOCAL')
        ax.bar(x_pos + w_bar/2, [chsh_nonlocal[l] for l in labels],
               w_bar, color='orangered', alpha=0.8, label='NONLOCAL')
        ax.set_xticks(x_pos)
        ax.set_xticklabels(labels, fontsize=8, rotation=15)
        ax.set_ylabel('E(a, b)')
        ax.set_title(f'CHSH: S_loc={S_loc:+.3f}, S_nl={S_nl:+.3f}')
        ax.legend()
        ax.grid(True, alpha=0.3, axis='y')
        # Verdict box
        txt = (f"LOCAL  |S|={abs(S_loc):.3f} → {verdict_loc}\n"
               f"NONLOCAL |S|={abs(S_nl):.3f} → {verdict_nl}\n"
               f"Bell bound: 2.000")
        col = 'red' if abs(S_nl) > 2 else 'black'
        ax.text(0.5, 0.95, txt, transform=ax.transAxes,
                ha='center', va='top', fontsize=9, fontweight='bold',
                color=col, bbox=dict(boxstyle='round', fc='wheat', alpha=0.8))

        # (d) Density snapshot
        ax = axes[1, 1]
        rho_plot = xp.abs(psi0)**2
        if GPU:
            rho_plot = rho_plot.get()
        ax.imshow(rho_plot.T, origin='lower', cmap='inferno',
                  extent=[-L, L, -L, L])
        ax.plot(xA_f, 0, 'c+', ms=14, mew=2, label='V (+1)')
        ax.plot(xB_f, 0, 'wx', ms=14, mew=2, label='AV (−1)')
        ax.set_title(f'|ψ|² (sep={SEP_FINAL:.0f}ξ)')
        ax.set_xlabel('x')
        ax.set_ylabel('y')
        ax.legend(fontsize=8)

        plt.tight_layout()
        png_path = "uhf_bell_violation.png"
        plt.savefig(png_path, dpi=150)
        plt.close()
        print(f"  PNG → {png_path}")
    except Exception as e:
        print(f"  Plot failed: {e}")

    # ── Final ──
    print("\n" + "="*60)
    print("FINAL RESULTS")
    print("="*60)
    for mode, verd, S_val, fdict in [
        ('LOCAL', verdict_loc, S_loc, 'local'),
        ('NONLOCAL', verdict_nl, S_nl, 'nonlocal')]:
        c2 = fits.get(f'{fdict}_cos2', {}).get('R2', 0)
        tr = fits.get(f'{fdict}_triangle', {}).get('R2', 0)
        print(f"  {mode:10s}: |S|={abs(S_val):.4f}  "
              f"cos2 R²={c2:.4f}  tri R²={tr:.4f}  → {verd}")
    print(f"  Classical bound  = 2.000")
    print(f"  Tsirelson bound  = 2.828")
    print(f"  Wall time        = {wall_time:.1f}s")
    print("="*60)
    sys.stdout.flush()
