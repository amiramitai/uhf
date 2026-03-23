/**
 * register-v5.js — UHF Paper Registry v5.0
 *
 * v5.0 (March 15, 2026) — NMR/MRI Retrodiction, Topological Quantum Chaos,
 *   Bekenstein Iterator, and Analog Cryptography:
 *   • §8.12: NMR/MRI relaxation dynamics from first principles (T₂* ∝ B₀⁻⁰·⁸⁵)
 *   • §11.1: Topological torsion as native engine for quantum chaos (GOE/GUE)
 *   • §11.2: Bekenstein Bound vs. Hilbert–Pólya — Quantum Dynamical Iterator
 *   • §11.3: Analog quantum cryptography via acoustic prime factorization
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
        version: "5.0-Part-I",
        label: "UHF Part I — Physical Core",
        expectedHash: "01f89a85a1d4188d22d30331a44f583153ae4627e082a0a3ec973af8b24a00ee"
    },
    {
        file: "UHF_Part_II_Mathematical_Foundations.md",
        version: "5.0-Part-II",
        label: "UHF Part II — Mathematical Foundations",
        expectedHash: "a61aa56057d271d59affd1062ecaa522d59fee518aedaa4acf8ce81e59907cf5"
    },
    {
        file: "UHF_Part_III_Standard_Model.md",
        version: "5.0-Part-III",
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
    console.log("SUCCESS — UHF v5.0 (ALL 3 PARTS) REGISTERED ON POLYGON MAINNET");
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
