"""
UHF Phase 0 — Mathematical Utilities  (Workblock 2)
=====================================================
Provides two foundational components for the Unified Hydrodynamic
Framework simulation stack:

**Logic Block A — Octonionic Algebra**
    ``OctonionMath``: static class implementing the full 8×8 Cayley
    multiplication table (Fano-plane convention), addition,
    multiplication, conjugation, norm, and inverse.  Backbone for the
    α derivation and the G₂ symmetry-breaking logic (Part III §9.3.24).

**Logic Block B — Torus Knot Geometry**
    ``KnotGeometry``: parametric coordinate generator for torus knots
    T(p, q) on the RTX 3090 via CuPy, plus legacy torus-surface and
    torus-volume helpers.  Provides the filament coordinates for CKM
    overlap integrals (§9.3.26) and Borromean-ring entanglement
    simulations (§9.3.28).

All GPU paths fall back to NumPy transparently when CuPy is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, Union

import numpy as np

# ── optional GPU backend ──
try:
    import cupy as cp            # type: ignore[import-untyped]
    CUPY_AVAILABLE = True
except ImportError:
    cp = None
    CUPY_AVAILABLE = False


# ╔═══════════════════════════════════════════════════════════════════╗
# ║  Logic Block A — Octonionic Algebra                              ║
# ╚═══════════════════════════════════════════════════════════════════╝

class OctonionMath:
    """
    Static/class-method implementation of the Cayley–Dickson octonions
    𝕆 = {1, e₁, …, e₇}.

    The multiplication table is derived from the **Fano-plane** oriented
    triples (Baez 2002).  The seven triples are:

        (1,2,3)  (1,4,5)  (1,7,6)
        (2,4,6)  (2,5,7)
        (3,4,7)  (3,6,5)

    Key identity:  e_i · e_j = −δ_{ij} + ε_{ijk} e_k

    This class is the backbone for:
    - The fine-structure constant α derivation
    - The G₂ automorphism / symmetry-breaking logic (Part III)
    """

    # Fano-plane oriented triples  (i, j, k)  ⇒  e_i · e_j = +e_k
    _FANO_TRIPLES = [
        (1, 2, 3),
        (1, 4, 5),
        (1, 7, 6),
        (2, 4, 6),
        (2, 5, 7),
        (3, 4, 7),
        (3, 6, 5),
    ]

    # Lazily built lookup  (i, j) → (sign, index)
    _MUL_TABLE: Optional[dict] = None

    # ────────────────── table construction ──────────────────

    @classmethod
    def _build_table(cls) -> dict:
        """Build the 8×8 multiplication lookup (lazy, once)."""
        if cls._MUL_TABLE is not None:
            return cls._MUL_TABLE

        table: dict[Tuple[int, int], Tuple[int, int]] = {}
        # e0 * anything = anything
        for i in range(8):
            table[(0, i)] = (+1, i)
            table[(i, 0)] = (+1, i)
        # ei * ei = -e0
        for i in range(1, 8):
            table[(i, i)] = (-1, 0)
        # Fano triples  ⇒  6 products per triple
        for (a, b, c) in cls._FANO_TRIPLES:
            table[(a, b)] = (+1, c)
            table[(b, a)] = (-1, c)
            table[(b, c)] = (+1, a)
            table[(c, b)] = (-1, a)
            table[(c, a)] = (+1, b)
            table[(a, c)] = (-1, b)

        cls._MUL_TABLE = table
        return table

    # ────────────────── constructors ──────────────────

    @staticmethod
    def octonion(
        a0: float = 0, a1: float = 0, a2: float = 0, a3: float = 0,
        a4: float = 0, a5: float = 0, a6: float = 0, a7: float = 0,
    ) -> np.ndarray:
        """Create an octonion as an 8-element float64 vector."""
        return np.array([a0, a1, a2, a3, a4, a5, a6, a7], dtype=np.float64)

    @staticmethod
    def basis(i: int) -> np.ndarray:
        """Return the *i*-th basis octonion e_i  (i = 0 … 7)."""
        v = np.zeros(8, dtype=np.float64)
        v[i] = 1.0
        return v

    # ────────────────── arithmetic ──────────────────

    @staticmethod
    def add(p: np.ndarray, q: np.ndarray) -> np.ndarray:
        """Component-wise addition of two octonions."""
        return p + q

    @classmethod
    def mul(cls, p: np.ndarray, q: np.ndarray) -> np.ndarray:
        """
        Multiply two octonions *p · q*  (each shape (8,)).

        Uses the full Fano-plane table.  Non-commutative and
        **non-associative** by construction.
        """
        table = cls._build_table()
        result = np.zeros(8, dtype=np.float64)
        for i in range(8):
            if p[i] == 0.0:
                continue
            for j in range(8):
                if q[j] == 0.0:
                    continue
                sign, idx = table[(i, j)]
                result[idx] += sign * p[i] * q[j]
        return result

    @staticmethod
    def conj(p: np.ndarray) -> np.ndarray:
        """Octonion conjugate: negate imaginary parts e₁…e₇."""
        c = p.copy()
        c[1:] = -c[1:]
        return c

    @staticmethod
    def norm(p: np.ndarray) -> float:
        """Octonion norm  |p| = √(p · p*)  = √(Σ aᵢ²)."""
        return float(np.sqrt(np.dot(p, p)))

    @classmethod
    def inv(cls, p: np.ndarray) -> np.ndarray:
        """Multiplicative inverse  p⁻¹ = p* / |p|²."""
        n2 = np.dot(p, p)
        if n2 == 0:
            raise ZeroDivisionError("Cannot invert the zero octonion.")
        return cls.conj(p) / n2

    # ────────────────── backward-compat aliases ──────────────────

    oct_mul  = mul
    oct_conj = conj
    oct_norm = norm
    oct_inv  = inv


# Legacy alias so existing code ` from uhf_math_utils import UHFMathUtils` works
UHFMathUtils = OctonionMath


# ╔═══════════════════════════════════════════════════════════════════╗
# ║  Logic Block B — Torus Knot Geometry                             ║
# ╚═══════════════════════════════════════════════════════════════════╝

class KnotGeometry:
    """
    Parametric coordinate generator for **torus knots** T(p, q).

    A torus knot T(p, q) winds *p* times around the meridian (poloidal)
    and *q* times around the longitude (toroidal) of a torus with major
    radius *R* and minor radius *r*.

    The standard parametrisation (one period, t ∈ [0, 2π]):

        x(t) = (R + r·cos(p·t)) · cos(q·t)
        y(t) = (R + r·cos(p·t)) · sin(q·t)
        z(t) =      r·sin(p·t)

    The output is an (N, 3) array of points — optionally on the GPU
    (CuPy) for downstream FFT / overlap-integral work.

    UHF context
    -----------
    Three generations of fermions are identified with:

    ========  ===========  ======
    Gen       Knot         (p,q)
    ========  ===========  ======
    1st       Trefoil      (2,3)
    2nd       Solomon      (2,5)
    3rd       —            (2,7)
    ========  ===========  ======

    The CKM mixing angles are geometric overlap integrals between
    these knot wave-functions on the torus (§9.3.26).
    """

    @staticmethod
    def torus_knot(
        p: int,
        q: int,
        R: float = 1.0,
        r: float = 0.22,
        N: int = 1024,
        *,
        use_gpu: bool = True,
        device_id: int = 0,
    ) -> Union[np.ndarray, "cp.ndarray"]:
        """
        Generate N points along a torus knot T(p, q).

        Parameters
        ----------
        p, q : int
            Winding numbers.  gcd(p, q) must be 1 for a knot (not a link).
        R : float
            Major radius (center-of-torus to center-of-tube).
        r : float
            Minor radius (tube radius).   Default 0.22 matches r/R from
            uhf_config.R_OVER_R_NOMINAL when R = 1.
        N : int
            Number of discretisation points along the curve.
        use_gpu : bool
            If True and CuPy is available, return a CuPy array on *device_id*.
        device_id : int
            CUDA device ordinal (default 0 = RTX 3090).

        Returns
        -------
        points : ndarray, shape (N, 3)
            Cartesian coordinates (x, y, z) of the knot.
        """
        xp = cp if (use_gpu and CUPY_AVAILABLE) else np

        if use_gpu and CUPY_AVAILABLE:
            with cp.cuda.Device(device_id):
                t = xp.linspace(0, 2 * xp.pi, N, endpoint=False)
                x = (R + r * xp.cos(p * t)) * xp.cos(q * t)
                y = (R + r * xp.cos(p * t)) * xp.sin(q * t)
                z = r * xp.sin(p * t)
                return xp.stack([x, y, z], axis=1)
        else:
            t = xp.linspace(0, 2 * xp.pi, N, endpoint=False)
            x = (R + r * xp.cos(p * t)) * xp.cos(q * t)
            y = (R + r * xp.cos(p * t)) * xp.sin(q * t)
            z = r * xp.sin(p * t)
            return xp.stack([x, y, z], axis=1)

    @staticmethod
    def torus_knot_tangent(
        p: int,
        q: int,
        R: float = 1.0,
        r: float = 0.22,
        N: int = 1024,
        *,
        use_gpu: bool = True,
        device_id: int = 0,
    ) -> Union[np.ndarray, "cp.ndarray"]:
        """
        Unit tangent vectors along T(p, q).

        Useful for Gauss linking integrals and Frenet–Serret frames.
        """
        xp = cp if (use_gpu and CUPY_AVAILABLE) else np

        if use_gpu and CUPY_AVAILABLE:
            ctx = cp.cuda.Device(device_id)
            ctx.__enter__()

        t = xp.linspace(0, 2 * xp.pi, N, endpoint=False)

        dx = -p * r * xp.sin(p * t) * xp.cos(q * t) \
             - q * (R + r * xp.cos(p * t)) * xp.sin(q * t)
        dy = -p * r * xp.sin(p * t) * xp.sin(q * t) \
             + q * (R + r * xp.cos(p * t)) * xp.cos(q * t)
        dz =  p * r * xp.cos(p * t)

        T = xp.stack([dx, dy, dz], axis=1)
        norms = xp.linalg.norm(T, axis=1, keepdims=True)
        T = T / norms

        if use_gpu and CUPY_AVAILABLE:
            ctx.__exit__(None, None, None)

        return T

    # ─────────── Legacy torus-surface / volume helpers ───────────

    @staticmethod
    def torus_surface(
        R: float,
        r: float,
        n_theta: int = 128,
        n_phi: int = 128,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Parametric torus surface (θ, φ) → (x, y, z).

        Returns  X, Y, Z, theta, phi  — each shape (n_theta, n_phi).
        """
        theta = np.linspace(0, 2 * np.pi, n_theta, endpoint=False)
        phi = np.linspace(0, 2 * np.pi, n_phi, endpoint=False)
        theta, phi = np.meshgrid(theta, phi, indexing="ij")

        X = (R + r * np.cos(theta)) * np.cos(phi)
        Y = (R + r * np.cos(theta)) * np.sin(phi)
        Z = r * np.sin(theta)

        return X, Y, Z, theta, phi

    @staticmethod
    def torus_volume_grid(
        R: float,
        r: float,
        n_theta: int = 64,
        n_phi: int = 64,
        n_rho: int = 32,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        3-D volume grid filling the interior of a torus.

        Returns  X, Y, Z  — each shape (n_rho, n_theta, n_phi).
        """
        rho = np.linspace(0, r, n_rho, endpoint=True)
        theta = np.linspace(0, 2 * np.pi, n_theta, endpoint=False)
        phi = np.linspace(0, 2 * np.pi, n_phi, endpoint=False)
        rho, theta, phi = np.meshgrid(rho, theta, phi, indexing="ij")

        X = (R + rho * np.cos(theta)) * np.cos(phi)
        Y = (R + rho * np.cos(theta)) * np.sin(phi)
        Z = rho * np.sin(theta)

        return X, Y, Z


# ─────────────────────────────────────────────────────────────────────
# Quick self-test
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    O = OctonionMath

    # ── Octonion smoke tests ──
    e = [O.basis(i) for i in range(8)]

    # e1·e2 should be +e3
    r12 = O.mul(e[1], e[2])
    assert np.allclose(r12, e[3]), f"e1·e2 failed: {r12}"

    # e2·e1 should be -e3  (anti-commutativity)
    r21 = O.mul(e[2], e[1])
    assert np.allclose(r21, -e[3]), f"e2·e1 failed: {r21}"

    # ei·ei = -1
    for i in range(1, 8):
        sq = O.mul(e[i], e[i])
        assert np.allclose(sq, -e[0]), f"e{i}² failed: {sq}"

    # Non-associativity:  (e1·e2)·e4  ≠  e1·(e2·e4)
    lhs = O.mul(O.mul(e[1], e[2]), e[4])
    rhs = O.mul(e[1], O.mul(e[2], e[4]))
    assert not np.allclose(lhs, rhs), "Octonions should be non-associative!"
    print(f"✓  Non-associativity verified:  (e1·e2)·e4 = {lhs}  ≠  e1·(e2·e4) = {rhs}")

    # norm & inverse
    p = O.octonion(1, 2, 3, 4, 5, 6, 7, 8)
    pinv = O.inv(p)
    identity_check = O.mul(p, pinv)
    assert abs(identity_check[0] - 1.0) < 1e-12, f"p·p⁻¹ ≠ 1: {identity_check}"
    assert np.max(np.abs(identity_check[1:])) < 1e-12, f"p·p⁻¹ has imaginary residue"

    print("✓  Octonion arithmetic tests passed.")

    # ── Torus-knot smoke test ──
    K = KnotGeometry
    pts = K.torus_knot(2, 3, R=1.0, r=0.22, N=512, use_gpu=False)
    assert pts.shape == (512, 3), f"Unexpected shape: {pts.shape}"
    # Check closure:  first point ≈ last point  (periodic curve)
    gap = np.linalg.norm(pts[0] - pts[-1])
    print(f"✓  Trefoil T(2,3): {pts.shape}, closure gap = {gap:.2e}")

    # ── Torus surface smoke test ──
    X, Y, Z, _, _ = K.torus_surface(R=1.0, r=0.22, n_theta=64, n_phi=64)
    assert X.shape == (64, 64)
    print(f"✓  Torus surface grid: {X.shape}  "
          f"x ∈ [{X.min():.3f}, {X.max():.3f}]")
