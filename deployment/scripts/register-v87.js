/**
 * register-v87.js — UHF Paper Registry v8.7
 *
 * Final QFT-level integration (Proofs H & I):
 *   • Part II  — §9.3.5 III-D: 1PI Transversality & LSZ Reduction (Proof I)
 *                 Pi_L(q^2) = 0 exact, delta m^2 = 0, LSZ four conditions verified
 *   • Part III — §9.3.25: Singular Vortex Gauge Dynamics (Proof H)
 *                 A = A_smooth + A_sing, [d_mu,d_nu]theta_sing = 2pi n delta^2,
 *                 F_sing^a = (2pi/g) sum n_j^a eps delta^2, Gauss law D_i E_i^a = J_0^a
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
        version: "8.7-Part-I",
        label: "UHF Part I — Physical Core",
        expectedHash: "18454ef4e5cd27eee16ae68c09d0eaad48ffd94136d6b3540c61019840a54c82"
    },
    {
        file: "UHF_Part_II_Mathematical_Foundations.md",
        version: "8.7-Part-II",
        label: "UHF Part II — Mathematical Foundations",
        expectedHash: "d02f292288e399ea023865dd08d206e217842448dfd61a34062483ec84b8816f"
    },
    {
        file: "UHF_Part_III_Standard_Model.md",
        version: "8.7-Part-III",
        label: "UHF Part III — Standard Model Extension",
        expectedHash: "15515ce7d69da56b65507b18ffbc1343be95ecd00cb69765e3aca47aa1699922"
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
    console.log("SUCCESS — UHF v8.7 (ALL 3 PARTS) REGISTERED ON POLYGON MAINNET");
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
