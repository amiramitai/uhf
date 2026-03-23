/**
 * register-v86.js — UHF Paper Registry v8.6
 *
 * Final QFT-level integration:
 *   • Part II  — §9.3.5 III-C: BRST-Lindblad Commutativity (Proof F)
 *                 Q_B² = 0, [Q_B, L_k]|_phys = 0, Slavnov-Taylor preserved,
 *                 Proca masses m_γ = m_g = 0 (exact, BRST-protected)
 *   • Part III — §9.3.25: Emergent Yang-Mills from GP Energy (Proof G)
 *                 A_μ^a = (1/g)∂_μθ^a, F_μν^a derived, E_GP → (1/4g²)FF,
 *                 g_YM² = 2/ρ₀^{3/2}
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
        version: "8.6-Part-I",
        label: "UHF Part I — Physical Core",
        expectedHash: "18454ef4e5cd27eee16ae68c09d0eaad48ffd94136d6b3540c61019840a54c82"
    },
    {
        file: "UHF_Part_II_Mathematical_Foundations.md",
        version: "8.6-Part-II",
        label: "UHF Part II — Mathematical Foundations",
        expectedHash: "1bbeb9a0a2c19c36f5a655c11057ccac13367fbd523f0b62071e608abb4b5d80"
    },
    {
        file: "UHF_Part_III_Standard_Model.md",
        version: "8.6-Part-III",
        label: "UHF Part III — Standard Model Extension",
        expectedHash: "480c3e0c74df7792d69ff85b9dfe8f75591e4525236ddbce6c0c3bb718d45d76"
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
    console.log("SUCCESS — UHF v8.6 (ALL 3 PARTS) REGISTERED ON POLYGON MAINNET");
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
