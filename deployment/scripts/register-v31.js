/**
 * register-v31.js — UHF Paper Registry v3.1
 *
 * v3.1 (March 15, 2026) — BSSN-EKG Singularity Avoidance & Torsional Dynamo:
 *   • New abstract: Superfluid vacuum with torsional defects, Phases 9–12
 *   • Purged mass-independent GW echo claims (empirically falsified by PTA data)
 *   • §8.10: 3D BSSN-EKG singularity avoidance (α > 0, no apparent horizon)
 *   • §8.11: Torsional dynamo equilibrium (K^5 ≈ 0.06)
 *   • §9.6: Crown jewel prediction — parity-violating circular polarization
 *           |h_L/h_R| ≈ 0.02–0.08 in BBH ringdowns (falsifiable by O5/LISA)
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
    {
        file: "UHF_Part_I_Core.md",
        version: "3.1-Part-I",
        label: "UHF Part I — Physical Core",
        expectedHash: "3f85464cf067c4f9e496edcee48227b512582716c4d1354e934c76e8b3ef090c"
    },
    {
        file: "UHF_Part_II_Mathematical_Foundations.md",
        version: "3.1-Part-II",
        label: "UHF Part II — Mathematical Foundations",
        expectedHash: "a61aa56057d271d59affd1062ecaa522d59fee518aedaa4acf8ce81e59907cf5"
    },
    {
        file: "UHF_Part_III_Standard_Model.md",
        version: "3.1-Part-III",
        label: "UHF Part III — Standard Model Extension",
        expectedHash: "aff14ba22576543ca3f3308947e3248716e8e50a03cb9c88d7170fe5d293749f"
    }
];

async function main() {
    // 1. Verify SHA-256 hashes
    console.log("Verifying SHA-256 hashes...\n");
    for (const p of PAPERS) {
        const actual = execSync(`shasum -a 256 ${p.file}`).toString().split(" ")[0];
        if (actual !== p.expectedHash) {
            console.error(`HASH MISMATCH for ${p.file}!`);
            console.error(`  Expected: ${p.expectedHash}`);
            console.error(`  Actual:   ${actual}`);
            process.exit(1);
        }
        console.log(`  ✓ ${p.file}  ${actual}`);
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

    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — UHF v3.1 (ALL 3 PARTS) REGISTERED ON POLYGON MAINNET");
    console.log("=".repeat(70));
    for (const r of results) {
        console.log(`\n  ${r.label} (${r.version})`);
        console.log(`    SHA-256:     ${r.hash}`);
        console.log(`    TX Hash:     ${r.txHash}`);
        console.log(`    Block:       ${r.block}`);
        console.log(`    PolygonScan: https://polygonscan.com/tx/${r.txHash}`);
    }
    console.log("=".repeat(70));

    for (const r of results) {
        const logEntry = `\n${r.version} | ${new Date().toISOString()} | Block: ${r.block} | TX: ${r.txHash} | Hash: ${r.hash}`;
        fs.appendFileSync("./scripts/on-chain-log.txt", logEntry);
    }
    console.log("\nAppended to on-chain-log.txt");
}

main().catch(e => { console.error(e); process.exit(1); });
