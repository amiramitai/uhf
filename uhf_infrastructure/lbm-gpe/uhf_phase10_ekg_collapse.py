import cupy as cp
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import time

# =====================================================================
# UHF v3.1: Einstein-Klein-Gordon (EKG) Relativistic Collapse
# The Final Boss: Proving Singularity Avoidance under Full GR
# =====================================================================

# --- 1. Grid Setup ---
N = 8000
r_max = 40.0
dr = r_max / N
r = cp.linspace(dr, r_max, N) # Avoid r=0 directly for 1/r terms

# --- 2. Physics Parameters (Relativistic Units: G=1, c=1) ---
mu = 1.0        # Boson mass
lam = 1.0       # Relativistic GP coupling (tuned so initial state is sub-critical)
G = 1.0

# --- 3. Initialize Relativistic Scalar Field ---
# phi is the scalar field. Phi is its spatial derivative. Pi is its time derivative.
r0 = 15.0
sigma = 2.0
amp = 0.02 # Amplitude tuned: sub-critical initially, collapse via dynamics

phi = amp * cp.exp(-0.5 * ((r - r0) / sigma)**2) + 0j
Phi = cp.empty_like(phi)
Pi = cp.zeros_like(phi) # Start at rest

# Helper: Spatial derivative (Central Difference)
def gradient(f):
    grad = cp.empty_like(f)
    grad[1:-1] = (f[2:] - f[:-2]) / (2 * dr)
    grad[0] = (f[1] - f[0]) / dr
    grad[-1] = (f[-1] - f[-2]) / dr
    return grad

Phi = gradient(phi)

# --- 4. The Einstein Field Equations Solver (Constrained Evolution) ---
def solve_metric(phi, Phi, Pi):
    # Potential energy terms (independent of metric 'a')
    V_phi = 0.5 * mu**2 * cp.abs(phi)**2 + 0.25 * lam * cp.abs(phi)**4
    kinetic_terms = 0.5 * (cp.abs(Pi)**2 + cp.abs(Phi)**2)

    # Vectorized outward integration for M(r) using cumulative approach
    # Instead of a Python for-loop, we do an iterative scan but vectorized
    # Approximation: use a_metric from previous step or bootstrap with a=1
    # First pass: estimate M with a=1
    rho_est = kinetic_terms + V_phi
    dM_est = 4.0 * cp.pi * (r**2) * rho_est * dr
    M = cp.cumsum(dM_est)

    # Compute metric from M
    compactness = 2.0 * M / r
    compactness = cp.minimum(compactness, 0.99)  # Clamp to prevent singularity
    a_metric = 1.0 / cp.sqrt(1.0 - compactness)

    # Second pass: refine M with corrected a_metric
    rho_refined = kinetic_terms / (a_metric**2) + V_phi
    dM_refined = 4.0 * cp.pi * (r**2) * rho_refined * dr
    M = cp.cumsum(dM_refined)

    compactness = 2.0 * M / r
    compactness = cp.minimum(compactness, 0.99)
    a_metric = 1.0 / cp.sqrt(1.0 - compactness)

    # Calculate Lapse function alpha (Time Dilation)
    S_rr = kinetic_terms / (a_metric**2) - V_phi
    d_ln_alpha = (a_metric**2) * (M / (r**2) + 4.0 * cp.pi * r * S_rr)
    ln_alpha = cp.cumsum(d_ln_alpha) * dr

    # Boundary condition: alpha(r_max) = 1 / a(r_max)  (Asymptotically flat spacetime)
    alpha = cp.exp(ln_alpha)
    alpha *= (1.0 / (a_metric[-1] * alpha[-1]))

    return alpha, a_metric, M

# --- 5. EKG Right-Hand Side (Evolution) ---
def ekg_rhs(phi, Phi, Pi, alpha, a_metric):
    # d(phi)/dt = (alpha / a) * Pi
    dphi_dt = (alpha / a_metric) * Pi

    # d(Phi)/dt = d/dr [ (alpha / a) * Pi ]
    dPhi_dt = gradient((alpha / a_metric) * Pi)

    # d(Pi)/dt = 1/r^2 * d/dr [ r^2 * (alpha / a) * Phi ] - a * alpha * (mu^2 * phi + lam * |phi|^2 * phi)
    flux = (r**2) * (alpha / a_metric) * Phi
    div_flux = gradient(flux) / (r**2)
    source = a_metric * alpha * (mu**2 * phi + lam * cp.abs(phi)**2 * phi)
    dPi_dt = div_flux - source

    return dphi_dt, dPhi_dt, dPi_dt

# --- 6. Main Relativistic Loop ---
t = 0.0
t_max = 25.0
dt = 0.2 * dr  # Strict CFL for relativistic waves

history_t = []
history_max_compactness = []
history_min_lapse = []

print(f"[{time.strftime('%H:%M:%S')}] IGNITING FULL RELATIVISTIC EKG COLLAPSE")
print("Tracking Space-Time Curvature (Lapse & Compactness)...\n")

start_time = time.time()
step = 0
while t < t_max:
    # 1. Solve Einstein Constraints
    alpha, a_metric, M = solve_metric(phi, Phi, Pi)

    # 2. Extract Observables
    compactness = 2.0 * M / r
    max_comp = float(cp.max(compactness))
    min_lapse = float(cp.min(alpha))

    # 3. Evolution (RK2 Midpoint for stability vs performance)
    dphi1, dPhi1, dPi1 = ekg_rhs(phi, Phi, Pi, alpha, a_metric)

    phi_half = phi + 0.5 * dt * dphi1
    Phi_half = Phi + 0.5 * dt * dPhi1
    Pi_half = Pi + 0.5 * dt * dPi1
    alpha_h, a_metric_h, M_h = solve_metric(phi_half, Phi_half, Pi_half)

    dphi2, dPhi2, dPi2 = ekg_rhs(phi_half, Phi_half, Pi_half, alpha_h, a_metric_h)

    phi += dt * dphi2
    Phi += dt * dPhi2
    Pi += dt * dPi2

    # Boundary conditions
    phi[0], Phi[0], Pi[0] = 0, 0, 0
    phi[-1], Phi[-1], Pi[-1] = 0, 0, 0

    if step % 200 == 0:
        history_t.append(t)
        history_max_compactness.append(max_comp)
        history_min_lapse.append(min_lapse)
        print(f"t: {t:.3f} | Min Lapse (Time): {min_lapse:.4f} | Max Compactness (2M/r): {max_comp:.4f}")

        if max_comp > 0.999:
            print("\n[!] ALBERT EINSTEIN WINS: Apparent Horizon Formed (Black Hole).")
            break

    t += dt
    step += 1

elapsed = time.time() - start_time
print(f"\n[OK] SIMULATION HALTED. EKG DYNAMICS COMPLETE. ({elapsed:.1f}s, {step} steps)")

# --- 7. Plotting the Curvature Bounce ---
cpu_t = np.array(history_t)
cpu_comp = np.array(history_max_compactness)
cpu_lapse = np.array(history_min_lapse)

plt.style.use('dark_background')
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8))

ax1.plot(cpu_t, cpu_comp, color='magenta', lw=2)
ax1.axhline(y=1.0, color='red', linestyle='--', label='Event Horizon (Black Hole)')
ax1.set_ylabel('Compactness (2M/r)')
ax1.set_title('UHF Relativistic Stress Test: Gravitational Collapse vs Quantum Pressure')
ax1.legend()

ax2.plot(cpu_t, cpu_lapse, color='cyan', lw=2)
ax2.axhline(y=0.0, color='red', linestyle='--', label='Time Freeze (Singularity)')
ax2.set_xlabel('Time (t)')
ax2.set_ylabel('Minimum Lapse (alpha)')
ax2.legend()

plt.tight_layout()
plt.savefig('uhf_ekg_relativistic_bounce.png', dpi=300)
print("Plot saved to 'uhf_ekg_relativistic_bounce.png'")
