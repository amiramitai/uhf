/**
 * register-v10.js — UHF Paper Registry v10.0
 *
 * v10.0 (March 21, 2026) — Publication Pipeline Release:
 *   Part I: §1.3 CFD verification (2D D2Q9 + 3D D3Q19 torus)
 *   Defense Addendum: §9.1 JWST rewrite, §9.3 Bullet Cluster,
 *     §10 Ponderomotive Thruster, rhetorical scrub
 *   Simulation Suite: 3D convergence scripts, Hawking v5 correlator,
 *     added-mass CFD/LBM verification, new plots
 *
 * Also registers (unchanged from v9.1-lexical):
 *   Part II, Part III — hashes verified but NOT re-registered
 *
 * Contract: 0xe0bB4bC3116e19F2c0c183eFf8802C4F707B0054  (Polygon Mainnet)
 */

import { ethers } from "ethers";
import { execSync } from "child_process";
import dotenv from "dotenv";
import fs from "fs";
import crypto from "crypto";
dotenv.config();

const CONTRACT_ADDRESS = "0xe0bB4bC3116e19F2c0c183eFf8802C4F707B0054";
const ABI = [
    "function registerPaper(string _hash, string _author, string _version) public"
];

const PAPERS = [
    {
        file: "UHF_Part_I_Core.md",
        version: "10.0-Part-I",
        label: "UHF Part I — Physical Core (CFD Verification + Publication Pipeline)",
        expectedHash: "6a0894ffe8165a2fde23d3fc2bf373bf9e27f208a122532ed7e6296d4dc68986"
    },
    {
        file: "UHF_Part_I_Core.pdf",
        version: "10.0-Part-I-PDF",
        label: "UHF Part I — Physical Core (PDF)",
        expectedHash: "76106eef97d49a9fb5eae621bfe3aa7fd5fd8482630e75a51dc2bc4f124a722b"
    },
    {
        file: "UHF_Defense_Addendum.md",
        version: "10.0-Addendum",
        label: "UHF Defense Addendum (JWST §9.1 + Bullet Cluster §9.3 + Thruster §10)",
        expectedHash: "d06696fabcee02c6ba9fe58289454ac9e22394b99793a48703a71422f7aa591a"
    },
    {
        file: "UHF_Defense_Addendum.pdf",
        version: "10.0-Addendum-PDF",
        label: "UHF Defense Addendum (PDF)",
        expectedHash: "6ddbebcec9bc7d0ce92758ac2ea884d284e2227a66eb13c59d0b5ad15243b0f4"
    }
];

const SIM_ZIP = {
    file: "out/UHF_Simulation_Suite.zip",
    version: "10.0-Simulation",
    label: "UHF Simulation Suite (3D Convergence + Hawking v5 + CFD Verification)",
    expectedHash: "494faa973288a5a23ccb63693c430f178ab80895a42bd316db3cbb335a5a38a0"
};

// Verify that Parts II & III are unchanged (no re-registration needed)
const UNCHANGED = [
    { file: "UHF_Part_II_Mathematical_Foundations.md", expectedHash: "9e240ab13b7d4879fadb4a7c262847ead4421ea64cbe0cbf7003d21591853219" },
    { file: "UHF_Part_III_Standard_Model.md", expectedHash: "9866c05e4d59dee4ec2dbbf0b31eff15756106f7b3bb237096c7fcbb0fd5f28a" }
];

async function main() {
    // 1. Verify unchanged parts
    console.log("Verifying unchanged manuscripts (Parts II & III)...\n");
    for (const p of UNCHANGED) {
        const actual = execSync(`shasum -a 256 ${p.file}`).toString().split(" ")[0];
        if (actual !== p.expectedHash) {
            console.error(`UNEXPECTED CHANGE in ${p.file}!`);
            console.error(`  Expected: ${p.expectedHash}`);
            console.error(`  Actual:   ${actual}`);
            process.exit(1);
        }
        console.log(`  ✓ ${p.file}  (unchanged)`);
    }

    // 2. Verify changed papers
    console.log("\nVerifying changed manuscripts...\n");
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

    // 3. Verify simulation zip
    console.log("\nVerifying simulation zip...\n");
    const content = fs.readFileSync(SIM_ZIP.file);
    const simHash = crypto.createHash("sha256").update(content).digest("hex");
    if (simHash !== SIM_ZIP.expectedHash) {
        console.error(`HASH MISMATCH for ${SIM_ZIP.file}!`);
        console.error(`  Expected: ${SIM_ZIP.expectedHash}`);
        console.error(`  Actual:   ${simHash}`);
        process.exit(1);
    }
    console.log(`  ✓ ${SIM_ZIP.file}  ${simHash}`);

    // 4. Connect to Polygon
    const provider = new ethers.JsonRpcProvider("https://polygon-bor-rpc.publicnode.com", 137);
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT_ADDRESS, ABI, wallet);

    console.log(`\nWallet: ${wallet.address}`);
    const balance = await provider.getBalance(wallet.address);
    console.log(`Balance: ${ethers.formatEther(balance)} MATIC\n`);

    // 5. Register each paper
    const allItems = [...PAPERS, SIM_ZIP];
    const results = [];
    for (const p of allItems) {
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

    // 6. Append to on-chain log
    const logLines = results.map(r =>
        `${r.version} | ${new Date().toISOString()} | Block: ${r.block} | TX: ${r.txHash} | Hash: ${r.hash}`
    ).join("\n") + "\n";
    fs.appendFileSync("scripts/on-chain-log.txt", logLines);

    // 7. Print summary
    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — UHF v10.0 REGISTERED ON POLYGON MAINNET");
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
