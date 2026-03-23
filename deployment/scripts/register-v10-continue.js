/**
 * register-v10-continue.js — Continue v10.0 registration from Part I PDF onward
 * (Part I markdown already registered at block 84466587)
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

const ITEMS = [
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

async function main() {
    // Verify hashes
    console.log("Verifying hashes...\n");
    for (const p of ITEMS) {
        const actual = execSync(`shasum -a 256 ${p.file}`).toString().split(" ")[0];
        if (actual !== p.expectedHash) { console.error(`MISMATCH: ${p.file}`); process.exit(1); }
        console.log(`  ✓ ${p.file}`);
    }
    const simContent = fs.readFileSync(SIM_ZIP.file);
    const simHash = crypto.createHash("sha256").update(simContent).digest("hex");
    if (simHash !== SIM_ZIP.expectedHash) { console.error(`MISMATCH: ${SIM_ZIP.file}`); process.exit(1); }
    console.log(`  ✓ ${SIM_ZIP.file}`);

    // Connect with retry-friendly provider (1RPC free endpoint)
    const provider = new ethers.JsonRpcProvider("https://1rpc.io/matic", 137);
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT_ADDRESS, ABI, wallet);

    console.log(`\nWallet: ${wallet.address}`);
    const balance = await provider.getBalance(wallet.address);
    console.log(`Balance: ${ethers.formatEther(balance)} MATIC\n`);

    const allItems = [...ITEMS, SIM_ZIP];
    const results = [];

    for (const p of allItems) {
        let success = false;
        for (let attempt = 1; attempt <= 3 && !success; attempt++) {
            try {
                console.log(`Registering ${p.label} (${p.version})... [attempt ${attempt}]`);
                const tx = await contract.registerPaper(p.expectedHash, "Amir Benjamin Amitay", p.version);
                console.log(`  TX sent: ${tx.hash}`);
                const receipt = await tx.wait();
                console.log(`  Confirmed in block ${receipt.blockNumber}`);
                results.push({ version: p.version, hash: p.expectedHash, txHash: tx.hash, block: receipt.blockNumber, label: p.label });
                success = true;
            } catch (err) {
                console.error(`  Attempt ${attempt} failed: ${err.shortMessage || err.message}`);
                if (attempt < 3) {
                    console.log(`  Waiting 5s before retry...`);
                    await new Promise(r => setTimeout(r, 5000));
                }
            }
        }
        if (!success) { console.error(`FAILED to register ${p.version} after 3 attempts.`); process.exit(1); }
    }

    // Append to log (include the already-confirmed Part I md)
    const priorLine = `10.0-Part-I | ${new Date().toISOString()} | Block: 84466587 | TX: 0x1de19b28d696600c6e640305feaad6fa45b0cb09bdc45f1a4466c4dccea17528 | Hash: 6a0894ffe8165a2fde23d3fc2bf373bf9e27f208a122532ed7e6296d4dc68986\n`;
    const logLines = results.map(r =>
        `${r.version} | ${new Date().toISOString()} | Block: ${r.block} | TX: ${r.txHash} | Hash: ${r.hash}`
    ).join("\n") + "\n";
    fs.appendFileSync("scripts/on-chain-log.txt", priorLine + logLines);

    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — UHF v10.0 REMAINING ITEMS REGISTERED ON POLYGON MAINNET");
    console.log("=".repeat(70));
    console.log(`\n  [PRIOR] Part I (md) — Block: 84466587 | TX: 0x1de19b28...`);
    for (const r of results) {
        console.log(`\n  ${r.label} (${r.version})`);
        console.log(`    SHA-256:     ${r.hash}`);
        console.log(`    TX Hash:     ${r.txHash}`);
        console.log(`    Block:       ${r.block}`);
        console.log(`    PolygonScan: https://polygonscan.com/tx/${r.txHash}`);
    }
    console.log("\n" + "=".repeat(70));
}

main().catch(err => { console.error(err); process.exit(1); });
