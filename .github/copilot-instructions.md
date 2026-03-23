# Copilot Instructions — UHF Paper Repository

## Blockchain-First Commit Policy

**ALWAYS register papers on the Polygon blockchain BEFORE making a git commit.**

When any of the following files are modified:

- `UHF_Part_I_Core.md`
- `UHF_Part_II_Mathematical_Foundations.md`
- `UHF_Part_III_Standard_Model.md`

You MUST follow this exact sequence:

1. **Rebuild PDFs** — Run pandoc for each modified paper:
   ```bash
   pandoc <file>.md -o <file>.pdf --pdf-engine=/Library/TeX/texbin/xelatex -V geometry:margin=1in -V fontsize=11pt --highlight-style=tango
   ```

2. **Compute SHA-256 hashes** — Verify integrity:
   ```bash
   shasum -a 256 UHF_Part_I_Core.md UHF_Part_II_Mathematical_Foundations.md UHF_Part_III_Standard_Model.md
   ```

3. **Update the registration script** — Update the `expectedHash` values in the appropriate `deployment/scripts/register-v*.js` file to match the current hashes.

4. **Register on Polygon mainnet** — Run the registration script:
   ```bash
   node deployment/scripts/register-v<VERSION>.js
   ```
   Wait for all three transactions to confirm. Verify the PolygonScan links.

5. **Only THEN git commit and push** — Include the blockchain transaction hashes in the commit message or reference them.

## Simulation Suite Policy

When any files in `physics-core/simulations/` or `infrastructure/lbm-gpe/` are modified, you MUST also:

1. **Rebuild the zip** — Run the build script:
   ```bash
   bash deployment/scripts/build-simulation-zip.sh
   ```
   This compiles simulation sources into `out/UHF_Simulation_Suite.zip` (the `out/` directory is `.gitignore`'d).

2. **Compute the zip SHA-256** — The build script prints it, or run:
   ```bash
   shasum -a 256 out/UHF_Simulation_Suite.zip
   ```

3. **Update and run the registration script** — Update `EXPECTED_HASH` in `deployment/scripts/register-simulation.js`, then:
   ```bash
   node deployment/scripts/register-simulation.js
   ```
   Wait for the transaction to confirm.

4. **Only THEN git commit and push.**

### Contract Details

- **Contract**: `0xe0bB4bC3116e19F2c0c183eFf8802C4F707B0054` (Polygon Mainnet)
- **On-chain log**: `deployment/scripts/on-chain-log.txt`

### Why

The blockchain timestamp provides an immutable, independently verifiable proof of existence that predates (or is contemporaneous with) the git commit. Git history can be rewritten; Polygon blocks cannot.
