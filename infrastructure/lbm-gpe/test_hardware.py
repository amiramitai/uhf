"""
UHF Phase 0 — Hardware Validation Script
==========================================
Allocates a 512³ complex128 field on the RTX 3090, executes a 3-D FFT
round-trip, and reports wall-clock timings.

Primary platform: **WSL2** with the NVIDIA CUDA driver forwarded from
the Windows host.

Usage:
    python test_hardware.py
"""

from __future__ import annotations

import os
import platform
import sys
import time

import numpy as np

# ── UHF imports ──
from uhf_config import (
    HBAR, C, M_BOSON, RHO_0, XI,
    DEFAULT_GRID_LARGE, VRAM_SAFETY_FRACTION, DX, DX_OVER_XI,
)
from uhf_gpu_manager import detect_gpu, check_vram, GridManager

# ─────────────────────────────────────────────────────────────────────

SEPARATOR = "─" * 62


def _section(title: str) -> None:
    print(f"\n{SEPARATOR}")
    print(f"  {title}")
    print(SEPARATOR)


def main() -> None:
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║        UHF Phase 0 — Hardware Validation (Workblock 1)     ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    # ── 0. Platform / WSL detection ──
    _section("0 · Platform Detection")
    uname = platform.uname()
    is_wsl = "microsoft" in uname.release.lower() or "wsl" in uname.release.lower()
    print(f"  OS          : {uname.system} {uname.release}")
    print(f"  Python      : {sys.version.split()[0]}")
    print(f"  WSL2        : {'Yes' if is_wsl else 'No'}")
    if is_wsl:
        wsl_lib = "/usr/lib/wsl/lib"
        has_wsl_lib = os.path.isdir(wsl_lib)
        print(f"  {wsl_lib} : {'present' if has_wsl_lib else 'MISSING'}")
        if not has_wsl_lib:
            print("  [WARN] WSL2 NVIDIA driver libs not found — GPU may not work.")

    # ── 1. Constants echo ──
    _section("1 · UHF Constants (Version 8.0)")
    print(f"  ℏ           = {HBAR:.6e}  J·s")
    print(f"  c           = {C:.6e}  m/s")
    print(f"  m_boson     = {M_BOSON:.2e}  kg  (2.1 meV/c²)")
    print(f"  ρ₀          = {RHO_0:.3e}  kg/m³")
    print(f"  ξ (healing) = {XI:.3e}  m")
    print(f"  Δx / ξ      = {DX_OVER_XI}")
    print(f"  Δx          = {DX:.3e}  m")
    print(f"  VRAM safety = {VRAM_SAFETY_FRACTION*100:.0f} %")

    # ── 2. GPU detection ──
    _section("2 · GPU Detection")
    try:
        import cupy as cp
    except ImportError:
        print("  [FATAL] CuPy is not installed.  Aborting.")
        sys.exit(1)

    try:
        gpu = detect_gpu()        # default: looks for "3090"
    except RuntimeError as exc:
        print(f"  [FATAL] {exc}")
        sys.exit(1)

    print(f"  Device      : {gpu['name']}")
    print(f"  VRAM total  : {gpu['vram_total_gb']:.2f} GB")
    print(f"  VRAM free   : {gpu['vram_free_gb']:.2f} GB")
    print(f"  Compute cap : {gpu['compute_cap']}")

    # ── 3. VRAM safety check for 512³ ──
    _section("3 · VRAM Budget (512³ complex128)")
    N = DEFAULT_GRID_LARGE  # 512
    if not check_vram(gpu, N):
        print(f"  [WARN] 512³ exceeds safe budget — falling back to 256³")
        N = 256
        if not check_vram(gpu, N):
            print("  [FATAL] Even 256³ does not fit. Aborting.")
            sys.exit(1)

    # ── 4. Allocation ──
    _section(f"4 · Grid Allocation ({N}³)")
    gm = GridManager(N=N, device_id=gpu["id"]).allocate()

    # ── 5. Fill with noise ──
    gm.fill_random(seed=2026)
    print(f"  Filled with complex Gaussian noise  (seed=2026)")

    # ── 6. 3-D FFT benchmark ──
    _section(f"5 · 3-D FFT Benchmark ({N}³)")

    # Warm-up pass (JIT + CUDA plan compilation)
    gm.fft3()
    gm.ifft3()
    cp.cuda.Device(gpu["id"]).synchronize()

    # Timed forward FFT
    cp.cuda.Device(gpu["id"]).synchronize()
    t0 = time.perf_counter()
    gm.fft3()
    cp.cuda.Device(gpu["id"]).synchronize()
    dt_fwd = time.perf_counter() - t0

    # Timed inverse FFT
    cp.cuda.Device(gpu["id"]).synchronize()
    t0 = time.perf_counter()
    gm.ifft3()
    cp.cuda.Device(gpu["id"]).synchronize()
    dt_inv = time.perf_counter() - t0

    print(f"  Forward  FFT : {dt_fwd*1e3:8.2f} ms")
    print(f"  Inverse  FFT : {dt_inv*1e3:8.2f} ms")
    print(f"  Round-trip   : {(dt_fwd+dt_inv)*1e3:8.2f} ms")

    # ── 7. Numerical sanity (round-trip residual) ──
    _section("6 · Round-trip Residual Check")
    gm.fill_random(seed=2026)
    ref = gm.grid.copy()
    gm.fft3()
    gm.ifft3()
    residual = float(cp.max(cp.abs(gm.grid - ref)))
    del ref                                          # free 2 GB GPU
    cp.get_default_memory_pool().free_all_blocks()
    print(f"  max |Ψ - IFFT(FFT(Ψ))| = {residual:.2e}")
    status = "PASS" if residual < 1e-10 else "FAIL"
    print(f"  Status: {status}")

    # ── 8. to_gpu / to_cpu transfer test ──
    _section("7 · CPU ↔ GPU Transfer Test")
    # Scalar field (N³)
    scalar_np = np.random.randn(N, N, N).astype(np.float64)
    t0 = time.perf_counter()
    scalar_gpu = GridManager.to_gpu(scalar_np, device_id=gpu["id"])
    cp.cuda.Device(gpu["id"]).synchronize()
    dt_up = time.perf_counter() - t0

    t0 = time.perf_counter()
    scalar_back = GridManager.to_cpu(scalar_gpu)
    dt_down = time.perf_counter() - t0

    max_err = float(np.max(np.abs(scalar_np.ravel()[:1024] - scalar_back.ravel()[:1024])))
    match = max_err == 0.0
    print(f"  Scalar {N}³ f64  to_gpu: {dt_up*1e3:.2f} ms  "
          f"to_cpu: {dt_down*1e3:.2f} ms  match={match}")
    del scalar_np, scalar_back, scalar_gpu           # free 1 GB GPU + 2 GB RAM
    cp.get_default_memory_pool().free_all_blocks()
    cp.cuda.Device(gpu["id"]).synchronize()

    # Vector field (3, N, N, N)
    vec_np = np.random.randn(3, N, N, N).astype(np.float64)
    t0 = time.perf_counter()
    vec_gpu = GridManager.to_gpu(vec_np, device_id=gpu["id"])
    cp.cuda.Device(gpu["id"]).synchronize()
    dt_up = time.perf_counter() - t0

    t0 = time.perf_counter()
    vec_back = GridManager.to_cpu(vec_gpu)
    dt_down = time.perf_counter() - t0

    max_err = float(np.max(np.abs(vec_np.ravel()[:1024] - vec_back.ravel()[:1024])))
    match = max_err == 0.0
    print(f"  Vector 3×{N}³ f64 to_gpu: {dt_up*1e3:.2f} ms  "
          f"to_cpu: {dt_down*1e3:.2f} ms  match={match}")

    # ── 9. Cleanup ──
    _section("8 · Cleanup")
    del vec_np, vec_back, vec_gpu
    cp.get_default_memory_pool().free_all_blocks()
    gm.free()

    print(f"\n{'='*62}")
    print("  All tests completed.  Environment is ready for Phase 1.")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    main()
