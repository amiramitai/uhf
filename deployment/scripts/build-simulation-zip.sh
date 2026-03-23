#!/usr/bin/env bash
# build-simulation-zip.sh — Compile simulation sources into out/UHF_Simulation_Suite.zip
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/out"
SIM_PY="$ROOT/uhf_infrastructure/lbm-gpe"
SIM_PNG="$ROOT/uhf_physics/simulations"

mkdir -p "$OUT"

# Remove old zip if exists
rm -f "$OUT/UHF_Simulation_Suite.zip"

# Zip all Python sources and image outputs (flat, no directory prefix)
cd "$SIM_PY"
zip -j "$OUT/UHF_Simulation_Suite.zip" *.py
cd "$SIM_PNG"
zip -uj "$OUT/UHF_Simulation_Suite.zip" *.png

echo ""
echo "Built: $OUT/UHF_Simulation_Suite.zip"
shasum -a 256 "$OUT/UHF_Simulation_Suite.zip"
