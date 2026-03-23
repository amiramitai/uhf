/**
 * register-papers.js — UHF Journal Preprint Registry
 *
 * Registers all 7 journal preprint PDFs (Papers 1–7) on Polygon Mainnet.
 *   Core Stack (Papers 1–3): Tone-revised, grid-converged LBM studies
 *   Exploratory Frontiers (Papers 4–7): Heuristic extensions
 *
 * Contract: 0xe0bB4bC3116e19F2c0c183eFf8802C4F707B0054  (Polygon Mainnet)
 */

import { ethers } from "ethers";
import { execSync } from "child_process";
import dotenv from "dotenv";
import fs from "fs";
dotenv.config();

const CONTRACT_ADDRESS = "0xe0bB4bC3116e19F2c0c183eFf8802C4F707B0054";
const ABI = [
    "function registerPaper(string _hash, string _author, string _version) public"
];

const PAPERS = [
    // ── Core Stack ──
    {
        file: "physics-core/papers/paper1/Paper1_Emergent_Inertia_LBM.pdf",
        version: "10.0-Paper1-PDF",
        label: "Paper 1 — Added-Mass Drag on Topological Defects (LBM)",
        expectedHash: "ee7a3f1bd942ea6195103551f93bdcd60806ee173dd6a0a148ad6eef58059af4"
    },
    {
        file: "physics-core/papers/paper2/Paper2_Effective_GR_Viscoelastic.pdf",
        version: "10.0-Paper2-PDF",
        label: "Paper 2 — Effective IR Correspondence (Linearized Gravity)",
        expectedHash: "a9ac4e4eec6d15123de68509af669868b997fce8f2dffa66222ac5b1ace85db3"
    },
    {
        file: "physics-core/papers/paper3/Paper3_Hawking_Analogue.pdf",
        version: "10.0-Paper3-PDF",
        label: "Paper 3 — Acoustic Hawking Radiation (Superfluid Analogue)",
        expectedHash: "3f49fbf0ef49f3faef4c44b13ddfd8dee0d9557d84fd7b8b65daaf5994c1a126"
    },
    // ── Exploratory Frontiers ──
    {
        file: "physics-core/papers/paper4/Paper4_Superfluid_Cosmology.pdf",
        version: "10.1-Paper4-PDF",
        label: "Paper 4 — Heuristic Superfluid Cosmology",
        expectedHash: "6736b5b3d00e7f4f0035b4088235b034e87e3e5a4edc322965b56ff127a6b8b1"
    },
    {
        file: "physics-core/papers/paper5/Paper5_Topological_Standard_Model.pdf",
        version: "10.1-Paper5-PDF",
        label: "Paper 5 — Torus Knot / Fermion Correspondences",
        expectedHash: "638fd38aec72d4edd4c9f763eead924bcd3beddf7802cae9f4d5d546d12ad033"
    },
    {
        file: "physics-core/papers/paper6/Paper6_Topological_Chromodynamics.pdf",
        version: "10.1-Paper6-PDF",
        label: "Paper 6 — Borromean Vortex Confinement Analogue",
        expectedHash: "8bf7141542e8571115d4db408ea17d7bcfff498a31ac852676f76049ced707d9"
    },
    {
        file: "physics-core/papers/paper7/Paper7_Quantum_Entanglement.pdf",
        version: "10.1-Paper7-PDF",
        label: "Paper 7 — Bell-Inequality Violation (Phase Synchronisation)",
        expectedHash: "b2dc777384543de3737a96b6026cdeafa649cf0f4ec3d85afc5e1ef8f2cfced1"
    }
];

async function main() {
    // 1. Verify SHA-256 hashes
    console.log("Verifying SHA-256 hashes for all 7 paper PDFs...\n");
    for (const p of PAPERS) {
        const actual = execSync(`shasum -a 256 "${p.file}"`).toString().split(" ")[0];
        if (actual !== p.expectedHash) {
            console.error(`HASH MISMATCH for ${p.file}!`);
            console.error(`  Expected: ${p.expectedHash}`);
            console.error(`  Actual:   ${actual}`);
            process.exit(1);
        }
        console.log(`  ✓ ${p.file}`);
        console.log(`    ${actual}`);
    }

    // 2. Connect to Polygon
    const provider = new ethers.JsonRpcProvider("https://polygon-bor-rpc.publicnode.com", 137);
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT_ADDRESS, ABI, wallet);

    console.log(`\nWallet: ${wallet.address}`);
    const balance = await provider.getBalance(wallet.address);
    console.log(`Balance: ${ethers.formatEther(balance)} MATIC\n`);

    // 3. Register each paper
    const results = [];
    for (const p of PAPERS) {
        console.log(`Registering ${p.label} (${p.version})...`);
        const tx = await contract.registerPaper(p.expectedHash, "Amir Benjamin Amitay", p.version);
        console.log(`  TX sent: ${tx.hash}`);
        const receipt = await tx.wait();
        console.log(`  Confirmed in block ${receipt.blockNumber}`);
        results.push({
            label: p.label,
            version: p.version,
            hash: p.expectedHash,
            txHash: tx.hash,
            block: receipt.blockNumber
        });
    }

    // 4. Append to on-chain log
    const logLines = results.map(r =>
        `${r.version} | ${new Date().toISOString()} | Block: ${r.block} | TX: ${r.txHash} | Hash: ${r.hash}`
    ).join("\n") + "\n";
    fs.appendFileSync("scripts/on-chain-log.txt", logLines);

    // 5. Print summary
    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — ALL 7 UHF JOURNAL PREPRINTS REGISTERED ON POLYGON MAINNET");
    console.log("=".repeat(70));
    for (const r of results) {
        console.log(`\n  ${r.label} (${r.version})`);
        console.log(`    SHA-256:     ${r.hash}`);
        console.log(`    TX Hash:     ${r.txHash}`);
        console.log(`    Block:       ${r.block}`);
        console.log(`    PolygonScan: https://polygonscan.com/tx/${r.txHash}`);
    }
    console.log("\n" + "=".repeat(70));
    console.log("On-chain log updated: scripts/on-chain-log.txt");
}

main().catch(err => { console.error(err); process.exit(1); });
