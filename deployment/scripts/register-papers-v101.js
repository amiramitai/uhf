/**
 * register-papers-v101.js — Golden Master re-registration (Papers 4–7 only)
 *
 * Re-registers the 4 exploratory preprint PDFs after data injection + tone revision.
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
        file: "uhf_physics/papers/paper4/Paper4_Superfluid_Cosmology.pdf",
        version: "10.1-Paper4-PDF",
        label: "Paper 4 — Heuristic Superfluid Cosmology",
        expectedHash: "6736b5b3d00e7f4f0035b4088235b034e87e3e5a4edc322965b56ff127a6b8b1"
    },
    {
        file: "uhf_physics/papers/paper5/Paper5_Topological_Standard_Model.pdf",
        version: "10.1-Paper5-PDF",
        label: "Paper 5 — Torus Knot / Fermion Correspondences",
        expectedHash: "638fd38aec72d4edd4c9f763eead924bcd3beddf7802cae9f4d5d546d12ad033"
    },
    {
        file: "uhf_physics/papers/paper6/Paper6_Topological_Chromodynamics.pdf",
        version: "10.1-Paper6-PDF",
        label: "Paper 6 — Borromean Vortex Confinement Analogue",
        expectedHash: "8bf7141542e8571115d4db408ea17d7bcfff498a31ac852676f76049ced707d9"
    },
    {
        file: "uhf_physics/papers/paper7/Paper7_Quantum_Entanglement.pdf",
        version: "10.1-Paper7-PDF",
        label: "Paper 7 — Bell-Inequality Violation (Phase Synchronisation)",
        expectedHash: "b2dc777384543de3737a96b6026cdeafa649cf0f4ec3d85afc5e1ef8f2cfced1"
    }
];

async function main() {
    console.log("Verifying SHA-256 hashes for Papers 4–7...\n");
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

    const provider = new ethers.JsonRpcProvider("https://polygon-bor-rpc.publicnode.com", 137);
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT_ADDRESS, ABI, wallet);

    console.log(`\nWallet: ${wallet.address}`);
    const balance = await provider.getBalance(wallet.address);
    console.log(`Balance: ${ethers.formatEther(balance)} MATIC\n`);

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

    const logLines = results.map(r =>
        `${r.version} | ${new Date().toISOString()} | Block: ${r.block} | TX: ${r.txHash} | Hash: ${r.hash}`
    ).join("\n") + "\n";
    fs.appendFileSync("scripts/on-chain-log.txt", logLines);

    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — PAPERS 4–7 RE-REGISTERED ON POLYGON MAINNET (v10.1)");
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
