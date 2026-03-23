/**
 * register-v803.js — UHF Paper Registry v8.0.3
 *
 * Search-and-Destroy Purge:
 *   • Eliminated all banned terms from Part II:
 *       - "vacuum bath" / "partial trace" → sector restriction / aux formalism
 *       - Pauli-Villars §4a DELETED, subsections renumbered
 *       - "AQFT superselection" → algebraic / hydrodynamic topological defects
 *       - "ξ → 0" → "kξ ≪ 1" (permanent cutoff, emergent Lorentz)
 *   • Purged all simulation files (uhf_phase41_proofs.py + 12 drafts)
 *   • Deleted stale manuscript copies from simulation/
 *   • Part I and Part III confirmed clean (no changes needed)
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
        version: "8.0.3-Part-I",
        label: "UHF Part I — Physical Core",
        expectedHash: "f86791ee45b3a6a2d9c82694f55351a7c442c3068379152b216ae69075ce9659"
    },
    {
        file: "UHF_Part_II_Mathematical_Foundations.md",
        version: "8.0.3-Part-II",
        label: "UHF Part II — Mathematical Foundations",
        expectedHash: "c854a7bfdcb63003b1fde60b16dbf1125b283194007e8dddcac0d32dd6732bd9"
    },
    {
        file: "UHF_Part_III_Standard_Model.md",
        version: "8.0.3-Part-III",
        label: "UHF Part III — Standard Model Extension",
        expectedHash: "fdbc37196745b0364b615384988637f1f5798bd01760d5421e71eef48e9c9b39"
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
    console.log("SUCCESS — UHF v8.0.3 (ALL 3 PARTS) REGISTERED ON POLYGON MAINNET");
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
