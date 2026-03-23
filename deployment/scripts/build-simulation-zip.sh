#!/usr/bin/env bash
# build-simulation-zip.sh — Compile simulation/ sources into out/UHF_Simulation_Suite.zip
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/out"
SIM="$ROOT/simulation"

mkdir -p "$OUT"

# Remove old zip if exists
rm -f "$OUT/UHF_Simulation_Suite.zip"

# Zip all Python sources and image outputs (flat, no directory prefix)
cd "$SIM"
zip -j "$OUT/UHF_Simulation_Suite.zip" *.py *.png

echo ""
echo "Built: $OUT/UHF_Simulation_Suite.zip"
shasum -a 256 "$OUT/UHF_Simulation_Suite.zip"
