/**
 * register-v91-lexical.js — UHF Paper Registry v9.1 (Lexical Scrub)
 *
 * v9.1 Lexical Scrub (March 2026):
 *   Part I:
 *   • Abstract: Part III reference → "Extension Module A (Part III)"
 *   • §0.1: Added Input Inventory disclaimer for A7/A8 predictions
 *   Part II:
 *   • §9.3.1: "Bianchi identity in disguise" → structural compatibility with contracted Bianchi identity
 *   • §9.3.1: "uniqueness theorem of Lovelock" → "restriction theorem"; "unique" → "attractive IR fixed point"
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
        version: "9.1-Part-I-lexical",
        label: "UHF Part I — Physical Core (Lexical Scrub)",
        expectedHash: "7dc226ec219a214aa26fa839471a224a910c03881f3cc223d81f630ffd9fc1c2"
    },
    {
        file: "UHF_Part_II_Mathematical_Foundations.md",
        version: "9.1-Part-II-lexical",
        label: "UHF Part II — Mathematical Foundations (Lexical Scrub)",
        expectedHash: "9e240ab13b7d4879fadb4a7c262847ead4421ea64cbe0cbf7003d21591853219"
    },
    {
        file: "UHF_Part_III_Standard_Model.md",
        version: "9.1-Part-III-r2",
        label: "UHF Part III — Extension Module A",
        expectedHash: "9866c05e4d59dee4ec2dbbf0b31eff15756106f7b3bb237096c7fcbb0fd5f28a"
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
    console.log("SUCCESS — UHF v9.1 LEXICAL SCRUB REGISTERED ON POLYGON MAINNET");
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
