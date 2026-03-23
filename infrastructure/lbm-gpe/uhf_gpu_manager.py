"""
UHF Phase 0 — GPU Manager
==========================
Detects an NVIDIA RTX 3090 via CuPy, validates VRAM, and provides a
GridManager class that allocates and manages 3-D FFT grids (256³ / 512³)
on the GPU.

Primary platform: **WSL2 (Ubuntu on Windows)** with the NVIDIA CUDA
driver forwarded from the Windows host.  Windows native is retained as
a fallback.

Dependencies
------------
* cupy — install with ``pip install cupy-cuda12x`` (adjust for your
  CUDA toolkit version).
* The WSL2 NVIDIA driver must expose ``/usr/lib/wsl/lib/libcuda.so``.
  Verify with ``nvidia-smi`` inside WSL.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

# ── Ensure CUDA shared libraries are discoverable ──
# WSL2 / Linux: CUDA libraries live in standard paths resolved by the
# dynamic linker.  We ensure LD_LIBRARY_PATH covers the usual locations
# so that CuPy (and cuFFT, cuBLAS, etc.) can find them at import time.
#
# Windows: uses os.add_dll_directory (kept as fallback for portability).
# ────────────────────────────────────────────────────────────────────
if sys.platform.startswith("linux"):
    # Typical CUDA toolkit + WSL2 driver stub paths
    _CUDA_LIB_DIRS = [
        "/usr/local/cuda/lib64",
        "/usr/local/cuda/extras/CUPTI/lib64",
        "/usr/lib/wsl/lib",          # WSL2 GPU driver user-mode libs
    ]
    # Also honour CUDA_HOME / CUDA_PATH if set
    for _env in ("CUDA_HOME", "CUDA_PATH"):
        _root = os.environ.get(_env, "")
        if _root:
            _CUDA_LIB_DIRS.append(os.path.join(_root, "lib64"))

    # Scan pip-installed nvidia-* packages (e.g. nvidia-cufft-cu12)
    import site as _site
    for _sp in _site.getsitepackages() + [_site.getusersitepackages()]:
        _nv = os.path.join(_sp, "nvidia")
        if os.path.isdir(_nv):
            for _pkg in os.listdir(_nv):
                for _sub in ("lib", "lib64"):
                    _lib = os.path.join(_nv, _pkg, _sub)
                    if os.path.isdir(_lib):
                        _CUDA_LIB_DIRS.append(_lib)

    _existing = os.environ.get("LD_LIBRARY_PATH", "")
    _to_add = [d for d in _CUDA_LIB_DIRS if os.path.isdir(d) and d not in _existing]
    if _to_add:
        os.environ["LD_LIBRARY_PATH"] = ":".join(_to_add) + ((":" + _existing) if _existing else "")

elif sys.platform == "win32" and hasattr(os, "add_dll_directory"):
    _CUDA_SEARCH_DIRS = [
        os.path.join(os.environ.get("CUDA_PATH", ""), "bin"),
        os.path.join(os.environ.get("CUDA_PATH", ""), "bin", "x64"),
    ]
    _TOOLKIT_ROOT = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"
    if os.path.isdir(_TOOLKIT_ROOT):
        for _ver in sorted(os.listdir(_TOOLKIT_ROOT), reverse=True):
            _CUDA_SEARCH_DIRS.append(os.path.join(_TOOLKIT_ROOT, _ver, "bin"))
    import site as _site
    for _sp in _site.getsitepackages() + [_site.getusersitepackages()]:
        _nv = os.path.join(_sp, "nvidia")
        if os.path.isdir(_nv):
            for _pkg in os.listdir(_nv):
                for _sub in ("bin", "lib"):
                    _lib = os.path.join(_nv, _pkg, _sub)
                    if os.path.isdir(_lib):
                        _CUDA_SEARCH_DIRS.append(_lib)
    for _d in _CUDA_SEARCH_DIRS:
        if os.path.isdir(_d):
            try:
                os.add_dll_directory(_d)
            except OSError:
                pass

try:
    import cupy as cp
    CUPY_AVAILABLE = True
except ImportError:
    cp = None  # type: ignore[assignment]
    CUPY_AVAILABLE = False

from uhf_config import (
    DEFAULT_GRID_SMALL,
    DEFAULT_GRID_LARGE,
    VRAM_REQUIRED_256,
    VRAM_REQUIRED_512,
    VRAM_SAFETY_FRACTION,
)


# ─────────────────────────────────────────────────────────────────────
# GPU discovery helpers
# ─────────────────────────────────────────────────────────────────────

def detect_gpu(required_name_fragment: str = "3090") -> dict:
    """
    Detect the first CUDA device whose name contains *required_name_fragment*.

    Returns
    -------
    dict  with keys: id, name, vram_total_gb, vram_free_gb, compute_cap
    Raises RuntimeError if CuPy is missing or no matching device is found.
    """
    if not CUPY_AVAILABLE:
        raise RuntimeError(
            "CuPy is not installed.  "
            "Install with:  pip install cupy-cuda12x  (adjust for your CUDA version)."
        )

    n_devices = cp.cuda.runtime.getDeviceCount()
    if n_devices == 0:
        raise RuntimeError("No CUDA devices detected.")

    for dev_id in range(n_devices):
        dev = cp.cuda.Device(dev_id)
        props = cp.cuda.runtime.getDeviceProperties(dev_id)
        name = props["name"].decode() if isinstance(props["name"], bytes) else props["name"]
        total_mem = props["totalGlobalMem"]

        if required_name_fragment.lower() in name.lower():
            # Query free memory while context is active
            with dev:
                free, total = cp.cuda.runtime.memGetInfo()
            return {
                "id": dev_id,
                "name": name,
                "vram_total_gb": total / (1024**3),
                "vram_free_gb": free / (1024**3),
                "compute_cap": (props["major"], props["minor"]),
            }

    raise RuntimeError(
        f"No GPU containing '{required_name_fragment}' in its name.  "
        f"Available: {[cp.cuda.runtime.getDeviceProperties(i)['name'] for i in range(n_devices)]}"
    )


def check_vram(gpu_info: dict, grid_n: int) -> bool:
    """
    Check whether a grid of size N³ fits within the **safe** VRAM budget.

    The safe budget is ``VRAM_SAFETY_FRACTION`` (default 80 %) of total
    VRAM, leaving headroom for FFT temporaries and CuPy internals so
    that we never OOM during a transform.
    """
    required_gb = VRAM_REQUIRED_512 if grid_n >= 512 else VRAM_REQUIRED_256
    usable_gb = gpu_info["vram_total_gb"] * VRAM_SAFETY_FRACTION
    ok = required_gb <= usable_gb and required_gb <= gpu_info["vram_free_gb"]
    print(f"[VRAM] need {required_gb:.1f} GB | "
          f"safe budget {usable_gb:.1f} GB ({VRAM_SAFETY_FRACTION*100:.0f}% of "
          f"{gpu_info['vram_total_gb']:.1f} GB) | "
          f"free {gpu_info['vram_free_gb']:.1f} GB => "
          f"{'OK' if ok else 'INSUFFICIENT'}")
    return ok


def vram_headroom_gb(gpu_info: dict) -> float:
    """Return the amount of VRAM (GB) still available within the safety budget."""
    usable = gpu_info["vram_total_gb"] * VRAM_SAFETY_FRACTION
    return max(0.0, min(usable, gpu_info["vram_free_gb"]))


# ─────────────────────────────────────────────────────────────────────
# GridManager — lifecycle owner for 3-D FFT grids
# ─────────────────────────────────────────────────────────────────────

@dataclass
class GridManager:
    """
    Manages a 3-D complex field on the GPU.

    Parameters
    ----------
    N : int
        Grid side-length (256 or 512).  Total cells = N³.
    device_id : int
        CUDA device ordinal (default 0).
    dtype : type
        NumPy/CuPy dtype for the field (default complex128).
    """

    N: int = DEFAULT_GRID_SMALL
    device_id: int = 0
    dtype: type = np.complex128

    # ── internal state ──
    _grid: Optional[object] = field(default=None, init=False, repr=False)
    _device: Optional[object] = field(default=None, init=False, repr=False)

    # ────────────────────────── lifecycle ──────────────────────────

    def allocate(self) -> "GridManager":
        """
        Allocate the N³ grid on the GPU and return *self* for chaining.

        Raises RuntimeError if the allocation would exceed the VRAM
        safety budget (80 % of total).
        """
        if not CUPY_AVAILABLE:
            raise RuntimeError("CuPy is required for GPU grid allocation.")
        self._device = cp.cuda.Device(self.device_id)

        # --- safety check ---
        with self._device:
            free, total = cp.cuda.runtime.memGetInfo()
        total_gb = total / (1024**3)
        free_gb = free / (1024**3)
        needed_gb = (self.N ** 3 * np.dtype(self.dtype).itemsize) / (1024**3)
        budget_gb = total_gb * VRAM_SAFETY_FRACTION

        if needed_gb > budget_gb:
            raise RuntimeError(
                f"Grid {self.N}³ ({needed_gb:.2f} GB) exceeds the "
                f"{VRAM_SAFETY_FRACTION*100:.0f}% safety budget "
                f"({budget_gb:.2f}/{total_gb:.1f} GB).")
        if needed_gb > free_gb:
            raise RuntimeError(
                f"Grid {self.N}³ ({needed_gb:.2f} GB) exceeds current "
                f"free VRAM ({free_gb:.2f} GB).")

        with self._device:
            self._grid = cp.zeros((self.N, self.N, self.N), dtype=self.dtype)
        print(f"[GridManager] Allocated {self.N}³ {self.dtype.__name__} grid "
              f"on device {self.device_id}  "
              f"({needed_gb:.2f} GB / {budget_gb:.1f} GB budget)")
        return self

    def free(self) -> None:
        """Explicitly release GPU memory."""
        if self._grid is not None:
            del self._grid
            self._grid = None
            if CUPY_AVAILABLE:
                cp.get_default_memory_pool().free_all_blocks()
        print("[GridManager] GPU memory released.")

    # ────────────────────────── accessors ─────────────────────────

    @property
    def grid(self):
        """Return the raw CuPy ndarray (or raise if not allocated)."""
        if self._grid is None:
            raise RuntimeError("Grid not allocated.  Call .allocate() first.")
        return self._grid

    @property
    def shape(self) -> Tuple[int, int, int]:
        return (self.N, self.N, self.N)

    # ────────────────────────── FFT helpers ───────────────────────

    def fft3(self):
        """Compute the 3-D FFT of the grid *in-place* and return the result."""
        with self._device:
            self._grid = cp.fft.fftn(self._grid)
        return self._grid

    def ifft3(self):
        """Compute the 3-D inverse FFT *in-place* and return the result."""
        with self._device:
            self._grid = cp.fft.ifftn(self._grid)
        return self._grid

    def fft_freq_axes(self):
        """Return the 3-D frequency coordinate arrays (on host)."""
        freqs = np.fft.fftfreq(self.N)
        kx, ky, kz = np.meshgrid(freqs, freqs, freqs, indexing="ij")
        return kx, ky, kz

    # ────────────────────────── utilities ─────────────────────────

    def fill_random(self, seed: int = 42) -> None:
        """Fill the grid with reproducible complex Gaussian noise."""
        with self._device:
            rng = cp.random.RandomState(seed)
            self._grid = (rng.standard_normal(self.shape)
                          + 1j * rng.standard_normal(self.shape)).astype(self.dtype)

    def density(self):
        """Return |Ψ|² on the GPU."""
        return cp.abs(self._grid) ** 2

    def to_numpy(self) -> np.ndarray:
        """Copy grid to host as a NumPy array."""
        return cp.asnumpy(self._grid)

    # -------- static CPU↔GPU transfer helpers for arbitrary fields --------

    @staticmethod
    def to_gpu(arr: np.ndarray, device_id: int = 0) -> "cp.ndarray":
        """
        Transfer an arbitrary NumPy array (scalar or vector field) to the GPU.

        Supports:
        - 3-D scalar fields  (Nx, Ny, Nz)
        - 3-D vector fields  (3, Nx, Ny, Nz)  or  (Nx, Ny, Nz, 3)
        - Any other shape (passed through).
        """
        if not CUPY_AVAILABLE:
            raise RuntimeError("CuPy is required for GPU transfers.")
        with cp.cuda.Device(device_id):
            return cp.asarray(arr)

    @staticmethod
    def to_cpu(arr) -> np.ndarray:
        """
        Transfer a CuPy array back to the CPU as a NumPy array.

        Safe to call on a NumPy array too (returns it unchanged).
        """
        if not CUPY_AVAILABLE:
            raise RuntimeError("CuPy is required for GPU transfers.")
        if isinstance(arr, np.ndarray):
            return arr
        return cp.asnumpy(arr)

    def __repr__(self) -> str:
        status = "allocated" if self._grid is not None else "empty"
        return f"GridManager(N={self.N}, dtype={self.dtype.__name__}, status={status})"


# ─────────────────────────────────────────────────────────────────────
# Quick self-test
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("── UHF GPU Manager self-test ──\n")

    try:
        info = detect_gpu("3090")
        print(f"  GPU found   : {info['name']}")
        print(f"  VRAM total  : {info['vram_total_gb']:.1f} GB")
        print(f"  VRAM free   : {info['vram_free_gb']:.1f} GB")
        print(f"  Compute cap : {info['compute_cap']}")

        grid_n = DEFAULT_GRID_LARGE if check_vram(info, DEFAULT_GRID_LARGE) else DEFAULT_GRID_SMALL
        print(f"\n  Selected grid size: {grid_n}³")

        gm = GridManager(N=grid_n, device_id=info["id"]).allocate()
        gm.fill_random()
        gm.fft3()
        gm.ifft3()
        print(f"  Round-trip FFT ✓  max residual = "
              f"{float(cp.max(cp.abs(gm.grid.imag))):.2e}")
        gm.free()

    except RuntimeError as exc:
        print(f"  [SKIP] {exc}", file=sys.stderr)
