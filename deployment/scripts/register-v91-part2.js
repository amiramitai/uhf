/**
 * register-v91-part2.js — UHF Paper Registry v9.1 (Part II Update)
 *
 * v9.1 Part II (March 18, 2026) — Journal-Safe Rhetorical Overhaul:
 *   • Subtitle: "Haag Resolution" → "Finite-Volume Haag Bypass"
 *   • Abstract: Rewritten (EFT framing, Extension Module B deferral)
 *   • §9.1 Introduction: New "Effective IR Bridge" subsection
 *   • §9.3.1: "Exact Derivation" → "Post-Newtonian Isomorphism"
 *   • III-B: "Exact Photon Masslessness" → exponentially suppressed Proca mass
 *   • §9.3.23: "Wightman-Madelung Isomorphism" → "Effective IR Wightman Compliance"
 *   • §9.3.23a: "Haag's Theorem Resolution" → "Finite-Volume Effective Bypass"
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
        version: "9.1-Part-I-r2",
        label: "UHF Part I — Physical Core",
        expectedHash: "4b59eb169b87bdefa683c6734b56e4fa06f8762e151340f7a0915973175d5f5f"
    },
    {
        file: "UHF_Part_II_Mathematical_Foundations.md",
        version: "9.1-Part-II",
        label: "UHF Part II — Mathematical Foundations (Journal-Safe Overhaul)",
        expectedHash: "081deed04da7592350152c4243d56840809d32acd7c3f3248f4b858aceaa0fc7"
    },
    {
        file: "UHF_Part_III_Standard_Model.md",
        version: "9.1-Part-III-r2",
        label: "UHF Part III — Standard Model Extension",
        expectedHash: "aed3c9a2301c92d33f273e56939ee6562a94cea9520baec6132eaa74357fc90f"
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
    console.log("SUCCESS — UHF v9.1 Part II REGISTERED ON POLYGON MAINNET");
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
