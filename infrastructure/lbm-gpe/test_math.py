"""
UHF Phase 0 — Mathematical Foundation Validation  (Workblock 2)
================================================================
Test 1: Verify non-associativity of octonions.
Test 2: Generate a Trefoil knot T(2,3), render it, confirm closure
        and r/R ratio from uhf_config.

Usage:
    python test_math.py
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np

# ── UHF imports ──
from uhf_config import R_OVER_R_NOMINAL
from uhf_math_utils import OctonionMath, KnotGeometry, UHFMathUtils
from uhf_plotter import plot_knot, plot_multi_knots

# ─────────────────────────────────────────────────────────────────────

SEPARATOR = "─" * 62


def _section(title: str) -> None:
    print(f"\n{SEPARATOR}")
    print(f"  {title}")
    print(SEPARATOR)


def main() -> None:
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║   UHF Phase 0 — Math Foundation Validation (Workblock 2)   ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    O = OctonionMath
    K = KnotGeometry
    all_pass = True

    # ═══════════════════════════════════════════════════════════════
    #  TEST 1 — Octonionic Algebra
    # ═══════════════════════════════════════════════════════════════
    _section("1 · Octonion Basis Setup")
    e = [O.basis(i) for i in range(8)]
    print(f"  Basis octonions e₀ … e₇ created  (8-vectors, float64)")

    # ── 1a. Fano-plane products ──
    _section("1a · Fano-Plane Triple Verification")
    triples = [
        (1, 2, 3), (1, 4, 5), (1, 7, 6),
        (2, 4, 6), (2, 5, 7),
        (3, 4, 7), (3, 6, 5),
    ]
    fano_ok = True
    for (a, b, c) in triples:
        prod = O.mul(e[a], e[b])
        if not np.allclose(prod, e[c]):
            print(f"  FAIL  e{a}·e{b} = {prod}, expected e{c}")
            fano_ok = False
        # Reverse should give -e_c  (anti-commutativity)
        prod_rev = O.mul(e[b], e[a])
        if not np.allclose(prod_rev, -e[c]):
            print(f"  FAIL  e{b}·e{a} = {prod_rev}, expected -e{c}")
            fano_ok = False
    status = "PASS" if fano_ok else "FAIL"
    print(f"  All 7 Fano triples + anti-commutativity: {status}")
    all_pass &= fano_ok

    # ── 1b. e_i² = -1 ──
    _section("1b · Imaginary Squaring  (eᵢ² = −1)")
    sq_ok = True
    for i in range(1, 8):
        sq = O.mul(e[i], e[i])
        if not np.allclose(sq, -e[0]):
            print(f"  FAIL  e{i}² = {sq}")
            sq_ok = False
    status = "PASS" if sq_ok else "FAIL"
    print(f"  e₁² through e₇² all equal −e₀: {status}")
    all_pass &= sq_ok

    # ── 1c. NON-ASSOCIATIVITY ──
    _section("1c · Non-Associativity Verification")
    # The spec example: (e₁·e₂)·e₄  ≠  e₁·(e₂·e₄)
    lhs = O.mul(O.mul(e[1], e[2]), e[4])
    rhs = O.mul(e[1], O.mul(e[2], e[4]))
    non_assoc = not np.allclose(lhs, rhs)
    print(f"  (e₁·e₂)·e₄ = {lhs}")
    print(f"  e₁·(e₂·e₄) = {rhs}")
    print(f"  Are they different? {non_assoc}")
    status = "PASS" if non_assoc else "FAIL"
    print(f"  Non-associativity: {status}")
    all_pass &= non_assoc

    # Check multiple triples for non-associativity
    na_count = 0
    total = 0
    for i in range(1, 8):
        for j in range(1, 8):
            if j == i:
                continue
            for k in range(1, 8):
                if k == i or k == j:
                    continue
                lhs = O.mul(O.mul(e[i], e[j]), e[k])
                rhs = O.mul(e[i], O.mul(e[j], e[k]))
                total += 1
                if not np.allclose(lhs, rhs):
                    na_count += 1
    print(f"  Non-associative triples: {na_count} / {total} "
          f"({100*na_count/total:.0f}%)")

    # ── 1d. Norm, conjugation, inverse ──
    _section("1d · Norm, Conjugation, Inverse")
    p = O.octonion(1, 2, 3, 4, 5, 6, 7, 8)
    print(f"  p       = {p}")
    print(f"  |p|     = {O.norm(p):.6f}  (expected √204 ≈ {np.sqrt(204):.6f})")
    print(f"  p*      = {O.conj(p)}")

    pinv = O.inv(p)
    pp_inv = O.mul(p, pinv)
    inv_ok = abs(pp_inv[0] - 1.0) < 1e-12 and np.max(np.abs(pp_inv[1:])) < 1e-12
    status = "PASS" if inv_ok else "FAIL"
    print(f"  p·p⁻¹   = {pp_inv}")
    print(f"  Inverse identity: {status}")
    all_pass &= inv_ok

    # ── 1e. Addition ──
    _section("1e · Addition")
    a = O.octonion(1, 0, 0, 0, 0, 0, 0, 0)
    b = O.octonion(0, 1, 0, 0, 0, 0, 0, 0)
    s = O.add(a, b)
    add_ok = np.allclose(s, O.octonion(1, 1, 0, 0, 0, 0, 0, 0))
    status = "PASS" if add_ok else "FAIL"
    print(f"  e₀ + e₁ = {s}  → {status}")
    all_pass &= add_ok

    # ── 1f. Backward compatibility ──
    _section("1f · Backward Compatibility (UHFMathUtils alias)")
    compat_ok = UHFMathUtils is OctonionMath
    status = "PASS" if compat_ok else "FAIL"
    print(f"  UHFMathUtils is OctonionMath: {compat_ok}  → {status}")
    all_pass &= compat_ok

    # ═══════════════════════════════════════════════════════════════
    #  TEST 2 — Torus Knot Geometry
    # ═══════════════════════════════════════════════════════════════
    _section("2 · Trefoil Knot T(2,3) Generation")

    R = 1.0
    r_ratio = R_OVER_R_NOMINAL          # 0.22 from config
    r = r_ratio * R
    N_pts = 2048

    print(f"  R       = {R}")
    print(f"  r/R     = {r_ratio}  (from uhf_config)")
    print(f"  r       = {r}")
    print(f"  N pts   = {N_pts}")

    # -- CPU path --
    t0 = time.perf_counter()
    pts_cpu = K.torus_knot(2, 3, R=R, r=r, N=N_pts, use_gpu=False)
    dt_cpu = time.perf_counter() - t0
    print(f"  CPU generation: {pts_cpu.shape}, {dt_cpu*1e3:.2f} ms")

    # -- GPU path --
    try:
        import cupy as cp
        t0 = time.perf_counter()
        pts_gpu = K.torus_knot(2, 3, R=R, r=r, N=N_pts, use_gpu=True)
        cp.cuda.Device(0).synchronize()
        dt_gpu = time.perf_counter() - t0
        print(f"  GPU generation: {pts_gpu.shape}, {dt_gpu*1e3:.2f} ms")
        # Transfer back for checks
        pts_check = cp.asnumpy(pts_gpu)
        gpu_match = np.allclose(pts_cpu, pts_check, atol=1e-12)
        print(f"  CPU ↔ GPU match: {gpu_match}")
    except ImportError:
        print("  [SKIP] CuPy not available — GPU path not tested")
        pts_check = pts_cpu
        gpu_match = True

    # ── 2a. Closure check ──
    _section("2a · Knot Closure Check")
    # For endpoint=False with N points, the curve is periodic:
    # the gap between the last point and the first should be small.
    gap = np.linalg.norm(pts_cpu[-1] - pts_cpu[0])
    # Theoretical gap for N→∞ is 0; for finite N it's O(2π/N)
    max_gap = 2 * np.pi * max(R + r, r) / N_pts * 5  # generous bound
    closure_ok = gap < max_gap
    status = "PASS" if closure_ok else "FAIL"
    print(f"  Gap between last → first point: {gap:.6e}")
    print(f"  Threshold (5× step size):       {max_gap:.6e}")
    print(f"  Knot closes: {status}")
    all_pass &= closure_ok

    # ── 2b. r/R ratio from bounding box ──
    _section("2b · Bounding-Box r/R Verification")
    x_range = pts_cpu[:, 0].max() - pts_cpu[:, 0].min()
    z_range = pts_cpu[:, 2].max() - pts_cpu[:, 2].min()
    # For T(2,3): x spans roughly [-(R+r), (R+r)], z spans [-r, r]
    R_eff = x_range / 2.0       # ≈ R + r
    r_eff = z_range / 2.0       # ≈ r
    ratio_measured = r_eff / (R_eff - r_eff)  # r / R
    ratio_ok = abs(ratio_measured - r_ratio) / r_ratio < 0.05  # 5% tolerance
    status = "PASS" if ratio_ok else "FAIL"
    print(f"  x span  = {x_range:.4f}  → R_eff ≈ {R_eff:.4f}")
    print(f"  z span  = {z_range:.4f}  → r_eff ≈ {r_eff:.4f}")
    print(f"  r/R measured = {ratio_measured:.4f}  (config = {r_ratio})")
    print(f"  Ratio match: {status}")
    all_pass &= ratio_ok

    # ── 2c. Tangent vectors ──
    _section("2c · Tangent Vector Sanity")
    tangents = K.torus_knot_tangent(2, 3, R=R, r=r, N=N_pts, use_gpu=False)
    norms = np.linalg.norm(tangents, axis=1)
    tangent_ok = np.allclose(norms, 1.0, atol=1e-12)
    status = "PASS" if tangent_ok else "FAIL"
    print(f"  Tangent norms: min={norms.min():.12f}  max={norms.max():.12f}")
    print(f"  All unit length: {status}")
    all_pass &= tangent_ok

    # ── 2d. All three fermion-generation knots ──
    _section("2d · Three Fermion Generations")
    gen_knots = []
    gen_labels = []
    for (p, q, name) in [(2, 3, "Trefoil T(2,3) — Gen 1"),
                          (2, 5, "Solomon T(2,5) — Gen 2"),
                          (2, 7, "T(2,7) — Gen 3")]:
        pts = K.torus_knot(p, q, R=R, r=r, N=N_pts, use_gpu=False)
        gap = np.linalg.norm(pts[-1] - pts[0])
        gen_knots.append(pts)
        gen_labels.append(name)
        print(f"  {name:30s}  shape={pts.shape}  closure gap={gap:.2e}")

    # ── 2e. Render the trefoil ──
    _section("2e · Visual Render — Trefoil T(2,3)")
    save_trefoil = "uhf_trefoil_T23.png"
    try:
        plot_knot(
            pts_cpu,
            title="Trefoil Knot T(2,3) — 1st Generation Fermion",
            color="cyan",
            save_path=save_trefoil,
            show_torus=True,
            R=R, r=r,
        )
        render_ok = os.path.isfile(save_trefoil)
        status = "PASS" if render_ok else "FAIL"
        print(f"  Trefoil render saved: {save_trefoil}  → {status}")
    except Exception as exc:
        print(f"  [WARN] Trefoil render failed: {exc}")
        render_ok = False
    all_pass &= render_ok

    # ── 2f. Render all three generations ──
    _section("2f · Visual Render — All Three Generations")
    save_all = "uhf_knots_3gen.png"
    try:
        plot_multi_knots(
            gen_knots,
            labels=gen_labels,
            colors=["cyan", "magenta", "yellow"],
            title="UHF Fermion Generations — T(2,3) / T(2,5) / T(2,7)",
            save_path=save_all,
            R=R, r=r,
        )
        render3_ok = os.path.isfile(save_all)
        status = "PASS" if render3_ok else "FAIL"
        print(f"  3-generation render saved: {save_all}  → {status}")
    except Exception as exc:
        print(f"  [WARN] Multi-knot render failed: {exc}")
        render3_ok = False
    all_pass &= render3_ok

    # ── 2g. Torus surface (legacy) ──
    _section("2g · Torus Surface Grid (legacy)")
    X, Y, Z, _, _ = K.torus_surface(R=R, r=r, n_theta=64, n_phi=64)
    surf_ok = X.shape == (64, 64)
    status = "PASS" if surf_ok else "FAIL"
    print(f"  Shape: {X.shape}  x ∈ [{X.min():.3f}, {X.max():.3f}]  → {status}")
    all_pass &= surf_ok

    # ═══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*62}")
    if all_pass:
        print("  ALL TESTS PASSED — Workblock 2 validated.")
    else:
        print("  SOME TESTS FAILED — review output above.")
    print(f"{'='*62}\n")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
