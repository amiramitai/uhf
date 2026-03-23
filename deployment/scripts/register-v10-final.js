/**
 * register-v10-final.js — Final v10.0 registration: verify Addendum PDF + register Simulation
 */
import { ethers } from "ethers";
import dotenv from "dotenv";
import fs from "fs";
import crypto from "crypto";
dotenv.config();

const CONTRACT_ADDRESS = "0xe0bB4bC3116e19F2c0c183eFf8802C4F707B0054";
const ABI = [
    "function registerPaper(string _hash, string _author, string _version) public"
];

const ADDENDUM_PDF_TX = "0x3b87f887056f2cda8900e8c19d018bedebd103dcb9d2f097292043610b4a3ebc";

const SIM_ZIP = {
    file: "out/UHF_Simulation_Suite.zip",
    version: "10.0-Simulation",
    label: "UHF Simulation Suite (3D Convergence + Hawking v5 + CFD Verification)",
    expectedHash: "494faa973288a5a23ccb63693c430f178ab80895a42bd316db3cbb335a5a38a0"
};

async function main() {
    const provider = new ethers.JsonRpcProvider("https://1rpc.io/matic", 137);
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT_ADDRESS, ABI, wallet);

    console.log(`Wallet: ${wallet.address}`);
    const balance = await provider.getBalance(wallet.address);
    console.log(`Balance: ${ethers.formatEther(balance)} MATIC\n`);

    // 1. Check Addendum PDF TX receipt
    console.log("Checking Addendum PDF TX receipt...");
    const receipt = await provider.getTransactionReceipt(ADDENDUM_PDF_TX);
    if (receipt && receipt.status === 1) {
        console.log(`  ✓ Addendum PDF TX confirmed in block ${receipt.blockNumber}\n`);
    } else if (receipt) {
        console.error(`  ✗ TX reverted!`);
        process.exit(1);
    } else {
        console.log(`  ⏳ TX still pending, waiting...\n`);
        const txResponse = await provider.getTransaction(ADDENDUM_PDF_TX);
        if (txResponse) {
            const r = await txResponse.wait();
            console.log(`  ✓ Confirmed in block ${r.blockNumber}\n`);
        }
    }

    // 2. Verify simulation zip hash
    console.log("Verifying simulation zip...");
    const content = fs.readFileSync(SIM_ZIP.file);
    const simHash = crypto.createHash("sha256").update(content).digest("hex");
    if (simHash !== SIM_ZIP.expectedHash) {
        console.error(`MISMATCH: ${SIM_ZIP.file}`);
        process.exit(1);
    }
    console.log(`  ✓ ${SIM_ZIP.file}\n`);

    // 3. Register simulation
    for (let attempt = 1; attempt <= 3; attempt++) {
        try {
            console.log(`Registering ${SIM_ZIP.label} (${SIM_ZIP.version})... [attempt ${attempt}]`);
            const tx = await contract.registerPaper(SIM_ZIP.expectedHash, "Amir Benjamin Amitay", SIM_ZIP.version);
            console.log(`  TX sent: ${tx.hash}`);
            const simReceipt = await tx.wait();
            console.log(`  Confirmed in block ${simReceipt.blockNumber}`);

            // Append all v10 results to on-chain log
            const addendumBlock = receipt ? receipt.blockNumber : "pending";
            const logLines = [
                `10.0-Part-I | 2026-03-21T02:27:00.000Z | Block: 84466587 | TX: 0x1de19b28d696600c6e640305feaad6fa45b0cb09bdc45f1a4466c4dccea17528 | Hash: 6a0894ffe8165a2fde23d3fc2bf373bf9e27f208a122532ed7e6296d4dc68986`,
                `10.0-Part-I-PDF | 2026-03-21T02:29:00.000Z | Block: 84466640 | TX: 0xbdaefd6533862f36b5e9107e9baf540a5ea1ae316a3c8260e4fc9422531b4f0f | Hash: 76106eef97d49a9fb5eae621bfe3aa7fd5fd8482630e75a51dc2bc4f124a722b`,
                `10.0-Addendum | 2026-03-21T02:29:30.000Z | Block: 84466645 | TX: 0x8d932ae96d4842d167df182e7e8dca29df0b511d18f8ae3f5ce989c783e10e62 | Hash: d06696fabcee02c6ba9fe58289454ac9e22394b99793a48703a71422f7aa591a`,
                `10.0-Addendum-PDF | 2026-03-21T02:30:00.000Z | Block: ${addendumBlock} | TX: ${ADDENDUM_PDF_TX} | Hash: 6ddbebcec9bc7d0ce92758ac2ea884d284e2227a66eb13c59d0b5ad15243b0f4`,
                `10.0-Simulation | ${new Date().toISOString()} | Block: ${simReceipt.blockNumber} | TX: ${tx.hash} | Hash: ${SIM_ZIP.expectedHash}`
            ].join("\n") + "\n";
            fs.appendFileSync("scripts/on-chain-log.txt", logLines);

            console.log("\n" + "=".repeat(70));
            console.log("SUCCESS — ALL UHF v10.0 ITEMS REGISTERED ON POLYGON MAINNET");
            console.log("=".repeat(70));
            console.log(`  Part I (md)      Block: 84466587`);
            console.log(`  Part I (PDF)     Block: 84466640`);
            console.log(`  Addendum (md)    Block: 84466645`);
            console.log(`  Addendum (PDF)   Block: ${addendumBlock}`);
            console.log(`  Simulation       Block: ${simReceipt.blockNumber}`);
            console.log("=".repeat(70));
            return;
        } catch (err) {
            console.error(`  Attempt ${attempt} failed: ${err.shortMessage || err.message}`);
            if (attempt < 3) {
                console.log(`  Waiting 10s before retry...`);
                await new Promise(r => setTimeout(r, 10000));
            }
        }
    }
    console.error("FAILED to register simulation after 3 attempts.");
    process.exit(1);
}

main().catch(err => { console.error(err); process.exit(1); });
