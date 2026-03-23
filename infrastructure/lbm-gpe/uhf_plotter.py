"""
UHF Phase 0 — Plotting Utilities
==================================
Lightweight helpers for rendering 3-D density isosurfaces from
NumPy grids.  Two back-ends are supported:

* **matplotlib** — always available; uses *marching cubes* via
  ``skimage.measure.marching_cubes`` and plots a ``Poly3DCollection``.
* **PyVista**    — richer interactive viewer; used when ``pyvista`` is
  importable.

All functions accept plain NumPy arrays so they work regardless of
whether data originated on CPU or GPU (call ``GridManager.to_numpy()``
first).
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

import numpy as np

# ── optional back-ends ──
try:
    import pyvista as pv
    PYVISTA_AVAILABLE = True
except ImportError:
    pv = None  # type: ignore[assignment]
    PYVISTA_AVAILABLE = False

try:
    from skimage.measure import marching_cubes
    SKIMAGE_AVAILABLE = True
except ImportError:
    marching_cubes = None  # type: ignore[assignment]
    SKIMAGE_AVAILABLE = False

import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection


# ─────────────────────────────────────────────────────────────────────
# Matplotlib isosurface (always available)
# ─────────────────────────────────────────────────────────────────────

def plot_isosurface_mpl(
    density: np.ndarray,
    level: Optional[float] = None,
    title: str = "UHF Density Isosurface",
    cmap: str = "plasma",
    alpha: float = 0.6,
    figsize: Tuple[int, int] = (10, 8),
    save_path: Optional[str] = None,
) -> None:
    """
    Render a single isosurface of a 3-D scalar field using matplotlib.

    Parameters
    ----------
    density : ndarray, shape (Nx, Ny, Nz)
        Real-valued 3-D field (e.g. |Ψ|²).
    level : float or None
        Iso-value.  If *None*, uses ``0.5 * (max + min)``.
    title, cmap, alpha, figsize : cosmetic options.
    save_path : str or None
        If given, saves the figure to this path instead of showing it.
    """
    if not SKIMAGE_AVAILABLE:
        raise ImportError(
            "scikit-image is required for marching_cubes.  "
            "Install with:  pip install scikit-image"
        )

    if level is None:
        level = 0.5 * (density.max() + density.min())

    verts, faces, normals, _ = marching_cubes(density, level=level)

    fig = plt.figure(figsize=figsize)
    ax = fig.add_subplot(111, projection="3d")

    mesh = Poly3DCollection(verts[faces], alpha=alpha, linewidths=0.1, edgecolors="k")
    # Color by z-coordinate of each face centroid
    face_centers = verts[faces].mean(axis=1)
    z_norm = (face_centers[:, 2] - face_centers[:, 2].min()) / (
        face_centers[:, 2].ptp() or 1.0
    )
    colors = plt.get_cmap(cmap)(z_norm)
    mesh.set_facecolor(colors)
    ax.add_collection3d(mesh)

    # Auto-scale axes
    ax.set_xlim(verts[:, 0].min(), verts[:, 0].max())
    ax.set_ylim(verts[:, 1].min(), verts[:, 1].max())
    ax.set_zlim(verts[:, 2].min(), verts[:, 2].max())
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_zlabel("Z")
    ax.set_title(title)

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"[plotter] Saved → {save_path}")
    else:
        plt.show()


# ─────────────────────────────────────────────────────────────────────
# Torus-Knot 3-D curve plotter  (Workblock 2)
# ─────────────────────────────────────────────────────────────────────

def plot_knot(
    points: np.ndarray,
    title: str = "Torus Knot",
    color: str = "cyan",
    linewidth: float = 1.8,
    figsize: Tuple[int, int] = (10, 8),
    save_path: Optional[str] = None,
    show_torus: bool = True,
    R: float = 1.0,
    r: float = 0.22,
    torus_alpha: float = 0.08,
) -> None:
    """
    Render a 3-D torus-knot curve (and optionally the host torus).

    Parameters
    ----------
    points : ndarray, shape (N, 3)
        Cartesian knot vertices.  May be a CuPy array — will be
        copied to CPU automatically.
    title : str
        Figure title.
    show_torus : bool
        If True, draw a semi-transparent wireframe of the host torus.
    R, r : float
        Major / minor radii for the ghost torus.
    save_path : str or None
        If given, saves the figure instead of displaying it.
    """
    # Accept CuPy arrays transparently
    if not isinstance(points, np.ndarray):
        points = np.asarray(points.get())  # CuPy → NumPy

    fig = plt.figure(figsize=figsize)
    ax = fig.add_subplot(111, projection="3d")

    # ── knot curve ──
    # Close the loop by appending the first point
    pts_closed = np.vstack([points, points[:1]])
    ax.plot(
        pts_closed[:, 0], pts_closed[:, 1], pts_closed[:, 2],
        color=color, linewidth=linewidth, label=title,
    )

    # ── ghost torus wireframe ──
    if show_torus:
        n_t, n_p = 40, 60
        theta = np.linspace(0, 2 * np.pi, n_t)
        phi = np.linspace(0, 2 * np.pi, n_p)
        theta, phi = np.meshgrid(theta, phi, indexing="ij")
        Xt = (R + r * np.cos(theta)) * np.cos(phi)
        Yt = (R + r * np.cos(theta)) * np.sin(phi)
        Zt = r * np.sin(theta)
        ax.plot_wireframe(
            Xt, Yt, Zt,
            color="gray", alpha=torus_alpha, linewidth=0.3,
            rstride=4, cstride=4,
        )

    # ── axis styling ──
    margin = R + r + 0.1
    ax.set_xlim(-margin, margin)
    ax.set_ylim(-margin, margin)
    ax.set_zlim(-margin, margin)
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_zlabel("Z")
    ax.set_title(title)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"[plotter] Saved → {save_path}")
    else:
        plt.show()
    plt.close(fig)


def plot_multi_knots(
    knots: list,
    labels: Optional[list] = None,
    colors: Optional[list] = None,
    title: str = "Torus Knots — UHF Fermion Generations",
    figsize: Tuple[int, int] = (10, 8),
    save_path: Optional[str] = None,
    R: float = 1.0,
    r: float = 0.22,
) -> None:
    """
    Overlay multiple knot curves on one plot.

    Parameters
    ----------
    knots : list of ndarray, each shape (N, 3)
    labels : list of str (one per knot)
    colors : list of colour specs
    """
    if labels is None:
        labels = [f"Knot {i}" for i in range(len(knots))]
    if colors is None:
        colors = ["cyan", "magenta", "yellow", "lime", "red"][:len(knots)]

    fig = plt.figure(figsize=figsize)
    ax = fig.add_subplot(111, projection="3d")

    for pts, lbl, clr in zip(knots, labels, colors):
        if not isinstance(pts, np.ndarray):
            pts = np.asarray(pts.get())
        pts_closed = np.vstack([pts, pts[:1]])
        ax.plot(pts_closed[:, 0], pts_closed[:, 1], pts_closed[:, 2],
                color=clr, linewidth=1.6, label=lbl)

    margin = R + r + 0.1
    ax.set_xlim(-margin, margin)
    ax.set_ylim(-margin, margin)
    ax.set_zlim(-margin, margin)
    ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.set_zlabel("Z")
    ax.set_title(title)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"[plotter] Saved → {save_path}")
    else:
        plt.show()
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────
# PyVista isosurface (optional, richer)
# ─────────────────────────────────────────────────────────────────────

def plot_isosurface_pv(
    density: np.ndarray,
    levels: Optional[Sequence[float]] = None,
    title: str = "UHF Density Isosurface",
    cmap: str = "plasma",
    opacity: float = 0.6,
    save_path: Optional[str] = None,
) -> None:
    """
    Render one or more isosurfaces using PyVista (GPU-accelerated viewer).

    Parameters
    ----------
    density : ndarray, shape (Nx, Ny, Nz)
    levels : sequence of floats or None
        Iso-values.  If *None*, picks three evenly-spaced levels.
    save_path : str or None
        If given, saves a screenshot.
    """
    if not PYVISTA_AVAILABLE:
        raise ImportError("PyVista is not installed.  pip install pyvista")

    grid = pv.ImageData(dimensions=np.array(density.shape) + 1)
    grid.cell_data["density"] = density.ravel(order="F")

    if levels is None:
        lo, hi = float(density.min()), float(density.max())
        levels = np.linspace(lo + 0.2 * (hi - lo), hi - 0.2 * (hi - lo), 3)

    plotter = pv.Plotter()
    plotter.set_background("black")
    for lev in levels:
        iso = grid.contour(isosurfaces=[lev], scalars="density")
        if iso.n_points > 0:
            plotter.add_mesh(iso, cmap=cmap, opacity=opacity, smooth_shading=True)
    plotter.add_title(title, font_size=12)

    if save_path:
        plotter.screenshot(save_path)
        print(f"[plotter] Saved → {save_path}")
    else:
        plotter.show()


# ─────────────────────────────────────────────────────────────────────
# Convenience dispatcher
# ─────────────────────────────────────────────────────────────────────

def plot_isosurface(
    density: np.ndarray,
    level: Optional[float] = None,
    backend: str = "auto",
    **kwargs,
) -> None:
    """
    Render an isosurface, auto-selecting the best available backend.

    Parameters
    ----------
    backend : {"auto", "matplotlib", "pyvista"}
    **kwargs : forwarded to the chosen backend function.
    """
    if backend == "auto":
        backend = "pyvista" if PYVISTA_AVAILABLE else "matplotlib"

    if backend == "pyvista":
        levels = [level] if level is not None else None
        plot_isosurface_pv(density, levels=levels, **kwargs)
    else:
        plot_isosurface_mpl(density, level=level, **kwargs)


# ─────────────────────────────────────────────────────────────────────
# Quick self-test — synthetic torus density
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from uhf_math_utils import UHFMathUtils

    # Build a toroidal density blob on a 64³ grid (fast test)
    N = 64
    lin = np.linspace(-2, 2, N)
    X, Y, Z = np.meshgrid(lin, lin, lin, indexing="ij")

    R_major, r_minor = 1.0, 0.22 * 1.0  # r/R ≈ 0.22
    # Distance from the torus skeleton
    d_torus = np.sqrt((np.sqrt(X**2 + Y**2) - R_major)**2 + Z**2)
    density = np.exp(-((d_torus / r_minor) ** 2))

    print(f"Synthetic torus density grid: {density.shape}, "
          f"range [{density.min():.4f}, {density.max():.4f}]")

    # Attempt to plot (will open a window or save a file)
    try:
        plot_isosurface(
            density,
            level=0.5,
            title="Phase 0 — Torus Test (r/R ≈ 0.22)",
            save_path="uhf_torus_test.png",
        )
    except ImportError as exc:
        print(f"[SKIP plotting] {exc}")
